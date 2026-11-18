"""The hub's money doors (founder 2026-09-03: "is anything left open?").
Pure pieces pinned here; the routes are exercised live after deploy."""
import os

import pytest
import time


def test_pipe_has_no_raw_infer_op():
    src = open("hub/server.py", encoding="utf-8").read()
    pipe = src.split('@app.websocket("/pipe")', 1)[1]
    assert 'op == "infer"' not in pipe, "the raw relay is back"
    assert 'op == "ping"' in pipe


def test_rule_units_one_per_rule_ten_per_project_birth():
    from hub import server
    assert server.rule_units("credential_filing") == 1
    assert server.rule_units("purpose_brain") == 10
    assert server.rule_units("") == 1


def test_bucket_refills_at_the_rate_and_caps_at_the_burst():
    from hub import server
    server._buckets.clear()
    now = time.time()
    assert all(server.bucket_ok("k", now, per_min=60, burst=3) for _ in range(3))
    assert not server.bucket_ok("k", now, per_min=60, burst=3)      # empty
    assert server.bucket_ok("k", now + 1.0, per_min=60, burst=3)     # one refilled
    assert not server.bucket_ok("k", now + 1.0, per_min=60, burst=3)
    assert server.bucket_ok("other", now, per_min=60, burst=3)       # per key


def test_heal_judgements_are_bounded_and_rule_saves_are_metered():
    j = open("hub/judge.py", encoding="utf-8").read()
    gate = j.split("async def _heal_gate(", 1)[1].split("\nasync def ", 1)[0]
    assert "hub.heal_over_allowance(tenant)" in gate and "hub.over_cap(tenant)" in gate
    src = open("hub/server.py", encoding="utf-8").read()
    put = src.split('@app.put("/v1/rules/{rule_id}")', 1)[1].split("@app.", 1)[0]
    assert "take_units(tenant, rule_units(rule_id))" in put and 'count=rule_units(rule_id)' in put


def test_cap_table_is_the_founders_numbers_one_list_for_both_shapes_plus_packs():
    from hub import server
    assert server.cap_for({"plan": "selfhosted", "tier": "starter"}) == 2000
    assert server.cap_for({"plan": "hosted", "tier": "starter"}) == 2000
    assert server.cap_for({"plan": "hosted", "tier": "pro"}) == 4000
    assert server.cap_for({"plan": "selfhosted", "tier": "team"}) == 10000
    assert server.cap_for({"plan": "selfhosted", "tier": "business", "packs": 1000}) == 20000   # packs are a balance, not cap
    assert server.cap_for({"plan": "hosted", "tier": "free"}) == 500
    assert server.cap_for({"plan": "selfhosted", "tier": "internal"}) is None
    assert server.cap_for({"plan": "hosted", "tier": "rigtiny"}) == 3



def test_packs_are_spent_only_beyond_the_cap_and_run_out():
    """A one-time pack (founder 2026-09-03: "you use the credit till it runs
    out"): units inside the month's cap never touch it; units beyond it come
    out of the balance; an empty balance refuses."""
    import asyncio
    from unittest import mock
    from hub import server
    tenant = {"id": "t1", "name": "t_test", "plan": "hosted", "tier": "rigtiny"}   # cap 3
    spent = []
    async def fake_execute(sql, args=()):
        spent.append(args[0]); return 1
    with mock.patch.object(server, "month_units", new=mock.AsyncMock(return_value=2)), \
         mock.patch.object(server, "packs_left", new=mock.AsyncMock(return_value=5)), \
         mock.patch.object(server.store, "execute", side_effect=fake_execute):
        assert asyncio.run(server.take_units(tenant, 1)) == ""        # 2+1 = cap, nothing beyond
        assert spent == []
        assert asyncio.run(server.take_units(tenant, 4)) == ""        # 2+4: three beyond the cap
        assert spent == [3]
    with mock.patch.object(server, "month_units", new=mock.AsyncMock(return_value=10)), \
         mock.patch.object(server, "packs_left", new=mock.AsyncMock(return_value=0)):
        assert "over monthly allowance" in asyncio.run(server.take_units(tenant, 1))
        assert asyncio.run(server.over_cap(tenant)) is True
    with mock.patch.object(server, "month_units", new=mock.AsyncMock(return_value=10)), \
         mock.patch.object(server, "packs_left", new=mock.AsyncMock(return_value=2)):
        assert asyncio.run(server.over_cap(tenant)) is False          # a balance keeps the doors open
    assert server.cap_for({"plan": "selfhosted", "tier": "internal"}) is None


