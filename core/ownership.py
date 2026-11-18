"""THE PROJECT-SPLIT DOOR — which project a stretch of the archive belongs to.

Founder 2026-09-09: splitting a project out of another must be a door, not
admin surgery. The customer's agent names what moves; our side is
mechanical; the archive is never edited.

WHY A TABLE BESIDE THE ARCHIVE. A session files under the project of the
folder it ran in, and that is often wrong: the estate's sessions run in the
brain repo's deploy/ folder and file under brain; a month-long imported blob
carries nine projects. The chat table is immutable by law — no UPDATE, no
DELETE, ever — so the correction lives HERE: one row per moved session or
turn range, latest row wins, and every reader of the archive consults this
table before the row's own project column. Moving back is another row.
Nothing is ever edited or deleted here either; the table is the door's own
history.

WHAT FOLLOWS A MOVE. Everything derived from the moved turns follows their
source, mechanically, by the provenance every derived row carries since
2026-09-09: records by meta.origin (session and turns), ledger rows by
origin and turns, the session's note. (The old inventory graph's mentions,
edges, nodes and queue batches followed too until the graph left on
2026-09-27.)

WHAT DOES NOT FOLLOW. A record curated before 2026-09-09 carries no origin
and stays where it is (the audit moves those by id). Rules and purposes live on the hub and are per project
already. The chat rows themselves never move: the reader's scope is the
move.

THE READER'S SCOPE. chat_where() gives every reader the SQL that selects
the rows belonging to a project TODAY: the rows filed under it minus the
stretches moved away, plus the stretches moved in. With no moves it is the
plain `project = %s` every reader had, so a box that never moved anything
pays nothing; with moves it stays index-friendly (an equality on project,
session equalities and turn ranges — a bitmap OR, never a computed column).
"""
from __future__ import annotations

import threading
import time

from . import store

DDL = """
CREATE TABLE IF NOT EXISTS session_owner (
    id          bigserial PRIMARY KEY,
    session_id  text NOT NULL,
    turn_lo     int  NOT NULL DEFAULT -2147483648,
    turn_hi     int  NOT NULL DEFAULT 2147483647,
    project     text NOT NULL,
    from_project text NOT NULL DEFAULT '',
    moved_at    timestamptz NOT NULL DEFAULT now(),
    who         text NOT NULL DEFAULT '',
    why         text NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS session_owner_session_idx ON session_owner (session_id, id DESC);
"""

LO, HI = -2147483648, 2147483647
_CACHE_S = 5.0
_cache: dict[str, tuple[float, list]] = {}
_lock = threading.Lock()


def ensure(cur) -> None:
    try:
        store.ensure_ddl(cur, "session_owner", DDL)
    except store.NoTenantBound:
        cur.execute(DDL)


def _tenant() -> str:
    try:
        return store.current_tenant()
    except store.NoTenantBound:
        return ""


def invalidate() -> None:
    with _lock:
        _cache.clear()


def rows(cur, fresh: bool = False) -> list[tuple]:
    """Every move, oldest first: (session_id, turn_lo, turn_hi, project).
    Cached a few seconds per tenant — the hot path reads it on every recall."""
    t = _tenant()
    now = time.monotonic()
    if not fresh:
        with _lock:
            hit = _cache.get(t)
        if hit and now - hit[0] < _CACHE_S:
            return hit[1]
    ensure(cur)
    cur.execute("SELECT session_id, turn_lo, turn_hi, project FROM session_owner "
                "ORDER BY id;")
    out = [(r[0], int(r[1]), int(r[2]), r[3]) for r in cur.fetchall()]
    with _lock:
        _cache[t] = (now, out)
    return out


def resolve(moves: list[tuple]) -> dict[str, list[tuple[int, int, str]]]:
    """Per session, the intervals that were moved and where they belong now
    — the LATEST row covering a turn wins, so a later move of a sub-range
    carves it out of an earlier one."""
    out: dict[str, list[tuple[int, int, str]]] = {}
    for sid, lo, hi, project in moves:
        kept: list[tuple[int, int, str]] = []
        for a, b, p in out.get(sid, []):
            if b < lo or a > hi:
                kept.append((a, b, p))
                continue
            if a < lo:
                kept.append((a, lo - 1, p))
            if b > hi:
                kept.append((hi + 1, b, p))
        kept.append((lo, hi, project))
        out[sid] = sorted(kept)
    return out


