#!/usr/bin/env python3
"""brain-hub — the gate every prompt-bearing operation passes through.

THE SPLIT (decided 2026-08-27): recall and archive run on the customer's own
machine — mechanical, no prompt, never gated on us. Everything WITH a prompt
(curation, the rule filter, ledger, heal and graph relay, merge) happens behind
this hub, because that
is where the inference key lives and where judgment is spent. The customer's
container holds the memory; the hub never stores a byte of it.

TRANSPORT. Customer containers sit behind NAT, so the hub cannot dial them.
The container dials OUT — GET /pipe, WebSocket, held open — and both sides
talk over that one pipe:

  container -> hub   {"rid": <container-chosen>, "op": "infer", "body": {...}}
                     answered as {"rid", "ok", ...}. rid only routes back down
                     the SAME pipe, so the container naming it is safe.
  hub -> container   {"cid": <HUB-chosen>, "op": <command>, "body": {...}}
                     answered as {"cid", "ok", ...}. The container NEVER picks
                     a cid — measured in hublab (1,800 req, 0 cross-tenant):
                     hub-chosen correlation ids are what makes a misbehaving
                     container unable to steer an answer into another
                     customer's request. Late or unknown cids are DROPPED,
                     never guessed at. The commands are a FIXED vocabulary
                     (COMMANDS below; design brain-design-hub-brain, 2026-09-10):
                     reads that return rows, writes that apply an answer —
                     `command()` is the one door, bounded by a timeout and an
                     in-flight cap per tenant. Until 2026-09-10 only `save`
                     travelled down.

AUTH. Bearer tenant key on the /pipe handshake; sha256 of the key looked up in
hub-pg. The tenant identity comes from the REGISTRY ROW, never from anything
the container sends. One pipe per tenant; a reconnect replaces the old pipe
(that is a container restart, not an attack).

FAIL FAST, LOUDLY, AND DEGRADE. No pipe -> refuse immediately, never hang: an
answer after the deadline is worse than an empty answer on time. Provider down
-> the container hears "provider" and its local mechanical path carries on;
memory is an enhancement, never a gate. Metering that cannot reach hub-pg
buffers in memory and SHOUTS in the log — a usage row lost silently is how v1
under-reported 164x.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import hashlib
import json
import os
import time

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from core.prompts import texts_digest
from hub import compose, jobs, judge, models, store

# ---------------------------------------------------------------- config
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "https://ollama.com")
OLLAMA_KEY = os.environ.get("OLLAMA_API_KEY", "")
# comma-separated allowlist; FIRST entry is the default. The hub decides what
# models exist — a tenant asking for an arbitrary model is refused, because the
# bill lands on OUR key.
# The value that means "do not think" for the model this hub runs. Empty =
# use the native path with think:false, which is right for deepseek. Set it
# ("low" for glm-5.3) to route through /v1 with reasoning_effort instead.
REASONING_EFFORT = os.environ.get("HUB_REASONING_EFFORT", "").strip()
# THE PROVIDER'S DIALECT (2026-09-20, the graph bench on a local model):
# "ollama" (the default: /api/generate with think, /v1 with reasoning_effort
# when set) or "openai" — a vLLM stack, /v1/chat/completions only, thinking
# switched through the chat template (enable_thinking), the only switch
# vLLM's Qwen template honours (global:mandatory_vllm_config_enable_thinking).
PROVIDER_STYLE = os.environ.get("HUB_PROVIDER_STYLE", "ollama").strip().lower()
# A LOCAL OLLAMA'S CONTEXT (2026-09-20): its default window is 4k and a
# stretch is 8k — the first local probe read every prompt cut to 2,050
# tokens and named nothing. Ollama Cloud sizes the window itself; a local
# server takes it per request, here, when set.
NUM_CTX = int(os.environ.get("HUB_NUM_CTX", "0") or 0)
MODELS = [m.strip() for m in os.environ.get(
    "HUB_MODELS", "deepseek-v4-flash:cloud").split(",") if m.strip()]
INFER_TIMEOUT_S = float(os.environ.get("HUB_INFER_TIMEOUT_S", "600"))
# Jobs a waiting prompt depends on: one try, this clock (recall questions).
FAST_JOBS = frozenset({"recall_q", "decision_fast"})   # asked while a person waits
FAST_TIMEOUT_S = float(os.environ.get("HUB_FAST_TIMEOUT_S", "8"))
# Connection-class failures (no route, no DNS, reset) are retried across
# about a minute before a piece is given up; provider answers are never.
INFER_RETRY_S = (5, 15, 40)
# A provider rate limit (429) waits longer: about five minutes in all before
# a piece is dropped — a window that has just closed usually reopens sooner.
INFER_LIMIT_RETRY_S = (30, 60, 90, 120)
# THE PROVIDER'S WINDOW (2026-09-09). Ollama Cloud meters a rolling window:
# when it fills, answers first crawl (past the relay's ticket mark, then
# past INFER_TIMEOUT_S) and then 429. Twice that night a backfill drain
# spent ~3M tokens in an hour, closed the window, and every LIVE curate
# call after it ate the 429 ladder — an exchange dropped for good under the
# no-back-curation law — while the drain merely waited. So the hub keeps
# the window's state: a 429 (or a graph call that ran out the clock) marks
# it closed for WINDOW_CLOSE_S, doubling on consecutive hits up to
# WINDOW_CLOSE_MAX_S, or for what Retry-After says; while closed, GRAPH
# work is refused at the door with the seconds left — no provider call
# spent, the drain sleeps exactly that long — and curation keeps the
# ladder, because a message is worth the wait and a backfill batch is not.
# 2026-09-18 (founder: "the problem is your queue"): a 600-second graph call
# closed the window for 300 s, the next for 600, then 1,800 — while a
# five-token probe answered in 3.6 s. Four drains slept 25 minutes on a
# window the provider had long reopened. So: only a 429 closes the window
# (a graph call that ran out the clock is that batch's problem — the box
# sends it to the back of its queue); the close is a minute, doubling to
# five at most; and while closed, a graph ask PROBES the provider once a
# minute with a five-token call and reopens the window the moment it
# answers. The box re-asks every minute instead of sleeping the figure.
WINDOW_CLOSE_S = int(os.environ.get("HUB_WINDOW_CLOSE_S", "60"))
WINDOW_CLOSE_MAX_S = int(os.environ.get("HUB_WINDOW_CLOSE_MAX_S", "300"))
WINDOW_PROBE_S = int(os.environ.get("HUB_WINDOW_PROBE_S", "60"))
WINDOW: dict = {"until": 0.0, "hits": 0, "seen": 0.0, "why": "", "probed": 0.0}


def window_closed(now: float | None = None) -> int:
    """Seconds the provider's window stays closed from `now`; 0 when open."""
    now = time.time() if now is None else now
    return max(0, int(WINDOW["until"] - now + 0.999)) if WINDOW["until"] > now else 0


def window_note(why: str, retry_after: str | None = None,
                now: float | None = None) -> int:
    """Mark the window closed after a provider limit; returns the seconds.
    A Retry-After in seconds (1..3600) is believed; otherwise WINDOW_CLOSE_S,
    doubled per consecutive hit (a hit within twice the ceiling of the last
    one), capped at WINDOW_CLOSE_MAX_S. A hit after a long quiet starts over."""
    now = time.time() if now is None else now
    if now - WINDOW["seen"] > 2 * WINDOW_CLOSE_MAX_S:
        WINDOW["hits"] = 0
    WINDOW["hits"] += 1
    WINDOW["seen"] = now
    WINDOW["why"] = why
    secs = 0
    try:
        secs = int(float(retry_after or 0))
    except ValueError:
        secs = 0
    if not 1 <= secs <= 3600:
        secs = min(WINDOW_CLOSE_S * (2 ** (WINDOW["hits"] - 1)), WINDOW_CLOSE_MAX_S)
    WINDOW["until"] = max(WINDOW["until"], now + secs)
    return window_closed(now)


async def window_probe(now: float | None = None) -> bool:
    """While the window is closed, one five-token call per WINDOW_PROBE_S
    asks the provider whether it answers; a 200 reopens the window at once.
    True when the window is open after this call."""
    now = time.time() if now is None else now
    if not window_closed(now):
        return True
    if now - WINDOW["probed"] < WINDOW_PROBE_S:
        return False
    WINDOW["probed"] = now
    own = models.ai()
    base, key, model = ((own["url"], own["key"], own["model"]) if own
                        else (OLLAMA_HOST.rstrip("/") + "/v1", OLLAMA_KEY, MODELS[0]))
    payload = {"model": model, "messages": [{"role": "user", "content": "Say OK."}],
               "max_tokens": 5}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(base + "/chat/completions",
                                  json=payload,
                                  headers={"User-Agent": "brethof-brain-hub/1",
                                           **({"Authorization": f"Bearer {key}"} if key else {})})
            r.raise_for_status()
    except Exception as e:                                          # noqa: BLE001
        log(f"provider window probe: still closed ({type(e).__name__}: {str(e)[:80]})")
        return False
    left = window_closed(now)
    WINDOW.update({"until": 0.0, "hits": 0})
    log(f"provider window reopened by probe ({left}s early)")
    return True
# PER TENANT and GLOBAL (2026-09-03). The provider no longer sells lines:
# eight concurrent calls from this box all answered in 2.4 s, so the old
# 2-per-tenant throttle was only slowing curation and heal. Per tenant
# stays modest (a tenant's chain is FIFO anyway; this covers the rule
# filter's parallel gather and heal beside curation); the global cap is
# the fleet's ceiling against the provider's unpublished limits.
INFER_CONCURRENCY = int(os.environ.get("HUB_INFER_CONCURRENCY", "6"))
# THE RELAY HANDS OUT A TICKET PAST THIS MANY SECONDS (2026-09-09). The box
# waits 90 s for a judgment and the edge in front of the hub allows 100; the
# provider call here ran up to 600. Past the box's patience the answer was
# finished, metered and thrown away — and the box bought the same batch
# again. Now an answer that is not back in time is kept under a ticket the
# box collects; nothing is paid for twice and no answer is lost.
INFER_SLOW_S = float(os.environ.get("HUB_INFER_SLOW_S", "60"))
INFER_GLOBAL = int(os.environ.get("HUB_INFER_GLOBAL", "24"))
# ONE SLOT FOR GRAPH WORK PER TENANT (2026-09-09 23:42 UTC, the provider's own
# words: `{"error":"timed out waiting for a concurrent request slot"}`,
# Retry-After 19). The limit we kept calling a window is Ollama Cloud's
# concurrent-request slots, per account and shared with everything else on
# the plan. A drain, the live drain, heal's pool and the curator held five or
# six requests at once, and the curator's own call queued behind graph
# batches at the provider. So graph calls queue HERE, one in flight per
# tenant (the relay's ticket covers the wait); curation never waits behind
# a backfill for a slot.
INFER_GRAPH_CONCURRENCY = int(os.environ.get("HUB_INFER_GRAPH_CONCURRENCY", "1"))
GRAPH_SEMS: dict[str, asyncio.Semaphore] = {}
_GLOBAL_SEM: asyncio.Semaphore | None = None
MAX_BODY_BYTES = int(os.environ.get("HUB_MAX_BODY_BYTES", str(2 * 1024 * 1024)))
# THE PIPE'S FRAME (2026-09-19): a 4.9 MB message from the box hit the 2 MiB
# websocket limit, the server closed the pipe with 1009, and every ask in
# flight on it was lost — four graph lanes waited out 900 s each. The pipe
# takes 16 MiB; a frame over that is answered "frame too large" on the same
# pipe, never closed on.
PIPE_MAX_BYTES = int(os.environ.get("HUB_PIPE_MAX_BYTES", str(16 * 1024 * 1024)))
# Projects told once (per hub life) that they curate with no description.
BARE_SAID: set[tuple[str, str]] = set()
# The control plane, for the usage push (panel numbers + threshold emails).
CONTROL_URL = os.environ.get("MIND_CONTROL_URL",
                             "https://api.brethof.ai/v1").rstrip("/")
CONTROL_TOKEN = os.environ.get("MIND_INTERNAL_TOKEN", "")

app = FastAPI()

# tenant_id -> live pipe (one per tenant, reconnect replaces)
PIPES: dict[str, WebSocket] = {}
# cid -> future, for hub-initiated ops (curation flows, next phase)
WAITING: dict[str, asyncio.Future] = {}
# tenant_id -> semaphore capping concurrent infers on OUR key
SEMS: dict[str, asyncio.Semaphore] = {}
# usage rows that could not reach hub-pg; flushed by a background task
USAGE_BUFFER: list[tuple] = []
USAGE_BUFFER_CAP = 10_000


def _today() -> str:
    return _dt.date.today().isoformat()


def log(msg: str) -> None:
    print(f"[hub] {msg}", flush=True)


# ---------------------------------------------------------------- outbound
# THE HUB KNOWS WHEN IT IS CUT OFF, AND UNTIL 2026-09-08 IT TOLD NOBODY WHO
# ASKED. From 2026-09-06 15:49 to 09-08 13:14 the container's resolver was
# dead: the usage pusher failed on name resolution 537 times, every model call
# failed, no exchange was curated — and /health answered 200 with
# provider_configured:true the whole time, because that field only says a key
# is set. The fleet watcher polled it 540 times and saw nothing wrong. The log
# had the truth every five minutes; nothing reads a log.
#
# So the last outbound outcome is kept here, and /health says it. A name
# resolution failure is flagged apart from the rest because it is the one the
# container can heal by being restarted — the resolver is the pod network's,
# and a fresh container re-attaches to it — which is what the container's
# health check does (deploy/hub/brain-hub.container, hub/healthcheck.py).
OUTBOUND = {"ok_at": 0.0, "err_at": 0.0, "err": "", "resolver": False}


