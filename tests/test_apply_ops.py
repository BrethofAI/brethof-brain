"""The op APPLIER's accounting — a report that cannot be trusted is worse than
none.

Found 2026-08-29 by reading a real heal log: a record absorbed in one batch
appeared in the next one's delete list, and both were counted as applied, so
ops_dropped said 0 while heal was demonstrably confused. Two causes, both
guarded here.

heal._apply is now shared by reconcile, registrar and judge — the fast pass it
was written for is gone (2026-09-01), and its watermark tests went with it,
because there is no watermark left to stamp.
"""
from unittest import mock

import core.heal as heal


def _apply(ops, shown, gone=None, current=None, res="Deleted"):
    log = []
    with mock.patch.object(heal.tools, "delete_memory", return_value=res), \
         mock.patch.object(heal.tools, "write_record", return_value="Saved"):
        a, d = heal._apply(ops, "brain", shown, False, log,
                           current=current, gone=gone)
    return a, d, log


def test_a_delete_that_deleted_nothing_is_not_an_applied_op():
    """delete_memory answers 'No record ... - nothing deleted.' which does not
    start with Error, so it was counted as work done."""
    a, d, log = _apply([{"op": "delete", "id": "x"}], {"x"},
                       res="No record brain:x — nothing deleted.")
    assert (a, d) == (0, 1)
    assert any("NO-OP" in line for line in log)


def test_a_real_delete_still_counts():
    a, d, _ = _apply([{"op": "delete", "id": "x"}], {"x"}, res="Deleted 1")
    assert (a, d) == (1, 0)


def test_an_id_absorbed_in_an_earlier_batch_is_refused():
    """Clusters overlap, so batch 5 can be offered a record batch 4 absorbed."""
    gone = {"x"}
    a, d, log = _apply([{"op": "delete", "id": "x"}], {"x"}, gone=gone)
    assert (a, d) == (0, 1)
    assert any("already absorbed" in line for line in log)


def test_an_UPDATE_into_an_absorbed_record_is_refused_and_that_is_the_point():
    """This is the one that loses data: folding new content into a row that no
    longer exists writes it nowhere and reports no error."""
    gone = {"survivor"}
    a, d, log = _apply([{"op": "update", "id": "survivor",
                         "content": "the merged body"}], {"survivor"},
                       gone=gone)
    assert (a, d) == (0, 1)
    assert any("already absorbed" in line for line in log)


def test_deletes_accumulate_into_gone_for_later_batches():
    gone = set()
    _apply([{"op": "delete", "id": "a"}], {"a"}, gone=gone, res="Deleted 1")
    assert gone == {"a"}


def test_two_ops_on_one_id_in_the_SAME_batch_still_refused():
    # the update carries a title (2026-09-27: an update without one is now
    # refused on its own — heal's empty-update guard)
    a, d, log = _apply([{"op": "update", "id": "x", "title": "t", "content": "new"},
                        {"op": "delete", "id": "x"}], {"x"})
    assert d == 1
    assert any("second op on same id" in line for line in log)


def test_a_batch_that_only_deletes_says_so():
    """Not an error — two identical records need no rewrite of the survivor.
    But a fold with nothing to fold into is the shape of silent loss, so it
    is never allowed to pass unremarked."""
    _, _, log = _apply([{"op": "delete", "id": "x"}], {"x"}, res="Deleted 1")
    assert any("UNPAIRED DELETES" in line for line in log)


def test_a_delete_paired_with_an_update_is_quiet():
    _, _, log = _apply([{"op": "update", "id": "s", "content": "folded body"},
                        {"op": "delete", "id": "x"}], {"s", "x"},
                       current={"s": "old body"}, res="Deleted 1")
    assert not any("UNPAIRED DELETES" in line for line in log)
