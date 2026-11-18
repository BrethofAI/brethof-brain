"""The door's proposer (2026-09-10): one call per session says which project
the session looks like; the owner decides with move_session or
dismiss_proposal. Pure pieces here; the row's life on a scratch Postgres."""
import os
import uuid

import pytest

from core import proposer


def test_render_lists_the_projects_with_their_lines_and_the_head():
    from hub import judgetext
    text = judgetext.proposal("brain", [("brain", "the memory product"), ("estate", "")], "hello there")
    assert text.startswith("FILED UNDER: brain") and "- brain: the memory product" in text and "- estate\n" in text
    assert "THE SESSION'S FIRST TURNS:\nhello there" in text


def test_parse_keeps_only_a_listed_project_and_clamps_the_confidence():
    projects = [("brain", ""), ("estate", "")]
    assert proposer.parse('{"project": "estate", "confidence": 0.9, "why": "adm and the sentinel"}', projects, "brain") == \
        {"project": "estate", "confidence": 0.9, "why": "adm and the sentinel"}
    # an invented project is the filed one; a wild confidence is clamped
    assert proposer.parse('{"project": "ops", "confidence": 7, "why": "x"}', projects, "brain")["project"] == "brain"
    assert proposer.parse('{"project": "ops", "confidence": 7, "why": "x"}', projects, "brain")["confidence"] == 1.0
    assert proposer.parse("not json at all", projects, "brain") is None
    assert proposer.parse("", projects, "brain") is None


def test_the_prompt_text_is_licensed_and_names_the_answer_shape():
    from core.prompts import TEXTS
    from hub import judgetext
    t = TEXTS["proposer"]
    # the filled system text carries today's date and the answer shape with real braces
    filled = judgetext.proposer_system()
    assert "{today}" not in filled and '{"project": "<name from the list>"' in filled
    assert "{{" not in t                            # single braces: the caller replaces, never formats
    assert "{today}" in t and '"project"' in t and '"confidence"' in t and "Never invent a project" in t
    assert judgetext.proposal("brain", [("brain", "the memory product"), ("estate", "")], "hello").startswith("FILED UNDER: brain")


psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("BRAIN_TEST_DSN", "")
pg = pytest.mark.skipif(not DSN, reason="BRAIN_TEST_DSN not set")

DDL = """
CREATE TABLE project (name text PRIMARY KEY);
INSERT INTO project VALUES ('brain'), ('estate');
CREATE TABLE chat (id bigserial PRIMARY KEY, project text, session_id text, turn_index int, role text, text text,
                   ts timestamptz NOT NULL DEFAULT now(), UNIQUE (session_id, turn_index));
"""


@pytest.fixture
def conn():
    name = "t_proposer_" + uuid.uuid4().hex[:10]
    admin = psycopg.connect(DSN, autocommit=True)
    admin.execute(f"CREATE DATABASE {name}")
    c = psycopg.connect(DSN.replace("dbname=postgres", f"dbname={name}"))
    c.execute(DDL)
    c.commit()
    try:
        yield c
    finally:
        c.close()
        admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")  # a stray connection must not leak the scratch tenant (93 found 2026-09-10)
        admin.close()


def _turns(conn, sid, n, project="brain"):
    cur = conn.cursor()
    for i in range(n):
        cur.execute("INSERT INTO chat (project, session_id, turn_index, role, text) VALUES (%s,%s,%s,%s,%s)",
                    [project, sid, i, "user" if i % 2 == 0 else "assistant", f"turn {i} about adm and the sentinel"])
    conn.commit()


