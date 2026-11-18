"""THE WINDOW ON THE BOX (founder 2026-09-24: "curate every five exchanges",
cached on the container and pushed at five; 2026-09-25: TWO curator calls
per window — records and graph lines — each reading the chat).

The box archives every exchange at once (the archive never waits) and
keeps the piece here, in `hub_pending`, until a window is ready: five
pieces of one session, or fewer at a compact or a save (no timers since
2026-10-02, the founder: "five messages"). A window
goes up as ONE post: the pieces under "EXCHANGE k of n" headers with their
dates (the shape both prompts were tuned on), the turn range of each, the
threads view the lines writer needs (`decisionline.open_threads_near`, the
box's embedder), the records this session already wrote, and — when the
window is pushed by the person's next prompt — that prompt as THE NEXT
MESSAGE (2026-09-23: 33/48 against 29/48 on the agreement set).

Rows survive a restart: a window the box never sent is still pending, not
lost. What the hub refused or never took stays refused (no-backfill)."""
from __future__ import annotations

import datetime as _dt
import os

DDL = """
CREATE TABLE IF NOT EXISTS hub_pending (
    id          bigserial PRIMARY KEY,
    project     text NOT NULL,
    session_id  text NOT NULL,
    piece       text NOT NULL,
    turn_lo     integer NOT NULL DEFAULT -1,
    turn_hi     integer NOT NULL DEFAULT -1,
    ts          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS hub_pending_session ON hub_pending (project, session_id, id);
"""

def ensure(cur) -> None:
    """The pending table, through store.ensure_ddl — the one DDL door (a
    catalog probe with no lock, the first run serialized on an advisory lock,
    memoised only once the table is seen durable).

    Two hand-rolled versions failed here. 2026-10-05: every hook ran the DDL
    ahead of its insert and a burst of ~30 stop hooks deadlocked 39 times.
    2026-11-10 (the Aurora test box): the memo was written inside the
    caller's transaction, a fresh box's first _push() rolled back with
    nothing to send, and the process went on believing in a table that never
    existed — every exchange after it failed to queue and nothing reached
    the curator."""
    from . import store
    store.ensure_ddl(cur, "hub_pending", DDL)


WINDOW = int(os.environ.get("BRAIN_WINDOW", "5"))            # pieces per window
# NO TIMERS (founder 2026-10-02: "I wouldn't put the timer. I would put five
# messages"). A window goes at five exchanges, at a compact, or at a save —
# events only. The 15-minute / 5-quiet-minute timers decided nearly every
# window (live windows averaged 1.9 exchanges, 80 in 38 on 2026-10-02), each
# paying the brief, the 20 records and Jev in full, and running closer to the
# one-exchange shape (75% of the key) than the measured window of five (87%).
# A session that ends with fewer than five waiting keeps them until it goes on.
PIECE_HEAD, PIECE_TAIL = 12000, 28000                        # one piece's cut, as the bench's
SO_FAR = 200                                                 # the session's records the still-true check reads
SO_FAR_NEAR = 20                                             # the curator's: the nearest this window
SO_FAR_FALLBACK = 100                                        # the newest, when the window has no vectors
SESSION_TEXT = 400                                           # characters of a record for the check


def _cut(piece: str, head: int = PIECE_HEAD, tail: int = PIECE_TAIL) -> str:
    """An oversized piece keeps its head and its tail, and says so."""
    if len(piece) <= head + tail:
        return piece
    return (piece[:head] + f"\n\n[… {len(piece) - head - tail} characters of this exchange not shown …]\n\n"
            + piece[-tail:])


def add(cur, project: str, session_id: str, pieces: list[tuple[str, int, int]]) -> int:
    """Keep the pieces; answer how many of this session are pending."""
    ensure(cur)
    for piece, lo, hi in pieces:
        if str(piece or "").strip():
            cur.execute("INSERT INTO hub_pending (project, session_id, piece, turn_lo, turn_hi) "
                        "VALUES (%s, %s, %s, %s, %s);", [project, session_id, _cut(piece), int(lo), int(hi)])
    cur.execute("SELECT count(*) FROM hub_pending WHERE project = %s AND session_id = %s;", [project, session_id])
    return int(cur.fetchone()[0])


def take(cur, project: str, session_id: str, n: int = WINDOW) -> list[tuple[int, str, int, int, _dt.datetime]]:
    """The oldest n pieces of a session, removed from the table — the caller
    sends them. A hub that refuses them (4xx) is final; a hub that cannot be
    reached gets them back through put_back()."""
    cur.execute("DELETE FROM hub_pending WHERE id IN (SELECT id FROM hub_pending WHERE project = %s "
                "AND session_id = %s ORDER BY id LIMIT %s) RETURNING id, piece, turn_lo, turn_hi, ts;",
                [project, session_id, n])
    return sorted(cur.fetchall(), key=lambda r: r[0])


