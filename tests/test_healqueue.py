"""The heal queue's retry schedule — spaced, not burst.

2026-09-02, harbor pipe test: the provider slowed for ~70 s, the box gave up
at 15 s three times in 45 s, and a record that would have been folded a
minute later was dropped from heal for good. Retries now wait 1, 5, 30, 120,
720 minutes; only after the last does the row go, loudly."""
from core import healqueue


def test_backoff_grows_and_then_drops():
    waits = [healqueue.backoff_minutes(n) for n in range(1, healqueue.MAX_ATTEMPTS + 1)]
    assert waits[:-1] == list(healqueue.BACKOFF_MINUTES)
    assert waits[-1] is None                       # the last attempt drops
    assert healqueue.MAX_ATTEMPTS == len(healqueue.BACKOFF_MINUTES) + 1
    assert healqueue.backoff_minutes(1) == 1       # a blip costs a minute


def test_a_minute_of_trouble_is_not_a_drop():
    """Three failures — the whole old budget — now leave the row pending."""
    assert all(healqueue.backoff_minutes(n) is not None for n in (1, 2, 3))


def test_claim_and_orphan_scan_only_take_due_rows():
    import inspect
    assert "next_try <= now()" in inspect.getsource(healqueue.claim)
    assert "next_try <= now()" in inspect.getsource(healqueue.pending_projects)
