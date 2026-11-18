"""Quota enforcement — the fence around the two meters.

THE DEGRADE ORDER (founder doctrine: memory is an enhancement, never a
gate; the archive is sacred):
  - exchanges over cap        → ARCHIVE STILL WRITES; learning pauses
                                (no curation enqueue, no gate saves, no
                                summarize) until the period rolls over.
  - storage ≥ 100% of cap     → same pause (documents grow storage too),
                                message says prune or upgrade.
  - storage ≥ 120% of cap     → new archive writes are REFUSED with a
                                clear status. The client keeps its spool
                                and retries — data waits, it is never
                                lost. 20% grace exists precisely so chat
                                never bounces in normal operation.
Deletion never happens on our side, at any stage. "Buy more or prune,
user's choice, never silent deletion."

WHO IS EXEMPT: static key-file tenants (the founder's own) resolve no
plan and carry no tenant_plan row → every check passes. Enforcement
applies exactly to tenants whose plan the control plane states.

BRAIN_ENFORCE_CAPS=0 turns every verdict into a log line (metering-first
mode) without changing any behavior — the kill-switch if a cap misfires
in the field.
"""
from __future__ import annotations

import datetime as _dt
import os
import time
import traceback

from . import store, tiers

_DDL = """
CREATE TABLE IF NOT EXISTS tenant_plan (
    one         boolean PRIMARY KEY DEFAULT true CHECK (one),
    plan        text NOT NULL,
    state       text NOT NULL DEFAULT 'active',
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS quota_usage (
    period      text PRIMARY KEY,
    exchanges   bigint NOT NULL DEFAULT 0,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE tenant_plan ADD COLUMN IF NOT EXISTS pack_credits bigint NOT NULL DEFAULT 0;
ALTER TABLE tenant_plan ADD COLUMN IF NOT EXISTS extra_mb bigint NOT NULL DEFAULT 0;
ALTER TABLE quota_usage ADD COLUMN IF NOT EXISTS pack_used bigint NOT NULL DEFAULT 0;
ALTER TABLE quota_usage ADD COLUMN IF NOT EXISTS wall_pushed timestamptz;
"""
# pack_credits/extra_mb (Lemon Squeezy top-ups, 2026-08-20): the control
# plane states the LIFETIME-purchased totals; pack CONSUMPTION is ledgered
# here per period (quota_usage.pack_used), so credits roll over months and a
# replayed control read can never refund what a past period already spent.

WARN_AT = 0.80
ARCHIVE_REFUSE_AT = 1.20

# An EMPTY tenant database measures ~8.6 MB (Postgres base + our schema and
# indexes) — measured on a fresh tenant in the 2026-07-30 stranger E2E, where
# a brand-new Free user saw "43% of storage used" before storing a word.
# Founder: "subtract the baseline from the gauge" — storage meters CONTENT,
# not database overhead. Env-overridable if the schema grows.
# The size of a freshly provisioned, EMPTY tenant — the whole schema (27
# tables, 55 indexes) with zero rows — measured 2026-09-04 on Postgres 17 +
# pgvector by cloning the live schema into a scratch database: 9.0 MB. It is
# subtracted so a new account starts at zero and its cap is all its own.
BASELINE_MB = float(os.environ.get("BRAIN_STORAGE_BASELINE_MB", "9.0"))


def _enforcing() -> bool:
    return os.environ.get("BRAIN_ENFORCE_CAPS", "1").strip() != "0"


def period_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m")


