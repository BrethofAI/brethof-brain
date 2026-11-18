"""THE RETIREMENT SWEEP, the box's half (founder 2026-10-05: "we need the real
solution, which will really clean it … something should be doing this
straight away"; 2026-10-06: "the cleanup is done in the app … for customers
we don't have access to their memories"; 2026-10-07: "Yes, wire it in").

A history line that drops or replaces a thing makes every record still
describing that thing as current false at once — records of other sessions,
written weeks earlier, nowhere near the window that said it. Heal never sees
them (it reads a NEW record's ~10 nearest) and the still-true check never sees
them (it reads the session's own records). Measured 2026-10-05: ~25% of
records stale on both sides of the rebuild's blind check.

So the line itself is the trigger. When a save writes history lines, the
retiring ones (kind 'dropped', or a line that names what it `replaced`) go
into this project's queue; a drain thread takes them oldest first. For one
line the box finds the records near the retired thing — by its names and by
meaning, last written on or before the line's day (exact search: the HNSW
filter trap) — and shows each with the few lines said since it was written
(SHOWING MORE: a side fact the record still carries can be seen to have died
too). The hub's retire_judge asks Jev which still describe the retired thing
as current, and one model call answers, of those, which records the line
made untrue (TEXTS["retire"]). The box deletes only those, only records it
sent, never a preference; every delete is copied to `retire_log` first —
the deleted text, the line that caused it and the reason — so a wrong
delete is seen and restored. Delete only (founder 2026-10-05: records hold
no history).

Measured before wiring (2026-10-06, GLM, 357 lines, blind-read): 87 of 139
known-stale records removed, ~87% of deletes right, ~$0.0020 per retirement
line ≈ $0.00075 a message.

THE BACKLOG: the first drain of a project queues its existing retirement
lines once, oldest first — a customer's memory is swept on its own, never by
a maintenance pass from us (founder 2026-10-06).

Limit, said plainly: a retirement made silently in code and never said in
conversation writes no line, and nothing here can catch it.
BRAIN_RETIRE=off stops the sweep (the queue keeps filling, nothing is judged).
"""
from __future__ import annotations

import datetime as _dt
import os
import threading as _threading
import traceback

from . import store

CANDIDATES = 50          # records sent per line
BY_NAME = 25             # of which found by name first
LATER = 8                # lines said after the line, shown to the judge
SINCE = 3                # lines said after a record was written, shown with it
NAME_MIN = 3
PER_DRAIN = 40           # lines one drain takes before it lets the next trigger in
BACKOFF_MINUTES = (2, 10, 60, 360)
MAX_ATTEMPTS = len(BACKOFF_MINUTES) + 1
STALE_MINUTES = 30


def on() -> bool:
    return os.environ.get("BRAIN_RETIRE", "on").strip().lower() not in ("off", "0", "no")


def backlog_on() -> bool:
    """BRAIN_RETIRE_BACKLOG=off: new lines are swept as they are written, the
    one-time pass over the lines a memory already has waits (2026-10-07: our
    own memory's backlog is ~6,000 lines, a third of a week's GLM limit — the
    founder's call when it runs)."""
    return os.environ.get("BRAIN_RETIRE_BACKLOG", "on").strip().lower() not in ("off", "0", "no")


def is_retirement(line: dict) -> bool:
    return str(line.get("kind") or "") == "dropped" or bool(str(line.get("replaced") or "").strip())


def names(line: dict) -> list[str]:
    out: list[str] = []
    for n in list(line.get("things") or []) + [line.get("replaced") or ""]:
        n = " ".join(str(n or "").split())
        if len(n) >= NAME_MIN and n.lower() not in {x.lower() for x in out}:
            out.append(n)
    return out[:10]


def _queries(line: dict) -> list[str]:
    """What the retired thing is called, for the search by meaning: the thing
    replaced when the line names one, and the line's own words."""
    q = [str(line.get("replaced") or "").strip(), str(line.get("what") or "").strip()]
    return [x[:2000] for x in q if x]


def _day(v) -> _dt.date:
    return v if isinstance(v, _dt.date) else _dt.date.fromisoformat(str(v)[:10])


