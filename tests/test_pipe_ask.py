"""The box asks the hub over the pipe it holds open (design step 2, the
forward shape): a sync caller's question rides the pipe thread's loop and
comes back by rid; no pipe means PipeDown at once; a late answer is dropped."""
import asyncio
import json
import threading
import time

import pytest

from core import pipeclient


class _WS:
    def __init__(self):
        self.sent = []

    async def send(self, s):
        self.sent.append(json.loads(s))


@pytest.fixture
def pipe_loop():
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run():
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()
    t = threading.Thread(target=run, daemon=True)
    t.start()
    ready.wait(2)
    old = (pipeclient._LOOP, pipeclient._WS, dict(pipeclient._WIRE))
    pipeclient._LOOP, pipeclient._WS = loop, _WS()
    pipeclient._up()
    try:
        yield loop, pipeclient._WS
    finally:
        pipeclient._LOOP, pipeclient._WS = old[0], old[1]
        pipeclient._WIRE.update(old[2])
        loop.call_soon_threadsafe(loop.stop)


def test_an_ask_rides_the_loop_and_returns_the_hubs_answer_by_rid(pipe_loop):
    loop, ws = pipe_loop

    def hub():
        # the hub answers the first frame it sees, by its rid, after a beat
        for _ in range(50):
            if ws.sent:
                break
            time.sleep(0.02)
        m = ws.sent[0]
        loop.call_soon_threadsafe(pipeclient._resolve, {"rid": m["rid"], "ok": True, "text": "the brief"})
    threading.Thread(target=hub).start()
    ans = pipeclient.ask("brief", {"project": "p", "part": 1}, timeout=2.0)
    assert ans == {"ok": True, "text": "the brief"}
    assert ws.sent[0]["op"] == "brief" and ws.sent[0]["body"] == {"project": "p", "part": 1} and len(ws.sent[0]["rid"]) == 24
    assert not pipeclient._PENDING


def test_no_answer_is_pipedown_in_time_and_the_rid_is_forgotten(pipe_loop):
    t0 = time.monotonic()
    with pytest.raises(pipeclient.PipeDown):
        pipeclient.ask("ambient", {}, timeout=0.3)
    assert time.monotonic() - t0 < 2.0 and not pipeclient._PENDING
    assert pipeclient._resolve({"rid": "unknown", "ok": True}) is False


def test_no_pipe_is_pipedown_at_once():
    old = pipeclient._LOOP
    pipeclient._LOOP = None
    try:
        with pytest.raises(pipeclient.PipeDown):
            pipeclient.ask("brief", {})
    finally:
        pipeclient._LOOP = old


def test_the_hub_dispatches_the_two_asks_on_its_pipe_loop():
    src = open("hub/server.py", encoding="utf-8").read()
    pipe = src.split('@app.websocket("/pipe")', 1)[1]
    assert "elif op in compose.OPS or op in judge.OPS:" in pipe and "asyncio.create_task(answer(ws, rid, _serve_ask(" in pipe
    from hub import compose
    assert set(compose.OPS) == {"brief", "ambient"}


# ── a drop fails every ask in flight at once (2026-09-19) ────────────────────
def test_a_pipe_drop_fails_every_pending_ask_at_once_instead_of_waiting_it_out():
    """Twelve drops in fourteen hours, four graph lanes each waiting 900 s
    per stranded ask: hours of reading lost to waiting for answers the hub
    could never send down a pipe it no longer had."""
    async def run():
        loop = asyncio.get_running_loop()
        f1, f2 = loop.create_future(), loop.create_future()
        done = loop.create_future(); done.set_result({"ok": True})
        pipeclient._PENDING.update({"a": f1, "b": f2, "c": done})
        try:
            n = pipeclient._fail_pending("ConnectionClosedError: 1009")
            assert n == 2
            for f in (f1, f2):
                with pytest.raises(pipeclient.PipeDown) as e:
                    f.result()
                assert "dropped mid-ask" in str(e.value)
            assert done.result() == {"ok": True}
        finally:
            for k in ("a", "b", "c"):
                pipeclient._PENDING.pop(k, None)
    asyncio.run(run())
    src = open("core/pipeclient.py", encoding="utf-8").read()
    serve = src.split("async def _serve(", 1)[1]
    assert serve.count("_fail_pending(") >= 2, "both exits of the serve loop fail the waiters"
    assert "max_size=PIPE_MAX_BYTES" in serve and pipeclient.PIPE_MAX_BYTES >= 16 * 1024 * 1024
    assert "BIG ask" in src and "BIG answer" in src, "an oversized message is named in the log"


def test_the_hub_pipe_takes_sixteen_megabytes_and_answers_a_bigger_frame_instead_of_closing():
    hub = open("hub/server.py", encoding="utf-8").read()
    assert "ws_max_size=PIPE_MAX_BYTES" in hub
    assert 'if len(raw) > PIPE_MAX_BYTES:' in hub and '"frame too large"' in hub
    from hub import server
    assert server.PIPE_MAX_BYTES >= 16 * 1024 * 1024
