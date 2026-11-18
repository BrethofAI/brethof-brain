"""THE DOOR'S PROPOSER — one short call per session that says which project
the session looks like, so the owner confirms with `move_session`.

Founder 2026-09-09: "splitting a project out of another must be a door, not
admin surgery" — the door shipped that night (core/ownership); this is the
eye in front of it. A session is archived under the folder it ran in, which
is wrong whenever the founder works on one project from another's folder
(estate work from the brain repo, the audit found thirteen). Nothing here
moves anything: the proposer WRITES A PROPOSAL and the owner decides.

WHEN: once per session, after PROPOSE_AFTER_TURNS user/assistant turns are
archived (enough to know what the session is about, early enough to matter),
in a daemon thread behind the stop hook — never on the hook's path.

WHAT IT READS: the session's first PROPOSE_CHARS characters, the project
it is filed under with its purpose, and every project of the tenant with
its purpose (the purposes live on the hub as the `purpose_<project>` rules;
the hub fills them in). WHAT IT ASKS: the hub, which holds the licensed
`proposer` prompt — one project name from the list, a confidence, one
sentence why. WHO READS: the hub, as a heal call inside heal's allowance
(founder 2026-09-10: "it should work the same like heal, be limited to
calls like heal has limited calls"; heal calls this month never exceed
the pieces the curator judged — the hub's `heal_over_allowance`). Not a
graph unit, not metered on its own.

WHERE IT SHOWS: the per-prompt block of the session itself (a few times,
then quiet), and the brief of the project it is filed under ("sessions the
proposer would file elsewhere"). `ownership.move` marks the proposal decided
for a moved session; `dismiss` marks it declined.
"""
from __future__ import annotations

import json
import threading
import traceback

from . import store

PROPOSE_AFTER_TURNS = 6           # user+assistant rows archived before the call
PROPOSE_CHARS = 6000              # the head of the session the reader sees
PURPOSE_CHARS = 220               # per project line in the list
CONFIDENCE_MIN = 0.6              # below this the proposal is kept but not shown
SHOW_TIMES = 3                    # per-prompt reminders before the line goes quiet
PENDING_DAYS = 14                 # how far back the brief lists pending proposals

DDL = """
CREATE TABLE IF NOT EXISTS session_proposal (
  session_id text PRIMARY KEY,
  filed_project text NOT NULL,
  proposed_project text NOT NULL DEFAULT '',
  confidence real NOT NULL DEFAULT 0,
  why text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now(),
  shown int NOT NULL DEFAULT 0,
  decided_at timestamptz,
  decision text NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS session_proposal_filed_idx ON session_proposal (filed_project, created_at);
"""

_lock = threading.Lock()
_running: set[str] = set()


def ensure(cur) -> None:
    try:
        store.ensure_ddl(cur, "session_proposal", DDL)
    except store.NoTenantBound:
        cur.execute(DDL)


# ── what the reader sees ─────────────────────────────────────────────────────
def due(cur, session_id: str) -> bool:
    """One proposal per session, after enough turns."""
    ensure(cur)
    cur.execute("SELECT 1 FROM session_proposal WHERE session_id = %s;", [session_id])
    if cur.fetchone():
        return False
    cur.execute("SELECT count(*) FROM chat WHERE session_id = %s "
                "AND role IN ('user', 'assistant');", [session_id])
    return int(cur.fetchone()[0]) >= PROPOSE_AFTER_TURNS


def head_text(cur, session_id: str, limit: int = PROPOSE_CHARS) -> str:
    cur.execute("SELECT role, text FROM chat WHERE session_id = %s "
                "AND role IN ('user', 'assistant') ORDER BY turn_index LIMIT 40;", [session_id])
    out, n = [], 0
    for role, text in cur.fetchall():
        t = " ".join((text or "").split())
        if not t:
            continue
        line = f"{role}: {t}"
        if n + len(line) > limit:
            line = line[: max(0, limit - n)]
        out.append(line)
        n += len(line)
        if n >= limit:
            break
    return "\n".join(out)


def project_names(cur) -> list[str]:
    """Every project of the tenant, bare — the purposes are the hub's own
    rules and the hub fills them in when it renders."""
    cur.execute("SELECT name FROM project WHERE name NOT IN ('global','rules','state') ORDER BY name;")
    return [r[0] for r in cur.fetchall()]






def parse(answer, projects: list[tuple[str, str]], filed: str) -> dict | None:
    """The answer as {project, confidence, why}; a name not in the list is
    the filed project (the reader may not invent a project). `answer` is the
    hub's parsed dict, or the raw text (the tests' shape)."""
    from . import inference
    if not answer:
        return None
    d = answer if isinstance(answer, dict) else inference.parse_json_loose(answer)
    if not isinstance(d, dict):
        return None
    names = {n for n, _ in projects}
    proj = str(d.get("project") or "").strip()
    if proj not in names:
        proj = filed
    try:
        conf = max(0.0, min(1.0, float(d.get("confidence") or 0)))
    except (TypeError, ValueError):
        conf = 0.0
    why = " ".join(str(d.get("why") or "").split())[:300]
    return {"project": proj, "confidence": conf, "why": why}