def _before(line: dict) -> tuple[str, list]:
    """WHICH RECORDS A LINE MAY JUDGE: only those written before it (a record
    written after the line already knows what the line said). Found live
    2026-10-07: two records written later the SAME day as their line were
    deleted by it — the day is too coarse. A line written live carries its
    write time (`at`, within a day of its `day`): records written before that
    moment. A backlog line replayed later carries its original day but a
    replay-time `at`: records of earlier days only. A line with no `at` (the
    bench's frozen lines): records up to its day, as measured."""
    day = _day(line.get("day"))
    at = line.get("at")
    if at is None:
        return "updated_at::date <= %s", [day]
    if abs((at.date() - day).days) <= 1:
        return "updated_at < %s", [at]
    return "updated_at::date < %s", [day]


def candidates(cur, project: str, line: dict, k: int = CANDIDATES, by_name: int = BY_NAME) -> list[dict]:
    from . import embed
    from .decisionline import _vec
    when, args = _before(line)
    order: list[str] = []
    for n in names(line):
        cur.execute(f"SELECT record_id FROM document, plainto_tsquery('simple', %s) q "
                    f"WHERE project = %s AND tsv @@ q AND record_id <> 'memory_index' AND {when} "
                    f"ORDER BY ts_rank(tsv, q) DESC LIMIT %s;", [n, project, *args, 10])
        for (rid,) in cur.fetchall():
            if rid not in order and len(order) < by_name:
                order.append(rid)
    best: dict[str, float] = {}
    for v in embed.embed(_queries(line), kind="query") or []:
        if not v:
            continue
        # EXACT, NEVER THE INDEX (2026-10-05): with the HNSW index the planner
        # took ~40 nearest of the whole project and THEN the date filter, so a
        # line got 10 candidates on one restore and 50 on the next. The
        # project's records, filtered first, then ordered — as near_subjects.
        cur.execute(f"WITH s AS MATERIALIZED (SELECT record_id, embedding FROM document WHERE project = %s "
                    f"AND record_id <> 'memory_index' AND embedding IS NOT NULL AND {when}) "
                    f"SELECT record_id, embedding <=> %s::halfvec FROM s ORDER BY 2 LIMIT %s;",
                    [project, *args, _vec(v), k])
        for rid, dist in cur.fetchall():
            best[rid] = min(best.get(rid, 9.0), float(dist))
    for rid, _ in sorted(best.items(), key=lambda x: x[1]):
        if len(order) >= k:
            break
        if rid not in order:
            order.append(rid)
    if not order:
        return []
    cur.execute("SELECT record_id, coalesce(type,''), coalesce(title,''), coalesce(content,''), updated_at::date "
                "FROM document WHERE project = %s AND record_id = ANY(%s);", [project, order])
    rows = {r[0]: r for r in cur.fetchall()}
    return [{"id": i, "type": rows[i][1], "title": rows[i][2], "content": rows[i][3], "day": rows[i][4].isoformat()}
            for i in order if i in rows]


def since(cur, project: str, record: dict, until, k: int = SINCE) -> list[dict]:
    """SHOWING MORE (founder 2026-10-06: "if it see more, it can do more"):
    the lines said after a record was written, up to the line's day (what the
    sweep knows at that moment), nearest the record itself. Measured: with
    them the judge removed 82% of known-stale records instead of 42%; three
    lines held the quality of six at three quarters of the input."""
    cur.execute("WITH r AS (SELECT embedding AS e FROM document WHERE project = %s AND record_id = %s "
                "AND embedding IS NOT NULL), s AS MATERIALIZED (SELECT day, kind, what, coalesce(replaced,'') AS replaced, "
                "embedding FROM decision WHERE project = %s AND day > %s AND day <= %s AND embedding IS NOT NULL) "
                "SELECT s.day, s.kind, s.what, s.replaced FROM s, r ORDER BY s.embedding <=> r.e LIMIT %s;",
                [project, record["id"], project, record.get("day"), until, k])
    return [{"day": d.isoformat(), "kind": kd, "what": w, "replaced": r} for d, kd, w, r in cur.fetchall()]


