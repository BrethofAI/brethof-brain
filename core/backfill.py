"""THE GRAPH BACKFILL (founder 2026-09-25: on the local 27B, our own archive
on our own box; records are NEVER backfilled — only the graph, whose lines
are history, not current truth).

One project at a time, its sessions in the order they happened, each
session's exchanges in windows of five exactly as the live switch sends
them (hubfeed._pieces, EXCHANGE headers with dates, the threads view from
the graph as it fills, the next prompt as THE NEXT MESSAGE); the lines call
alone (`TEXTS['batch_lines']`, V6) asked up the pipe as `curate_probe`
prompt "plines"; each window's lines filed under their own exchanges.

DETACHED AND RESUMABLE: `backfill_state` keeps, per session, the highest
turn already written; a second run continues where the first stopped. A
window whose answer fails `tries` times is written to `backfill_skip` with
its reason and the run goes on — the gaps are known, never silent. The
destination is a tenant name: a bench variant for the proven sample first,
then the memory itself."""
from __future__ import annotations

import datetime as _dt
import threading
import time
import traceback

from . import store

STATE_DDL = """
CREATE TABLE IF NOT EXISTS backfill_state (
    project     text NOT NULL,
    session_id  text NOT NULL,
    done_hi     integer NOT NULL DEFAULT -1,
    windows     integer NOT NULL DEFAULT 0,
    lines       integer NOT NULL DEFAULT 0,
    skipped     integer NOT NULL DEFAULT 0,
    finished    boolean NOT NULL DEFAULT false,
    at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project, session_id)
);
CREATE TABLE IF NOT EXISTS backfill_skip (
    id          bigserial PRIMARY KEY,
    project     text NOT NULL,
    session_id  text NOT NULL,
    turn_lo     integer NOT NULL,
    turn_hi     integer NOT NULL,
    why         text NOT NULL,
    at          timestamptz NOT NULL DEFAULT now()
);
"""

WINDOW = 5
WINDOW_CHARS = 240000
TRIES = 3
ASK_S = 600          # one window's answer; the pipe's default of 900 s × 3 tries hid a lost ask for 45 min.
                     # 600, not 300 (2026-09-30): a 27B runaway fills the 20,000-token cap in ~370 s, and
                     # at 300 s it read as NO answer — an outage, which halts — instead of a broken one
NO_ANSWER_WAIT_S = 20  # between tries when the hub gave no answer and no wait
RUNS: dict[str, dict] = {}          # project → the live run's counters (one per project at a time)
_LOCK = threading.Lock()


def _src_connection(source: str):
    """The archive read: a plain connection to the source tenant's database
    (the memory's own, read only here)."""
    import psycopg
    from .bench import _dsn
    # autocommit: a read-only source must not sit 'idle in transaction' for
    # the whole run (2026-09-26: eight source reads held open transactions on
    # chat for minutes — every ALTER on that table would queue behind them)
    return psycopg.connect(_dsn(store.tenant_db(source)), autocommit=True)


def owned(scur) -> dict:
    """The source's session moves, resolved (core.ownership): the stretches
    move_session gave to another project than the archive's own column
    (2026-09-29: global's sessions are split among their real projects
    before the backfill, and every runner must follow the split). Read on
    the runner's own source connection; {} when nothing was ever moved."""
    from . import ownership
    scur.execute("SELECT to_regclass('session_owner') IS NOT NULL;")
    if not scur.fetchone()[0]:
        return {}
    scur.execute("SELECT session_id, turn_lo, turn_hi, project FROM session_owner ORDER BY id;")
    return ownership.resolve([(r[0], int(r[1]), int(r[2]), r[3]) for r in scur.fetchall()])


def _mine(scur, project: str) -> tuple[str, list]:
    from . import ownership
    return ownership.where_from(owned(scur), project)


