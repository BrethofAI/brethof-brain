"""THE RETIREMENT SWEEP'S BOX SIDE, on a real Postgres (founder 2026-10-07:
"Yes, wire it in"). A save's retiring lines go into the project's queue; the
first drain of a project queues its existing retiring lines once; lines are
taken oldest first; the drain sends each line's records with the lines said
since they were written, deletes only what the hub names, copies every
delete to retire_log first, and never deletes a preference. The hub and the
embedder are faked; every SQL statement is real (BRAIN_TEST_DSN, as
scripts/ops/pt.sh sets it)."""
import datetime as dt
import os
import uuid

import pytest

DSN = os.environ.get("BRAIN_TEST_DSN", "")
pg = pytest.mark.skipif(not DSN, reason="BRAIN_TEST_DSN not set — the queue and the search are SQL")

VOCAB = ["graph", "cards", "embedder", "gemma", "hermes", "backup"]


def _vec(text):
    t = text.lower()
    return [1.0 if w in t else 0.0 for w in VOCAB] + [0.01] * (768 - len(VOCAB))


def _lit(v):
    return "[" + ",".join(f"{x:.4f}" for x in v) + "]"


@pytest.fixture
def conn(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    from core import embed, heal, store, tools
    # this machine's own box holds its backlog (deploy/selfhosted/.env); the
    # tests set what they test
    monkeypatch.setenv("BRAIN_RETIRE_BACKLOG", "on")
    monkeypatch.setenv("BRAIN_RETIRE", "on")
    name = "t_retire_" + uuid.uuid4().hex[:10]
    admin = psycopg.connect(DSN, autocommit=True)
    admin.execute(f"CREATE DATABASE {name}")
    c = psycopg.connect(DSN.replace("dbname=postgres", f"dbname={name}"))
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "core", "schema.sql"), encoding="utf-8") as fh:
        c.execute(fh.read().replace(":dim", "768"))
    from core import decisionline
    c.execute(decisionline.DDL)
    c.execute("INSERT INTO project (name) VALUES ('p');")
    c.commit()

    class _Conn:
        def __enter__(self):
            return c

        def __exit__(self, *a):
            c.commit()
    monkeypatch.setattr(store, "connection", lambda: _Conn())
    monkeypatch.setattr(store, "ensure_ddl", lambda cur, key, ddl: cur.execute(ddl))
    monkeypatch.setattr(embed, "embed", lambda texts, kind="document", **k: [_vec(t) for t in texts])

    def _del(project, rid):
        cur = c.cursor()
        cur.execute("DELETE FROM document WHERE project = %s AND record_id = %s;", [project, rid])
        return "Deleted." if cur.rowcount else "No record."
    monkeypatch.setattr(tools, "delete_memory", _del)
    monkeypatch.setattr(heal, "_current_body", lambda p, rid: (c.execute(
        "SELECT content FROM document WHERE project = %s AND record_id = %s;", [p, rid]).fetchone() or [None])[0])
    try:
        yield c
    finally:
        c.close()
        admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        admin.close()


def _record(c, rid, title, content, typ="fact", day="2026-09-01"):
    c.execute("INSERT INTO document (project, record_id, type, title, content, source, embedding, captured_at, "
              "updated_at) VALUES ('p', %s, %s, %s, %s, 'conversation', %s::halfvec, %s, %s);",
              [rid, typ, title, content, _lit(_vec(f"{title} {content}")), day, day])


def _line(c, day, kind, what, replaced="", session="s1", lo=0, things=()):
    return c.execute("INSERT INTO decision (project, day, kind, what, replaced, why, things, said, session_id, "
                     "turn_lo, turn_hi, embedding) VALUES ('p', %s, %s, %s, %s, '', %s, '', %s, %s, %s, "
                     "%s::halfvec) RETURNING id;",
                     [day, kind, what, replaced, list(things), session, lo, lo + 4, _lit(_vec(what))]).fetchone()[0]


@pg
def test_a_save_queues_only_its_retiring_lines(conn):
    from core import retire
    _line(conn, "2026-09-27", "dropped", "The old graph cards are removed", session="s1", lo=10)
    _line(conn, "2026-09-27", "decided", "Gemma stays the embedder", session="s1", lo=10)
    _line(conn, "2026-09-27", "decided", "Lines replace the graph", replaced="the old graph", session="s1", lo=10)
    _line(conn, "2026-09-27", "dropped", "Another window's drop", session="s1", lo=20)
    assert retire.enqueue_session(conn, "p", "s1", 10) == 2
    assert retire.enqueue_session(conn, "p", "s1", 10) == 0          # a line is queued once
    conn.commit()


@pg
def test_the_backlog_runs_once_and_lines_are_taken_oldest_first(conn):
    from core import retire
    late = _line(conn, "2026-09-27", "dropped", "Graph removed")
    early = _line(conn, "2026-08-01", "dropped", "Hermes backup retired")
    _line(conn, "2026-08-10", "tried", "Tried a thing")
    conn.commit()
    assert retire.backlog(conn, "p") == 2 and retire.backlog(conn, "p") == 0
    conn.commit()
    qid, line_id, attempts = retire._claim(conn, "p")
    assert line_id == early and attempts == 1
    assert retire._claim(conn, "p") is None                            # one running per project
    retire._done(conn, qid)
    assert retire._claim(conn, "p")[1] == late


