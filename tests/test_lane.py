"""The ranked retrieval lane (founder 2026-08-24): tenant-ordered, deadline-
aware, judge-demotable, and it logs instead of judging."""
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core import lane  # noqa: E402


def _reset():
    with lane._lock:
        lane._heap.clear()
        lane._plan_cache.clear()
        lane._shed_counts.clear()
        lane._ema_ms = lane._EMA_INIT_MS


class Ordering(unittest.TestCase):
    def test_paid_overtakes_free_demoted_goes_last(self):
        """Push three jobs by hand; the heap must serve business, then
        free, then the demoted-business account — behavior outranks
        money (founder: 'he will buy the plan... put the priority for
        those people very low')."""
        _reset()
        import heapq
        served = []
        for tenant, weight in (("freeacct", 1.0), ("demoted", 0.05),
                               ("bizacct", 5.0)):
            job = lane._Job((-weight, next(lane._seq)), ["q"], 8.0,
                            "query", False)
            heapq.heappush(lane._heap, (*job.prio, job))
            job.texts = [tenant]
        while lane._heap:
            served.append(heapq.heappop(lane._heap)[2].texts[0])
        self.assertEqual(served, ["bizacct", "freeacct", "demoted"])

class Shedding(unittest.TestCase):
    def test_silence_falls_bottom_up(self):
        """Founder's ladder: with a flood of free-tier jobs queued, a free
        arrival sheds instantly — but a BUSINESS arrival sees an empty
        road at its own rank and is admitted. The projection counts only
        the queue ahead of you, so who hears nothing is free users first,
        paid later, business last."""
        _reset()
        lane._ema_ms = 500.0
        with lane._lock:
            import heapq
            for i in range(40):
                j = lane._Job((-1.0, next(lane._seq)), ["x"], 8.0,
                              "query", False)
                heapq.heappush(lane._heap, (*j.prio, j))
        sheds = []
        t0 = time.monotonic()
        with mock.patch.object(lane, "_ensure_workers"), \
             mock.patch.object(lane.store, "current_tenant",
                               return_value="flooder"), \
             mock.patch.object(lane, "_weight", return_value=1.0), \
             mock.patch.object(lane, "_note_shed",
                               side_effect=lambda t: sheds.append(t)):
            out = lane.ranked(["query"], 8.0, "query", False)
        self.assertIsNone(out)
        self.assertLess(time.monotonic() - t0, 0.5, "shed must be instant")
        self.assertEqual(sheds, ["flooder"])
        # The same moment, the same flood: the door's own arithmetic —
        # free is refused, business sees an empty road at its rank and is
        # admitted, a demoted account is refused even harder than free.
        self.assertFalse(lane._admit(1.0, 8.0))
        self.assertTrue(lane._admit(5.0, 8.0))
        self.assertFalse(lane._admit(lane.DEMOTED_WEIGHT, 8.0))

class FailOpen(unittest.TestCase):
    def test_kill_switch_and_lane_trouble_fall_through_to_direct(self):
        _reset()
        with mock.patch.dict(os.environ, {"BRAIN_RANKED_EMBED": "0"}), \
             mock.patch("core.embed._call_sidecar",
                        return_value=[[0.1]]) as direct:
            out = lane.ranked(["q"], 8.0, "query", False)
        self.assertEqual(out, [[0.1]])
        direct.assert_called_once()
        with mock.patch.object(lane.store, "current_tenant",
                               side_effect=RuntimeError("unbound")), \
             mock.patch("core.embed._call_sidecar",
                        return_value=[[0.2]]) as direct:
            out = lane.ranked(["q"], 8.0, "query", False)
        self.assertEqual(out, [[0.2]])


class EndToEnd(unittest.TestCase):
    def test_jobs_flow_through_workers_and_ema_learns(self):
        _reset()
        def fake_sidecar(texts, timeout, kind, interactive):
            time.sleep(0.01)
            return [[42.0]] * len(texts)
        with mock.patch("core.embed._call_sidecar",
                        side_effect=fake_sidecar), \
             mock.patch.object(lane.store, "current_tenant",
                               return_value="t1"), \
             mock.patch.object(lane, "_weight", return_value=3.0):
            outs = []
            ts = [threading.Thread(
                target=lambda: outs.append(
                    lane.ranked(["hello"], 8.0, "query", False)))
                for _ in range(6)]
            for t in ts:
                t.start()
            for t in ts:
                t.join(timeout=5)
        self.assertEqual(len(outs), 6)
        self.assertTrue(all(o == [[42.0]] for o in outs))
        self.assertNotEqual(lane._ema_ms, lane._EMA_INIT_MS,
                            "service time must be measured, not assumed")


if __name__ == "__main__":
    unittest.main()