def later(cur, project: str, line: dict, k: int = LATER) -> list[dict]:
    """The lines said after the line about the same thing — nearest by
    meaning (a thing seen in use again is not dead: the 2026-09-21 rule). A
    line swept the moment it is said has none; the backlog has them."""
    from . import embed
    from .decisionline import _vec
    vs = [v for v in (embed.embed(_queries(line)[:1], kind="query") or []) if v]
    if not vs:
        return []
    cur.execute("WITH s AS MATERIALIZED (SELECT day, kind, what, coalesce(replaced,'') AS replaced, embedding "
                "FROM decision WHERE project = %s AND day > %s AND id <> %s AND embedding IS NOT NULL) "
                "SELECT day, kind, what, replaced FROM s ORDER BY embedding <=> %s::halfvec LIMIT %s;",
                [project, line.get("day"), int(line.get("id") or 0), _vec(vs[0]), k])
    return [{"day": d.isoformat(), "kind": kd, "what": w, "replaced": r} for d, kd, w, r in cur.fetchall()]


def ask_body(cur, project: str, line: dict) -> dict:
    """The hub's retire_judge body for one line, or {} when nothing is near:
    the line, the records near it each with what was said since, and the
    lines said after the line."""
    recs = candidates(cur, project, line)
    if not recs:
        return {}
    day = str(line.get("day"))[:10]
    for r in recs:
        r["since"] = since(cur, project, r, day)
    return {"project": project, "today": day,
            "line": {"day": day, "kind": line.get("kind"), "what": line.get("what"),
                     "replaced": line.get("replaced") or "", "why": line.get("why") or ""},
            "records": recs, "later": later(cur, project, line)}


def apply(project: str, ops: list[dict], records: list[dict], dry: bool, log: list[str],
          line: dict | None = None) -> tuple[int, int]:
    """The hub's deletes onto the records it was sent, and nothing else. A
    record that moved since it was shown is not touched from a stale view; a
    preference is never deleted, whatever the hub said; each delete is copied
    to retire_log (with the line and the reason) before the row goes."""
    from . import tools
    from .heal import _current_body
    sent = {str(r["id"]): r for r in records}
    applied = dropped = 0
    seen: set[str] = set()
    for o in ops or []:
        oid, op = str((o or {}).get("id") or ""), str((o or {}).get("op") or "")
        r = sent.get(oid)
        if r is None or op != "delete" or oid in seen:
            log.append(f"DROPPED (not sent, not a delete, or a second op): {str(o)[:120]}")
            dropped += 1
            continue
        seen.add(oid)
        if r.get("type") == "preference":
            log.append(f"REFUSED delete of a preference: {oid}")
            dropped += 1
            continue
        if not dry and _current_body(project, oid) not in (None, r.get("content")):
            log.append(f"REFUSED delete from a stale view: {oid} changed since it was shown")
            dropped += 1
            continue
        log.append(f"{'DRY ' if dry else ''}DELETE {project}:{oid} — {str(o.get('reason') or '')[:110]}")
        if dry:
            applied += 1
            continue
        if line is not None:
            _log_delete(project, r, line, str(o.get("reason") or ""))
        res = tools.delete_memory(project, oid)
        if str(res).startswith(("Error", "No record")):
            log.append(f"  REFUSED: {str(res)[:120]}")
            dropped += 1
        else:
            applied += 1
    return applied, dropped