def sessions(scur, project: str, since: str = "", span: tuple[str, str] | None = None,
             until: str = "") -> list[tuple[str, _dt.datetime]]:
    """A project's sessions, in the order they began; with `since`, only
    those that were still going on or after that day; with `span` (from,
    to), only those that BEGAN in [from, to) — one chunk of a project run
    side by side with its other chunks (founder 2026-09-26: "we could run at
    least eight")."""
    # what belongs to the project TODAY: its archived rows less the stretches
    # moved away, plus the stretches moved to it (core.ownership)
    # (the April–July archive is in today's format since 2026-09-29 —
    # scripts/ops/v1_normalize.py — so every session is read the same way)
    w, wp = _mine(scur, project)
    if span:
        a, b = span
        scur.execute(f"SELECT session_id, min(ts) FROM chat WHERE {w} "
                     "GROUP BY session_id HAVING min(ts) >= %s AND min(ts) < %s ORDER BY min(ts), session_id;",
                     wp + [a or "1970-01-01", b or "9999-01-01"])
    elif since:
        scur.execute(f"SELECT session_id, min(ts) FROM chat WHERE {w} "
                     "GROUP BY session_id HAVING max(ts) >= %s ORDER BY min(ts), session_id;", wp + [since])
    else:
        scur.execute(f"SELECT session_id, min(ts) FROM chat WHERE {w} "
                     "GROUP BY session_id ORDER BY min(ts), session_id;", wp)
    rows = [(s, t) for s, t in scur.fetchall()]
    if until:
        cut = _dt.datetime.fromisoformat(until)
        cut = cut if cut.tzinfo else cut.replace(tzinfo=_dt.timezone.utc)
        rows = [(s, t) for s, t in rows if t is None or t < cut]
    return rows


def live_from(cur, project: str, session_id: str, done_hi: int) -> int | None:
    """The first turn of a session the LIVE curator already wrote lines for
    (2026-09-26: the switch went live mid-session — this very session was
    curated live from turn 17,611 on — and a backfill walking the whole
    session would write those exchanges' lines a second time). Lines above
    what the backfill itself wrote (`done_hi`) are the live curator's; the
    backfill stops before the first of them."""
    cur.execute("SELECT min(turn_lo) FROM decision WHERE project = %s AND session_id = %s AND turn_lo > %s;",
                [project, session_id, done_hi])
    got = cur.fetchone()
    return int(got[0]) if got and got[0] is not None else None


def windows(scur, session_id: str, since: str = "", project: str = "") -> list[dict]:
    """A session's exchanges in windows of five, each with its pieces'
    turn ranges and days and the next window's first prompt; with `since`,
    only the turns said on or after that day (a sample of the recent past —
    the stretch a reader can judge)."""
    # with `project`: only the stretches of the session that belong to it
    # today (a split session gives each project its own turns)
    w, wp = _mine(scur, project) if project else ("TRUE", [])
    if since:
        scur.execute(f"SELECT turn_index, role, text, ts FROM chat WHERE session_id = %s "
                     f"AND ts >= %s AND {w} ORDER BY turn_index;", [session_id, since] + wp)
    else:
        scur.execute(f"SELECT turn_index, role, text, ts FROM chat WHERE session_id = %s "
                     f"AND {w} ORDER BY turn_index;", [session_id] + wp)
    return _group([(int(i), r, x or "", t) for i, r, x, t in scur.fetchall()])


def _group(rows: list[tuple]) -> list[dict]:
    """(turn_index, role, text, ts) in reading order → windows of five
    exchanges. A piece's day is its earliest turn's, and a window's range is
    the lowest and highest turn it holds."""
    from . import hubfeed, hubwindow
    day = {int(i): t for i, _r, _x, t in rows}
    pieces = hubfeed._pieces([{"index": int(i), "role": r, "text": x or ""} for i, r, x, _t in rows])

    def _day(q):
        ds = [d for d in (day.get(q[1]), day.get(q[2])) if d is not None]
        return min(ds) if ds else None
    groups: list[list] = []
    for p in pieces:
        g = groups[-1] if groups else None
        if g and len(g) < WINDOW and sum(len(hubwindow._cut(q[0])) for q in g) + len(hubwindow._cut(p[0])) <= WINDOW_CHARS:
            g.append(p)
        else:
            groups.append([p])
    out = []
    for k, g in enumerate(groups):
        n = len(g)
        days = [str(_day(q) or "")[:16] for q in g]
        text = "\n\n".join(f"===== EXCHANGE {j + 1} of {n} — {days[j]} =====\n\n{hubwindow._cut(q[0])}"
                           for j, q in enumerate(g)) if n > 1 else hubwindow._cut(g[0][0])
        nxt = groups[k + 1][0][0].split("\n\n[", 1)[0][:6000] if k + 1 < len(groups) else ""
        first = _day(g[0])
        out.append({"piece": text, "lo": min(q[1] for q in g), "hi": max(q[2] for q in g),
                    "ranges": [[q[1], q[2]] for q in g], "days": days,
                    "day": first.date() if first else None, "next": nxt, "n": n})
    return out