def _resolution_failure(e: BaseException) -> bool:
    msg = str(e)
    return any(m in msg for m in ("name resolution", "Name or service not known",
                                  "nodename nor servname", "[Errno -3]",
                                  "[Errno -2]"))


def outbound_ok() -> None:
    OUTBOUND["ok_at"] = time.time()


def outbound_err(e: BaseException) -> None:
    OUTBOUND["err_at"] = time.time()
    OUTBOUND["err"] = f"{type(e).__name__}: {str(e)[:160]}"
    OUTBOUND["resolver"] = _resolution_failure(e)


def outbound_state() -> dict:
    """provider_ok: the last thing that left this container came back. No
    attempt yet counts as ok — absence of evidence, said as such by err ''.
    resolver_ok: false only while the standing failure is name resolution."""
    ok = OUTBOUND["ok_at"] >= OUTBOUND["err_at"]
    return {"provider_ok": ok,
            "resolver_ok": ok or not OUTBOUND["resolver"],
            "outbound_err": "" if ok else OUTBOUND["err"],
            "outbound_err_at": "" if ok else time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(OUTBOUND["err_at"]))}


# ---------------------------------------------------------------- lifecycle
@app.on_event("shutdown")
async def _drain_on_stop() -> None:
    """A RESTART FINISHES WHAT IS QUEUED (2026-10-03): the exchange queues
    live in memory, so every deploy used to drop the windows still waiting.
    The stop waits up to STOP_DRAIN_S for the chains to empty (the unit gives
    it the time: StopTimeout in brain-hub.container) and names what it could
    not finish — a window held through a provider outage is not waited for."""
    t0 = time.time()
    while time.time() - t0 < STOP_DRAIN_S:
        left = sum(q.qsize() for q in EXQ.values())
        if not left and not EXBUSY:
            if t0 and time.time() - t0 > 1:
                log(f"stop: queues drained in {int(time.time() - t0)}s")
            return
        await asyncio.sleep(1)
    left = {t: q.qsize() for t, q in EXQ.items() if q.qsize()}
    log(f"ERROR: stop after {STOP_DRAIN_S}s with windows unfinished — queued {left}, "
        f"mid-window {sorted(EXBUSY)}; the box's archive keeps them")


@app.on_event("startup")
async def startup() -> None:
    if not OLLAMA_KEY and not os.environ.get("HUB_AI_URL"):
        # Not fatal: pipes and health still work, infer answers "provider"
        # until the owner sets a model on the settings page.
        log("no provider in the environment — the settings page's AI model is used once set")
    store.connect()
    await _ensure_schema()
    await models.load()
    _load_prompts()
    asyncio.create_task(usage_flusher())
    asyncio.create_task(usage_pusher())
    log(f"up — models={MODELS} concurrency={INFER_CONCURRENCY}/tenant, {INFER_GLOBAL} global "
        f"prompts={sorted(PROMPTS)}")


async def _ensure_schema() -> None:
    """Apply schema.sql on EVERY boot, not just the first.

    The postgres image runs docker-entrypoint-initdb.d exactly once, on an
    empty data directory. That is fine until you add a table: the deploy
    succeeds, the container is healthy, and the first request that touches the
    new table 500s in production. Measured 2026-08-28 — the rule table was
    added, deployed, and did not exist. Every statement here is IF NOT EXISTS,
    so running it always is free and removes a whole class of "worked on a
    fresh box, broke on the real one"."""
    try:
        with open("/app/hub/schema.sql", encoding="utf-8") as fh:
            ddl = fh.read()
    except OSError as e:
        log(f"WARNING: cannot read schema.sql ({e}) — schema not verified")
        return
    store.apply_schema(ddl)
    log("schema verified")


async def authenticate(ws: WebSocket) -> dict | None:
    """Bearer key -> tenant row, or None. The row is the identity."""
    auth = ws.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    digest = hashlib.sha256(auth[7:].strip().encode()).hexdigest()
    row = await store.fetchone(
        "SELECT id, name, plan FROM tenants"
        " WHERE key_hash = ? AND active = 1", (digest,))
    if row is None:
        return None
    return {"id": row[0], "name": row[1], "plan": row[2]}


# ---------------------------------------------------------------- metering
def meter(tenant_id: str, op: str, tokens_in: int, tokens_out: int,
          count: int = 1) -> None:
    USAGE_BUFFER.append((tenant_id, op, count, tokens_in, tokens_out))
    if len(USAGE_BUFFER) > USAGE_BUFFER_CAP:
        # Shed oldest and SAY SO — losing usage silently is the v1 failure.
        del USAGE_BUFFER[: len(USAGE_BUFFER) - USAGE_BUFFER_CAP]
        log("ERROR: usage buffer overflow — oldest rows dropped")


USAGE_SQL = ("INSERT INTO usage_daily (tenant_id, day, op, calls,"
             " tokens_in, tokens_out)"
             " VALUES (?, date('now'), ?, ?, ?, ?)"
             " ON CONFLICT (tenant_id, day, op) DO UPDATE SET"
             " calls = usage_daily.calls + EXCLUDED.calls,"
             " tokens_in = usage_daily.tokens_in + EXCLUDED.tokens_in,"
             " tokens_out = usage_daily.tokens_out + EXCLUDED.tokens_out")


async def flush_usage(batch: list) -> tuple[int, int, list]:
    """Write a batch row by row. A row whose tenant no longer exists (the
    tenant was torn down while its last calls sat in the buffer — every
    rig and gate run does this) is DROPPED and named; any other failure
    keeps the row for the next tick. Returns (written, dropped, kept_rows).

    Measured 2026-09-03: one such row failed the whole batch with
    IntegrityError, the batch went back whole, and the flusher wrote
    NOTHING for any tenant from 18:20 UTC on — 421 rows and growing, the
    meter and the caps blind, and a restart would have lost them all.
    """
    written = dropped = 0
    kept = []
    for row in batch:
        tid, op, n, tin, tout = row
        try:
            await store.execute(USAGE_SQL, (tid, op, n, tin, tout))
            written += 1
        except Exception as e:
            if "FOREIGN KEY" in str(e) or type(e).__name__ == "IntegrityError":
                dropped += 1
                log(f"usage: dropped {n} {op} for {tid} — tenant no longer exists")
            else:
                kept.append(row)
    if kept:
        log(f"ERROR: usage flush failed for {len(kept)} rows — buffered, will retry")
    return written, dropped, kept


async def usage_flusher() -> None:
    while True:
        await asyncio.sleep(15)
        if not USAGE_BUFFER:
            continue
        batch, USAGE_BUFFER[:] = USAGE_BUFFER[:], []
        _, _, kept = await flush_usage(batch)
        if kept:
            USAGE_BUFFER[:0] = kept


async def usage_pusher() -> None:
    """Every 5 minutes: month-to-date message units per capped tenant, up to
    the control plane's /internal/mind/usage — the same door the old data
    plane fed, so the panel numbers, the notify thresholds and the wall
    emails all keep working unchanged. The hub owns usage truth; the
    control plane owns the email address and the customer's threshold."""
    if not CONTROL_TOKEN:
        log("WARNING: MIND_INTERNAL_TOKEN unset — usage never reaches the "
            "panel and threshold emails never fire")
        return
    while True:
        await asyncio.sleep(300)
        try:
            rows = await store.fetchall(
                "SELECT t.id, t.name, t.tier, t.plan, t.packs, "
                "COALESCE(SUM(u.calls), 0) "
                "FROM tenants t JOIN usage_daily u ON u.tenant_id = t.id "
                "WHERE t.active = 1 AND u.op = 'exchange' "
                "AND u.day LIKE strftime('%Y-%m-', 'now') || '%' "
                "GROUP BY t.id")
            period = _dt.date.today().strftime("%Y-%m")
            async with httpx.AsyncClient(timeout=20) as cl:
                for tid, name, tier, plan, packs, units in rows:
                    cap = cap_for({"tier": tier, "plan": plan})
                    counters = [{"counter": "exchanges", "period": period,
                                 "value": int(units)},
                                # the pack BALANCE (one-time purchases, spent
                                # beyond the cap) — a gauge, it goes down
                                {"counter": "packs_remaining", "period": period,
                                 "value": int(packs or 0), "kind": "gauge"}]
                    if cap:
                        counters.append({"counter": "quota_pct_exchanges",
                                         "period": period,
                                         "value": round(100.0 * units / cap, 1)})
                    r = await cl.post(
                        f"{CONTROL_URL}/internal/mind/usage",
                        json={"tenant_id": name.removeprefix("t_"),
                              "counters": counters},
                        headers={"Authorization": f"Bearer {CONTROL_TOKEN}"})
                    if r.status_code != 200:
                        log(f"ERROR: usage push for {name} -> {r.status_code}")
                outbound_ok()
        except Exception as e:                               # noqa: BLE001
            outbound_err(e)
            log(f"ERROR: usage pusher ({type(e).__name__}: {str(e)[:120]})")


# ---------------------------------------------------------------- inference
async def _project_names(tenant: dict) -> list:
    """The tenant's own project names — what the curator needs to route a fact
    to another project. It was told the name "must be a project that exists"
    and never shown which do."""
    return [r[0] for r in await store.fetchall(
        "SELECT DISTINCT area FROM rule WHERE tenant_id = ?"
        " AND rule_id LIKE 'purpose_%' AND area <> '' ORDER BY area",
        (tenant["id"],))]


BUDGET_WINDOW_S = 5 * 3600
# tenants told (per hub life, per window) that their budget refused a call
BUDGET_SAID: dict[str, float] = {}


async def budget_over(tenant_id: str) -> int:
    """0 when the tenant may spend; else the seconds until enough of its
    window ages out. No infer_budget row = never capped."""
    row = await store.fetchone("SELECT tokens_5h FROM infer_budget WHERE tenant_id = ?",
                               (tenant_id,))
    if not row:
        return 0
    cap, since = int(row[0]), time.time() - BUDGET_WINDOW_S
    spent = await store.fetchone(
        "SELECT COALESCE(SUM(tokens), 0), MIN(ts) FROM infer_spend"
        " WHERE tenant_id = ? AND ts > ?", (tenant_id, since))
    if int(spent[0]) < cap:
        return 0
    wait = max(60, int((spent[1] or since) - since) + 1)
    if time.time() - BUDGET_SAID.get(tenant_id, 0) > BUDGET_WINDOW_S:
        BUDGET_SAID[tenant_id] = time.time()
        log(f"budget: tenant {tenant_id} spent {int(spent[0])} of {cap} tokens in 5h"
            f" — its calls are refused for ~{wait}s (the key is not spent)")
    return wait


async def budget_spend(tenant_id: str, tokens: int) -> None:
    """Record a capped tenant's call; uncapped tenants write nothing."""
    if not await store.fetchone("SELECT 1 FROM infer_budget WHERE tenant_id = ?", (tenant_id,)):
        return
    now = time.time()
    await store.execute("INSERT INTO infer_spend (tenant_id, ts, tokens) VALUES (?, ?, ?)",
                        (tenant_id, now, int(tokens)))
    await store.execute("DELETE FROM infer_spend WHERE tenant_id = ? AND ts < ?",
                        (tenant_id, now - BUDGET_WINDOW_S))


