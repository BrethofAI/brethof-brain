"""THE MEMORY OF DECISIONS AND ATTEMPTS, WITH THEIR REASONS (founder
2026-09-23: "this is the most brilliant way how we can use the graph").

Records hold what is true now. This holds how it came to be: what was
decided, tried, rejected or given up, what it replaced, why, the day, and
the words (a 'who' column was dropped 2026-09-26: no prompt since the V
series asked for it and nothing read it; the line's text names the user or
the assistant where it matters). Written by the curator in the same read of the
same exchange as the records (TEXTS["curator_decisions"]); never edited,
never closed by code — a later line on the same thing stacks on top, and the
stack is the story.

It exists for one moment: when someone — the founder in a prompt, the agent
in a proposal or a command — names a thing again, the thing's decisions come
first, so an idea already dropped is not proposed again and a dropped
thing's old recipe is not taken for today's. The test that asked for it
(2026-09-23, 43 proposals through the real hook): 13 warned with a reason,
5 handed the dropped thing's recipe as current.
"""
from __future__ import annotations

import datetime as _dt
import re


def _day(v, fallback) -> _dt.date | None:
    """The day the words give, when it is a real past day; else the exchange's."""
    try:
        d = _dt.date.fromisoformat(str(v or "").strip()[:10])
        if _dt.date(2015, 1, 1) <= d <= (fallback or _dt.date.today()):
            return d
    except ValueError:
        pass
    return fallback


KINDS = ("decided", "tried", "rejected", "dropped")

DDL = """
CREATE TABLE IF NOT EXISTS decision (
    id          bigserial PRIMARY KEY,
    project     text NOT NULL,
    day         date,
    kind        text NOT NULL,
    what        text NOT NULL,
    replaced    text NOT NULL DEFAULT '',
    why         text NOT NULL DEFAULT '',
    things      text[] NOT NULL DEFAULT '{}',
    said        text NOT NULL DEFAULT '',
    session_id  text NOT NULL DEFAULT '',
    turn_lo     integer NOT NULL DEFAULT -1,
    turn_hi     integer NOT NULL DEFAULT -1,
    embedding   halfvec(768),
    thing_vec   halfvec(768),
    at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS decision_project_day ON decision (project, day);
ALTER TABLE decision ADD COLUMN IF NOT EXISTS thing_vec halfvec(768);
ALTER TABLE decision ADD COLUMN IF NOT EXISTS thread text NOT NULL DEFAULT '';
ALTER TABLE decision ADD COLUMN IF NOT EXISTS follows bigint;
ALTER TABLE decision ADD COLUMN IF NOT EXISTS how text NOT NULL DEFAULT '';
ALTER TABLE decision ADD COLUMN IF NOT EXISTS thread_id bigint;
CREATE TABLE IF NOT EXISTS decision_thread (
    id          bigserial PRIMARY KEY,
    project     text NOT NULL,
    question    text NOT NULL,
    day         date,
    last_line   bigint,
    n_lines     integer NOT NULL DEFAULT 0,
    at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS decision_thread_project_at ON decision_thread (project, at DESC);
"""


def ensure(cur) -> None:
    """The decision tables through the one DDL door (store.ensure_ddl). The
    DDL's ALTER TABLE ... ADD COLUMN IF NOT EXISTS takes ACCESS EXCLUSIVE
    even when the column exists, so running it bare on every prompt's
    history call and every window's save deadlocked concurrent callers
    (the hubwindow fault of 2026-11-10, ee1da24). Unbound callers (a CLI,
    a bench) still run it directly."""
    from . import store
    try:
        store.ensure_ddl(cur, "decision", DDL)
    except store.NoTenantBound:
        cur.execute(DDL)

# THREADS (founder 2026-09-23, the approved design: "a newer entry on the same
# question stacks on top, and the stack is the story"; "you can find the
# things and follow — this was tested and so on"). Every line names the
# thread — the question or piece of work it advances — and which earlier
# line it takes further: continues, result (what it showed), overturns
# (reverses, rejects or replaces it). A push-back read in the next exchange
# overturns the line it answers, so a wrong line and its correction stay
# linked. Loose lines sorted by date were noise and let a wrong line surface
# alone (09-23 round three).
HOWS = ("continues", "result", "overturns")
EARLIER = 12      # the session's latest lines the curator sees
THREADS = 30      # the project's thread names it may reuse


