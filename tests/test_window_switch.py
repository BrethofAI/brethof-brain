"""THE SWITCH (founder 2026-09-24/25/26): the box keeps a session's pieces
and sends a WINDOW of five; the hub judges it with TWO calls — the records
call with the ledger, the graph-lines call (V6) with the threads view — and
one save carries records, ledger rows and decision lines back, each line
filed under ITS exchange. The box side is SQL and needs a real Postgres
(BRAIN_TEST_DSN, as scripts/ops/pt.sh sets it); the hub side runs on fakes."""
import asyncio
import datetime as dt
import json
import os
import uuid

import pytest

from hub import judge

DSN = os.environ.get("BRAIN_TEST_DSN", "")
pg = pytest.mark.skipif(not DSN, reason="BRAIN_TEST_DSN not set — the window lives in SQL")


class Doors:
    """The hub's doors, faked: every call answered with the text given per job."""
    WINDOW_CLOSE_S = 300

    def __init__(self, answers: dict, graph=True):
        self.calls, self.metered, self.answers, self._graph = [], [], answers, graph

    async def infer(self, tenant, body, job="generate"):
        self.calls.append((job, body))
        a = self.answers[job]
        text = a.pop(0) if isinstance(a, list) else a
        return {"ok": True, "text": text, "input_tokens": 100, "output_tokens": 20}

    def meter(self, tid, op, i, o):
        self.metered.append(op)

    def window_closed(self):
        return 0

    def graph_allowed(self, tenant):
        return self._graph

    async def over_cap(self, tenant):
        return False

    async def heal_over_allowance(self, tenant):
        return False


T = {"id": "t1", "name": "box"}
WINDOW = {"project": "p", "piece": "===== EXCHANGE 1 of 2 — 2026-09-26 10:00 =====\n\n[user] hi\n\n[assistant] yes\n\n"
                                  "===== EXCHANGE 2 of 2 — 2026-09-26 10:05 =====\n\n[user] go\n\n[assistant] done",
          "threads": [{"n": 1, "id": 41, "question": "backup tool", "day": "2026-09-20", "kind": "decided",
                       "what": "Kopia replaced Borg."}],
          "near_lines": [{"id": 40, "n": 1, "kind": "rejected", "day": "2026-09-18", "what": "Borg stays for the VPS."}],
          "records_so_far": [{"title": "Backups run nightly", "content": "at 03:00"}],
          "next": "and what about the cache?", "n": 2}
REC = json.dumps({"notes": ["1: x", "2: y"], "records": [{"type": "fact", "title": "T", "content": "C", "ex": 2}],
                  "ledger": [{"content": "we will add retries next week"}]})
LINES = json.dumps({"notes": [], "decisions": [{"kind": "decided", "what": "Retries are added next week.", "thread": 1,
                                                "how": "continues", "ex": 2}], "questions": ["q1"]})



@pytest.fixture(autouse=True)
def _bound_tenant(monkeypatch):
    """Production binds the tenant before any window code runs (hubfeed's
    _run/_push/_flush_due, the pipe's commands); the DDL door needs it."""
    from core import store
    monkeypatch.setattr(store, "current_tenant", lambda: "t_window")
    for k in ("hub_pending", "decision"):
        store._ddl_ensured.discard(("t_window", k))


def test_the_window_is_judged_with_two_calls_under_the_production_pair():
    d = Doors({"curate": REC, "graph": LINES})
    out = asyncio.run(judge.curate_window(d, T, "the project", WINDOW, rules=["one rule line"], names=["p", "q"]))
    assert out["ok"] and [j for j, _ in d.calls] == ["curate", "graph"]
    rec, lines = d.calls[0][1], d.calls[1][1]
    assert "THEN THE LEDGER" in rec["system"] and "one rule line" in rec["system"]
    assert "RECORDS THIS SESSION HAS WRITTEN" in rec["prompt"] and "Backups run nightly" in rec["prompt"]
    assert "THE NEXT MESSAGE" in rec["prompt"] and "what about the cache" in rec["prompt"]
    # production lines text kC2 (2026-09-29): the choice, never the doing
    assert "the line is the choice" in " ".join(lines["system"].split()) and "FIRST, TAKE STOCK" in lines["system"]
    assert "LINES NEAREST THIS PIECE, when given" in lines["system"]
    assert "THREADS — questions of this project so far" in lines["prompt"] and "Kopia replaced Borg" in lines["prompt"]
    assert "LINES NEAREST THIS PIECE" in lines["prompt"] and "Borg stays for the VPS" in lines["prompt"]
    assert rec["json"] is True and lines["max_tokens"] == 20000 and rec["max_tokens"] == 12000
    got = out["answer"]
    assert got["records"][0]["title"] == "T" and got["ledger"][0]["content"].startswith("we will")
    assert got["decisions"][0]["thread"] == 1 and got["questions"] == ["q1"]
    assert out["input_tokens"] == 200 and got["failed"] == [] and d.metered == []   # the units are the door's


