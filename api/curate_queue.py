"""The PRUNE lane — cleanup_history's queue: delete old archive rows, then
VACUUM FULL, one job per tenant at a time.

This file was the compact-era CURATE queue (founder 2026-07-26: "keep a queue
of everything coming, in the worst case we lag a bit"). That lane died when
every exchange started reaching the hub as it happens (2026-08-30) and was
deleted on 2026-09-02 with the /v1/curate route it fed. What stayed is the
one thing a customer still asks this box to do on its own: cleanup_history
(founder 2026-08-29, delete-only, 90-day floor). The wake channel keeps its
old name "curate" — it is a board key, not a promise.
"""
from __future__ import annotations

import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

from core import ranking, store, wake

POLL_S = 60.0
MAX_BACKOFF_S = 3600.0
# One VACUUM FULL at a time per box.
CONCURRENCY = 1

_DDL = """
CREATE TABLE IF NOT EXISTS prune_job (
    id          bigserial PRIMARY KEY,
    project     text NOT NULL,
    older_than_days int NOT NULL,
    status      text NOT NULL DEFAULT 'pending',
    attempts    int  NOT NULL DEFAULT 0,
    last_error  text NOT NULL DEFAULT '',
    result      text NOT NULL DEFAULT '',
    next_try    timestamptz NOT NULL DEFAULT now(),
    requested_at timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
"""


def enqueue_prune(project: str, older_than_days: int) -> str:
    """Called from cleanup_history past its confirm, inside a bound tenant
    context. One prune at a time per tenant — a second request while one is
    pending/running reports it instead of stacking."""
    with store.connection() as conn:
        cur = conn.cursor()
        store.ensure_ddl(cur, "curate_queue", _DDL)
        cur.execute("SELECT id, project, status FROM prune_job "
                    "WHERE status IN ('pending', 'running') LIMIT 1;")
        row = cur.fetchone()
        if row:
            conn.commit()
            return (f"A cleanup is already {row[2]} for project "
                    f"'{row[1]}' — one at a time; check back shortly.")
        cur.execute(
            "INSERT INTO prune_job (project, older_than_days) "
            "VALUES (%s, %s);", [project, older_than_days])
        conn.commit()
    wake.poke("curate", store.current_tenant())
    return ""

def last_prune_result(project: str) -> str:
    with store.connection() as conn:
        cur = conn.cursor()
        store.ensure_ddl(cur, "curate_queue", _DDL)
        conn.commit()
        cur.execute(
            "SELECT status, result, last_error, updated_at::date "
            "FROM prune_job WHERE project = %s "
            "ORDER BY id DESC LIMIT 1;", [project])
        row = cur.fetchone()
    if not row:
        return ""
    status, result, err, day = row
    if status == "done":
        return f"Last cleanup ({day}): {result}"
    if status == "failed":
        return f"Last cleanup FAILED ({day}): {err} — nothing was deleted."
    return f"Cleanup {status} (requested {day})."

def _due_prune_jobs(tenant: str) -> list[tuple[int, str, int, int]]:
    tok = store.bind(tenant)
    try:
        with store.connection() as conn:
            cur = conn.cursor()
            store.ensure_ddl(cur, "curate_queue", _DDL)
            conn.commit()
            cur.execute(
                "SELECT id, project, older_than_days, attempts "
                "FROM prune_job WHERE status = 'pending' AND next_try <= now() "
                "ORDER BY requested_at LIMIT 1;")
            return cur.fetchall()
    finally:
        store.unbind(tok)