def put_back(cur, project: str, session_id: str, rows) -> None:
    """A WINDOW LAGS, IT IS NEVER LOST (2026-10-03): pieces whose send never
    reached the hub go back under their own ids, so they stay the oldest of
    the session and leave first on the next push or the flusher's tick."""
    cur.executemany("INSERT INTO hub_pending (id, project, session_id, piece, turn_lo, turn_hi, ts) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING;",
                    [(r[0], project, session_id, r[1], r[2], r[3], r[4]) for r in rows])


def due(cur, now: _dt.datetime | None = None) -> list[tuple[str, str]]:
    """Sessions holding a full window (five pieces) that has not gone yet —
    the flusher's catch for a push that failed; no clock decides a window."""
    ensure(cur)
    now = now or _dt.datetime.now(_dt.timezone.utc)
    cur.execute("SELECT project, session_id, min(ts), max(ts), count(*) FROM hub_pending "
                "GROUP BY project, session_id;")
    out = []
    for project, sid, oldest, newest, n in cur.fetchall():
        if n >= WINDOW:
            out.append((project, sid))
    return out


def _nearest(cur, project: str, session_id: str, vecs: list, session: list) -> list[dict]:
    """THE CURATOR'S LIST, SEARCHED (founder 2026-10-02: "they should be
    searched once. What's the point putting the last ones?"). The session's
    records nearest this window by meaning — each record's best distance to
    any of the user's words, the vectors the history view already made —
    plus every record not embedded yet (the newest, just written), in the
    session's order. Measured on 24 windows of a 1,661-record session (the
    27B, production text, scripts/ops/sofar_cap_bench.py): the 20 nearest
    (6,100 characters) wrote no record repeating one out of sight and
    rewrote 7 in place; the newest 200 (59,500) 0 and 5; the newest 40, 5
    and 0. No vectors (the embedder down): the newest SO_FAR_FALLBACK."""
    from .decisionline import _vec
    if not vecs:
        if session:
            print(f"[hubwindow] {project}/{session_id[:8]}: no window vectors — the newest {SO_FAR_FALLBACK} records "
                  "go to the curator", flush=True)
        return session[-SO_FAR_FALLBACK:]
    best: dict[str, float] = {}
    for v in vecs:
        # exact distances over this session's records (a few thousand at most):
        # an HNSW scan filtered to one session can come back short
        cur.execute("WITH s AS MATERIALIZED (SELECT record_id, embedding FROM document WHERE project = %s "
                    "AND meta->'origin'->>'session_id' = %s AND record_id <> 'memory_index' AND embedding IS NOT NULL) "
                    "SELECT record_id, embedding <=> %s::halfvec FROM s ORDER BY 2 LIMIT %s;",
                    [project, session_id, _vec(v), SO_FAR_NEAR])
        for rid, dist in cur.fetchall():
            best[rid] = min(best.get(rid, 9.0), float(dist))
    keep = [r for r, _ in sorted(best.items(), key=lambda x: x[1])[:SO_FAR_NEAR]]
    cur.execute("SELECT coalesce(title,''), coalesce(content,''), record_id, coalesce(type,'') FROM document "
                "WHERE project = %s AND meta->'origin'->>'session_id' = %s AND record_id <> 'memory_index' "
                "AND (record_id = ANY(%s) OR embedding IS NULL) ORDER BY updated_at DESC LIMIT %s;",
                [project, session_id, keep, 2 * SO_FAR_NEAR])
    return [{"title": t, "content": c, "id": i, "type": k} for t, c, i, k in reversed(cur.fetchall())]