@pg
def test_a_failed_line_backs_off_and_is_dropped_past_the_last_try(conn):
    from core import retire
    _line(conn, "2026-09-27", "dropped", "Graph removed")
    conn.commit()
    retire.backlog(conn, "p")
    conn.commit()
    qid, _, attempts = retire._claim(conn, "p")
    assert retire._fail(conn, qid, attempts, "hub down") is False
    assert retire._claim(conn, "p") is None                            # waiting out the backoff
    conn.execute("UPDATE retire_queue SET next_try = now() - interval '1 second';")
    assert retire._fail(conn, qid, retire.MAX_ATTEMPTS, "hub down") is True
    assert conn.execute("SELECT count(*) FROM retire_queue;").fetchone()[0] == 0


@pg
def test_the_drain_deletes_what_the_hub_names_logs_it_and_spares_a_preference(conn, monkeypatch):
    from core import judge, retire
    _record(conn, "cards", "Graph cards print every session", "The graph prints cards in the brief.")
    _record(conn, "pref", "Founder likes short graph cards", "Graph cards stay short.", typ="preference")
    _record(conn, "gemma", "Gemma is the embedder", "EmbeddingGemma serves embeddings.")
    _line(conn, "2026-09-20", "decided", "The graph cards get a dated head")     # said after the records
    line_id = _line(conn, "2026-09-27", "dropped", "The old graph and its cards are removed", things=["graph"])
    conn.commit()
    asked = []

    def fake_ask(op, body, timeout=None):
        asked.append((op, body))
        return {"answer": {"ops": [{"id": "cards", "op": "delete", "reason": "the cards are gone"},
                                   {"id": "pref", "op": "delete", "reason": "cards gone"}]}}
    monkeypatch.setattr(judge, "ask", fake_ask)
    out = retire.drain("t", "p")
    assert out["lines"] == 1 and out["deleted"] == 1 and out["refused"] == 1 and out["depth"] == 0
    op, body = asked[0]
    assert op == "retire_judge" and body["line"]["what"].startswith("The old graph")
    sent = {r["id"]: r for r in body["records"]}
    assert {"cards", "pref"} <= set(sent)
    assert any("dated head" in s["what"] for s in sent["cards"]["since"])       # what was said since
    left = {r[0] for r in conn.execute("SELECT record_id FROM document;")}
    assert left == {"pref", "gemma"}
    logged = conn.execute("SELECT record_id, content, line_id, reason FROM retire_log;").fetchall()
    assert logged == [("cards", "The graph prints cards in the brief.", line_id, "the cards are gone")]


@pg
def test_a_hub_that_says_wait_stops_the_drain_without_spending_a_try(conn, monkeypatch):
    from core import judge, retire
    _record(conn, "cards", "Graph cards", "The graph prints cards.")
    _line(conn, "2026-09-27", "dropped", "The old graph is removed")
    _line(conn, "2026-09-28", "dropped", "The graph cards are removed")
    conn.commit()

    def window_closed(op, body, timeout=None):
        raise judge.NoAnswer("provider window closed", retry_s=120)
    monkeypatch.setattr(judge, "ask", window_closed)
    out = retire.drain("t", "p")
    assert out["failed"] == 1 and out["lines"] == 0
    rows = conn.execute("SELECT attempts, status, next_try > now() FROM retire_queue ORDER BY line_day;").fetchall()
    assert rows[0] == (0, "pending", True) and rows[1][1] == "pending"


@pg
def test_a_held_backlog_queues_nothing_but_new_lines_still_go(conn, monkeypatch):
    from core import retire
    monkeypatch.setenv("BRAIN_RETIRE_BACKLOG", "off")
    _line(conn, "2026-08-01", "dropped", "An old drop", session="s0", lo=0)
    _line(conn, "2026-09-27", "dropped", "A new drop", session="s9", lo=40)
    conn.commit()
    assert retire.backlog(conn, "p") == 0 and retire._needs(conn) == []
    assert retire.enqueue_session(conn, "p", "s9", 40) == 1 and retire._needs(conn) == ["p"]


@pg
def test_a_line_judges_only_records_written_before_it(conn):
    """Found live 2026-10-07: records written later the same day were judged
    by an earlier line and deleted."""
    from core import retire
    _record(conn, "old", "Graph cards print", "The graph prints cards.", day="2026-09-20")
    conn.execute("INSERT INTO document (project, record_id, type, title, content, source, embedding, captured_at, "
                 "updated_at) VALUES ('p', 'later', 'fact', 'Graph cards are gone', 'The graph prints no cards.', "
                 "'conversation', %s::halfvec, '2026-09-27', '2026-09-27 18:00+00');", [_lit(_vec("graph cards"))])
    lid = _line(conn, "2026-09-27", "dropped", "The graph cards are removed", things=["graph"])
    conn.execute("UPDATE decision SET at = '2026-09-27 12:00+00' WHERE id = %s;", [lid])
    conn.commit()
    line = retire._line(conn, lid)
    assert {r["id"] for r in retire.candidates(conn.cursor(), "p", line)} == {"old"}       # live: by the moment
    conn.execute("UPDATE decision SET at = '2026-10-05 08:00+00' WHERE id = %s;", [lid])      # a replayed line
    line = retire._line(conn, lid)
    assert {r["id"] for r in retire.candidates(conn.cursor(), "p", line)} == {"old"}       # backlog: earlier days


@pg
def test_a_fresh_memory_without_a_decision_table_needs_nothing(conn):
    """Found 2026-11-17 on the first local-first install: the sweep ran two
    minutes after the first start, before any line existed, and raised
    UndefinedTable on decision. A fresh memory has nothing to sweep."""
    from core import retire
    conn.execute("DROP TABLE decision CASCADE;")
    conn.commit()
    assert retire._needs(conn) == []
