"""Pins the history-cleanup contract (founder feature 2026-07-30; delete-only
since 2026-08-29 — no summarize, no digest: hygiene is heal's job and this
door only sheds old raw bulk).

What must never regress:
1. No confirm = no deletion — the default path previews and instructs.
2. The MIN_AGE floor — "old history" only; a 3-day cutoff is refused. This
   IS the protection: through our software nothing younger than 90 days can
   be deleted, so an injected or misbehaving agent cannot strip the recent
   archive memory depends on.
3. Customer surface enforces write scope before touching the owner tool.
4. Bad project rejected before anything is queued.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import prune, tools  # noqa: E402
from api import customer_tools  # noqa: E402


class ConfirmContract(unittest.TestCase):
    def test_no_confirm_previews_never_deletes(self):
        with mock.patch.object(prune, "preview",
                               return_value={"rows": 120, "sessions": 3,
                                             "text_mb": 1.5,
                                             "oldest": "2026-01-01",
                                             "newest": "2026-02-01",
                                             "db_size_now": "700 MB"}) as pv, \
             mock.patch("api.curate_queue.enqueue_prune") as enq, \
             mock.patch("api.curate_queue.last_prune_result",
                        return_value=""):
            out = tools.cleanup_history("mind", 180)
        pv.assert_called_once()
        enq.assert_not_called()
        self.assertIn("PREVIEW", out)
        self.assertIn('confirm="mind"', out)

    def test_confirm_enqueues(self):
        with mock.patch.object(prune, "preview",
                               return_value={"rows": 10, "sessions": 1,
                                             "text_mb": 0.1,
                                             "oldest": "2026-01-01",
                                             "newest": "2026-02-01",
                                             "db_size_now": "1 MB"}), \
             mock.patch("api.curate_queue.enqueue_prune",
                        return_value="") as enq, \
             mock.patch("api.curate_queue.last_prune_result",
                        return_value=""):
            out = tools.cleanup_history("mind", 180, confirm="mind")
        enq.assert_called_once_with("mind", 180)
        self.assertIn("queued", out.lower())

    def test_confirm_on_empty_range_does_not_enqueue(self):
        # Caught live in the 2026-07-30 soak E2E: preview said "nothing to
        # clean" but a confirmed call still queued a pointless job.
        with mock.patch.object(prune, "preview",
                               return_value={"rows": 0, "sessions": 0,
                                             "text_mb": 0, "oldest": "None",
                                             "newest": "None",
                                             "db_size_now": "1 MB"}), \
             mock.patch("api.curate_queue.enqueue_prune") as enq, \
             mock.patch("api.curate_queue.last_prune_result",
                        return_value=""):
            out = tools.cleanup_history("mind", 180, confirm="mind")
        enq.assert_not_called()
        self.assertIn("Nothing to clean", out)

    def test_wrong_confirm_is_preview_not_delete(self):
        with mock.patch.object(prune, "preview",
                               return_value={"rows": 0, "sessions": 0,
                                             "text_mb": 0, "oldest": "None",
                                             "newest": "None",
                                             "db_size_now": "1 MB"}), \
             mock.patch("api.curate_queue.enqueue_prune") as enq, \
             mock.patch("api.curate_queue.last_prune_result",
                        return_value=""):
            tools.cleanup_history("mind", 180, confirm="mnid")
        enq.assert_not_called()


class Guards(unittest.TestCase):
    def test_min_age_floor(self):
        out = tools.cleanup_history("mind", 3, confirm="mind")
        self.assertTrue(out.startswith("Error"))
        self.assertIn(str(prune.MIN_AGE_DAYS), out)

    def test_bad_project(self):
        self.assertTrue(tools.cleanup_history("Mind!", 180)
                        .startswith("Error"))

    def test_customer_readonly_scope_blocks(self):
        with mock.patch.object(customer_tools, "_visible", return_value=True), \
             mock.patch.object(customer_tools, "_writable",
                               return_value=False), \
             mock.patch.object(tools, "cleanup_history") as owner:
            out = customer_tools.cleanup_history("mind", 180,
                                                 confirm="mind")
        owner.assert_not_called()
        self.assertIn("read-only", out)

    def test_customer_invisible_project_denied(self):
        with mock.patch.object(customer_tools, "_visible",
                               return_value=False), \
             mock.patch.object(tools, "cleanup_history") as owner:
            out = customer_tools.cleanup_history("secret", 180)
        owner.assert_not_called()
        self.assertEqual(out, "No such project.")


if __name__ == "__main__":
    unittest.main()