def note_plan(plan: str, ents: dict | None = None) -> None:
    """Record the caller's plan (and purchased top-ups, when the key carried
    them) in ITS OWN tenant DB so background workers (which hold a tenant,
    not a key) can enforce too. Bound tenant context. Cheap: single-row
    upsert, skipped unless changed or stale."""
    if not plan:
        return
    pack = int((ents or {}).get("pack_credits") or 0)
    extra = int((ents or {}).get("extra_storage_mb") or 0)
    try:
        with store.connection() as conn:
            cur = conn.cursor()
            store.ensure_ddl(cur, "quota", _DDL)
            if ents is None:
                # Plan-only caller (no key context): never zero out top-ups
                # someone paid for just because this path can't see them.
                cur.execute(
                    "INSERT INTO tenant_plan (plan) VALUES (%s) "
                    "ON CONFLICT (one) DO UPDATE SET plan=EXCLUDED.plan, "
                    "  updated_at=now() "
                    "WHERE tenant_plan.plan IS DISTINCT FROM EXCLUDED.plan "
                    "   OR tenant_plan.updated_at < now() - interval '1 hour';",
                    [plan])
            else:
                cur.execute(
                    "INSERT INTO tenant_plan (plan, pack_credits, extra_mb) "
                    "VALUES (%s, %s, %s) "
                    "ON CONFLICT (one) DO UPDATE SET plan=EXCLUDED.plan, "
                    "  pack_credits=EXCLUDED.pack_credits, "
                    "  extra_mb=EXCLUDED.extra_mb, updated_at=now() "
                    "WHERE tenant_plan.plan IS DISTINCT FROM EXCLUDED.plan "
                    "   OR tenant_plan.pack_credits IS DISTINCT FROM "
                    "      EXCLUDED.pack_credits "
                    "   OR tenant_plan.extra_mb IS DISTINCT FROM "
                    "      EXCLUDED.extra_mb "
                    "   OR tenant_plan.updated_at < now() - interval '1 hour';",
                    [plan, pack, extra])
            conn.commit()
    except Exception:
        traceback.print_exc()


def effective_plan(key_plan: str | None = None) -> str | None:
    """The plan that governs this tenant: the request key's plan if the
    caller carried one, else the stored tenant_plan row (workers), else
    None = exempt (static/owner tenants)."""
    if key_plan:
        return key_plan
    try:
        with store.connection() as conn:
            cur = conn.cursor()
            store.ensure_ddl(cur, "quota", _DDL)
            conn.commit()
            cur.execute("SELECT plan FROM tenant_plan LIMIT 1;")
            row = cur.fetchone()
            return row[0] if row else None
    except Exception:
        traceback.print_exc()
        return None


def stored_entitlements() -> tuple[int, int]:
    """(pack_credits, extra_mb) from the tenant_plan row — the fallback for
    callers with no key context (workers, MCP without fresh ents). Missing
    row or error = (0, 0): the fail-closed direction."""
    try:
        with store.connection() as conn:
            cur = conn.cursor()
            store.ensure_ddl(cur, "quota", _DDL)
            conn.commit()
            cur.execute("SELECT pack_credits, extra_mb FROM tenant_plan LIMIT 1;")
            row = cur.fetchone()
            return (int(row[0]), int(row[1])) if row else (0, 0)
    except Exception:
        traceback.print_exc()
        return (0, 0)


def pack_ledger(total_credits: int, ex_cap: int) -> int:
    """Available pack credits ENTERING this period = lifetime purchased minus
    what CLOSED periods consumed. Also refreshes the CURRENT period's
    pack_used ledger row (overage past ex_cap, clamped to what was available)
    — recomputed idempotently on every call, so retries and redeliveries can
    never double-spend. Bound tenant context."""
    try:
        with store.connection() as conn:
            cur = conn.cursor()
            store.ensure_ddl(cur, "quota", _DDL)
            cur.execute(
                "SELECT COALESCE(SUM(pack_used), 0) FROM quota_usage "
                "WHERE period <> %s;", [period_now()])
            consumed_closed = int(cur.fetchone()[0])
            avail = max(0, int(total_credits) - consumed_closed)
            cur.execute(
                "UPDATE quota_usage SET pack_used = LEAST(GREATEST("
                "  exchanges - %s, 0), %s), updated_at = now() "
                "WHERE period = %s;",
                [int(ex_cap), avail, period_now()])
            conn.commit()
            return avail
    except Exception:
        traceback.print_exc()
        return 0