def test_without_the_graph_add_on_only_the_records_call_is_made():
    d = Doors({"curate": REC, "graph": LINES}, graph=False)
    out = asyncio.run(judge.curate_window(d, T, "", WINDOW))
    assert [j for j, _ in d.calls] == ["curate"] and out["answer"]["decisions"] == [] and out["answer"]["records"]


def test_a_lines_answer_without_decisions_is_asked_once_more_then_named():
    d = Doors({"curate": REC, "graph": ['{"notes": [{"ex": 860}]}', "not json at all {"]})
    out = asyncio.run(judge.curate_window(d, T, "", WINDOW))
    assert [j for j, _ in d.calls] == ["curate", "graph", "graph"]
    assert out["answer"]["decisions"] == [] and out["answer"]["failed"][0].startswith("lines:")
    d = Doors({"curate": REC, "graph": ['{"notes": []}', LINES]})
    out = asyncio.run(judge.curate_window(d, T, "", WINDOW))
    assert len(out["answer"]["decisions"]) == 1 and out["answer"]["failed"] == []


def test_the_hub_pushes_one_save_with_lines_view_and_ranges(monkeypatch):
    from hub import server
    pushed, metered = [], []

    async def rules(tenant, project):
        return "the purpose", ["a line"]

    async def names(tenant):
        return ["p"]

    async def cw(hub, tenant, purpose, body, rules=None, names=()):
        assert purpose == "the purpose" and rules == ["a line"] and body["n"] == 2
        return {"ok": True, "answer": {"records": [{"title": "T", "content": "C"}], "ledger": [{"content": "we will"}],
                "decisions": [{"kind": "tried", "what": "x", "ex": 1}], "questions": [], "failed": ["lines: x"]}}

    async def fetchone(q, a):
        return None

    async def push(tenant, body):
        pushed.append(body)

    monkeypatch.setattr(server, "_bearing_rules_shared", rules)
    monkeypatch.setattr(server, "_project_names", names)
    monkeypatch.setattr(server.judge, "curate_window", cw)
    monkeypatch.setattr(server.store, "fetchone", fetchone)
    monkeypatch.setattr(server, "push_save", push)
    monkeypatch.setattr(server, "meter", lambda tid, op, i, o, count=1: metered.append((op, count)))
    w = {"ranges": [[3, 5], [6, 9]], "days": ["2026-09-26 10:00", "2026-09-26 10:05"],
         "threads": [{"n": 1, "id": 41}], "records_so_far": [], "next": "", "n": 2}
    asyncio.run(server.process_window(T, "p", "s1", "the text", 3, 9, w))
    assert metered == [("curate", 2)]
    body = pushed[0]
    assert body["decisions"] and body["threads"] == [{"n": 1, "id": 41}] and body["ranges"] == [[3, 5], [6, 9]]
    assert body["records"] and body["ledger"] and body["moved"] == [] and body["turn_lo"] == 3