async def infer(tenant: dict, body: dict, job: str = "generate") -> dict:
    """Relay ONE prompt to the provider on our key. Mirrors core/inference.py:
    greedy temperature + fixed seed by default (curation is judgement, not
    writing), think switchable (thinking shares the output budget), loud None
    on failure."""
    own = models.ai()
    host, key = (own["url"], own["key"]) if own else (OLLAMA_HOST, OLLAMA_KEY)
    if not own and not OLLAMA_KEY:
        return {"ok": False, "why": "provider"}
    if own:
        # THE CUSTOMER'S OWN MODEL (2026-11-17, local-first): one model, set on
        # the settings page; whatever a caller names is ignored.
        model = own["model"]
    else:
        model = body.get("model") or MODELS[0]
        if model not in MODELS:
            return {"ok": False, "why": f"model not offered (have: {MODELS})"}
    kind = body.get("kind", "generate")
    options = {"temperature": float(body.get("temperature", 0.0)),
               "seed": int(body.get("seed", 42))}
    if NUM_CTX:
        options["num_ctx"] = NUM_CTX
    if own:
        # PLAIN OPENAI-COMPATIBLE: only the fields every such server accepts
        # (OpenAI itself 400s on unknown ones). The base URL carries /v1.
        payload = {"model": model, "stream": False,
                   "temperature": options["temperature"], "seed": options["seed"],
                   **({"reasoning_effort": own["reasoning"]} if own["reasoning"] else {}),
                   **({"max_tokens": int(body["max_tokens"])} if body.get("max_tokens") else {}),
                   **({"response_format": {"type": "json_object"}} if body.get("json") else {}),
                   "messages": ([{"role": "system", "content": body["system"]}]
                                if body.get("system") else [])
                               + [{"role": "user", "content": body.get("prompt", "")}]}
        for k, v in (own.get("extra") or {}).items():
            if k not in ("model", "messages", "stream"):
                payload[k] = v
        path = "/chat/completions"
    elif kind == "chat":
        payload: dict = {"model": model, "stream": False,
                         "messages": body.get("messages") or [],
                         "options": options}
        if body.get("tools"):
            payload["tools"] = body["tools"]
        path = "/api/chat"
    elif PROVIDER_STYLE == "openai":
        payload = {"model": model, "stream": False,
                   "temperature": options["temperature"], "seed": options["seed"],
                   "chat_template_kwargs": {"enable_thinking": bool(body.get("think", False))},
                   **({"max_tokens": int(body["max_tokens"])} if body.get("max_tokens") else {}),
                   **({"response_format": {"type": "json_object"}} if body.get("json") else {}),
                   "messages": ([{"role": "system", "content": body["system"]}]
                                if body.get("system") else [])
                               + [{"role": "user", "content": body.get("prompt", "")}]}
        path = "/v1/chat/completions"
    elif REASONING_EFFORT:
        # THE OFF SWITCH IS NOT THE SAME ON EVERY MODEL. Native `think:false`
        # silences deepseek cleanly, but glm-5.3 answers it by emitting its
        # reasoning INLINE, wrapped in <think></think>, inside the response
        # body. Measured 2026-09-02 through this exact call shape: the first
        # "{" then sits inside the reasoning prose and the last "}" after it,
        # so parse_loose spans the two and returns None — EVERY hub job would
        # have curated nothing, silently. glm's real control is the
        # OpenAI-standard reasoning_effort, which lives on /v1.
        payload = {"model": model, "stream": False,
                   "temperature": options["temperature"],
                   "reasoning_effort": REASONING_EFFORT,
                   **({"max_tokens": int(body["max_tokens"])} if body.get("max_tokens") else {}),
                   **({"response_format": {"type": "json_object"}} if body.get("json") else {}),
                   "messages": ([{"role": "system", "content": body["system"]}]
                                if body.get("system") else [])
                               + [{"role": "user", "content": body.get("prompt", "")}]}
        path = "/v1/chat/completions"
    else:
        payload = {"model": model, "stream": False,
                   "prompt": body.get("prompt", ""), "options": options}
        if body.get("system"):
            payload["system"] = body["system"]
        if body.get("think") is not None:
            payload["think"] = bool(body["think"])
        if body.get("json"):
            payload["format"] = "json"
        if body.get("max_tokens"):
            options["num_predict"] = int(body["max_tokens"])
        path = "/api/generate"

    # THE BUDGET (2026-11-11): a capped tenant past its five-hour tokens is
    # refused here, before the key is spent. Uncapped tenants: one lookup.
    over = await budget_over(tenant["id"])
    if over:
        if job == "graph":
            return {"ok": False, "why": "budget", "retry_s": over}
        return {"ok": False, "why": "budget"}

    global _GLOBAL_SEM
    if _GLOBAL_SEM is None:
        _GLOBAL_SEM = asyncio.Semaphore(INFER_GLOBAL)
    sem = SEMS.setdefault(tenant["id"], asyncio.Semaphore(INFER_CONCURRENCY))
    gsem = (GRAPH_SEMS.setdefault(tenant["id"], asyncio.Semaphore(INFER_GRAPH_CONCURRENCY))
            if job == "graph" else contextlib.nullcontext())
    t0 = time.time()
    async with gsem, _GLOBAL_SEM, sem:
        res = None
        # A PROMPT IS WAITING (2026-11-17): the recall questions are asked
        # while the user's prompt waits for its memory — one try, a short
        # clock; a dead model must cost one failed call, not a retry ladder.
        fast = job in FAST_JOBS
        for attempt, wait in enumerate(((None,) if fast else INFER_LIMIT_RETRY_S + (None,))):
            if fast:
                wait = None
            elif attempt < len(INFER_RETRY_S):
                wait = INFER_RETRY_S[attempt] if wait is not None else None
            elif attempt < len(INFER_LIMIT_RETRY_S):
                wait = None                 # connection-class retries end here
            try:
                async with httpx.AsyncClient(timeout=FAST_TIMEOUT_S if fast else INFER_TIMEOUT_S) as client:
                    r = await client.post(
                        host.rstrip("/") + path, json=payload,
                        headers={"User-Agent": "brethof-brain-hub/1",
                                 **({"Authorization": f"Bearer {key}"} if key else {})})
                    r.raise_for_status()
                    res = r.json()
                outbound_ok()
                break
            except Exception as e:
                outbound_err(e)
                # A CONNECTION-CLASS FAILURE IS RETRIED, a provider answer is
                # not. 2026-09-08: the container's resolver died after a host
                # upgrade and every call failed on name resolution for two
                # days; 39 exchanges were dropped uncurated in an hour, each
                # gone for good under the no-back-curation law. A retry a few
                # seconds later is delivery of the SAME fresh piece, not
                # back-curation. A 4xx/5xx from the provider is its verdict
                # and stands.
                transient = isinstance(e, (httpx.ConnectError, httpx.ReadError,
                                           httpx.WriteError, httpx.PoolTimeout,
                                           httpx.ConnectTimeout))
                # A RATE LIMIT IS A WAIT, NOT A VERDICT (2026-09-08: the
                # provider's five-hour window closed at 18:10 and every call
                # was a 429 for fifty minutes — 44 exchanges dropped
                # uncurated, gone for good under the no-back-curation law,
                # and 57 graph batches burned their attempts). A 429 waits
                # the longer ladder before the piece is given up.
                limited = isinstance(e, httpx.HTTPStatusError) and \
                    e.response is not None and e.response.status_code == 429
                ran_out = isinstance(e, (httpx.ReadTimeout, httpx.TimeoutException))
                if limited:
                    ra = e.response.headers.get("retry-after")
                    left = window_note("429", ra)
                    if WINDOW["hits"] == 1:
                        body = e.response.text[:200].replace("\n", " ")
                        log(f"provider window closed ({WINDOW['why']}) for {left}s "
                            f"retry-after={ra!r} body={body!r}")
                    if job == "graph":
                        # A BACKFILL BATCH DOES NOT WAIT ON THE LADDER: it goes
                        # back to its queue with the seconds the window needs.
                        return {"ok": False, "why": "provider window closed",
                                "retry_s": left}
                if ran_out and job == "graph":
                    # THE CLOCK RAN OUT ON ONE BATCH (2026-09-18): that is the
                    # batch's problem, not the window's — the box sends it to
                    # the back of its queue and reads the next one.
                    log(f"graph read ran out the clock for {tenant['name']} — "
                        f"the batch goes back, the window stays open")
                    return {"ok": False, "why": "provider"}
                if limited:
                    wait = INFER_LIMIT_RETRY_S[attempt] if attempt < len(INFER_LIMIT_RETRY_S) else None
                if (transient or limited) and wait is not None:
                    log(f"infer retry {attempt + 1} in {wait}s tenant="
                        f"{tenant['name']} {type(e).__name__}: {str(e)[:120]}")
                    await asyncio.sleep(wait)
                    continue
                # THE MODEL'S OWN REASON (2026-11-17, the 27B hammer): a 400
                # said only "Bad Request" on both sides, and an owner whose
                # model refuses every window (a context too small, a wrong
                # option) could not tell why. Its first words go in the log.
                said = ""
                if isinstance(e, httpx.HTTPStatusError) and e.response is not None:
                    said = " — the model said: " + e.response.text[:300].replace("\n", " ")
                log(f"infer FAIL tenant={tenant['name']} {type(e).__name__}:"
                    f" {str(e).splitlines()[0][:160]}{said}")
                return {"ok": False, "why": "provider"}
        if res is None:
            return {"ok": False, "why": "provider"}
    usage = res.get("usage") or {}
    tin = int(res.get("prompt_eval_count") or usage.get("prompt_tokens") or 0)
    tout = int(res.get("eval_count") or usage.get("completion_tokens") or 0)
    # METERED BY JOB (2026-09-03): every hub call used to land under one op,
    # so the cost of curate vs register vs rulefilter vs heal was a guess
    # made from the box's side table. Pricing needs the split.
    if not str(tenant["id"]).startswith("_"):
        # "_decision" (the AI model standing in for the decision model) is
        # no tenant: nothing to meter, no row it could be written under
        meter(tenant["id"], f"infer:{job}", tin, tout)
        await budget_spend(tenant["id"], tin + tout)
    secs = round(time.time() - t0, 3)
    if secs > INFER_SLOW_S:
        log(f"infer slow {job} {secs:.0f}s tenant={tenant['name']} "
            f"in={tin} out={tout}")
    out: dict = {"ok": True, "model": model, "input_tokens": tin,
                 "output_tokens": tout, "seconds": secs}
    if kind == "chat":
        out["message"] = res.get("message") or {}
    else:
        out["text"] = (res.get("response")
                       or ((res.get("choices") or [{}])[0]
                           .get("message", {}).get("content"))
                       or "")
        if own and out["text"].lstrip().startswith("<think>") and "</think>" in out["text"]:
            # a reasoning model that writes its thinking inline (glm, qwen)
            out["text"] = out["text"].split("</think>", 1)[1].lstrip()
    return out


# ---------------------------------------------------------------- prompts
# THE LICENSED PART. A self-hosted box ships mechanics: it can find a
# near-duplicate with vector maths and count what changed since the last heal.
# It cannot judge, and it holds no inference key by design. The judgment is
# done by the CUSTOMER'S OWN agent, using these prompts — which is why they
# live here and are served per call rather than baked into the image. Stop
# paying and the mechanics keep running; the judgment stops. That is the whole
# licence, with no lock, no phone-home check in the data path and no hostage
# taken: their memory stays theirs and stays readable.
PROMPTS: dict[str, str] = {}


def _load_prompts() -> None:
    """Every licensed prompt, from the same file our cloud uses.

    ONE SOURCE. If the hub kept its own copy, a prompt improved on the cloud
    would leave self-hosted customers on the old one — the same product
    judging the same merge differently depending on where it runs."""
    try:
        from core.prompts import TEXTS
        PROMPTS.update(TEXTS)
    except ImportError as e:
        log(f"WARNING: core/prompts.py not available ({e}) — self-hosted "
            f"boxes cannot fetch prompts and their judgment features are "
            f"DOWN until this is fixed")


# The ONLY prompts a box may fetch: the customer-side jobs — heal, and the
# near-dup merge verdict the customer's own agent renders on its queued
# saves (next_task). Everything else never leaves the hub — the loop's
# prompts ARE the product, and serving them hands over the blueprint
# (founder 2026-08-30: reverse engineering must stay hard).
@app.post("/v1/admin/tenants")
async def admin_mint_tenant(request: Request) -> JSONResponse:
    """Mint a tenant + its key — the control plane's door, at signup.

    Gated by HUB_ADMIN_KEY (hub.env), which only the control plane holds.
    The key is returned ONCE and only its hash is stored, same as every
    other key in the product."""
    want = os.environ.get("HUB_ADMIN_KEY", "")
    got = request.headers.get("X-Admin-Key", "")
    import secrets as _secrets
    if not want or not _secrets.compare_digest(want, got):
        return JSONResponse({"ok": False, "why": "admin key"}, status_code=403)
    try:
        body = await request.json()
        name = str(body.get("name", "")).strip()
        plan = str(body.get("plan", "") or "hosted").strip()
        # the BILLING tier — 'plan' above historically carries the shape.
        # No tier named = 'internal' (uncapped): only our own tooling mints
        # without one; the control plane always names it.
        tier = str(body.get("tier", "") or "internal").strip()
        graph = 1 if body.get("graph") else 0
    except Exception:
        return JSONResponse({"ok": False, "why": "bad body"}, status_code=400)
    import re as _re
    if not _re.fullmatch(r"t_[a-z0-9_]{3,40}", name):
        return JSONResponse({"ok": False, "why": "name must be t_<3-40 of "
                             "a-z 0-9 _>"}, status_code=400)
    import uuid as _uuid
    if body.get("deactivate"):
        # GDPR account erasure: the pipe key dies with the account. The row
        # stays (usage history keys off it); active=0 closes every door.
        n = await store.execute(
            "UPDATE tenants SET active = 0, revoked_at = datetime('now')"
            " WHERE name = ?", (name,))
        log(f"admin: deactivated tenant {name}")
        return JSONResponse({"ok": True, "deactivated": bool(n)})
    key = "bhub_" + _secrets.token_hex(24)
    existing = await store.fetchone(
        "SELECT id FROM tenants WHERE name = ?", (name,))
    if existing:
        # remint: the control plane retries a provision that died between
        # the mint and the stamp — the old key was shown once and is gone.
        # It only ever calls this for a tenant it knows is NOT provisioned,
        # so no live pipe is cut. Anything else stays a hard 409.
        if not body.get("remint"):
            return JSONResponse({"ok": False, "why": "name taken"},
                                status_code=409)
        await store.execute(
            "UPDATE tenants SET key_hash = ?, plan = ?, tier = ?, graph = ? "
            "WHERE name = ?",
            (hashlib.sha256(key.encode()).hexdigest(), plan, tier, graph, name))
        log(f"admin: reminted key for {name} ({plan}/{tier})")
        return JSONResponse({"ok": True, "id": existing[0], "name": name,
                             "key": key, "reminted": True})
    tid = str(_uuid.uuid4())
    await store.execute(
        "INSERT INTO tenants (id, name, key_hash, plan, tier, graph) "
        "VALUES (?,?,?,?,?,?)",
        (tid, name, hashlib.sha256(key.encode()).hexdigest(), plan, tier, graph))
    log(f"admin: minted tenant {name} ({plan}/{tier})")
    return JSONResponse({"ok": True, "id": tid, "name": name, "key": key})