# ── the queue, the log, the drain ───────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS retire_queue (
    id          bigserial   PRIMARY KEY,
    project     text        NOT NULL,
    line_id     bigint      NOT NULL UNIQUE,
    line_day    date        NOT NULL,
    status      text        NOT NULL DEFAULT 'pending',
    attempts    integer     NOT NULL DEFAULT 0,
    last_error  text        NOT NULL DEFAULT '',
    enqueued_at timestamptz NOT NULL DEFAULT now(),
    next_try    timestamptz NOT NULL DEFAULT now(),
    started_at  timestamptz
);
CREATE INDEX IF NOT EXISTS retire_queue_next ON retire_queue (project, line_day, line_id) WHERE status = 'pending';
CREATE UNIQUE INDEX IF NOT EXISTS retire_queue_one_running ON retire_queue (project) WHERE status = 'running';
CREATE TABLE IF NOT EXISTS retire_log (
    id          bigserial   PRIMARY KEY,
    project     text        NOT NULL,
    record_id   text        NOT NULL,
    type        text        NOT NULL DEFAULT '',
    title       text        NOT NULL DEFAULT '',
    content     text        NOT NULL DEFAULT '',
    line_id     bigint,
    line_what   text        NOT NULL DEFAULT '',
    reason      text        NOT NULL DEFAULT '',
    deleted_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS retire_log_project ON retire_log (project, deleted_at);
CREATE TABLE IF NOT EXISTS retire_state (
    project     text        PRIMARY KEY,
    backlog_at  timestamptz NOT NULL DEFAULT now()
);
"""

_RETIRING = "(kind = 'dropped' OR coalesce(replaced,'') <> '')"


def ensure(cur) -> None:
    store.ensure_ddl(cur, "retire_queue", _DDL)


def enqueue_session(conn, project: str, session_id: str, lo: int) -> int:
    """The retiring lines a save just wrote for this stretch, into the queue
    (a line already queued is skipped)."""
    cur = conn.cursor()
    ensure(cur)
    cur.execute(f"INSERT INTO retire_queue (project, line_id, line_day) SELECT project, id, day FROM decision "
                f"WHERE project = %s AND session_id = %s AND turn_lo = %s AND {_RETIRING} "
                f"ON CONFLICT (line_id) DO NOTHING;", [project, session_id, lo])
    return cur.rowcount


def backlog(conn, project: str) -> int:
    """THE BACKLOG, once per project: every retiring line it already has,
    queued oldest first. Marked so it never runs twice."""
    if not backlog_on():
        return 0
    cur = conn.cursor()
    ensure(cur)
    cur.execute("INSERT INTO retire_state (project) VALUES (%s) ON CONFLICT (project) DO NOTHING;", [project])
    if not cur.rowcount:
        return 0
    cur.execute(f"INSERT INTO retire_queue (project, line_id, line_day) SELECT project, id, day FROM decision "
                f"WHERE project = %s AND {_RETIRING} ORDER BY day, id ON CONFLICT (line_id) DO NOTHING;", [project])
    return cur.rowcount


def _claim(conn, project: str):
    import psycopg
    cur = conn.cursor()
    cur.execute("UPDATE retire_queue SET status = 'pending' WHERE project = %s AND status = 'running' "
                "AND started_at < now() - make_interval(mins => %s);", [project, STALE_MINUTES])
    try:
        cur.execute("UPDATE retire_queue SET status = 'running', attempts = attempts + 1, started_at = now() "
                    "WHERE id = (SELECT id FROM retire_queue WHERE project = %s AND status = 'pending' "
                    "            AND next_try <= now() ORDER BY line_day, line_id LIMIT 1 FOR UPDATE SKIP LOCKED) "
                    "RETURNING id, line_id, attempts;", [project])
    except psycopg.errors.UniqueViolation:
        conn.rollback()
        return None
    row = cur.fetchone()
    conn.commit()
    return row


def _done(conn, qid: int) -> None:
    conn.cursor().execute("DELETE FROM retire_queue WHERE id = %s;", [qid])
    conn.commit()


def _fail(conn, qid: int, attempts: int, err: str, wait_s: int = 0) -> bool:
    """Back off, or drop the line for good (loudly) past the last attempt.
    A wait the hub named (the provider's window) does not spend an attempt."""
    cur = conn.cursor()
    if wait_s:
        cur.execute("UPDATE retire_queue SET status = 'pending', attempts = attempts - 1, last_error = %s, "
                    "next_try = now() + make_interval(secs => %s) WHERE id = %s;", [err[:500], wait_s, qid])
        conn.commit()
        return False
    if attempts >= MAX_ATTEMPTS:
        cur.execute("DELETE FROM retire_queue WHERE id = %s;", [qid])
        conn.commit()
        return True
    cur.execute("UPDATE retire_queue SET status = 'pending', last_error = %s, "
                "next_try = now() + make_interval(mins => %s) WHERE id = %s;",
                [err[:500], BACKOFF_MINUTES[attempts - 1], qid])
    conn.commit()
    return False


def _log_delete(project: str, rec: dict, line: dict, reason: str) -> None:
    with store.connection() as conn:
        cur = conn.cursor()
        ensure(cur)
        cur.execute("INSERT INTO retire_log (project, record_id, type, title, content, line_id, line_what, reason) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s);",
                    [project, rec["id"], rec.get("type") or "", rec.get("title") or "", rec.get("content") or "",
                     line.get("id"), str(line.get("what") or "")[:2000], reason[:1000]])
        conn.commit()


def _line(conn, line_id: int) -> dict | None:
    cur = conn.cursor()
    cur.execute("SELECT id, day, kind, what, coalesce(replaced,''), coalesce(why,''), coalesce(things,'{}'), at "
                "FROM decision WHERE id = %s;", [line_id])
    r = cur.fetchone()
    return None if not r else {"id": r[0], "day": r[1], "kind": r[2], "what": r[3], "replaced": r[4],
                               "why": r[5], "things": list(r[6]), "at": r[7]}


def drain(tenant: str, project: str, max_lines: int = PER_DRAIN) -> dict:
    """Work this project's queue oldest first, one line per hub ask. A hub
    that does not answer leaves the line queued (backoff); a refusal with a
    wait (the provider's window, the allowance) stops this drain."""
    from . import judge
    out = {"lines": 0, "deleted": 0, "refused": 0, "failed": 0, "lost": 0, "queued": 0}
    tok = store.bind(tenant)
    try:
        with store.connection() as conn:
            ensure(conn.cursor())
            out["queued"] = backlog(conn, project)
            conn.commit()
        for _ in range(max_lines):
            with store.connection() as conn:
                claimed = _claim(conn, project)
            if not claimed:
                break
            qid, line_id, attempts = claimed
            try:
                with store.connection() as conn:
                    line = _line(conn, line_id)
                    body = ask_body(conn.cursor(), project, line) if line else {}
                    conn.rollback()
                if not body:
                    with store.connection() as conn:
                        _done(conn, qid)
                    out["lines"] += 1
                    continue
                try:
                    ans = judge.ask("retire_judge", body)
                except judge.NoAnswer as e:
                    with store.connection() as conn:
                        _fail(conn, qid, attempts, e.why, e.retry_s or (600 if "allowance" in e.why else 0))
                    out["failed"] += 1
                    if e.retry_s or "allowance" in e.why or e.off:
                        break                    # the hub said wait: this drain stops
                    continue
                a = ans.get("answer") or {}
                log: list[str] = []
                applied, dropped = apply(project, a.get("ops") or [], body["records"], False, log, line=line)
                for x in log:
                    print(f"[retire] {project}: {x}", flush=True)
                with store.connection() as conn:
                    _done(conn, qid)
                out["lines"] += 1
                out["deleted"] += applied
                out["refused"] += dropped
            except Exception as e:                           # noqa: BLE001
                traceback.print_exc()
                out["failed"] += 1
                with store.connection() as conn:
                    if _fail(conn, qid, attempts, f"{type(e).__name__}: {e}"):
                        out["lost"] += 1
                        print(f"[retire] {project}: line {line_id} DROPPED after {MAX_ATTEMPTS} attempts — "
                              f"never swept: {type(e).__name__}: {e}", flush=True)
        with store.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM retire_queue WHERE project = %s;", [project])
            out["depth"] = int(cur.fetchone()[0])
    finally:
        store.unbind(tok)
    return out


def pending_projects(conn) -> list[str]:
    cur = conn.cursor()
    ensure(cur)
    cur.execute("SELECT DISTINCT project FROM retire_queue WHERE status = 'pending' AND next_try <= now();")
    return [r[0] for r in cur.fetchall()]


_running: set[str] = set()
_lock = _threading.Lock()


def trigger(tenant: str, project: str) -> str:
    """Start the sweep for a project in a daemon thread (one per project at a
    time): from every save that wrote lines, and once per project for its
    backlog. A skipped trigger is free — the queue keeps the lines."""
    if not on():
        return "retire: off"
    key = f"{tenant}/{project}"
    with _lock:
        if key in _running:
            return "retire: already running"
        _running.add(key)

    def _run() -> None:
        try:
            while True:
                r = drain(tenant, project)
                if r.get("lines") or r.get("failed") or r.get("queued"):
                    print(f"[retire] {key}: {r['lines']} lines, {r['deleted']} records deleted, "
                          f"{r['refused']} refused, {r['failed']} failed, {r['lost']} lost, "
                          f"backlog queued {r['queued']}, queue {r.get('depth', 0)}", flush=True)
                # a full drain with work left goes on; a stop (the hub said
                # wait, or nothing due) ends the thread — the next save wakes it
                if r.get("lines", 0) < PER_DRAIN or r.get("failed"):
                    break
        except Exception as e:                               # noqa: BLE001
            traceback.print_exc()
            print(f"[retire] {key} FAILED: {type(e).__name__}: {e}", flush=True)
        finally:
            with _lock:
                _running.discard(key)

    _threading.Thread(target=_run, name=f"retire-{project}", daemon=True).start()
    return "retire: started"


def _needs(conn) -> list[str]:
    """Projects with retiring lines never backlogged, or with due rows."""
    cur = conn.cursor()
    ensure(cur)
    if not backlog_on():
        cur.execute("SELECT DISTINCT project FROM retire_queue WHERE status = 'pending' AND next_try <= now();")
        return sorted(r[0] for r in cur.fetchall())
    # A BRAND-NEW MEMORY HAS NO DECISION TABLE until its first line is written
    # (store.ensure_ddl). The sweep met that two minutes after the first start
    # of every fresh install and printed a traceback (found on the local-first
    # install test, 2026-11-17). Only the queue can hold work then.
    cur.execute("SELECT to_regclass('decision') IS NOT NULL;")
    if not cur.fetchone()[0]:
        cur.execute("SELECT DISTINCT project FROM retire_queue WHERE status = 'pending' AND next_try <= now();")
        return sorted(r[0] for r in cur.fetchall())
    cur.execute(f"SELECT DISTINCT project FROM decision WHERE {_RETIRING} "
                f"AND project NOT IN (SELECT project FROM retire_state) "
                f"UNION SELECT DISTINCT project FROM retire_queue WHERE status = 'pending' AND next_try <= now();")
    return sorted(r[0] for r in cur.fetchall())


def _sweep_tenant(tenant: str) -> None:
    """One project at a time — a backlog never fans out across the hub."""
    tok = store.bind(tenant)
    try:
        with store.connection() as conn:
            projects = _needs(conn)
            conn.commit()
    finally:
        store.unbind(tok)
    for project in projects:
        key = f"{tenant}/{project}"
        with _lock:
            if key in _running:
                continue
            _running.add(key)
        try:
            while True:
                r = drain(tenant, project)
                if r.get("lines") or r.get("failed") or r.get("queued"):
                    print(f"[retire] {key}: {r['lines']} lines, {r['deleted']} records deleted, "
                          f"{r['refused']} refused, {r['failed']} failed, {r['lost']} lost, "
                          f"backlog queued {r['queued']}, queue {r.get('depth', 0)}", flush=True)
                if r.get("lines", 0) < PER_DRAIN or r.get("failed"):
                    break
        except Exception as e:                               # noqa: BLE001
            traceback.print_exc()
            print(f"[retire] {key} FAILED: {type(e).__name__}: {e}", flush=True)
        finally:
            with _lock:
                _running.discard(key)


START_DELAY_S = int(os.environ.get("BRAIN_RETIRE_START_S", "120"))
PIN_S = int(os.environ.get("BRAIN_RETIRE_PIN_S", "1800"))


def start() -> None:
    """From the API's startup hook: after the pipe is up, every tenant's
    backlog and due lines, then the same every PIN_S (a line whose backoff
    ran out, a project no save has woken)."""
    if not on():
        print("[retire] off (BRAIN_RETIRE=off) — lines queue, nothing is swept", flush=True)
        return
    if not backlog_on():
        print("[retire] backlog held (BRAIN_RETIRE_BACKLOG=off) — new lines are swept, existing ones wait", flush=True)

    def _loop() -> None:
        import time
        time.sleep(START_DELAY_S)
        while True:
            try:
                for t in store.provisioned_tenants():
                    _sweep_tenant(t)
            except Exception:                                # noqa: BLE001
                traceback.print_exc()
            time.sleep(PIN_S)

    _threading.Thread(target=_loop, name="retire-sweeper", daemon=True).start()
    print(f"[retire] started — backlog after {START_DELAY_S}s, then every {PIN_S}s", flush=True)
