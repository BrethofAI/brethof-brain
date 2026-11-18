"""The wake board — the last metre of the push pipeline (2026-08-09).

Workers no longer sweep every tenant database on a timer; they block on
this board and visit only who was poked. These tests pin the contract that
makes that safe: nothing poked is lost, retries re-arm, and the slow safety
sweep still returns everyone so a lost hint lags hours, never forever.
"""
import time
import unittest

import api.main  # noqa: F401  — match uvicorn's import order
from core import wake


class WakeBoard(unittest.TestCase):
    def setUp(self):
        # Boards are module-global; give each test its own namespace.
        self.board = f"test_{self._testMethodName}"

    def test_poke_then_wait_returns_tenant_and_drains(self):
        wake.poke(self.board, "alice")
        got = wake.wait(self.board, lambda: [], timeout=0.05)
        self.assertEqual(got, ["alice"])
        # Drained: a second wait comes back empty, not a repeat.
        self.assertEqual(wake.wait(self.board, lambda: [], timeout=0.05), [])

    def test_wait_blocks_until_poked_not_full_timeout(self):
        import threading
        t0 = time.monotonic()
        threading.Timer(0.1, wake.poke, [self.board, "bob"]).start()
        got = wake.wait(self.board, lambda: [], timeout=5.0)
        self.assertEqual(got, ["bob"])
        self.assertLess(time.monotonic() - t0, 2.0,
                        "wait() must return on the poke, not the timeout")

    def test_pokes_coalesce_per_tenant(self):
        for _ in range(50):
            wake.poke(self.board, "alice")
        wake.poke(self.board, "carol")
        self.assertEqual(wake.wait(self.board, lambda: [], timeout=0.05),
                         ["alice", "carol"])

    def test_poke_later_fires(self):
        wake.poke_later(self.board, "dave", 0.1)
        self.assertEqual(wake.wait(self.board, lambda: [], timeout=0.05), [],
                         "must not fire before its delay")
        time.sleep(0.2)
        self.assertEqual(wake.wait(self.board, lambda: [], timeout=0.05),
                         ["dave"])

    def test_poke_later_dedupes_to_the_earliest(self):
        # An hourly-backoff job observed many times arms ONE near timer,
        # not a timer per observation.
        wake.poke_later(self.board, "erin", 0.1)
        wake.poke_later(self.board, "erin", 500.0)   # later — absorbed
        time.sleep(0.2)
        self.assertEqual(wake.wait(self.board, lambda: [], timeout=0.05),
                         ["erin"])

    def test_safety_sweep_returns_everyone_after_interval(self):
        old = wake.SAFETY_SWEEP_S
        wake.SAFETY_SWEEP_S = 0.0        # every wait is due a sweep
        try:
            wake.poke(self.board, "alice")
            got = wake.wait(self.board, lambda: ["t1", "t2"], timeout=0.05)
            self.assertEqual(got, ["alice", "t1", "t2"],
                             "sweep is a union with pending pokes")
        finally:
            wake.SAFETY_SWEEP_S = old

    def test_mark_swept_suppresses_the_sweep(self):
        wake.mark_swept(self.board)      # the boot recovery sweep just ran
        got = wake.wait(self.board, lambda: ["t1"], timeout=0.05)
        self.assertEqual(got, [], "no double sweep right after boot")

    def test_sweep_survives_a_broken_tenant_source(self):
        old = wake.SAFETY_SWEEP_S
        wake.SAFETY_SWEEP_S = 0.0
        try:
            wake.poke(self.board, "alice")

            def boom():
                raise RuntimeError("pg down")
            self.assertEqual(wake.wait(self.board, boom, timeout=0.05),
                             ["alice"],
                             "a failing tenant list must not eat the pokes")
        finally:
            wake.SAFETY_SWEEP_S = old


if __name__ == "__main__":
    unittest.main()
