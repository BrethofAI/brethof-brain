"""Heal's answer is an "ops" list; the 27B mis-closes it (2026-09-24/25)."""
from hub.judge import salvage_ops


def test_nothing_to_do_with_the_closing_brace_missing_is_an_answer():
    assert salvage_ops('{"ops":[]') == {"ops": []}
    assert salvage_ops('{"ops": []') == {"ops": []}


def test_a_stray_closing_brace_is_mended():
    assert salvage_ops('{"ops":[]}}') == {"ops": []}


def test_an_answer_cut_off_mid_list_keeps_the_ops_that_closed():
    t = ('{"ops":[{"id":"a_1","reason":"older state","op":"delete","into":"b_2"},'
         '{"id":"b_2","reason":"carries it","op":"upd')
    v = salvage_ops(t)
    assert v["ops"] == [{"id": "a_1", "reason": "older state", "op": "delete", "into": "b_2"}]


def test_prose_is_still_no_answer():
    assert salvage_ops("I think nothing needs to change here.") is None
