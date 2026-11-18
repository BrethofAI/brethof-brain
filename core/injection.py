"""Session-start and prompt-submit injection, on the box.

Design `brain-design-hub-brain`, step 2 (2026-09-10): the COMPOSITION of
what a session is told lives in core/composer (which sections, the bars,
the slots, the cooldowns) and the WORDS in core/brieftext — one code for
the hub and the box. What stays here is the box's side: the database
readers the pipe's commands answer with (core/pipecommands), and the two
entry points the box's hook routes still call, which compose through the
same commands in process. When the hub composes (the design's second
path), this file keeps only the readers.

THE READS ARE DEFENSIVE ON PURPOSE. v1's rule and state lines each rendered a
wall of literal "None" because the payload field drifted across writer
generations (`content` on old rows, `rule`/`status` on newer ones). Both are
read here, and a row with neither is DROPPED rather than emitted as an empty
bullet. The migrated `meta` jsonb is what makes that possible at all.
"""
from __future__ import annotations

import asyncio
import os
import threading
import traceback

from . import brieftext as T, composer, store

# The names the tests and the neighbours knew here — the words moved to
# core/brieftext, the doctrine to core/composer; the same objects.
_NONWORD, _SID_RE = T._NONWORD, T._SID_RE
_flat, _clip, _destutter, _norm = T.flat, T.clip, T.destutter, T.norm
_rule_line = T.rule_line
_note_line, _description_section, _DESC_CALL = T.note_line, T.description_section, T.DESC_CALL
_MEMORY_HEADER_OWNER, _MEMORY_HEADER_CUSTOMER = T.MEMORY_HEADER_OWNER, T.MEMORY_HEADER_CUSTOMER
_PART_BUDGET, _HOOK_BUDGET, _PLAN_LINES = T.PART_BUDGET, T.HOOK_BUDGET, T.PLAN_LINES
_DESC_CHECK_TURNS, _is_cooling = T.DESC_CHECK_TURNS, T.is_cooling
_STRONG, _RECORD_SLOTS = composer.STRONG, composer.RECORD_SLOTS
_RECORD_COOLDOWN = composer.RECORD_COOLDOWN
_RULE_COOLDOWN_TURNS = composer.RULE_COOLDOWN_TURNS


def plan_next_lines(content: str, limit: int = _PLAN_LINES, clip: int = 220) -> list[str]:
    return T.plan_next_lines(content, limit=limit, clip_at=clip)


def _record_block(rid: str, e: dict, room: int) -> str:
    return T.record_block(rid, e.get("row") or {}, room)


# ── the readers (what the pipe's commands answer with) ──────────────────────
def _rows(cur) -> list[dict]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ── cooldown ledger — ONE ledger for everything the ambient hook injects ──
# (knowledge records, playbooks, rule reminders.)
#
# Founder 2026-08-12 ("I thought it went for like 20 turns... knowledge and
# such should be the same"): the old inject_ledger BURNED a record for the
# whole session — in a 1,200-turn session a record injected at turn 40 was
# invisible at turn 900 when it finally mattered, and after a /compact (the
# model's context wiped, the ledger not) it could never return at all. Every
# injection now COOLS DOWN instead: suppressed while
# 0 < current_turn - stamped_turn < cooldown, eligible again after — the
# same insight that gave rules their re-fire in August, applied uniformly.
# A stamp AT the current turn does NOT suppress: the prompt-submit parts run
# concurrently for one prompt, and this is what keeps their candidate lists
# identical regardless of write order (part 1 takes candidate[0], part 2
# candidate[1] — deterministic, no shared state).
# Best-effort like everything ambient: any ledger error degrades to
# "inject anyway", it never blocks recall.
#
# UNITS: a turn is one USER PROMPT — the count of the session's archived
# user rows (since 2026-09-11; core/composer says why the row count was
# wrong). `first_turn` and `fires` are the instrument for the founder's
# open question ("maybe every 20 is too often — should be tested"): how
# often the same item comes back inside one context window, and how far
# apart. The compact reset clears them with the rest: a window is one
# model context.
_COOLDOWN_DDL = """
CREATE TABLE IF NOT EXISTS cooldown_ledger (
    session_id  text NOT NULL,
    item_id     text NOT NULL,
    turn_idx    int  NOT NULL DEFAULT 0,
    injected_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, item_id)
);
ALTER TABLE cooldown_ledger ADD COLUMN IF NOT EXISTS first_turn int NOT NULL DEFAULT 0;
ALTER TABLE cooldown_ledger ADD COLUMN IF NOT EXISTS fires int NOT NULL DEFAULT 1;
"""

