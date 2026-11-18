"""ONE MESSAGE PER EXCHANGE, THE GRAPH INCLUDED (founder 2026-10-03; until
then the graph add-on took a second unit at this door, 2026-09-10). The
reading itself charges nothing; uncapped tiers (ours) pay nothing."""
import asyncio

from hub import judge


def test_the_exchange_door_takes_one_unit_with_the_graph_included():
    src = open("hub/server.py", encoding="utf-8").read()
    door = src.split('@app.post("/v1/exchange/{project}")', 1)[1].split("\nasync def exchange_worker", 1)[0]
    assert "take_units(tenant, units)" in door
    assert '"graph", 0, 0' not in door and "units * 2" not in door
    assert '"units": units' in door
    from hub import server
    assert server.graph_allowed({"tier": "free", "plan": "hosted"}) is True


def test_a_graph_read_and_the_fold_meter_tokens_but_no_unit_heal_keeps_its_count():
    class Doors:
        WINDOW_CLOSE_S = 300

        def __init__(self):
            self.metered = []

        async def infer(self, tenant, body, job="generate"):
            return {"ok": True, "text": '{"things": [], "relations": []}', "input_tokens": 5, "output_tokens": 1}

        def meter(self, tid, op, i, o):
            self.metered.append(op)

        def window_closed(self):
            return 0

        def graph_allowed(self, tenant):
            return True

        async def over_cap(self, tenant):
            return False

        async def heal_over_allowance(self, tenant):
            return False
    d = Doors()
    r = asyncio.run(judge.graph_read(d, {"id": "t"}, "", {"project": "p", "session_id": "s", "turn_lo": 0,
                                                            "turn_hi": 1, "text": "hello there"}))
    assert r["ok"] and d.metered == []
    cands = [{"name": "A", "kind": "x", "mentions": 1, "aliases": [], "near": [["a", "x", 2]], "near_gloss": {}}]
    asyncio.run(judge.fold_judge(d, {"id": "t"}, "", {"project": "p", "candidates": cands}))
    assert d.metered == [], "the fold rides inside the second unit (founder 2026-09-10)"
    recs = [{"record_id": "a", "type": "fact", "title": "A", "content": "x"}]
    asyncio.run(judge.heal_judge(d, {"id": "t"}, "", {"project": "p", "records": recs, "candidates": []}))
    assert d.metered == ["heal"]


def test_the_packs_are_spent_beyond_the_cap_for_the_doubled_units():
    """A capped tenant with the graph on sends one exchange at the edge of
    its cap: the exchange's second unit lands beyond it and comes out of
    the packs — the same take_units the door always used."""
    from unittest import mock
    from hub import server
    tenant = {"id": "t9", "name": "t_nine", "plan": "hosted", "tier": "rigtiny", "graph": True}   # cap 3
    spent = []

    async def fake_execute(sql, args=()):
        spent.append(args[0]); return 1

    async def month(tid):
        return 2

    async def packs(tid):
        return 5
    with mock.patch.object(server.store, "execute", fake_execute), \
         mock.patch.object(server, "month_units", month), \
         mock.patch.object(server, "packs_left", packs):
        assert asyncio.run(server.take_units(tenant, 2)) == ""
    assert spent == [1], "one unit inside the cap, one from the packs"