@pg
def test_due_once_after_enough_turns_and_the_rows_life(conn):
    cur = conn.cursor()
    _turns(conn, "s1", 4)
    assert not proposer.due(cur, "s1")                        # too early
    _turns(conn, "s1", 0)
    cur.execute("INSERT INTO chat (project, session_id, turn_index, role, text) VALUES ('brain','s1',4,'user','more'), ('brain','s1',5,'assistant','ok')")
    conn.commit()
    assert proposer.due(cur, "s1")
    head = proposer.head_text(cur, "s1")
    assert head.startswith("user: turn 0 about adm") and "assistant: ok" in head
    # the row: a proposal elsewhere shows in the brief and in the prompt line, three times
    cur.execute("INSERT INTO session_proposal (session_id, filed_project, proposed_project, confidence, why) "
                "VALUES ('s1','brain','estate',0.85,'the turns are about adm and the sentinel')")
    conn.commit()
    assert not proposer.due(cur, "s1")                        # once per session
    rows = proposer.pending(cur, "brain")
    assert len(rows) == 1 and rows[0]["project"] == "estate"
    assert "move_session('s1', 'estate'" in proposer.brief_lines(cur, "brain")
    seen = [proposer.prompt_line(cur, "s1") for _ in range(proposer.SHOW_TIMES + 1)]
    assert all("LOOKS LIKE PROJECT 'estate'" in x for x in seen[:proposer.SHOW_TIMES]) and seen[-1] == ""
    # a low-confidence or same-project proposal never shows
    cur.execute("INSERT INTO session_proposal (session_id, filed_project, proposed_project, confidence, why) "
                "VALUES ('s2','brain','estate',0.3,'weak'), ('s3','brain','brain',0.99,'itself')")
    conn.commit()
    assert proposer.prompt_line(cur, "s2") == "" and proposer.prompt_line(cur, "s3") == ""
    assert [r["session_id"] for r in proposer.pending(cur, "brain")] == ["s1"]
    # decided: gone from both
    assert proposer.decide(cur, "s1", "dismissed")
    assert not proposer.decide(cur, "s1", "again")
    assert proposer.pending(cur, "brain") == [] and proposer.brief_lines(cur, "brain") == ""


@pg
def test_propose_asks_once_and_writes_what_the_reader_said(conn, monkeypatch):
    from core import judge, store
    _turns(conn, "s9", 8)
    monkeypatch.setattr(store, "bind", lambda t: None)
    monkeypatch.setattr(store, "unbind", lambda tok: None)

    class _Ctx:
        def __enter__(self): return conn
        def __exit__(self, *a):
            conn.commit()
    monkeypatch.setattr(store, "connection", lambda: _Ctx())
    monkeypatch.setattr(proposer, "project_names", lambda cur: ["brain", "estate"])
    from core import metering
    monkeypatch.setattr(metering, "record", lambda *a, **k: None)
    calls = []

    def ask(op, body, timeout=None):
        calls.append((op, body))
        return {"answer": {"project": "estate", "confidence": 0.8, "why": "adm and the sentinel"},
                "fault": "", "input_tokens": 10, "output_tokens": 5}
    monkeypatch.setattr(judge, "ask", ask)
    r = proposer.propose("t", "brain", "s9")
    assert r["project"] == "estate" and r["confidence"] == 0.8 and r["filed"] == "brain"
    assert len(calls) == 1 and calls[0][0] == "propose" and calls[0][1]["filed"] == "brain"
    assert calls[0][1]["projects"] == ["brain", "estate"] and calls[0][1]["text"]
    assert proposer.propose("t", "brain", "s9") is None and len(calls) == 1   # once
    cur = conn.cursor()
    cur.execute("SELECT proposed_project, confidence FROM session_proposal WHERE session_id='s9'")
    assert cur.fetchone() == ("estate", 0.8)


def test_the_proposer_is_judged_on_the_hub_under_heals_allowance():
    src = open("core/proposer.py", encoding="utf-8").read()
    assert 'judge.ask("propose"' in src and 'metering.record("heal"' in src and 'metering.record("graph"' not in src
    hub = open("hub/judge.py", encoding="utf-8").read()
    prop = hub.split("async def propose(", 1)[1].split("\nOPS", 1)[0]
    assert "_heal_gate(" in prop and '"heal"' in prop