def _pred(alias: str, sid: str, lo: int, hi: int, params: list) -> str:
    params.append(sid)
    if lo <= LO and hi >= HI:
        return f"{alias}session_id = %s"
    params.extend([lo, hi])
    return f"({alias}session_id = %s AND {alias}turn_index BETWEEN %s AND %s)"


def chat_where(cur, projects, alias: str = "") -> tuple[str, list]:
    """The SQL (no leading AND) and its params selecting the chat rows that
    belong to `projects` (a name or a list) today. `alias` prefixes the
    columns ("t." for a joined chat)."""
    projs = [projects] if isinstance(projects, str) else list(projects)
    projs = [p for p in projs if p]
    if not projs:
        return "TRUE", []
    try:
        with store.savepoint(cur, "own"):
            moves = rows(cur)
    except Exception:                                          # noqa: BLE001
        moves = []                    # the transaction is intact (savepoint)
    return where_from(resolve(moves) if moves else {}, projs, alias)


def where_from(owned: dict, projects, alias: str = "") -> tuple[str, list]:
    """chat_where's rule on moves already read and resolved — for a reader
    that holds its own connection (the backfill runners read the source
    tenant on an autocommit connection, where a savepoint cannot run)."""
    projs = [projects] if isinstance(projects, str) else [p for p in projects if p]
    a = alias or ""
    if len(projs) == 1:
        base, base_params = f"{a}project = %s", [projs[0]]
    else:
        base, base_params = f"{a}project = ANY(%s)", [projs]
    if not owned:
        return base, base_params
    params: list = []
    away: list[str] = []
    here: list[str] = []
    for sid, ivs in owned.items():
        for lo, hi, p in ivs:
            if p in projs:
                here.append(_pred(a, sid, lo, hi, params))
    away_params: list = []
    for sid, ivs in owned.items():
        for lo, hi, p in ivs:
            if p not in projs:
                away.append(_pred(a, sid, lo, hi, away_params))
    sql = base
    out_params = list(base_params)
    if away:
        sql = f"({base} AND NOT ({' OR '.join(away)}))"
        out_params += away_params
    if here:
        sql = f"({sql} OR {' OR '.join(here)})"
        out_params += params
    return sql, out_params


def effective_project(cur, session_id: str, turn_index: int) -> str:
    """Where one turn belongs today, '' when the archive's own column is
    the truth (no move covers it)."""
    for lo, hi, p in resolve(rows(cur)).get(session_id, []):
        if lo <= turn_index <= hi:
            return p
    return ""


def relabel(cur, hits: list[dict]) -> list[dict]:
    """Chat rows carry the ARCHIVED project column; a reader shows where
    they belong today. In place; cheap (the moves are cached)."""
    try:
        owned = resolve(rows(cur))
    except Exception:                                          # noqa: BLE001
        return hits
    if not owned:
        return hits
    for r in hits:
        ivs = owned.get(str(r.get("session_id") or ""))
        if not ivs:
            continue
        ti = r.get("turn_index")
        for lo, hi, p in ivs:
            if ti is None or lo <= int(ti) <= hi:
                r["project"] = p
                break
    return hits


def history(cur, session_id: str = "") -> list[dict]:
    ensure(cur)
    cur.execute("SELECT id, session_id, turn_lo, turn_hi, project, from_project, "
                "       moved_at, who, why FROM session_owner "
                + ("WHERE session_id = %s " if session_id else "")
                + "ORDER BY id DESC LIMIT 200;", [session_id] if session_id else [])
    return [{"id": r[0], "session_id": r[1], "turn_lo": r[2], "turn_hi": r[3],
             "project": r[4], "from_project": r[5], "moved_at": r[6],
             "who": r[7], "why": r[8]} for r in cur.fetchall()]


