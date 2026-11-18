"""salvage_ops recovers the live curator's truncated output.

GLM engineering audit, 2026-08-09: a per-turn curation that hit the 65k
output ceiling mid-array parsed as nothing and silently dropped every op,
because the only salvage path knew the batch curator's {"records":...} shape,
not the live curator's {"ops":...}. Calibrated on the actual failure: a
truncated ops array must yield the complete ops before the cut.
"""
import unittest

from core import inference


class SalvageOps(unittest.TestCase):
    def test_truncated_ops_array_recovers_complete_objects(self):
        # a live-curator response cut off mid-third-op
        s = ('{"ops": [{"op":"create","id":"a","content":"one"},'
             '{"op":"update","id":"b","content":"two"},'
             '{"op":"create","id":"c","content":"thre')  # <-- truncated
        got = inference.salvage_ops(s)
        self.assertIsNotNone(got)
        ids = [o["id"] for o in got["ops"]]
        self.assertEqual(ids, ["a", "b"])   # the two that closed
        self.assertTrue(got["_truncated"])

    def test_recovers_entities_too(self):
        s = ('{"ops": [{"op":"create","id":"a","content":"x"}],'
             ' "entities": [{"name":"Foo","type":"tool"},'
             '{"name":"Bar","type":"conce')  # truncated
        got = inference.salvage_ops(s)
        self.assertEqual([o["id"] for o in got["ops"]], ["a"])
        self.assertEqual([e["name"] for e in got["entities"]], ["Foo"])

    def test_records_shape_is_not_salvaged_as_ops(self):
        # the OLD bug: a records-shaped payload has no ops -> returns None,
        # not a false empty success
        s = '{"records": [{"id":"x","content":"y"}]}'
        self.assertIsNone(inference.salvage_ops(s))

    def test_braces_inside_strings_do_not_break_the_scan(self):
        s = ('{"ops": [{"op":"create","id":"a","content":"has } and { braces"},'
             '{"op":"create","id":"b","content":"cut')
        got = inference.salvage_ops(s)
        self.assertEqual([o["id"] for o in got["ops"]], ["a"])

    def test_batch_records_salvage_still_works(self):
        s = '{"records": [{"id":"a","content":"one"},{"id":"b","content":"tw'
        got = inference.salvage_truncated(s)
        self.assertEqual([r["id"] for r in got["records"]], ["a"])


if __name__ == "__main__":
    unittest.main()