def _run_prune_job(tenant: str, job_id: int, project: str, days: int,
                   attempts: int) -> None:
    from core import prune                        # lazy: keeps import cheap
    tok = store.bind(tenant)
    try:
        res = prune.run_prune(project, days)
        with store.connection() as conn:
            conn.cursor().execute(
                "UPDATE prune_job SET status='done', result=%s, "
                "  updated_at=now() WHERE id=%s;",
                [f"removed {res['deleted']} archived rows, "
                 f"freed {res['freed_mb']} MB", job_id])
            conn.commit()
        for line in res["log"]:
            print(f"[prune] {tenant}/{project}: {line}", flush=True)
        wake.poke("curate", tenant)      # LIMIT 1 — a queued prune follows
    except Exception as e:
        traceback.print_exc()
        # A crash before the DELETE commits deleted nothing, and a re-run of
        # the same cutoff is idempotent. Three strikes → failed.
        status = "failed" if attempts + 1 >= 3 else "pending"
        with store.connection() as conn:
            conn.cursor().execute(
                "UPDATE prune_job SET status=%s, attempts=%s, last_error=%s, "
                "  next_try=now() + interval '300 seconds', updated_at=now() "
                "WHERE id=%s;",
                [status, attempts + 1, f"{type(e).__name__}: {e}"[:300],
                 job_id])
            conn.commit()
        if status == "pending":
            wake.poke_later("curate", tenant, 301.0)
    finally:
        store.unbind(tok)

def _claim(tenant: str, table: str, where: str, arg) -> None:
    """Mark a job running SYNCHRONOUSLY, so the next poll cycle cannot
    double-submit it before the worker thread has started."""
    tok = store.bind(tenant)
    try:
        with store.connection() as conn:
            conn.cursor().execute(
                f"UPDATE {table} SET status='running', updated_at=now() "
                f"WHERE {where};", [arg])
            conn.commit()
    finally:
        store.unbind(tok)


def _dispatch(tenant: str, pool: ThreadPoolExecutor) -> None:
    """Every due prune on one tenant, claimed then handed to the pool.

    Lifted out of _loop (2026-08-13) so the loop can catch TenantGone for a
    SINGLE tenant — a customer deleted mid-cycle used to abort the whole
    pass, taking every other tenant's due work with it."""
    for job_id, project, days, attempts in _due_prune_jobs(tenant):
        _claim(tenant, "prune_job", "id=%s", job_id)
        pool.submit(_run_prune_job, tenant, job_id, project, days, attempts)


def _backlog(tenant: str) -> int:
    """Pending prunes RIGHT NOW — the ranking's demotion input."""
    tok = store.bind(tenant)
    try:
        with store.connection() as conn:
            cur = conn.cursor()
            store.ensure_ddl(cur, "curate_queue", _DDL)
            cur.execute("SELECT count(*) FROM prune_job WHERE status='pending';")
            return int(cur.fetchone()[0])
    finally:
        store.unbind(tok)


def _loop() -> None:
    pool = ThreadPoolExecutor(max_workers=CONCURRENCY)
    # Crash recovery: this process owns every running job, so any row still
    # 'running' at startup is an orphan from a dead worker. Prune re-runs are
    # safe: same cutoff, idempotent delete.
    try:
        boot = store.provisioned_tenants()
        for tenant in boot:
            tok = store.bind(tenant)
            try:
                with store.connection() as conn:
                    cur = conn.cursor()
                    store.ensure_ddl(cur, "curate_queue", _DDL)
                    cur.execute("UPDATE prune_job SET status='pending', "
                                "next_try=now() WHERE status='running';")
                    if cur.rowcount:
                        print(f"[prune] recovered {cur.rowcount} orphaned "
                              f"prune job(s) for {tenant}", flush=True)
                    conn.commit()
            finally:
                store.unbind(tok)
        wake.poke_all("curate", boot)   # work enqueued before the restart
    except Exception:
        traceback.print_exc()
    wake.mark_swept("curate")
    while True:
        try:
            for tenant in ranking.order(
                    wake.wait("curate", store.provisioned_tenants,
                              timeout=POLL_S),
                    _backlog):
                try:
                    _dispatch(tenant, pool)
                except store.TenantGone:
                    wake.forget("curate", tenant)
                    print(f"[prune] tenant {tenant} is gone — dropped "
                          f"from the board", flush=True)
        except Exception:
            traceback.print_exc()


def start() -> None:
    t = threading.Thread(target=_loop, name="prune-queue", daemon=True)
    t.start()
    print("[prune] queue started", flush=True)
