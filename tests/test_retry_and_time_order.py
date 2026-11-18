"""Two fixes of 2026-09-30 (founder: "do the rebuild in time order", "try
different seed").

A RETRY IS A NEW DRAW: every call is greedy, so asking the same prompt again
returned the same broken answer (ms 1ceb3777#4995: 57,557 characters, fifteen
times). The first ask stays greedy; each retry samples at RETRY_TEMPERATURE
with its own seed.

THE ORDER THINGS WERE SAID: a project's windows across all its sessions,
earliest first — never one whole session before the sessions beside it."""
import asyncio
import json

from core import backfill
from hub import judge
from tests.test_window_switch import Doors, T, WINDOW, REC, LINES


def _w(lo, *days):
    return {"lo": lo, "hi": lo + 1, "days": list(days)}


def _order(parts):
    return [(sid, w["lo"]) for sid, w in backfill.in_time_order(parts)]


def test_windows_of_sessions_side_by_side_go_in_the_order_they_were_said():
    long_ = ("A", [_w(0, "2026-07-01 10:00"), _w(10, "2026-07-10 09:00"), _w(20, "2026-07-20 08:00")])
    short = ("B", [_w(0, "2026-07-05 12:00"), _w(10, "2026-07-06 12:00")])
    assert _order([long_, short]) == [("A", 0), ("B", 0), ("B", 10), ("A", 10), ("A", 20)]


def test_a_session_never_goes_back_in_time_and_a_dayless_window_keeps_its_place():
    # A's second window carries an earlier minute than its first (the v1 era
    # stamped whole sessions alike) and its third has no day: A's own order holds
    a = ("A", [_w(0, "2026-07-03 10:00"), _w(10, "2026-07-02 10:00"), _w(20, "")])
    b = ("B", [_w(0, "2026-07-03 11:00")])
    assert _order([a, b]) == [("A", 0), ("A", 10), ("A", 20), ("B", 0)]


def test_a_tie_goes_to_the_session_that_began_first():
    a = ("A", [_w(0, "2026-07-01 10:00")])
    b = ("B", [_w(0, "2026-07-01 10:00")])
    assert _order([a, b]) == [("A", 0), ("B", 0)]


def test_the_first_ask_is_greedy_and_a_retry_is_a_new_draw():
    d = Doors({"curate": [REC, REC], "graph": [LINES, LINES]})
    asyncio.run(judge.curate_window(d, T, "the project", WINDOW))
    rec = d.calls[0][1]
    assert "temperature" not in rec and "seed" not in rec
    asyncio.run(judge.curate_window(d, T, "the project", dict(WINDOW, retry=3)))
    rec = d.calls[2][1]
    assert rec["temperature"] == judge.RETRY_TEMPERATURE and rec["seed"] == 45


def test_the_lines_call_asked_once_more_is_not_the_same_greedy_answer_again():
    d = Doors({"curate": REC, "graph": ["not json at all", LINES]})
    out = asyncio.run(judge.curate_window(d, T, "the project", WINDOW))
    first, second = d.calls[1][1], d.calls[2][1]
    assert "temperature" not in first
    assert second["temperature"] == judge.RETRY_TEMPERATURE and second["seed"] == 43
    assert out["answer"]["decisions"]


def test_the_probe_door_carries_the_retry_too():
    d = Doors({"graph": json.dumps({"decisions": []})})
    asyncio.run(judge.curate_probe(d, T, "the project", {"project": "p", "piece": "x", "prompt": "plines",
                                                         "json": True, "retry": 2}))
    assert d.calls[0][1]["temperature"] == judge.RETRY_TEMPERATURE and d.calls[0][1]["seed"] == 44
