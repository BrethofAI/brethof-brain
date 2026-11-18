"""A model's JSON is mended, not thrown away (2026-09-09: an afternoon of bv
batches bought two to four times each because a quoted "said" carried a raw
quote)."""
import json

from core import inference


def test_a_raw_quote_inside_a_string_is_escaped_and_the_answer_parses():
    bad = '{"relations":[{"src":"a","kind":"uses","dst":"b","said":"he said "yes" and left"}]}'
    fixed = json.loads(inference.repair_json(bad))
    assert fixed["relations"][0]["said"] == 'he said "yes" and left'
    assert inference.parse_json_loose(bad)["relations"][0]["dst"] == "b"


def test_raw_newlines_and_trailing_commas_are_mended():
    bad = '{"known":["a","b",],"new":[{"name":"x","kind":"file","spellings":["x",]},],\n"relations":[{"src":"x","kind":"uses","dst":"a","said":"line one\nline two"}]}'
    obj = inference.parse_json_loose(bad)
    assert obj["known"] == ["a", "b"]
    assert obj["new"][0]["spellings"] == ["x"]
    assert obj["relations"][0]["said"] == "line one\nline two"


def test_valid_json_passes_through_unchanged():
    good = '{"a": "x, y: z", "b": [1, 2], "c": {"d": "q\\"uoted"}, "e": "tab\\tok"}'
    assert inference.repair_json(good) == good
    assert json.loads(good) == inference.parse_json_loose(good)


def test_the_graph_shape_is_salvaged_when_cut_off():
    cut = ('{"known":["brethof-voice","hub"],"new":[{"name":"x","kind":"file","spellings":["x"]},'
           '{"name":"y","kind":"tool","spellings":["y"]},{"name":"z","ki')
    assert inference.parse_json_loose(cut) is None
    got = inference.salvage_graph(cut)
    assert got["known"] == ["brethof-voice", "hub"]
    assert [n["name"] for n in got["new"]] == ["x", "y"]
    assert got["relations"] == [] and got["_truncated"] is True


def test_nothing_is_invented_from_prose():
    assert inference.salvage_graph("I could not find anything here.") is None
    assert inference.parse_json_loose("no json at all") is None


def test_the_fault_line_names_the_break_before_and_after_mending():
    bad = '{"a":"he said "yes", then "no" and left"}'
    line = inference.json_fault(bad)
    assert line.startswith("raw: ") and " at " in line and "near" in line
    assert inference.json_fault('{"a":"fine"}') == "raw: parses; mended: parses"
    assert inference.json_fault("nothing").startswith("no {...} span in the answer")