def test_usage_flush_drops_only_the_dead_tenants_rows_and_writes_the_rest():
    """2026-09-03: one row for a torn-down tenant failed the whole batch with
    IntegrityError; the batch went back whole and nothing flushed for any
    tenant from 18:20 UTC on. Row by row: the dead row is dropped and named,
    the live rows land, a store outage keeps the rows."""
    import asyncio
    from unittest import mock
    from hub import server

    class IntegrityError(Exception):
        pass

    written = []

    async def execute(sql, args):
        if args[0] == "t_dead":
            raise IntegrityError("FOREIGN KEY constraint failed")
        if args[0] == "t_outage":
            raise RuntimeError("database is locked")
        written.append(args)

    batch = [("t_live", "infer:curate", 1, 10, 2), ("t_dead", "exchange", 1, 0, 0),
             ("t_outage", "exchange", 1, 0, 0), ("t_live", "exchange", 1, 0, 0)]
    with mock.patch.object(server.store, "execute", side_effect=execute), \
         mock.patch.object(server, "log") as log:
        w, d, kept = asyncio.run(server.flush_usage(batch))
    assert (w, d) == (2, 1) and kept == [("t_outage", "exchange", 1, 0, 0)]
    assert [a[0] for a in written] == ["t_live", "t_live"]
    said = " ".join(str(c) for c in log.call_args_list)
    assert "t_dead" in said and "no longer exists" in said and "will retry" in said


def test_health_says_whether_the_last_outbound_call_came_back():
    """2026-09-06→08: 45 hours with a dead resolver, 200 OK on every /health,
    provider_configured true throughout. The watcher polled it 540 times and
    saw nothing. The hub keeps the last outbound outcome now and says it."""
    from hub import server
    server.OUTBOUND.update(ok_at=0.0, err_at=0.0, err="", resolver=False)
    st = server.outbound_state()
    assert st["provider_ok"] and st["resolver_ok"] and st["outbound_err"] == ""
    server.outbound_err(ConnectionError("[Errno -3] Temporary failure in name resolution"))
    st = server.outbound_state()
    assert not st["provider_ok"] and not st["resolver_ok"]
    assert "name resolution" in st["outbound_err"] and st["outbound_err_at"]
    server.outbound_ok()
    st = server.outbound_state()
    assert st["provider_ok"] and st["resolver_ok"] and st["outbound_err"] == ""
    # a provider that answered badly is not a dead resolver: no restart for that
    server.outbound_err(RuntimeError("HTTP 502 from the provider"))
    st = server.outbound_state()
    assert not st["provider_ok"] and st["resolver_ok"]


@pytest.mark.skipif(not os.path.exists("deploy/hub/brain-hub.container"), reason="hosted deploy files are not in this tree")
def test_every_outbound_path_reports_and_the_container_heals_its_resolver():
    src = open("hub/server.py", encoding="utf-8").read()
    infer = src.split("async def infer(", 1)[1].split("\n# ----", 1)[0]
    assert "outbound_ok()" in infer and "outbound_err(e)" in infer
    pusher = src.split("async def usage_pusher(", 1)[1].split("\n# ----", 1)[0]
    assert "outbound_ok()" in pusher and "outbound_err(e)" in pusher
    health = src.split('@app.get("/health")', 1)[1].split("@app.", 1)[0]
    assert "outbound_state()" in health
    unit = open("deploy/hub/brain-hub.container", encoding="utf-8").read()
    assert "HealthCmd=python3 /app/hub/healthcheck.py" in unit
    assert "HealthOnFailure=kill" in unit and "Restart=always" in unit
    assert "COPY hub/ hub/" in open("hub/Dockerfile", encoding="utf-8").read()
    # The matching assertion about the fleet WATCHER — that it reads
    # reachability rather than configuration — moved to the Estate repo with
    # the watcher itself on 2026-09-12 (tests/test_sentinel_codify.py). A
    # product test cannot reach into the estate's source any more, and should
    # not: they are two systems that meet at an HTTP endpoint.


def test_the_box_says_when_it_has_gone_deaf_to_the_hub():
    """2026-08-23: 26 agent lanes fed nothing for a whole run on a podman DNS
    quirk while the Brain was healthy and /health said ok. The up-wire knew and
    told only its log. It publishes the outcome now."""
    from core import hubfeed
    hubfeed._WIRE.update(ok_at=0.0, err_at=0.0, err="", resolver=False)

    # no key: this box does not feed the hub, and that is not a fault
    key, hubfeed.HUB_KEY = hubfeed.HUB_KEY, ""
    try:
        st = hubfeed.state()
        assert st["hub_ok"] and st["resolver_ok"] and "no key" in st["hub_wire"]
    finally:
        hubfeed.HUB_KEY = key or "k"

    hubfeed.HUB_KEY = "k"
    assert hubfeed.state()["hub_ok"], "a wire that has not been used is not broken"
    hubfeed._mark_err(OSError("[Errno -3] Temporary failure in name resolution"))
    st = hubfeed.state()
    assert not st["hub_ok"] and not st["resolver_ok"] and st["hub_wire"] == "FAILING"
    assert "name resolution" in st["hub_err"] and st["hub_err_at"]
    # the hub answering 429 is the quota door, not a broken wire
    hubfeed._mark_ok()
    assert hubfeed.state()["hub_ok"]
    # a hub that is down is NOT this container's fault: no restart-worthy flag
    hubfeed._mark_err(ConnectionRefusedError("connection refused"))
    st = hubfeed.state()
    assert not st["hub_ok"] and st["resolver_ok"]
    hubfeed._WIRE.update(ok_at=0.0, err_at=0.0, err="", resolver=False)


