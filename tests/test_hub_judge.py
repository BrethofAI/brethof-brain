"""Every judgement runs on the hub (design step 3, 2026-09-10): the box sends
data, the hub renders under its own prompt, gates, meters, parses and
answers data. The doors are handed in, so the tests hand fakes."""
import asyncio

from hub import judge, judgetext


class Doors:
    WINDOW_CLOSE_S = 300

    def __init__(self, text='{"things": [], "relations": []}', ok=True, cap=False, heal_over=False,
                 graph=True, window=0, why="provider"):
        self.calls = []
        self.metered = []
        self._text, self._ok, self._cap, self._heal_over, self._graph, self._window, self._why = \
            text, ok, cap, heal_over, graph, window, why

    async def infer(self, tenant, body, job="generate"):
        self.calls.append((job, body))
        if not self._ok:
            return {"ok": False, "why": self._why, "retry_s": 45}
        return {"ok": True, "text": self._text, "input_tokens": 100, "output_tokens": 20}

    def meter(self, tid, op, i, o):
        self.metered.append(op)

    def window_closed(self):
        return self._window

    def graph_allowed(self, tenant):
        return self._graph

    async def over_cap(self, tenant):
        return self._cap

    async def heal_over_allowance(self, tenant):
        return self._heal_over


T = {"id": "t1", "name": "one"}
STRETCH = {"project": "brain", "session_id": "s", "turn_lo": 0, "turn_hi": 3, "text": "[user] the sentinel moved to adm",
           "known": [{"name": "adm", "kind": "host", "aliases": ["adm-cf"], "gloss": "the admin box"}],
           "kinds": [["host", 3], ["service", 2]]}


def test_a_graph_read_renders_the_stretch_under_the_graph_prompt_and_answers_data():
    d = Doors(text='{"things": [{"name": "sentinel", "kind": "service"}], "relations": []}')
    r = asyncio.run(judge.graph_read(d, T, "the memory product", STRETCH))
    assert r["ok"] and r["answer"]["things"][0]["name"] == "sentinel" and r["fault"] == ""
    assert r["input_tokens"] == 100 and d.metered == [], "a graph READ is paid at the exchange door"
    job, body = d.calls[0]
    assert job == "graph" and "KNOWN HERE" in body["prompt"] and '"adm" [host]' in body["prompt"]
    assert "also written: adm-cf" in body["prompt"] and "the sentinel moved to adm" in body["prompt"]
    assert "the memory product" in body["system"] and body["think"] is False


def test_the_graph_gates_the_add_on_the_window_and_the_cap():
    off = asyncio.run(judge.graph_read(Doors(graph=False), T, "", STRETCH))
    assert not off["ok"] and off["off"] and "add-on" in off["why"]
    closed = asyncio.run(judge.graph_read(Doors(window=120), T, "", STRETCH))
    assert not closed["ok"] and closed["retry_s"] == 120 and "window" in closed["why"]
    capped = asyncio.run(judge.graph_read(Doors(cap=True), T, "", STRETCH))
    assert not capped["ok"] and "allowance" in capped["why"]
    assert not asyncio.run(judge.graph_read(Doors(), T, "", {"project": "brain", "text": ""}))["ok"]


def test_an_unparseable_answer_is_said_and_the_tokens_still_counted():
    d = Doors(text="I cannot tell")
    r = asyncio.run(judge.graph_read(d, T, "", STRETCH))
    assert r["ok"] and r["answer"] is None and r["fault"] and r["input_tokens"] == 100


def test_a_provider_refusal_carries_the_windows_wait():
    d = Doors(ok=False, why="provider window closed")
    r = asyncio.run(judge.graph_read(d, T, "", STRETCH))
    assert not r["ok"] and r["retry_s"] == 45 and d.metered == []