def bump_exchanges(n: int = 1) -> int:
    """Count n exchanges against the current period. Returns the new total.
    Bound tenant context. Counting is unconditional — metering-first: the
    numbers accumulate whether or not enforcement acts on them."""
    with store.connection() as conn:
        cur = conn.cursor()
        store.ensure_ddl(cur, "quota", _DDL)
        cur.execute(
            "INSERT INTO quota_usage (period, exchanges) VALUES (%s, %s) "
            "ON CONFLICT (period) DO UPDATE SET "
            "  exchanges = quota_usage.exchanges + EXCLUDED.exchanges, "
            "  updated_at = now() RETURNING exchanges;",
            [period_now(), int(n)])
        total = cur.fetchone()[0]
        conn.commit()
    return total


def used_exchanges() -> int:
    with store.connection() as conn:
        cur = conn.cursor()
        store.ensure_ddl(cur, "quota", _DDL)
        conn.commit()
        cur.execute("SELECT exchanges FROM quota_usage WHERE period=%s;",
                    [period_now()])
        row = cur.fetchone()
        return int(row[0]) if row else 0


def storage_mb() -> float:
    """CONTENT megabytes — raw database size minus the empty-DB baseline."""
    with store.connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT pg_database_size(current_database());")
        return max(0.0, cur.fetchone()[0] / 1048576.0 - BASELINE_MB)


def decide(plan: str | None, used: int, db_mb: float,
           pack_avail: int = 0, extra_mb: int = 0) -> dict:
    """PURE verdict — all thresholds in one testable place.

    Returns {learn: bool, archive: bool, warn: str}. learn=False pauses
    curation/gate/summarize; archive=False refuses new chat writes.
    pack_avail = purchased exchange credits still unspent entering this
    period (they extend the exchange cap and roll over); extra_mb =
    purchased storage that widens the storage cap."""
    if plan is None:
        return {"learn": True, "archive": True, "warn": ""}
    c = tiers.caps(plan)
    ex_cap = c["exchanges"]
    st_cap = c["storage_mb"] + max(0, int(extra_mb))
    ex_eff = ex_cap + max(0, int(pack_avail))
    if db_mb >= st_cap * ARCHIVE_REFUSE_AT:
        return {"learn": False, "archive": False,
                "warn": (f"storage {db_mb:.0f} MB is far over your "
                         f"{st_cap} MB plan — new history is ON HOLD until "
                         f"you free space (cleanup_history) or upgrade.")}
    if db_mb >= st_cap:
        return {"learn": False, "archive": True,
                "warn": (f"storage {db_mb:.0f}/{st_cap} MB — memory has "
                         f"stopped learning; free space (cleanup_history) "
                         f"or upgrade.")}
    if used >= ex_eff:
        spent_credits = " (top-up credits included)" if pack_avail > 0 else ""
        return {"learn": False, "archive": True,
                "warn": (f"monthly allowance used ({used}/{ex_eff} "
                         f"exchanges{spent_credits}) — memory has stopped "
                         f"learning until next period; upgrade or buy a "
                         f"top-up pack for more.")}
    if used >= ex_cap:
        return {"learn": True, "archive": True,
                "warn": (f"plan allowance used — running on top-up credits "
                         f"({ex_eff - used} left).")}
    warn = ""
    if used >= ex_eff * WARN_AT:
        warn = f"heads up: {used}/{ex_eff} exchanges used this period."
    elif db_mb >= st_cap * WARN_AT:
        warn = f"heads up: storage {db_mb:.0f}/{st_cap} MB."
    return {"learn": True, "archive": True, "warn": warn}


