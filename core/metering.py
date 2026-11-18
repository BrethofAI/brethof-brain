"""Usage metering — v2 flushes per-tenant numbers to the control plane.

FOUNDER, 2026-07-28: "we need usage metrics to be able to set values for
plans." The July-24 price points were set before the write gate and
server-side curation existed; this is the instrument that lets the new plan be
set from MEASURED cost per tenant instead of guesses.

Two kinds, matching the control plane's ingest contract exactly:
- GAUGES (kind=gauge, period ''): db_size_mb — `pg_database_size()` is the
  exact per-customer number database-per-tenant was chosen for — plus
  curated_records and chat_rows. Sent absolute; the control plane overwrites.
- MONTHLY counters (period YYYY-MM): inference tokens by source (curate/gate),
  summed from the local usage_event table each flush. Sent absolute
  period-to-date; the control plane max()es, so a retried flush is harmless.

Failures degrade, never gate: a control-plane blip costs one flush cycle,
events stay in usage_event and the next flush carries them.
"""
from __future__ import annotations

import datetime as _dt
import json
import threading
import time
import traceback
import urllib.request

from core import auth, store

FLUSH_S = 6 * 3600.0
STARTUP_DELAY_S = 120.0

_DDL = """
CREATE TABLE IF NOT EXISTS usage_event (
    id      bigserial PRIMARY KEY,
    kind    text NOT NULL,
    tokens_in  bigint NOT NULL DEFAULT 0,
    tokens_out bigint NOT NULL DEFAULT 0,
    ts      timestamptz NOT NULL DEFAULT now()
);
"""


_TOOL_DDL = """
CREATE TABLE IF NOT EXISTS tool_call (
    day   date NOT NULL DEFAULT CURRENT_DATE,
    tool  text NOT NULL,
    calls int  NOT NULL DEFAULT 0,
    PRIMARY KEY (day, tool)
);
"""

# The behavioral question of 2026-08-05: "models now not use memory" — these
# are the tools whose per-session use answers it. Counted, never contented.
READ_TOOLS = ["recall", "get_memory", "search_memory", "semantic_search",
              "search_chat", "search_chat_text", "search_history", "graph",
              "list_memory", "recent_records", "load_project", "query_raw",
              "session_context", "list_rules", "list_projects"]


def count_tool(tool: str) -> None:
    """One MCP tool call, inside a bound tenant context. Instrumentation for
    judging OUR injection design (do agents actually reach for memory?) —
    aggregate counts only, nothing customer-visible, must never break the
    call it counts."""
    try:
        with store.connection() as conn:
            cur = conn.cursor()
            cur.execute(_TOOL_DDL)
            cur.execute(
                "INSERT INTO tool_call (day, tool, calls) "
                "VALUES (CURRENT_DATE, %s, 1) ON CONFLICT (day, tool) "
                "DO UPDATE SET calls = tool_call.calls + 1;", [tool])
            conn.commit()
    except Exception:
        pass


# Inference completions whose usage row could NOT be written, on a tenant
# that still EXISTS. This is billed work we can no longer bill for, and it
# was previously a printed traceback and nothing else — the same silent
# shape as the July flush that had never once run. /v1/health publishes it
# and the sentinel alarms on it, so the number has somewhere to be seen.
_lost = {"events": 0, "tokens": 0, "last": ""}
_lost_lock = threading.Lock()


def lost() -> dict:
    with _lost_lock:
        return dict(_lost)