def earlier(cur, project: str, session_id: str, lo: int, k: int = EARLIER) -> list[dict]:
    """This session's latest lines before `lo`, oldest first, numbered for
    the curator; each carries its id so 'follows' can be stored."""
    cur.execute("SELECT id, thread, kind, what, why FROM decision WHERE project = %s AND session_id = %s "
                "AND turn_lo < %s ORDER BY turn_lo DESC, id DESC LIMIT %s;", [project, session_id, lo, k])
    rows = list(reversed(cur.fetchall()))
    return [{"n": n, "id": i, "thread": t or "", "kind": kd, "what": w, "why": y}
            for n, (i, t, kd, w, y) in enumerate(rows, 1)]


# THREADS AS THINGS (2026-09-25, measured on the long40 thread key: the
# curator saw the session's last 12 lines while one window writes ~17, so the
# earlier step of a question was out of view — 15 of 144 real links drawn;
# threads were topic buckets, purity 66%). A thread is now a row of its own —
# one question, its latest line — and the curator is shown the project's
# open questions, each with its latest line; a line names the question it
# takes further (or starts one) and the code links it to that question's
# latest line. Questions as lanes, lines as dated steps: a graph to draw.
OPEN = 40        # the project's most recently moved questions the curator sees


def open_threads(cur, project: str, k: int = OPEN) -> list[dict]:
    """The project's questions, the most recently moved first, numbered for
    the curator, each with its latest line."""
    cur.execute("SELECT t.id, t.question, d.day, d.kind, d.what, t.last_line FROM decision_thread t "
                "LEFT JOIN decision d ON d.id = t.last_line WHERE t.project = %s "
                "ORDER BY t.at DESC, t.id DESC LIMIT %s;", [project, k])
    return [{"n": n, "id": i, "question": q, "day": day.isoformat() if day else "", "kind": kd or "",
             "what": w or "", "last_line": ll} for n, (i, q, day, kd, w, ll) in enumerate(cur.fetchall(), 1)]


# NEAR, NOT ONLY RECENT (2026-09-25): with the 40 most recently moved
# questions, links rose 9% → 49% and purity 66% → 88%, but 144 of 230 threads
# held one line — an older question dropped out of view and a near-duplicate
# was started. Now half the view is the questions nearest this piece: each
# exchange's user words are embedded and the questions whose lines sit
# closest are shown beside the most recent ones.
RECENT, NEARBY = 20, 20


def _user_words(piece: str) -> list[str]:
    """What the user said in each exchange of a piece (the questions of the
    work are asked there), one text per exchange."""
    out = []
    for ex in re.split(r"^={3,} EXCHANGE .*$", str(piece or ""), flags=re.M):
        said = re.findall(r"\[user\](.*?)(?=\n\[(?:assistant|user)\]|\Z)", ex, flags=re.S)
        t = " ".join(" ".join(said).split())[:1500]
        if t:
            out.append(t)
    return out


def open_threads_near(cur, project: str, piece: str, recent: int = RECENT, nearby: int = NEARBY,
                      before: _dt.date | None = None) -> list[dict]:
    """The questions the curator sees: the most recently moved, then those
    nearest this piece by meaning, numbered as one list. With `before` (the
    backfill, 2026-09-26: one project written by several date-ordered
    chunks at once) only lines said on or before that day count — recency is
    by the day a line was said, and each question shows its latest line up
    to that day, so a window never sees its own future."""
    from . import embed
    if before is None:
        cur.execute("SELECT id FROM decision_thread WHERE project = %s ORDER BY at DESC, id DESC LIMIT %s;",
                    [project, recent])
    else:
        cur.execute("SELECT thread_id FROM decision WHERE project = %s AND thread_id IS NOT NULL AND day <= %s "
                    "GROUP BY thread_id ORDER BY max(day) DESC, max(id) DESC LIMIT %s;", [project, before, recent])
    ids = [r[0] for r in cur.fetchall()]
    texts = _user_words(piece)
    vecs = embed.embed(texts, kind="query") if texts else []
    best: dict[int, float] = {}
    for v in vecs or []:
        if not v:
            continue
        cur.execute("SELECT thread_id, embedding <=> %s::halfvec FROM decision WHERE project = %s "
                    "AND thread_id IS NOT NULL AND embedding IS NOT NULL"
                    + (" AND day <= %s" if before is not None else "")
                    + " ORDER BY embedding <=> %s::halfvec LIMIT 60;",
                    [_vec(v), project] + ([before] if before is not None else []) + [_vec(v)])
        for tid, dist in cur.fetchall():
            if tid not in best or dist < best[tid]:
                best[tid] = dist
    for tid in sorted(best, key=best.get):
        if len(ids) >= recent + nearby:
            break
        if tid not in ids:
            ids.append(tid)
    if not ids:
        return []
    rows = {r[0]: r for r in _thread_rows(cur, ids, before)}
    return [{"n": n, "id": i, "question": rows[i][1], "day": rows[i][2].isoformat() if rows[i][2] else "",
             "kind": rows[i][3] or "", "what": rows[i][4] or "", "last_line": rows[i][5]}
            for n, i in enumerate([i for i in ids if i in rows], 1)]


