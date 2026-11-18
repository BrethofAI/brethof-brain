"""A turn keeps its own time (2026-10-23): core/archive.said_at accepts the
client's per-turn timestamp inside [now - 30 days, now + 5 min] and nothing
else, so a period report can tell when a thing was said and a client can
never age a fresh row past cleanup_history's 90-day line."""
from datetime import datetime, timedelta, timezone

from core import archive


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def test_a_recent_turn_keeps_its_own_time():
    t = datetime.now(timezone.utc) - timedelta(hours=3)
    got = archive.said_at({"timestamp": _iso(t)})
    assert got is not None and abs((got - t).total_seconds()) < 1


def test_the_future_and_the_far_past_fall_back_to_the_write_time():
    now = datetime.now(timezone.utc)
    assert archive.said_at({"timestamp": _iso(now + timedelta(hours=1))}) is None
    assert archive.said_at({"timestamp": _iso(now - timedelta(days=31))}) is None


def test_no_time_or_a_bad_one_falls_back():
    assert archive.said_at({}) is None
    assert archive.said_at({"timestamp": "yesterday"}) is None
    assert archive.said_at({"timestamp": 12345}) is None


def test_a_naive_time_is_read_as_utc():
    t = (datetime.now(timezone.utc) - timedelta(minutes=10)).replace(tzinfo=None)
    assert archive.said_at({"timestamp": t.isoformat()}).tzinfo is not None


def test_the_insert_writes_the_turns_time():
    src = open(archive.__file__).read()
    assert "turn_index, role, text, ts)" in src and "coalesce(%s, now())" in src
