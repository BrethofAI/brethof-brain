"""A RE-SENT TURN IS NOT NEW CONVERSATION (2026-09-28). A compact's re-flush
re-sends turns the archive already holds; the stop hook fed the whole batch to
the curator, and a month-old stretch of one long session came back as 146
records and 194 lines (rules:never_back_curate_old_chat). Real scratch
Postgres: only the turns the archive takes for the first time go up."""
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
    t = "refeed_" + uuid.uuid4().hex[:10]
    store.provision_tenant(t)
    tok = store.bind(t)
    try:
        yield t
    finally:
        store.unbind(tok)
        store.deprovision_tenant(t)


def _turns(lo, hi):
    return [{"index": i, "role": "user" if i % 2 == 0 else "assistant", "text": f"turn {i}"}
            for i in range(lo, hi)]


@pg
def test_a_re_sent_turn_is_not_fed_again(tenant):
    archive.archive_turns("brain", "s1", _turns(0, 6))
    fresh = archive.unseen("s1", _turns(0, 8))
    assert [t["index"] for t in fresh] == [6, 7]


@pg
def test_a_whole_re_flush_feeds_nothing(tenant):
    archive.archive_turns("brain", "s1", _turns(0, 6))
    assert archive.unseen("s1", _turns(0, 6)) == []


@pg
def test_new_turns_keep_their_archive_index(tenant):
    # a turn without an index is archived at its position: the piece's range
    # must say the same after the batch is filtered
    archive.archive_turns("brain", "s1", _turns(0, 2))
    batch = [{"role": "user", "text": "a"}, {"role": "assistant", "text": "b"},
             {"role": "user", "text": "c"}]
    assert [t["index"] for t in archive.unseen("s1", batch)] == [2]


@pg
def test_another_session_s_turns_do_not_count(tenant):
    archive.archive_turns("brain", "s1", _turns(0, 4))
    assert len(archive.unseen("s2", _turns(0, 4))) == 4


# ── a re-sent turn under a NEW number (2026-10-15) ───────────────────────────
# Claude Code writes old messages into a long session's transcript again; the
# client numbers them anew, so the turn-index key let ~9,000 copies in. The box
# refuses them by content, whatever harness sent them.
LONG = "a message long enough to be judged a repeat when it comes back — " * 2


def _long(lo, hi, offset=0):
    return [{"index": i + offset, "role": "user" if i % 2 == 0 else "assistant",
             "text": f"{LONG} #{i}"} for i in range(lo, hi)]


@pg
def test_old_turns_re_sent_under_new_numbers_are_neither_stored_nor_fed(tenant):
    archive.archive_turns("brain", "s1", _long(0, 6))
    copies = _long(2, 6, offset=100) + [{"index": 110, "role": "user", "text": LONG + " new"}]
    assert [t["index"] for t in archive.unseen("s1", copies)] == [110]
    assert archive.archive_turns("brain", "s1", copies) == 1
    with store.connection() as conn:
        n = conn.execute("SELECT count(*) FROM chat WHERE session_id = 's1';").fetchone()[0]
    assert n == 7


@pg
def test_short_repeats_and_other_sessions_are_left_alone(tenant):
    archive.archive_turns("brain", "s1", [{"index": 0, "role": "user", "text": "yes"}] + _long(1, 3))
    again = [{"index": 5, "role": "user", "text": "yes"}]
    assert archive.archive_turns("brain", "s1", again) == 1          # "yes" twice is a real exchange
    assert archive.archive_turns("brain", "s2", _long(1, 3)) == 2   # the same text in another session is new


@pg
def test_a_repeat_inside_one_batch_keeps_the_first(tenant):
    batch = _long(0, 2) + [dict(_long(0, 1)[0], index=9)]
    assert archive.archive_turns("brain", "s1", batch) == 2