@app.post("/v1/admin/tenants/{name}/packs")
async def admin_add_packs(name: str, request: Request) -> JSONResponse:
    """The control plane credits a bought PACK here: {units} added to the
    tenant's month allowance for good (packs do not expire; the cap table
    is per month, packs sit on top). Gated by HUB_ADMIN_KEY."""
    want = os.environ.get("HUB_ADMIN_KEY", "")
    got = request.headers.get("X-Admin-Key", "")
    import secrets as _secrets
    if not want or not _secrets.compare_digest(want, got):
        return JSONResponse({"ok": False, "why": "admin key"}, status_code=403)
    try:
        units = int((await request.json()).get("units") or 0)
    except Exception:                                     # noqa: BLE001
        units = 0
    if units == 0:
        return JSONResponse({"ok": False, "why": "units"}, status_code=400)
    # a refund arrives as negative units; the floor is zero
    n = await store.execute("UPDATE tenants SET packs = MAX(0, packs + ?) WHERE name = ?",
                            (units, name))
    if not n:
        return JSONResponse({"ok": False, "why": "no such tenant"}, status_code=404)
    row = await store.fetchone("SELECT packs FROM tenants WHERE name = ?", (name,))
    log(f"admin: +{units} pack units for {name} (packs now {row[0]})")
    return JSONResponse({"ok": True, "packs": int(row[0])})


@app.post("/v1/admin/graph")
async def admin_graph(request: Request) -> JSONResponse:
    """THE GRAPH ADD-ON SWITCH (founder 2026-09-08). Same gate as the mint
    door. Body: {name, on}. The control plane flips it with the plan; the
    box's relay calls for graph work answer 403 while it is off."""
    want = os.environ.get("HUB_ADMIN_KEY", "")
    got = request.headers.get("X-Admin-Key", "")
    import secrets as _secrets
    if not want or not _secrets.compare_digest(want, got):
        return JSONResponse({"ok": False, "why": "admin key"}, status_code=403)
    try:
        body = await request.json()
        name = str(body.get("name", "")).strip()
        on = 1 if body.get("on") else 0
    except Exception:
        return JSONResponse({"ok": False, "why": "bad body"}, status_code=400)
    n = await store.execute(
        "UPDATE tenants SET graph = ? WHERE name = ? AND active = 1", (on, name))
    if not n:
        return JSONResponse({"ok": False, "why": "no such tenant"},
                            status_code=404)
    log(f"admin: graph add-on {'ON' if on else 'off'} for {name}")
    return JSONResponse({"ok": True, "name": name, "graph": bool(on)})


@app.post("/v1/admin/pipe")
async def admin_pipe(request: Request) -> JSONResponse:
    """ONE COMMAND DOWN ONE TENANT'S PIPE, on the admin key — the proof
    instrument for the command channel (design step 1) and the ops door
    for reading a box. Body: {name, cmd, body?, timeout?}. The answer is
    the box's, verbatim, plus the round trip in ms."""
    want = os.environ.get("HUB_ADMIN_KEY", "")
    got = request.headers.get("X-Admin-Key", "")
    import secrets as _secrets
    if not want or not _secrets.compare_digest(want, got):
        return JSONResponse({"ok": False, "why": "admin key"}, status_code=403)
    try:
        body = await request.json()
        name = str(body.get("name", "")).strip()
        cmd = str(body.get("cmd", "")).strip()
        args = body.get("body") or {}
        timeout = float(body.get("timeout") or 0) or None
    except Exception:
        return JSONResponse({"ok": False, "why": "bad body"}, status_code=400)
    row = await store.fetchone(
        "SELECT id, name, plan FROM tenants WHERE name = ? AND active = 1", (name,))
    if not row:
        return JSONResponse({"ok": False, "why": "no such tenant"}, status_code=404)
    tenant = {"id": row[0], "name": row[1], "plan": row[2]}
    ans = await command(tenant, cmd, args if isinstance(args, dict) else {}, timeout=timeout)
    log(f"admin: {cmd} -> {name}: {'ok' if ans.get('ok') else ans.get('why')} {ans.get('ms', '?')}ms")
    return JSONResponse(ans, status_code=200 if ans.get("ok") else 502)


@app.post("/v1/admin/ledger")
async def admin_ledger(request: Request) -> JSONResponse:
    """The ledger off-switch (founder 2026-08-31: a builder on the Brain
    does not want a task list extracted from their users' chats, or the
    cost of one). Same gate as the mint door. Body: {name} reads the state;
    add {off, project?} to flip it — project '' (or absent) is the whole
    account. Always answers with the current state: the account flag and
    every project the hub has curated for, with its own flag."""
    want = os.environ.get("HUB_ADMIN_KEY", "")
    got = request.headers.get("X-Admin-Key", "")
    import secrets as _secrets
    if not want or not _secrets.compare_digest(want, got):
        return JSONResponse({"ok": False, "why": "admin key"}, status_code=403)
    try:
        body = await request.json()
        name = str(body.get("name", "")).strip()
    except Exception:
        return JSONResponse({"ok": False, "why": "bad body"}, status_code=400)
    row = await store.fetchone(
        "SELECT id FROM tenants WHERE name = ? AND active = 1", (name,))
    if not row:
        return JSONResponse({"ok": False, "why": "no such tenant"},
                            status_code=404)
    tid = row[0]
    if "off" in body:
        project = str(body.get("project", "") or "")
        if bool(body.get("off")):
            await store.execute(
                "INSERT OR IGNORE INTO ledger_off (tenant_id, project) "
                "VALUES (?, ?)", (tid, project))
        else:
            await store.execute(
                "DELETE FROM ledger_off WHERE tenant_id = ? AND project = ?",
                (tid, project))
        log(f"admin: ledger {'OFF' if body.get('off') else 'on'} for "
            f"{name}/{project or '(account)'}")
    # The projects this hub knows: those with a rule or a purpose (there is
    # no brief table — the brief is composed per call).
    projects = [r[0] for r in await store.fetchall(
        "SELECT DISTINCT area AS project FROM rule WHERE tenant_id = ? "
        "AND area <> 'all' ORDER BY project", (tid,))]
    offs = {r[0] for r in await store.fetchall(
        "SELECT project FROM ledger_off WHERE tenant_id = ?", (tid,))}
    return JSONResponse({"ok": True, "account_off": "" in offs,
                         "projects": [{"project": p, "off": p in offs}
                                      for p in projects]})


async def seeds_on(tenant_id: str) -> bool:
    """False when the tenant switched the ★SEED rules off (hub table
    seeds_off; admin door /v1/admin/seeds)."""
    return not await store.fetchone("SELECT 1 FROM seeds_off WHERE tenant_id = ?", (tenant_id,))


@app.post("/v1/admin/seeds")
async def admin_seeds(request: Request) -> JSONResponse:
    """THE SEED SWITCH (founder 2026-11-10). Same gate as the mint door.
    Body: {name} reads the state; add {off} to flip it for the whole
    account. Answers with the current state."""
    want = os.environ.get("HUB_ADMIN_KEY", "")
    got = request.headers.get("X-Admin-Key", "")
    import secrets as _secrets
    if not want or not _secrets.compare_digest(want, got):
        return JSONResponse({"ok": False, "why": "admin key"}, status_code=403)
    try:
        body = await request.json()
        name = str(body.get("name", "")).strip()
    except Exception:
        return JSONResponse({"ok": False, "why": "bad body"}, status_code=400)
    row = await store.fetchone("SELECT id FROM tenants WHERE name = ? AND active = 1", (name,))
    if not row:
        return JSONResponse({"ok": False, "why": "no such tenant"}, status_code=404)
    tid = row[0]
    if "off" in body:
        if bool(body.get("off")):
            await store.execute("INSERT OR IGNORE INTO seeds_off (tenant_id) VALUES (?)", (tid,))
        else:
            await store.execute("DELETE FROM seeds_off WHERE tenant_id = ?", (tid,))
        log(f"admin: seeds {'OFF' if body.get('off') else 'on'} for {name}")
    return JSONResponse({"ok": True, "name": name, "seeds": await seeds_on(tid)})


@app.post("/v1/admin/budget")
async def admin_budget(request: Request) -> JSONResponse:
    """THE INFERENCE BUDGET (2026-11-11). Same gate as the seed switch.
    Body: {name} reads it; add {tokens_5h: N} to cap the account at N tokens
    (in + out) per rolling five hours, {tokens_5h: 0} to remove the cap.
    Answers with the cap and what was spent in the window."""
    want = os.environ.get("HUB_ADMIN_KEY", "")
    got = request.headers.get("X-Admin-Key", "")
    import secrets as _secrets
    if not want or not _secrets.compare_digest(want, got):
        return JSONResponse({"ok": False, "why": "admin key"}, status_code=403)
    try:
        body = await request.json()
        name = str(body.get("name", "")).strip()
        cap = int(body["tokens_5h"]) if "tokens_5h" in body else None
    except Exception:
        return JSONResponse({"ok": False, "why": "bad body"}, status_code=400)
    row = await store.fetchone("SELECT id FROM tenants WHERE name = ? AND active = 1", (name,))
    if not row:
        return JSONResponse({"ok": False, "why": "no such tenant"}, status_code=404)
    tid = row[0]
    if cap is not None:
        if cap > 0:
            await store.execute("INSERT INTO infer_budget (tenant_id, tokens_5h) VALUES (?, ?)"
                                " ON CONFLICT (tenant_id) DO UPDATE SET tokens_5h = EXCLUDED.tokens_5h",
                                (tid, cap))
        else:
            await store.execute("DELETE FROM infer_budget WHERE tenant_id = ?", (tid,))
            await store.execute("DELETE FROM infer_spend WHERE tenant_id = ?", (tid,))
        log(f"admin: inference budget for {name} = {cap or 'none'} tokens/5h")
    b = await store.fetchone("SELECT tokens_5h FROM infer_budget WHERE tenant_id = ?", (tid,))
    s = await store.fetchone("SELECT COALESCE(SUM(tokens), 0) FROM infer_spend"
                             " WHERE tenant_id = ? AND ts > ?", (tid, time.time() - BUDGET_WINDOW_S))
    return JSONResponse({"ok": True, "name": name, "tokens_5h": b[0] if b else None,
                         "spent_5h": int(s[0])})


# ── THE RATE WALL (2026-09-03) — capacity is mechanical, never judgment ──
# One token bucket per bearer key. Generous: a working agent's hooks make a
# handful of calls per prompt; only a loop hits this. Health and admin
# routes carry no bearer and are not walled here.
RATE_PER_MIN = float(os.environ.get("HUB_RATE_PER_MIN", "240"))
RATE_BURST = float(os.environ.get("HUB_RATE_BURST", "480"))
_buckets: dict[str, list] = {}


def bucket_ok(key: str, now: float, per_min: float = RATE_PER_MIN,
              burst: float = RATE_BURST) -> bool:
    b = _buckets.get(key)
    if b is None:
        b = _buckets[key] = [burst, now]
    tokens = min(burst, b[0] + (now - b[1]) * per_min / 60.0)
    if tokens < 1.0:
        b[0], b[1] = tokens, now
        return False
    b[0], b[1] = tokens - 1.0, now
    return True


@app.middleware("http")
async def rate_wall(request: Request, call_next):
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        key = hashlib.sha256(auth[7:].strip().encode()).hexdigest()[:32]
        if not bucket_ok(key, time.time()):
            return JSONResponse({"ok": False, "why": "too many requests — "
                                 "this key shares one request budget; "
                                 "slow the loop"}, status_code=429,
                                headers={"Retry-After": "5"})
    return await call_next(request)


async def authenticate_http(request: Request) -> dict | None:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    digest = hashlib.sha256(auth[7:].strip().encode()).hexdigest()
    row = await store.fetchone(
        "SELECT id, name, plan, tier, packs, graph FROM tenants"
        " WHERE key_hash = ? AND active = 1", (digest,))
    return None if row is None else {"id": row[0], "name": row[1],
                                     "plan": row[2], "tier": row[3],
                                     "packs": int(row[4] or 0),
                                     "graph": bool(row[5])}


# ── THE QUOTA DOOR (founder caps 2026-08-30; built 2026-09-01) ────────────
# Message-units per calendar month, per BILLING tier. Reading is the candy
# and never gated — recall, archive and history live on the customer's box
# and cost us nothing. What is gated is LEARNING: the inference this hub
# spends on an exchange (curator, entities, ledger, rule filter) and on heal.
# 'internal' and any unknown tier are uncapped — every pre-door row was
# ours, and a mint that names no tier is ours too. Paid tiers land here
# when pricing LOCKS; until then free is the only sellable tier.
# 'rigtiny' exists so the ship gate can PROVE the door shuts without
# sending five hundred messages.
# THE CAP TABLE (founder-set 2026-08-30, encoded 2026-09-03 when the
# per-job meter showed the caps hold 64-78% gross at full use). Keyed by
# SHAPE — `plan` on the tenant row: selfhosted = the customer's own box, a
# few more messages and their own disk; hosted = our VPS — then by billing
# tier. 'internal' and any unknown tier stay uncapped: every pre-door row
# was ours. 'rigtiny' lets the ship gate prove the door shuts.
# ONE LIST FOR BOTH SHAPES, THE GRAPH INCLUDED (founder 2026-10-03: "keep free
# at 500 and release everything"; the plans 2026-10-02/03). Windows of five
# measured on GLM at ~$0.0012-0.0015 an exchange with Jev: ~70-76% gross at
# full use at $0.005 a message.
CAPS: dict[tuple[str, str], int] = {
    ("selfhosted", "free"): 500, ("selfhosted", "starter"): 2000,
    ("selfhosted", "pro"): 4000, ("selfhosted", "team"): 10000,
    ("selfhosted", "business"): 20000,
    ("hosted", "free"): 500, ("hosted", "starter"): 2000,
    ("hosted", "pro"): 4000, ("hosted", "team"): 10000,
    ("hosted", "business"): 20000,
    ("selfhosted", "rigtiny"): 3, ("hosted", "rigtiny"): 3,
}
TIER_CAPS: dict[str, int] = {"free": 500, "rigtiny": 3}   # tier-only fallback


