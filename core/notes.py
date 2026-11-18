"""SESSION NOTES — the handover, out of the record pool.

Founder 2026-09-03, after reading that half of one session's prompt-hook
injections were the agent's own multi-subject handover notes: "that's really
not curated records. That's more like handovers... they usually are really
long, and they put so many subjects inside." A handover is where a thread of
work stopped, written for whoever picks it up. It is not a record (one
subject, pushed at sessions that did not ask), and it is not chat (append-
only, said once). It is a document a session maintains.

THE SHAPE, his and mine together:
  - keyed by SESSION and PROJECT. A session has one note in the project it
    works in, and may leave a note in another project ("write a note for
    marketing") — that note files under marketing and is what the next
    marketing session sees. A session that writes again replaces its own
    note: a compact does not stack notes.
  - a RING OF FIVE per project. The sixth session's note pushes the oldest
    off. Notes never age out by time — a project left for a month still
    shows its last five threads. His ring of ten was keyed by note, which one
    long session compacting ten times would fill by itself, evicting the
    parallel sessions' handovers it was meant to protect.
  - injected WHOLE at session start for the project, newest first, each
    headed by when it was written and a one-line thread name. Capped so five
    of them cost ~2.5K tokens and cannot become essays.
  - never in the record pool, never healed, never competing for the prompt
    hook's slots. Never sent to the hub: it is content, and the hub stores
    no customer content.

With this, agents stop hand-writing records: what they find they SAY, and
the curator files what passes the push test.
"""
from __future__ import annotations

from . import store

RING = 5                    # notes kept per project
BODY_CHARS = 2000           # a pick-up, not an essay
TITLE_CHARS = 120

_DDL = """
CREATE TABLE IF NOT EXISTS note (
    project     text NOT NULL,
    session_id  text NOT NULL,
    title       text NOT NULL DEFAULT '',
    body        text NOT NULL,
    written_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project, session_id)
);
CREATE INDEX IF NOT EXISTS note_project_recent ON note (project, written_at DESC);
"""


def ensure(cur) -> None:
    store.ensure_ddl(cur, "note", _DDL)


def _clean(text: str, n: int) -> str:
    text = (text or "").replace("\x00", "").strip()
    return text if len(text) <= n else text[:n].rstrip() + "…"


def save(conn, project: str, session_id: str, title: str, body: str) -> dict:
    """Upsert this session's note for the project, then trim the project's
    ring to RING notes by written_at. Returns {evicted: [...], size: n}."""
    project = (project or "").strip()
    session_id = (session_id or "").strip()
    if not project or not session_id:
        raise ValueError("project and session_id are required")
    body = _clean(body, BODY_CHARS)
    if not body:
        raise ValueError("an empty note is not a handover")
    title = _clean(title, TITLE_CHARS)
    cur = conn.cursor()
    ensure(cur)
    cur.execute(
        "INSERT INTO note (project, session_id, title, body, written_at) "
        "VALUES (%s, %s, %s, %s, now()) "
        "ON CONFLICT (project, session_id) DO UPDATE SET "
        "  title = EXCLUDED.title, body = EXCLUDED.body, written_at = now();",
        [project, session_id, title, body])
    cur.execute(
        "DELETE FROM note WHERE project = %s AND session_id IN ("
        "  SELECT session_id FROM note WHERE project = %s "
        "  ORDER BY written_at DESC OFFSET %s) RETURNING session_id;",
        [project, project, RING])
    evicted = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT count(*) FROM note WHERE project = %s;", [project])
    size = int(cur.fetchone()[0])
    conn.commit()
    return {"evicted": evicted, "size": size}


def delete(conn, project: str, session_id: str) -> bool:
    cur = conn.cursor()
    ensure(cur)
    cur.execute("DELETE FROM note WHERE project = %s AND session_id = %s;",
                [project, session_id])
    gone = cur.rowcount > 0
    conn.commit()
    return gone


def recent(conn, project: str, limit: int = RING) -> list[dict]:
    """The project's ring, newest first."""
    cur = conn.cursor()
    ensure(cur)
    cur.execute(
        "SELECT session_id, title, body, "
        "       to_char(written_at, 'YYYY-MM-DD HH24:MI') FROM note "
        "WHERE project = %s ORDER BY written_at DESC LIMIT %s;",
        [project, limit])
    return [{"session_id": r[0], "title": r[1], "body": r[2], "at": r[3]}
            for r in cur.fetchall()]


def render(notes: list[dict], this_session: str = "") -> str:
    """The session-start block: newest first, each headed by time and
    thread name. The reader's own note (after a compact) is marked."""
    parts = []
    for n in notes:
        mine = " (this session's own note)" if n["session_id"] == this_session else ""
        head = f"— {n['at']}{mine}" + (f" · {n['title']}" if n["title"] else "")
        parts.append(f"{head}\n{n['body']}")
    return "\n\n".join(parts)