# ── the move ────────────────────────────────────────────────────────────────
def _refile(conn, cur, src: str, project: str, session_id: str, whole: bool,
            lo: int, hi: int, out: dict, log: list) -> list[str]:
    """Re-file one source project's derived rows (records, ledger, decision
    lines and their threads, notes)
    for the session/range into `project`; the counts add into `out`.
    Returns the record ids moved. One call per archived project of the
    range."""
    rng_p = [lo, hi] if not whole else []

    # RECORDS by their origin. A record whose id already exists in the
    # target stays, loudly — two records with one id are the merge door's
    # business, not this one's.
    cur.execute("SELECT to_regclass('document');")
    moved_records: list[str] = []
    if src:
        cur.execute(
            "SELECT record_id FROM document WHERE project = %s "
            "AND meta->'origin'->>'session_id' = %s "
            + ("" if whole else "AND coalesce((meta->'origin'->>'turn_lo')::int, -1) BETWEEN %s AND %s "),
            [src, session_id] + rng_p)
        cands = [r[0] for r in cur.fetchall()]
        for rid in cands:
            cur.execute("SELECT 1 FROM document WHERE project = %s AND record_id = %s;",
                        [project, rid])
            if cur.fetchone():
                log.append(f"RECORD kept in {src}: {rid} already exists in {project}")
                continue
            cur.execute("UPDATE document SET project = %s, updated_at = now() "
                        "WHERE project = %s AND record_id = %s;", [project, src, rid])
            moved_records.append(rid)
    out["records"] = out.get("records", 0) + (len(moved_records))

    if src:
        # LEDGER rows by origin and turns; the session's NOTE on a whole move
        cur.execute("SELECT to_regclass('ledger');")
        if (cur.fetchone() or [None])[0]:
            cur.execute(
                "UPDATE ledger SET project = %s WHERE project = %s AND origin = %s "
                + ("" if whole else "AND turn_lo BETWEEN %s AND %s;"),
                [project, src, session_id] + rng_p)
            out["ledger"] = out.get("ledger", 0) + (cur.rowcount or 0)
        # THE DECISION LINES (the graph's history) by session and turns —
        # left behind until 2026-10-03, so a moved session's story stayed in
        # the project it was moved out of. A line's THREAD is per project: a
        # thread whose every line moves goes with them; one split by the cut
        # gets its copy in the target for the moved lines, so no line ever
        # points at another project's thread.
        cur.execute("SELECT to_regclass('decision'), to_regclass('decision_thread');")
        has_d, has_t = cur.fetchone()
        if has_d:
            cur.execute(
                "SELECT id, thread_id FROM decision WHERE project = %s AND session_id = %s "
                + ("" if whole else "AND turn_lo BETWEEN %s AND %s "), [src, session_id] + rng_p)
            lines = cur.fetchall()
            ids = [r[0] for r in lines]
            if ids:
                cur.execute("UPDATE decision SET project = %s WHERE id = ANY(%s);", [project, ids])
                out["lines"] = out.get("lines", 0) + len(ids)
            if has_t:
                for tid in sorted({r[1] for r in lines if r[1]}):
                    cur.execute("SELECT count(*) FROM decision WHERE thread_id = %s AND project = %s;", [tid, src])
                    left = cur.fetchone()[0]
                    if not left:
                        cur.execute("UPDATE decision_thread SET project = %s WHERE id = %s AND project = %s;",
                                    [project, tid, src])
                        continue
                    cur.execute("INSERT INTO decision_thread (project, question, day, last_line, n_lines) "
                                "SELECT %s, question, day, NULL, 0 FROM decision_thread WHERE id = %s RETURNING id;",
                                [project, tid])
                    new_tid = cur.fetchone()[0]
                    cur.execute("UPDATE decision SET thread_id = %s WHERE thread_id = %s AND project = %s;",
                                [new_tid, tid, project])
                    for t in (tid, new_tid):
                        cur.execute("UPDATE decision_thread SET n_lines = (SELECT count(*) FROM decision "
                                    "WHERE thread_id = %s), last_line = (SELECT max(id) FROM decision "
                                    "WHERE thread_id = %s) WHERE id = %s;", [t, t, t])
                    log.append(f"THREAD {tid} split by the move: {left} line(s) stay in {src}, "
                               f"the moved ones under thread {new_tid} in {project}")
        cur.execute("SELECT to_regclass('note');")
        if whole and (cur.fetchone() or [None])[0]:
            cur.execute("DELETE FROM note WHERE project = %s AND session_id = %s "
                        "AND EXISTS (SELECT 1 FROM note x WHERE x.project = %s "
                        "  AND x.session_id = note.session_id "
                        "  AND x.written_at >= note.written_at);",
                        [src, session_id, project])
            cur.execute("DELETE FROM note WHERE project = %s AND session_id = %s "
                        "AND EXISTS (SELECT 1 FROM note x WHERE x.project = %s "
                        "  AND x.session_id = note.session_id);",
                        [project, session_id, src])
            cur.execute("UPDATE note SET project = %s WHERE project = %s AND session_id = %s;",
                        [project, src, session_id])
            out["notes"] = out.get("notes", 0) + (cur.rowcount or 0)
    return moved_records


