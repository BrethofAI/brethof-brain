"""The provider's window (2026-09-09): the hub keeps its state, refuses graph
work at the door with the seconds left, and the box's ask carries that
figure back — so a backfill never closes the window on the live curation
behind it. (The old graph reader's drain and its token ceiling left with
that graph, 2026-09-27.)"""
import pytest

from core import judge, pipeclient


# ── the hub's state ─────────────────────────────────────────────────────────
def test_a_limit_closes_the_window_and_consecutive_hits_double_it():
    from hub import server
    server.WINDOW.update({"until": 0.0, "hits": 0, "seen": 0.0, "why": ""})
    t = 1_000_000.0
    assert server.window_closed(t) == 0
    assert server.window_note("429", None, t) == server.WINDOW_CLOSE_S
    assert server.window_closed(t + 10) == server.WINDOW_CLOSE_S - 10
    # a second hit soon after doubles; a Retry-After in seconds is believed
    assert server.window_note("429", None, t + 60) == 2 * server.WINDOW_CLOSE_S
    assert server.window_note("429", "45", t + 100) >= 45
    assert server.WINDOW["until"] >= t + 60 + 2 * server.WINDOW_CLOSE_S      # never shortened
    # capped
    for _ in range(6):
        server.window_note("429", None, t + 200)
    assert server.window_closed(t + 200) <= server.WINDOW_CLOSE_MAX_S
    # a hit after a long quiet starts the ladder over
    assert server.window_note("timeout", None, t + 10 * server.WINDOW_CLOSE_MAX_S) == server.WINDOW_CLOSE_S
    server.WINDOW.update({"until": 0.0, "hits": 0, "seen": 0.0, "why": ""})


def test_the_hub_refuses_graph_work_at_the_door_while_closed_and_skips_the_ladder():
    src = open("hub/judge.py", encoding="utf-8").read()
    gate = src.split("async def _graph_gate(", 1)[1].split("\nasync def ", 1)[0]
    assert "hub.window_closed()" in gate and '"retry_s": int(w)' in gate and '"off": True' in gate
    server = open("hub/server.py", encoding="utf-8").read()
    infer = server.split("async def infer(", 1)[1].split("\n# ----", 1)[0]
    assert 'if job == "graph":' in infer and '"why": "provider window closed"' in infer
    assert "window_note(" in infer, "a 429 marks the window"
    # 2026-09-18: a graph call that ran out the clock does NOT close the window
    assert 'if ran_out and job == "graph":' in infer and 'window_note("429" if limited else "timeout"' not in infer
    assert "window_probe" in gate, "a closed window is probed before the box is told to wait"
    assert server_mod_window_defaults()
    call = src.split("async def _call(", 1)[1].split("\nasync def ", 1)[0]
    assert 'startswith("provider window")' in call and '"retry_s"' in call


def server_mod_window_defaults():
    from hub import server
    return server.WINDOW_CLOSE_S <= 60 and server.WINDOW_CLOSE_MAX_S <= 300 and server.WINDOW_PROBE_S <= 60


# ── the box's side ──────────────────────────────────────────────────────────
def test_the_ask_carries_the_hubs_wait(monkeypatch):
    monkeypatch.setattr(pipeclient, "ask", lambda op, body, timeout=None:
                        {"ok": False, "why": "provider window closed", "retry_s": 210})
    with pytest.raises(judge.NoAnswer) as e:
        judge.ask("curate_window", {})
    assert e.value.retry_s == 210 and not e.value.off
    # a plain refusal carries no wait; the add-on off says so
    monkeypatch.setattr(pipeclient, "ask", lambda op, body, timeout=None:
                        {"ok": False, "why": "the graph add-on is not on this plan", "off": True})
    with pytest.raises(judge.NoAnswer) as e2:
        judge.ask("curate_window", {})
    assert e2.value.retry_s == 0 and e2.value.off


def test_graph_work_holds_one_provider_slot_per_tenant_and_curation_none_of_it():
    from hub import server
    assert server.INFER_GRAPH_CONCURRENCY == 1
    src = open("hub/server.py", encoding="utf-8").read()
    infer = src.split("async def infer(", 1)[1].split("\n# ----", 1)[0]
    assert 'if job == "graph" else contextlib.nullcontext()' in infer
    assert "async with gsem, _GLOBAL_SEM, sem:" in infer, "the graph slot is taken before the shared ones"


