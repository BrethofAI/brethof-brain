"""THE BACKFILL FOLLOWS move_session (2026-09-29: global's sessions are split
among their real projects before the backfill). A session archived under one
project with a stretch moved to another is walked by each project for its own
turns only — never the archive's column alone. Real scratch Postgres."""
import os
import uuid

import pytest

from core import archive, backfill, ownership, store

DSN = os.environ.get("BRAIN_TEST_DSN", "")
pg = pytest.mark.skipif(not DSN, reason="BRAIN_TEST_DSN not set")


@pytest.fixture
def tenant():
    parts = dict(kv.split("=", 1) for kv in DSN.split())
    for k, v in (("PG_HOST", parts.get("host")), ("PG_PORT", parts.get("port")),
                 ("PG_USER", parts.get("user")), ("PG_PASS", parts.get("password"))):
        if v:
            setattr(store, k, v)
    t = "bfmove_" + uuid.uuid4().hex[:10]
    store.provision_tenant(t)
    tok = store.bind(t)
    try:
        yield t
    finally:
        store.unbind(tok)
        store.deprovision_tenant(t)


def _turns(lo, hi):
    return [{"index": i, "role": "user" if i % 2 == 0 else "assistant",
             "text": f"{'question' if i % 2 == 0 else 'answer'} number {i}"} for i in range(lo, hi)]


@pg
def test_each_project_walks_only_the_turns_it_owns(tenant):
    archive.archive_turns("global", "s1", _turns(0, 20))
    with store.connection() as conn:
        conn.execute("INSERT INTO project (name) VALUES ('brain'), ('global') ON CONFLICT DO NOTHING;")
        ownership.move(conn, "s1", "brain", "10", "19", who="test", why="split")
        conn.commit()
    ownership.invalidate()
    with store.connection() as conn:
        cur = conn.cursor()
        assert [s for s, _ in backfill.sessions(cur, "brain")] == ["s1"]
        assert [s for s, _ in backfill.sessions(cur, "global")] == ["s1"]
        brain = backfill.windows(cur, "s1", project="brain")
        glob = backfill.windows(cur, "s1", project="global")
        conn.rollback()
    assert brain and min(w["lo"] for w in brain) >= 10
    assert glob and max(w["hi"] for w in glob) <= 9


@pg
def test_a_whole_session_moved_leaves_its_old_project(tenant):
    archive.archive_turns("global", "s2", _turns(0, 6))
    with store.connection() as conn:
        conn.execute("INSERT INTO project (name) VALUES ('estate'), ('global') ON CONFLICT DO NOTHING;")
        ownership.move(conn, "s2", "estate", who="test", why="whole")
        conn.commit()
    ownership.invalidate()
    with store.connection() as conn:
        cur = conn.cursor()
        assert "s2" not in [s for s, _ in backfill.sessions(cur, "global")]
        assert [s for s, _ in backfill.sessions(cur, "estate")] == ["s2"]
        conn.rollback()