def _hold_setup(monkeypatch, answers):
    """process_window against a fake judge that answers from `answers` in turn."""
    from hub import server
    pushed, metered, slept, logs = [], [], [], []

    async def rules(tenant, project):
        return "the purpose", []

    async def names(tenant):
        return []

    async def cw(hub, tenant, purpose, body, rules=None, names=()):
        return answers.pop(0)

    async def fetchone(q, a):
        return None

    async def push(tenant, body):
        pushed.append(body)

    async def sleep(s):
        slept.append(s)

    monkeypatch.setattr(server, "_bearing_rules_shared", rules)
    monkeypatch.setattr(server, "_project_names", names)
    monkeypatch.setattr(server.judge, "curate_window", cw)
    monkeypatch.setattr(server.store, "fetchone", fetchone)
    monkeypatch.setattr(server, "push_save", push)
    monkeypatch.setattr(server, "meter", lambda tid, op, i, o, count=1: metered.append((op, count)))
    monkeypatch.setattr(server.asyncio, "sleep", sleep)
    monkeypatch.setattr(server, "log", logs.append)
    return server, pushed, metered, slept, logs


W1 = {"ranges": [[1, 2]], "days": ["2026-10-03 10:00"], "threads": [], "records_so_far": [], "next": "", "n": 1}
OK = {"ok": True, "answer": {"records": [{"title": "T", "content": "C"}], "ledger": [], "decisions": [], "questions": []}}


def test_a_provider_outage_holds_the_window_and_curates_it_when_the_provider_answers(monkeypatch):
    # 2026-09-30: a provider outage dropped a live window for good
    server, pushed, metered, slept, logs = _hold_setup(
        monkeypatch, [{"ok": False, "why": "provider"}, {"ok": False, "why": "provider"}, OK])
    asyncio.run(server.process_window(T, "p", "s1", "the text", 1, 2, W1))
    assert slept == [60, 120]                       # held, asked again, longer each time
    assert len(pushed) == 1 and pushed[0]["records"]
    assert metered == [("curate", 1)]                # counted once, not per attempt
    assert not any("DROPPED" in l for l in logs)


def test_a_window_held_past_the_limit_is_dropped_loudly(monkeypatch):
    server, pushed, metered, slept, logs = _hold_setup(monkeypatch, [{"ok": False, "why": "provider"}] * 50)
    monkeypatch.setattr(server, "HOLD_WINDOW_S", 0)
    asyncio.run(server.process_window(T, "p", "s1", "the text", 1, 2, W1))
    assert pushed == [] and any("DROPPED uncurated (provider" in l for l in logs)


def test_a_closed_provider_window_waits_at_least_what_the_provider_asked(monkeypatch):
    server, pushed, metered, slept, logs = _hold_setup(
        monkeypatch, [{"ok": False, "why": "provider window closed", "retry_s": 240}, OK])
    asyncio.run(server.process_window(T, "p", "s1", "the text", 1, 2, W1))
    assert slept == [240] and len(pushed) == 1


# ── the box side, on a real Postgres ────────────────────────────────────────
@pytest.fixture
def conn():
    psycopg = pytest.importorskip("psycopg")
    name = "t_window_" + uuid.uuid4().hex[:10]
    admin = psycopg.connect(DSN, autocommit=True)
    admin.execute(f"CREATE DATABASE {name}")
    c = psycopg.connect(DSN.replace("dbname=postgres", f"dbname={name}"))
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "core", "schema.sql"), encoding="utf-8") as fh:
        c.execute(fh.read().replace(":dim", "768"))
    from core import decisionline, hubwindow
    c.execute(hubwindow.DDL)
    c.execute(decisionline.DDL)
    c.execute("INSERT INTO project (name) VALUES ('p');")
    c.commit()
    try:
        yield c
    finally:
        c.close()
        admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        admin.close()


def _chat(conn, sid, rows):
    cur = conn.cursor()
    for i, (role, text, ts) in enumerate(rows):
        cur.execute("INSERT INTO chat (project, session_id, turn_index, role, text, ts) VALUES (%s, %s, %s, %s, %s, %s);",
                    ["p", sid, i, role, text, ts])
    conn.commit()