def _thread_rows(cur, ids: list[int], before: _dt.date | None = None) -> list[tuple]:
    """(id, question, day, kind, what, last_line) of each question: its
    latest line, or with `before` its latest line said on or before that day."""
    if before is None:
        cur.execute("SELECT t.id, t.question, d.day, d.kind, d.what, t.last_line FROM decision_thread t "
                    "LEFT JOIN decision d ON d.id = t.last_line WHERE t.id = ANY(%s);", [ids])
    else:
        cur.execute("SELECT t.id, t.question, d.day, d.kind, d.what, d.id FROM decision_thread t "
                    "JOIN LATERAL (SELECT id, day, kind, what FROM decision WHERE thread_id = t.id AND day <= %s "
                    "ORDER BY day DESC, id DESC LIMIT 1) d ON true WHERE t.id = ANY(%s);", [before, ids])
    return cur.fetchall()


NEAR_LINES = 12   # the nearest existing lines the curator sees, by meaning


def view_near(cur, project: str, piece: str, k: int = NEAR_LINES,
              before: _dt.date | None = None, vecs_out: list | None = None) -> tuple[list[dict], list[dict]]:
    """THE VIEW WITH THE NEAREST LINES (2026-09-26): the open questions as
    open_threads_near gives them, PLUS the k lines of this project nearest
    the piece by meaning — each with the number of its question, the
    question added to the list when it was not there. A reversal, a
    replacement or a repeat of an existing line is a line the writer must
    see to join its question (measured: 35-45% of the key's reversals were
    not chained, 17-19% of lines repeated an item already written). With
    `before`, only lines said on or before that day (open_threads_near).
    `vecs_out` receives the piece's query vectors, so the window can search
    its records with them without a second embed."""
    from . import embed
    opened = open_threads_near(cur, project, piece, before=before)
    texts = _user_words(piece)
    vecs = embed.embed(texts, kind="query") if texts else []
    if vecs_out is not None:
        vecs_out.extend(v for v in vecs or [] if v)
    best: dict[int, tuple[float, tuple]] = {}
    for v in vecs or []:
        if not v:
            continue
        cur.execute("SELECT id, thread_id, kind, day, what, embedding <=> %s::halfvec FROM decision "
                    "WHERE project = %s AND embedding IS NOT NULL"
                    + (" AND day <= %s" if before is not None else "")
                    + " ORDER BY embedding <=> %s::halfvec LIMIT %s;",
                    [_vec(v), project] + ([before] if before is not None else []) + [_vec(v), k])
        for i, tid, kind, day, what, dist in cur.fetchall():
            if i not in best or dist < best[i][0]:
                best[i] = (dist, (i, tid, kind, day, what))
    near_rows = [r for _, r in sorted(best.values(), key=lambda x: x[0])[:k]]
    by_tid = {t["id"]: t for t in opened}
    n = len(opened)
    for i, tid, kind, day, what in near_rows:
        if tid and tid not in by_tid:
            got = _thread_rows(cur, [tid], before)
            r = got[0] if got else None
            if r:
                n += 1
                t = {"n": n, "id": r[0], "question": r[1], "day": r[2].isoformat() if r[2] else "",
                     "kind": r[3] or "", "what": r[4] or "", "last_line": r[5]}
                opened.append(t)
                by_tid[tid] = t
    near = [{"id": i, "n": by_tid[tid]["n"] if tid in by_tid else 0, "kind": kind,
             "day": day.isoformat() if day else "", "what": what} for i, tid, kind, day, what in near_rows]
    return opened, near


SUBJECT_EACH, SUBJECT_CAP = 4, 60   # nearest lines per record of the window, and the pool at most


