"""The split pass (core/splitpass): the guards are the applier's, the
judge only proposes; a kept record is untouched; a split writes the parts
with the original's origin and then deletes the original; a malformed or
lossy answer changes nothing."""
from __future__ import annotations

import contextlib

from core import curate_apply, healqueue, judge, splitpass, store, tools


class _Cur:
    def __init__(self, rows):
        self.rows = rows
    def execute(self, *a, **k):
        pass
    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, rows):
        self.rows = rows
    def cursor(self):
        return _Cur(self.rows)
    def commit(self):
        pass


def _wire(monkeypatch, rows, answers):
    class _L(list):
        queued: list = []
    written, deleted, asked = _L(), [], []
    monkeypatch.setattr(store, "bind", lambda t: "tok")
    monkeypatch.setattr(store, "unbind", lambda t: None)
    monkeypatch.setattr(store, "connection", lambda: contextlib.nullcontext(_Conn(rows)))
    monkeypatch.setattr(tools, "write_record",
                        lambda p, rid, t, title, content, dedup=True, origin=None:
                        written.append((rid, t, title, content, origin)) or "ok")
    monkeypatch.setattr(tools, "delete_memory", lambda p, rid: deleted.append(rid) or "DELETED")
    monkeypatch.setattr(curate_apply, "_known_projects", lambda conn: {"brain", "estate"})
    queued = []
    monkeypatch.setattr(healqueue, "enqueue", lambda conn, p, rid: queued.append((p, rid)) or 1)

    def ask(op, body, timeout=None):
        asked.append((op, body["record"]["record_id"], body.get("note", "")))
        a = answers[body["record"]["record_id"]]
        if isinstance(a, list):
            a = a[1] if body.get("note") else a[0]
        if isinstance(a, Exception):
            raise a
        return {"answer": a, "fault": "", "input_tokens": 10, "output_tokens": 5}
    monkeypatch.setattr(judge, "ask", ask)
    written.queued = queued
    return written, deleted, asked


LONG = "the box does one thing. " * 40          # ~1000 chars
ROW = lambda rid: (rid, "fact", "T " + rid, LONG, "2026-09-10", "2026-09-01", {"origin": {"session_id": "s", "turn_lo": 1, "turn_hi": 2}})


def test_a_split_writes_the_parts_with_the_origin_then_deletes_the_original(monkeypatch):
    parts = [{"type": "decision", "title": "The box", "content": "x" * 200},
             {"type": "gotcha", "title": "The hub", "content": "y" * 200, "project": "estate"},
             {"type": "fact", "title": "Nowhere", "content": "z" * 200, "project": "nosuch"}]
    w, d, asked = _wire(monkeypatch, [ROW("big")], {"big": {"reason": "three subjects", "records": parts}})
    c = splitpass.run("t", "brain", min_len=800)
    assert asked == [("split_judge", "big", "")] and c["split"] == 1
    assert [x[0] for x in w] == ["box_2026_09_01", "hub_2026_09_01", "nowhere_2026_09_01"]
    assert w[0][1] == "decision" and w[0][4] == {"session_id": "s", "turn_lo": 1, "turn_hi": 2}
    assert d == ["big"]
    assert w.queued == [("brain", "box_2026_09_01"), ("estate", "hub_2026_09_01"), ("brain", "nowhere_2026_09_01")]


def test_one_long_part_is_asked_again_with_a_note_and_refused_if_still_long(monkeypatch):
    long_one = {"reason": "one subject", "records": [{"type": "fact", "title": "All", "content": "a" * 950}]}
    two = {"reason": "two", "records": [{"type": "fact", "title": "Alpha", "content": "a" * 300},
                                        {"type": "fact", "title": "B", "content": "b" * 300}]}
    w, d, asked = _wire(monkeypatch, [ROW("big"), ROW("stubborn")],
                        {"big": [long_one, two], "stubborn": [long_one, long_one]})
    c = splitpass.run("t", "brain", min_len=800)
    assert [a[1] for a in asked] == ["big", "big", "stubborn", "stubborn"]
    assert asked[1][2].startswith("Your previous answer kept this as ONE record of 950")
    assert c["split"] == 1 and c["refused"] == 1 and [x[0] for x in w] == ["alpha_2026_09_01", "b_2026_09_01"]


def test_kept_refused_failed_and_dry_change_nothing(monkeypatch):
    rows = [ROW("keep"), ROW("lossy"), ROW("many"), ROW("bad"), ROW("down")]
    answers = {"keep": {"reason": "one subject", "records": []},
               "lossy": {"reason": "cut", "records": [{"type": "fact", "title": "A", "content": "a" * 100}]},
               "many": {"reason": "nine", "records": [{"type": "fact", "title": f"P{i}", "content": "p" * 200} for i in range(9)]},
               "bad": {"reason": "no list"},
               "down": judge.NoAnswer("the pipe loop is not running")}
    w, d, _ = _wire(monkeypatch, rows, answers)
    c = splitpass.run("t", "brain", min_len=800)
    assert c["kept"] == 1 and c["refused"] == 2 and c["failed"] == 2 and c["split"] == 0
    assert w == [] and d == []
    parts = [{"type": "fact", "title": "The box", "content": "x" * 400}]
    w, d, _ = _wire(monkeypatch, [ROW("big")], {"big": {"reason": "short", "records": parts}})
    c = splitpass.run("t", "brain", min_len=800, dry=True)
    assert c["split"] == 1 and w == [] and d == []


def test_the_pass_has_a_route_and_a_hub_op():
    src = open("api/main.py", encoding="utf-8").read()
    assert '@app.post("/v1/tools/split-pass")' in src and "require_full_access" in src.split('"/v1/tools/split-pass"')[1][:400]
    assert "split_judge" in judge.OPS


def test_a_part_that_reuses_the_originals_id_is_not_deleted_with_it(monkeypatch):
    row = ("box_2026_09_01", "fact", "The box", LONG, "2026-09-10", "2026-09-01", {})
    parts = [{"type": "fact", "title": "The box", "content": "x" * 300},
             {"type": "fact", "title": "The hub", "content": "y" * 300}]
    w, d, _ = _wire(monkeypatch, [row], {"box_2026_09_01": {"reason": "two", "records": parts}})
    c = splitpass.run("t", "brain", min_len=800)
    assert [x[0] for x in w] == ["box_2026_09_01", "hub_2026_09_01"] and d == [] and c["split"] == 1
