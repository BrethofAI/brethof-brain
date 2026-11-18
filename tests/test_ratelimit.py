"""Per-ACCOUNT rate limiter + lane ranking + teach debounce — the three
2026-08-24 defenses from the founder's signed quota architecture."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core import ranking, ratelimit  # noqa: E402


class Buckets(unittest.TestCase):
    def setUp(self):
        ratelimit._buckets.clear()

    def test_burst_then_refusal_then_recovery(self):
        # free plan: 60/min sustained, 120 burst. Fire 120 instantly — all
        # pass; the 121st refuses with a real retry hint.
        with mock.patch.object(ratelimit.time, "monotonic",
                               return_value=1000.0):
            for _ in range(120):
                ok, _r = ratelimit.allow("t1", "free")
                self.assertTrue(ok)
            ok, retry = ratelimit.allow("t1", "free")
        self.assertFalse(ok)
        self.assertGreaterEqual(retry, 1)
        # 2 seconds later two tokens have refilled.
        with mock.patch.object(ratelimit.time, "monotonic",
                               return_value=1002.0):
            ok, _r = ratelimit.allow("t1", "free")
        self.assertTrue(ok)

    def test_plans_scale_the_budget(self):
        with mock.patch.object(ratelimit.time, "monotonic",
                               return_value=1000.0):
            for _ in range(120):
                ratelimit.allow("f", "free")
            self.assertFalse(ratelimit.allow("f", "free")[0])
            # business: 16x the budget — the same burst leaves plenty.
            for _ in range(120):
                self.assertTrue(ratelimit.allow("b", "business")[0])

    def test_keys_never_multiply_the_budget(self):
        """The bucket is per TENANT — the caller passes the tenant, so ten
        keys land on one bucket by construction. Pin the unit: same tenant,
        one budget."""
        with mock.patch.object(ratelimit.time, "monotonic",
                               return_value=1000.0):
            for _ in range(120):
                ratelimit.allow("acct", "free")
            self.assertFalse(ratelimit.allow("acct", "free")[0])

    def test_planless_and_killswitch_are_exempt(self):
        with mock.patch.object(ratelimit.time, "monotonic",
                               return_value=1000.0):
            for _ in range(500):
                self.assertTrue(ratelimit.allow("owner", None)[0])
        with mock.patch.dict(os.environ, {"BRAIN_RATE_LIMIT": "0"}):
            with mock.patch.object(ratelimit.time, "monotonic",
                                   return_value=1000.0):
                for _ in range(500):
                    self.assertTrue(ratelimit.allow("t9", "free")[0])


class Ranking(unittest.TestCase):
    def test_single_tenant_pays_nothing(self):
        with mock.patch.object(ranking, "plan_weight") as pw:
            out = ranking.order(["only"], lambda t: 0)
        pw.assert_not_called()
        self.assertEqual(out, ["only"])

    def test_plan_lifts_backlog_demotes(self):
        weights = {"biz": 5.0, "free": 1.0, "dumper": 5.0}
        backlog = {"biz": 10, "free": 0, "dumper": 5000}
        with mock.patch.object(ranking, "plan_weight",
                               side_effect=lambda t: weights[t]):
            out = ranking.order(["free", "dumper", "biz"],
                                lambda t: backlog[t])
        # biz first (high plan, shallow queue); the dumper's own backlog
        # pushes it behind even the free account — flooding is pointless.
        self.assertEqual(out[0], "biz")
        self.assertEqual(out[-1], "dumper")

    def test_scoring_failure_never_denies_service(self):
        def boom(_t):
            raise RuntimeError("db gone")
        with mock.patch.object(ranking, "plan_weight", side_effect=boom):
            out = ranking.order(["a", "b"], lambda t: 0)
        self.assertEqual(sorted(out), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
