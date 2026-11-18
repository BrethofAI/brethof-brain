"""The hub restarts itself when it stops answering (2026-11-19)."""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from hub import local


def _server(status):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status()); self.end_headers(); self.wfile.write(b"{}")
        def log_message(self, *a):
            pass
    s = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s, f"http://127.0.0.1:{s.server_port}/health"


def _run(urls, rounds):
    exits, n = [], [0]

    def sleep(_):
        n[0] += 1
        if n[0] > rounds:
            raise StopIteration
    try:
        local.watchdog(urls, exit=exits.append, sleep=sleep)
    except StopIteration:
        pass
    return exits


def test_a_healthy_hub_is_never_stopped():
    s, u = _server(lambda: 200)
    assert _run([u], 10) == []
    s.shutdown()


def test_three_misses_in_a_row_exit_non_zero(monkeypatch):
    monkeypatch.setattr(local, "WATCHDOG_TIMEOUT", 0.5)
    s, u = _server(lambda: 500)
    assert _run([u], 10) == [local.WATCHDOG_EXIT]
    s.shutdown()


def test_a_dead_door_counts_as_a_miss_and_one_good_answer_resets(monkeypatch):
    monkeypatch.setattr(local, "WATCHDOG_TIMEOUT", 0.5)
    seq = iter([500, 500, 200, 500, 500, 200, 500, 500])
    s, u = _server(lambda: next(seq, 200))
    assert _run([u], 8) == []          # never three in a row
    s.shutdown()
    assert _run(["http://127.0.0.1:9/health"], 3) == [local.WATCHDOG_EXIT]   # nothing listening
