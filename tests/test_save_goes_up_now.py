"""An explicit save goes to the hub at once (2026-09-28): a save is a whole
unit of its own, a one-piece window that the quiet flush held five minutes —
the hosted rig's seed timed out on it. A conversation piece still waits for
its window."""
import threading

from core import hubfeed


def _run_feed(monkeypatch, now, count=1):
    pushed = []
    monkeypatch.setattr(hubfeed, "active", lambda: True)
    monkeypatch.setattr(hubfeed, "_tenant", lambda: "t")
    monkeypatch.setattr(hubfeed, "_ensure_flusher", lambda t: None)
    monkeypatch.setattr(hubfeed, "_pieces", lambda turns: [("piece", 0, 0)])
    monkeypatch.setattr("core.hubwindow.add", lambda cur, p, s, pieces: count)
    monkeypatch.setattr(hubfeed, "_push", lambda t, p, s, nxt="", force=False: pushed.append(force))

    class _Conn:
        def cursor(self):
            return None

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr("core.store.connection", lambda: _Conn())
    monkeypatch.setattr("core.store.bind", lambda t: None)
    monkeypatch.setattr("core.store.unbind", lambda tok: None)
    hubfeed.feed("brain", "save-1", [{"role": "saved", "text": "x"}], now=now)
    for t in threading.enumerate():
        if t.name == "hubfeed":
            t.join(timeout=5)
    return pushed


def test_a_save_goes_up_at_once(monkeypatch):
    assert _run_feed(monkeypatch, now=True) == [True]


def test_a_conversation_piece_waits_for_its_window(monkeypatch):
    assert _run_feed(monkeypatch, now=False) == []
    assert _run_feed(monkeypatch, now=False, count=5) == [False]