# ── THE HOLD-BACK (founder 2026-09-28: "inject with delay") ──────────────────
# A record or decision line a session wrote is new to every OTHER agent on
# the memory, and they get it at once. To the session that wrote it, it is
# its own words from minutes ago — until the session compacts and loses the
# details; then it comes back. The compact point is the last archived turn
# the pre-compact hook saw. Measured the day it was decided: most records
# injected into one session were its own statements from minutes before.
_COMPACT_DDL = """
CREATE TABLE IF NOT EXISTS session_compact (
    session_id  text PRIMARY KEY,
    turn_index  int NOT NULL,
    at          timestamptz NOT NULL DEFAULT now()
);
"""


def mark_compact(conn, cur, sid: str, turn_index: int) -> None:
    if not sid:
        return
    try:
        store.ensure_ddl(cur, "session_compact", _COMPACT_DDL)
        cur.execute("INSERT INTO session_compact (session_id, turn_index) VALUES (%s, %s) "
                    "ON CONFLICT (session_id) DO UPDATE SET turn_index = EXCLUDED.turn_index, at = now();",
                    [sid, int(turn_index)])
        conn.commit()
    except Exception:
        conn.rollback()
        traceback.print_exc()


def compact_turn(cur, sid: str) -> int:
    """The last archived turn before the session's latest compact; -1 when
    it never compacted (everything it wrote is still in its context)."""
    if not sid:
        return -1
    try:
        store.ensure_ddl(cur, "session_compact", _COMPACT_DDL)
        cur.execute("SELECT turn_index FROM session_compact WHERE session_id = %s;", [sid])
        r = cur.fetchone()
        return int(r[0]) if r else -1
    except Exception:
        return -1


# THE SAVE GAP (2026-09-28, founder: "fix the save gap"): an explicit save
# (save_memory) is archived under its own id "save-…", so the hold-back could
# not tell it was the asking session's own words. The MCP door is not told the
# harness session; the box is — every prompt names it. A save is attributed to
# the one session that prompted in its project in the last ACTIVE_MIN minutes;
# two sessions active in the project at once → left unattributed (everyone,
# the saver included, then gets it — nothing is lost).
ACTIVE_MIN = 15
_ACTIVITY_DDL = """
CREATE TABLE IF NOT EXISTS session_activity (
    project     text NOT NULL,
    session_id  text NOT NULL,
    at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project, session_id)
);
CREATE TABLE IF NOT EXISTS save_origin (
    save_sid    text PRIMARY KEY,
    session_id  text NOT NULL,
    at          timestamptz NOT NULL DEFAULT now()
);
"""


def note_prompt(conn, cur, project: str, sid: str) -> None:
    if not project or not sid:
        return
    try:
        store.ensure_ddl(cur, "session_activity", _ACTIVITY_DDL)
        cur.execute("INSERT INTO session_activity (project, session_id) VALUES (%s, %s) "
                    "ON CONFLICT (project, session_id) DO UPDATE SET at = now();", [project, sid])
        conn.commit()
    except Exception:
        conn.rollback()
        traceback.print_exc()