def view_near_subjects(cur, project: str, piece: str, queries: list[str], each: int = SUBJECT_EACH,
                       cap: int = SUBJECT_CAP) -> tuple[list[dict], list[dict]]:
    """THE VIEW FROM THE WINDOW'S OWN SUBJECTS (founder 2026-10-02, the
    history design): the open questions as open_threads_near gives them,
    PLUS for each record the window's records call wrote, the `each` lines of
    this project nearest it by meaning — a wide pool (up to `cap`) the hub's
    judge then picks from. Same numbering as view_near: a pool line's
    question joins the list when it was not there. Measured on six later
    windows against today's view (the 12 nearest to the user's words):
    right links 47 vs 13, lines worth keeping 90% vs 85%, same catch."""
    from . import embed
    opened = open_threads_near(cur, project, piece)
    texts = [str(q)[:1500] for q in queries if str(q).strip()]
    vecs = embed.embed(texts, kind="query") if texts else []
    best: dict[int, tuple[float, tuple]] = {}
    for v in vecs or []:
        if not v:
            continue
        cur.execute("SELECT id, thread_id, kind, day, what, embedding <=> %s::halfvec FROM decision "
                    "WHERE project = %s AND embedding IS NOT NULL ORDER BY embedding <=> %s::halfvec LIMIT %s;",
                    [_vec(v), project, _vec(v), each])
        for i, tid, kind, day, what, dist in cur.fetchall():
            if i not in best or dist < best[i][0]:
                best[i] = (dist, (i, tid, kind, day, what))
    pool = [r for _, r in sorted(best.values(), key=lambda x: x[0])[:cap]]
    by_tid = {t["id"]: t for t in opened}
    n = len(opened)
    for i, tid, kind, day, what in pool:
        if tid and tid not in by_tid:
            got = _thread_rows(cur, [tid])
            r = got[0] if got else None
            if r:
                n += 1
                t = {"n": n, "id": r[0], "question": r[1], "day": r[2].isoformat() if r[2] else "",
                     "kind": r[3] or "", "what": r[4] or "", "last_line": r[5]}
                opened.append(t)
                by_tid[tid] = t
    near = [{"id": i, "n": by_tid[tid]["n"] if tid in by_tid else 0, "kind": kind,
             "day": day.isoformat() if day else "", "what": what} for i, tid, kind, day, what in pool]
    return opened, near


def _thread_for(cur, project: str, d: dict, by_n: dict[int, int], made: dict[str, int],
                day: _dt.date | None, questions: list[str] | None = None) -> int | None:
    """The thread a line takes further: a shown question by its number, else
    the question it names (the same words again, in this answer or stored),
    else a new question."""
    raw = str(d.get("thread") or "").strip().lower()
    m = re.fullmatch(r"q\s*(\d+)", raw)
    if m and questions and 1 <= int(m[1]) <= len(questions):
        # a new question this piece opened, listed once in the answer
        d = dict(d, question=questions[int(m[1]) - 1])
    try:
        tn = int(raw) if raw.isdigit() else 0
    except (TypeError, ValueError):
        tn = 0
    if tn in by_n:
        return by_n[tn]
    q = " ".join(str(d.get("question") or "").split())[:200]
    if not q:
        return None
    if q.lower() in made:
        return made[q.lower()]
    cur.execute("SELECT id FROM decision_thread WHERE project = %s AND lower(question) = lower(%s) "
                "ORDER BY id DESC LIMIT 1;", [project, q])
    row = cur.fetchone()
    if row:
        made[q.lower()] = row[0]
        return row[0]
    cur.execute("INSERT INTO decision_thread (project, question, day) VALUES (%s, %s, %s) RETURNING id;",
                [project, q, day])
    tid = cur.fetchone()[0]
    made[q.lower()] = tid
    return tid


def thread_names(cur, project: str, k: int = THREADS) -> list[str]:
    """The project's threads, most recently extended first."""
    cur.execute("SELECT thread FROM decision WHERE project = %s AND thread <> '' GROUP BY thread "
                "ORDER BY max(id) DESC LIMIT %s;", [project, k])
    return [t for (t,) in cur.fetchall()]


def _things_text(things: list[str], what: str) -> str:
    """What a line is ABOUT, as a proposal names it: the things alone. A
    proposal ('build them as MetaHumans in UE5') is far in meaning from the
    line that settles it ('MetaHuman is phased out and replaced by the H3
    reference workflow') and near to the names (round two, 2026-09-23)."""
    return "; ".join(things) if things else what


