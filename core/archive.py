"""The chat write path. IMMUTABLE store, and the one thing nothing can rebuild.

THE SINGLE MOST IMPORTANT LINE IN THIS FILE IS THE ONE THAT IS MISSING: there
is no embed() call here.

v1 embedded inline while archiving. When the sidecar timed out — which it does
under a 40-turn flush against a 1-core embedder — the exception was swallowed
and the turn was stored with no vector. Nobody was told. That is how 60,278 of
199,680 chat rows (30% of the archive) ended up invisible to vector recall for
months, discovered only when a migration counted them.

Here a turn lands with `embedding IS NULL` and the drainer fills it later. The
partial index `chat_unembedded` IS the queue: the backlog is one COUNT away, it
cannot silently drain to nothing, and a slow embedder costs latency in the
background instead of data in the foreground. Writes never depend on inference
being up.
"""
from __future__ import annotations

import re

from . import store

# A TURN'S OWN TIME (2026-10-23): the client has always sent each message's
# timestamp (the harness transcript's), and the archive dropped it — every row
# carried the moment its BATCH was written, ~20 turns to one time in this
# memory (1,736 turns, 89 distinct times in one session). A period report
# ("what was done that day") needs when a thing was said. Accepted only inside
# [now - 30 days, now + 5 min]: a late flush of a long session is in range,
# a clock in the future or a backdated row is not — cleanup_history deletes by
# this time at 90+ days, so a client must never be able to age a fresh row.
SAID_AT_PAST_DAYS = 30
SAID_AT_FUTURE_S = 300


def said_at(turn: dict):
    """The turn's own time as an aware datetime, or None (the write time stands)."""
    from datetime import datetime, timedelta, timezone
    raw = turn.get("timestamp") or turn.get("ts")
    if not raw or not isinstance(raw, str):
        return None
    try:
        t = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if t > now + timedelta(seconds=SAID_AT_FUTURE_S) or t < now - timedelta(days=SAID_AT_PAST_DAYS):
        return None
    return t

# "hash: <sha>, branch: main, files: a.py, b.py" — the shape the commit
# hook produces when the caller passes no message. Matched so it can be
# refused; see record_commit.
_BARE_COMMIT = re.compile(
    r"^hash:\s*[0-9a-f]{6,40}\s*,\s*branch:\s*\S*\s*,?\s*files:", re.I)


def ensure_project(conn, project: str) -> None:
    """A project costs ONE ROW here. In v1 it cost 3 tables and 11 indexes of
    runtime DDL, which is what capped tenants at ~100 projects."""
    conn.cursor().execute(
        "INSERT INTO project (name) VALUES (%s) ON CONFLICT DO NOTHING;",
        [project])


# A RE-SENT TURN IS NOT NEW, WHATEVER ITS NUMBER (2026-10-15). Claude Code
# writes old messages into a long session's transcript a second time (the same
# message id and timestamp, measured in marketing's file), and a client that
# numbers lines as it reads them gives each copy a NEW turn index — so the
# (session_id, turn_index) key let ~9,000 copies into the archive in 30 days
# (three long sessions) and the curator filed them as new conversation. Any
# harness can do the same, so the box decides: a turn whose role and text the
# session already holds is refused, by content. Text under REPEAT_MIN_CHARS
# ("yes", "go") legitimately repeats and is never judged.
REPEAT_MIN_CHARS = 80
_REPEAT_DDL = "CREATE INDEX IF NOT EXISTS chat_session_text_md5 ON chat (session_id, md5(text));"


def _role(t: dict) -> str:
    return t.get("line_type") or t.get("role") or "user"


def repeats(cur, session_id: str, turns: list[dict]) -> set[int]:
    """Positions in `turns` that repeat a turn the session already holds, or
    an earlier turn of the same batch (the first stays)."""
    import hashlib
    cand = [(i, _role(t), (t.get("text") or "").replace("\x00", "")) for i, t in enumerate(turns)]
    cand = [(i, r, x, hashlib.md5(x.encode()).hexdigest()) for i, r, x in cand
            if len(x.strip()) >= REPEAT_MIN_CHARS]
    if not cand:
        return set()
    store.ensure_ddl(cur, "chat_repeat_md5", _REPEAT_DDL)
    cur.execute("SELECT role, md5(text) FROM chat WHERE session_id = %s AND md5(text) = ANY(%s);",
                [session_id, [h for *_, h in cand]])
    held = {(r, h) for r, h in cur.fetchall()}
    out: set[int] = set()
    for i, r, _x, h in cand:
        if (r, h) in held:
            out.add(i)
        held.add((r, h))
    return out