def propose(tenant: str, project: str, session_id: str) -> dict | None:
    """Read the session's head, ask the hub once, write the proposal. Returns
    it, or None when nothing was asked (not due, no text, no answer)."""
    from . import judge, metering
    tok = store.bind(tenant)
    try:
        with store.connection() as conn:
            cur = conn.cursor()
            if not due(cur, session_id):
                return None
            text = head_text(cur, session_id)
            names = project_names(cur)
            conn.commit()                # hold nothing across the ask
        if not text.strip() or not names:
            return None
        try:
            a = judge.ask("propose", {"filed": project, "session_id": session_id,
                                      "projects": names, "text": text})
        except judge.NoAnswer as e:
            print(f"[proposer] {tenant}/{project} {session_id[:8]}: no answer — {e.why}", flush=True)
            return None                  # asked again next stop
        metering.record("heal", a["input_tokens"], a["output_tokens"])
        p = parse(a["answer"], [(n, "") for n in names], project) or \
            {"project": project, "confidence": 0.0, "why": "unparseable answer"}
        with store.connection() as conn:
            file(conn.cursor(), session_id, project, p["project"], p["confidence"], p["why"])
            conn.commit()
        same = p["project"] == project
        print(f"[proposer] {tenant}/{project} {session_id[:8]}: "
              + (f"looks like {project} itself ({p['confidence']:.2f})" if same
                 else f"proposes {p['project']} ({p['confidence']:.2f}) — {p['why'][:120]}")
              + f" [{a['input_tokens']}/{a['output_tokens']} tokens]", flush=True)
        return {**p, "filed": project, "session_id": session_id}
    finally:
        store.unbind(tok)


def trigger(tenant: str, project: str, session_id: str) -> str:
    """Behind the stop hook: one thread per session at a time, free when not
    due (one cheap count), never raising into the hook."""
    if not session_id:
        return "proposer: off"
    key = f"{tenant}/{session_id}"
    with _lock:
        if key in _running:
            return "proposer: running"
        _running.add(key)

    def _run() -> None:
        try:
            propose(tenant, project, session_id)
        except Exception as e:                                 # noqa: BLE001
            traceback.print_exc()
            print(f"[proposer] {key} FAILED: {type(e).__name__}: {e}", flush=True)
        finally:
            with _lock:
                _running.discard(key)

    threading.Thread(target=_run, name=f"proposer-{session_id[:8]}", daemon=True).start()
    return "proposer: started"


# ── what the owner sees ──────────────────────────────────────────────────────
def pending(cur, project: str, limit: int = 5) -> list[dict]:
    """Proposals for sessions filed under `project` that point elsewhere and
    are not decided — for the project's brief."""
    ensure(cur)
    cur.execute(
        "SELECT session_id, proposed_project, confidence, why, created_at FROM session_proposal "
        "WHERE filed_project = %s AND proposed_project <> filed_project AND decision = '' "
        "AND confidence >= %s AND created_at > now() - (%s * interval '1 day') "
        "ORDER BY created_at DESC LIMIT %s;", [project, CONFIDENCE_MIN, PENDING_DAYS, limit])
    return [{"session_id": s, "project": p, "confidence": float(c), "why": w, "at": str(t)[:16]}
            for s, p, c, w, t in cur.fetchall()]


def brief_lines(cur, project: str) -> str:
    rows = pending(cur, project)
    if not rows:
        return ""
    return "\n".join(
        f"- {r['session_id'][:8]} ({r['at']}) looks like {r['project']} ({r['confidence']:.2f}) — {r['why']} "
        f"→ move_session('{r['session_id']}', '{r['project']}', why='…') to confirm, "
        f"dismiss_proposal('{r['session_id']}') to keep it here" for r in rows)


def prompt_row(cur, session_id: str) -> dict | None:
    """The session's own pending proposal, when it is still to be shown —
    counted as shown when returned; SHOW_TIMES times, then None."""
    ensure(cur)
    cur.execute("SELECT filed_project, proposed_project, confidence, why, shown FROM session_proposal "
                "WHERE session_id = %s AND decision = '';", [session_id])
    r = cur.fetchone()
    if not r:
        return None
    filed, proposed, conf, why, shown = r
    if proposed == filed or float(conf) < CONFIDENCE_MIN or int(shown) >= SHOW_TIMES:
        return None
    cur.execute("UPDATE session_proposal SET shown = shown + 1 WHERE session_id = %s;", [session_id])
    return {"session_id": session_id, "filed": filed, "proposed": proposed,
            "confidence": float(conf), "why": why, "shown": int(shown) + 1}


def prompt_line(cur, session_id: str) -> str:
    """The session's own reminder, SHOW_TIMES times, then quiet."""
    r = prompt_row(cur, session_id)
    if not r:
        return ""
    return (f"THIS SESSION LOOKS LIKE PROJECT '{r['proposed']}' ({r['confidence']:.2f}): {r['why']} — "
            f"it is filed under '{r['filed']}'. move_session('{session_id}', '{r['proposed']}', why='…') "
            f"moves it; dismiss_proposal('{session_id}') keeps it here.")


def file(cur, session_id: str, filed: str, proposed: str, confidence: float, why: str) -> int:
    """Write one proposal; a session is proposed once (the first wins)."""
    ensure(cur)
    cur.execute(
        "INSERT INTO session_proposal (session_id, filed_project, proposed_project, confidence, why) "
        "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (session_id) DO NOTHING;",
        [session_id, filed, proposed, confidence, why])
    return cur.rowcount


def decide(cur, session_id: str, decision: str) -> bool:
    ensure(cur)
    cur.execute("UPDATE session_proposal SET decision = %s, decided_at = now() "
                "WHERE session_id = %s AND decision = '';", [decision[:40], session_id])
    return cur.rowcount == 1
