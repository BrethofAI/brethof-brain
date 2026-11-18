"""THE RECORDS REPLAY (founder 2026-09-26: "if we curate everything, starting
from the oldest one … we will just do the whole job from the beginning, like
it should be"; "the only thing which we need is a date").

The live curation job run again over the archive, into an EMPTY scratch
tenant: a project's sessions in the order they began, each in the windows
the live switch sends (backfill.windows), each window through production's
records call (curate_window, records_only — the project's brief, the
session's records so far, the next message) and saved through the same
writers the live save uses (curate_apply.apply_records, ledger.add); then
heal exactly as live — the save's records queue, and the project's heal
drains in fours (reconcile.drain). Every judgement carries the day the
window was SAID as TODAY (judge.TODAY), so a July window is judged in July.

WHY THIS IS NOT BACK-CURATION: the no-back-curation law (2026-08-25) stops a
late record landing in a memory that already holds newer truth. Here the
memory starts empty and is written in the order things were said — at each
window the curator sees only what came before it, as it did live. The
result is compared with the live records; nothing reaches the live memory
unless the founder swaps it in.

One thread per project (a project's order matters: heal supersedes across
the whole project), resumable (`replay_state`), a skip log (`replay_skip`),
windows said on or after `until` left out (the live curator has them)."""
from __future__ import annotations

import datetime as _dt
import os
import threading
import time
import traceback

from . import store

STATE_DDL = """
CREATE TABLE IF NOT EXISTS replay_state (
    project     text NOT NULL,
    session_id  text NOT NULL,
    done_hi     integer NOT NULL DEFAULT -1,
    windows     integer NOT NULL DEFAULT 0,
    records     integer NOT NULL DEFAULT 0,
    skipped     integer NOT NULL DEFAULT 0,
    finished    boolean NOT NULL DEFAULT false,
    at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project, session_id)
);
CREATE TABLE IF NOT EXISTS replay_skip (
    id          bigserial PRIMARY KEY,
    project     text NOT NULL,
    session_id  text NOT NULL,
    turn_lo     integer NOT NULL,
    turn_hi     integer NOT NULL,
    why         text NOT NULL,
    at          timestamptz NOT NULL DEFAULT now()
);
"""

TRIES = 3
HALT_AFTER = 5       # rounds of TRIES a window may fail before its project halts there
ASK_S = 300
NO_ANSWER_WAIT_S = 20
CLOSED_WAIT_MAX_S = 6 * 3600
# AT MOST THIS MANY WINDOWS ASK THE MODEL AT ONCE (founder 2026-10-05: the
# 27B runs eight in parallel at its long context). One thread per project
# keeps each project in order; the gate only caps how many of them are inside
# a model call at the same moment, so 40-odd projects never queue 40 requests.
PARALLEL = int(os.environ.get("REPLAY_PARALLEL", "8"))
_GATE = threading.BoundedSemaphore(max(1, PARALLEL))
RUNS: dict[str, dict] = {}
STOP = threading.Event()
_LOCK = threading.Lock()


