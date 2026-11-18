"""THE HEAL QUEUE — one FIFO per project, one consumer, arrival order IS truth
order.

Founder 2026-09-01: "the things coming to the heal queue and saved one by one.
Then we don't have any locks... What came in first is saved first, what came in
last is saved the last. So we know that the last record which came is the
newest one. I think it's a hundred times better than the lock."

He is right, and it retires three mechanisms at once:

  ORDERING    `id` ascending IS the timeline. Nothing has to infer recency from
              a timestamp, which a hand pass or a restore can restamp — the bug
              that put six identical dates in the session brief.
  EXCLUSION   at most one row 'running' per project, enforced by a UNIQUE
              PARTIAL INDEX rather than by a lock in application code, so two
              reconciles can never read the same before-state and each conclude
              there is no collision. The advisory lock this replaces would have
              bought exclusion and nothing else.
  DURABILITY  a hub outage is a queue that waits, not a cursor that advances
              past unread work. The watermark this replaces already lost ~200
              records exactly that way (measured 2026-08-30).

WHAT IT HOLDS IS A POINTER, NOT THE RECORD. The record is written to `document`
first and its id enqueued; heal then reconciles it against what was already
stored and may well delete it again as a duplicate. That order is deliberate: a
queue carrying the only copy would make SAVING depend on a reconciler running,
and "memory is an enhancement, never a gate" is the older law. Every property
asked for — one door, timeline order, no watermark, no sweep, heal fixes
everything at once — is unaffected by writing the row a moment earlier.

A FAILURE IS DROPPED, NOT PARKED (founder 2026-09-01: "skip, delete, remove...
We didn't record it. It's gone. We don't care."). A dead-letter table nobody
walks is the junk pile the tidiness rule forbids, and the same law already
governs an uncurated exchange when the provider is down. Dropped LOUDLY — that
no failure is silent is the one guard that survives.
"""
from __future__ import annotations

from . import store

# RETRIES ARE SPACED, NOT BURST. The first schedule was three tries fifteen
# seconds apart, then the record was dropped from heal for good. Measured
# 2026-09-02 in the harbor pipe test: the provider slowed for ~70 s (three
# heal calls answered 60-70 s late, one curator call refused outright), the
# box gave up at 15 s three times in 45 s, and a record that would have been
# folded a minute later was announced "never reconciled". A minute of
# provider trouble must not cost a customer a heal. Attempt n fails -> the
# row waits BACKOFF_MINUTES[n-1] before it is claimable again; past the last
# entry it is dropped, loudly, as before.
BACKOFF_MINUTES = (1, 5, 30, 120, 720)
MAX_ATTEMPTS = len(BACKOFF_MINUTES) + 1
STALE_MINUTES = 15


def backoff_minutes(attempts: int) -> int | None:
    """Minutes to wait after the given (1-based) failed attempt, or None when
    the row has used its last attempt and is dropped."""
    if attempts >= MAX_ATTEMPTS:
        return None
    return BACKOFF_MINUTES[min(attempts, len(BACKOFF_MINUTES)) - 1]

_DDL = """
CREATE TABLE IF NOT EXISTS heal_queue (
    id          bigserial   PRIMARY KEY,
    project     text        NOT NULL,
    record_id   text        NOT NULL,
    status      text        NOT NULL DEFAULT 'pending',
    attempts    integer     NOT NULL DEFAULT 0,
    last_error  text        NOT NULL DEFAULT '',
    enqueued_at timestamptz NOT NULL DEFAULT now(),
    started_at  timestamptz
);
-- next_try (2026-09-02): a failed attempt waits before it is claimable.
ALTER TABLE heal_queue ADD COLUMN IF NOT EXISTS next_try timestamptz
    NOT NULL DEFAULT now();
CREATE INDEX IF NOT EXISTS heal_queue_next
    ON heal_queue (project, id) WHERE status = 'pending';
-- THE EXCLUSION, in the one place that cannot race: two claims that both see
-- "nothing running" under READ COMMITTED would both proceed. A unique index
-- makes the second one fail instead, and a failed claim is just "busy".
CREATE UNIQUE INDEX IF NOT EXISTS heal_queue_one_running
    ON heal_queue (project) WHERE status = 'running';
"""


def ensure(cur) -> None:
    store.ensure_ddl(cur, "heal_queue", _DDL)


def enqueue(conn, project: str, record_id: str) -> int:
    """Put a saved record in line to be reconciled. Returns the queue id."""
    cur = conn.cursor()
    ensure(cur)
    cur.execute("INSERT INTO heal_queue (project, record_id) "
                "VALUES (%s, %s) RETURNING id;", [project, record_id])
    return int(cur.fetchone()[0])


def claim(conn, project: str):
    """The oldest pending row for this project, or None if the project is
    already busy or empty. Returns (queue_id, record_id, attempts)."""
    import psycopg
    cur = conn.cursor()
    ensure(cur)
    try:
        cur.execute(
            "UPDATE heal_queue SET status = 'running', "
            "  attempts = attempts + 1, started_at = now() "
            "WHERE id = (SELECT id FROM heal_queue "
            "            WHERE project = %s AND status = 'pending' "
            "              AND next_try <= now() "
            "            ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED) "
            "RETURNING id, record_id, attempts;", [project])
    except psycopg.errors.UniqueViolation:
        # Another consumer holds this project. Not an error — the whole point.
        conn.rollback()
        return None
    row = cur.fetchone()
    conn.commit()
    return row