def _vec(v) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def write(cur, project: str, lines: list[dict], session_id: str, lo: int, hi: int,
          exchange_day: _dt.date | None, text: str = "", shown: list[dict] | None = None,
          threads: list[dict] | None = None, questions: list[str] | None = None,
          ranges: list[list[int]] | None = None, days: list[str] | None = None, by_day: bool = False) -> int:
    """The decisions one exchange produced. A 'said' that is not in the
    exchange's words is dropped (the quote proves; the line is what shows).
    `shown` is the earlier lines the curator saw: a line's 'follows' number
    is resolved against it, anything else links nowhere. `ranges` / `days`
    (2026-09-26): the window's exchanges' turn ranges and dates, so a line
    with "ex" points at ITS exchange, not the window's first turn."""
    if threads is not None:
        return _write_threaded(cur, project, lines, session_id, lo, hi, exchange_day, text, threads, questions,
                               ranges, days, by_day)
    by_n = {int(e["n"]): int(e["id"]) for e in (shown or [])}
    thread_of = {int(e["id"]): " ".join(str(e.get("thread") or "").split()).lower() for e in (shown or [])}
    from . import embed
    rows = []
    hay = " ".join(str(text or "").split()).lower()
    for d in lines or []:
        if not isinstance(d, dict):
            continue
        what = " ".join(str(d.get("what") or "").split())[:400]
        kind = str(d.get("kind") or "").strip().lower()
        if not what or kind not in KINDS:
            continue
        said = " ".join(str(d.get("said") or "").split())[:300]
        if said and hay and said.lower()[:80] not in hay:
            said = ""
        things = [str(t).strip()[:120] for t in (d.get("things") or []) if str(t).strip()][:8]
        replaced = " ".join(str(d.get("replaced") or "").split())[:160]
        if replaced and replaced.lower() not in (t.lower() for t in things):
            things.append(replaced)
        try:
            follows = by_n.get(int(d.get("follows") or 0))
        except (TypeError, ValueError):
            follows = None
        thread = " ".join(str(d.get("thread") or "").split())[:80]
        # a link stays inside its thread: a line that names one question and
        # links to a line of another contradicts itself (round one, 09-23:
        # 8 of 10 wrong links crossed threads, none of the right ones did)
        if follows and thread_of.get(follows, "") != thread.lower():
            follows = None
        how = str(d.get("how") or "").strip().lower()
        how = how if (follows and how in HOWS) else ("continues" if follows else "")
        rows.append([project, _day(d.get("day"), exchange_day), kind, what, replaced,
                     " ".join(str(d.get("why") or "").split())[:400],
                     things, said, session_id, lo, hi,
                     thread, follows, how])
    if not rows:
        return 0
    vecs = embed.embed([f"{r[3]} {r[5]} {' '.join(r[6])}" for r in rows], kind="document") or [None] * len(rows)
    tvecs = embed.embed([_things_text(r[6], r[3]) for r in rows], kind="document") or [None] * len(rows)
    n = 0
    for r, v, tv in zip(rows, vecs, tvecs):
        # the same line on the same day is one decision (a resumed session
        # says it twice)
        cur.execute("SELECT 1 FROM decision WHERE project = %s AND day IS NOT DISTINCT FROM %s "
                    "AND lower(what) = lower(%s) LIMIT 1;", [r[0], r[1], r[3]])
        if cur.fetchone():
            continue
        cur.execute("INSERT INTO decision (project, day, kind, what, replaced, why, things, said, "
                    "session_id, turn_lo, turn_hi, thread, follows, how, embedding, thing_vec) VALUES (%s, %s, %s, "
                    "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::halfvec, %s::halfvec);",
                    r + [None if v is None else _vec(v), None if tv is None else _vec(tv)])
        n += 1
    return n