def cap_for(tenant: dict) -> int | None:
    """The month's BASE allowance from the shape/tier table, or None for
    uncapped. PACKS are not part of it: a pack is a one-time purchase
    (founder 2026-09-03: "you use the credit till it runs out"), a balance
    on tenants.packs that is spent only by units beyond the month's cap
    and never refills — see take_units()."""
    tier = str(tenant.get("tier") or "internal")
    shape = "hosted" if str(tenant.get("plan") or "") == "hosted" else "selfhosted"
    return CAPS.get((shape, tier), TIER_CAPS.get(tier))


async def packs_left(tenant_id: str) -> int:
    row = await store.fetchone("SELECT packs FROM tenants WHERE id = ?", (tenant_id,))
    return int(row[0] or 0) if row else 0


async def take_units(tenant: dict, units: int) -> str:
    """Spend `units` of allowance: the month's cap first, then the pack
    balance. Returns "" when they can be spent (the caller meters them) or
    the reason they cannot. Only the part of this spend that lies beyond
    the cap comes out of the packs, so a pack is never charged for units
    the plan already covers."""
    base = cap_for(tenant)
    if base is None:
        return ""
    used = await month_units(tenant["id"])
    beyond = max(0, used + units - base) - max(0, used - base)
    if beyond <= 0:
        return ""
    packs = await packs_left(tenant["id"])
    if packs < beyond:
        return ("over monthly allowance — learning is paused until the next "
                "period, or a message pack; your memory, recall and full "
                "history stay live on your box")
    await store.execute("UPDATE tenants SET packs = MAX(0, packs - ?) WHERE id = ?",
                        (beyond, tenant["id"]))
    return ""


_OVERCAP_SAID: set[str] = set()          # one loud line per tenant per boot


async def month_units(tenant_id: str) -> int:
    row = await store.fetchone(
        "SELECT COALESCE(SUM(calls), 0) FROM usage_daily WHERE tenant_id = ?"
        " AND op = 'exchange'"
        " AND day LIKE strftime('%Y-%m-', 'now') || '%'",
        (tenant_id,))
    stored = int(row[0]) if row else 0
    # The meter BUFFERS for up to 15 s before flushing — a burst of
    # exchanges is invisible to the table while it matters most (the gate
    # proved it: 4 rapid sends, cap 3, all four accepted). The unflushed
    # rows are part of this month by definition.
    # ONE MESSAGE PER EXCHANGE, THE GRAPH INCLUDED (founder 2026-10-03). The
    # add-on's second unit (2026-09-08 → 10-03) no longer counts — the month's
    # earlier 'graph' rows stay in the table as history, outside the allowance.
    buffered = sum(n for tid, op, n, _ti, _to in USAGE_BUFFER
                   if tid == tenant_id and op == "exchange")
    return stored + buffered


HEAL_GRACE = int(os.environ.get("HUB_HEAL_GRACE", "0"))


async def month_calls(tenant_id: str, op: str) -> int:
    row = await store.fetchone(
        "SELECT COALESCE(SUM(calls), 0) FROM usage_daily WHERE tenant_id = ?"
        " AND op = ? AND day LIKE strftime('%Y-%m-', 'now') || '%'",
        (tenant_id, op))
    stored = int(row[0]) if row else 0
    buffered = sum(n for tid, o, n, _ti, _to in USAGE_BUFFER
                   if tid == tenant_id and o == op)
    return stored + buffered


async def heal_over_allowance(tenant: dict) -> bool:
    """HEAL IS BOUND TO THE CURATOR, ONE FOR ONE (founder 2026-09-03: "the
    curator done the job sets one; heal clears it to zero; heal cannot run
    more often than the curator"). Heal calls this month may not exceed the
    pieces the curator judged this month. With heal by four this is never
    reached in honest use, which is what a wall should look like; it is the
    bound that makes the relay safe to expose to a box the customer owns.
    Uncapped tiers (ours) are exempt like everywhere else."""
    if cap_for(tenant) is None:
        return False
    heals = await month_calls(tenant["id"], "heal")
    curated = await month_calls(tenant["id"], "curate")
    return heals >= curated + HEAL_GRACE


def graph_allowed(tenant: dict) -> bool:
    """The graph of decisions is part of every plan since 2026-10-03 (founder:
    "we get rid of the graph [add-on] and it's now included"). The tenant
    row's flag stays as history; nothing reads it for a price."""
    return True


def rule_units(rule_id: str) -> int:
    """What a rule save costs, in message units (founder 2026-09-03): one
    message for a rule saved or changed; ten for a project's birth, which is
    its purpose rule. Deletes are free."""
    return 10 if str(rule_id or "").startswith("purpose_") else 1


async def over_cap(tenant: dict) -> bool:
    """No allowance left: the month's cap is spent AND the pack balance is
    empty. The gate the relays and rule saves stand behind."""
    cap = cap_for(tenant)
    if cap is None:
        return False
    used = await month_units(tenant["id"])
    if used < cap or await packs_left(tenant["id"]) > 0:
        return False
    if tenant["id"] not in _OVERCAP_SAID:
        _OVERCAP_SAID.add(tenant["id"])
        log(f"OVER CAP: {tenant['name']} (tier {tenant.get('tier')}, "
            f"{used}/{cap} units this month) — learning paused, "
            f"recall and archive unaffected on their box")
    meter(tenant["id"], "overcap", 0, 0)
    return True


# ---------------------------------------------------------------- rules
# ONE COPY, HERE. A self-hosted box holds a read-only CACHE of these rows and
# can never write back — see core/rulesync.py on the container side. Two
# reasons, and the second is the one that decided it: rules teach the curator,
# so they must be identical everywhere; and a customer editing them in their
# own psql breaks their curator while the support ticket still arrives here.
# Rules are system law, not customer content — which is also why they are the
# ONLY customer-adjacent thing this hub stores at all.
MAX_RULE_CHARS = 1000          # matches the data plane's per-rule cap
MAX_RULES_PER_TENANT = 500     # a ceiling, not a product limit


@app.get("/v1/rules")
async def get_rules(request: Request) -> JSONResponse:
    tenant = await authenticate_http(request)
    if tenant is None:
        return JSONResponse({"ok": False, "why": "unauthorized"},
                            status_code=401)
    rows = await store.fetchall(
        "SELECT rule_id, title, content, area, pin, updated_at FROM rule"
        " WHERE tenant_id = ? ORDER BY rule_id", (tenant["id"],))
    meter(tenant["id"], "rules:get", 0, 0)
    # ?project=<name> hands a box exactly what its session needs — the law
    # that bears there plus the seeds — fetched at session start and never
    # stored on the box (founder 2026-09-02). No project = the whole set.
    project = str(request.query_params.get("project") or "").strip()
    rules = jobs.rules_for(rows, project, seeds=await seeds_on(tenant["id"]))
    # which rules still lack a vector, so a box can send them (backfill)
    novec = {r[0] for r in await store.fetchall(
        "SELECT rule_id FROM rule WHERE tenant_id = ? AND vec IS NULL",
        (tenant["id"],))}
    for r in rules:
        r["has_vector"] = r["seed"] and r["rule_id"] in SEED_VECS or r["rule_id"] not in novec and not r["seed"]
    return JSONResponse({"ok": True, "project": project, "rules": rules})


# The seeds' vectors, sent once by any box that notices they are missing
# (they are ours and identical everywhere, so one box's embedding serves all).
SEED_VECS: dict[str, list[float]] = {}


@app.post("/v1/rules/vectors")
async def post_rule_vectors(request: Request) -> JSONResponse:
    """A box sends embeddings for rules that have none — its own rules on
    save, and at session start whichever ones the set came back without.
    The hub has no embedder; every box has one."""
    tenant = await authenticate_http(request)
    if tenant is None:
        return JSONResponse({"ok": False, "why": "unauthorized"},
                            status_code=401)
    body = await request.json()
    vectors = body.get("vectors") or {}
    n = 0
    for rid, vec in vectors.items():
        if jobs.is_seed_id(str(rid)):
            if isinstance(vec, list) and len(vec) > 8:
                SEED_VECS[str(rid)] = [float(x) for x in vec]; n += 1
            continue
        blob = jobs.pack_vec(vec)
        if blob is None:
            continue
        n += await store.execute(
            "UPDATE rule SET vec = ? WHERE tenant_id = ? AND rule_id = ?",
            (blob, tenant["id"], str(rid)))
    meter(tenant["id"], "rules:vectors", 0, 0)
    return JSONResponse({"ok": True, "stored": n})


@app.post("/v1/rules/match/{project}")
async def match_rules(project: str, request: Request) -> JSONResponse:
    """THE START-OF-MESSAGE HOOK'S QUESTION: which rule does this prompt
    touch? Answered from this table, now, for an ACTIVE key — an inactive
    one gets the 401 above, and that is the gate the founder wants on the
    ambient feature (2026-09-03): recall on their own box is theirs, the
    Brain speaking unasked is the product. Nothing about the prompt is kept."""
    tenant = await authenticate_http(request)
    if tenant is None:
        return JSONResponse({"ok": False, "why": "unauthorized"},
                            status_code=401)
    body = await request.json()
    prompt_text = str(body.get("prompt") or "")
    vec = body.get("vector")
    rows = await store.fetchall(
        "SELECT rule_id, title, content, area, pin, updated_at, vec FROM rule"
        " WHERE tenant_id = ? ORDER BY rule_id", (tenant["id"],))
    rules = jobs.rules_for([r[:6] for r in rows], project, seeds=await seeds_on(tenant["id"]))
    vecs = {r[0]: jobs.unpack_vec(r[6]) for r in rows if r[6]}
    for r in rules:
        v = SEED_VECS.get(r["rule_id"]) if r["seed"] else vecs.get(r["rule_id"])
        if v:
            r["vec"] = v
    rule = None
    how = "words"
    if isinstance(vec, list) and len(vec) > 8:
        rule = jobs.match_rule_vec(rules, [float(x) for x in vec], prompt_text)
        how = "vector"
    if rule is None and how == "words":
        rule = jobs.match_rule(rules, prompt_text)
    if rule is not None:
        rule = {k: v for k, v in rule.items() if k != "vec"}
    meter(tenant["id"], "rules:match", 0, 0)
    return JSONResponse({"ok": True, "project": project, "rule": rule, "how": how})


@app.put("/v1/rules/{rule_id}")
async def put_rule(rule_id: str, request: Request) -> JSONResponse:
    tenant = await authenticate_http(request)
    if tenant is None:
        return JSONResponse({"ok": False, "why": "unauthorized"},
                            status_code=401)
    try:
        body = await request.json()
    except Exception:                                        # noqa: BLE001
        return JSONResponse({"ok": False, "why": "bad json"}, status_code=400)
    if jobs.is_seed_id(rule_id):
        return JSONResponse({"ok": False, "why": "★SEED rules are the "
                             "service's own teaching — read-only"},
                            status_code=400)
    content = str(body.get("content") or "")
    if not content.strip():
        return JSONResponse({"ok": False, "why": "empty rule"},
                            status_code=400)
    if len(content) > MAX_RULE_CHARS:
        # The cap is what forces consolidation instead of accretion; enforce
        # it HERE too so a modified client cannot route around it.
        return JSONResponse(
            {"ok": False, "why": f"rule exceeds {MAX_RULE_CHARS} chars "
                                 f"({len(content)}) — merge or trim it"},
            status_code=400)
    row = await store.fetchone(
        "SELECT count(*) FROM rule WHERE tenant_id = ? AND rule_id <> ?",
        (tenant["id"], rule_id))
    if row[0] >= MAX_RULES_PER_TENANT:
        return JSONResponse(
            {"ok": False, "why": f"at the {MAX_RULES_PER_TENANT}-rule "
                                 f"ceiling — delete some first"},
            status_code=409)
    old = await store.fetchone(
        "SELECT content FROM rule WHERE tenant_id = ? AND rule_id = ?",
        (tenant["id"], rule_id))
    title = str(body.get("title") or "")[:300]
    # THE POOL (founder 2026-08-07, back on the hub 2026-09-10): a save that
    # would overfill its pool is refused with the fix attached; nothing of
    # the owner's is ever trimmed by us. Checked BEFORE the unit is taken —
    # a refused save costs nothing.
    rows = await store.fetchall(
        "SELECT rule_id, title, content, area FROM rule WHERE tenant_id = ?", (tenant["id"],))
    pool_why = jobs.pool_refusal(
        [{"rule_id": r[0], "title": r[1], "content": r[2], "area": r[3]} for r in rows],
        rule_id, str(body.get("area") or "all"), title, content)
    if pool_why:
        return JSONResponse({"ok": False, "why": pool_why}, status_code=409)
    why = await take_units(tenant, rule_units(rule_id))
    if why:
        return JSONResponse({"ok": False, "why": "saving a rule costs a message "
                             "(a project ten) and the allowance is spent; "
                             "deleting is free"}, status_code=429)
    await store.execute(
        "INSERT INTO rule (tenant_id, rule_id, title, content, area, pin,"
        " updated_at) VALUES (?,?,?,?,?,?,datetime('now'))"
        " ON CONFLICT (tenant_id, rule_id) DO UPDATE SET"
        " title=excluded.title, content=excluded.content,"
        " area=excluded.area, pin=excluded.pin,"
        " updated_at=datetime('now')",
        (tenant["id"], rule_id, title,
         content, str(body.get("area") or "all")[:64],
         1 if body.get("pin", True) else 0))
    meter(tenant["id"], "rules:put", 0, 0)
    # A RULE SAVE IS A MESSAGE; a project's birth is ten (founder 2026-09-03).
    meter(tenant["id"], "exchange", 0, 0, count=rule_units(rule_id))
    # A changed rule is re-judged on its next render (the verdict cache is
    # keyed on content); the old verdicts are dead rows, so they go now.
    # Nothing wakes: the brief is composed at use time, so the next exchange
    # of every project this rule bears on reads the new line by itself.
    if old is not None and str(old[0] or "") != content:
        await store.execute(
            "DELETE FROM rule_verdict WHERE tenant_id = ? AND rule_id = ?",
            (tenant["id"], rule_id))
    if rule_id.startswith("purpose_") and old is None:
        # A NEW PROJECT: warm its verdict cache now, off the request, so its
        # first exchange composes as a string operation instead of paying
        # every general rule's filter call in the exchange chain.
        area = str(body.get("area") or "").strip()
        if area and area != "all":
            log(f"warm: {tenant['name']}/{area} — new purpose, composing")
            asyncio.create_task(_bearing_rules_shared(tenant, area))
    return JSONResponse({"ok": True, "rule_id": rule_id})


