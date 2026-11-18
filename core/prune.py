"""History cleanup — the owner's delete-only door, floored at 90 days.

THE DOCTRINE, PRESERVED: chat is immutable to the SYSTEM. No curator, no
heal, no background job ever deletes a chat row. This module exists for the
one actor with the right to prune: the data's OWNER, deliberately, with the
cutoff spelled out and a confirm string typed. rules:chat_history_immutable
bans silent deletion; this is the opposite of silent.

DELETE-ONLY (founder, 2026-08-29): there is no summarize mode and no digest.
Record hygiene is heal's job; this door exists solely to let an owner shed
old raw bulk. The 90-day floor is the protection mechanism — the connection
refuses to delete anything younger, so the recent archive memory depends on
cannot be stripped by a misbehaving or injected agent. Outside our software
the data is theirs and they can do as they like; through our software the
floor holds.

WHY VACUUM FULL IS NOT OPTIONAL: measured 2026-07-30 on t_soak — deleting
50,000 rows freed exactly 0 bytes until VACUUM FULL ran, then 707→649 MB and
pg_database_size (the number we bill on) actually dropped. A prune that
skips the vacuum charges the customer for space they freed. VACUUM FULL
takes an ACCESS EXCLUSIVE lock on `chat` for seconds-to-minutes; archive
writes during it WAIT, they do not fail — which is why the whole operation
runs from the queue, never inline in a request.
"""
from __future__ import annotations

from . import store

# 90, NOT 7 — founder rule 2026-07-30, two reasons: (1) memory needs the
# recent raw archive to work properly (chat streams feed recall), and
# (2) THE HOLE: curated records + graph SURVIVE pruning, so with a short
# floor a user could prune to a few weeks of raw chat and keep ~95% of
# memory function while paying for almost no storage. Three months of
# archive is the non-negotiable base every tenant carries.
MIN_AGE_DAYS = 90


def preview(project: str, older_than_days: int) -> dict:
    """What WOULD be pruned — always safe, never writes. The dry run IS the
    default behavior of the tool; deletion happens only past a confirm."""
    days = max(int(older_than_days), MIN_AGE_DAYS)
    with store.connection() as conn:
        cur = conn.cursor()
        from . import ownership
        w, a = ownership.chat_where(cur, project)
        cur.execute(
            f"SELECT count(*), coalesce(sum(length(text)),0), "
            f"       count(DISTINCT session_id), min(ts)::date, max(ts)::date "
            f"FROM chat WHERE ({w}) "
            f"  AND ts < now() - make_interval(days => %s);",
            a + [days])
        rows, chars, sessions, oldest, newest = cur.fetchone()
        cur.execute("SELECT pg_size_pretty(pg_database_size(current_database()));")
        db_now = cur.fetchone()[0]
    return {"project": project, "older_than_days": days, "rows": rows,
            "sessions": sessions, "text_mb": round(chars / 1048576, 1),
            "oldest": str(oldest), "newest": str(newest),
            "db_size_now": db_now}


def run_prune(project: str, older_than_days: int) -> dict:
    """The queued job body: delete → VACUUM FULL → report.
    Runs inside a bound tenant context."""
    days = max(int(older_than_days), MIN_AGE_DAYS)
    log: list[str] = []
    with store.connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT pg_database_size(current_database());")
        before = cur.fetchone()[0]
        from . import ownership
        w, a = ownership.chat_where(cur, project)
        cur.execute(
            f"DELETE FROM chat WHERE ({w}) "
            f"  AND ts < now() - make_interval(days => %s);",
            a + [days])
        deleted = cur.rowcount
        conn.commit()
    # VACUUM FULL cannot run inside a transaction — dedicated autocommit
    # connection, and per-table so only `chat` is locked.
    import psycopg
    with psycopg.connect(host=store.PG_HOST, port=store.PG_PORT,
                         user=store.PG_USER, password=store.PG_PASS,
                         dbname=store.tenant_db(store.current_tenant()),
                         autocommit=True) as vconn:
        vconn.execute("VACUUM FULL chat;")
        cur = vconn.execute("SELECT pg_database_size(current_database());")
        after = cur.fetchone()[0]
    freed = round((before - after) / 1048576, 1)
    log.append(f"deleted {deleted} rows, VACUUM FULL freed {freed} MB "
               f"({round(before/1048576)}→{round(after/1048576)} MB)")
    return {"deleted": deleted, "freed_mb": freed, "log": log}