def in_time_order(parts: list[tuple[str, list[dict]]]):
    """(session, window) across a project's sessions in the order they were
    SAID — each window keyed by its first exchange's minute, earliest first
    (founder 2026-09-30: the rebuild goes in time order; sessions run side by
    side, and walking them one whole session at a time put brain's weeks-long
    sessions ahead of everything said beside them). A session never goes back
    in time: its windows keep their own order (a window with no day takes the
    one before it), so a resume by done_hi stays exact. Ties go to the session
    that began first."""
    import heapq

    def keyed(k: int, sid: str, wins: list[dict]):
        last = ""
        for j, w in enumerate(wins):
            t = next((str(d)[:16] for d in (w.get("days") or []) if d), "")
            last = max(last, t)
            yield (last, k, j), sid, w
    for _key, sid, w in heapq.merge(*(keyed(k, sid, wins) for k, (sid, wins) in enumerate(parts)),
                                    key=lambda x: x[0]):
        yield sid, w


STOP = threading.Event()        # backfill_stop: every project stops after its current window
CLOSED_WAIT_MAX_S = 6 * 3600     # a provider window (Ollama's plan) may stay closed for hours


def _ask(project: str, w: dict, opened: list[dict], tries: int, near: list[dict] | None = None) -> tuple[dict | None, str]:
    """The lines call, up to `tries` draws; (answer, why-it-failed). A CLOSED
    PROVIDER WINDOW is a wait, never a try (founder 2026-09-26: GLM's plan
    closes for hours — a run that burned its tries on that would skip every
    window until the plan refreshed)."""
    from . import judge
    why, waited = "", 0.0
    attempt = 0
    while attempt < max(1, tries):
        if STOP.is_set():
            return None, "stopped"
        try:
            ans = judge.ask("curate_probe", {"project": project, "piece": w["piece"], "prompt": "plines",
                                             "next": w["next"], "json": True, "max_tokens": 20000,
                                             "open_threads": opened, "near_lines": near or [],
                                             # every ask after the first is a new draw (hub/judge._call)
                                             "retry": attempt}, timeout=ASK_S)
        except judge.NoAnswer as e:
            why = f"no answer — {e.why}"
            if e.retry_s and waited < CLOSED_WAIT_MAX_S:
                # the hub says how long the provider's window stays closed:
                # wait it out, then ask again as if nothing happened
                wait = min(int(e.retry_s), 600)
                time.sleep(wait)
                waited += wait
                continue
            # anything else (the pipe down for a moment after a hub restart —
            # 2026-09-26: 19 windows skipped in one second) waits a little
            attempt += 1
            time.sleep(NO_ANSWER_WAIT_S)
            continue
        attempt += 1
        got = ans.get("answer")
        if isinstance(got, dict) and isinstance(got.get("decisions"), list):
            return got, ""
        why = f"unparseable — {ans.get('fault')[:120]}" if not isinstance(got, dict) else "no decisions list"
    return None, why


def _before(w: dict) -> _dt.date | None:
    """The day of a window's last exchange: the guard on what it may see."""
    for d in reversed(w.get("days") or []):
        try:
            return _dt.date.fromisoformat(str(d)[:10])
        except ValueError:
            continue
    return None