def _cut(until: str) -> str:
    """`until` as the windows' day strings are written ('YYYY-MM-DD HH:MM', UTC)."""
    if not until:
        return ""
    t = _dt.datetime.fromisoformat(until)
    t = t if t.tzinfo else t.replace(tzinfo=_dt.timezone.utc)
    return t.astimezone(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M")


def _prepare(source: str, dst: str) -> None:
    """The scratch tenant's tables and project list (a record routed to
    another project needs that project to exist, as live)."""
    from . import backfill
    with backfill._src_connection(source) as src:
        scur = src.cursor()
        scur.execute("SELECT name FROM project;")
        names = [r[0] for r in scur.fetchall()]
    with store.connection() as conn:
        cur = conn.cursor()
        cur.execute(STATE_DDL)
        # the history-lines table the window's view reads, made ONCE here under
        # the lock — four project threads making it at the same moment on a
        # fresh database collide in Postgres's catalog (2026-10-05: every
        # project of the first tightening run STOPPED on UniqueViolation)
        from . import decisionline
        cur.execute(decisionline.DDL)
        # A REBUILD IS KEPT (2026-10-05): the marker bench.drop and every
        # cleanup reads before dropping a database — the 09-29/30 rebuild was
        # swept away with 357 disposable benches on 10-03 and was unrecoverable.
        cur.execute("CREATE TABLE IF NOT EXISTS keep_reason (reason text NOT NULL, at timestamptz NOT NULL DEFAULT now());")
        cur.execute("INSERT INTO keep_reason (reason) SELECT %s WHERE NOT EXISTS (SELECT 1 FROM keep_reason);",
                    [f"records rebuild into {dst} — keep until it is swapped into the live memory or the founder drops it"])
        for n in names:
            cur.execute("INSERT INTO project (name) VALUES (%s) ON CONFLICT DO NOTHING;", [n])
        conn.commit()


def _bound() -> str:
    """The rebuild database this thread writes to ("" when none is bound)."""
    try:
        return store.current_tenant()
    except store.NoTenantBound:
        return ""


def _lists(cur, project: str, session_id: str, piece: str) -> tuple[list[dict], list[dict]]:
    """The curator's list (the 20 records of this session nearest the window)
    and the still-true check's list (the session's records with id and type),
    built by production's own code (2026-10-05: the replay had sent the newest
    200 without ids, so Jev's still-true check could name nothing to apply)."""
    from . import decisionline, hubwindow
    # no DDL here: _prepare made the table once under the lock — its ALTERs
    # take exclusive locks, and project threads running it per window
    # deadlocked each other (2026-10-05)
    vecs: list = []
    decisionline.view_near(cur, project, piece, vecs_out=vecs)
    return hubwindow.records_lists(cur, project, session_id, vecs)


def _ask(project: str, w: dict, so_far: list[dict], tries: int, retry0: int = 0,
         session: list[dict] | None = None, session_id: str = "", opts: dict | None = None) -> tuple[dict | None, str]:
    """Production's records call on one window; (answer, why-it-failed). A
    closed provider window is a wait, never a try (as the backfill). Every
    ask after the first is a new draw (`retry`, hub/judge.RETRY_TEMPERATURE):
    the same greedy prompt only repeats the answer that broke."""
    from . import judge
    why, waited, attempt = "", 0.0, 0
    while attempt < max(1, tries):
        if STOP.is_set():
            return None, "stopped"
        try:
            # records only, and the records text without its ledger paragraph:
            # the rebuild writes no ledger (founder 2026-10-05)
            with _GATE:
                ans = judge.ask("curate_window", {"project": project, "piece": w["piece"], "n": w["n"],
                                                  "records_so_far": so_far, "next": w["next"],
                                                  "records_session": [{**r, "content": str(r.get("content") or "")[:400]}
                                                                      for r in (session or [])],
                                                  "session_id": session_id,
                                                  "replay_tenant": _bound(),
                                                  "records_only": True, "records_prompt": (opts or {}).get("records_prompt") or "noledger",
                                                  **({"twin": True} if (opts or {}).get("twin") else {}),
                                                  "retry": retry0 + attempt}, timeout=ASK_S)
        except judge.NoAnswer as e:
            why = f"no answer — {e.why}"
            if e.retry_s and waited < CLOSED_WAIT_MAX_S:
                wait = min(int(e.retry_s), 600)
                time.sleep(wait)
                waited += wait
                continue
            attempt += 1
            time.sleep(NO_ANSWER_WAIT_S)
            continue
        attempt += 1
        got = ans.get("answer")
        failed = [f for f in (got or {}).get("failed") or [] if str(f).startswith("records")] if isinstance(got, dict) else []
        if isinstance(got, dict) and not failed:
            return got, ""
        why = failed[0][:200] if failed else f"unparseable — {str(ans.get('fault'))[:120]}"
    return None, why


def _wall(cur) -> _dt.datetime:
    cur.execute("SELECT clock_timestamp();")
    return cur.fetchone()[0]


def _stamp(projects: set[str], since: _dt.datetime, when: str) -> None:
    """Every record the window wrote or heal rewrote carries the time the
    window was SAID, not the replay's clock — the brief orders by it and heal
    reads each candidate's date ("upd …")."""
    if not when or not projects:
        return
    with store.connection() as conn:
        cur = conn.cursor()
        ts = _dt.datetime.fromisoformat(when).replace(tzinfo=_dt.timezone.utc)
        cur.execute("UPDATE document SET updated_at = %s WHERE updated_at >= %s AND project = ANY(%s);",
                    [ts, since, sorted(projects)])
        # captured_at is a DATE: a record still dated the replay's own day
        # has not been stamped yet
        cur.execute("UPDATE document SET captured_at = %s WHERE captured_at >= %s AND project = ANY(%s);",
                    [ts.date(), since.date(), sorted(projects)])
        conn.commit()


def _save(project: str, session_id: str, lo: int, hi: int, got: dict, log,
          today: str = "") -> tuple[int, set[str]]:
    """The live save's records, through the live writers. NO LEDGER ROWS: the
    rebuild writes none (founder 2026-10-05), and the text it asks has no
    ledger paragraph — any row a model sends anyway is dropped here.
    Returns (records landed, projects they landed in)."""
    from . import curate_apply, ledger
    recs = [r for r in (got.get("records") or []) if isinstance(r, dict)]
    lines: list[str] = []
    n = 0
    touched: set[str] = set()
    if recs:
        with store.connection() as conn:
            n, _ = curate_apply.apply_records(conn, project, recs, lines, today=today,
                                              origin={"session_id": session_id, "turn_lo": lo, "turn_hi": hi})
            conn.commit()
        touched = {str(r["_id"]).partition(":")[0] for r in recs if r.get("_id")}
    # the still-true check's ops on this session's records — the live save's
    # own function, so the rebuild deletes and rewrites exactly as live does
    over = [o for o in (got.get("overtaken") or []) if isinstance(o, dict)]
    if over:
        from .pipecommands import apply_overtaken
        k = apply_overtaken(project, session_id, over, lines)
        if k:
            log(f"[replay] {project} {session_id[:8]}#{lo}-{hi}: {k} overtaken record(s) applied")
    for a in []:                                  # the rebuild writes no ledger
        content = " ".join(str((a or {}).get("content") or "").split()) if isinstance(a, dict) else ""
        if content:
            try:
                ledger.add(str(a.get("project") or "").strip() or project, content[:ledger.ROW_CHARS],
                           origin=session_id, turn_lo=lo, turn_hi=hi)
            except Exception as e:                           # noqa: BLE001
                lines.append(f"LEDGER row refused: {e}")
    for m in lines:
        if "FAILED" in m or "REJECTED" in m or "refused" in m:
            log(f"[replay] {project}: {m}")
    return n, touched


def _heal(dst: str, projects: set[str], log, force: bool = False) -> None:
    """Heal as live: each project's queue drains in fours (the pool), in this
    thread so the window's day rides every heal ask."""
    from . import reconcile
    for p in sorted(projects):
        try:
            r = reconcile.drain(dst, p, force=force)
            if r.get("failed") or r.get("dropped_forever"):
                log(f"[replay] heal {p}: {r}")
        except Exception as e:                               # noqa: BLE001
            log(f"[replay] heal {p} FAILED: {type(e).__name__}: {e}")


def run(project: str, dst: str, until: str = "", source: str = "memory", tries: int = TRIES,
        limit_windows: int = 0, log=print, since: str = "", opts: dict | None = None) -> dict:
    """One project's archive through the live records job into `dst`. Resumes.
    With `since` (a day), only what was said on or after it — the April–July
    archive gives the graph its lines and no records (founder 2026-09-29), so
    the rebuild's records start the day the live hooks began (2026-06-30)."""
    from . import backfill, judge
    tot = {"project": project, "sessions": 0, "windows": 0, "records": 0, "skipped": 0, "done": False}
    with _LOCK:
        if RUNS.get(f"{dst}/{project}", {}).get("running"):
            return {"project": project, "why": "already running"}
        RUNS[f"{dst}/{project}"] = dict(tot, running=True)
    tok = store.bind(dst)
    cut = _cut(until)
    touched_all: set[str] = {project}
    last_day = last_when = ""
    try:
        with _LOCK:
            _prepare(source, dst)
        with backfill._src_connection(source) as src:
            scur = src.cursor()
            parts: list[tuple[str, list[dict]]] = []
            cut_off: set[str] = set()
            span = ((opts or {}).get("spans") or {}).get(project)
            for sid, _t0 in backfill.sessions(scur, project, since=since, until=until):
                if span and sid != span[0]:
                    continue
                with store.connection() as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT done_hi, finished FROM replay_state WHERE project = %s AND session_id = %s;",
                                [project, sid])
                    st = cur.fetchone()
                done_hi, finished = (st[0], st[1]) if st else (-1, False)
                if finished:
                    continue
                # (2026-09-29: the 09-27 "curated once" guard is gone — windows()
                # reads only the turns this project owns and ownership splits a
                # session with no overlap; the guard dropped every owner's part
                # but the first)
                wins = backfill.windows(scur, sid, since=since, project=project)
                if span:
                    wins = [w for w in wins if w["lo"] >= int(span[1])][:int(span[2])]
                n_all = len(wins)
                if cut:
                    wins = [w for w in wins if all(str(d)[:16] < cut for d in w["days"] if d)]
                if len(wins) < n_all:
                    # A SESSION THE CUT CROSSES IS NEVER FINISHED HERE (2026-10-05:
                    # the 27B's part to 08-01 marked a July→August session finished
                    # with 46 August windows unasked, and GLM's part would have
                    # skipped them all); its windows past the cut wait for the run
                    # that has no cut, which resumes it by done_hi
                    cut_off.add(sid)
                tot["sessions"] += 1
                wins = [w for w in wins if w["lo"] > done_hi]
                parts.append((sid, wins))
            # THE ORDER THINGS WERE SAID (founder 2026-09-30, "do the rebuild in
            # time order"): every session's windows merged, earliest first — a
            # session that ran for weeks no longer goes in whole before the
            # sessions that ran beside it (brain: 35% of turns went in after
            # newer ones, up to 41 days). Each session keeps its own order, so
            # replay_state still resumes it by done_hi.
            left = {sid: len(wins) for sid, wins in parts}
            for sid in [s for s, n in left.items() if n == 0 and s not in cut_off]:
                with store.connection() as conn:
                    conn.cursor().execute("INSERT INTO replay_state (project, session_id, finished) VALUES (%s, %s, true) "
                                          "ON CONFLICT (project, session_id) DO UPDATE SET finished = true, at = now();",
                                          [project, sid])
                    conn.commit()
            for sid, w in backfill.in_time_order(parts):
                if STOP.is_set():
                    log(f"[replay] {project}: stopped on request after window {tot['windows']}")
                    return tot
                if limit_windows and tot["windows"] >= limit_windows:
                    log(f"[replay] {project}: stopped at the sample of {limit_windows} windows")
                    return tot
                day = backfill._before(w)
                last_day = day.isoformat() if day else last_day
                last_when = next((str(d)[:16] for d in reversed(w["days"]) if d), last_when)
                ctx = judge.TODAY.set(day.isoformat() if day else "")
                try:
                    with store.connection() as conn:
                        so_far, session = _lists(conn.cursor(), project, sid, w["piece"])
                        conn.commit()
                    # RECORDS GO IN ORDER OR NOT AT ALL (founder 2026-09-26: "if you
                    # mix up the order, the old record can overwrite the new one"):
                    # a failing window is asked again in place, never skipped and
                    # never redone later; if it keeps failing the project HALTS
                    # here and the next run starts at this same window
                    got, why = _ask(project, w, so_far, tries, session=session, session_id=sid, opts=opts)
                    rounds = 1
                    while got is None and why != "stopped" and rounds < HALT_AFTER:
                        log(f"[replay] {project} {sid[:8]}#{w['lo']}-{w['hi']}: round {rounds} failed — {why}; "
                            f"asking again in place")
                        time.sleep(60)
                        got, why = _ask(project, w, so_far, tries, retry0=rounds * max(1, tries), session=session, session_id=sid, opts=opts)
                        rounds += 1
                    if got is None and why != "stopped":
                        with store.connection() as conn:
                            conn.cursor().execute(
                                "INSERT INTO replay_skip (project, session_id, turn_lo, turn_hi, why) "
                                "VALUES (%s, %s, %s, %s, %s);",
                                [project, sid, w["lo"], w["hi"], f"HALTED — {why}"[:400]])
                            conn.commit()
                        log(f"[replay] {project}: HALTED at {sid[:8]}#{w['lo']}-{w['hi']} after {rounds} rounds — "
                            f"{why}; nothing after it is written, the next run starts here")
                        tot["halted"] = f"{sid[:8]}#{w['lo']}"
                        return tot
                    n = 0
                    if got is not None:
                        when = next((str(d)[:16] for d in reversed(w["days"]) if d), "")
                        with store.connection() as conn:
                            wall = _wall(conn.cursor())
                            conn.commit()
                        n, touched = _save(project, sid, w["lo"], w["hi"], got, log,
                                           today=day.isoformat() if day else "")
                        touched_all |= touched
                        _stamp(touched, wall, when)
                        if touched:
                            _heal(dst, touched, log)
                            _stamp(touched, wall, when)
                finally:
                    judge.TODAY.reset(ctx)
                if got is None and why == "stopped":
                    # a stop is not a skip: the window stays unwritten and
                    # the next run asks it (2026-09-26: a stop for a rebuild
                    # logged one as skipped and marked it done)
                    log(f"[replay] {project}: stopped on request before window {tot['windows'] + 1}")
                    return tot
                with store.connection() as conn:
                    cur = conn.cursor()
                    if got is None:
                        cur.execute("INSERT INTO replay_skip (project, session_id, turn_lo, turn_hi, why) "
                                    "VALUES (%s, %s, %s, %s, %s);", [project, sid, w["lo"], w["hi"], why[:400]])
                        tot["skipped"] += 1
                        log(f"[replay] {project} {sid[:8]}#{w['lo']}-{w['hi']}: SKIPPED after {tries} — {why}")
                    tot["windows"] += 1
                    tot["records"] += n
                    cur.execute("INSERT INTO replay_state (project, session_id, done_hi, windows, records, skipped, at) "
                                "VALUES (%s, %s, %s, 1, %s, %s, now()) ON CONFLICT (project, session_id) DO UPDATE SET "
                                "done_hi = EXCLUDED.done_hi, windows = replay_state.windows + 1, "
                                "records = replay_state.records + EXCLUDED.records, "
                                "skipped = replay_state.skipped + EXCLUDED.skipped, at = now();",
                                [project, sid, w["hi"], n, 1 if got is None else 0])
                    conn.commit()
                RUNS[f"{dst}/{project}"] = dict(tot, running=True)
                log(f"[replay] {project} {sid[:8]}#{w['lo']}-{w['hi']} ({day}): {n} records "
                    f"(window {tot['windows']}, {tot['records']} so far)")
                left[sid] -= 1
                if left[sid] == 0 and sid not in cut_off:
                    with store.connection() as conn:
                        conn.cursor().execute("INSERT INTO replay_state (project, session_id, finished) VALUES (%s, %s, true) "
                                              "ON CONFLICT (project, session_id) DO UPDATE SET finished = true, at = now();",
                                              [project, sid])
                        conn.commit()
        # the pool's last records: heal what is left, as a compact would, on
        # the day of the project's last window
        ctx = judge.TODAY.set(last_day)
        try:
            with store.connection() as conn:
                wall = _wall(conn.cursor())
                conn.commit()
            _heal(dst, touched_all, log, force=True)
            _stamp(touched_all, wall, last_when)
        finally:
            judge.TODAY.reset(ctx)
        tot["done"] = True
        log(f"[replay] {project}: done — {tot}")
        return tot
    except Exception as e:                                       # noqa: BLE001
        traceback.print_exc()
        tot["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        log(f"[replay] {project}: STOPPED — {tot['error']}")
        # THE STOP IS WRITTEN WHERE IT CAN ALWAYS BE READ (2026-09-30: ms's run
        # stopped at 22:59 and journald had dropped the line — the reason was
        # lost and nothing noticed for five hours). The progress table says
        # where; this row says why.
        try:
            with store.connection() as conn:
                conn.cursor().execute(
                    "INSERT INTO replay_skip (project, session_id, turn_lo, turn_hi, why) VALUES (%s, %s, %s, %s, %s);",
                    [project, str(locals().get("sid") or ""), -1, -1, f"STOPPED — {tot['error']}"[:400]])
                conn.commit()
        except Exception as e2:                              # noqa: BLE001
            log(f"[replay] {project}: the stop could not be recorded — {type(e2).__name__}: {e2}")
        return tot
    finally:
        with _LOCK:
            RUNS[f"{dst}/{project}"] = dict(tot, running=False)
        store.unbind(tok)


def start(projects: list[str], dst: str, until: str = "", source: str = "memory", tries: int = TRIES,
          limit_windows: int = 0, since: str = "", opts: dict | None = None) -> str:
    """One detached thread per project; the container's log is the progress."""
    STOP.clear()
    started = []
    for p in projects:
        if RUNS.get(f"{dst}/{p}", {}).get("running"):
            continue
        threading.Thread(target=run, args=(p, dst, until, source, tries, limit_windows),
                         kwargs={"log": lambda m: print(m, flush=True), "since": since, "opts": opts},
                         name=f"replay-{p}", daemon=True).start()
        started.append(p)
    return f"records replay started for {started} → {dst} (since {since or 'the start'}, until {until or 'now'})"


def stop() -> str:
    STOP.set()
    return f"stop requested for {[p for p, r in RUNS.items() if r.get('running')]}"


def status(dst: str) -> dict:
    tok = store.bind(dst)
    try:
        with store.connection() as conn:
            cur = conn.cursor()
            cur.execute(STATE_DDL)
            cur.execute("SELECT project, count(*), count(*) FILTER (WHERE finished), sum(windows), sum(records), "
                        "sum(skipped) FROM replay_state GROUP BY project ORDER BY project;")
            rows = [{"project": p, "sessions": n, "finished": f, "windows": w, "records_written": r, "skipped": s}
                    for p, n, f, w, r, s in cur.fetchall()]
            cur.execute("SELECT project, count(*) FROM document WHERE record_id <> 'memory_index' GROUP BY project;")
            left = dict(cur.fetchall())
            for r in rows:
                r["records_now"] = int(left.get(r["project"], 0))
            cur.execute("SELECT project, session_id, turn_lo, turn_hi, why FROM replay_skip ORDER BY id DESC LIMIT 20;")
            skips = [{"project": p, "session": s[:8], "lo": lo, "hi": hi, "why": w} for p, s, lo, hi, w in cur.fetchall()]
        return {"running": {p: r for p, r in RUNS.items() if r.get("running")},
                # a run that ended without finishing — halted or stopped — is named,
                # never folded into "not running" (2026-09-30)
                "ended_unfinished": {p: {k: r.get(k) for k in ("halted", "error", "windows")}
                                     for p, r in RUNS.items()
                                     if not r.get("running") and (r.get("halted") or r.get("error"))},
                "state": rows, "last_skips": skips}
    finally:
        store.unbind(tok)