def move(conn, session_id: str, project: str, turn_lo=None, turn_hi=None,
         who: str = "", why: str = "", log: list | None = None) -> dict:
    """Move a session (or a turn range of it) to `project`. Writes the
    ownership row and re-files every derived row. The caller commits.
    Raises ValueError on a bad ask; never touches chat."""
    log = log if log is not None else []
    session_id = (session_id or "").strip()
    project = (project or "").strip()
    if not session_id or not project:
        raise ValueError("session_id and project are required")
    lo = LO if turn_lo is None or turn_lo == "" else int(turn_lo)
    hi = HI if turn_hi is None or turn_hi == "" else int(turn_hi)
    if lo > hi:
        raise ValueError("turn_lo is past turn_hi")
    whole = lo <= LO and hi >= HI
    cur = conn.cursor()
    ensure(cur)
    cur.execute("SELECT 1 FROM project WHERE name = %s;", [project])
    if not cur.fetchone():
        raise ValueError(f"no such project '{project}' — add_project first")
    cur.execute("SELECT count(*), array_agg(DISTINCT project), min(turn_index), "
                "       max(turn_index) FROM chat WHERE session_id = %s "
                "AND turn_index BETWEEN %s AND %s;", [session_id, lo, hi])
    n_chat, archived, tmin, tmax = cur.fetchone()
    if not n_chat:
        raise ValueError(f"no archived turns for session {session_id[:12]} "
                         f"in that range")
    archived = [p for p in (archived or []) if p]
    # where these turns belong NOW (the archive's column unless a move covers them)
    cur.execute("SELECT project FROM chat WHERE session_id = %s AND turn_index = %s;",
                [session_id, tmin])
    r0 = cur.fetchone()
    before = effective_project(cur, session_id, tmin) or (r0[0] if r0 else (archived[0] if archived else ""))
    if before == project and not any(
            p != project for _, _, p in resolve(rows(cur, fresh=True)).get(session_id, [])
            if not whole):
        cur.execute("SELECT count(*) FROM session_owner WHERE session_id = %s;", [session_id])
        if before == project and archived == [project] and not cur.fetchone()[0]:
            raise ValueError(f"session {session_id[:12]} already belongs to {project}")
    cur.execute("INSERT INTO session_owner (session_id, turn_lo, turn_hi, project, "
                "  from_project, who, why) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id;",
                [session_id, lo, hi, project, before, (who or "")[:80], (why or "")[:400]])
    move_id = cur.fetchone()[0]
    invalidate()
    try:                                  # the proposer's row, if any, is decided by this
        from . import proposer
        proposer.decide(cur, session_id, f"moved to {project}")
    except Exception:                                        # noqa: BLE001
        pass
    out = {"move_id": move_id, "session_id": session_id, "project": project,
           "from": before, "turns": int(n_chat), "turn_lo": lo, "turn_hi": hi,
           "whole": whole}
    # THE SOURCES: the range's rows belong to `before` now, but an imported
    # session's rows can sit under several archived projects (2026-09-09: a
    # blob the harness had filed under two columns) — every archived project
    # of the range is a source, each re-filed.
    srcs = [x for x in dict.fromkeys([before] + archived) if x and x != project]
    src = srcs[0] if srcs else ""
    for s_ in srcs:
        _refile(conn, cur, s_, project, session_id, whole, lo, hi, out, log)
    log.append(f"MOVED {session_id[:12]} {'whole' if whole else f'turns {lo}-{hi}'} "
               f"{src or '?'} -> {project}: {n_chat} turns, {out.get('records', 0)} records, "
               f"{out.get('ledger', 0)} ledger rows, {out.get('notes', 0)} notes")
    return out