def record(kind: str, tokens_in: int, tokens_out: int) -> None:
    """One inference completion, inside a bound tenant context. Best-effort:
    metering must never break the work it measures — but a failure on a LIVE
    tenant is unbilled revenue, so it gets counted, not just printed."""
    tin, tout = int(tokens_in or 0), int(tokens_out or 0)
    try:
        with store.connection() as conn:
            cur = conn.cursor()
            store.ensure_ddl(cur, "metering", _DDL)
            cur.execute("INSERT INTO usage_event (kind, tokens_in, tokens_out) "
                        "VALUES (%s, %s, %s);", [kind, tin, tout])
            conn.commit()
    except store.TenantGone:
        # The account was deleted mid-job. There is nothing to bill and
        # nobody to bill it to — expected, not a fault.
        pass
    except Exception as e:                                # noqa: BLE001
        with _lost_lock:
            _lost["events"] += 1
            _lost["tokens"] += tin + tout
            _lost["last"] = f"{kind}: {type(e).__name__}: {e}"[:200]
        print(f"[metering] LOST {kind} {tin}+{tout} tokens — "
              f"{type(e).__name__}: {str(e)[:120]}", flush=True)
        traceback.print_exc()


def _tenant_counters(tenant: str) -> list[dict]:
    period = _dt.date.today().strftime("%Y-%m")
    tok = store.bind(tenant)
    try:
        with store.connection() as conn:
            cur = conn.cursor()
            store.ensure_ddl(cur, "metering", _DDL)
            conn.commit()
            cur.execute("SELECT pg_database_size(current_database());")
            # CONTENT size, not raw database size: the ~8.6 MB empty-DB
            # baseline is overhead, not the customer's memory (founder
            # 2026-07-30, after the stranger E2E showed a fresh Free tenant
            # at "43% used"). Same subtraction quota.decide() uses — the
            # billed gauge and the enforced number stay one number.
            from core.quota import BASELINE_MB
            size_mb = max(0.0, cur.fetchone()[0] / 1048576.0 - BASELINE_MB)
            cur.execute("SELECT (SELECT count(*) FROM document), "
                        "       (SELECT count(*) FROM chat), "
                        "       (SELECT max(ts) FROM chat);")
            docs, chats, last_ts = cur.fetchone()
            cur.execute(
                "SELECT kind, coalesce(sum(tokens_in),0), "
                "       coalesce(sum(tokens_out),0) FROM usage_event "
                "WHERE to_char(ts, 'YYYY-MM') = %s GROUP BY kind;", [period])
            tok_rows = cur.fetchall()
            # Memory-read activity: the number behind "do agents use
            # memory". Tenants without the table simply report none.
            reads = 0
            try:
                cur.execute("SELECT coalesce(sum(calls), 0) FROM tool_call "
                            "WHERE to_char(day, 'YYYY-MM') = %s "
                            "AND tool = ANY(%s);", [period, READ_TOOLS])
                reads = int(cur.fetchone()[0])
            except Exception:
                conn.rollback()
            # The billed meter itself (2026-07-30) — monthly, so the panel
            # can show "6,412 / 8,000 exchanges" from the same number the
            # data plane enforces on. Tenants predating the table have none.
            exchanges = 0
            plan = None
            try:
                cur.execute("SELECT exchanges FROM quota_usage "
                            "WHERE period = %s;", [period])
                row = cur.fetchone()
                exchanges = int(row[0]) if row else 0
                cur.execute("SELECT plan FROM tenant_plan LIMIT 1;")
                row = cur.fetchone()
                plan = row[0] if row else None
            except Exception:
                conn.rollback()
    finally:
        store.unbind(tok)
    out = [
        {"counter": "db_size_mb", "period": "", "kind": "gauge",
         "value": round(size_mb, 2)},
        {"counter": "curated_records", "period": "", "kind": "gauge",
         "value": docs},
        {"counter": "chat_rows", "period": "", "kind": "gauge",
         "value": chats},
    ]
    # LIFECYCLE INPUT (2026-08-09): the control plane drives the founder's
    # archive ladder (free: 30d inactive; churned: 30d grace) from this
    # number. A tenant with no chat yet sends none — the control plane
    # falls back to account age for never-used accounts.
    if last_ts is not None:
        idle_days = max(0.0, (_dt.datetime.now(_dt.timezone.utc)
                              - last_ts).total_seconds() / 86400.0)
        out.append({"counter": "days_inactive", "period": "",
                    "kind": "gauge", "value": round(idle_days, 1)})
    for kind, tin, tout in tok_rows:
        out.append({"counter": f"{kind}_tokens_in", "period": period,
                    "value": int(tin)})
        out.append({"counter": f"{kind}_tokens_out", "period": period,
                    "value": int(tout)})
    if reads:
        out.append({"counter": "memory_reads", "period": period,
                    "value": reads})
    if exchanges:
        out.append({"counter": "exchanges", "period": period,
                    "value": exchanges})
    # USAGE-ALERT PERCENTAGES (founder 2026-07-30): WE own the caps, the
    # control plane owns the email address and the customer's threshold —
    # so the flush carries the ready-made percentages and the control
    # plane just compares and mails. Plan-less (owner) tenants send none.
    if plan is not None:
        from core import quota, tiers
        c = tiers.caps(plan)
        # Top-ups widen the denominators (2026-08-20): a customer running on
        # purchased pack credits must not get a "100% used" email while
        # learning correctly continues.
        # RE-BIND (2026-08-23): this block runs AFTER the counters' own
        # bind/unbind, but stored_entitlements/pack_ledger read the tenant
        # DB. Unbound they raised NoTenantBound every 6h flush and the
        # gauges silently computed with ZERO entitlements — a pack customer
        # would get "100% used" alerts, the exact failure the 2026-08-20
        # widening exists to prevent.
        tok2 = store.bind(tenant)
        try:
            pack_total, extra_mb = quota.stored_entitlements()
            pack_avail = (quota.pack_ledger(pack_total, c["exchanges"])
                          if pack_total > 0 else 0)
        finally:
            store.unbind(tok2)
        ex_cap = c["exchanges"] + pack_avail
        st_cap = c["storage_mb"] + max(0, extra_mb)
        out.append({"counter": "quota_pct_exchanges", "period": period,
                    "kind": "gauge",
                    "value": round(exchanges / ex_cap * 100, 1)})
        out.append({"counter": "quota_pct_storage", "period": period,
                    "kind": "gauge",
                    "value": round(size_mb / st_cap * 100, 1)})
    return out


