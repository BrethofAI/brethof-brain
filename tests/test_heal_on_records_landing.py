"""Heal starts where the records LAND, on both curation paths.

Founder 2026-09-01: "the heal will start straight away when the new curated
records are saved." The trigger that shipped on 2026-08-31 fired at the
ARCHIVE FLUSH instead — before the hub had written anything back — so it could
only ever heal the exchange before the current one, and on a session that
never compacts the last exchange's records were never read at all.

Records land in exactly two places: down the pipe (the hub curated them) and
at the end of a local curate job (this box curated them). Both must fire, and
both must stay free when there is nothing to heal.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import pipeclient, pipecommands  # noqa: E402


def _store():
    """A store whose connection() is a no-op context manager."""
    st = mock.MagicMock()
    st.connection.return_value.__enter__.return_value = mock.MagicMock()
    st.connection.return_value.__exit__.return_value = False
    return st


class PipeLanding(unittest.TestCase):
    """The hub curated it; the pipe wrote it down. Heal reads it now."""

    def _run(self, records, mode="brethof", n_rec=2):
        trigger = mock.MagicMock()
        with mock.patch.object(pipecommands, "store", _store()), \
                mock.patch("core.curate_apply.apply_records",
                           return_value=(n_rec, None)), \
                                mock.patch("core.reconcile.trigger", trigger):
            out = pipeclient._apply_save(
                "acme", {"project": "brain", "records": records})
        return out, trigger

    def test_records_landing_fires_heal_for_that_project(self):
        out, trigger = self._run([{"record_id": "r1", "title": "t"}])
        self.assertTrue(out["ok"])
        trigger.assert_called_once_with("acme", "brain")

    def test_a_save_with_no_records_does_not_fire(self):
        # Entities-only or ledger-only saves seed nothing: the watermark
        # would make the run empty anyway, so it must not cost a trigger.
        _, trigger = self._run([], n_rec=0)
        trigger.assert_not_called()


    def test_a_failing_heal_never_breaks_the_write_loop(self):
        # The pipe carries the customer's saves. Heal is an enhancement on
        # this path and must never be able to fail the write that preceded it.
        trigger = mock.MagicMock(side_effect=RuntimeError("hub down"))
        with mock.patch.object(pipecommands, "store", _store()), \
                mock.patch("core.curate_apply.apply_records",
                           return_value=(1, None)), \
                                mock.patch("core.reconcile.trigger", trigger):
            out = pipeclient._apply_save(
                "acme", {"project": "brain",
                         "records": [{"record_id": "r1", "title": "t"}]})
        self.assertTrue(out["ok"])
        self.assertEqual(out["records"], 1)


if __name__ == "__main__":
    unittest.main()