def check(key_plan: str | None = None, spend: int = 0,
          ents: dict | None = None) -> dict:
    """The one call sites use. Bound tenant context. Counts `spend`
    exchanges, then returns the verdict — with enforcement off, verdicts
    are forced permissive and only logged. ents = the key's purchased
    top-ups when the caller has them fresh from auth; None falls back to
    the tenant_plan row (workers, older callers)."""
    plan = effective_plan(key_plan)
    if plan is None:
        return {"learn": True, "archive": True, "warn": "", "plan": None}
    if ents is not None:
        pack_total = int(ents.get("pack_credits") or 0)
        extra_mb = int(ents.get("extra_storage_mb") or 0)
    else:
        pack_total, extra_mb = stored_entitlements()
    used = bump_exchanges(spend) if spend else used_exchanges()
    ex_cap = tiers.caps(plan)["exchanges"]
    pack_avail = pack_ledger(pack_total, ex_cap) if pack_total > 0 else 0
    v = decide(plan, used, storage_mb(), pack_avail=pack_avail,
               extra_mb=extra_mb)
    v["plan"] = plan
    if not _enforcing() and not (v["learn"] and v["archive"]):
        print(f"[quota] LOG-ONLY ({store.current_tenant()}/{plan}): "
              f"{v['warn']}", flush=True)
        return {"learn": True, "archive": True, "warn": "", "plan": plan}
    return v


# ── THE DOOR (founder 2026-08-24: full or off, never partial) ───────────────
# A brain at partial power is the product's worst version shown to exactly
# the people deciding whether to pay — the same law that keeps partial
# harnesses off the website. So the quota verdict maps to ONE account state
# enforced at the doors (hooks + MCP dispatch), and out-of-quota closes
# EVERYTHING with a notice instead of degrading anything. The wall moment is
# the conversion surface: the notice says what is missing and the 60-second
# fix, because the user's next session starts with a suddenly-dumb agent and
# that panic is when they decide.
# THE THREE WALLS (founder 2026-08-25). Each meter cuts only what it
# funds: the allowance funds INFERENCE, so it stops learning; storage
# funds DISK, so it stops recording. RECALL IS NEVER CUT BY A LIMIT —
# it is mechanical, costs us CPU and no tokens, and it is the customer's
# own memory. Only non-payment takes it away, and even then only when
# the data leaves live systems.
#
# Every notice carries the GAP WARNING, and it is disclosure, not a
# threat: we never learn from old conversation (rules:never_back_curate_
# old_chat), so a gap is permanent as MEMORY and the customer inherits
# the repair. Saying so is both the honest thing and the strongest
# reason to stay inside the plan.
#
# NEVER advise freeing space: deletion is deliberately gated behind
# confirmation, login and 2FA, so sending someone at a wall into it is
# not help. Every wall sells exactly two one-click things.
_GAP = ("That gap is permanent: we never go back and learn from old "
        "conversation, so the longer this lasts the further your memory "
        "drifts from your work — and when you resume, your agent has to "
        "correct outdated memories itself.")
WALL_NOTICE = (
    "Your Brain has stopped LEARNING — this plan's monthly allowance is "
    "spent. Your history is still being recorded and recall still works, "
    "but nothing new is being learned from it. " + _GAP + " Resume now: "
    "buy a pack or upgrade at brethof.ai/account.")
STORAGE_NOTICE = (
    "Your Brain has stopped RECORDING — stored memory is at this plan's "
    "storage limit. Recall still works over everything already stored, but "
    "nothing new is being saved. " + _GAP + " Add storage or upgrade at "
    "brethof.ai/account.")
OFF_NOTICE = (
    "Your Brain is off for new memory — the monthly allowance is spent AND "
    "storage is full. Recall still works, so everything you have stored "
    "still answers, but nothing new is recorded or learned. " + _GAP + " "
    "Reactivate in 60 seconds at brethof.ai/account.")