@pg
def test_pieces_wait_in_the_window_and_leave_oldest_first(conn):
    from core import hubwindow
    cur = conn.cursor()
    n = hubwindow.add(cur, "p", "s1", [("[user] a\n\n[assistant] b", 0, 1), ("[user] c\n\n[assistant] d", 2, 3)])
    assert n == 2
    n = hubwindow.add(cur, "p", "s2", [("[user] other session", 0, 0)])
    assert n == 1
    rows = hubwindow.take(cur, "p", "s1", 1)
    assert len(rows) == 1 and rows[0][2] == 0                      # the oldest went
    cur.execute("SELECT turn_lo FROM hub_pending WHERE session_id = 's1';")
    assert [r[0] for r in cur.fetchall()] == [2]                    # the other stayed
    big = "x" * 100000
    hubwindow.add(cur, "p", "s3", [(big, 5, 5)])
    cur.execute("SELECT length(piece) FROM hub_pending WHERE session_id = 's3';")
    assert cur.fetchone()[0] < 41000                                # cut like the bench's pieces


@pg
def test_a_window_is_due_only_at_five_pieces_however_old_or_quiet(conn):
    from core import hubwindow
    cur = conn.cursor()
    now = dt.datetime(2026, 9, 26, 12, 0, tzinfo=dt.timezone.utc)
    fresh = now - dt.timedelta(seconds=10)
    for sid, ts_list in {"full": [fresh] * 5, "old": [now - dt.timedelta(days=2), fresh],
                         "quiet": [now - dt.timedelta(hours=5)], "young": [fresh, fresh]}.items():
        for ts in ts_list:
            cur.execute("INSERT INTO hub_pending (project, session_id, piece, turn_lo, turn_hi, ts) "
                        "VALUES ('p', %s, 'x', 0, 0, %s);", [sid, ts])
    assert sorted(s for _, s in hubwindow.due(cur, now)) == ["full"]


@pg
def test_the_window_body_carries_headers_ranges_days_records_and_the_view(conn, monkeypatch):
    from core import embed, hubwindow
    monkeypatch.setattr(embed, "embed", lambda texts, kind="": [[0.0] * 768 for _ in texts])
    t0 = dt.datetime(2026, 9, 26, 9, 0, tzinfo=dt.timezone.utc)
    _chat(conn, "s1", [("user", "one", t0), ("assistant", "two", t0), ("user", "three", t0 + dt.timedelta(days=1)),
                       ("assistant", "four", t0 + dt.timedelta(days=1))])
    cur = conn.cursor()
    cur.execute("INSERT INTO document (project, record_id, type, title, content, meta) VALUES "
                "('p', 'r1', 'fact', 'Backups run nightly', 'at 03:00', %s), "
                "('p', 'r2', 'fact', 'Another session', 'x', %s);",
                [json.dumps({"origin": {"session_id": "s1", "turn_lo": 0, "turn_hi": 1}}),
                 json.dumps({"origin": {"session_id": "s9"}})])
    hubwindow.add(cur, "p", "s1", [("[user] one\n\n[assistant] two", 0, 1), ("[user] three\n\n[assistant] four", 2, 3)])
    rows = hubwindow.take(cur, "p", "s1", 5)
    body = hubwindow.build(cur, "p", "s1", rows, nxt="  and   next?  ")
    assert body["n"] == 2 and body["ranges"] == [[0, 1], [2, 3]] and body["turn_lo"] == 0 and body["turn_hi"] == 3
    assert body["piece"].startswith("===== EXCHANGE 1 of 2 — 2026-09-26 09:00 =====\n\n[user] one")
    assert "===== EXCHANGE 2 of 2 — 2026-09-27 09:00 =====" in body["piece"]
    assert body["days"] == ["2026-09-26 09:00", "2026-09-27 09:00"]
    # the session's own records only; r1 has no vector yet, so it always goes (the newest, just written)
    assert body["records_so_far"] == [{"title": "Backups run nightly", "content": "at 03:00", "id": "r1", "type": "fact"}]
    assert body["records_session"] == [{"title": "Backups run nightly", "content": "at 03:00", "id": "r1", "type": "fact"}]
    assert body["threads"] == [] and body["near_lines"] == [] and body["next"] == "and next?"
    cur.execute("SELECT count(*) FROM hub_pending;")
    assert cur.fetchone()[0] == 0


