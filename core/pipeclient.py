"""THE PIPE, box side — the one wire the hub speaks down.

THE DESIGN, as it stands (founder 2026-09-10, design `brain-design-hub-brain`:
"the hub is the brain, our secret and everything; the worker which needs
power, CPU, RAM, drive is in the container"). The hooks deliver the
conversation UP to the hub; the hub composes what a session is told, judges
the graph, the fold, heal and the proposer, and speaks DOWN this pipe in a
FIXED vocabulary of commands — reads that return rows, writes that apply an
answer through the mechanical guards (core/pipecommands). Nothing free-form
travels down; an unknown command is refused by name. Until 2026-09-10 the
wire carried finished SAVES only (founder 2026-08-30: a question pattern was
held to be a blueprint of the loop readable from the customer's traffic);
the founder reversed that the day the readers moved onto the hub — the
box holds the memory and answers search, and the logic is no longer here to
read.

The box dials OUT (NAT), holds the pipe open, and reconnects with backoff
forever. Commands run concurrently under an in-flight cap and a deadline
(a slow save must not hold a recall); a command the box cannot answer in
time is answered late to nobody — the hub has already degraded to silence.
No pipe = the hub drops that exchange's results loudly and the hooks inject
nothing; the chat archive on this box still holds the conversation (the
stop hook writes it locally BEFORE the hub sees it), so nothing that
matters most is ever lost.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
import traceback

from . import store

HUB_URL = os.environ.get("BRAIN_HUB_URL", "https://hub.brethof.ai")
HUB_KEY = os.environ.get("BRAIN_HUB_KEY", "")
RECONNECT_MAX_S = 60.0
# How long the thread waits before rebuilding its whole event loop, if the loop
# itself ever ends. Longer than the reconnect backoff: this is the outer net,
# not the ordinary retry.
RELOOP_S = 30.0
# Commands in flight at once on this box, and the deadline each gets. The
# hub's side times out sooner for reads (HUB_CMD_TIMEOUT_S); the longer
# deadline here is for the writes, which must finish what they started.
INFLIGHT = int(os.environ.get("BRAIN_PIPE_INFLIGHT", "8"))
DEADLINE_S = float(os.environ.get("BRAIN_PIPE_CMD_DEADLINE_S", "30"))
SLOW_S = float(os.environ.get("BRAIN_PIPE_SLOW_S", "2"))
_LONG = frozenset({"save", "write_saves", "apply_updates"})

# ── WHETHER THE DOWN-WIRE IS ACTUALLY THERE ─────────────────────────────────
# THIS IS INSURANCE, NOT A POST-MORTEM. Written 2026-09-09 after the hub showed
# an 11.5-hour gap between `pipe down` and `pipe up` on the founder's own box.
# The first reading was that the thread had died; it had not. The workstation
# was SHUT DOWN at 05:23 and booted at 16:52 local, and the two events match
# the gap to the second — his machine, his evening, nothing broken. The
# founder said so before the check was run and the check proved him right.
#
# What survives that correction is a real hole, kept for its own sake rather
# than for a story: `except Exception` does not catch CancelledError or
# anything else deriving from BaseException, and if the loop below ever ended
# that way, `asyncio.run` would return, `_run` would return with it, and this
# daemon thread — the box's only inbound wire — would be gone for the life of
# the process with nothing said. That has never been observed. It is cheap to
# make impossible, so it is made impossible here.
#
# The state below earns its place on its own merit: a box whose pipe is down
# looks perfectly well from every other angle — it archives, it recalls, it
# answers /v1/health with ok — while the hub drops everything it curates for
# it. The up-wire says whether we are heard (core/hubfeed); this says whether
# we can be written to.
_WIRE = {"up_at": 0.0, "down_at": 0.0, "err": "", "started": False}
# THE UP-DIRECTION ASK (design step 2, the forward shape, founder 2026-09-10):
# the box's hook routes ask the hub for what the session is told —
# {rid, op: "brief" | "ambient", body} — and the hub answers by rid after
# composing through the commands it sends down this same pipe. The loop
# that owns the socket lives in the pipe thread; a sync caller hands its
# coroutine to that loop and waits. No pipe = PipeDown at once, never a
# hang: the hook has its own 12 s life.
_LOOP: asyncio.AbstractEventLoop | None = None
_WS = None
_PENDING: dict[str, asyncio.Future] = {}
ASK_TIMEOUT_S = float(os.environ.get("BRAIN_PIPE_ASK_TIMEOUT_S", "10"))


class PipeDown(RuntimeError):
    """The hub cannot be asked: no pipe, or no answer in time."""
# What the wire has been asked, for /v1/health and the log: answered, failed
# (a refusal or a late answer), and the last command by name.
_STATS = {"answered": 0, "failed": 0, "late": 0, "last": ""}


def _up() -> None:
    _WIRE["up_at"] = time.time()


def _down(e: BaseException) -> None:
    _WIRE["down_at"] = time.time()
    _WIRE["err"] = f"{type(e).__name__}: {str(e)[:160]}"


def state() -> dict:
    """What /v1/health publishes about the wire the hub speaks down.

    A box with no hub key does not have a pipe and is not broken. A box that
    has one and is NOT connected is broken, however healthy everything else
    looks — that is the whole point of saying it out loud."""
    if not HUB_KEY.strip():
        return {"pipe": "no key — this box has no hub pipe", "pipe_ok": True,
                "pipe_err": "", "pipe_err_at": ""}
    if not _WIRE["started"]:
        return {"pipe": "not started", "pipe_ok": False,
                "pipe_err": "the pipe thread was never started",
                "pipe_err_at": ""}
    ok = _WIRE["up_at"] > _WIRE["down_at"]
    return {"pipe": "up" if ok else "DOWN",
            "pipe_ok": ok,
            "pipe_err": "" if ok else _WIRE["err"],
            "pipe_err_at": "" if ok else time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(_WIRE["down_at"])),
            "pipe_cmds": dict(_STATS)}


def _pipe_url() -> str:
    base = HUB_URL.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    return base + "/pipe"


def _apply_save(tenant: str, body: dict) -> dict:
    """The curator's finished writes (kept under its old name for the tests
    and the callers that knew it here; the handler lives in pipecommands)."""
    from . import pipecommands
    return {"ok": True, **pipecommands.run(tenant, "write_saves", body)}


BIG_BYTES = 1024 * 1024        # a pipe message this large is said in the log with its name
PIPE_MAX_BYTES = int(os.environ.get("BRAIN_PIPE_MAX_BYTES", str(16 * 1024 * 1024)))


def _fail_pending(why: str) -> int:
    """THE PIPE DROPPED MID-ASK (2026-09-19): every ask in flight can never be
    answered — the hub lost the pipe it would answer down — yet each waiter
    sat out its whole timeout (900 s for a judgement) before saying "no
    answer". Twelve drops in fourteen hours, four graph lanes: hours of
    reading lost to waiting. Now a drop fails every pending ask at once;
    the caller retries on the new pipe."""
    n = 0
    for rid, fut in list(_PENDING.items()):
        if not fut.done():
            fut.set_exception(PipeDown(f"pipe dropped mid-ask: {why[:80]}"))
            n += 1
    return n


async def _ask(op: str, body: dict, timeout: float) -> dict:
    ws = _WS
    if ws is None or _WIRE["up_at"] <= _WIRE["down_at"]:
        raise PipeDown("no pipe to the hub")
    rid = hashlib.sha256(os.urandom(16)).hexdigest()[:24]
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _PENDING[rid] = fut
    try:
        raw = json.dumps({"rid": rid, "op": op, "body": body}, default=str)
        if len(raw) > BIG_BYTES:
            print(f"[pipe] BIG ask {op}: {len(raw):,} bytes", flush=True)
        await ws.send(raw)
        ans = await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        raise PipeDown(f"{op}: no answer from the hub in {timeout:.0f}s") from None
    except PipeDown:
        raise
    except Exception as e:                                       # noqa: BLE001
        raise PipeDown(f"{op}: {type(e).__name__}: {str(e)[:80]}") from e
    finally:
        _PENDING.pop(rid, None)
    if not isinstance(ans, dict):
        raise PipeDown(f"{op}: malformed answer")
    ans.pop("rid", None)
    return ans


def ask(op: str, body: dict, timeout: float | None = None) -> dict:
    """From sync code: one question up the pipe, the hub's answer back
    ({"ok": ..., ...}). Raises PipeDown when the hub cannot be asked."""
    loop = _LOOP
    if loop is None or not loop.is_running():
        raise PipeDown("the pipe loop is not running")
    t = timeout or ASK_TIMEOUT_S
    try:
        return asyncio.run_coroutine_threadsafe(_ask(op, body, t), loop).result(t + 2.0)
    except PipeDown:
        raise
    except Exception as e:                                       # noqa: BLE001
        raise PipeDown(f"{op}: {type(e).__name__}: {str(e)[:80]}") from e


def _resolve(msg: dict) -> bool:
    """A hub answer to one of our asks: hand it to its waiter. Late or
    unknown rids are dropped, never guessed at."""
    rid = msg.get("rid")
    fut = _PENDING.get(rid) if isinstance(rid, str) else None
    if fut is None or fut.done():
        return False
    fut.set_result(msg)
    return True


def deadline_for(op: str) -> float:
    return DEADLINE_S if op in _LONG else min(DEADLINE_S, 15.0)


async def answer_command(tenant: str, op: str, body: dict) -> dict:
    """One command, bounded: the handler runs in a thread under the tenant
    binding; a refusal, a failure or a late answer is a {"ok": False, "why"}
    the hub can read, never silence. A late handler keeps running in its
    thread (the wire cannot kill it) and its answer is dropped — said so in
    the log, counted in the stats."""
    from . import pipecommands
    if op not in pipecommands.HANDLERS:
        _STATS["failed"] += 1
        return {"ok": False, "why": "unknown op"}
    _STATS["last"] = op
    t0 = time.monotonic()
    try:
        data = await asyncio.wait_for(asyncio.to_thread(pipecommands.run, tenant, op, body),
                                      timeout=deadline_for(op))
    except asyncio.TimeoutError:
        _STATS["late"] += 1
        print(f"[pipe] {op}: no answer within {deadline_for(op):.0f}s — the hub has "
              f"moved on; the handler's late result is dropped", flush=True)
        return {"ok": False, "why": f"{op}: deadline {deadline_for(op):.0f}s"}
    except Exception as e:                                       # noqa: BLE001
        _STATS["failed"] += 1
        traceback.print_exc()
        return {"ok": False, "why": f"{type(e).__name__}: {e}"[:200]}
    _STATS["answered"] += 1
    if not isinstance(data, dict):
        data = {"data": data}
    data.pop("cid", None)
    return {"ok": True, "ms": round((time.monotonic() - t0) * 1000), **data}


async def _handle(ws, sem: asyncio.Semaphore, tenant: str, msg: dict) -> None:
    cid, op = msg.get("cid"), str(msg.get("op") or "")
    t0 = time.monotonic()
    async with sem:
        waited = time.monotonic() - t0
        ans = await answer_command(tenant, op, msg.get("body") or {})
    total = time.monotonic() - t0
    if total > SLOW_S:
        # WHERE THE TIME WENT (2026-11-17, recall slow under load): waiting for
        # one of the box's INFLIGHT command slots, or running the command
        print(f"[pipe] {op} slow: {total:.1f}s ({waited:.1f}s waiting for a slot of {INFLIGHT}, "
              f"{total - waited:.1f}s running)", flush=True)
    ans["cid"] = cid
    try:
        raw = json.dumps(ans, default=str)
        if len(raw) > BIG_BYTES:
            print(f"[pipe] BIG answer to {op}: {len(raw):,} bytes", flush=True)
        await ws.send(raw)
    except Exception:                                            # noqa: BLE001
        pass                    # the pipe died while we worked; the hub timed out


async def _serve(tenant: str) -> None:
    import websockets
    global _LOOP
    _LOOP = asyncio.get_running_loop()
    delay = 2.0
    while True:
        try:
            sem = asyncio.Semaphore(INFLIGHT)
            async with websockets.connect(
                    _pipe_url(),
                    additional_headers={"Authorization": f"Bearer {HUB_KEY}"},
                    max_size=PIPE_MAX_BYTES) as ws:
                print(f"[pipe] up: {_pipe_url()}", flush=True)
                global _WS
                _WS = ws
                _up()
                delay = 2.0
                tasks: set[asyncio.Task] = set()
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(msg, dict):
                        continue
                    if not msg.get("cid"):
                        _resolve(msg)           # an answer to one of our asks
                        continue
                    t = asyncio.create_task(_handle(ws, sem, tenant, msg))
                    tasks.add(t)
                    t.add_done_callback(tasks.discard)
        except asyncio.CancelledError:
            # Someone is genuinely shutting this loop down. Record it and let
            # it through — the thread's outer net decides whether to rebuild.
            _down(asyncio.CancelledError("loop cancelled"))
            _fail_pending("loop cancelled")
            print("[pipe] down (CancelledError) — the loop was cancelled",
                  flush=True)
            raise
        except BaseException as e:                           # noqa: BLE001
            # BaseException, not Exception: a CancelledError escaping here
            # would end the loop, and with it the box's only inbound wire, for
            # the life of the process. Never observed — closed anyway.
            _down(e)
            failed = _fail_pending(f"{type(e).__name__}: {e}")
            print(f"[pipe] down ({type(e).__name__}: {str(e)[:100]}) — "
                  f"{failed} ask(s) in flight failed at once; reconnect in {delay:.0f}s", flush=True)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_S)


def start() -> None:
    """Daemon thread with its own event loop; dies with the process."""
    if not HUB_KEY:
        print("[pipe] no BRAIN_HUB_KEY — pipe not started; the hub cannot "
              "reach this box", flush=True)
        return
    from . import mode
    tenant = mode.selfhosted_tenant() if mode.is_selfhosted() else ""
    if not tenant:
        tenants = store.provisioned_tenants()
        tenant = tenants[0] if tenants else ""
        if len(tenants) > 1:
            print(f"[pipe] WARNING: {len(tenants)} tenants on this box — "
                  f"pipe serves only '{tenant}'", flush=True)
    if not tenant:
        print("[pipe] no tenant provisioned yet — pipe not started",
              flush=True)
        return

    def _run() -> None:
        # THE OUTER NET. If `asyncio.run` ever returns — for any reason at
        # all, including one the loop inside could not catch — this thread
        # would end, and nothing restarts a dead pipe thread. Rebuild the loop
        # instead, and say out loud that it had to be rebuilt.
        while True:
            try:
                asyncio.run(_serve(tenant))
                _down(RuntimeError("the pipe loop returned"))
                print("[pipe] the loop ENDED without an error — rebuilding it",
                      flush=True)
            except BaseException as e:                       # noqa: BLE001
                _down(e)
                print(f"[pipe] the loop DIED ({type(e).__name__}: "
                      f"{str(e)[:120]}) — rebuilding it in {RELOOP_S:.0f}s",
                      flush=True)
                traceback.print_exc()
            time.sleep(RELOOP_S)

    _WIRE["started"] = True
    _down(RuntimeError("connecting"))
    threading.Thread(target=_run, name="hub-pipe", daemon=True).start()
    print(f"[pipe] started for tenant {tenant}", flush=True)