def test_the_fold_heal_and_proposer_ask_under_their_own_prompts_and_gates():
    cands = [{"name": "Sentinel", "kind": "service", "mentions": 1, "aliases": [], "gloss": "",
              "near": [["sentinel", "service", 9]], "near_gloss": {}}]
    d = Doors(text='{"folds": [{"absorbed": "Sentinel", "into": "sentinel"}]}')
    r = asyncio.run(judge.fold_judge(d, T, "p", {"project": "brain", "candidates": cands}))
    assert r["ok"] and r["answer"]["folds"] and d.calls[0][0] == "graph" and "NEW NAMES" in d.calls[0][1]["prompt"]
    recs = [{"record_id": "a", "type": "fact", "title": "A", "content": "the box"}]
    cs = [{"record_id": "b", "type": "fact", "title": "B", "content": "the same box", "upd": "2026-09-01", "why": "vector"}]
    d = Doors(text='{"ops": [{"op": "delete", "id": "a", "reason": "twin"}]}')
    r = asyncio.run(judge.heal_judge(d, T, "p", {"project": "brain", "records": recs, "candidates": cs, "body_ids": ["b"]}))
    assert r["ok"] and r["answer"]["ops"][0]["id"] == "a" and d.calls[0][0] == "heal" and d.metered == ["heal"]
    assert "WHAT IS ALREADY STORED" in d.calls[0][1]["prompt"] and "the same box" in d.calls[0][1]["prompt"]
    over = asyncio.run(judge.heal_judge(Doors(heal_over=True), T, "p", {"project": "brain", "records": recs, "candidates": []}))
    assert not over["ok"] and "heal allowance" in over["why"]
    d = Doors(text='{"project": "estate", "confidence": 0.7, "why": "adm work"}')

    async def purposes(name):
        return {"brain": "the memory product", "estate": "the boxes"}.get(name, "")
    r = asyncio.run(judge.propose(d, T, purposes, {"filed": "brain", "session_id": "s", "projects": ["brain", "estate"], "text": "we moved adm"}))
    assert r["ok"] and r["answer"]["project"] == "estate" and d.calls[0][0] == "heal"
    assert "- estate: the boxes" in d.calls[0][1]["prompt"] and "FILED UNDER: brain" in d.calls[0][1]["prompt"]


def test_the_hub_dispatches_the_judgements_and_serves_no_prompt_and_no_relay():
    src = open("hub/server.py", encoding="utf-8").read()
    assert "elif op in compose.OPS or op in judge.OPS:" in src
    assert '@app.post("/v1/heal")' not in src and '@app.get("/v1/prompt/{kind}")' not in src
    assert "TICKETS" not in src and "SERVABLE_PROMPTS" not in src
    assert set(judge.OPS) == {"graph_read", "fold_judge", "heal_judge", "heal_cluster", "split_judge", "propose", "movement_judge", "gloss_judge", "fact_close_judge", "takeover_judge", "same_judge", "same_family", "describe", "curate_probe", "decision_check", "curate_window", "retire_judge"}
    assert judgetext.heal_system("").count("{") == judgetext.heal_system("").count("}")


def test_the_split_judgement_shows_one_whole_record_under_its_own_prompt():
    rec = {"record_id": "a", "type": "fact", "title": "A", "content": "the box; the hub", "upd": "2026-09-01"}
    d = Doors(text='{"reason": "two subjects", "records": [{"type": "fact", "title": "The box", "content": "the box"}]}')
    r = asyncio.run(judge.split_judge(d, T, "p", {"project": "brain", "record": rec}))
    assert r["ok"] and r["answer"]["records"][0]["title"] == "The box" and d.calls[0][0] == "heal"
    assert "THE RECORD" in d.calls[0][1]["prompt"] and "the box; the hub" in d.calls[0][1]["prompt"]
    assert "SPLIT one curated record" in d.calls[0][1]["system"] and "several subjects" in d.calls[0][1]["system"].lower()
    assert not asyncio.run(judge.split_judge(d, T, "p", {"project": "brain"}))["ok"]
    d = Doors(text='{"reason": "x", "records": []}')
    asyncio.run(judge.split_judge(d, T, "p", {"project": "brain", "record": rec, "note": "too long"}))
    assert "NOTE: too long" in d.calls[0][1]["prompt"]
    over = asyncio.run(judge.split_judge(Doors(heal_over=True), T, "p", {"project": "brain", "record": rec}))
    assert not over["ok"]