@app.delete("/v1/project/{project}")
async def delete_project_hub(project: str, request: Request) -> JSONResponse:
    """A project deleted on its box takes its footprint here with it
    (founder 2026-09-03: "if the project is removed we don't need indexes
    for the graph for this project, and we delete the rules and the
    description"): its rules including the purpose, its rule verdicts, its
    kind list and its name index. General law stays. Idempotent; says what
    went."""
    tenant = await authenticate_http(request)
    if tenant is None:
        return JSONResponse({"ok": False, "why": "unauthorized"},
                            status_code=401)
    project = project.strip()
    if not project or project in ("all", "global"):
        return JSONResponse({"ok": False, "why": "not a project"},
                            status_code=400)
    gone = {}
    gone["rules"] = await store.execute(
        "DELETE FROM rule WHERE tenant_id = ? AND area = ?", (tenant["id"], project))
    gone["verdicts"] = await store.execute(
        "DELETE FROM rule_verdict WHERE tenant_id = ? AND project = ?",
        (tenant["id"], project))
    COMPOSING.pop((tenant["id"], project), None)
    BARE_SAID.discard((tenant["id"], project))
    meter(tenant["id"], "project:delete", 0, 0)
    log(f"project deleted {tenant['name']}/{project}: {gone}")
    return JSONResponse({"ok": True, "project": project, "gone": gone})


@app.delete("/v1/rules/{rule_id}")
async def delete_rule(rule_id: str, request: Request) -> JSONResponse:
    tenant = await authenticate_http(request)
    if tenant is None:
        return JSONResponse({"ok": False, "why": "unauthorized"},
                            status_code=401)
    if jobs.is_seed_id(rule_id):
        return JSONResponse({"ok": False, "why": "★SEED rules are the "
                             "service's own teaching — read-only"},
                            status_code=400)
    gone = await store.execute(
        "DELETE FROM rule WHERE tenant_id = ? AND rule_id = ?",
        (tenant["id"], rule_id))
    meter(tenant["id"], "rules:delete", 0, 0)
    if gone:
        # Its line leaves every brief on the next compose; its verdicts are
        # dead rows and go with it.
        await store.execute(
            "DELETE FROM rule_verdict WHERE tenant_id = ? AND rule_id = ?",
            (tenant["id"], rule_id))
    # A delete that matched nothing is worth saying out loud: a client that
    # believes it removed a rule which is still teaching the curator is the
    # quiet failure here.
    return JSONResponse({"ok": bool(gone), "rule_id": rule_id,
                         "why": "" if gone else "no such rule"},
                        status_code=200 if gone else 404)


# ──────────────────────────────── the loop: THE HUB THINKS, THE BOX HOLDS
# Founder 2026-08-29/30: UP comes only what the hooks carry — the current
# exchange (plus rule writes on their own routes). DOWN went only finished
# SAVES, and the hub asked the box nothing mid-job: a question pattern was
# held to be a blueprint of our internals readable from the customer's own
# traffic. REVERSED by the founder 2026-09-10 ("the hub is the brain, the
# container is the worker"): the logic moves up here where nobody can read
# it, and the hub asks the box for rows through `command()` — a fixed
# vocabulary, nothing free-form. The curator's exchange path below is as it
# was; the readers arrive in the design's later steps.
#
# NOTHING RESTS HERE. The queue is in memory only; a hub restart drops only
# what was in flight, which the no-backfill law already accepts — that
# stretch stays in the customer's own archive, searchable, uncurated.
#
# THERE IS NO TEACHER (founder 2026-09-02). The curator's brief is composed at
# use time from the project's purpose and one filter-written line per bearing
# rule — see jobs.render_brief for the measurement. No birth buffer: the
# purpose exists from add_project, so the FIRST exchange curates.

# Exchanges queued per tenant before old ones are shed (loudly).
EXQ_MAX = int(os.environ.get("HUB_EXQ_MAX", "200"))
# THE SIZE GATE (founder 2026-08-31: "if someone sends us one million
# tokens as one message we need to charge it as many messages"). One
# billing unit covers a normal exchange with headroom (median real
# exchange is ~4.8K chars, p75 ~8.7K); a giant paste bills as several.
EXCHANGE_UNIT_CHARS = int(os.environ.get("HUB_EXCHANGE_UNIT_CHARS", "12000"))
# And the processing STOP: beyond this the curator reads the newest tail
# with a loud marker — a 2MB paste must not eat a model call whole.
PIECE_CAP_CHARS = int(os.environ.get("HUB_PIECE_CAP_CHARS", "60000"))
# a WINDOW of up to five pieces, each cut on the box to 40k (2026-09-26)
WINDOW_CAP_CHARS = int(os.environ.get("HUB_WINDOW_CAP_CHARS", "240000"))
PUSH_TIMEOUT_S = float(os.environ.get("HUB_PUSH_TIMEOUT_S", "30"))

# tenant_id -> queue of (tenant, project, session_id, piece)
EXQ: dict[str, asyncio.Queue] = {}
EXWORKER: dict[str, asyncio.Task] = {}
EXBUSY: set = set()           # tenants whose chain is mid-window right now
HOLD_WINDOW_S = int(os.environ.get("HUB_HOLD_WINDOW_S", str(12 * 3600)))
STOP_DRAIN_S = int(os.environ.get("HUB_STOP_DRAIN_S", "120"))


async def _judge(tenant: dict, prompt: str, system: str,
                 job: str = "judge") -> dict | None:
    """One judgement call on our key, through the same relay the pipe uses."""
    res = await infer(tenant, {"prompt": prompt, "system": system,
                               "think": False}, job=job)
    if not res.get("ok"):
        log(f"job: provider refused ({res.get('why')})")
        return None
    return jobs.parse_loose(res.get("text") or "")


