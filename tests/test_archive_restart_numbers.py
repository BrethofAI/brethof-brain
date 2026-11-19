"""A NUMBER TAKEN BY OTHER TEXT IS A NEW TURN (2026-11-19). An adapter that
restarts with the same session id numbers from 0 again; the archive dropped
every such turn (ON CONFLICT DO NOTHING) until the count passed the old one.
Real scratch Postgres: the new turns land at the session's end, in order, a
true retry is still skipped, and the curator gets exactly what landed."""
import os
import uuid

import pytest

from core import archive, store

DSN = os.environ.get("BRAIN_TEST_DSN", "")
pg = pytest.mark.skipif(not DSN, reason="BRAIN_TEST_DSN not set")


@pytest.fixture
def tenant():
    parts = dict(kv.split("=", 1) for kv in DSN.split())
    for k, v in (("PG_HOST", parts.get("host")), ("PG_PORT", parts.get("port")),
                 ("PG_USER", parts.get("user")), ("PG_PASS", parts.get("password"))):
        if v:
            setattr(store, k, v)
    t = "renum_" + uuid.uuid4().hex[:10]
    store.provision_tenant(t)
    tok = store.bind(t)
    try:
        yield t
    finally:
        store.unbind(tok)
        store.deprovision_tenant(t)


def _pair(i, said, answer):
    return [{"index": i, "role": "user", "text": said}, {"index": i + 1, "role": "assistant", "text": answer}]


def _session(sid):
    with store.connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT turn_index, text FROM chat WHERE session_id = %s ORDER BY turn_index;", [sid])
        return cur.fetchall()


@pg
def test_a_restarted_client_s_turns_are_kept_at_the_end_in_order(tenant):
    archive.archive_turns("p", "s1", _pair(0, "first question", "first answer"))
    archive.archive_turns("p", "s1", _pair(2, "second question", "second answer"))
    # the adapter restarts: its count is 0 again
    placed = []
    n = archive.archive_turns("p", "s1", _pair(0, "after the restart", "answered"), placed=placed)
    assert n == 2
    assert [i for i, _ in _session("s1")] == [0, 1, 2, 3, 4, 5]
    assert [x for _, x in _session("s1")][4:] == ["after the restart", "answered"]
    assert [t["index"] for t in placed] == [4, 5]
    # and its next pair (numbered 2, 3 by the restarted client) follows
    archive.archive_turns("p", "s1", _pair(2, "next one", "next answer"))
    assert [x for _, x in _session("s1")][6:] == ["next one", "next answer"]


@pg
def test_a_true_retry_is_still_skipped(tenant):
    archive.archive_turns("p", "s2", _pair(0, "hello there", "hi"))
    placed = []
    assert archive.archive_turns("p", "s2", _pair(0, "hello there", "hi"), placed=placed) == 0
    assert placed == [] and len(_session("s2")) == 2


@pg
def test_a_batch_that_starts_free_and_runs_into_held_numbers_keeps_its_order(tenant):
    archive.archive_turns("p", "s3", _pair(2, "old two", "old three"))
    archive.archive_turns("p", "s3", [{"index": 0, "role": "user", "text": "new zero"},
                                      {"index": 1, "role": "assistant", "text": "new one"},
                                      {"index": 2, "role": "user", "text": "new two"},
                                      {"index": 3, "role": "assistant", "text": "new three"}])
    got = _session("s3")
    assert [x for _, x in got] == ["new zero", "new one", "old two", "old three", "new two", "new three"]


@pg
def test_a_restarted_client_s_retry_of_a_moved_pair_is_not_stored_twice(tenant):
    archive.archive_turns("p", "s4", _pair(0, "before", "ok"))
    archive.archive_turns("p", "s4", _pair(0, "after the restart", "ok then"))     # moved to 2, 3
    placed = []
    assert archive.archive_turns("p", "s4", _pair(0, "after the restart", "ok then"), placed=placed) == 0
    assert placed == [] and len(_session("s4")) == 4
    # a genuinely new pair under the same numbers still lands
    archive.archive_turns("p", "s4", _pair(0, "a third thing", "ok then"))
    assert [x for _, x in _session("s4")][4:] == ["a third thing", "ok then"]