# THE BILLING LINE (founder, 2026-08-24, final — after a day of grace
# periods, cool-offs and cadence windows were all tried and killed):
# "Paid works. Not paid stopped. No cadency, no cooldown period, nothing."
# A grace window is not kindness, it is a bill nobody wants: charge from
# the period start and the customer complains about days they were cut
# off; charge from the payment day and we eat the gap. Nobody in the
# industry does it, so neither do we. The moment the paid period ends
# unpaid, the door closes — and the moment they pay, it opens from that
# day. Frozen NEVER means demoted-to-free: a paid account's memory is far
# over the free cap, so "free tier" would be fiction. What a frozen
# account keeps is what is theirs — export, delete a project, delete
# everything. Those are never blocked.
FROZEN_NOTICE = (
    "Your Brain is paused — the subscription is not active. Every memory "
    "is intact and waiting: reactivate at brethof.ai/account and it "
    "resumes immediately. You can export or delete your data at any time.")
# Any state that is not a paid, current subscription. Unknown states are
# NOT here on purpose — an unrecognised value fails OPEN, because a
# billing-state bug must never lock out someone who paid.
CLOSED_STATES = {"frozen", "past_due", "unpaid", "expired", "suspended",
                 "cancelled"}


def door(key_plan: str | None = None, ents: dict | None = None,
         state: str | None = None) -> tuple[str, str]:
    """One state for the whole account: 'active' | 'exhausted' |
    'storage_full' | 'frozen' — with the notice the closed door shows.
    Bound tenant context. Exempt/plan-less tenants and enforcement-off are
    always active (check() already answers permissively for those).

    `state` is the SUBSCRIPTION state from the control plane, checked
    FIRST and without touching the meters: an unpaid account is closed
    whatever its usage says. There is no dunning branch — paid works, not
    paid stopped."""
    if state in CLOSED_STATES:
        return "frozen", FROZEN_NOTICE
    v = check(key_plan, ents=ents)
    if v["learn"] and v["archive"]:
        return "active", v.get("warn", "")
    # INSTANT WALL (founder 2026-08-24): the first refusal of a period fires
    # this tenant's gauges to the control plane NOW, so the suspended email
    # leaves within seconds of the wall, not on the 6-hour sweep — "they
    # will send fucking 50 emails with threats before we send them the
    # message". Once per period, claimed atomically; failures are loud and
    # the sweep remains the safety net.
    try:
        _wall_push_once()
    except Exception:                                     # noqa: BLE001
        traceback.print_exc()
    if not v["archive"] and not v["learn"]:
        return "off", OFF_NOTICE + " (" + v["warn"] + ")"
    if not v["archive"]:
        return "storage_full", STORAGE_NOTICE + " (" + v["warn"] + ")"
    return "exhausted", WALL_NOTICE + " (" + v["warn"] + ")"


# THE HORIZON — what the memory does NOT know, said out loud.
#
# A limited account KEEPS its recall; that is deliberate, and it is also
# the danger. Recall looks exactly as authoritative as it did last month
# while the world moved on, so the agent acts on a picture that quietly
# stopped updating and nobody can see the gap. Stale memory served
# confidently is worse than no memory at all — it is the one failure a
# memory product cannot survive.
#
# TWO CLOCKS STOP, in two different states, so there are two sentences:
#   storage_full / off — RECORDING stopped. Nothing after the last
#       archived turn exists anywhere, raw or curated.
#   exhausted — recording continues but LEARNING stopped. The raw history
#       is current; the distilled memory is frozen behind it.
# Naming the date is the whole point: "your Brain is behind" is a shrug,
# "your Brain knows nothing after the 12th" is actionable.
_HORIZON_TTL_S = 60.0
_horizon_cache: dict[str, tuple[float, str]] = {}


def _newest(sql: str) -> str:
    with store.connection() as conn:
        cur = conn.cursor()
        cur.execute(sql)
        row = cur.fetchone()
    if not row or not row[0]:
        return ""
    return str(row[0])[:10]