def attribute_save(conn, cur, project: str, save_sid: str) -> str:
    """The session this save came from, or "" when it cannot be told."""
    try:
        store.ensure_ddl(cur, "session_activity", _ACTIVITY_DDL)
        cur.execute("SELECT session_id FROM session_activity WHERE project = %s "
                    "AND at > now() - make_interval(mins => %s);", [project, ACTIVE_MIN])
        live = [r[0] for r in cur.fetchall()]
        if len(live) != 1:
            return ""
        cur.execute("INSERT INTO save_origin (save_sid, session_id) VALUES (%s, %s) "
                    "ON CONFLICT (save_sid) DO NOTHING;", [save_sid, live[0]])
        conn.commit()
        return live[0]
    except Exception:
        conn.rollback()
        traceback.print_exc()
        return ""


def held_save(cur, sid: str, save_sid: str) -> bool:
    """A save the asking session made since its last compact."""
    try:
        store.ensure_ddl(cur, "session_activity", _ACTIVITY_DDL)
        store.ensure_ddl(cur, "session_compact", _COMPACT_DDL)
        cur.execute("SELECT o.at > coalesce(c.at, '-infinity'::timestamptz) FROM save_origin o "
                    "LEFT JOIN session_compact c ON c.session_id = o.session_id "
                    "WHERE o.save_sid = %s AND o.session_id = %s;", [save_sid, sid])
        r = cur.fetchone()
        return bool(r and r[0])
    except Exception:
        return False


def held(sid: str, compact: int, origin_sid: str, origin_turn) -> bool:
    """Its own session wrote it after the session's last compact."""
    if not sid or origin_sid != sid:
        return False
    try:
        return int(origin_turn) > compact
    except (TypeError, ValueError):
        return True


def _turn_now(cur, sid: str) -> int:
    """The session's exchange count: its archived USER rows, never the row
    count (a tool call is a row too)."""
    cur.execute("SELECT count(*) FROM chat WHERE session_id = %s AND role = 'user';", [sid])
    return int(cur.fetchone()[0])

def _stamps(cur, sid: str) -> dict[str, int]:
    """The whole session's ledger (small — bounded by cooldown churn):
    item_id → turn it was last injected at."""
    if not sid:
        return {}
    try:
        store.ensure_ddl(cur, "cooldown_ledger", _COOLDOWN_DDL)
        cur.execute("SELECT item_id, turn_idx FROM cooldown_ledger "
                    "WHERE session_id = %s;", [sid])
        return dict(cur.fetchall())
    except Exception:
        return {}

def _stamp(conn, cur, sid: str, turn: int, ids: list[str]) -> None:
    if not sid or not ids:
        return
    try:
        store.ensure_ddl(cur, "cooldown_ledger", _COOLDOWN_DDL)
        cur.execute(
            "INSERT INTO cooldown_ledger (session_id, item_id, turn_idx, first_turn) "
            "SELECT %s, x, %s, %s FROM unnest(%s::text[]) x "
            "ON CONFLICT (session_id, item_id) DO UPDATE SET "
            "  turn_idx = EXCLUDED.turn_idx, injected_at = now(), "
            "  fires = cooldown_ledger.fires + CASE WHEN cooldown_ledger.turn_idx = EXCLUDED.turn_idx THEN 0 ELSE 1 END;",
            [sid, turn, turn, ids])
        conn.commit()
    except Exception:
        conn.rollback()

# ── the box's two entry points, through the same commands in process ─────────
async def _ask(cmd: str, body: dict) -> dict:
    """The composer's `ask` on the box: the pipe's handlers, called in
    process under the tenant already bound — the same rows the hub gets
    down the pipe, the same refusals."""
    from . import pipecommands
    try:
        return {"ok": True, **pipecommands.run(store.current_tenant(), cmd, body)}
    except Exception as e:                                       # noqa: BLE001
        traceback.print_exc()
        return {"ok": False, "why": f"{type(e).__name__}: {e}"[:200]}


def _tenant_or_empty() -> str:
    try:
        return store.current_tenant()
    except Exception:                                            # noqa: BLE001
        return ""