def _write_threaded(cur, project: str, lines: list[dict], session_id: str, lo: int, hi: int,
                    exchange_day: _dt.date | None, text: str, threads: list[dict],
                    questions: list[str] | None = None, ranges: list[list[int]] | None = None,
                    days: list[str] | None = None, by_day: bool = False) -> int:
    """write() when the curator was shown the open questions: each line joins
    one (by number) or names a new one; 'follows' is that question's latest
    line at the moment the line lands, so lines of one answer chain in the
    order of their exchanges. `by_day` (the backfill, several date-ordered
    chunks of one project at once): 'follows' is the question's latest line
    said on or before this line's day, and the question's last line is its
    latest by day — a line written late never chains onto its own future."""
    from . import embed
    by_n = {int(t["n"]): int(t["id"]) for t in threads}

    def _at(d: dict) -> tuple[int, int, _dt.date | None]:
        """The line's own exchange (by "ex") when the window's ranges are known."""
        e = str(d.get("ex") or "")
        if ranges and e.isdigit() and 1 <= int(e) <= len(ranges):
            r = ranges[int(e) - 1]
            day = exchange_day
            if days and int(e) <= len(days) and str(days[int(e) - 1])[:10]:
                try:
                    day = _dt.date.fromisoformat(str(days[int(e) - 1])[:10])
                except ValueError:
                    pass
            return int(r[0]), int(r[1]), day
        return lo, hi, exchange_day
    hay = " ".join(str(text or "").split()).lower()
    keep = []
    for d in sorted([x for x in lines or [] if isinstance(x, dict)], key=lambda x: int(x.get("ex") or 0)
                    if str(x.get("ex") or "").lstrip("-").isdigit() else 0):
        what = " ".join(str(d.get("what") or "").split())[:400]
        kind = str(d.get("kind") or "").strip().lower()
        if what and kind in KINDS:
            keep.append((d, what, kind))
    if not keep:
        return 0
    rows = []
    for d, what, kind in keep:
        said = " ".join(str(d.get("said") or "").split())[:300]
        if said and hay and said.lower()[:80] not in hay:
            said = ""
        things = [str(t).strip()[:120] for t in (d.get("things") or []) if str(t).strip()][:8]
        replaced = " ".join(str(d.get("replaced") or "").split())[:160]
        if replaced and replaced.lower() not in (t.lower() for t in things):
            things.append(replaced)
        lo_, hi_, day_ = _at(d)
        rows.append((d, [project, _day(d.get("day"), day_), kind, what, replaced,
                         " ".join(str(d.get("why") or "").split())[:400],
                         things, said, session_id, lo_, hi_]))
    vecs = embed.embed([f"{r[3]} {r[5]} {' '.join(r[6])}" for _, r in rows], kind="document") or [None] * len(rows)
    tvecs = embed.embed([_things_text(r[6], r[3]) for _, r in rows], kind="document") or [None] * len(rows)
    made: dict[str, int] = {}
    n = 0
    for (d, r), v, tv in zip(rows, vecs, tvecs):
        cur.execute("SELECT 1 FROM decision WHERE project = %s AND day IS NOT DISTINCT FROM %s "
                    "AND lower(what) = lower(%s) LIMIT 1;", [r[0], r[1], r[3]])
        if cur.fetchone():
            continue
        tid = _thread_for(cur, project, d, by_n, made, r[1],
                          [" ".join(str(q).split())[:200] for q in (questions or []) if str(q).strip()])
        question, follows = "", None
        if tid:
            cur.execute("SELECT question, last_line FROM decision_thread WHERE id = %s;", [tid])
            question, follows = cur.fetchone()
            if by_day and r[1] is not None:
                cur.execute("SELECT id FROM decision WHERE thread_id = %s AND day <= %s "
                            "ORDER BY day DESC, id DESC LIMIT 1;", [tid, r[1]])
                got = cur.fetchone()
                follows = got[0] if got else None
        how = str(d.get("how") or "").strip().lower()
        how = how if (follows and how in HOWS) else ("continues" if follows else "")
        cur.execute("INSERT INTO decision (project, day, kind, what, replaced, why, things, said, "
                    "session_id, turn_lo, turn_hi, thread, follows, how, thread_id, embedding, thing_vec) VALUES "
                    "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::halfvec, %s::halfvec) "
                    "RETURNING id;",
                    r + [question[:200], follows, how, tid,
                         None if v is None else _vec(v), None if tv is None else _vec(tv)])
        new_id = cur.fetchone()[0]
        if tid and by_day:
            cur.execute("UPDATE decision_thread SET last_line = (SELECT id FROM decision WHERE thread_id = %s "
                        "ORDER BY day DESC NULLS LAST, id DESC LIMIT 1), n_lines = n_lines + 1, at = now() "
                        "WHERE id = %s;", [tid, tid])
        elif tid:
            cur.execute("UPDATE decision_thread SET last_line = %s, n_lines = n_lines + 1, at = now() "
                        "WHERE id = %s;", [new_id, tid])
        n += 1
    return n


NEAR = 0.50      # cosine distance under which a line counts as about the words by meaning alone
NAMED = 0.60     # a line whose things the words name may sit this far (round 1b: false lines sat at 0.61-0.69)
POOL = 30


