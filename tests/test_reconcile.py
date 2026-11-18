"""Pins the reconcile stage's two load-bearing choices.

Both come from a measurement on 257 records of real memory (2026-09-01), not
from taste: similarity alone recovers only 67.3% of what the hand pass says is
related at k=20, so the ENTITY channel is the precise core; and full bodies for
every candidate cost ~75k chars on web, so only the core gets a body.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import reconcile  # noqa: E402


def _cur(rows):
    c = mock.MagicMock()
    c.fetchall.return_value = rows
    return c


# (The shared-name channel and its tests left on 2026-09-17: the records and
#  the graph are two systems, neither reads the other — founder.)


class BodiesGoByRankNotByChannel(unittest.TestCase):
    """Which candidates arrive with a body is decided by fused RANK.

    Tiering by channel — bodies for entity-found, titles for similarity-found —
    was measured wrong on 2026-09-01. It holds only when the arriving record
    already has entity links. A NEW record has none, so similarity finds the
    true target: for a fresh arrival it sat in the similarity tail 15 times in
    39, and 7 of the 8 duplicates/contradictions heal then missed were tail.
    The model may only act on what it has read, so what it reads is chosen by
    likelihood of mattering, not by which channel found it.
    """
    def _render(self, n):
        rec = {"record_id": "new", "type": "note", "title": "T",
               "content": "the arrived body"}
        cands = [{"record_id": f"c{i}", "type": "note", "title": f"TITLE{i}",
                  "content": f"BODY{i}", "upd": "2026-09-01",
                  "why": "vector"} for i in range(n)]
        # the single arrival goes up as a batch of one; the box names which
        # candidates carry a body by fused rank (reconcile.BODY_N)
        from hub import judgetext
        return judgetext.heal_batch("web", [rec], cands,
                                    {c["record_id"] for c in cands[:reconcile.BODY_N]})

    def test_the_top_ranked_candidates_carry_bodies(self):
        out = self._render(reconcile.BODY_N + 4)
        self.assertIn("BODY0", out)
        self.assertIn(f"BODY{reconcile.BODY_N - 1}", out)

    def test_the_rest_are_titles_only(self):
        out = self._render(reconcile.BODY_N + 4)
        self.assertIn(f"TITLE{reconcile.BODY_N}", out)
        self.assertNotIn(f"BODY{reconcile.BODY_N}", out)

    def test_a_similarity_found_candidate_can_still_carry_a_body(self):
        # The exact regression: every candidate here is similarity-only, and
        # under channel tiering not one of them would have had a body.
        self.assertIn("BODY0", self._render(3))

    def test_the_tail_says_out_loud_that_bodies_were_withheld(self):
        # A model that cannot tell "there is nothing more" from "you were not
        # shown it" will merge on a title alone — measured, 9 blind ops.
        self.assertIn("titles only", self._render(reconcile.BODY_N + 4))

    def test_a_short_candidate_list_has_no_withheld_tail_at_all(self):
        self.assertNotIn("titles only", self._render(3))

    def test_the_arrived_record_is_always_shown_in_full(self):
        self.assertIn("the arrived body", self._render(3))


class QueueContract(unittest.TestCase):
    def test_exclusion_is_a_database_constraint_not_application_code(self):
        # Two claims that both read "nothing running" under READ COMMITTED
        # would both proceed; only a unique index makes the second one fail.
        from core import healqueue
        self.assertIn("UNIQUE INDEX", healqueue._DDL)
        self.assertIn("status = 'running'", healqueue._DDL)

    def test_a_crashed_consumer_can_be_reclaimed(self):
        # The exclusion that cannot be released is worse than no exclusion.
        from core import healqueue
        self.assertTrue(hasattr(healqueue, "reclaim"))
        self.assertGreater(healqueue.STALE_MINUTES, 0)


if __name__ == "__main__":
    unittest.main()


class OrphanedQueuesGetCleared(unittest.TestCase):
    """A trigger only knows the project whose record just landed.

    Found on the FIRST live run (2026-09-01): six bv rows sat pending with
    attempts=0 for an hour while brain and ms drained repeatedly around them.
    Their drain thread had died with a container restart, and bv received no
    further records, so nothing ever came back for them. A durable queue that
    nothing returns to is just a slower way to lose the work.
    """
    def test_pending_projects_is_ordered_by_the_oldest_waiting_row(self):
        from core import healqueue
        cur = mock.MagicMock()
        cur.fetchall.return_value = [("bv",), ("brain",)]
        conn = mock.MagicMock(); conn.cursor.return_value = cur
        with mock.patch.object(healqueue, "ensure"):
            self.assertEqual(healqueue.pending_projects(conn), ["bv", "brain"])
        sql = cur.execute.call_args[0][0]
        self.assertIn("status = 'pending'", sql)
        self.assertIn("ORDER BY min(id)", sql)


class AMergedRecordIsDoneNotLost(unittest.TestCase):
    """"record is gone" is the ordinary end of a record, not a failure.

    Seen the first day live (2026-09-01): an earlier op folded a record into a
    survivor and deleted it; its own queue row came up, found nothing, and was
    retried three times before being announced as "it will never be
    reconciled" — reporting data loss where there was none and putting
    failed=3 lost=1 on an otherwise clean run. A report that cannot be trusted
    is worse than none.
    """
    def test_a_gone_record_is_finished_quietly_not_retried(self):
        from core import reconcile as rc
        finished, failed = [], []
        claims = [(7, "gone_one", 1), None]
        with mock.patch.object(rc, "store") as st, \
             mock.patch.object(rc, "reconcile_batch",
                               return_value={"ok": False, "note": "records are gone"}), \
             mock.patch("core.healqueue.reclaim"), \
             mock.patch("core.healqueue.ready", return_value=(True, 1)), \
             mock.patch("core.healqueue.peek_more", return_value=[]), \
             mock.patch("core.healqueue.depth", return_value=0), \
             mock.patch("core.healqueue.claim", side_effect=claims), \
             mock.patch("core.healqueue.finish", side_effect=lambda c, q: finished.append(q)), \
             mock.patch("core.healqueue.fail", side_effect=lambda c, q, e: failed.append(q)):
            st.connection.return_value.__enter__.return_value = mock.MagicMock()
            r = rc.drain("t", "brain")
        self.assertEqual(finished, [7])
        self.assertEqual(failed, [])
        self.assertEqual(r["failed"], 0)
        self.assertEqual(r["dropped_forever"], 0)
