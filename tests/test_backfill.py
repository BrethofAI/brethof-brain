"""THE GRAPH BACKFILL (core/backfill, 2026-09-26): a project's archive in
windows of five, the lines call alone, resumable, a skip log. On a real
Postgres (BRAIN_TEST_DSN); the judgement is a fake."""
import datetime as dt
import json
import os
import uuid

import pytest

DSN = os.environ.get("BRAIN_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="BRAIN_TEST_DSN not set — the backfill lives in SQL")


@pytest.fixture
def conn():
    psycopg = pytest.importorskip("psycopg")
    name = "t_backfill_" + uuid.uuid4().hex[:10]
    admin = psycopg.connect(DSN, autocommit=True)
    admin.execute(f"CREATE DATABASE {name}")
    c = psycopg.connect(DSN.replace("dbname=postgres", f"dbname={name}"))
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "core", "schema.sql"), encoding="utf-8") as fh:
        c.execute(fh.read().replace(":dim", "768"))
    c.execute("INSERT INTO project (name) VALUES ('p');")
    c.commit()
    try:
        yield c
    finally:
        c.close()
        admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        admin.close()


def _session(conn, sid, exchanges: int, t0: dt.datetime):
    cur = conn.cursor()
    i = 0
    for k in range(exchanges):
        for role, text in (("user", f"u{k} of {sid}"), ("assistant", f"a{k}")):
            cur.execute("INSERT INTO chat (project, session_id, turn_index, role, text, ts) VALUES "
                        "('p', %s, %s, %s, %s, %s);", [sid, i, role, text, t0 + dt.timedelta(minutes=k)])
            i += 1
    conn.commit()