def horizon(state: str) -> str:
    """The date this account's memory stopped moving, or "" if it hasn't.

    Cached briefly per tenant: this runs on EVERY prompt of a limited
    account, and a wall notice must never become the expensive part of a
    request. Never raises — a horizon we cannot compute must not cost the
    customer their recall."""
    if state not in ("exhausted", "storage_full", "off"):
        return ""
    try:
        tenant = store.current_tenant()
        key = f"{tenant}:{state}"
        now = time.time()
        hit = _horizon_cache.get(key)
        if hit and now - hit[0] < _HORIZON_TTL_S:
            return hit[1]
        if state == "exhausted":
            # ORDER BY the primary key, not a scan over updated_at: this is
            # on the request path of every prompt.
            val = _newest("SELECT updated_at FROM document "
                          "ORDER BY updated_at DESC LIMIT 1")
        else:
            val = _newest("SELECT ts FROM chat ORDER BY id DESC LIMIT 1")
        _horizon_cache[key] = (now, val)
        return val
    except Exception:                                     # noqa: BLE001
        traceback.print_exc()
        return ""


def horizon_sentence(state: str) -> str:
    """The long form, for the session-start notice."""
    when = horizon(state)
    if not when:
        return ""
    if state == "exhausted":
        return (f"Your memory is frozen at {when} — everything since then "
                f"is recorded and searchable, but nothing new has been "
                f"learned from it.")
    return (f"Your memory knows nothing after {when} — nothing has been "
            f"recorded since.")


# THE PER-PROMPT LINE. Rides inside the CUSTOMER'S OWN context window on
# every prompt while limited, so the gap being dug is visible now instead
# of discovered in three weeks. One line, hard — the full wall text is
# session-start only; taxing their context is not how we win them back.
_PROMPT_LINE = {
    "exhausted": "⚠ Brain is not learning — monthly allowance spent{when}. "
                 "History is still recorded, recall still works, but no new "
                 "memories are being made. brethof.ai/account",
    "storage_full": "⚠ Brain is not recording — storage full{when}. Recall "
                    "still works. brethof.ai/account",
    "off": "⚠ Brain is off for new memory — allowance spent and storage "
           "full{when}. Recall still works. brethof.ai/account",
    "frozen": "⚠ Brain is paused — subscription inactive. Recall works "
              "until your memory moves to cold storage. brethof.ai/account",
}


def prompt_line(state: str) -> str:
    """ONE line, on every prompt of a limited account — it rides inside the
    CUSTOMER'S own context window, so length is a cost we pay for them.
    Carries the horizon date where there is one, because a date is
    actionable and "you are behind" is not."""
    tmpl = _PROMPT_LINE.get(state, "")
    if not tmpl:
        return ""
    when = horizon(state)
    return tmpl.format(when=f", frozen at {when}" if when else "")


# WHAT EACH CLOSED STATE STILL ALLOWS. One place, so a caller can never
# invent its own reading of the door — the 2026-08-25 design in three
# predicates.
def may_record(state: str) -> bool:
    """Writing the turn down. Costs disk they bought and no tokens, so
    only a full store or an unpaid account stops it."""
    return state in ("active", "exhausted")


def may_learn(state: str) -> bool:
    """Curation, ledger, graph, teaching — everything that burns
    inference. Only a fully active account gets it."""
    return state == "active"


def may_recall(state: str) -> bool:
    """ALWAYS TRUE, deliberately. Recall is mechanical (vector + BM25 on
    our own CPU) and it is the customer's own memory. No limit takes it
    away; non-payment does not either, because a frozen tenant stops
    being served when its database leaves the box at day 7 — physics,
    not policy. Kept as a function so the intent is greppable and a
    future caller cannot quietly gate recall behind a meter."""
    return True