def unseen(session_id: str, turns: list[dict]) -> list[dict]:
    """The turns of a batch the archive does not hold yet — each carrying its
    archive index, so a piece built from them keeps its true range.

    WHY (2026-09-28): a compact's re-flush re-sends turns the archive already
    holds; archive_turns ignores them, but the stop hook fed the WHOLE batch
    to the curator — 146 records and 194 lines curated from a month-old stretch
    of one long session at its two compacts (rules:never_back_curate_old_chat).
    Only what the archive takes for the first time is new conversation."""
    if not turns or not session_id:
        return []
    marked = [dict(t, index=int(t.get("index", i))) for i, t in enumerate(turns)]
    with store.connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT turn_index FROM chat WHERE session_id = %s AND turn_index = ANY(%s);",
                    [session_id, [t["index"] for t in marked]])
        have = {r[0] for r in cur.fetchall()}
        rep = repeats(cur, session_id, marked)          # a re-sent copy under a new number
        conn.commit()
    return [t for i, t in enumerate(marked) if t["index"] not in have and i not in rep]



def _place(cur, project: str, session_id: str, rows: list, srcs: list):
    """Drop retries and move a turn whose number is held by OTHER text — with
    the rest of its batch — to the session's end.

    A retry is the same number and the same text, or (2026-11-19, found by the
    live probe) the whole moved run again: a restarted client re-sends a
    spooled batch under its own numbers, and the run already sits at the end."""
    import hashlib
    cur.execute("SELECT turn_index, md5(text) FROM chat WHERE session_id = %s AND turn_index = ANY(%s);",
                [session_id, [r[2] for r in rows]])
    held = dict(cur.fetchall())
    if not held:
        return rows, srcs
    md5 = [hashlib.md5(r[4].encode()).hexdigest() for r in rows]
    # same number, same text: already stored
    keep = [i for i, r in enumerate(rows) if held.get(r[2]) != md5[i]]
    rows = [rows[i] for i in keep]; srcs = [srcs[i] for i in keep]; md5 = [md5[i] for i in keep]
    k = next((i for i, r in enumerate(rows) if r[2] in held), None)
    if k is None:
        return rows, srcs
    run = [(rows[i][3], md5[i]) for i in range(k, len(rows))]
    cur.execute("SELECT turn_index, role, md5(text) FROM chat WHERE session_id = %s AND md5(text) = ANY(%s) "
                "ORDER BY turn_index;", [session_id, list({h for _, h in run})])
    at = {int(i): (r, h) for i, r, h in cur.fetchall()}
    for start in at:
        if all(at.get(start + j) == run[j] for j in range(len(run))):
            print(f"[archive] {project}/{session_id}: {len(run)} re-sent turn(s) already stored "
                  f"at {start}.. — not stored again", flush=True)
            return rows[:k], srcs[:k]
    cur.execute("SELECT coalesce(max(turn_index), -1) FROM chat WHERE session_id = %s;", [session_id])
    nxt = max(int(cur.fetchone()[0]), max(r[2] for r in rows)) + 1
    moved = []
    for r in rows[k:]:
        moved.append((r[0], r[1], nxt, r[3], r[4], r[5])); nxt += 1
    print(f"[archive] {project}/{session_id}: {len(moved)} turn(s) arrived under numbers "
          f"the session already used for other text — stored at the end", flush=True)
    return rows[:k] + moved, srcs