@app.post("/v1/exchange/{project}")
async def exchange(project: str, request: Request) -> JSONResponse:
    """THE ONE DOOR for the write loop — the end-of-message hook posts the
    exchange here (having already archived it to the box's own Postgres, so
    the chat history never depends on us). Returns immediately; the chain
    runs from the in-memory queue."""
    tenant = await authenticate_http(request)
    if tenant is None:
        return JSONResponse({"ok": False, "why": "unauthorized"},
                            status_code=401)
    body = await request.json()
    piece = str(body.get("piece") or "")
    if not piece.strip() and isinstance(body.get("turns"), list):
        piece = "\n\n".join(f"[{t.get('role', '?')}] {t.get('text', '')}"
                            for t in body["turns"] if isinstance(t, dict))
    if not piece.strip():
        return JSONResponse({"ok": False, "why": "no piece"}, status_code=400)
    q = EXQ.get(tenant["id"])
    if q is None:
        q = EXQ[tenant["id"]] = asyncio.Queue()
    if q.qsize() >= EXQ_MAX:
        # Shed OLDEST and say so — the stream stays current, the archive on
        # the box keeps what curation missed (no-backfill: never caught up).
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            pass
        log(f"ERROR: exchange queue overflow for {tenant['name']} — "
            f"oldest dropped (archive keeps it)")
    # A WINDOW (2026-09-26, the switch): a box at hubfeed/2.0 posts up to
    # five pieces of one session as one — the window text under EXCHANGE
    # headers, each piece's turn range, the threads view and the session's
    # records for the two calls, the person's next prompt when it pushed
    # the window. One unit per piece at least; the graph's second unit per
    # piece as before. An old box's single piece is the same door.
    ranges = [[int(r[0]), int(r[1])] for r in (body.get("ranges") or [])
              if isinstance(r, list) and len(r) == 2 and all(isinstance(x, int) and not isinstance(x, bool) for x in r)]
    window = None
    if ranges:
        window = {"ranges": ranges, "days": [str(d)[:16] for d in (body.get("days") or [])][:len(ranges)],
                  "threads": [t for t in (body.get("threads") or []) if isinstance(t, dict)][:60],
                  "near_lines": [x for x in (body.get("near_lines") or []) if isinstance(x, dict)][:20],
                  "records_so_far": [r for r in (body.get("records_so_far") or []) if isinstance(r, dict)][:200],
                  "records_session": [r for r in (body.get("records_session") or []) if isinstance(r, dict)][:200],
                  "next": str(body.get("next") or "")[:6000], "n": len(ranges)}
    units = max(len(ranges) or 1, -(-len(piece) // EXCHANGE_UNIT_CHARS))
    # THE SECOND MESSAGE IS COUNTED HERE (founder 2026-09-10, design step 4:
    # "one message for curated data and one message for graph, charging the
    # same whoever reads"). With the graph add-on on, every exchange costs
    # two units at this door — the curator's and the graph reader's — and
    # the reading itself (hub/judge graph_read) charges nothing more; a
    # stretch read later, or never, was paid for with its exchange. The
    # fold's calls keep their own unit as before (the founder's to change).
    # SINCE 2026-10-03 ONE MESSAGE PER EXCHANGE, the graph included.
    # Out of allowance means out of allowance (founder 2026-08-25) — the
    # month's cap, then the pack balance (2026-09-03). The box already
    # archived this exchange; it is never curated later (no-backfill).
    why = await take_units(tenant, units)
    if why:
        return JSONResponse({"ok": False, "why": why}, status_code=429)
    cap = WINDOW_CAP_CHARS if window else PIECE_CAP_CHARS
    if len(piece) > cap:
        dropped = len(piece) - cap
        piece = (f"[…oldest {dropped} chars of this oversized exchange "
                 f"not shown — the newest text follows whole]\n"
                 + piece[-cap:])
        log(f"oversized exchange from {tenant['name']}/{project}: "
            f"billed {units} units, oldest {dropped} chars trimmed loudly")
    # THE PIECE'S TURNS (2026-09-09) ride along and come back with the save:
    # the box stamps every record, movement and ledger row with them. Ints
    # or -1; never trusted for anything but a label.
    def _turn(v) -> int:
        return v if isinstance(v, int) and not isinstance(v, bool) else -1
    await q.put((tenant, project, str(body.get("session_id") or ""), piece,
                 _turn(body.get("turn_lo")), _turn(body.get("turn_hi")), window))
    w = EXWORKER.get(tenant["id"])
    if w is None or w.done():
        EXWORKER[tenant["id"]] = asyncio.create_task(
            exchange_worker(tenant["id"]))
    meter(tenant["id"], "exchange", 0, 0, count=units)
    return JSONResponse({"ok": True, "queued": q.qsize(), "units": units})


async def exchange_worker(tenant_id: str) -> None:
    """One tenant's chain, strictly FIFO — belief-time is write-time."""
    q = EXQ[tenant_id]
    while True:
        try:
            tenant, project, session_id, piece, lo, hi, window = await asyncio.wait_for(
                q.get(), timeout=300)
        except asyncio.TimeoutError:
            EXWORKER.pop(tenant_id, None)   # idle tenant = no task
            return
        EXBUSY.add(tenant_id)
        try:
            if window:
                await process_window(tenant, project, session_id, piece, lo, hi, window)
            else:
                await process_exchange(tenant, project, session_id, piece, lo, hi)
        except Exception as e:                               # noqa: BLE001
            log(f"ERROR: exchange chain {project}: {type(e).__name__}:"
                f" {str(e)[:160]}")
        finally:
            EXBUSY.discard(tenant_id)


async def process_window(tenant: dict, project: str, session_id: str, piece: str,
                         turn_lo: int, turn_hi: int, window: dict) -> None:
    """THE SWITCH (founder 2026-09-25/26): one window of a session, two
    calls — the records call with the ledger under the project's brief, the
    graph-lines call (V6) under the purpose with the threads view — and one
    save down the pipe carrying records, ledger rows and decision lines with
    the view and the per-exchange turn ranges the box needs to file them."""
    purpose, lines = await _bearing_rules_shared(tenant, project)
    key = (tenant["id"], project)
    if not purpose.strip() and key not in BARE_SAID:
        BARE_SAID.add(key)
        log(f"bare: {tenant['name']}/{project} has no purpose rule — "
            f"curating on the base alone until its owner's agent writes one")
    n = int(window.get("n") or 1)
    names = await _project_names(tenant)
    # A WINDOW LAGS, IT IS NEVER LOST (2026-10-03, plan Next 3). On 2026-09-30
    # a provider outage dropped a live window of the studio box for good: the
    # model call's own retries end in seconds and a 5xx was taken as final.
    # Now the window is held and asked again — 1, 2, 4, 8, then every 10
    # minutes — and this tenant's later windows wait behind it, so the order
    # the memory is written in stays the order things were said (founder
    # 2026-10-02: "it's about the order"). Only after HOLD_WINDOW_S of a
    # dead provider is it dropped, loudly, so one stuck window cannot stop
    # a tenant's memory for good.
    held, wait = time.time(), 60
    while True:
        res = await judge.curate_window(DOORS, tenant, purpose, {"project": project, "piece": piece, "session_id": session_id, **window},
                                        rules=lines, names=names)
        if res.get("ok") or res.get("why") == "no piece":
            break
        lag = time.time() - held
        if lag >= HOLD_WINDOW_S:
            log(f"DROPPED uncurated (provider, held {int(lag)}s): {tenant['name']}/{project} window of {n} — {res.get('why')}")
            return
        wait = max(wait, int(res.get("retry_s") or 0))
        log(f"window HELD (provider): {tenant['name']}/{project} window of {n}, held {int(lag)}s — "
            f"{res.get('why')}; asking again in {wait}s")
        await asyncio.sleep(wait)
        wait = min(wait * 2, 600)
    if not res.get("ok"):
        log(f"DROPPED (no piece): {tenant['name']}/{project} window of {n}")
        return
    if time.time() - held > 1:
        log(f"window curated after a hold of {int(time.time() - held)}s: {tenant['name']}/{project}")
    meter(tenant["id"], "curate", 0, 0, count=n)
    got = res.get("answer") or {}
    for f in got.get("failed") or []:
        log(f"window {tenant['name']}/{project} #{turn_lo}-{turn_hi}: {f}")
    ledger_off = await store.fetchone(
        "SELECT 1 FROM ledger_off WHERE tenant_id = ? AND project IN ('', ?)", (tenant["id"], project))
    adds = [] if ledger_off else [
        a for a in (got.get("ledger") or []) if str(a.get("content") or "").strip()][:8 * n]
    records, decisions = got.get("records") or [], got.get("decisions") or []
    over = got.get("overtaken") or []
    if over or got.get("links_cut"):
        log(f"jev {tenant['name']}/{project}: {len(over)} overtaken ops, {got.get('links_cut') or 0} links cut")
    closed = [] if ledger_off else (got.get("ledger_closed") or [])
    dropped_rows = [] if ledger_off else (got.get("ledger_dropped_rows") or [])
    if got.get("ledger_mode"):
        log(f"ledger cleaner ({got['ledger_mode']}) {tenant['name']}/{project}: "
            f"{got.get('ledger_dropped') or 0} new rows dropped, {sum(bool(a.get('clean')) for a in adds)} marked, "
            f"{len(closed)} open rows done or overtaken")
    if not records and not adds and not decisions and not over and not closed and not dropped_rows:
        return                                   # a quiet window
    await push_save(tenant, {"project": project, "session_id": session_id,
                             "turn_lo": turn_lo, "turn_hi": turn_hi,
                             "records": records, "ledger": adds, "moved": [],
                             "decisions": decisions, "questions": got.get("questions") or [],
                             # the view the lines call was shown (the history design builds its own)
                             "threads": got.get("threads_used") or window.get("threads") or [],
                             "ranges": window.get("ranges") or [], "days": window.get("days") or [],
                             "overtaken": over,
                             "ledger_closed": closed, "ledger_mode": got.get("ledger_mode") or "",
                             # what the cleaner kept out, for the box's ledger_clean_log
                             "ledger_dropped_rows": dropped_rows})


async def process_exchange(tenant: dict, project: str, session_id: str,
                           piece: str, turn_lo: int = -1,
                           turn_hi: int = -1) -> None:
    # THE BRIEF IS COMPOSED HERE, EVERY TIME — base doctrine (ours, current by
    # definition) + this project's purpose + one line per bearing rule, from
    # the verdict cache. No stored copy to go stale (measured 2026-09-02: four
    # projects were running a two-day-old base because their stored brief
    # only refreshed when a teacher fired), and no birth: a project without a
    # description curates on the base and is told so, once.
    system = await _compose_brief(tenant, project)
    obj = await _judge(tenant,
                       f"NEW PIECE:\n\n{piece}\n\nDecide. JSON only.",
                       system.replace("{today}", _today()), job="curate")
    meter(tenant["id"], "curate", 0, 0)
    if obj is None:
        # Provider down: this exchange is never curated (no-backfill law —
        # recovery is curating forward). Loud, and the archive keeps it.
        log(f"DROPPED uncurated (provider): {tenant['name']}/{project}")
        return
    records = [r for r in (obj.get("records") or []) if isinstance(r, dict)]
    # What TOOK OVER FROM what. The curator is the only hub job that hears it
    # said — by the time a movement reaches a stored record the "instead of
    # X" half is usually gone. Names go down AS HEARD (2026-09-08): the
    # registrar that used to canonicalise them judged from a shadow index
    # with no aliases, counts or links, and made most of the graph's twins;
    # the box's graph worker (the add-on) folds a spelling onto its node
    # when it reads the same stretch, and drags these rows with it. Without
    # the add-on the rows still drive every "do not resurrect" card.
    # MOVEMENTS ARE THE GRAPH READER'S since 2026-09-17 (founder: "only the
    # graph needs the movement" — without the add-on there is no graph, and
    # the reader is shown the known nodes, so a movement lands on the real
    # name, never on a spelling or a sentence the curator heard). A curator
    # answer that still carries "moved" is ignored here.
    moved: list = []
    # THE LEDGER COMES OUT OF THE SAME CALL (founder 2026-09-02): the separate
    # extractor re-read every piece with its own prompt — measured at 41% of
    # token spend, 97% of it the pile block — and filed work being done as
    # intentions. The curator hears the piece once and answers records,
    # entities, movements and commitments under three separate bars.
    # The off-switch (account-wide = project '', or this project) drops the
    # rows; the clause in the prompt is universal and costs nothing to keep.
    ledger_off = await store.fetchone(
        "SELECT 1 FROM ledger_off WHERE tenant_id = ? "
        "AND project IN ('', ?)", (tenant["id"], project))
    adds = [] if ledger_off else [
        a for a in (obj.get("ledger") or []) if isinstance(a, dict)
        and str(a.get("content") or "").strip()][:8]
    # WHAT THE PIECE WAS ABOUT (founder 2026-09-22): the curator's plain
    # line with the shortcuts written out, kept beside the archive for the
    # graph reader and the healer. Every piece has one, a quiet one too.
    about = " ".join(str(obj.get("about") or "").split())[:600]
    if not records and not adds and not moved and not about:
        return                                   # a quiet piece — most are
    await push_save(tenant, {"project": project, "session_id": session_id,
                             "turn_lo": turn_lo, "turn_hi": turn_hi,
                             "records": records, "ledger": adds,
                             "moved": moved, "about": about})


# THE VOCABULARY the hub may speak down a pipe — pinned equal to the box's
# handlers (core/pipecommands.HANDLERS) by tests/test_pipe_commands.py.
# Reads return rows; writes apply an answer through the box's mechanical
# guards. Nothing else is ever sent: an unknown name is refused HERE.
# (The old inventory graph's commands — cards, known_here, since,
# graph_summary, fold_candidates, merge, drop_alias, gloss, graph_apply —
# left on 2026-09-27; a box still running them simply is not asked.)
COMMANDS = frozenset({
    # search
    "embed", "recall", "last_reply",
    # the brief's readers
    "notes", "plan_next", "proposals", "projects", "recap", "records",
    # the prompt hook's ledger
    "stamps", "stamp", "proposal_line",
    # the prompt hook's history part (the graph add-on)
    "history_near",
    # the lines call's view from the window's own records (the history design, 2026-10-02)
    "lines_near_subjects",
    # the still-true check's list from the window's own records (2026-10-02)
    "records_near_subjects",
    # the ledger cleaner's neighbours (2026-10-03)
    "ledger_near",
    # the judgements' material and their answers
    "heal_candidates", "apply_updates", "write_proposal", "write_saves", "save",
})
CMD_TIMEOUT_S = float(os.environ.get("HUB_CMD_TIMEOUT_S", "8"))
SLOW_CMD_MS = int(os.environ.get("HUB_SLOW_CMD_MS", "2000"))
CMD_INFLIGHT = int(os.environ.get("HUB_CMD_INFLIGHT", "8"))
# tenant_id -> the in-flight cap on that tenant's pipe (backpressure: a box
# is asked at most this many things at once; the rest wait, then time out)
CMD_SEMS: dict[str, asyncio.Semaphore] = {}


async def command(tenant: dict, cmd: str, body: dict | None = None,
                  timeout: float | None = None) -> dict:
    """THE ONE DOOR DOWN THE PIPE. Sends {cid, op, body}, awaits {cid, ok,
    ...} and returns it without the cid (plus `ms`). Bounded: no pipe
    refuses at once; the in-flight cap and the box's answer share one
    timeout; a late answer is dropped by the pipe loop (unknown cid). A
    refusal is a dict the caller can read, never an exception — a hook
    degrades to silence, a judgement skips its write."""
    if cmd not in COMMANDS:
        return {"ok": False, "why": f"unknown command {cmd!r}"}
    ws = PIPES.get(tenant["id"])
    if ws is None:
        return {"ok": False, "why": "no pipe"}
    sem = CMD_SEMS.setdefault(tenant["id"], asyncio.Semaphore(CMD_INFLIGHT))
    cid = hashlib.sha256(os.urandom(16)).hexdigest()[:24]
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    WAITING[cid] = fut
    t0 = time.monotonic()
    wait = timeout or CMD_TIMEOUT_S

    sent = {"at": 0.0}

    async def _ask() -> dict:
        async with sem:
            sent["at"] = time.monotonic()
            await ws.send_text(json.dumps({"cid": cid, "op": cmd, "body": body or {}}))
            return await fut

    try:
        ans = await asyncio.wait_for(_ask(), timeout=wait)
    except asyncio.TimeoutError:
        WAITING.pop(cid, None)
        # WHERE THE TIME WENT (2026-11-17, recall slow under load): queued here
        # behind this tenant's other commands, or sent and waiting on the box
        queued = (sent["at"] or time.monotonic()) - t0
        where = "never sent" if not sent["at"] else f"{time.monotonic() - sent['at']:.1f}s at the box"
        log(f"command {cmd} to {tenant['name']}: no answer in {wait:.0f}s — "
            f"{queued:.1f}s queued on the hub (cap {CMD_INFLIGHT}), {where}")
        return {"ok": False, "why": "timeout"}
    except Exception as e:                                   # noqa: BLE001
        WAITING.pop(cid, None)
        log(f"command {cmd} to {tenant['name']} failed: {type(e).__name__}: {str(e)[:120]}")
        return {"ok": False, "why": f"{type(e).__name__}"}
    if not isinstance(ans, dict):
        return {"ok": False, "why": "malformed answer"}
    ans.pop("cid", None)
    ans.setdefault("ok", False)
    ans["ms"] = round((time.monotonic() - t0) * 1000)
    if ans["ms"] > SLOW_CMD_MS:
        log(f"command {cmd} to {tenant['name']} slow: {ans['ms']} ms "
            f"({round((sent['at'] - t0) * 1000)} ms queued on the hub)")
    return ans


async def push_save(tenant: dict, body: dict) -> None:
    """DOWN THE PIPE: the curator's finished writes. No pipe = the loudest
    line this file can print, and the results are gone — the box's archive
    still holds the conversation, and heal re-derives the graph's worth over
    time."""
    if PIPES.get(tenant["id"]) is None:
        log(f"ERROR: no pipe to {tenant['name']} — save DROPPED "
            f"({len(body.get('records') or [])} records, "
            f"{len(body.get('entities') or [])} entities, "
            f"{len(body.get('ledger') or [])} ledger rows)")
        return
    try:
        ans = await command(tenant, "save", body, timeout=PUSH_TIMEOUT_S)
        if not ans.get("ok"):
            log(f"ERROR: save {'timed out' if ans.get('why') == 'timeout' else 'refused'} "
                f"by {tenant['name']}: {str(ans.get('why'))[:120]} — DROPPED")
        else:
            log(f"saved {tenant['name']}/{body['project']}: "
                f"{ans.get('records', 0)} records, "
                + (f"{ans.get('entities')} entities, " if ans.get("entities") else "")
                + f"{ans.get('ledger', 0)} ledger rows"
                + (f", {ans.get('lines')} lines" if ans.get("lines") else "")
                + (", about" if ans.get("about") else ""))
    except Exception as e:                                   # noqa: BLE001
        log(f"ERROR: save to {tenant['name']} failed: {type(e).__name__}")
    # (no finally: the pipe door `command()` owns its own cid and pops it —
    # a `WAITING.pop(cid, None)` here named a variable this function never
    # had, so EVERY save ended in a NameError after the box had written the
    # records; the exchange worker logged "exchange chain … NameError" and
    # carried on. Found 2026-09-16 in the hub's journal.)


async def _bearing_rules(tenant: dict, project: str) -> tuple[str, list[str]]:
    """(purpose, one instruction line per bearing rule) — stage-0 filtered,
    verdicts cached, purpose in the prompt AND the cache key. Project rules
    first, then the general law."""
    rows = await store.fetchall(
        "SELECT rule_id, title, content FROM rule WHERE tenant_id = ?"
        " AND (area = ? OR area = 'all') ORDER BY (area = ?) DESC, rule_id",
        (tenant["id"], project, project))
    purpose = next((str(t or "") for rid, _t, t in rows
                    if str(rid).startswith("purpose_")), "")
    todo = [(rid, title, text) for rid, title, text in rows
            if (text or title) and not str(rid).startswith("purpose_")]
    # IN PARALLEL, bounded by the tenant's inference semaphore. Measured
    # 2026-09-02 on the first compose of a new project: 42 general rules,
    # 42 sequential filter calls, 158 s — Cloudflare cut the GET at 100 s.
    # Cached forever after, but the first exchange must not wait minutes.
    verdicts = await asyncio.gather(
        *[_rule_line(tenant, project, purpose, rid, title, text)
          for rid, title, text in todo])
    lines: list[str] = []
    for (rid, title, _text), (bears, line) in zip(todo, verdicts):
        if bears:
            lines.append(line or title or rid)
    return purpose, lines


# (tenant_id, project) -> the compose in flight, so the purpose-PUT warm-up
# and a first exchange arriving seconds later share ONE set of filter calls.
COMPOSING: dict[tuple[str, str], asyncio.Task] = {}


async def _bearing_rules_shared(tenant: dict, project: str) -> tuple[str, list[str]]:
    key = (tenant["id"], project)
    task = COMPOSING.get(key)
    if task is None or task.done():
        task = COMPOSING[key] = asyncio.create_task(
            _bearing_rules(tenant, project))
    try:
        return await task
    finally:
        if COMPOSING.get(key) is task and task.done():
            COMPOSING.pop(key, None)


async def _rule_line(tenant: dict, project: str, purpose: str,
                     rid: str, title: str, text: str) -> tuple[bool, str]:
    """(bears, line) for one rule in one project, from the verdict cache.

    One model call in a rule's life: the filter answers bears + why + LINE
    together and the row is kept until the rule's text changes. A bearing
    row with no line is a verdict from before the line existed (2026-09-02)
    and is asked once more, then updated in place. A failed ask never stores
    — the rule renders under its title this time and is asked again next.
    """
    # The FILTER PROMPT is part of the key: a verdict made under an older
    # prompt is not this prompt's verdict. Changing the filter re-judges
    # every rule once, in parallel, at its next compose — no hand sweep.
    digest = jobs.sha(purpose + "\n" + (text or "") + "\n"
                      + jobs.sha(PROMPTS.get("rulefilter", "")))
    hit = await store.fetchone(
        "SELECT bears, line FROM rule_verdict WHERE tenant_id = ? AND"
        " project = ? AND rule_id = ? AND content_sha = ?",
        (tenant["id"], project, rid, digest))
    if hit is not None and (not hit[0] or str(hit[1] or "").strip()):
        return bool(hit[0]), str(hit[1] or "")
    ans = await _judge(tenant,
                       jobs.rule_prompt(project, rid, title, text,
                                        purpose=purpose),
                       PROMPTS.get("rulefilter", ""), job="rulefilter")
    bears, why, line = jobs.rule_verdict(ans)
    meter(tenant["id"], "rulefilter", 0, 0)
    if ans is None:
        # provider/parse failure: fail OPEN for this render under the rule's
        # title, store nothing, ask again on the next compose
        return True, title
    if hit is not None:
        await store.execute(
            "UPDATE rule_verdict SET bears = ?, why = ?, line = ?,"
            " decided_at = datetime('now') WHERE tenant_id = ? AND"
            " project = ? AND rule_id = ? AND content_sha = ?",
            (1 if bears else 0, why, line, tenant["id"], project, rid, digest))
    else:
        await store.execute(
            "INSERT INTO rule_verdict (tenant_id, project, rule_id,"
            " content_sha, bears, why, line) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT DO NOTHING",
            (tenant["id"], project, rid, digest, 1 if bears else 0, why, line))
    return bears, line


async def _compose_brief(tenant: dict, project: str) -> str:
    """The curator's system prompt for one project, right now. A string
    operation once the verdicts are cached; a new or changed rule costs one
    filter call, inline, and never again."""
    purpose, lines = await _bearing_rules_shared(tenant, project)
    key = (tenant["id"], project)
    if not purpose.strip() and key not in BARE_SAID:
        BARE_SAID.add(key)
        log(f"bare: {tenant['name']}/{project} has no purpose rule — "
            f"curating on the base alone until its owner's agent writes one")
    return jobs.render_brief(PROMPTS.get("curator", ""), project, purpose,
                             lines, await _project_names(tenant))




@app.get("/v1/brief/{project}")
async def get_brief(project: str, request: Request) -> JSONResponse:
    tenant = await authenticate_http(request)
    if tenant is None:
        return JSONResponse({"ok": False, "why": "unauthorized"},
                            status_code=401)
    purpose, lines = await _bearing_rules_shared(tenant, project)
    prompt = jobs.render_brief(PROMPTS.get("curator", ""), project, purpose,
                               lines, await _project_names(tenant))
    return JSONResponse({"ok": True, "project": project, "prompt": prompt,
                         "purpose": purpose, "lines": lines})


@app.websocket("/pipe")
async def pipe(ws: WebSocket) -> None:
    tenant = None
    try:
        tenant = await authenticate(ws)
    except Exception as e:
        # hub-pg down: refuse retryably, never guess at identity
        log(f"ERROR: auth unavailable ({type(e).__name__})")
        await ws.close(code=1013)  # try again later
        return
    if tenant is None:
        await asyncio.sleep(0.5)  # blunt brute force; one code, no reason
        await ws.close(code=4401)
        return
    await ws.accept()
    old = PIPES.get(tenant["id"])
    PIPES[tenant["id"]] = ws
    if old is not None:
        try:
            await old.close(code=4409)  # replaced by reconnect
        except Exception:
            pass
    log(f"pipe up: {tenant['name']}")
    try:
        while True:
            raw = await ws.receive_text()
            if len(raw) > PIPE_MAX_BYTES:
                log(f"pipe frame too large from {tenant['name']}: {len(raw):,} bytes")
                await ws.send_text(json.dumps(
                    {"ok": False, "why": "frame too large"}))
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            cid = msg.get("cid")
            if cid:  # answer to a hub-initiated op
                fut = WAITING.pop(cid, None)
                if fut and not fut.done():
                    fut.set_result(msg)
                continue  # late/unknown cid: dropped, never guessed at
            rid, op = msg.get("rid"), msg.get("op")
            if not rid or not isinstance(rid, str):
                continue
            # The raw "infer" op is GONE (2026-09-03): nothing on any box
            # sent it, and a generic model relay on our key with no meter
            # behind it is a door left open.
            if op == "ping":
                await ws.send_text(json.dumps({"rid": rid, "ok": True}))
            elif op in compose.OPS or op in judge.OPS:
                # THE BOX ASKS: for what its session is told (step 2, the
                # forward shape) or for a judgement (step 3) — composed or
                # judged here, on a task so this loop keeps receiving the
                # answers to the commands sent down the same pipe.
                asyncio.create_task(answer(ws, rid, _serve_ask(tenant, str(op), msg.get("body") or {})))
            else:
                await ws.send_text(json.dumps(
                    {"rid": rid, "ok": False, "why": "unknown op"}))
    except WebSocketDisconnect:
        pass
    finally:
        if PIPES.get(tenant["id"]) is ws:
            PIPES.pop(tenant["id"], None)
        log(f"pipe down: {tenant['name']}")


def _jev_on(tenant: dict) -> bool:
    from . import jev
    return jev.on(tenant)


class _Doors:
    """The server's doors handed to hub/judge so it imports nothing of this
    file: the model call, the meter, the window, the gates."""
    infer = staticmethod(lambda tenant, body, job="generate": infer(tenant, body, job=job))
    meter = staticmethod(lambda tid, op, i, o: meter(tid, op, i, o))
    window_closed = staticmethod(lambda: window_closed())
    window_probe = staticmethod(lambda: window_probe())
    graph_allowed = staticmethod(lambda tenant: graph_allowed(tenant))
    over_cap = staticmethod(lambda tenant: over_cap(tenant))
    heal_over_allowance = staticmethod(lambda tenant: heal_over_allowance(tenant))
    command = staticmethod(lambda tenant, cmd, body=None, timeout=None: command(tenant, cmd, body, timeout))
    jev_on = staticmethod(lambda tenant: _jev_on(tenant))
    WINDOW_CLOSE_S = property(lambda self: WINDOW_CLOSE_S)


DOORS = _Doors()


async def _purpose_of(tenant: dict, project: str) -> str:
    rows = await store.fetchall(
        "SELECT rule_id, title, content, area, pin, updated_at FROM rule"
        " WHERE tenant_id = ? ORDER BY rule_id", (tenant["id"],))
    return compose.purpose_of(jobs.rules_for(rows, project), project)


async def _serve_ask(tenant: dict, op: str, body: dict) -> dict:
    """One brief, one prompt block or one judgement for a box, through its
    own pipe. A failure is a refusal the box reads as "nothing injected" or
    "no answer", never an exception on the loop."""
    t0 = time.monotonic()

    async def ask(cmd: str, b: dict) -> dict:
        return await command(tenant, cmd, b)

    try:
        if op in compose.OPS:
            # the graph add-on is the hub's to say, from the tenant row —
            # whatever a box put under the key is overwritten
            from hub import jev as _jev
            body = {**body, "_graph": graph_allowed(tenant), "_jev": _jev.on(tenant)}
            res = await compose.OPS[op](ask, store.fetchall, SEED_VECS, tenant["id"], body)
            meter(tenant["id"], op, 0, 0)
            if res.get("jev_tokens"):
                meter(tenant["id"], "infer:jev", int(res["jev_tokens"]), 0)
        elif op == "propose":
            async def purposes(name: str) -> str:
                return await _purpose_of(tenant, name)
            res = await judge.propose(DOORS, tenant, purposes, body)
        else:
            project = str(body.get("project") or "")
            purpose = await _purpose_of(tenant, project)
            if op == "graph_read" and project:
                # THE READER IS TAUGHT LIKE THE CURATOR (founder 2026-09-17):
                # the same bearing-rule lines, from the same verdict cache.
                _, lines = await _bearing_rules_shared(tenant, project)
                body = dict(body, _rules=lines)
            if op == "curate_probe" and project:
                # THE BRIEF UNDER TEST (2026-09-26): with_rules adds the
                # project's filtered rule lines, as the records call has
                # them; no_purpose drops the description. Rule lines only
                # ever come from the hub's own table, never from the box.
                body = {k: v for k, v in body.items() if k != "_rules"}
                if body.get("with_rules"):
                    _, lines = await _bearing_rules_shared(tenant, project)
                    body = dict(body, _rules=lines)
                if body.get("no_purpose"):
                    purpose = ""
            if op == "curate_window" and project:
                # THE SAME RECORDS CALL AS THE LIVE WINDOW (2026-09-26, the
                # records replay): process_window's brief — the project's
                # bearing-rule lines and the project list — not the bare purpose
                purpose, lines = await _bearing_rules_shared(tenant, project)
                res = await judge.curate_window(DOORS, tenant, purpose, body, rules=lines,
                                                names=await _project_names(tenant))
            else:
                res = await judge.OPS[op](DOORS, tenant, purpose, body)
    except Exception as e:                                   # noqa: BLE001
        log(f"{op} for {tenant['name']} failed: {type(e).__name__}: {str(e)[:120]}")
        return {"ok": False, "why": f"{type(e).__name__}"}
    ms = round((time.monotonic() - t0) * 1000)
    if op in compose.OPS and ms > COMPOSE_SLOW_MS:
        log(f"{op} slow for {tenant['name']}: {ms} ms (part {body.get('part')})")
    if op in judge.OPS and ms > INFER_SLOW_S * 1000:
        log(f"{op} slow for {tenant['name']}: {ms} ms")
    if not res.get("ok") and op in judge.OPS:
        log(f"{op} refused for {tenant['name']}: {str(res.get('why'))[:100]}")
    res["ms"] = ms
    return res


# A composed block past this is said in the log — the design's budget is
# about half a second hosted, a second on a home line.
COMPOSE_SLOW_MS = int(os.environ.get("HUB_COMPOSE_SLOW_MS", "2000"))


async def answer(ws: WebSocket, rid: str, coro) -> None:
    res = await coro
    res["rid"] = rid
    try:
        await ws.send_text(json.dumps(res))
    except Exception:
        pass  # pipe died while we worked; container will retry on reconnect


@app.get("/health")
async def health() -> dict:
    db_ok = False
    try:
        await store.fetchone("SELECT 1")
        db_ok = True
    except Exception:
        pass
    return {"pipes": len(PIPES), "db": db_ok,
            "provider_configured": bool(OLLAMA_KEY or models.ai()),
            "usage_buffered": len(USAGE_BUFFER),
            "texts": texts_digest(),
            **outbound_state()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080,
                ws_max_size=PIPE_MAX_BYTES)