def near_subjects(cur, project: str, session_id: str, queries: list[str], each: int = 5, cap: int = 25) -> list[dict]:
    """THE STILL-TRUE CHECK'S LIST, SEARCHED (founder 2026-10-02: "putting 200
    records every time is stupid … it need to be done the intelligent way"):
    for each record the window's records call just wrote, the session's
    `each` records nearest it — the ones on its subject, the only ones it can
    overturn — best first, at most `cap`. Measured: on sheet 33's 48 stale,
    17 caught against 18 for every record; on 24 windows of a 200-record
    session 307 records asked instead of 4,800 (5,853 Jev tokens a window
    against 43,266); every record checked re-flagged the same 29 stale-looking
    records window after window (154 of 191 flags), a sweep, not a check."""
    from . import embed
    from .decisionline import _vec
    vecs = embed.embed([q[:2000] for q in queries], kind="query") if queries else []
    best: dict[str, float] = {}
    for v in vecs or []:
        if not v:
            continue
        cur.execute("WITH s AS MATERIALIZED (SELECT record_id, embedding FROM document WHERE project = %s "
                    "AND meta->'origin'->>'session_id' = %s AND record_id <> 'memory_index' AND embedding IS NOT NULL) "
                    "SELECT record_id, embedding <=> %s::halfvec FROM s ORDER BY 2 LIMIT %s;",
                    [project, session_id, _vec(v), each])
        for rid, dist in cur.fetchall():
            best[rid] = min(best.get(rid, 9.0), float(dist))
    keep = [r for r, _ in sorted(best.items(), key=lambda x: x[1])[:cap]]
    if not keep:
        return []
    cur.execute("SELECT record_id, coalesce(type,''), coalesce(title,''), coalesce(content,'') FROM document "
                "WHERE project = %s AND record_id = ANY(%s);", [project, keep])
    rows = {i: (k, t, c) for i, k, t, c in cur.fetchall()}
    return [{"id": i, "type": rows[i][0], "title": rows[i][1], "content": rows[i][2][:SESSION_TEXT]} for i in keep if i in rows]


def records_lists(cur, project: str, session_id: str, vecs: list) -> tuple[list[dict], list[dict]]:
    """(the curator's list, the still-true check's list) for one window — the
    live window and the records rebuild both use this, so the rebuild asks
    exactly what production asks (2026-10-05). The records this session already
    wrote, oldest first, by origin (2026-10-02: with their id and type — the
    hub's still-true check names the ones a window overtook, and the box
    applies only to these); the curator gets the 20 nearest of them."""
    cur.execute("SELECT coalesce(title,''), coalesce(content,''), record_id, coalesce(type,'') FROM document "
                "WHERE project = %s AND meta->'origin'->>'session_id' = %s AND record_id <> 'memory_index' "
                "ORDER BY updated_at DESC LIMIT %s;", [project, session_id, SO_FAR])
    session = [{"title": t, "content": c, "id": i, "type": k} for t, c, i, k in reversed(cur.fetchall())]
    return _nearest(cur, project, session_id, vecs, session), session


def build(cur, project: str, session_id: str, rows: list, nxt: str = "") -> dict:
    """The window's body for the hub's door."""
    from . import decisionline
    n = len(rows)
    parts, ranges, days = [], [], []
    for k, (_id, piece, lo, hi, ts) in enumerate(rows, 1):
        day = ""
        if lo >= 0:
            cur.execute("SELECT min(ts) FROM chat WHERE session_id = %s AND turn_index BETWEEN %s AND %s;",
                        [session_id, lo, hi])
            got = cur.fetchone()
            day = str(got[0])[:16] if got and got[0] else ""
        day = day or str(ts)[:16]
        days.append(day)
        ranges.append([int(lo), int(hi)])
        parts.append(f"===== EXCHANGE {k} of {n} — {day} =====\n\n{piece}" if n > 1 else piece)
    text = "\n\n".join(parts)
    # the threads view: the questions this project has so far, the recently
    # moved and those nearest this window by meaning
    # through the one DDL door (2026-11-10): the bare DDL here ran its
    # ALTER TABLE … ADD COLUMN IF NOT EXISTS on EVERY window, which takes
    # ACCESS EXCLUSIVE on `decision` even when the columns exist — two
    # windows built at once deadlocked (measured on the Aurora test box) and
    # the loser's exchanges were never curated
    from . import store
    store.ensure_ddl(cur, "decision", decisionline.DDL)
    # the view: the open questions plus the nearest existing lines (V9,
    # 2026-09-26: reversals chained 58% → 80% on the rig)
    vecs: list = []
    threads, near = decisionline.view_near(cur, project, text, vecs_out=vecs)
    so_far, session = records_lists(cur, project, session_id, vecs)
    los = [r[0] for r in ranges if r[0] >= 0]
    his = [r[1] for r in ranges if r[1] >= 0]
    return {"session_id": session_id, "piece": text, "n": n, "ranges": ranges, "days": days,
            "turn_lo": min(los) if los else -1, "turn_hi": max(his) if his else -1,
            "threads": threads, "near_lines": near, "records_so_far": so_far,
            "records_session": [{**r, "content": r["content"][:SESSION_TEXT]} for r in session],
            "next": " ".join(str(nxt or "").split())[:6000]}