def _run(coro):
    """Run a coroutine from sync code — in a fresh loop, or, when this
    thread already runs one (the MCP server calling a tool), in a helper
    thread with its own loop, carrying the tenant binding along."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    out: dict = {}
    tenant = _tenant_or_empty()

    def _go():
        tok = store.bind(tenant) if tenant else None
        try:
            out["v"] = asyncio.run(coro)
        except BaseException as e:                               # noqa: BLE001
            out["e"] = e
        finally:
            if tok is not None:
                store.unbind(tok)
    t = threading.Thread(target=_go, name="compose")
    t.start()
    t.join()
    if "e" in out:
        raise out["e"]
    return out.get("v")


UNREACHABLE = ("the hub could not be asked — no brief and no rules this session "
               "(the hub composes what a session is told; it is down, or this box's "
               "key is not active on it). Recall and history keep working.")


def _one_line(project: str, part: int, why: str) -> str:
    """What a session-start hears when the hub cannot be asked — part 1
    only, loud in the log, never a blank."""
    print(f"[brief] {project}: {why}", flush=True)
    if part > 1:
        return ""
    return f"brethof-brain — project: {project}\n\n## BRAIN — NOT AVAILABLE\n⚠ {UNREACHABLE}"


def session_blob(project: str, part: int = 0, tier: str = "owner",
                 source: str = "", session_id: str = "") -> str:
    """part=0: the whole payload (legacy clients, tests). part=1..N: one
    budgeted chunk per registered SessionStart hook. COMPOSED ON THE HUB
    (design step 2, the forward shape): this box asks over its pipe and
    hands back the text; the hub gathers the rows by commanding this box."""
    from . import pipeclient
    body = {"project": project, "part": part, "tier": tier, "source": source,
            "session_id": session_id}
    try:
        ans = pipeclient.ask("brief", body)
    except pipeclient.PipeDown as e:
        return _one_line(project, part, str(e))
    if not ans.get("ok"):
        return _one_line(project, part, f"the hub refused the brief: {ans.get('why')}")
    return str(ans.get("text") or "")


def ambient_blob(project: str, session_id: str, prompt: str,
                 part: int = 0, ephemeral: bool = False) -> str:
    """The prompt block (core/composer.ambient), COMPOSED ON THE HUB: the
    box sends the prompt with its vector (the hub has no embedder; the rule
    match is by cosine there) and hands back the text. The hub unreachable
    is our outage: silence, said in the log — the Brain speaking unasked is
    the hub's feature and there is nothing to inject without it."""
    from . import embed, pipeclient
    # short prompts go up: the hub decides (core/composer) — with B+v3 a
    # "yes, do it" is searched through the previous reply (2026-11-17)
    if T.trivial_prompt(prompt, allow_short=True):
        return ""
    vec = embed.embed_one(prompt)
    body = {"project": project, "session_id": session_id, "prompt": prompt, "part": part,
            "ephemeral": bool(ephemeral),
            "vector": vec}
    try:
        ans = pipeclient.ask("ambient", body)
    except pipeclient.PipeDown as e:
        print(f"[ambient] {project}: {e}", flush=True)
        return ""
    if not ans.get("ok"):
        print(f"[ambient] {project}: the hub refused: {ans.get('why')}", flush=True)
        return ""
    return str(ans.get("text") or "")


# ── the same composition in process: the goldens' harness ───────────────────
def brief_in_process(project: str, part: int = 0, tier: str = "owner",
                     source: str = "", session_id: str = "") -> str:
    """The composer with this box's handlers as its `ask` and the hub's law
    fetched over HTTP — what the hub does with command(); pinned against the
    goldens. Not a live path."""
    from . import rules_hub
    law = rules_hub.fetch(project)
    return _run(composer.brief(_ask, law, project, part, tier, source, session_id))


def ambient_in_process(project: str, session_id: str, prompt: str, part: int = 0) -> str:
    from . import embed, rules_hub

    async def match(text: str):
        pvec = embed.embed_one(text)
        return rules_hub.match(project, text, vector=pvec)

    async def purpose_of() -> str:
        return rules_hub.fetch(project)["purpose"]

    return _run(composer.ambient(_ask, match, purpose_of, project, session_id, prompt, part,
                                 scope=_tenant_or_empty()))