@pytest.mark.skipif(not os.path.exists("deploy/hub/brain-hub.container"), reason="hosted deploy files are not in this tree")
def test_every_shipped_container_has_eyes_and_only_ours_has_hands():
    """The customer's box reports; it never restarts itself over a name
    lookup. Only our own hub kills itself, and a hosted tenant never does —
    that would lock a paying customer out of their memory."""
    import yaml
    d = yaml.safe_load(open("deploy/selfhosted/compose.yml", encoding="utf-8"))
    for svc in ("brain-db", "brain-api", "brain-embedder"):
        assert "healthcheck" in d["services"][svc], f"{svc} has no health command"
    api = open("api/healthcheck.py", encoding="utf-8").read()
    assert "resolver_ok" in api and "db" in api
    assert "COPY api/ ./api/" in open("Dockerfile", encoding="utf-8").read()
    host = open("deploy/hosted/host.py", encoding="utf-8").read()
    assert "HealthCmd=python3 /app/api/healthcheck.py" in host
    # the DIRECTIVE, not the word — the unit's own comment explains why it is
    # absent, and a substring test read that as the thing it forbids
    assert not [l for l in host.splitlines() if l.strip().startswith("HealthOnFailure")], \
        "a hosted tenant must never self-kill"
    hub = open("deploy/hub/brain-hub.container", encoding="utf-8").read()
    assert "HealthOnFailure=kill" in hub, "our own hub is the one that may"


def test_the_boxs_health_door_publishes_the_wire():
    src = open("api/main.py", encoding="utf-8").read()
    health = src.split("def health():", 1)[1].split("@app.", 1)[0]
    assert "hubfeed.state()" in health


def test_the_health_command_points_at_the_route_that_exists():
    """It was written against /health, which 404s — the door is /v1/health.
    A health command on the wrong path marks every container unhealthy
    forever, and this one ships to customers."""
    import re
    routes = set(re.findall(r'@app\.get\("([^"]+)"\)',
                            open("api/main.py", encoding="utf-8").read()))
    hc = open("api/healthcheck.py", encoding="utf-8").read()
    path = re.search(r'"http://127\.0\.0\.1:8610(/[^"]*)"', hc).group(1)
    assert path in routes, f"health command asks for {path}; routes are {sorted(routes)}"


def test_the_box_says_when_its_down_wire_is_dead():
    """A box whose pipe is down answers every other line of /v1/health
    perfectly while the hub drops everything it curates for it. The wire the
    hub writes down reports itself, the same way the up-wire does."""
    from core import pipeclient
    key, pipeclient.HUB_KEY = pipeclient.HUB_KEY, ""
    try:
        st = pipeclient.state()
        assert st["pipe_ok"] and "no key" in st["pipe"]
    finally:
        pipeclient.HUB_KEY = key or "k"

    pipeclient.HUB_KEY = "k"
    pipeclient._WIRE.update(up_at=0.0, down_at=0.0, err="", started=False)
    st = pipeclient.state()
    assert not st["pipe_ok"] and "never started" in st["pipe_err"], \
        "a pipe that was never started is not a healthy pipe"

    pipeclient._WIRE["started"] = True
    pipeclient._down(RuntimeError("the pipe loop returned"))
    st = pipeclient.state()
    assert not st["pipe_ok"] and st["pipe"] == "DOWN"
    assert "RuntimeError" in st["pipe_err"] and st["pipe_err_at"]
    pipeclient._up()
    st = pipeclient.state()
    assert st["pipe_ok"] and st["pipe"] == "up" and st["pipe_err"] == ""
    pipeclient._WIRE.update(up_at=0.0, down_at=0.0, err="", started=False)


def test_the_pipe_thread_cannot_end_silently():
    """A CancelledError — or anything else escaping asyncio.run — would end
    the thread and the box's only inbound wire with it, for the life of the
    process, with nothing said. Never observed; closed anyway."""
    src = open("core/pipeclient.py", encoding="utf-8").read()
    serve = src.split("async def _serve(", 1)[1].split("\ndef start(", 1)[0]
    assert "except BaseException" in serve, "the reconnect loop must catch everything"
    assert "except asyncio.CancelledError" in serve, "a real cancel must be recorded"
    run = src.split("def _run() -> None:", 1)[1].split("_WIRE[", 1)[0]
    assert "while True:" in run, "the loop must be rebuilt if asyncio.run ever returns"
    assert "except BaseException" in run
    health = open("api/main.py", encoding="utf-8").read().split("def health():", 1)[1]
    assert "pipeclient.state()" in health
