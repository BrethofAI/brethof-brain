"""Heal by four (founder 2026-09-03): a save pools; a heal call is made when
four records are due, or the oldest has waited, or a compact forces it."""
from unittest import mock

from core import healqueue, reconcile, hubfeed


def _conn(rows):
    cur = mock.MagicMock(); cur.fetchone.side_effect = rows
    conn = mock.MagicMock(); conn.cursor.return_value = cur
    return conn, cur


def test_ready_when_four_are_due():
    conn, _ = _conn([(4,)])
    with mock.patch.object(healqueue, "ensure"):
        assert healqueue.ready(conn, "p") == (True, 4)


def test_not_ready_when_fewer_no_matter_how_old():
    conn, _ = _conn([(3,)])
    with mock.patch.object(healqueue, "ensure"):
        assert healqueue.ready(conn, "p") == (False, 3)
    assert not hasattr(healqueue, "POOL_WAIT_MIN"), "the pool has no clock"


def test_nothing_due_is_not_ready():
    conn, _ = _conn([(0,)])
    with mock.patch.object(healqueue, "ensure"):
        assert healqueue.ready(conn, "p") == (False, 0)


def test_drain_pools_instead_of_claiming_unless_forced():
    with mock.patch.object(reconcile, "store") as st, \
         mock.patch("core.healqueue.reclaim"), \
         mock.patch("core.healqueue.ready", return_value=(False, 2)), \
         mock.patch("core.healqueue.claim") as claim:
        st.connection.return_value.__enter__.return_value = mock.MagicMock()
        r = reconcile.drain("t", "p")
    assert r["pooling"] == 2 and r["reconciled"] == 0
    claim.assert_not_called()
    with mock.patch.object(reconcile, "store") as st, \
         mock.patch("core.healqueue.reclaim"), \
         mock.patch("core.healqueue.ready", return_value=(False, 2)), \
         mock.patch("core.healqueue.depth", return_value=0), \
         mock.patch("core.healqueue.claim", return_value=None) as claim:
        st.connection.return_value.__enter__.return_value = mock.MagicMock()
        reconcile.drain("t", "p", force=True)
    claim.assert_called_once()


def test_hubfeed_retries_a_refused_connection_and_not_a_4xx():
    import urllib.error
    calls = []
    def flaky(req, timeout=None):
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.URLError("refused")
        return mock.MagicMock(read=lambda: b"")
    with mock.patch("urllib.request.urlopen", side_effect=flaky), \
         mock.patch("time.sleep") as slp:
        hubfeed._post(object())
    assert len(calls) == 3 and slp.call_count == 2
    def refused(req, timeout=None):
        raise urllib.error.HTTPError("u", 429, "over", {}, None)
    with mock.patch("urllib.request.urlopen", side_effect=refused), \
         mock.patch("time.sleep") as slp:
        try:
            hubfeed._post(object())
        except urllib.error.HTTPError as e:
            assert e.code == 429
        else:
            raise AssertionError("a 4xx must not be retried")
    slp.assert_not_called()


def test_compact_does_not_force_the_pool():
    src = open("api/main.py", encoding="utf-8").read()
    assert "force=True" not in src.split("pre-compact", 1)[1].split("@app.", 1)[0]


def test_hub_heal_bound_is_the_curator_one_for_one():
    src = open("hub/server.py", encoding="utf-8").read()
    fn = src.split("async def heal_over_allowance", 1)[1].split("\n\n\n", 1)[0]
    assert 'month_calls(tenant["id"], "curate")' in fn and "month_units" not in fn
    assert 'HUB_HEAL_GRACE", "0"' in src
