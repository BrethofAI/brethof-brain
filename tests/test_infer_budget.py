"""The per-tenant inference budget (2026-11-11). Aurora's test tenant spent
the one provider key's five-hour window and four of the founder's own
curations failed on the 429s. A capped tenant past its tokens is refused
BEFORE the key is spent; a tenant without a row is never capped."""
import asyncio
import time

import pytest


@pytest.fixture
def hub(tmp_path, monkeypatch):
    from hub import server, store
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "hub.sqlite"))
    monkeypatch.setattr(store, "_conn", None)
    store.apply_schema(open("hub/schema.sql", encoding="utf-8").read())
    c = store.connect()
    for tid in ("t_cap", "t_free"):
        c.execute("INSERT INTO tenants (id, name, key_hash) VALUES (?, ?, ?)", (tid, tid, tid))
    c.execute("INSERT INTO infer_budget (tenant_id, tokens_5h) VALUES ('t_cap', 1000)")
    server.BUDGET_SAID.clear()
    yield server, store
    monkeypatch.setattr(store, "_conn", None)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_an_uncapped_tenant_is_never_refused_and_writes_nothing(hub):
    server, store = hub
    for _ in range(5):
        run(server.budget_spend("t_free", 10_000_000))
    assert run(server.budget_over("t_free")) == 0
    assert store.connect().execute("SELECT count(*) FROM infer_spend").fetchone()[0] == 0


def test_a_capped_tenant_is_refused_past_its_tokens_until_they_age_out(hub):
    server, store = hub
    run(server.budget_spend("t_cap", 600))
    assert run(server.budget_over("t_cap")) == 0
    run(server.budget_spend("t_cap", 600))
    wait = run(server.budget_over("t_cap"))
    assert 60 <= wait <= server.BUDGET_WINDOW_S + 1
    # five hours on, the spend has aged out
    store.connect().execute("UPDATE infer_spend SET ts = ts - ?", (server.BUDGET_WINDOW_S + 5,))
    assert run(server.budget_over("t_cap")) == 0


def test_infer_refuses_a_spent_tenant_without_calling_the_provider(hub, monkeypatch):
    server, _ = hub
    monkeypatch.setattr(server, "OLLAMA_KEY", "k")
    called = []

    class Boom:
        def __init__(self, *a, **k):
            called.append(1)
    monkeypatch.setattr(server.httpx, "AsyncClient", Boom)
    run(server.budget_spend("t_cap", 5000))
    ans = run(server.infer({"id": "t_cap", "name": "t_cap"}, {"prompt": "x"}))
    assert ans == {"ok": False, "why": "budget"}
    g = run(server.infer({"id": "t_cap", "name": "t_cap"}, {"prompt": "x"}, job="graph"))
    assert g["why"] == "budget" and g["retry_s"] >= 60
    assert called == []


def test_spend_older_than_the_window_is_pruned(hub):
    server, store = hub
    c = store.connect()
    c.execute("INSERT INTO infer_spend VALUES ('t_cap', ?, 1)", (time.time() - 6 * 3600,))
    run(server.budget_spend("t_cap", 1))
    assert c.execute("SELECT count(*) FROM infer_spend").fetchone()[0] == 1
