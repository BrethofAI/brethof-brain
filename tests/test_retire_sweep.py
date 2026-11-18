"""THE RETIREMENT SWEEP (founder 2026-10-05: "something should be doing this
straight away"). A history line that drops or replaces a thing triggers it;
the hub's Jev flags the records still describing the thing as current, one
heal-text call rewrites or deletes only those, and the box applies the ops
only to records it sent — a preference never deleted. Hub side on fakes."""
import asyncio

import pytest

from core import retire
from hub import judge

T = {"id": "t1", "name": "box"}
RECS = [{"id": "graph_cards", "type": "fact", "title": "The inventory graph prints cards",
         "content": "Every session brief carries graph cards.", "day": "2026-09-20"},
        {"id": "embedder", "type": "decision", "title": "EmbeddingGemma serves embeddings",
         "content": "fp32 ONNX export.", "day": "2026-09-01"},
        {"id": "pref_cards", "type": "preference", "title": "Founder likes short cards",
         "content": "Cards stay short.", "day": "2026-09-02"}]
BODY = {"project": "brain", "today": "2026-09-27",
        "line": {"day": "2026-09-27", "kind": "dropped", "what": "The old inventory graph is removed",
                 "replaced": "", "why": "decision lines replace it"},
        "records": RECS, "later": [{"day": "2026-09-28", "kind": "decided", "what": "Lines are the graph"}]}


class Hub:
    WINDOW_CLOSE_S = 60

    def __init__(self, text):
        self.text, self.prompts, self.metered = text, [], []

    def meter(self, tid, op, i, o):
        self.metered.append(op)

    async def over_cap(self, t):
        return False

    async def heal_over_allowance(self, t):
        return False

    def window_closed(self):
        return 0

    async def infer(self, tenant, ask, job="generate"):
        self.prompts.append(ask)
        return {"ok": True, "text": self.text}


def _jev(monkeypatch, p):
    from hub import jev
    seen = []

    async def noul(state, questions):
        seen.append((state, questions))
        return {q: p.get(state["records"][q.split("_")[0]].split(" — ")[0], 0.1) for q in questions}, 50
    monkeypatch.setattr(jev, "noul", noul)
    return seen


def test_trigger_is_a_drop_or_a_replacement():
    assert retire.is_retirement({"kind": "dropped", "what": "x"})
    assert retire.is_retirement({"kind": "decided", "what": "x", "replaced": "the old way"})
    assert not retire.is_retirement({"kind": "decided", "what": "x", "replaced": "  "})
    assert not retire.is_retirement({"kind": "tried", "what": "x"})


def test_names_dedupe_and_skip_short():
    assert retire.names({"things": ["AGE", "ag", "Apache AGE", "age"], "replaced": "the cypher tool"}) == \
        ["AGE", "Apache AGE", "the cypher tool"]


def test_only_flagged_records_reach_the_judge_and_only_deletes_come_back(monkeypatch):
    seen = _jev(monkeypatch, {"The inventory graph prints cards": 0.9, "Founder likes short cards": 0.7})
    text = ('{"ops":[{"id":"graph_cards","op":"delete","reason":"cards are gone"},'
            '{"id":"embedder","op":"delete","reason":"not flagged"},'
            '{"id":"pref_cards","op":"delete","reason":"cards gone"},'
            '{"id":"graph_cards","op":"update","content":"a rewrite"},'
            '{"id":"pref_cards","op":"edit","cuts":[{"quote":"short","with":"long"}]}]}')
    hub = Hub(text)
    res = asyncio.run(judge.retire_judge(hub, T, "purpose", BODY))
    assert res["ok"]
    a = res["answer"]
    assert [f["id"] for f in a["flagged"]] == ["graph_cards", "pref_cards"]
    # the unflagged record, a preference, a rewrite and an edit never come back
    assert [(o["id"], o["op"]) for o in a["ops"]] == [("graph_cards", "delete")]
    state = seen[0][0]
    assert "inventory graph is removed" in state["retired"] and state["said_after"]
    user = hub.prompts[0]["prompt"]
    assert "graph_cards" in user and "pref_cards" in user and "id: embedder" not in user
    assert "SAID AFTER IT" in user and "lesson or a ruling" in hub.prompts[0]["system"]
    assert "infer:jev" in hub.metered


def test_nothing_flagged_asks_no_judge(monkeypatch):
    _jev(monkeypatch, {})
    hub = Hub('{"ops":[]}')
    res = asyncio.run(judge.retire_judge(hub, T, "purpose", BODY))
    assert res["ok"] and res["answer"]["ops"] == [] and hub.prompts == []


def test_jev_down_is_a_refusal_not_an_empty_sweep(monkeypatch):
    from hub import jev

    async def down(state, questions):
        return None, 0
    monkeypatch.setattr(jev, "noul", down)
    res = asyncio.run(judge.retire_judge(Hub('{"ops":[]}'), T, "purpose", BODY))
    assert not res["ok"] and res["why"] == "jev"


def test_box_deletes_only_what_it_sent_and_never_a_preference(monkeypatch):
    from core import heal, tools
    gone = []
    monkeypatch.setattr(heal, "_current_body", lambda p, rid: next(r["content"] for r in RECS if r["id"] == rid))
    monkeypatch.setattr(tools, "write_record", lambda *a, **k: pytest.fail("the sweep never writes a record"))
    monkeypatch.setattr(tools, "delete_memory", lambda p, rid: gone.append(rid) or "Deleted")
    log = []
    n = retire.apply("brain", [
        {"id": "pref_cards", "op": "delete"},
        {"id": "graph_cards", "op": "delete"},
        {"id": "graph_cards", "op": "delete"},                      # a second op on one id
        {"id": "stranger", "op": "delete"},                          # never sent
        {"id": "embedder", "op": "edit", "cuts": [{"quote": "fp32", "with": "int8"}]}], RECS, False, log)
    assert n == (1, 4) and gone == ["graph_cards"]
    assert "REFUSED delete of a preference: pref_cards" in log


def test_a_record_that_moved_since_it_was_shown_is_left(monkeypatch):
    from core import heal, tools
    monkeypatch.setattr(heal, "_current_body", lambda p, rid: "someone saved a newer text")
    monkeypatch.setattr(tools, "delete_memory", lambda p, rid: pytest.fail("deleted from a stale view"))
    log = []
    assert retire.apply("brain", [{"id": "graph_cards", "op": "delete"}], RECS, False, log) == (0, 1)
    assert "stale view" in log[-1]