def _named(cur, projects: list[str], text: str) -> list[str]:
    """The SPECIFIC things decisions are about that the words name — whole
    words, any case, a plural or possessive ending allowed ('MetaHumans'
    names MetaHuman). A thing most lines carry ('graph' in a memory
    project) names nothing: round one matched every brain line through it."""
    cur.execute("SELECT t, count(*) FROM decision, unnest(things) t WHERE project = ANY(%s) GROUP BY t;", [projects])
    rows = cur.fetchall()
    cur.execute("SELECT count(*) FROM decision WHERE project = ANY(%s);", [projects])
    total = (cur.fetchone() or [0])[0] or 0
    common = max(5, int(total * 0.25))
    low = " " + " ".join(str(text or "").lower().split()) + " "
    out = []
    for t, df in rows:
        n = " ".join(str(t).lower().split())
        if len(n) >= 3 and df <= common and re.search(
                r"(?<![\w-])" + re.escape(n) + r"(?:s|es|'s)?(?![\w-])", low):
            out.append(t)
    return out


def find(cur, project: str, text: str, k: int = 3) -> list[dict]:
    """The decisions these words touch — by meaning and by the specific
    things they name, fused — the project's and the global ones. A line
    found by meaning alone must sit under NEAR; one the words name, under
    NAMED."""
    from . import embed
    projects = [project, "global"] if project != "global" else ["global"]
    score: dict[int, float] = {}
    dist: dict[int, float] = {}
    hit: set[int] = set()
    v = embed.embed_one(text, kind="query")
    if v:
        cur.execute("SELECT id, embedding <=> %s::halfvec FROM decision WHERE project = ANY(%s) "
                    "AND embedding IS NOT NULL ORDER BY 2 LIMIT %s;", [_vec(v), projects, POOL])
        for rank, (i, d) in enumerate(cur.fetchall()):
            dist[i] = float(d)
            score[i] = score.get(i, 0) + 1 / (60 + rank)
    names = _named(cur, projects, text)
    if names:
        cur.execute("SELECT id, embedding <=> %s::halfvec FROM decision WHERE project = ANY(%s) "
                    "AND things && %s::text[] ORDER BY day DESC NULLS LAST, id DESC LIMIT %s;",
                    [_vec(v) if v else None, projects, names, POOL])
        for rank, (i, d) in enumerate(cur.fetchall()):
            hit.add(i)
            if d is not None:
                dist[i] = float(d)
            score[i] = score.get(i, 0) + 1 / (60 + rank)
    keep = [i for i in score if dist.get(i, 9) < (NAMED if i in hit else NEAR)]
    top = sorted(keep, key=lambda i: -score[i])[:k]
    if not top:
        return []
    cur.execute("SELECT id, project, day, kind, what, why, things, session_id, turn_lo, turn_hi, thread "
                "FROM decision WHERE id = ANY(%s);", [top])
    rows = {r[0]: r for r in cur.fetchall()}
    out = []
    for i in top:
        _, p, day, kind, what, why, things, sid, lo, hi, th = rows[i]
        out.append({"id": i, "project": p, "day": day.isoformat() if day else "", "kind": kind,
                    "what": what, "why": why, "things": list(things or []), "thread": th or "",
                    "named": i in hit, "distance": round(dist[i], 3) if i in dist else None,
                    "at": f"{(sid or '')[:8]}#{lo}-{hi}"})
    # the newest first: the latest word on a thing reads first
    out.sort(key=lambda d: (d["day"], d["id"]), reverse=True)
    return out