def flush_tenant(tenant: str) -> bool:
    """One tenant's counters to the control plane, now. Raises nothing."""
    token = auth.internal_token()
    if not auth.CONTROL_URL or not token:
        return False
    try:
        counters = _tenant_counters(tenant)
        req = urllib.request.Request(
            auth.CONTROL_URL.rstrip("/") + "/v1/internal/mind/usage",
            data=json.dumps({"tenant_id": tenant,
                             "counters": counters}).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {token}",
                     "User-Agent": "brethof-brain-v2/1.0"})
        with urllib.request.urlopen(req, timeout=15.0) as r:
            json.load(r)
        return True
    except Exception as e:
        print(f"[metering] flush {tenant}: {type(e).__name__}: "
              f"{str(e)[:120]}", flush=True)
        return False


def flush_tenant_async(tenant: str) -> None:
    """Fire-and-forget single-tenant flush — the INSTANT WALL path (founder
    2026-08-24: 'people coming, looking, not working... those messages need
    to be instant'). Called when quota.door() first closes on a period; the
    control plane's ingest runs the notify tiers, so the suspended email
    leaves within seconds of the first refusal instead of riding the
    6-hour bookkeeping loop (which stays, as the belt-and-braces sweep)."""
    threading.Thread(target=flush_tenant, args=(tenant,),
                     name=f"wall-flush-{tenant}", daemon=True).start()


# The periodic flush loop (flush_once / _loop / start) was the cloud plane's
# instrument and is gone (2026-09-02): usage reaches the control plane from the
# hub (exchange units) and from brain-host (ciphertext bytes, activity). What
# stays is the INSTANT wall push above — quota.door fires flush_tenant_async
# the moment a tenant hits its allowance (founder 2026-08-24: the suspended
# email must be instant) — and the local instrumentation (record, count_tool).