@pg
def test_a_line_is_filed_under_its_own_exchange_by_ex(conn, monkeypatch):
    from core import decisionline, embed
    monkeypatch.setattr(embed, "embed", lambda texts, kind="": [[0.0] * 768 for _ in texts])
    cur = conn.cursor()
    n = decisionline.write(cur, "p", [
        {"kind": "decided", "what": "Kopia replaces Borg.", "replaced": "Borg", "thread": 0, "question": "backup tool", "ex": 2},
        {"kind": "tried", "what": "Borg failed on the cloud target.", "thread": 0, "question": "backup tool", "ex": 1},
        {"kind": "decided", "what": "No ex at all.", "thread": 0, "question": "other"},
    ], "s1", 0, 9, dt.date(2026, 9, 26), "", threads=[], questions=["backup tool", "other"],
        ranges=[[0, 3], [4, 9]], days=["2026-09-25 10:00", "2026-09-26 11:00"])
    assert n == 3
    cur.execute("SELECT what, turn_lo, turn_hi, day FROM decision ORDER BY id;")
    rows = cur.fetchall()
    # written in exchange order: no ex (0) first, then ex 1, then ex 2
    assert [(r[1], r[2]) for r in rows] == [(0, 9), (0, 3), (4, 9)]       # no ex → the window; ex 1; ex 2
    assert [str(r[3]) for r in rows] == ["2026-09-26", "2026-09-25", "2026-09-26"]


@pg
def test_the_feed_keeps_pieces_and_sends_a_full_window_with_the_next_prompt(conn, monkeypatch):
    from core import embed, hubfeed, store
    monkeypatch.setattr(embed, "embed", lambda texts, kind="": [[0.0] * 768 for _ in texts])
    _chat(conn, "s1", [("user", f"u{i}", dt.datetime(2026, 9, 26, 9, i, tzinfo=dt.timezone.utc)) for i in range(12)])
    sent, threads = [], []

    class _Conn:
        def __enter__(self):
            return conn

        def __exit__(self, *a):
            conn.commit()

    monkeypatch.setattr(store, "connection", lambda: _Conn())
    monkeypatch.setattr(store, "bind", lambda t: None)
    monkeypatch.setattr(store, "unbind", lambda t: None)
    monkeypatch.setattr(hubfeed, "_tenant", lambda: "t1")
    monkeypatch.setattr(hubfeed, "HUB_KEY", "k")
    monkeypatch.setattr(hubfeed, "_send_window", lambda body: sent.append(body) or True)
    monkeypatch.setattr(hubfeed, "_ensure_flusher", lambda t: None)

    class _T:                       # threads run inline: the test reads the result at once
        def __init__(self, target=None, name="", daemon=True):
            self.t = target

        def start(self):
            self.t()

    monkeypatch.setattr(hubfeed.threading, "Thread", _T)
    for i in range(4):
        hubfeed.feed("p", "s1", [{"index": 2 * i, "role": "user", "text": f"u{i}"},
                                 {"index": 2 * i + 1, "role": "assistant", "text": "a"}])
    assert sent == []                                                  # four wait
    hubfeed.on_prompt("p", "s1", "the fifth prompt")
    assert sent == []                                                  # a prompt pushes only a full window
    hubfeed.feed("p", "s1", [{"index": 8, "role": "user", "text": "u4"}, {"index": 9, "role": "assistant", "text": "a"}])
    assert len(sent) == 1 and sent[0]["n"] == 5 and sent[0]["project"] == "p" and sent[0]["next"] == ""
    assert sent[0]["ranges"] == [[0, 1], [2, 3], [4, 5], [6, 7], [8, 9]] and "EXCHANGE 5 of 5" in sent[0]["piece"]
    hubfeed.feed("p", "s1", [{"index": 10, "role": "user", "text": "u5"}, {"index": 11, "role": "assistant", "text": "a"}])
    hubfeed.flush_session("p", "s1")                                   # a compact sends the one that waits
    assert len(sent) == 2 and sent[1]["n"] == 1 and sent[1]["ranges"] == [[10, 11]]
    # 2026-10-02, no timers: what a finished session left waiting goes at the next session's start,
    # oldest session first, and never the starting session's own
    hubfeed.feed("p", "s1", [{"index": 12, "role": "user", "text": "u6"}, {"index": 13, "role": "assistant", "text": "a"}])
    _chat(conn, "s0", [("user", "old", dt.datetime(2026, 9, 1, 9, 0, tzinfo=dt.timezone.utc))])
    conn.cursor().execute("INSERT INTO hub_pending (project, session_id, piece, turn_lo, turn_hi, ts) "
                          "VALUES ('p', 's0', '[user] old', 0, 0, '2026-09-01 09:00+00');")
    conn.commit()
    hubfeed.on_session_start("p", "s2")
    assert [b["session_id"] for b in sent[2:]] == ["s0", "s1"]
    sent.clear()
    hubfeed.feed("p", "s2", [{"index": 0, "role": "user", "text": "n"}, {"index": 1, "role": "assistant", "text": "a"}])
    hubfeed.on_session_start("p", "s2")                               # a resumed session keeps its own
    assert sent == []


