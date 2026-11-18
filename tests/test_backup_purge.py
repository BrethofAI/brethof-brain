"""Backup erasure (founder 2026-08-24: "we can't say deleted and keep copy
in borg"). The API queues an intent; the host drains it. These pin the
half that lives in this repo: every deletion path queues, bad ids never
reach a delete command, and a queue failure never blocks the erasure of
the live data."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core import backup_purge  # noqa: E402


class Queue(unittest.TestCase):
    def _tmp(self):
        d = tempfile.mkdtemp()
        return os.path.join(d, "purge.queue")

    def test_queues_the_tenant(self):
        path = self._tmp()
        with mock.patch.object(backup_purge, "QUEUE_PATH", path):
            self.assertTrue(backup_purge.queue("abc123"))
            self.assertTrue(backup_purge.queue("t_def456"))   # t_ stripped
        self.assertEqual(open(path).read().split(),
                         ["abc123", "def456"])

    def test_bad_ids_never_reach_the_queue(self):
        """The queued string becomes a glob in a DELETE command — the one
        thing worse than keeping a deleted customer's backups is deleting
        a live customer's."""
        path = self._tmp()
        with mock.patch.object(backup_purge, "QUEUE_PATH", path):
            for bad in ("", "  ", "*", "a", "has space", "semi;colon",
                        "x" * 41, "../etc"):
                self.assertFalse(backup_purge.queue(bad), bad)
        self.assertFalse(os.path.exists(path))
        # An UPPERCASE id is normalised, not rejected — tenant ids are
        # lowercase by construction and lowering one is safe.
        with mock.patch.object(backup_purge, "QUEUE_PATH", path):
            self.assertTrue(backup_purge.queue("ABC123"))
        self.assertEqual(open(path).read().split(), ["abc123"])

    def test_queue_failure_never_blocks_erasure(self):
        with mock.patch.object(backup_purge, "QUEUE_PATH",
                               "/nonexistent-root/x/purge.queue"), \
             mock.patch.object(backup_purge.os, "makedirs",
                               side_effect=OSError("read-only")):
            self.assertFalse(backup_purge.queue("abc123"))   # loud, not raised


class BothDeletionPathsQueue(unittest.TestCase):
    def test_account_erasure_and_retention_purge_both_queue(self):
        """A dropped database whose dumps live on is the promise we must
        never break — so BOTH doors that end a tenant queue the backups."""
        import inspect
        from core import store
        self.assertIn("backup_purge.queue",
                      inspect.getsource(store.deprovision_tenant))
        # (the retention purge door was the cloud plane's lifecycle, deleted
        #  2026-09-02; brain-host owns the hosted ladder now)


if __name__ == "__main__":
    unittest.main()
