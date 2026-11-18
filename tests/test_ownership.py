"""THE PROJECT-SPLIT DOOR (founder 2026-09-09): the archive never changes; an
ownership row says where a session or a turn range belongs now, every reader
consults it, and every derived row follows its source.

Real Postgres (BRAIN_TEST_DSN) for the SQL half; the interval resolution is
pure Python and runs everywhere."""
import os
import uuid

import pytest

from core import ownership

DSN = os.environ.get("BRAIN_TEST_DSN", "")


# ── the pure part: the latest row covering a turn wins ──────────────────────
def test_resolve_carves_a_later_subrange_out_of_an_earlier_whole_move():
    L, H = ownership.LO, ownership.HI
    got = ownership.resolve([("s", L, H, "estate"), ("s", 10, 20, "web")])
    assert got == {"s": [(L, 9, "estate"), (10, 20, "web"), (21, H, "estate")]}
    got = ownership.resolve([("s", 10, 20, "web"), ("s", L, H, "estate")])
    assert got == {"s": [(L, H, "estate")]}


def test_chat_where_is_the_plain_filter_when_nothing_moved():
    class Cur:
        def execute(self, *a): pass
        def fetchall(self): return []
    ownership.invalidate()
    assert ownership.chat_where(Cur(), "brain") == ("project = %s", ["brain"])
    assert ownership.chat_where(Cur(), ["brain", "estate"]) == ("project = ANY(%s)", [["brain", "estate"]])
    assert ownership.chat_where(Cur(), "") == ("TRUE", [])


def test_chat_where_excludes_what_moved_away_and_includes_what_moved_in():
    class Cur:
        def execute(self, *a): pass
        def fetchall(self):
            return [("s1", ownership.LO, ownership.HI, "estate"), ("s2", 5, 9, "brain")]
    ownership.invalidate()
    sql, params = ownership.chat_where(Cur(), "brain", alias="t.")
    assert sql == ("((t.project = %s AND NOT (t.session_id = %s)) OR "
                   "(t.session_id = %s AND t.turn_index BETWEEN %s AND %s))")
    assert params == ["brain", "s1", "s2", 5, 9]
    ownership.invalidate()
    sql, params = ownership.chat_where(Cur(), "estate")
    assert sql == ("((project = %s AND NOT ((session_id = %s AND turn_index BETWEEN %s AND %s))) "
                   "OR session_id = %s)")
    assert params == ["estate", "s2", 5, 9, "s1"]
    ownership.invalidate()


# ── the SQL part ─────────────────────────────────────────────────────────────
psycopg = pytest.importorskip("psycopg")
pg = pytest.mark.skipif(not DSN, reason="BRAIN_TEST_DSN not set — the move is SQL and needs a real Postgres")

DDL = """
CREATE TABLE project (name text PRIMARY KEY);
INSERT INTO project VALUES ('brain'), ('estate');
CREATE TABLE chat (id bigserial PRIMARY KEY, project text, session_id text, turn_index int, role text, text text,
                   ts timestamptz NOT NULL DEFAULT now(), embedding int, embed_skip boolean DEFAULT false,
                   UNIQUE (session_id, turn_index));
CREATE TABLE document (project text, record_id text, type text DEFAULT 'note', title text DEFAULT '', content text DEFAULT '',
                       meta jsonb NOT NULL DEFAULT '{}', updated_at timestamptz DEFAULT now(), UNIQUE (project, record_id));
CREATE TABLE ledger (id bigserial PRIMARY KEY, project text, content text, origin text DEFAULT '', turn_lo int DEFAULT -1,
                     turn_hi int DEFAULT -1, created_at timestamptz DEFAULT now());
CREATE TABLE note (project text, session_id text, title text DEFAULT '', body text, written_at timestamptz DEFAULT now(),
                   PRIMARY KEY (project, session_id));
"""

SEED = """
INSERT INTO chat (project, session_id, turn_index, role, text) VALUES
  ('brain', 'est', 0, 'user', 'adm sentinel work'), ('brain', 'est', 1, 'assistant', 'done on adm'),
  ('brain', 'est', 2, 'user', 'more'), ('brain', 'est', 3, 'assistant', 'ok'),
  ('brain', 'own', 0, 'user', 'the hub'), ('brain', 'own', 1, 'assistant', 'yes');
INSERT INTO document (project, record_id, meta) VALUES
  ('brain', 'adm_thing', '{"origin": {"session_id": "est", "turn_lo": 0, "turn_hi": 1}}'),
  ('brain', 'later_thing', '{"origin": {"session_id": "est", "turn_lo": 2, "turn_hi": 3}}'),
  ('brain', 'hub_thing', '{"origin": {"session_id": "own", "turn_lo": 0, "turn_hi": 1}}');
INSERT INTO ledger (project, content, origin, turn_lo, turn_hi) VALUES
  ('brain', 'fix the doctor', 'est', 2, 3), ('brain', 'ship hub', 'own', 0, 1);
INSERT INTO note (project, session_id, body) VALUES ('brain', 'est', 'estate handover');
"""