def snapshot(plan: str | None = None, pack_credits: int | None = None,
             extra_mb: int | None = None) -> dict:
    """LIVE usage picture for the account panel (founder 2026-08-24: "we are
    the only cloud provider which is doing statistics, not live"). Bound
    tenant context.

    A read-only VIEW of the same numbers the door enforces on: it never
    counts an exchange and never fires the wall push. The control plane
    passes plan + purchased top-ups from the subscription row it owns, so
    even enforcement-exempt (static-key) tenants — which store no
    tenant_plan row on purpose — get an honest display against their plan's
    caps. Absent those arguments it falls back to the stored row, exactly
    like check() does for workers.

    The one write inside: pack_ledger()'s idempotent refresh of the current
    period's pack_used row — the recompute check() would do anyway, so the
    panel and the door always show the same pack arithmetic."""
    plan_eff = tiers.PLAN_ALIASES.get(plan, plan) if plan \
        else effective_plan(None)
    used = used_exchanges()
    db_mb = storage_mb()
    out: dict = {"plan": plan_eff, "period": period_now(),
                 "exchanges": {"used": used, "cap": None},
                 "storage": {"used_mb": round(db_mb, 2), "cap_mb": None},
                 "pack": None, "state": "active"}
    if plan_eff is None:
        return out
    if pack_credits is not None:
        pack_total = max(0, int(pack_credits or 0))
        extra = max(0, int(extra_mb or 0))
    else:
        pack_total, extra = stored_entitlements()
    c = tiers.caps(plan_eff)
    ex_cap = c["exchanges"]
    st_cap = c["storage_mb"] + extra
    pack_avail = pack_ledger(pack_total, ex_cap) if pack_total > 0 else 0
    pack_used_total = 0
    if pack_total > 0:
        with store.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COALESCE(SUM(pack_used), 0) "
                        "FROM quota_usage;")
            pack_used_total = int(cur.fetchone()[0])
    ex_eff = ex_cap + pack_avail
    v = decide(plan_eff, used, db_mb, pack_avail=pack_avail, extra_mb=extra)
    if not v["archive"]:
        state = "storage_full"
    elif not v["learn"]:
        state = "exhausted"
    else:
        state = "active"
    out.update({
        "state": state,
        "exchanges": {"used": used, "cap": ex_cap,
                      "pack_available": pack_avail,
                      "effective_cap": ex_eff,
                      "pct": round(used / ex_eff * 100, 1) if ex_eff else 0},
        "storage": {"used_mb": round(db_mb, 2), "cap_mb": st_cap,
                    "extra_mb": extra,
                    "pct": round(db_mb / st_cap * 100, 1) if st_cap else 0},
        "pack": ({"purchased": pack_total,
                  "used": min(pack_used_total, pack_total),
                  "remaining": max(0, pack_total - pack_used_total)}
                 if pack_total > 0 else None),
    })
    return out


def _wall_push_once() -> None:
    """Atomically claim this period's one wall push, then fire the flush in
    a daemon thread. The UPDATE-where-NULL is the claim: under concurrent
    refusals exactly one caller wins; a period row is guaranteed first so a
    storage-walled tenant with zero exchanges still notifies."""
    period = period_now()
    tenant = store.current_tenant()
    with store.connection() as conn:
        cur = conn.cursor()
        store.ensure_ddl(cur, "quota", _DDL)
        cur.execute("INSERT INTO quota_usage (period) VALUES (%s) "
                    "ON CONFLICT (period) DO NOTHING;", [period])
        cur.execute("UPDATE quota_usage SET wall_pushed = now() "
                    "WHERE period = %s AND wall_pushed IS NULL;", [period])
        claimed = cur.rowcount
        conn.commit()
    if claimed:
        from core import metering
        metering.flush_tenant_async(tenant)
        print(f"[quota] WALL — instant gauge push fired for {tenant} "
              f"({period})", flush=True)