def test_the_records_replay_asks_records_only_on_the_day_it_was_said():
    """2026-09-26, the records replay: a July window is judged in July (the
    box sends `today`), and the lines call is not made (the lines exist)."""
    d = Doors({"curate": REC, "graph": LINES})
    out = asyncio.run(judge.curate_window(d, T, "", dict(WINDOW, records_only=True, today="2026-07-28")))
    assert [j for j, _ in d.calls] == ["curate"] and out["answer"]["records"]
    assert "TODAY is 2026-07-28" in d.calls[0][1]["system"] or "2026-07-28" in d.calls[0][1]["system"]
    assert judge._day({"today": "not a day"}) == judge.J._today() and judge._day({}) == judge.J._today()


def test_heal_judges_on_the_replayed_day():
    from hub import judge as hj
    recs = [{"record_id": "a", "type": "fact", "title": "A", "content": "the box"}]
    d = Doors({"heal": '{"ops": []}'})
    asyncio.run(hj.heal_judge(d, T, "p", {"project": "brain", "records": recs, "candidates": [], "today": "2026-07-28"}))
    assert "TODAY is 2026-07-28" in d.calls[0][1]["system"]


def test_the_probe_renders_the_rules_it_is_handed_under_the_description():
    d = Doors({"graph": LINES})
    asyncio.run(judge.curate_probe(d, T, "the memory product", {"project": "p", "piece": "x", "prompt": "thV9",
                                                               "_rules": ["Decisions carry their why."]}))
    system = d.calls[0][1]["system"]
    assert "ABOUT THIS PROJECT: the memory product" in system and "Decisions carry their why." in system
    d = Doors({"graph": LINES})
    asyncio.run(judge.curate_probe(d, T, "", {"project": "p", "piece": "x", "prompt": "thV9"}))
    assert "(no description yet)" in d.calls[0][1]["system"] and "RULES THAT BEAR" not in d.calls[0][1]["system"]


def test_the_records_call_takes_only_a_named_variant_text():
    d = Doors({"curate": REC, "graph": LINES})
    asyncio.run(judge.curate_window(d, T, "", dict(WINDOW, records_only=True, records_prompt="nt")))
    assert "Nor the result of a" in d.calls[0][1]["system"] and "measurement" not in d.calls[0][1]["system"].split("JSON only")[-1]
    # an unknown name is an error (2026-09-29) — never something the caller
    # chose, and never production's text in silence either
    d = Doors({"curate": REC, "graph": LINES})
    with pytest.raises(KeyError):
        asyncio.run(judge.curate_window(d, T, "", dict(WINDOW, records_only=True, records_prompt="anything else")))
    assert not d.calls
    e = Doors({"curate": REC, "graph": LINES})
    asyncio.run(judge.curate_window(e, T, "", dict(WINDOW, records_only=True)))
    # production records text (C4's rules, E1's work order since 2026-09-29)
    assert "ONE SUBJECT, ONE RECORD" in e.calls[0][1]["system"] \
        and "still the story" in " ".join(e.calls[0][1]["system"].split())


