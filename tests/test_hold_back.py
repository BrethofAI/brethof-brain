"""THE HOLD-BACK (founder 2026-09-28: "inject with delay"). Real scratch
Postgres: what a session wrote since its last compact never comes back to
that session's prompt hook — its older writing and every other session's
do; another session sees all of it at once."""
import json
import os
import uuid

import pytest

from core import decisionline, injection, pipecommands, store

DSN = os.environ.get("BRAIN_TEST_DSN", "")
pg = pytest.mark.skipif(not DSN, reason="BRAIN_TEST_DSN not set")
S, T = "sess-own-0001", "sess-other-0002"


@pytest.fixture
def tenant(monkeypatch):
    parts = dict(kv.split("=", 1) for kv in DSN.split())
    for k, v in (("PG_HOST", parts.get("host")), ("PG_PORT", parts.get("port")),
                 ("PG_USER", parts.get("user")), ("PG_PASS", parts.get("password"))):
        if v:
            setattr(store, k, v)
    monkeypatch.setattr("core.embed.embed_one", lambda *a, **k: None)
    t = "hold_" + uuid.uuid4().hex[:10]
    store.provision_tenant(t)
    tok = store.bind(t)
    try:
        with store.connection() as c:
            cur = c.cursor()
            cur.execute("INSERT INTO project (name) VALUES ('brain') ON CONFLICT DO NOTHING;")
            for rid, sid, hi in (("own_new", S, 50), ("own_old", S, 10), ("other_new", T, 50)):
                cur.execute("INSERT INTO document (project, record_id, type, title, content, meta) "
                            "VALUES ('brain', %s, 'fact', %s, 'the flusher writes usage rows one by one', %s);",
                            [rid, f"usage flusher {rid}", json.dumps({"origin": {"session_id": sid, "turn_lo": hi - 2,
                                                                                  "turn_hi": hi}})])
            cur.execute(decisionline.DDL)
            for what, sid, hi in (("own new line", S, 50), ("own old line", S, 10), ("other new line", T, 50)):
                cur.execute("INSERT INTO decision (project, day, kind, what, things, session_id, turn_lo, turn_hi) "
                            "VALUES ('brain', '2026-09-28', 'decided', %s, ARRAY['usage flusher'], %s, %s, %s);",
                            [f"usage flusher: {what}", sid, hi - 2, hi])
            injection.mark_compact(c, cur, S, 20)
            c.commit()
        yield t
    finally:
        store.unbind(tok)
        store.deprovision_tenant(t)


def _rids(sid):
    ans = pipecommands.recall("", {"project": "brain", "query": "usage flusher rows", "session_id": sid,
                                   "include_chat": False})
    return {h["row"]["record_id"] for h in ans["hits"]}


def _lines(sid):
    ans = pipecommands.history_near("", {"project": "brain", "query": "the usage flusher", "session_id": sid})
    return {d["what"].split(": ", 1)[1] for d in ans["lines"]}


@pg
def test_the_session_does_not_get_back_what_it_wrote_since_its_compact(tenant):
    assert _rids(S) == {"own_old", "other_new"}
    assert _lines(S) == {"own old line", "other new line"}


@pg
def test_another_session_gets_everything_but_its_own_new_writing(tenant):
    # T never compacted: its own line is held, S's writing is new to it
    assert _rids(T) == {"own_new", "own_old"}
    assert _lines(T) == {"own new line", "own old line"}


@pg
def test_after_the_next_compact_it_comes_back(tenant):
    with store.connection() as c:
        injection.mark_compact(c, c.cursor(), S, 60)
    assert _rids(S) == {"own_new", "own_old", "other_new"}
    assert _lines(S) == {"own new line", "own old line", "other new line"}


@pg
def test_no_session_no_hold_back(tenant):
    assert _rids("") == {"own_new", "own_old", "other_new"}


@pg
def test_a_save_is_held_from_the_session_that_made_it(tenant, monkeypatch):
    """The save gap (2026-09-28): a save is archived under its own "save-…"
    id; the one session prompting in the project owns it, and its records and
    lines stay out of that session's prompt hook until it compacts."""
    monkeypatch.setattr("core.hubfeed.active", lambda: False)
    from core import tools
    with store.connection() as c:
        injection.note_prompt(c, c.cursor(), "brain", S)
    tools.save_memory("brain", "the usage flusher batches are written one row at a time", "usage flusher")
    with store.connection() as c:
        cur = c.cursor()
        cur.execute("SELECT save_sid, session_id FROM save_origin;")
        rows = cur.fetchall()
        assert len(rows) == 1 and rows[0][1] == S
        save_sid = rows[0][0]
        cur.execute("INSERT INTO document (project, record_id, type, title, content, meta) VALUES "
                    "('brain', 'saved_rec', 'fact', 'usage flusher saved', 'the flusher writes usage rows one by one', %s);",
                    [json.dumps({"origin": {"session_id": save_sid, "turn_lo": 0, "turn_hi": 0}})])
        c.commit()
    # S compacted at the fixture's setup, BEFORE the save: held from S
    assert "saved_rec" not in _rids(S)
    assert "saved_rec" in _rids(T)


@pg
def test_two_sessions_active_leave_a_save_unattributed(tenant, monkeypatch):
    monkeypatch.setattr("core.hubfeed.active", lambda: False)
    from core import tools
    with store.connection() as c:
        injection.note_prompt(c, c.cursor(), "brain", S)
        injection.note_prompt(c, c.cursor(), "brain", T)
    tools.save_memory("brain", "another usage flusher fact", "usage flusher 2")
    with store.connection() as c:
        cur = c.cursor()
        cur.execute("SELECT count(*) FROM save_origin;")
        assert cur.fetchone()[0] == 0
