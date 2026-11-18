"""core.reconcile.plain_ops — the heal round's op shapes (2026-09-29): a
"keep" verdict changes nothing; a "merge" becomes an update of the survivor
and a delete of every other id INTO it, so it can never mint a twin."""
from core.reconcile import plain_ops


def test_keep_leaves():
    assert plain_ops([{"op": "keep", "id": "a"}]) == []


def test_merge_with_text_updates_survivor_and_deletes_the_rest_into_it():
    out = plain_ops([{"op": "merge", "ids": ["a", "b", "c"], "keep": "b", "reason": "same",
                      "type": "fact", "title": "T", "content": "C"}])
    assert out[0] == {"id": "b", "op": "update", "reason": "same", "type": "fact", "title": "T", "content": "C"}
    assert [(o["id"], o["op"], o["into"]) for o in out[1:]] == [("a", "delete", "b"), ("c", "delete", "b")]


def test_merge_without_text_only_deletes():
    out = plain_ops([{"op": "merge", "ids": ["a", "b", "a"], "keep": "a"}])
    assert [(o["id"], o["op"], o["into"]) for o in out] == [("b", "delete", "a")]


def test_plain_ops_pass_through():
    ops = [{"op": "delete", "id": "x", "into": ""}, {"op": "update", "id": "y", "title": "t", "content": "c"}]
    assert plain_ops(ops) == ops