@pytest.fixture
def conn():
    name = "t_ownership_" + uuid.uuid4().hex[:10]
    admin = psycopg.connect(DSN, autocommit=True)
    admin.execute(f"CREATE DATABASE {name}")
    c = psycopg.connect(DSN.replace("dbname=postgres", f"dbname={name}"))
    c.execute(DDL)
    c.execute(SEED)
    c.commit()
    ownership.invalidate()
    try:
        yield c
    finally:
        c.close()
        admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")  # a stray connection must not leak the scratch tenant (93 found 2026-09-10)
        admin.close()
        ownership.invalidate()


def q(conn, sql, args=None):
    cur = conn.cursor()
    cur.execute(sql, args or [])
    return cur.fetchall()


@pg
def test_a_whole_session_moves_and_every_derived_row_follows(conn):
    log: list = []
    r = ownership.move(conn, "est", "estate", who="test", why="estate work", log=log)
    conn.commit()
    assert r["from"] == "brain" and r["turns"] == 4 and r["whole"]
    assert r["records"] == 2 and r["ledger"] == 1 and r["notes"] == 1
    # the old inventory graph's rows no longer travel (it left 2026-09-27)
    assert not {"mentions", "edges", "batches", "nodes_created", "nodes_removed"} & set(r)
    # the archive itself is untouched
    assert q(conn, "SELECT count(*) FROM chat WHERE project = 'brain'")[0][0] == 6
    # readers see the move
    cur = conn.cursor()
    w, a = ownership.chat_where(cur, "estate")
    assert sorted(x[0] for x in q(conn, f"SELECT session_id FROM chat WHERE {w}", a)) == ["est"] * 4
    w, a = ownership.chat_where(cur, "brain")
    assert sorted(x[0] for x in q(conn, f"SELECT session_id FROM chat WHERE {w}", a)) == ["own", "own"]
    assert ownership.effective_project(cur, "est", 3) == "estate"
    assert ownership.effective_project(cur, "own", 0) == ""
    # records, ledger, note
    assert q(conn, "SELECT project FROM document WHERE record_id = 'adm_thing'") == [("estate",)]
    assert q(conn, "SELECT project FROM document WHERE record_id = 'hub_thing'") == [("brain",)]
    assert q(conn, "SELECT project FROM ledger WHERE content = 'fix the doctor'") == [("estate",)]
    assert q(conn, "SELECT project FROM note WHERE session_id = 'est'") == [("estate",)]
    assert q(conn, "SELECT project FROM ledger WHERE content = 'ship hub'") == [("brain",)]


@pg
def test_a_turn_range_moves_and_the_rest_stays(conn):
    r = ownership.move(conn, "est", "estate", 2, 3, who="test")
    conn.commit()
    assert r["turns"] == 2 and not r["whole"]
    assert r["records"] == 1 and r["ledger"] == 1
    assert "notes" not in r                                      # a ranged move leaves the note
    cur = conn.cursor()
    w, a = ownership.chat_where(cur, "brain")
    assert sorted(x[1] for x in q(conn, f"SELECT session_id, turn_index FROM chat WHERE {w} AND session_id = 'est'", a)) == [0, 1]
    w, a = ownership.chat_where(cur, "estate")
    assert sorted(x[1] for x in q(conn, f"SELECT session_id, turn_index FROM chat WHERE {w}", a)) == [2, 3]
    assert q(conn, "SELECT project FROM document WHERE record_id = 'later_thing'") == [("estate",)]
    assert q(conn, "SELECT project FROM document WHERE record_id = 'adm_thing'") == [("brain",)]


@pg
def test_moving_back_is_another_row_and_the_readers_follow(conn):
    ownership.move(conn, "est", "estate", who="test")
    conn.commit()
    r = ownership.move(conn, "est", "brain", who="test", why="wrong call")
    conn.commit()
    assert r["from"] == "estate" and r["project"] == "brain"
    cur = conn.cursor()
    w, a = ownership.chat_where(cur, "brain")
    assert q(conn, f"SELECT count(*) FROM chat WHERE {w}", a)[0][0] == 6
    assert len(ownership.history(cur, "est")) == 2
    assert q(conn, "SELECT project FROM document WHERE record_id = 'adm_thing'") == [("brain",)]
    assert q(conn, "SELECT project FROM ledger WHERE content = 'fix the doctor'") == [("brain",)]


@pg
def test_a_bad_ask_is_refused_before_anything_is_written(conn):
    with pytest.raises(ValueError):
        ownership.move(conn, "est", "nowhere")
    with pytest.raises(ValueError):
        ownership.move(conn, "ghost", "estate")
    with pytest.raises(ValueError):
        ownership.move(conn, "est", "brain")               # already there, never moved
    conn.rollback()                      # what the tool does on a refusal
    ownership.ensure(conn.cursor())
    conn.commit()
    assert q(conn, "SELECT count(*) FROM session_owner")[0][0] == 0
    assert q(conn, "SELECT project FROM document WHERE record_id = 'adm_thing'") == [("brain",)]