def archive_turns(project: str, session_id: str, turns: list[dict],
                  placed: list | None = None) -> int:
    """Write a batch of turns. Returns the number actually inserted.

    turns: [{index:int, line_type|role:'user'|'assistant', text:str, ...}]

    Re-archiving the same batch is a no-op rather than a duplicate — the client
    retries on any non-ok status, and UNIQUE (session_id, turn_index) is what
    makes that safe.

    A NUMBER TAKEN BY OTHER TEXT IS A NEW TURN (2026-11-19). An adapter that
    restarts with the same session id numbers from 0 again (AgentHooks keeps
    its count in memory), and ON CONFLICT DO NOTHING dropped every new turn
    until the count passed the old one — the conversation after a restart was
    not archived, with an "INSERT SHORTFALL" line the only trace. Now the
    same number AND the same text is a retry (skipped, as before); the same
    number with different text goes on the END of the session, and so does
    the rest of its batch, so the order holds. `placed`, when given, receives
    each stored turn with the index it was stored under.
    """
    if not turns or not session_id:
        return 0
    from .chatkind import is_recap
    rows = []
    srcs: list[dict] = []
    recaps = 0
    for i, t in enumerate(turns):
        text = t.get("text") or ""
        # A RECAP IS NOT STORED (founder 2026-09-22): a compacting session's
        # retelling of turns this archive already holds.
        if is_recap(t.get("line_type") or t.get("role") or "user", text):
            recaps += 1
            continue
        # Strip NULs: PostgreSQL text cannot contain U+0000 and captured tool
        # output really does carry them (25,376 found in the v1 archive). One
        # unstripped byte would reject the whole batch.
        text = text.replace("\x00", "")
        rows.append((project, session_id, int(t.get("index", i)),
                     t.get("line_type") or t.get("role") or "user", text, said_at(t)))
        srcs.append(t)

    if recaps:
        print(f"[archive] {project}/{session_id}: {recaps} compaction recap(s) not stored", flush=True)
    if not rows:
        return 0
    with store.connection() as conn:
        ensure_project(conn, project)
        cur = conn.cursor()
        dup = repeats(cur, session_id, [{"role": r[3], "text": r[4]} for r in rows])
        if dup:
            print(f"[archive] {project}/{session_id}: {len(dup)} re-sent turn(s) the session already holds "
                  f"(same text, new number) — not stored", flush=True)
            rows = [r for i, r in enumerate(rows) if i not in dup]
            srcs = [t for i, t in enumerate(srcs) if i not in dup]
            if not rows:
                conn.commit()
                return 0
        rows, srcs = _place(cur, project, session_id, rows, srcs)
        if not rows:
            conn.commit()
            return 0
        cur.executemany(
            "INSERT INTO chat (project, session_id, turn_index, role, text, ts) "
            "VALUES (%s, %s, %s, %s, %s, coalesce(%s, now())) "
            "ON CONFLICT (session_id, turn_index) DO NOTHING;", rows)
        n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        conn.commit()
    if placed is not None:
        placed.extend(dict(t, index=r[2]) for r, t in zip(rows, srcs))
    if n != len(rows):
        # NEVER SILENT (2026-09-01: a scale storm lost 4 of 400 turns with
        # every reply reading ok — this line is how the next one names
        # itself). A shortfall here is either a client re-send hitting the
        # dedupe (benign) or a real conflict eating fresh turns (not).
        print(f"[archive] INSERT SHORTFALL {project}/{session_id}: "
              f"received {len(rows)}, inserted {n} — "
              f"{len(rows) - n} skipped by ON CONFLICT", flush=True)
    if n:
        # New chat rows carry no vector — hand the drainer this tenant
        # instead of making it sweep everyone. This is a HINT: it may only
        # add work. Nothing but the database may conclude there is none.
        from core import drainer
        drainer.note_work(store.current_tenant())
        try:
            # THE DOOR'S PROPOSER (2026-09-10): once per session, after a
            # few turns, the hub says which project the session looks like;
            # the owner confirms with move_session.
            from core import proposer
            proposer.trigger(store.current_tenant(), project, session_id)
        except Exception:                                    # noqa: BLE001
            import traceback
            traceback.print_exc()
    return n


def record_commit(project: str, payload: dict) -> None:
    """Commits are curated records, not a separate table — one more thing v1
    spent 16 tables on."""
    # "hash" included: that is what the shipped client actually sends — the
    # sha/commit-only read dropped every hooked commit silently until the
    # 2026-07-28 hook sweep caught it. A skipped commit is LOGGED now; the
    # quiet `return` was exactly the v1 failure pattern this rewrite banned.
    sha = str(payload.get("sha") or payload.get("commit")
              or payload.get("hash") or "")[:40]
    if not sha:
        print(f"[archive] commit payload had no sha/commit/hash — dropped "
              f"(keys: {sorted(payload)})", flush=True)
        return
    msg = (payload.get("message") or "").replace("\x00", "")
    # A COMMIT WITHOUT A MESSAGE IS NOT KNOWLEDGE (2026-08-07): a record
    # reading "hash: …, branch: main, files: …" tells a future session
    # nothing — no what, no why — while diluting every search and every
    # teacher sample. 428 such stubs were deleted fleet-wide; this is the
    # guard that stops them coming back. The hash/branch/file list still
    # rides along WITH a message, and git itself keeps the full history.
    # Same NUL discipline as archive_turns above: a commit message is
    # arbitrary bytes from a git hook, and one 0x00 would fail the insert.
    msg = msg.replace("\x00", "") if isinstance(msg, str) else msg
    body = " ".join(msg.split())
    if not body or _BARE_COMMIT.match(body):
        print(f"[archive] commit {sha[:12]} has no message — not recorded "
              f"(hash+files alone is not knowledge)", flush=True)
        return
    with store.connection() as conn:
        ensure_project(conn, project)
        conn.cursor().execute(
            "INSERT INTO document (project, record_id, type, title, content, "
            "                      source, meta) "
            "VALUES (%s, %s, 'commit', %s, %s, 'hook', %s::jsonb) "
            "ON CONFLICT (project, record_id) DO UPDATE SET "
            "  content = EXCLUDED.content, updated_at = now();",
            [project, f"commit_{sha[:12]}", msg.split("\n")[0][:200], msg,
             '{"sha": "%s"}' % sha])
        conn.commit()


def unembedded_counts() -> dict:
    """The backlog, always answerable in one query. A number nobody can see is
    a number that goes wrong quietly."""
    with store.connection() as conn:
        cur = conn.cursor()
        # ELIGIBLE rows only (2026-09-03): embed_skip rows are kept in the
        # archive and never embedded by design — counting them read as a
        # 21-row backlog the drainer "never cleared".
        cur.execute("SELECT (SELECT count(*) FROM chat WHERE embedding IS NULL AND NOT embed_skip), "
                    "       (SELECT count(*) FROM document WHERE embedding IS NULL AND NOT embed_skip);")
        chat_n, doc_n = cur.fetchone()
    return {"chat": chat_n, "document": doc_n}