def run(project: str, source: str = "memory", dst: str = "memory", tries: int = TRIES,
        limit_windows: int = 0, only_session: str = "", log=print, since: str = "",
        span: tuple[str, str] | None = None, until: str = "") -> dict:
    """One project's archive into `dst`'s graph — or, with `span`, the
    sessions that began in [from, to). Resumes. Returns the counters. Every
    window sees and chains onto only lines said on or before its own day, so
    chunks of one project can run at once and a late redo never sees ahead."""
    from . import decisionline
    key = f"{project}@{span[0]}" if span else project
    tot = {"project": project, "sessions": 0, "windows": 0, "lines": 0, "skipped": 0, "asked_at": time.time(),
           "done": False, "key": key}
    with _LOCK:
        if RUNS.get(key, {}).get("running"):
            return {"project": project, "why": "already running"}
        RUNS[key] = dict(tot, running=True)
    tok = store.bind(dst)
    try:
        # the tables once, under the lock: four projects starting together
        # ran the same CREATE IF NOT EXISTS from four connections and Postgres
        # answered DeadlockDetected (2026-09-26, aurora on the studio box)
        with _LOCK, store.connection() as conn:
            cur = conn.cursor()
            cur.execute(STATE_DDL)
            cur.execute(decisionline.DDL)
            conn.commit()
        with _src_connection(source) as src:
            scur = src.cursor()
            parts: list[tuple[str, list[dict]]] = []
            for sid, _t0 in sessions(scur, project, since, span, until):
                skey = sid
                if only_session and not sid.startswith(only_session):
                    continue
                with store.connection() as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT done_hi, finished, windows FROM backfill_state WHERE project = %s AND session_id = %s;",
                                [project, skey])
                    st = cur.fetchone()
                done_hi, finished, done_n = (st[0], st[1], st[2] or 0) if st else (-1, False, 0)
                if finished:
                    continue
                # (2026-09-29: the 09-27 "a session is read once" guard is gone —
                # windows() reads only the turns this project owns, and
                # ownership splits a session with no overlap, so a split
                # session gives each owner its own part; the guard dropped
                # every part but the first — estate's of d84f3920 and 14662f6c)
                with store.connection() as conn:
                    live = live_from(conn.cursor(), project, sid, done_hi)
                wins = windows(scur, sid, since, project)
                if live is not None:
                    cut = [w for w in wins if w["hi"] >= live]
                    wins = [w for w in wins if w["hi"] < live]
                    if cut:
                        log(f"[backfill] {key} {sid[:8]}: stops before turn {live} — {len(cut)} windows "
                            f"already curated live")
                tot["sessions"] += 1
                wins = [w for w in wins if w["lo"] > done_hi]
                parts.append((skey, wins))
            # THE ORDER THINGS WERE SAID (founder 2026-09-30): every session's
            # windows merged, earliest first, so a line is written with every
            # line said before it already in view — a session that ran for
            # weeks no longer goes in whole before the sessions beside it.
            left = {sid: len(wins) for sid, wins in parts}
            for skey in [s for s, n in left.items() if n == 0]:
                with store.connection() as conn:
                    conn.cursor().execute("INSERT INTO backfill_state (project, session_id, finished) VALUES (%s, %s, true) "
                                          "ON CONFLICT (project, session_id) DO UPDATE SET finished = true, at = now();",
                                          [project, skey])
                    conn.commit()
            for sid, w in in_time_order(parts):
                skey = sid
                if STOP.is_set():
                    RUNS[key] = dict(tot, running=False)
                    log(f"[backfill] {key}: stopped on request after window {tot['windows']}")
                    return tot
                if limit_windows and tot["windows"] >= limit_windows:
                    RUNS[key] = dict(tot, running=False)
                    log(f"[backfill] {key}: stopped at the sample of {limit_windows} windows")
                    return tot
                with store.connection() as conn:
                    cur = conn.cursor()
                    opened, near = decisionline.view_near(cur, project, w["piece"], before=_before(w))
                    conn.commit()
                got, why = _ask(project, w, opened, tries, near)
                if got is None and why.startswith("no answer"):
                    # NO ANSWER IS AN OUTAGE, NOT A BAD WINDOW (2026-09-30: the
                    # 27B came up with a 12k context and refused every ask —
                    # 28 windows skipped in ten minutes, and a skip is never
                    # redone). The project halts here and the next run starts
                    # at this window; only an answer that came back broken skips.
                    with store.connection() as conn:
                        conn.cursor().execute(
                            "INSERT INTO backfill_skip (project, session_id, turn_lo, turn_hi, why) "
                            "VALUES (%s, %s, %s, %s, %s);",
                            [project, sid, w["lo"], w["hi"], f"HALTED — {why}"[:400]])
                        conn.commit()
                    tot["halted"] = f"{sid[:8]}#{w['lo']}"
                    log(f"[backfill] {key}: HALTED at {sid[:8]}#{w['lo']}-{w['hi']} — {why}; nothing after "
                        f"it is written, the next run starts here")
                    return tot
                n = 0
                with store.connection() as conn:
                    cur = conn.cursor()
                    if got is None:
                        cur.execute("INSERT INTO backfill_skip (project, session_id, turn_lo, turn_hi, why) "
                                    "VALUES (%s, %s, %s, %s, %s);", [project, sid, w["lo"], w["hi"], why[:400]])
                        tot["skipped"] += 1
                        log(f"[backfill] {project} {sid[:8]}#{w['lo']}-{w['hi']}: SKIPPED after {tries} — {why}")
                    else:
                        n = decisionline.write(cur, project, got.get("decisions") or [], sid, w["lo"], w["hi"],
                                               w["day"], "", threads=opened,
                                               questions=[str(q) for q in (got.get("questions") or [])],
                                               ranges=w["ranges"], days=w["days"], by_day=True)
                        tot["lines"] += n
                    tot["windows"] += 1
                    cur.execute("INSERT INTO backfill_state (project, session_id, done_hi, windows, lines, skipped, at) "
                                "VALUES (%s, %s, %s, 1, %s, %s, now()) ON CONFLICT (project, session_id) DO UPDATE SET "
                                "done_hi = EXCLUDED.done_hi, windows = backfill_state.windows + 1, "
                                "lines = backfill_state.lines + EXCLUDED.lines, "
                                "skipped = backfill_state.skipped + EXCLUDED.skipped, at = now();",
                                [project, skey, w["hi"], n, 1 if got is None else 0])
                    conn.commit()
                RUNS[key] = dict(tot, running=True)
                log(f"[backfill] {key} {sid[:8]}#{w['lo']}-{w['hi']}: {n} lines "
                    f"(session {tot['sessions']}, window {tot['windows']}, {tot['lines']} lines so far)")
                left[sid] -= 1
                if left[sid] == 0:
                    with store.connection() as conn:
                        conn.cursor().execute("INSERT INTO backfill_state (project, session_id, finished) VALUES (%s, %s, true) "
                                              "ON CONFLICT (project, session_id) DO UPDATE SET finished = true, at = now();",
                                              [project, skey])
                        conn.commit()
        tot["done"] = True
        log(f"[backfill] {key}: done — {tot}")
        return tot
    except Exception as e:                                       # noqa: BLE001
        traceback.print_exc()
        tot["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        log(f"[backfill] {key}: STOPPED — {tot['error']}")
        return tot
    finally:
        with _LOCK:
            RUNS[key] = dict(tot, running=False)
        store.unbind(tok)


def stop() -> str:
    """Every running project stops after the window it is on; the state
    keeps where it got to. `start` clears the flag."""
    STOP.set()
    return f"stop requested for {[p for p, r in RUNS.items() if r.get('running')]}"


def start(projects: list[str], source: str = "memory", dst: str = "memory", tries: int = TRIES,
          limit_windows: int = 0, only_session: str = "", since: str = "",
          spans: list[tuple[str, str]] | None = None, until: str = "") -> str:
    """One detached thread per project (a project's windows go in order;
    projects run side by side) — or, with `spans`, one per chunk of each
    project (the sessions that began in [from, to)). The container's log is
    the progress signal."""
    STOP.clear()
    started = []
    for p in projects:
        for span in (spans or [None]):
            key = f"{p}@{span[0]}" if span else p
            if RUNS.get(key, {}).get("running"):
                continue
            threading.Thread(target=run, args=(p, source, dst, tries, limit_windows, only_session),
                             kwargs={"log": lambda m: print(m, flush=True), "since": since,
                                     "span": tuple(span) if span else None, "until": until},
                             name=f"backfill-{key}", daemon=True).start()
            started.append(key)
    return f"backfill started for {started} → {dst} (source {source}{', since ' + since if since else ''})"


def status(dst: str = "memory") -> dict:
    """The live counters and the state table's totals."""
    tok = store.bind(dst)
    try:
        with store.connection() as conn:
            cur = conn.cursor()
            cur.execute(STATE_DDL)
            cur.execute("SELECT project, count(*), count(*) FILTER (WHERE finished), sum(windows), sum(lines), sum(skipped) "
                        "FROM backfill_state GROUP BY project ORDER BY project;")
            rows = [{"project": p, "sessions": n, "finished": f, "windows": w, "lines": ln, "skipped": s}
                    for p, n, f, w, ln, s in cur.fetchall()]
            cur.execute("SELECT project, session_id, turn_lo, turn_hi, why FROM backfill_skip ORDER BY id DESC LIMIT 20;")
            skips = [{"project": p, "session": s[:8], "lo": lo, "hi": hi, "why": w} for p, s, lo, hi, w in cur.fetchall()]
        return {"running": {p: r for p, r in RUNS.items() if r.get("running")}, "state": rows, "last_skips": skips}
    finally:
        store.unbind(tok)


def redo_skips(project: str, dst: str = "memory", tries: int = TRIES, log=print) -> dict:
    """The windows a run skipped (backfill_skip), asked again from the
    archive; a row that lands is deleted, one that fails keeps its reason."""
    from . import decisionline
    tot = {"project": project, "skips": 0, "landed": 0, "lines": 0, "still": 0}
    tok = store.bind(dst)
    try:
        with store.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, session_id, turn_lo, turn_hi FROM backfill_skip WHERE project = %s "
                        "AND why NOT LIKE 'HALTED%%' ORDER BY id;",
                        [project])
            rows = cur.fetchall()
        tot["skips"] = len(rows)
        with _src_connection("memory") as src:
            scur = src.cursor()
            for sk_id, sid, lo, hi in rows:
                w = next((x for x in windows(scur, sid, project=project) if x["lo"] == lo and x["hi"] == hi), None)
                if w is None:
                    # the window is cut differently now: ask the turns as one window
                    ws = [x for x in windows(scur, sid, project=project) if x["lo"] >= lo and x["hi"] <= hi]
                    if not ws:
                        tot["still"] += 1
                        continue
                    w = ws[0]
                with store.connection() as conn:
                    cur = conn.cursor()
                    opened, near = decisionline.view_near(cur, project, w["piece"], before=_before(w))
                    conn.commit()
                got, why = _ask(project, w, opened, tries, near)
                with store.connection() as conn:
                    cur = conn.cursor()
                    if got is None:
                        cur.execute("UPDATE backfill_skip SET why = %s, at = now() WHERE id = %s;", [why[:400], sk_id])
                        tot["still"] += 1
                        log(f"[backfill] {project} redo {sid[:8]}#{lo}-{hi}: still failing — {why}")
                    else:
                        n = decisionline.write(cur, project, got.get("decisions") or [], sid, w["lo"], w["hi"], w["day"], "",
                                               threads=opened, questions=[str(q) for q in (got.get("questions") or [])],
                                               ranges=w["ranges"], days=w["days"], by_day=True)
                        cur.execute("DELETE FROM backfill_skip WHERE id = %s;", [sk_id])
                        cur.execute("UPDATE backfill_state SET lines = lines + %s, skipped = greatest(0, skipped - 1), at = now() "
                                    "WHERE project = %s AND session_id = %s;", [n, project, sid])
                        tot["landed"] += 1
                        tot["lines"] += n
                        log(f"[backfill] {project} redo {sid[:8]}#{lo}-{hi}: {n} lines")
                    conn.commit()
        log(f"[backfill] {project}: redo done — {tot}")
        return tot
    finally:
        store.unbind(tok)