@pg
def test_a_range_archived_under_two_projects_refiles_both_sides(conn):
    """2026-09-09: an imported blob's rows sat under two archived projects
    ('bv' rows interleaved in global's session); a ranged move must take
    the derived rows of EVERY archived project of the range, and `from`
    is the first turn's project, never the alphabetical first."""
    cur = conn.cursor()
    cur.execute("INSERT INTO project VALUES ('bv');")
    cur.execute("INSERT INTO chat (project, session_id, turn_index, role, text) VALUES "
                "('brain', 'mix', 0, 'user', 'a'), ('bv', 'mix', 1, 'assistant', 'b'), "
                "('brain', 'mix', 2, 'user', 'c'), ('bv', 'mix', 3, 'assistant', 'd');")
    cur.execute("INSERT INTO document (project, record_id, meta) VALUES "
                "('brain', 'mix_a', '{\"origin\": {\"session_id\": \"mix\", \"turn_lo\": 0, \"turn_hi\": 1}}'), "
                "('bv', 'mix_b', '{\"origin\": {\"session_id\": \"mix\", \"turn_lo\": 2, \"turn_hi\": 3}}');")
    cur.execute("INSERT INTO ledger (project, content, origin, turn_lo, turn_hi) VALUES "
                "('brain', 'row a', 'mix', 0, 1), ('bv', 'row b', 'mix', 1, 2), ('bv', 'row c', 'mix', 3, 3);")
    conn.commit()
    r = ownership.move(conn, "mix", "estate", 0, 3, who="test")
    conn.commit()
    assert r["from"] == "brain" and r["turns"] == 4
    assert r["records"] == 2 and r["ledger"] == 3
    assert q(conn, "SELECT project, count(*) FROM document WHERE record_id LIKE 'mix_%%' GROUP BY 1") == [("estate", 2)]
    assert q(conn, "SELECT project, count(*) FROM ledger WHERE origin = 'mix' GROUP BY 1") == [("estate", 3)]
    w, a = ownership.chat_where(cur, "bv")
    assert q(conn, f"SELECT count(*) FROM chat WHERE {w} AND session_id = 'mix'", a) == [(0,)]
    w, a = ownership.chat_where(cur, "estate")
    assert q(conn, f"SELECT count(*) FROM chat WHERE {w} AND session_id = 'mix'", a) == [(4,)]


LINES = """
CREATE TABLE decision (id bigserial PRIMARY KEY, project text NOT NULL, kind text NOT NULL DEFAULT 'decided',
                       what text NOT NULL DEFAULT '', session_id text NOT NULL DEFAULT '', turn_lo int NOT NULL DEFAULT -1,
                       turn_hi int NOT NULL DEFAULT -1, thread_id bigint);
CREATE TABLE decision_thread (id bigserial PRIMARY KEY, project text NOT NULL, question text NOT NULL, day date,
                              last_line bigint, n_lines int NOT NULL DEFAULT 0);
INSERT INTO decision_thread (id, project, question) VALUES (1, 'brain', 'how the sentinel runs'), (2, 'brain', 'the hub');
SELECT setval('decision_thread_id_seq', 2);
INSERT INTO decision (project, what, session_id, turn_lo, turn_hi, thread_id) VALUES
  ('brain', 'sentinel on adm', 'est', 0, 1, 1), ('brain', 'sentinel muted', 'est', 2, 3, 1),
  ('brain', 'hub first', 'own', 0, 1, 2);
"""


@pg
def test_a_moved_session_takes_its_decision_lines_and_their_thread(conn):
    # 2026-10-03, plan Next 5: the lines were left behind in the old project
    conn.execute(LINES); conn.commit()
    r = ownership.move(conn, "est", "estate", who="test")
    conn.commit()
    assert r["lines"] == 2
    assert q(conn, "SELECT project, count(*) FROM decision GROUP BY project ORDER BY 1") == [("brain", 1), ("estate", 2)]
    assert q(conn, "SELECT project FROM decision_thread WHERE id = 1") == [("estate",)]     # every line moved: so does the thread
    assert q(conn, "SELECT project FROM decision_thread WHERE id = 2") == [("brain",)]


@pg
def test_a_range_move_splits_a_thread_and_no_line_points_across_projects(conn):
    conn.execute(LINES); conn.commit()
    log: list = []
    r = ownership.move(conn, "est", "estate", 2, 3, who="test", log=log)
    conn.commit()
    assert r["lines"] == 1
    assert q(conn, "SELECT project, thread_id FROM decision WHERE what = 'sentinel on adm'") == [("brain", 1)]
    (proj, tid), = q(conn, "SELECT project, thread_id FROM decision WHERE what = 'sentinel muted'")
    assert proj == "estate" and tid != 1
    assert q(conn, "SELECT project, question, n_lines FROM decision_thread WHERE id = %s", [tid]) == \
        [("estate", "how the sentinel runs", 1)]
    assert q(conn, "SELECT project, n_lines FROM decision_thread WHERE id = 1") == [("brain", 1)]
    assert q(conn, "SELECT count(*) FROM decision d JOIN decision_thread t ON t.id = d.thread_id "
                   "WHERE d.project <> t.project")[0][0] == 0
    assert any("split by the move" in l for l in log)