def candidates(cur, project: str, text: str, k: int = 10, projects: list[str] | None = None,
               vec: list | None = None, since=None, until=None) -> list[dict]:
    """The few lines worth ASKING about — no bar, three arms fused: the
    line's meaning, the meaning of the things it is about, and the specific
    things the words name. Round two (2026-09-23): the right line ranked
    first far more often than any distance could tell it from a wrong one,
    so a small model call (hub op decision_check) chooses among these.
    Ten, not six (2026-09-25, 97 re-proposals from the long40 decisions key:
    with six the right line was often never shown to the check — 75 found;
    with ten, 81, and still no false alarm on 20 controls)."""
    from . import embed
    # `projects` (2026-09-28, the recall tool): an explicit set — a scoped
    # key's projects, or every project for an unscoped search; `vec`: the
    # caller's query vector, so the query is not embedded twice
    if projects is None:
        projects = [project, "global"] if project != "global" else ["global"]
    if not projects:
        return []
    score: dict[int, float] = {}
    # a period (2026-10-23, the period report): lines whose day is in it
    dsql, dargs = "", []
    if since:
        dsql, dargs = dsql + " AND day >= %s", dargs + [since]
    if until:
        dsql, dargs = dsql + " AND day < %s", dargs + [until]
    v = vec if vec else embed.embed_one(text, kind="query")
    if v:
        for col in ("embedding", "thing_vec"):
            cur.execute(f"SELECT id FROM decision WHERE project = ANY(%s) AND {col} IS NOT NULL{dsql} "
                        f"ORDER BY {col} <=> %s::halfvec LIMIT %s;", [projects] + dargs + [_vec(v), POOL])
            for rank, (i,) in enumerate(cur.fetchall()):
                score[i] = score.get(i, 0) + 1 / (60 + rank)
    names = _named(cur, projects, text)
    if names:
        cur.execute("SELECT id FROM decision WHERE project = ANY(%s) AND things && %s::text[]" + dsql +
                    " ORDER BY day DESC NULLS LAST, id DESC LIMIT %s;", [projects, names] + dargs + [POOL])
        for rank, (i,) in enumerate(cur.fetchall()):
            score[i] = score.get(i, 0) + 1 / (60 + rank)
    top = sorted(score, key=lambda i: -score[i])[:k]
    if not top:
        return []
    cur.execute("SELECT id, project, day, kind, what, why, things, session_id, turn_lo, turn_hi, thread "
                "FROM decision WHERE id = ANY(%s);", [top])
    rows = {r[0]: r for r in cur.fetchall()}
    out = []
    for n, i in enumerate(top, 1):
        _, p, day, kind, what, why, things, sid, lo, hi, th = rows[i]
        out.append({"n": n, "id": i, "project": p, "day": day.isoformat() if day else "", "kind": kind,
                    "what": what, "why": why, "things": list(things or []), "thread": th or "",
                    "named": False, "distance": None, "at": f"{(sid or '')[:8]}#{lo}-{hi}",
                    "session_id": sid or "", "turn_hi": hi})
    return out


def chain(cur, line_id: int, k: int = 6) -> list[dict]:
    """What a found line is part of, newest first: the line, what came
    after it on its links (what continued it, what it showed, what
    overturned it) and what it came from — never the thread's newest lines
    whatever they are (round one, 09-23: a 28-line thread showed six lines
    that had nothing to do with the one found)."""
    cur.execute("""WITH RECURSIVE up(id, depth) AS (
                       SELECT %s::bigint, 0
                       UNION SELECT d.follows, up.depth + 1 FROM decision d JOIN up ON d.id = up.id
                       WHERE d.follows IS NOT NULL AND up.depth < 4),
                   down(id, depth) AS (
                       SELECT %s::bigint, 0
                       UNION SELECT d.id, down.depth + 1 FROM decision d JOIN down ON d.follows = down.id
                       WHERE down.depth < 4)
                   SELECT d.id, d.day, d.kind, d.what, d.why, d.follows, d.how, d.turn_lo
                   FROM decision d WHERE d.id IN (SELECT id FROM up UNION SELECT id FROM down)
                   ORDER BY d.day DESC NULLS LAST, d.turn_lo DESC, d.id DESC LIMIT %s;""",
                [line_id, line_id, k])
    rows = cur.fetchall()
    ids = [r[0] for r in rows]
    over: dict[int, int] = {}
    if ids:
        cur.execute("SELECT follows, id FROM decision WHERE follows = ANY(%s) AND how = 'overturns';", [ids])
        over = {f: i for f, i in cur.fetchall()}
    return [{"id": i, "day": d.isoformat() if d else "", "kind": kd, "what": w, "why": y,
             "follows": f, "how": h, "overturned_by": over.get(i), "found": i == line_id}
            for i, d, kd, w, y, f, h, _lo in rows]


def render(lines: list[dict], chains: dict[int, list[dict]] | None = None) -> str:
    """What the hook shows first: each found line with what it is part of,
    newest first, so a line never reads without what came after it."""
    if not lines:
        return ""
    out = ["**Brain — decided before** (what was chosen, tried or given up, and why — "
           "check before proposing it again):"]
    seen: set[int] = set()
    for d in lines:
        if d.get("id") in seen:
            continue
        ch = (chains or {}).get(d.get("id")) or []
        if len(ch) > 1:
            out.append(f"- '{d.get('thread') or 'the same question'}', newest first:")
            for c in ch:
                seen.add(c["id"])
                why = f" — why: {c['why']}" if c.get("why") else ""
                ov = " [overturned later]" if c.get("overturned_by") else ""
                out.append(f"    {c['kind']} {c['day'] or '????-??-??'}: {c['what']}{why}{ov}")
            continue
        seen.add(d.get("id"))
        why = f" — why: {d['why']}" if d.get("why") else ""
        out.append(f"- {d['kind']} {d['day'] or '????-??-??'}: {d['what']}{why}")
    return "\n".join(out)