@pg
def test_the_curator_and_the_check_get_the_records_nearest_the_window(conn, monkeypatch):
    """2026-10-02: the curator's list is the session's records nearest the window (plus any not yet
    embedded); the still-true check's list is the records nearest what the window wrote."""
    from core import embed, hubwindow
    from core.decisionline import _vec
    def one_hot(i):
        v = [0.0] * 768
        v[i] = 1.0
        return v
    monkeypatch.setattr(hubwindow, "SO_FAR_NEAR", 2)
    cur = conn.cursor()
    origin = json.dumps({"origin": {"session_id": "s1"}})
    for rid, axis in (("near_a", 1), ("near_b", 2), ("far", 9)):
        cur.execute("INSERT INTO document (project, record_id, type, title, content, meta, embedding) "
                    "VALUES ('p', %s, 'fact', %s, 'c', %s, %s::halfvec);", [rid, rid, origin, _vec(one_hot(axis))])
    cur.execute("INSERT INTO document (project, record_id, type, title, content, meta) "
                "VALUES ('p', 'fresh', 'fact', 'fresh', 'c', %s);", [origin])
    # the window's words point at axes 1 and 2: the two near records plus the unembedded one, never 'far'
    got = hubwindow._nearest(cur, "p", "s1", [one_hot(1), one_hot(2)], [])
    assert sorted(r["id"] for r in got) == ["fresh", "near_a", "near_b"]
    # no window vectors (embedder down): the newest go, as many as SO_FAR_FALLBACK allows
    assert hubwindow._nearest(cur, "p", "s1", [], [{"id": "x"}]) == [{"id": "x"}]
    # the check: what the window wrote points at axis 9 — 'far' is the one it can overturn
    monkeypatch.setattr(embed, "embed", lambda texts, kind="": [one_hot(9) for _ in texts])
    got = hubwindow.near_subjects(cur, "p", "s1", ["a new record about the far subject"], each=1, cap=25)
    assert [r["id"] for r in got] == ["far"]


@pg
def test_a_window_the_hub_never_received_goes_back_and_leaves_first(conn, monkeypatch):
    # 2026-10-03, lag-not-lose: the rows were taken out before the send, so an
    # unreachable hub used to lose the window for good
    from core import hubfeed, hubwindow, store
    cur = conn.cursor()
    for i in range(6):
        cur.execute("INSERT INTO hub_pending (project, session_id, piece, turn_lo, turn_hi) "
                    "VALUES ('p', 's1', %s, %s, %s);", [f"piece {i}", i, i])
    conn.commit()
    sent = []

    class C:
        def __enter__(self):
            return conn

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(store, "bind", lambda t: None)
    monkeypatch.setattr(store, "unbind", lambda t: None)
    monkeypatch.setattr(store, "connection", lambda: C())
    monkeypatch.setattr(hubwindow, "build", lambda cur, p, s, rows, nxt: {"n": len(rows), "turn_lo": rows[0][2]})
    monkeypatch.setattr(hubfeed, "_send_window", lambda body: sent.append(body) or False)
    assert hubfeed._push("t", "p", "s1") == 0              # the hub was not reached
    cur.execute("SELECT piece FROM hub_pending ORDER BY id;")
    assert [r[0] for r in cur.fetchall()] == [f"piece {i}" for i in range(6)]
    monkeypatch.setattr(hubfeed, "_send_window", lambda body: sent.append(body) or True)
    assert hubfeed._push("t", "p", "s1") == 5              # it goes again, oldest first
    assert sent[-1]["turn_lo"] == 0
    cur.execute("SELECT piece FROM hub_pending;")
    assert [r[0] for r in cur.fetchall()] == ["piece 5"]


def test_the_box_sends_a_5xx_window_again_and_a_4xx_never(monkeypatch):
    import urllib.error
    from core import hubfeed
    def boom(code):
        def post(req):
            raise urllib.error.HTTPError("u", code, "x", {}, None)
        return post
    monkeypatch.setattr(hubfeed, "_post", boom(503))
    assert hubfeed._send_window({"project": "p", "session_id": "s", "n": 5}) is False
    monkeypatch.setattr(hubfeed, "_post", boom(400))
    assert hubfeed._send_window({"project": "p", "session_id": "s", "n": 5}) is True   # a refusal is final