def peek_more(conn, project: str, exclude_id: int, limit: int = 3) -> list[tuple[int, str]]:
    """Up to `limit` further pending, due rows of this project, oldest first,
    WITHOUT claiming them — for the consumer that already holds the
    project's one running row. That row is the lock: no second consumer can
    claim while it stands, so these rows are safe to reconcile in the same
    model call (founder 2026-09-03: one call per save). If the run dies they
    are still pending and go with the next leader. Returns (id, record_id)."""
    cur = conn.cursor()
    cur.execute("SELECT id, record_id FROM heal_queue WHERE project = %s "
                "  AND status = 'pending' AND next_try <= now() AND id <> %s "
                "ORDER BY id LIMIT %s;", [project, exclude_id, limit])
    return [(int(r[0]), str(r[1])) for r in cur.fetchall()]


POOL = 4                    # records that make a heal call worth making


def ready(conn, project: str, pool: int = POOL) -> tuple[bool, int]:
    """HEAL BY FOUR (founder 2026-09-03): a save no longer fires a heal
    call; the queue pools until `pool` records are due for the project.
    No clock and no compact (founder: "how long we wait makes no
    difference — if they're slow the records can wait, if they're working
    the fourth message comes soon; what has compact to do with it?").
    Order is kept — a pool is reconciled oldest first. Returns (ready, due)."""
    cur = conn.cursor()
    ensure(cur)
    cur.execute("SELECT count(*) FROM heal_queue WHERE project = %s "
                "AND status = 'pending' AND next_try <= now();", [project])
    n = int(cur.fetchone()[0] or 0)
    return n >= pool, n


def due(conn, project: str, limit: int = 4) -> list[tuple[int, str]]:
    """Pending rows that are due, oldest first, WITHOUT claiming them — the
    agent-mode heal shows them to the customer's own agent inside a task and
    the task's completion finishes them (2026-09-02). Returns (id, record_id)."""
    cur = conn.cursor()
    ensure(cur)
    cur.execute("SELECT id, record_id FROM heal_queue WHERE project = %s "
                "  AND status = 'pending' AND next_try <= now() "
                "ORDER BY id LIMIT %s;", [project, limit])
    return [(int(r[0]), str(r[1])) for r in cur.fetchall()]


def finish(conn, queue_id: int) -> None:
    """Done is GONE. Completed rows are not kept — a table of finished work is
    a pile nobody reads, and the records themselves are the evidence."""
    cur = conn.cursor()
    cur.execute("DELETE FROM heal_queue WHERE id = %s;", [queue_id])
    conn.commit()


def fail(conn, queue_id: int, err: str) -> bool:
    """Put it back, or drop it for good. Returns True when it was DROPPED.

    The caller must say so out loud — a dropped record is a fact about this
    customer's memory, not a log line to swallow.
    """
    cur = conn.cursor()
    cur.execute("SELECT attempts, project, record_id FROM heal_queue "
                "WHERE id = %s;", [queue_id])
    row = cur.fetchone()
    if not row:
        conn.commit()
        return False
    attempts = int(row[0] or 0)
    wait = backoff_minutes(attempts)
    if wait is None:
        cur.execute("DELETE FROM heal_queue WHERE id = %s;", [queue_id])
        conn.commit()
        return True
    cur.execute("UPDATE heal_queue SET status = 'pending', last_error = %s, "
                "  next_try = now() + make_interval(mins => %s) "
                "WHERE id = %s;", [str(err)[:500], wait, queue_id])
    conn.commit()
    return False


def reclaim(conn, project: str = "") -> int:
    """Free rows a dead process left 'running'.

    Without this the unique partial index — the very thing that guarantees one
    consumer — would block a project's queue forever after one crash. The
    exclusion that cannot be released is worse than no exclusion at all.
    """
    cur = conn.cursor()
    ensure(cur)
    where = "status = 'running' AND started_at < now() - make_interval(mins => %s)"
    args: list = [STALE_MINUTES]
    if project:
        where += " AND project = %s"
        args.append(project)
    cur.execute(f"UPDATE heal_queue SET status = 'pending' WHERE {where} "
                f"RETURNING id;", args)
    n = len(cur.fetchall())
    conn.commit()
    return n


def pending_projects(conn) -> list[str]:
    """Every project with work waiting, oldest first.

    A trigger only ever knows the project whose record just landed, so a queue
    orphaned by a restart — its drain thread died mid-flight — would wait for
    that project to receive another record, which for a quiet project is
    never. Found on the first live run: six bv rows sat pending with attempts=0
    while brain and ms drained repeatedly around them. Any trigger now clears
    every orphan, so activity anywhere heals a queue left anywhere.
    """
    cur = conn.cursor()
    ensure(cur)
    cur.execute("SELECT project FROM heal_queue WHERE status = 'pending' "
                "  AND next_try <= now() GROUP BY project ORDER BY min(id);")
    return [r[0] for r in cur.fetchall()]


def depth(conn, project: str = "") -> int:
    """Pending rows. A queue that only grows is the loud signal that heal has
    stopped working — there is no watermark left to hide it."""
    cur = conn.cursor()
    ensure(cur)
    if project:
        cur.execute("SELECT count(*) FROM heal_queue WHERE status = 'pending' "
                    "AND project = %s;", [project])
    else:
        cur.execute("SELECT count(*) FROM heal_queue WHERE status = 'pending';")
    return int(cur.fetchone()[0])