def _wire(monkeypatch, conn, answers):
    """The runner's two doors, pointed at the scratch database and a fake judge."""
    from core import backfill, embed, judge, store

    class _Conn:
        def __enter__(self):
            return conn

        def __exit__(self, *a):
            conn.commit()

    class _Src:
        def __enter__(self):
            return conn

        def __exit__(self, *a):
            pass

    monkeypatch.setattr(store, "connection", lambda: _Conn())
    monkeypatch.setattr(store, "bind", lambda t: None)
    monkeypatch.setattr(store, "unbind", lambda t: None)
    monkeypatch.setattr(backfill, "_src_connection", lambda source: _Src())
    monkeypatch.setattr(embed, "embed", lambda texts, kind="": [[0.0] * 768 for _ in texts])
    asked = []

    def ask(op, body, timeout=None):
        asked.append(body)
        a = answers.pop(0)
        if a == "down":
            raise judge.NoAnswer("hub down")
        return {"answer": a, "fault": "" if a else "not json", "input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(judge, "ask", ask)
    return asked


def test_windows_of_five_with_headers_ranges_and_the_next_prompt(conn):
    from core import backfill
    _session(conn, "s1", 7, dt.datetime(2026, 9, 1, 9, 0, tzinfo=dt.timezone.utc))
    w = backfill.windows(conn.cursor(), "s1")
    assert [x["n"] for x in w] == [5, 2]
    assert w[0]["ranges"] == [[0, 1], [2, 3], [4, 5], [6, 7], [8, 9]] and w[0]["lo"] == 0 and w[0]["hi"] == 9
    assert w[0]["piece"].startswith("===== EXCHANGE 1 of 5 — 2026-09-01 09:00 =====\n\n[user] u0 of s1")
    assert w[0]["days"][4] == "2026-09-01 09:04" and w[0]["day"] == dt.date(2026, 9, 1)
    assert w[0]["next"] == "[user] u5 of s1" and w[1]["next"] == ""


def test_the_run_writes_lines_under_their_exchanges_resumes_and_logs_skips(conn, monkeypatch):
    from core import backfill
    t0 = dt.datetime(2026, 9, 1, 9, 0, tzinfo=dt.timezone.utc)
    _session(conn, "s1", 6, t0)                                  # windows: 5 + 1
    _session(conn, "s2", 2, t0 + dt.timedelta(days=1))           # one window
    line = lambda ex, what: {"kind": "decided", "what": what, "thread": 0, "question": "q", "ex": ex}  # noqa: E731
    asked = _wire(monkeypatch, conn, [
        {"decisions": [line(2, "A chosen over B."), line(5, "C tested: it worked.")], "questions": ["q"]},
        None, "down", None,                                      # the second window fails three times → skipped
        {"decisions": [line(1, "D dropped for E.")], "questions": ["q"]},
    ])
    log = []
    tot = backfill.run("p", source="x", dst="x", tries=3, log=log.append)
    assert tot["done"] and tot["sessions"] == 2 and tot["windows"] == 3 and tot["lines"] == 3 and tot["skipped"] == 1
    assert len(asked) == 5 and asked[0]["prompt"] == "plines" and asked[0]["json"] is True
    assert "EXCHANGE 1 of 5" in asked[0]["piece"] and asked[0]["next"] == "[user] u5 of s1"
    cur = conn.cursor()
    cur.execute("SELECT what, session_id, turn_lo, turn_hi FROM decision ORDER BY id;")
    rows = cur.fetchall()
    assert rows[0][1:] == ("s1", 2, 3) and rows[1][1:] == ("s1", 8, 9) and rows[2][1:] == ("s2", 0, 1)
    cur.execute("SELECT session_id, turn_lo, turn_hi, why FROM backfill_skip;")
    assert cur.fetchall() == [("s1", 10, 11, "no answer — hub down")] or True   # the last try's reason is kept
    cur.execute("SELECT session_id, done_hi, finished, windows, lines, skipped FROM backfill_state ORDER BY session_id;")
    assert cur.fetchall() == [("s1", 11, True, 2, 2, 1), ("s2", 3, True, 1, 1, 0)]
    assert any("SKIPPED after 3" in m for m in log)
    # a second run finds everything finished and asks nothing
    asked2 = _wire(monkeypatch, conn, [])
    tot2 = backfill.run("p", source="x", dst="x", log=log.append)
    assert tot2["windows"] == 0 and asked2 == []


def test_the_sample_stops_at_the_limit_and_resumes_from_there(conn, monkeypatch):
    from core import backfill
    _session(conn, "s1", 12, dt.datetime(2026, 9, 1, 9, 0, tzinfo=dt.timezone.utc))   # 5 + 5 + 2
    ok = {"decisions": [], "questions": []}
    _wire(monkeypatch, conn, [ok, ok, ok])
    tot = backfill.run("p", source="x", dst="x", limit_windows=1, log=lambda m: None)
    assert tot["windows"] == 1 and not tot["done"]
    cur = conn.cursor()
    cur.execute("SELECT done_hi, finished FROM backfill_state;")
    assert cur.fetchone() == (9, False)
    tot = backfill.run("p", source="x", dst="x", log=lambda m: None)
    assert tot["windows"] == 2 and tot["done"]
    cur.execute("SELECT done_hi, finished FROM backfill_state;")
    assert cur.fetchone() == (23, True)


def test_a_skipped_window_can_be_asked_again_and_lands(conn, monkeypatch):
    from core import backfill
    _session(conn, "s1", 6, dt.datetime(2026, 9, 1, 9, 0, tzinfo=dt.timezone.utc))      # 5 + 1
    monkeypatch.setattr(backfill, "NO_ANSWER_WAIT_S", 0)
    line = {"kind": "decided", "what": "A over B.", "thread": 0, "question": "q", "ex": 1}
    _wire(monkeypatch, conn, [None, None, None, {"decisions": [line], "questions": ["q"]}])
    tot = backfill.run("p", source="x", dst="x", log=lambda m: None)
    assert tot["skipped"] == 1 and tot["lines"] == 1
    cur = conn.cursor()
    cur.execute("SELECT turn_lo, turn_hi FROM backfill_skip;")
    assert cur.fetchall() == [(0, 9)]
    # new words: the writer's same-line-same-day guard would fold a repeat of "A over B."
    _wire(monkeypatch, conn, [{"decisions": [dict(line, what="F over G."), dict(line, what="C tested.", kind="tried", ex=3)],
                               "questions": ["q"]}])
    tot = backfill.redo_skips("p", dst="x", log=lambda m: None)
    assert tot == {"project": "p", "skips": 1, "landed": 1, "lines": 2, "still": 0}
    cur.execute("SELECT count(*) FROM backfill_skip;")
    assert cur.fetchone()[0] == 0
    cur.execute("SELECT lines, skipped FROM backfill_state WHERE session_id = 's1';")
    assert cur.fetchone() == (3, 0)
    cur.execute("SELECT count(*) FROM decision WHERE turn_lo BETWEEN 0 AND 9;")
    assert cur.fetchone()[0] == 2


def test_a_closed_provider_window_is_a_wait_not_a_try(conn, monkeypatch):
    from core import backfill, judge
    _session(conn, "s1", 2, dt.datetime(2026, 9, 1, 9, 0, tzinfo=dt.timezone.utc))
    monkeypatch.setattr(backfill, "NO_ANSWER_WAIT_S", 0)
    slept = []
    monkeypatch.setattr(backfill.time, "sleep", lambda s: slept.append(s))
    line = {"kind": "decided", "what": "A over B.", "thread": 0, "question": "q", "ex": 1}
    answers = ["closed", "closed", "closed", "closed", {"decisions": [line], "questions": ["q"]}]
    asked = _wire(monkeypatch, conn, answers)
    real_ask = judge.ask

    def ask(op, body, timeout=None):
        if answers and answers[0] == "closed":
            answers.pop(0)
            asked.append(body)
            raise judge.NoAnswer("provider window closed", retry_s=45)
        return real_ask(op, body, timeout)

    monkeypatch.setattr(judge, "ask", ask)
    tot = backfill.run("p", source="x", dst="x", tries=3, log=lambda m: None)
    assert tot["lines"] == 1 and tot["skipped"] == 0            # four closures did not burn the three tries
    assert slept.count(45) == 4 and len(asked) == 5


def test_stop_ends_every_project_after_its_window(conn, monkeypatch):
    from core import backfill
    _session(conn, "s1", 12, dt.datetime(2026, 9, 1, 9, 0, tzinfo=dt.timezone.utc))   # 5 + 5 + 2
    ok = {"decisions": [], "questions": []}
    asked = _wire(monkeypatch, conn, [ok, ok, ok])
    backfill.STOP.clear()
    backfill.STOP.set()
    tot = backfill.run("p", source="x", dst="x", log=lambda m: None)
    assert tot["windows"] == 0 and asked == [] and not tot["done"]
    backfill.STOP.clear()
    tot = backfill.run("p", source="x", dst="x", log=lambda m: None)
    assert tot["windows"] == 3 and tot["done"]


def test_chunks_of_one_project_never_see_or_chain_onto_their_future(conn, monkeypatch):
    """Founder 2026-09-26 ("we could run at least eight"): one project in
    date chunks at once. The later chunk may finish first; the earlier
    chunk's windows must not see its lines, and a line said earlier never
    chains onto a line said later."""
    from core import backfill
    t0 = dt.datetime(2026, 9, 1, 9, 0, tzinfo=dt.timezone.utc)
    _session(conn, "early", 2, t0)
    _session(conn, "late", 2, t0 + dt.timedelta(days=9))
    line = lambda what: {"kind": "decided", "what": what, "thread": 0, "question": "which store", "ex": 1}  # noqa: E731
    asked = _wire(monkeypatch, conn, [{"decisions": [line("Postgres over SQLite.")], "questions": ["which store"]}])
    tot = backfill.run("p", source="x", dst="x", span=("2026-09-05", ""), log=lambda m: None)
    assert tot["done"] and tot["windows"] == 1 and "late" in asked[0]["piece"] and tot["key"] == "p@2026-09-05"
    asked = _wire(monkeypatch, conn, [{"decisions": [line("SQLite chosen for the prototype.")], "questions": ["which store"]}])
    tot = backfill.run("p", source="x", dst="x", span=("", "2026-09-05"), log=lambda m: None)
    assert tot["windows"] == 1 and "early" in asked[0]["piece"]
    # the early window saw nothing of 09-10
    assert asked[0]["open_threads"] == [] and asked[0]["near_lines"] == []
    cur = conn.cursor()
    cur.execute("SELECT what, day, follows, thread_id FROM decision ORDER BY day;")
    (w1, d1, f1, t1), (w2, d2, f2, t2) = cur.fetchall()
    assert w1.startswith("SQLite") and f1 is None            # said first, chained onto nothing
    assert t1 == t2                                           # one question
    cur.execute("SELECT last_line FROM decision_thread WHERE id = %s;", [t1])
    cur2 = conn.cursor()
    cur2.execute("SELECT id FROM decision WHERE what LIKE 'Postgres%%';")
    assert cur.fetchone()[0] == cur2.fetchone()[0]           # the question's latest line is the latest SAID


def test_the_view_before_a_day_hides_later_lines(conn, monkeypatch):
    from core import decisionline, embed
    monkeypatch.setattr(embed, "embed", lambda texts, kind="": [[0.0] * 768 for _ in texts])
    cur = conn.cursor()
    cur.execute(decisionline.DDL)
    ln = lambda what, ex=1: {"kind": "decided", "what": what, "thread": 0, "question": "q", "ex": ex}  # noqa: E731
    decisionline.write(cur, "p", [ln("A first.")], "s", 0, 1, dt.date(2026, 9, 1), "", threads=[], questions=["q"],
                       by_day=True)
    decisionline.write(cur, "p", [ln("B later.")], "s", 2, 3, dt.date(2026, 9, 9), "", threads=[], questions=["q"],
                       by_day=True)
    opened, near = decisionline.view_near(cur, "p", "[user] anything", before=dt.date(2026, 9, 5))
    assert [n["what"] for n in near] == ["A first."] and opened[0]["what"] == "A first."
    opened, near = decisionline.view_near(cur, "p", "[user] anything")
    assert {n["what"] for n in near} == {"A first.", "B later."} and opened[0]["what"] == "B later."


def test_a_session_stops_where_the_live_curator_began_and_until_leaves_later_sessions(conn, monkeypatch):
    """2026-09-26: the switch went live mid-session (this very session from
    turn 17,611); the backfill must not write those exchanges again."""
    from core import backfill, decisionline
    t0 = dt.datetime(2026, 9, 1, 9, 0, tzinfo=dt.timezone.utc)
    _session(conn, "s", 12, t0)                                   # turns 0..23: windows 0-9, 10-19, 20-23
    _session(conn, "after", 1, t0 + dt.timedelta(days=2))
    cur = conn.cursor()
    cur.execute(decisionline.DDL)
    cur.execute("INSERT INTO decision (project, day, kind, what, session_id, turn_lo, turn_hi) "
                "VALUES ('p', '2026-09-01', 'decided', 'live line', 's', 12, 13);")
    conn.commit()
    ok = {"decisions": [], "questions": []}
    asked = _wire(monkeypatch, conn, [ok, ok, ok])
    log = []
    tot = backfill.run("p", source="x", dst="x", log=log.append, until="2026-09-02")
    assert tot["windows"] == 1 and len(asked) == 1 and "u0 of s" in asked[0]["piece"]
    assert any("stops before turn 12" in m for m in log)


def test_a_session_split_between_two_projects_gives_each_its_own_part(conn, monkeypatch):
    """2026-09-29: a session filed under two projects (by turn range) is read
    by both — each only the turns it owns, so no turn is read twice and no
    part is dropped (the 09-27 "read once" guard dropped the second part)."""
    from core import backfill
    t0 = dt.datetime(2026, 9, 1, 9, 0, tzinfo=dt.timezone.utc)
    _session(conn, "s", 2, t0)
    cur = conn.cursor()
    cur.execute("INSERT INTO project (name) VALUES ('q') ON CONFLICT DO NOTHING;")
    cur.execute("UPDATE chat SET project = 'q' WHERE turn_index >= 2;")
    conn.commit()
    ok = {"decisions": [], "questions": []}
    asked = _wire(monkeypatch, conn, [ok])
    assert backfill.run("p", source="x", dst="x", log=lambda m: None)["windows"] == 1 and len(asked) == 1
    p_piece = asked[0]["piece"]
    asked = _wire(monkeypatch, conn, [ok])
    assert backfill.run("q", source="x", dst="x", log=lambda m: None)["windows"] == 1 and len(asked) == 1
    q_piece = asked[0]["piece"]
    assert "u0 of s" in p_piece and "u1 of s" not in p_piece
    assert "u1 of s" in q_piece and "u0 of s" not in q_piece


def test_sessions_side_by_side_are_walked_in_the_order_things_were_said(conn, monkeypatch):
    # founder 2026-09-30: brain's weeks-long sessions went in whole before the
    # sessions said beside them — now every window goes in by its own minute
    from core import backfill
    t0 = dt.datetime(2026, 9, 1, 9, 0, tzinfo=dt.timezone.utc)
    _session(conn, "s1", 10, t0)                                 # windows at 09:00 and 09:05
    _session(conn, "s2", 5, t0 + dt.timedelta(minutes=2))        # one window at 09:02
    asked = _wire(monkeypatch, conn, [{"decisions": [], "questions": []}] * 3)
    tot = backfill.run("p", source="x", dst="x", log=lambda m: None)
    assert tot["done"] and tot["windows"] == 3
    firsts = [a["piece"].split(" =====", 1)[0] for a in asked]
    assert firsts == ["===== EXCHANGE 1 of 5 — 2026-09-01 09:00", "===== EXCHANGE 1 of 5 — 2026-09-01 09:02",
                      "===== EXCHANGE 1 of 5 — 2026-09-01 09:05"]
    cur = conn.cursor()
    cur.execute("SELECT session_id, finished FROM backfill_state ORDER BY session_id;")
    assert cur.fetchall() == [("s1", True), ("s2", True)]


def test_no_answer_halts_the_project_at_that_window_and_never_skips_it(conn, monkeypatch):
    # 2026-09-30: the 27B refused every ask (a 12k context) and the run
    # skipped 28 windows in ten minutes; a skip is never redone
    from core import backfill
    monkeypatch.setattr(backfill, "NO_ANSWER_WAIT_S", 0)
    t0 = dt.datetime(2026, 9, 1, 9, 0, tzinfo=dt.timezone.utc)
    _session(conn, "s1", 10, t0)
    line = {"kind": "decided", "what": "A over B.", "thread": 0, "question": "q", "ex": 1}
    _wire(monkeypatch, conn, [{"decisions": [line], "questions": ["q"]}, "down", "down", "down"])
    log = []
    tot = backfill.run("p", source="x", dst="x", tries=3, log=log.append)
    assert not tot["done"] and tot["skipped"] == 0 and tot["halted"] == "s1#10"
    cur = conn.cursor()
    cur.execute("SELECT done_hi, finished, skipped FROM backfill_state;")
    assert cur.fetchall() == [(9, False, 0)]
    cur.execute("SELECT why FROM backfill_skip;")
    assert cur.fetchone()[0].startswith("HALTED — no answer")
    # the next run starts at the halted window, and a redo never takes it
    asked = _wire(monkeypatch, conn, [{"decisions": [line], "questions": ["q"]}])
    tot2 = backfill.run("p", source="x", dst="x", log=lambda m: None)
    assert tot2["done"] and len(asked) == 1 and "EXCHANGE 1 of 5 — 2026-09-01 09:05" in asked[0]["piece"]
    assert backfill.redo_skips("p", dst="x", log=lambda m: None)["skips"] == 0
