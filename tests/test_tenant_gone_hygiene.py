"""A deleted tenant must leave the system QUIETLY — and a real metering
failure must not.

MEASURED 2026-08-12, the run that motivated this. In an eight-minute window
around a release-gate teardown the API log took 13 tracebacks about accounts
that no longer existed: `TenantGone: tenant wht6k4xouq was deprovisioned` and
`database "t_rg86565954p" does not exist`. The second one is the interesting
half — that database was dropped by a DIFFERENT process, so this process's
tombstone never learned about it, pool_for happily rebuilt a pool, and the
same tenant threw 18 times in one minute and kept firing in bursts for six.

None of it cost a customer anything. It cost something worse: it filled the
log with red exactly where a REAL failure would have to be spotted, and
metering.record() swallows real failures by design ("must never break the
work it measures"). So the two halves are pinned together here — dead tenants
go silent, live-tenant losses get counted.

Pure in-process; no Postgres.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import metering, store, wake                    # noqa: E402

T = "gonehygiene1"
DB = store.tenant_db(T)


@pytest.fixture(autouse=True)
def _clean():
    def reset():
        store._dead_dbs.discard(DB)
        store._pools.pop(DB, None)
        store._prov_cache = (0.0, [])
        wake._boards.clear()
        wake._armed.clear()
        with metering._lost_lock:
            metering._lost.update({"events": 0, "tokens": 0, "last": ""})
    reset()
    yield
    reset()


class _DeadPool:
    """A pool whose connection() fails the way psycopg does for a dropped
    database — on ENTRY, not when connection() is called. That timing is the
    whole reason the guard cannot live around pool_for()."""

    def __init__(self, msg):
        self.msg = msg
        self.closed = False

    def connection(self):
        pool = self

        class _CM:
            def __enter__(self):
                raise RuntimeError(pool.msg)

            def __exit__(self, *a):
                return False
        return _CM()

    def close(self):
        self.closed = True


def _install(msg):
    p = _DeadPool(msg)
    store._pools[DB] = p
    return p


def test_a_dropped_database_tombstones_itself_on_first_use():
    """THE regression: a database dropped by another process was rediscovered
    forever. One failure must be enough to learn."""
    p = _install(f'connection failed: FATAL:  database "{DB}" does not exist')
    tok = store.bind(T)
    try:
        with pytest.raises(store.TenantGone):
            with store.connection():
                pass
        assert DB in store._dead_dbs, "first failure did not tombstone"
        assert p.closed, "the dead pool was left open"
        # Every later caller is refused instantly — no second connect.
        with pytest.raises(store.TenantGone):
            store.connection()
    finally:
        store.unbind(tok)


def test_a_server_outage_is_NOT_mistaken_for_a_deletion():
    """The dangerous inverse. Postgres being down must never tombstone a live
    tenant — that would take a customer offline until the process restarts."""
    _install("connection failed: Connection refused; is the server running?")
    tok = store.bind(T)
    try:
        with pytest.raises(RuntimeError):
            with store.connection():
                pass
        assert DB not in store._dead_dbs, \
            "an outage tombstoned a LIVE tenant — this is worse than the bug"
    finally:
        store.unbind(tok)


def test_the_wake_board_never_hands_out_a_dead_tenant():
    """One filter, five worker loops. If wait() can return a dead tenant,
    every loop rediscovers it on its own schedule."""
    wake.poke("turn", T)
    wake.poke("turn", "livetenant1")
    with store._pools_lock:
        store._dead_dbs.add(DB)
    got = wake.wait("turn", lambda: [], timeout=0.01)
    assert got == ["livetenant1"], got


def test_the_safety_sweep_does_not_resurrect_one_either():
    """The sweep returns EVERYONE, so it is the path most likely to bring a
    tombstoned tenant back."""
    with store._pools_lock:
        store._dead_dbs.add(DB)
    wake._last_sweep.pop("curate", None)
    got = wake.wait("curate", lambda: [T, "livetenant1"], timeout=0.01)
    assert T not in got, got


def test_forget_clears_the_board_and_the_armed_retry():
    wake.poke("curate", T)
    wake.poke_later("briefs", T, 3600.0)
    assert ("briefs", T) in wake._armed
    wake.forget("briefs", T)
    assert ("briefs", T) not in wake._armed
    assert not (wake._boards.get("briefs") or set())


def test_metering_is_silent_for_a_deleted_tenant():
    """Nothing to bill, nobody to bill it to. Counting it would cry wolf on
    every account deletion."""
    with store._pools_lock:
        store._dead_dbs.add(DB)
    tok = store.bind(T)
    try:
        metering.record("turn_curate", 1000, 200)     # must not raise
    finally:
        store.unbind(tok)
    assert metering.lost()["events"] == 0


def test_metering_COUNTS_a_failure_on_a_tenant_that_still_exists():
    """The half that matters for money. This used to be a printed traceback
    and nothing else — unbilled inference, invisible."""
    _install("deadlock detected")
    tok = store.bind(T)
    try:
        metering.record("turn_curate", 1000, 200)
        metering.record("merge", 50, 10)
    finally:
        store.unbind(tok)
    lost = metering.lost()
    assert lost["events"] == 2, lost
    assert lost["tokens"] == 1260, lost
    assert "deadlock" in lost["last"], lost
