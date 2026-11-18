"""The records rebuild writes no ledger and asks at most PARALLEL windows at once
(founder 2026-10-05: "when we're doing the backfill, we do not backfill ledger";
the 27B runs eight in parallel).

    python -m pytest tests/test_replay_noledger.py -q
"""
from __future__ import annotations

import threading
import time

from core import records_replay
from core.prompts import TEXTS


def test_noledger_text_is_production_minus_the_ledger():
    full, cut = TEXTS["batch_records"], TEXTS["batch_records_noledger"]
    assert "THEN THE LEDGER" in full and "THEN THE LEDGER" not in cut
    assert '"ledger":' in full and '"ledger":' not in cut
    # everything before the ledger paragraph is production's, word for word
    head = full[:full.index("THEN THE LEDGER")]
    assert cut.startswith(head)
    assert cut.rstrip().endswith('"ex":1}}]}}')


def test_noledger_text_renders_like_the_hub_renders_it():
    from hub import jobs
    out = jobs.render_brief(TEXTS["batch_records_noledger"], "p", "purpose", [], ())
    assert out.rstrip().endswith('"ex":1}]}')


def test_the_replay_asks_the_noledger_text(monkeypatch):
    from core import judge
    seen = {}

    def fake_ask(op, body, timeout=0):
        seen.update(body)
        return {"answer": {"records": [], "ledger": [{"content": "x"}]}}
    monkeypatch.setattr(judge, "ask", fake_ask)
    got, why = records_replay._ask("p", {"piece": "x", "n": 1, "next": []}, [], 1)
    assert why == "" and seen["records_prompt"] == "noledger" and seen["records_only"] is True


def test_a_ledger_row_sent_anyway_is_never_written(monkeypatch):
    from core import ledger
    wrote = []
    monkeypatch.setattr(ledger, "add", lambda *a, **k: wrote.append(a))
    n, touched = records_replay._save("p", "s", 0, 1, {"records": [], "ledger": [{"content": "do X later"}]},
                                      log=lambda m: None)
    assert wrote == [] and n == 0


def test_at_most_parallel_windows_ask_at_once(monkeypatch):
    from core import judge
    live, peak, lock = [0], [0], threading.Lock()

    def slow_ask(op, body, timeout=0):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.05)
        with lock:
            live[0] -= 1
        return {"answer": {"records": []}}
    monkeypatch.setattr(judge, "ask", slow_ask)
    monkeypatch.setattr(records_replay, "_GATE", threading.BoundedSemaphore(3))
    ts = [threading.Thread(target=records_replay._ask, args=("p", {"piece": "x", "n": 1, "next": []}, [], 1))
          for _ in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert peak[0] == 3, f"{peak[0]} asked at once with a gate of 3"


def test_the_replay_applies_what_jev_marked_overtaken(monkeypatch):
    from core import pipecommands
    applied = []
    monkeypatch.setattr(pipecommands, "apply_overtaken",
                        lambda project, sid, over, log: applied.append((project, sid, over)) or len(over))
    records_replay._save("p", "s1", 0, 4, {"records": [], "overtaken": [{"id": "r1", "op": "delete"}]},
                         log=lambda m: None)
    assert applied == [("p", "s1", [{"id": "r1", "op": "delete"}])]


def test_a_kept_database_is_not_dropped(monkeypatch):
    from core import bench
    monkeypatch.setattr(bench, "kept", lambda db: "records rebuild — keep")
    dropped = []

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, q, *a): dropped.append(q)
    import psycopg
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: Conn())
    out = bench.drop("tgt")
    assert "KEPT" in out and dropped == []
    bench.drop("tgt", force=True)
    assert any("DROP DATABASE" in q for q in dropped)


def test_the_replay_names_its_own_database_for_the_still_true_search(monkeypatch):
    from core import judge, store
    seen = {}
    monkeypatch.setattr(judge, "ask", lambda op, body, timeout=0: seen.update(body) or {"answer": {"records": []}})
    tok = store.bind("bench_rb1005")
    try:
        records_replay._ask("p", {"piece": "x", "n": 1, "next": []}, [], 1, session_id="s1")
    finally:
        store.unbind(tok)
    assert seen["replay_tenant"] == "bench_rb1005" and seen["session_id"] == "s1"


def test_the_box_searches_a_bench_database_only(monkeypatch):
    from core import hubwindow, pipecommands, store
    bound = []
    monkeypatch.setattr(hubwindow, "near_subjects", lambda cur, *a: bound.append(store.current_tenant()) or [])

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return None
        def rollback(self): pass
    monkeypatch.setattr(store, "connection", lambda: Conn())
    tok = store.bind("memory")
    try:
        body = {"project": "p", "session_id": "s", "queries": ["q"]}
        pipecommands.records_near_subjects("memory", {**body, "replay_tenant": "bench_rb1005"})
        pipecommands.records_near_subjects("memory", {**body, "replay_tenant": "someone_else"})
        pipecommands.records_near_subjects("memory", body)
    finally:
        store.unbind(tok)
    assert bound == ["bench_rb1005", "memory", "memory"], bound


def test_the_round_options_reach_the_hub(monkeypatch):
    from core import judge
    seen = {}
    monkeypatch.setattr(judge, "ask", lambda op, body, timeout=0: seen.update(body) or {"answer": {"records": []}})
    records_replay._ask("p", {"piece": "x", "n": 1, "next": []}, [], 1,
                        opts={"records_prompt": "noledgerD", "twin": True})
    assert seen["records_prompt"] == "noledgerD" and seen["twin"] is True
    seen.clear()
    records_replay._ask("p", {"piece": "x", "n": 1, "next": []}, [], 1)
    assert seen["records_prompt"] == "noledger" and "twin" not in seen


def test_the_sharper_text_differs_from_production_only_in_the_decision_bullet():
    full, d = TEXTS["batch_records_noledger"], TEXTS["batch_records_noledgerD"]
    assert "NEVER a decision record" in d and "NEVER a decision record" not in full
    assert full[:full.index("How one")] == d[:d.index("A choice about how one")]


def test_the_duplicate_judge_gives_a_twin_the_old_title():
    import asyncio
    from hub import judge, jev

    async def noul(state, questions):
        # pair p0 = new[0] vs old[0]: the same; everything else different
        return {q: (0.9 if q == "p0_same" else 0.1) for q in questions}, 10
    jev.noul, real = noul, jev.noul
    metered = []

    class Hub:
        def meter(self, *a): metered.append(a)
    try:
        new = [{"title": "Use 127.0.0.1 on Windows", "content": "localhost hangs"},
               {"title": "Something else", "content": "x"}]
        olds = [{"title": "Windows loopback: 127.0.0.1, not localhost", "content": "2 s IPv6 wait"}]
        n = asyncio.run(judge._twins(Hub(), {"id": "t"}, new, olds))
    finally:
        jev.noul = real
    assert n == 1 and new[0]["title"] == olds[0]["title"] and new[1]["title"] == "Something else"
    assert metered
