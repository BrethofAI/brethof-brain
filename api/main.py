"""brethof-brain v2 — data-plane API.

Endpoint contract is IDENTICAL to v1's on purpose. The same client talks to
either server by changing one base URL, which is what makes the side-by-side
test cheap and the rollback trivial: v1 keeps serving live memory while this
answers the same calls against the migrated copy.

Two rules the handlers below exist to keep:

MEMORY IS AN ENHANCEMENT, NEVER A GATE. Every read path returns `ok` with an
empty injection when something is wrong. A user's session must not break because
recall had a bad day — so recall failures are a status, never a 500.

NOTHING FAILS QUIETLY. The counterpart to the rule above: every degradation is
logged and the unembedded backlog is exposed on /v1/health. v1's silences were
survivable individually and catastrophic together.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import time
import asyncio
import traceback
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException

from core import (archive, auth, drainer, envelope, injection,
                  metering, quota, ratelimit, recall_pg, redact, store, embed)
from . import curate_queue
from core import mode

# No public swagger: /docs listed every endpoint name (incl. /v1/internal/*)
# to the internet. Customers integrate via MCP config + website docs.
_DEAD_TABLES = ("curate_job", "gate_job", "teach_job", "turn_job", "merge_job",
                "curate_marker", "curator_brief", "graph_backfill",
                "recurate_mark", "recurate_graph_mark", "inject_ledger",
                "rule_ledger", "doc", "doc_ids", "entity_ids", "mention",
                "abuse_signal", "abuse_verdict", "tenant_flags",
                # 2026-09-17: the record-to-thing link — the curated records
                # and the graph are two systems (founder); a record points
                # only into the archive
                "entity_record",
                # 2026-09-27: the old inventory graph — its nodes, mentions,
                # links, reader queue and every table its passes kept; the
                # decision lines (core/decisionline) replace it (founder)
                "entity", "entity_mention", "entity_link", "graph_queue",
                "movement_shadow", "graph_ended", "graph_heal_state", "fact",
                "fact_close_state", "entity_merge", "exchange_about",
                "graph_correction", "entity_type", "same_verdict",
                "name_suggestion", "graph_answer", "bench_variant")
_DEAD_SCHEMAS = ("pre", "sync")


def _drop_dead_tables() -> None:
    """One deliberate migration (founder 2026-09-02: "the things which are
    dead, we need to clean — that's making only mess"). Every table here
    belonged to a lane deleted this week — the box-side curator, the gate,
    the teach lane, the compact-era queue, the cloud-plane judge — or to an
    experiment that left an empty schema behind. Idempotent, per tenant, and
    it SAYS what it dropped; a boot that drops nothing prints nothing."""
    try:
        tenants = store.provisioned_tenants()
    except Exception as e:                                     # noqa: BLE001
        print(f"[migrate] tenants unreadable: {type(e).__name__}: {e}", flush=True)
        return
    for tenant in tenants:
        tok = store.bind(tenant)
        try:
            with store.connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' "
                            "AND tablename = ANY(%s);", [list(_DEAD_TABLES)])
                present = [r[0] for r in cur.fetchall()]
                cur.execute("SELECT nspname FROM pg_namespace WHERE nspname = ANY(%s);",
                            [list(_DEAD_SCHEMAS)])
                schemas = [r[0] for r in cur.fetchall()]
                for t in present:
                    cur.execute(f'DROP TABLE IF EXISTS "{t}" CASCADE;')
                for s in schemas:
                    cur.execute(f'DROP SCHEMA IF EXISTS "{s}" CASCADE;')
                # THE LOCAL RULE COPY (founder 2026-09-02: no rules on customer
                # boxes — the hub is the only store, sessions fetch them).
                cur.execute("DELETE FROM document WHERE project='rules';")
                rules_gone = cur.rowcount
                conn.commit()
            if present or schemas or rules_gone:
                print(f"[migrate] {tenant}: dropped tables {present} "
                      f"schemas {schemas}, local rule copies {rules_gone}",
                      flush=True)
        except Exception as e:                                 # noqa: BLE001
            print(f"[migrate] {tenant}: FAILED {type(e).__name__}: {e}", flush=True)
        finally:
            store.unbind(tok)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Start the background workers, once, at boot.

    LIFESPAN, NOT @app.on_event("startup") — FastAPI deprecated that and will
    eventually remove it, taking this wiring with it. Nothing here was broken;
    the framework warned us twice on every test run and the release gate
    surfaced the warning, so it is moved before it is moved for us.

    The write paths only QUEUE embeddings (embedding IS NULL + a partial
    index). These workers are what consume the queue — without them every
    post-migration row stays unembedded forever, which the compact rehearsal
    drill demonstrated on 2026-07-26 (backlog 12, four polls, no movement)."""
    mode.validate()
    if mode.is_selfhosted():
        # Nobody provisions this box but itself: there is no control plane to
        # call CREATE DATABASE on a subscription. Idempotent, so it is also
        # the upgrade path -- a release that adds schema or changes the seed
        # rules applies them on the next boot.
        t = mode.selfhosted_tenant()
        # RETRY, don't die. Compose gates on a healthcheck, but ordering is not
        # a guarantee on somebody else's machine: a slow disk, a big recovery,
        # a laptop waking up. Exiting here means the memory never comes back
        # and the customer has to know to run a command. Loud on every attempt
        # so a real failure is never mistaken for slowness.
        for attempt in range(1, 31):
            try:
                created = store.provision_tenant(t)
                print(f"[mode] tenant {t}: "
                      f"{'created' if created else 'present'}", flush=True)
                break
            except Exception as e:                       # noqa: BLE001
                print(f"[mode] database not ready (attempt {attempt}/30): "
                      f"{type(e).__name__}: {str(e)[:120]}", flush=True)
                await asyncio.sleep(2)
        else:
            raise SystemExit(
                f"[mode] gave up after 60s waiting for the database. The "
                f"memory container is not answering — check `docker compose "
                f"logs brain-db`.")
    # Self-hosted starts a SUBSET: the mechanical paths (recall, archive,
    # embedding) run everywhere; anything needing a prompt or a bill does not
    # start at all on a customer's box. core/mode.py says which and why, and
    # prints the list at boot — a worker that starts and silently fails every
    # job is worse than one that never started.
    _drop_dead_tables()
    # BRAIN_WORKERS=off (the graph bench's second api, 2026-09-20): a box
    # that serves doors and holds a pipe but runs NO background worker —
    # no drainer, no curation lane — so a second api on the same database,
    # dialled to a different hub, can never curate the tenant's memory
    # through that hub. Said at boot, loudly.
    workers = os.environ.get("BRAIN_WORKERS", "on").strip().lower() != "off"
    if workers:
        drainer.start()
    else:
        print("[mode] BRAIN_WORKERS=off — no drainer, no curation lane: doors and the pipe only",
              flush=True)
    # The pipe: the wire the hub speaks down, carrying finished saves from
    # the write loop. Self-gates on BRAIN_HUB_KEY — a box without a licence
    # simply never dials.
    from core import pipeclient
    pipeclient.start()
    if workers:
        curate_queue.start()      # the prune lane; runs on every box
        # THE RETIREMENT SWEEP (2026-10-07): each tenant's backlog once, then
        # every line that drops or replaces something, judged on the hub
        from core import retire
        retire.start()
    yield
    # Nothing to unwind: every worker above is a daemon thread that dies with
    # the process, and the queues are durable in Postgres — an interrupted job
    # is picked up on the next boot, which is the lag-not-loss contract.


app = FastAPI(title="brethof-brain v2", version="2.0.0-sidebyside",
              docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=_lifespan)


def _extract_key(x_api_key: str, authorization: str) -> str:
    key = x_api_key
    if not key and authorization:
        parts = authorization.split(None, 1)
        key = parts[1].strip() if len(parts) == 2 else authorization.strip()
    return key


def resolve_caller(x_api_key: str = Header(default="", alias="X-API-Key"),
                   authorization: str = Header(default="")) -> str:
    """Accepts `X-API-Key` or `Authorization: Bearer <key>`.

    v1 used the Authorization header, so honouring both is what lets an existing
    client entry point at v2 by editing only its URL.
    """
    tenant = auth.resolve(_extract_key(x_api_key, authorization))
    if not tenant:
        # 401 with the envelope shape, so the client renders a real reason
        # instead of a blank injection it cannot explain.
        raise HTTPException(status_code=401, detail=envelope.envelope(
            "auth_failed", notice="unknown or missing API key"))
    # (restore-on-touch was the cloud plane's; a hosted memory is unlocked
    #  by its passphrase through brain-host, never restored here)
    return tenant


def resolve_tier(x_api_key: str = Header(default="", alias="X-API-Key"),
                 authorization: str = Header(default="")) -> str:
    """'owner' | 'customer' for the calling key. Unknown keys never get here —
    resolve_caller already 401s — but fail CLOSED to 'customer' regardless."""
    return auth.resolve_tier(_extract_key(x_api_key, authorization)) or "customer"


def resolve_plan_dep(x_api_key: str = Header(default="", alias="X-API-Key"),
                     authorization: str = Header(default="")) -> str | None:
    """The key's billing plan — None for quota-exempt static (owner) keys."""
    return auth.resolve_plan(_extract_key(x_api_key, authorization))


def resolve_ents_dep(x_api_key: str = Header(default="", alias="X-API-Key"),
                     authorization: str = Header(default="")) -> dict | None:
    """Purchased top-ups riding on the key (pack credits, extra storage) —
    None for static keys, which are quota-exempt anyway."""
    return auth.resolve_entitlements(_extract_key(x_api_key, authorization))


def resolve_state_dep(x_api_key: str = Header(default="", alias="X-API-Key"),
                      authorization: str = Header(default="")) -> str | None:
    """The key's SUBSCRIPTION state (active | past_due | frozen) — None for
    billing-exempt static keys. The door reads it before the meters."""
    return auth.resolve_state(_extract_key(x_api_key, authorization))


def resolve_scopes_dep(x_api_key: str = Header(default="", alias="X-API-Key"),
                       authorization: str = Header(default="")):
    """Project scopes for the calling key — None = full access."""
    return auth.resolve_scopes(_extract_key(x_api_key, authorization))


def rate_gate(tenant: str = Depends(resolve_caller),
              plan: str | None = Depends(resolve_plan_dep)) -> None:
    """Per-ACCOUNT request rate (2026-08-24 audit: zero limits existed and
    ambient recall embeds inline on shared CPU). Every key an account mints
    draws one bucket; plan-less (our own) tenants are exempt. The client is
    fail-open: a 429'd hook is a silent no-op, a 429'd stop keeps its
    spool — data waits, it is never lost."""
    ok, retry = ratelimit.allow(tenant, plan)
    if not ok:
        # Tripwire for the judge (founder redesign 2026-08-24): a flood
        # that keeps hitting the wall is exactly what the evaluator should
        # look at. Throttled inside note_refusal; never blocks the 429.
        ratelimit.note_refusal(tenant)      # counted; the judge that read it
                                            # returns on the hub (2026-09-02)
        raise HTTPException(status_code=429, detail=ratelimit.NOTICE,
                            headers={"Retry-After": str(retry)})


def require_full_access(scopes=Depends(resolve_scopes_dep)) -> None:
    """Hooks and raw recall are the FULL-CLIENT surface — a project-scoped key
    reaching them would see (and write) past its scopes, because injection,
    archiving and fused recall are tenant-wide by design. Scoped keys get the
    memory tools; these endpoints demand an unscoped key. Closes the bypass
    found in the 2026-07-28 what-is-missing audit."""
    if scopes is not None:
        raise HTTPException(status_code=403, detail=envelope.envelope(
            "scoped_key",
            notice="this key is project-scoped — hooks and recall need a "
                   "full-access key; use the memory tools instead"))


def _internal_guard(authorization: str = Header(default="")) -> None:
    """Control-plane-only endpoints. Same shared token both directions."""
    token = auth.internal_token()
    parts = authorization.split(None, 1)
    presented = parts[1].strip() if len(parts) == 2 else ""
    if not token or not presented or not hmac.compare_digest(presented, token):
        raise HTTPException(status_code=401, detail="Unauthorized (internal)")


def _project(payload: dict) -> str:
    p = (payload.get("project") or "").strip().lower()
    return p if p.replace("_", "").isalnum() and p else ""


@app.get("/v1/health")
def health():
    """Liveness AND the embedding backlog. The backlog belongs in health
    because 'rows waiting to be embedded' is precisely the number that went
    unwatched in v1 until 30% of the archive was invisible.

    DB REACHABILITY IS PART OF HEALTH (2026-07-28): a deploy with an empty
    password left every tenant pool dead for minutes while this endpoint kept
    answering ok — the reflex watches this URL, so 'ok' must mean 'can serve',
    not 'process exists'."""
    db_ok = True
    try:
        tens = auth.tenants()
        if tens:
            tok = store.bind(tens[0])
            try:
                with store.connection() as conn:
                    conn.cursor().execute("SELECT 1;")
            finally:
                store.unbind(tok)
    except Exception:
        db_ok = False
    # UNBILLED WORK (2026-08-13): inference completions whose usage row could
    # not be written on a tenant that still exists. Published here for the
    # same reason the backlog is — a metering failure used to be a printed
    # traceback and nothing else, which is how v1 lost months of usage.
    # IS THIS BOX STILL BEING HEARD? (2026-09-08) The up-wire prints a line
    # per dropped exchange and nothing else reads it, so a box whose resolver
    # died feeds nothing, learns nothing, and answers ok here — measured on
    # our own hub for 45 hours, and on 26 novel lanes in August. An exchange
    # the hub never saw is never curated, so this is the number to watch.
    # BOTH WIRES, not just the one going out. A box whose pipe is down answers
    # every line below perfectly while the hub drops everything it curates for
    # it — the same shape of blindness as the up-wire's, and worth the same
    # one line of truth.
    from core import hubfeed, pipeclient
    return {"status": "ok" if db_ok else "db_unreachable",
            "keys_loaded": auth.key_count(),
            "plaintext_keys": auth.plaintext_key_count(), "db": db_ok,
            "metering_lost": metering.lost(),
            **hubfeed.state(), **pipeclient.state()}


@app.get("/v1/usage")
def usage(tenant: str = Depends(resolve_caller),
          plan: str | None = Depends(resolve_plan_dep),
          ents: dict | None = Depends(resolve_ents_dep)):
    """The caller's own plan and period-to-date consumption.

    BUILT 2026-08-08. It did not exist — yet the client library called it and
    the CLI displayed it in three places, so `usage` was dead on arrival for
    every customer and the clean-room install test 404'd here. The data was
    always available locally (quota reads the bound tenant); nothing was
    missing but the door.

    Own-tenant only, so it needs no tier gate: a key can only ever report
    itself."""
    tok = store.bind(tenant)
    try:
        v = quota.check(plan, ents=ents)
        return {"status": "ok",
                "plan": v.get("plan") or plan or "unmetered",
                "period": quota.period_now(),
                "exchanges_used": quota.used_exchanges(),
                "storage_mb": round(quota.storage_mb(), 2),
                "pack_credits": (ents or {}).get("pack_credits", 0),
                "extra_storage_mb": (ents or {}).get("extra_storage_mb", 0),
                # What the caller can still DO — the part a client acts on.
                "can_learn": v.get("learn", True),
                "can_archive": v.get("archive", True),
                "warn": v.get("warn", "")}
    except Exception:
        traceback.print_exc()
        return envelope.server_error(notice="usage temporarily unavailable")
    finally:
        store.unbind(tok)


@app.get("/v1/health/backlog")
def backlog(tenant: str = Depends(resolve_caller)):
    tok = store.bind(tenant)
    try:
        return {"status": "ok", "unembedded": archive.unembedded_counts()}
    finally:
        store.unbind(tok)


@app.post("/v1/hooks/session-start")
def session_start(payload: dict, tenant: str = Depends(resolve_caller),
         _rl=Depends(rate_gate),
         plan: str | None = Depends(resolve_plan_dep),
         tier: str = Depends(resolve_tier),
         sub_state: str | None = Depends(resolve_state_dep),
         _=Depends(require_full_access)):
    project = _project(payload)
    if not project:
        return envelope.envelope("unknown_project", notice="no project given")
    # part=1..N → one budgeted chunk per registered SessionStart hook (the
    # harness caps EACH hook's output at 10k chars); part absent/0 → whole
    # payload, for legacy clients and tests.
    try:
        part = int(payload.get("part") or 0)
    except (TypeError, ValueError):
        part = 0
    tok = store.bind(tenant)
    t0 = time.perf_counter()
    try:
        # THE DOOR (founder 2026-08-25, replacing the full-or-off of
        # 2026-08-24): a limit NEVER takes recall away — "that's the candy
        # which I would give them... it's mechanical, only CPU, that's
        # their memory." The session brief IS recall, so a limited account
        # still receives it, with the wall notice riding in front. The only
        # thing that ends recall is the database leaving the box on the
        # retention ladder, and that is lifecycle, not the door.
        notice = ""
        try:
            _state, notice = quota.door(plan, state=sub_state)
        except Exception:
            traceback.print_exc()      # the door fails OPEN, loudly
        # `source` is the harness's reason for the start (startup / resume /
        # compact) — after a compact the agent holds the whole session and
        # is asked to check the project description (founder 2026-09-02).
        blob = injection.session_blob(project, part=part, tier=tier,
                                      source=str(payload.get("source") or ""),
                                      session_id=str(payload.get("session_id") or ""))
        # THE LEFTOVERS OF EARLIER SESSIONS GO NOW (2026-10-02): windows go at
        # five exchanges with no timer, so what a finished session left
        # waiting is sent at the next session's start, once per start
        if part <= 1:
            try:
                from core import hubfeed
                hubfeed.on_session_start(project, str(payload.get("session_id") or ""))
            except Exception:
                traceback.print_exc()
        # ACCOUNT STATE GREETS THE SESSION, not the failure: a user at 85%,
        # or one whose card just failed, reads it HERE — days before
        # anything pauses. The DOOR's notice is the one source (usage
        # warning and dunning warning already combined there); reading it
        # from check() again both duplicated the work and silently dropped
        # the billing half — found by the 2026-08-24 billing live-fire.
        # First part only, so a warning is never doubled.
        # A closed account with no memory yet must still hear the notice —
        # dropping it when the blob is empty is how someone learns about a
        # wall only from their next invoice. The HORIZON rides with it: a
        # limited account keeps its recall, so it must also be told what
        # that recall no longer knows, and from which date.
        if part <= 1 and notice:
            try:
                edge = quota.horizon_sentence(_state)
            except Exception:
                traceback.print_exc()
                edge = ""
            head = f"{notice} {edge}".strip() if edge else notice
            blob = f"⚠ {head}\n\n{blob}" if blob else f"⚠ {head}"
    except Exception:
        traceback.print_exc()          # loud in logs, quiet to the user
        return envelope.server_error(notice="memory temporarily unavailable")
    finally:
        store.unbind(tok)
    return envelope.ok(injection=blob, ms=round((time.perf_counter()-t0)*1000))


@app.post("/v1/hooks/prompt-submit")
def prompt_submit(payload: dict, tenant: str = Depends(resolve_caller),
         _rl=Depends(rate_gate),
         plan: str | None = Depends(resolve_plan_dep),
         sub_state: str | None = Depends(resolve_state_dep),
         _=Depends(require_full_access)):
    project = _project(payload)
    prompt = (payload.get("prompt") or "").strip()
    session_id = payload.get("session_id") or ""
    if not project or not prompt or not session_id:
        return envelope.ok()           # nothing to inject is not an error
    # part=1/2 → one registered hook per injected record (each rides its own
    # 10K harness cap); absent/0 → the legacy single-blob shape.
    try:
        part = int(payload.get("part") or 0)
    except (TypeError, ValueError):
        part = 0
    tok = store.bind(tenant)
    t0 = time.perf_counter()
    try:
        # THE DOOR DOES NOT GATE RECALL (founder 2026-08-25). A limited
        # account gets its memory exactly as a paying one does; what it
        # also gets is one short line saying what has stopped, on every
        # prompt, so the gap being dug is visible NOW instead of being
        # discovered in three weeks when the memory has drifted. Short on
        # purpose — this rides inside the customer's own context window.
        state = "active"
        try:
            state = quota.door(plan, state=sub_state)[0]
        except Exception:
            traceback.print_exc()      # the door fails OPEN, loudly
        # THE RETRIEVAL LOG (founder 2026-08-24: "we can evaluate them on
        # logs — how much they using this"): every ambient recall counts
        # in the per-tenant daily table, the judge's raw material. The
        # lane logs sheds the same way; together they are the usage shape.
        metering.count_tool("ambient_recall")
        # THE NEXT PROMPT PUSHES A FULL WINDOW (2026-09-26): the five cached
        # exchanges go up with this prompt as THE NEXT MESSAGE, once per
        # prompt (the sibling parts of one prompt share the session's rows;
        # the second part finds nothing full)
        if part <= 1:
            try:
                from core import hubfeed
                hubfeed.on_prompt(project, session_id, prompt)
            except Exception:
                traceback.print_exc()
            # the session is active in this project now (the save gap, 2026-09-28)
            try:
                with store.connection() as conn:
                    injection.note_prompt(conn, conn.cursor(), project, session_id)
            except Exception:
                traceback.print_exc()
        # a harness that adds our text to one model call only (OpenClaw's
        # prependContext, OpenCode 2's context hook) says "ephemeral": its
        # records are not cooled (core/composer.ambient)
        blob = injection.ambient_blob(project, session_id, prompt, part=part,
                                      ephemeral=bool(payload.get("ephemeral")))
        line = quota.prompt_line(state)
        if part <= 1 and line:
            blob = f"{line}\n\n{blob}" if blob else line
    except Exception:
        traceback.print_exc()
        return envelope.ok()           # ambient recall is strictly best-effort
    finally:
        store.unbind(tok)
    return envelope.ok(injection=blob, ms=round((time.perf_counter()-t0)*1000))


@app.post("/v1/hooks/stop")
def stop(payload: dict, tenant: str = Depends(resolve_caller),
         _rl=Depends(rate_gate),
         plan: str | None = Depends(resolve_plan_dep),
         ents: dict | None = Depends(resolve_ents_dep),
         sub_state: str | None = Depends(resolve_state_dep),
         _=Depends(require_full_access)):
    project = _project(payload)
    turns = payload.get("turns") or []
    if not project or not turns:
        return envelope.ok(archived=0)
    session_id = payload.get("session_id", "")
    tok = store.bind(tenant)
    try:
        # THE DOOR (founder 2026-08-24, replacing the 2026-07-30 soft
        # degrade): FULL OR OFF. Out of quota — learning OR storage — the
        # whole account closes and NOTHING is archived: an over-quota turn
        # that entered the archive would either be curated for free later
        # (the heal-sweep hole, measured 2026-08-23) or need a permanent
        # unpaid-marks system to keep it dead — the soft path IS the
        # complexity. The client keeps its spool and retries; on reopen the
        # spool lands as paid work. The refusal notice is the wall copy —
        # at this moment it is the product.
        quota.note_plan(plan, ents)
        state, notice = "active", ""
        try:
            state, notice = quota.door(plan, ents=ents, state=sub_state)
        except Exception:
            traceback.print_exc()      # the door fails OPEN, loudly
        # RECORDING IS GATED BY STORAGE, NOT BY THE ALLOWANCE (founder
        # 2026-08-25). Writing a turn into disk they have already bought
        # costs us no tokens; DISTILLING it costs the allowance they have
        # spent. So an out-of-allowance account keeps its history — and,
        # because we never learn from old conversation afterwards
        # (rules:never_back_curate_old_chat), that history staying
        # searchable is the only access this period will ever have.
        if not quota.may_record(state):
            return envelope.over_cap(notice=notice)
        # Only what the archive takes for the first time is new conversation:
        # a compact's re-flush re-sends turns it already holds (2026-09-28).
        from core import hubfeed
        feeding = bool(quota.may_learn(state) and session_id and hubfeed.active())
        fresh = archive.unseen(session_id, turns) if feeding else []
        n = archive.archive_turns(project, session_id, turns)
        # METERING-FIRST (founder 2026-07-30, panel showed him "0 messages"):
        # count EVERY exchange, plan or not — the number must be reality for
        # owner/static traffic too. Enforcement stays plan-gated in
        # quota.check(); plan-less tenants are counted, never capped.
        try:
            quota.bump_exchanges(1)
        except Exception:
            traceback.print_exc()
        # LEARNING IS THE PAID PART. An archived-but-unlearned turn is a
        # legitimate state — exactly what an out-of-allowance account
        # produces — and it is never curated later (no-backfill).
        #
        # WHO CURATES (2026-09-01): a box holding a hub key feeds each
        # archived exchange UP to the hub — the hub is the curator, the
        # quota door, and the usage meter, all on this one wire. The
        # client-side parallel mirror this replaces was a transition
        # mechanism no stamped customer ever had config for, which meant a
        # real customer's exchanges were archived and never curated at
        # all. Boxes WITHOUT a hub key keep the local agent-offered queue.
        if feeding:
            hubfeed.feed(project, session_id, fresh)
        # A box WITHOUT a hub key archives and does not curate: the box-side
        # curator that used to take this branch was deleted 2026-09-02 (dead
        # since 08-29; no prompts ship to a box, so it could never have run
        # on a customer's). Curation is the hub's, or it is not.
        # PER-EXCHANGE FAST HEAL (founder 2026-08-31: "waiting for end of
        # the session is pretty long"). The compact trigger stays; this
        # fires on every flush and is free when idle — the watermark makes
        # an empty run zero-inference, and reconcile's one-per-project lock
        # stops pile-ups. The judgement is the hub's (design step 3).
        # Hub-written records land asynchronously, so each trigger heals
        # what earlier exchanges already produced; the next one covers this
        # exchange's records.
        try:
            from core import reconcile
            reconcile.trigger(tenant, project)
        except Exception:
            traceback.print_exc()
    except Exception:
        traceback.print_exc()
        # A write failure IS reported — the client must keep its spool and
        # retry rather than advancing its offset over lost turns.
        return envelope.server_error(notice="archive temporarily unavailable")
    finally:
        store.unbind(tok)
    return envelope.ok(archived=n, notice=notice)


@app.post("/v1/hooks/commit")
def commit(payload: dict, tenant: str = Depends(resolve_caller),
         _rl=Depends(rate_gate),
         _=Depends(require_full_access)):
    project = _project(payload)
    if not project:
        return envelope.envelope("unknown_project")
    tok = store.bind(tenant)
    try:
        archive.record_commit(project, payload)
    except Exception:
        traceback.print_exc()
        return envelope.server_error()
    finally:
        store.unbind(tok)
    return envelope.ok()


@app.post("/v1/tools/split-pass")
def split_pass(payload: dict, tenant: str = Depends(resolve_caller),
               _rl=Depends(rate_gate),
               _=Depends(require_full_access)):
    """Our own tool (founder 2026-09-18): the one-time split pass over a
    project's long curated records — core/splitpass, asked by
    scripts/records_split.py. Starts the pass in a thread and returns; the
    pass says everything it does in the log under "[split] <project>:"."""
    project = _project(payload)
    try:
        min_len = max(200, int(payload.get("min_len") or 800))
        limit = max(1, int(payload.get("limit") or 100000))
    except (TypeError, ValueError):
        return envelope.ok(notice="min_len and limit must be integers")
    from core import splitpass
    line = splitpass.trigger(tenant, project, min_len=min_len, limit=limit,
                             dry=bool(payload.get("dry")))
    print(f"[split] {tenant}/{project}: {line}", flush=True)
    return envelope.ok(notice=line)


@app.post("/v1/tools/bench")
def graph_bench(payload: dict, tenant: str = Depends(resolve_caller),
                _rl=Depends(rate_gate),
                _=Depends(require_full_access)):
    """Our own tool (the bench, founder 2026-09-20): one scratch tenant per
    variant, the rigs of core/bench. Body: {action: status | drop |
    decisions | decision_find | decision_dump | heal_replay | heal_dump |
    record_cross | record_pairs | records_replay* | backfill*, variant, ...}.
    (The old inventory graph's actions — load, drain, cards, fold, close,
    facts, fact_cards, describe, probe — left on 2026-09-27.)"""
    from core import bench
    action = str(payload.get("action") or "status").strip().lower()
    variant = str(payload.get("variant") or "").strip()
    try:
        if action in ("records_replay", "records_replay_status", "records_replay_stop"):
            # THE RECORDS REPLAY (founder 2026-09-26: "curate everything,
            # starting from the oldest one … the only thing which we need is
            # a date"): the live records job over the archive into an EMPTY
            # scratch tenant (the variant's), never the memory itself.
            from core import records_replay
            dst = bench.tenant_of(variant)
            if action == "records_replay_status":
                return envelope.ok(notice="status", **json.loads(json.dumps(records_replay.status(dst), default=str)))
            if action == "records_replay_stop":
                return envelope.ok(notice=records_replay.stop())
            projects = [str(p) for p in (payload.get("projects") or []) if str(p).strip()]
            if not projects:
                return envelope.envelope("bad_request", notice="records_replay needs projects: [...]")
            store.provision_tenant(dst)
            return envelope.ok(notice=records_replay.start(projects, dst, until=str(payload.get("until") or ""),
                                                           tries=int(payload.get("tries") or records_replay.TRIES),
                                                           limit_windows=int(payload.get("limit") or 0),
                                                           since=str(payload.get("since") or ""),
                                                           opts={k: payload[k] for k in ("records_prompt", "twin", "spans")
                                                                 if payload.get(k)}))
        if action in ("backfill", "backfill_status", "backfill_redo", "backfill_stop"):
            # THE GRAPH BACKFILL (founder 2026-09-25): the lines of a project's
            # whole archive on the local 27B, into a variant tenant for the
            # proven sample first (dst = the variant), then the memory itself
            # (dst: "memory", said in so many words). Detached, resumable,
            # three tries per window, a skip log; status reads the state.
            from core import backfill
            dst = "memory" if str(payload.get("dst") or "") == "memory" else bench.tenant_of(variant)
            if action == "backfill_status":
                return envelope.ok(notice="status", **json.loads(json.dumps(backfill.status(dst), default=str)))
            if action == "backfill_stop":
                return envelope.ok(notice=backfill.stop())
            if action == "backfill_redo":
                # the skipped windows of one project, asked again (in a thread)
                p = str((payload.get("projects") or [""])[0] or "")
                if not p:
                    return envelope.envelope("bad_request", notice="backfill_redo needs projects: [one]")
                import threading
                threading.Thread(target=backfill.redo_skips, args=(p, dst), kwargs={"log": lambda m: print(m, flush=True)},
                                 name=f"backfill-redo-{p}", daemon=True).start()
                return envelope.ok(notice=f"redo of {p}'s skipped windows started → {dst}")
            projects = [str(p) for p in (payload.get("projects") or []) if str(p).strip()]
            if not projects:
                return envelope.envelope("bad_request", notice="backfill needs projects: [...]")
            if dst != "memory":
                store.provision_tenant(dst)
            return envelope.ok(notice=backfill.start(projects, source=str(payload.get("source") or "memory"), dst=dst,
                                                     tries=int(payload.get("tries") or backfill.TRIES),
                                                     limit_windows=int(payload.get("limit") or 0),
                                                     only_session=str(payload.get("session") or ""),
                                                     since=str(payload.get("since") or ""),
                                                     spans=[(str(a or ""), str(b or "")) for a, b in (payload.get("spans") or [])] or None,
                                                     until=str(payload.get("until") or "")))
        if action == "drop":
            return envelope.ok(notice=bench.drop(variant, force=bool(payload.get("force"))))
        if action == "decisions":
            exs = [e for e in (payload.get("exchanges") or []) if isinstance(e, dict)]
            if not exs:
                return envelope.envelope("bad_request", notice="decisions needs exchanges: [{project, session, turn}, ...]")
            return envelope.ok(notice=bench.decisions(variant, exs, int(payload.get("workers") or 8), tenant,
                                                      nxt=bool(payload.get("next")),
                                                      seen_records=bool(payload.get("seen_records")),
                                                      window=int(payload.get("window") or 1),
                                                      prompt=str(payload.get("prompt") or "decisions"),
                                                      window_chars=int(payload.get("window_chars") or 40000),
                                                      headers=bool(payload.get("headers")),
                                                      json_mode=bool(payload.get("json"))))
        if action == "decision_find":
            qs = [q for q in (payload.get("prompts") or []) if isinstance(q, dict)]
            return envelope.ok(notice=f"{len(qs)} prompts", found=json.loads(json.dumps(
                bench.decision_find(variant, qs, bool(payload.get("check")),
                                    version=str(payload.get("version") or ""),
                                    k=int(payload.get("k") or 6),
                                    tenant="memory" if str(payload.get("dst") or "") == "memory" else ""),
                default=str)))
        if action == "heal_replay":
            return envelope.ok(notice=bench.heal_replay(str(payload.get("source") or ""), variant,
                                                        str(payload.get("prompt") or ""),
                                                        int(payload.get("batch") or 0),
                                                        int(payload.get("eyes") or 0),
                                                        int(payload.get("cap") or 0), int(payload.get("body") or 0),
                                                        [str(x) for x in (payload.get("sessions") or [])] or None))
        if action == "filed_build":
            return envelope.ok(notice=bench.filed_build(list(payload.get("sections") or [])))
        if action == "heal_dump":
            return envelope.ok(notice="dump", lines=bench.heal_dump(variant))
        if action == "record_cross":
            return envelope.ok(notice="cross", cross=bench.record_cross(variant, str(payload.get("other") or "")))
        if action == "record_pairs":
            return envelope.ok(notice="pairs", pairs=bench.record_pairs(variant, float(payload.get("near") or 0.20)))
        if action == "decision_dump":
            return envelope.ok(notice="dump", lines=bench.decision_dump(variant))
        return envelope.ok(notice=json.dumps(bench.status(variant), default=str))
    except ValueError as e:
        return envelope.envelope("bad_request", notice=str(e))
    except Exception:
        traceback.print_exc()
        return envelope.server_error(notice=f"bench {action} failed")


@app.post("/v1/hooks/pre-compact")
def pre_compact(payload: dict, tenant: str = Depends(resolve_caller),
         _rl=Depends(rate_gate),
         _=Depends(require_full_access)):
    """/compact = the split line. Two jobs, both founder-specified 2026-07-26:

    RECONCILE — "does it send chat history with it that we can compare we
    didnt lose anything mid way?" The client sends the last turn index it has
    locally; we answer with the highest index the archive holds. A gap means
    the client must flush its spool NOW — the curate job's grace period exists
    precisely so that flush lands before curation reads the window.

    ENQUEUE — curation runs from the queue, not inline. Inference down means
    the job retries with backoff: "in worst case scenario we will just lag a
    bit." The marker keeps even a crashed attempt lossless.
    """
    project = _project(payload)
    session_id = (payload.get("session_id") or "").strip()
    if not session_id:
        return envelope.ok(notice="no session_id — nothing to curate")
    client_last = payload.get("last_index")
    tok = store.bind(tenant)
    try:
        with store.connection() as conn:
            cur = conn.cursor()
            # The founder's intent for this number (2026-07-28): a SAFETY NET
            # — "check do we miss any records from hooks on end of each
            # message; nothing to save if they match." (No negative ordinals
            # since the April–July archive's rewrite, 2026-09-29.)
            cur.execute("SELECT coalesce(max(turn_index), -1), count(*) "
                        "FROM chat WHERE session_id = %s;", [session_id])
            server_last, server_count = cur.fetchone()
            # THE HOLD-BACK's compact point (2026-09-28): what this session
            # wrote after it comes back to the session only after the NEXT one
            injection.mark_compact(conn, cur, session_id, server_last)
            # LEDGER RESET (founder 2026-08-12: "the compact have to clear
            # the ledger... that's treated as new session"): compaction wipes
            # everything ever injected from the model's context, so the
            # cooldown ledger must reset with it — otherwise a record
            # delivered before the compact stays suppressed for a model that
            # no longer remembers it. SessionStart re-fires on compact
            # (matcher "", verified live 2026-08-12) and re-delivers the
            # rules; the ambient path starts fresh. Best-effort per table —
            # legacy ledgers included so old rows can't linger.
            for tbl in ("cooldown_ledger",):
                try:
                    cur.execute(f"DELETE FROM {tbl} WHERE session_id = %s;",
                                [session_id])
                    conn.commit()
                except Exception:
                    conn.rollback()    # table may not exist on this tenant
        # The compact-era curate enqueue is RETIRED (founder 2026-08-06:
        # "the precompact is the leftover of the code which has been
        # retired") — per-turn curation owns every window on every tenant.
        # This endpoint is now purely the archive-parity handshake (plus the
        # ledger reset above).
    except Exception:
        traceback.print_exc()
        return envelope.server_error(notice="pre-compact parity check "
                                            "failed; archive unverified")
    finally:
        store.unbind(tok)
    # A COMPACT FLUSHES THE SESSION'S WINDOW (2026-09-26): whatever pieces
    # the box still holds for this session go up now, however few.
    try:
        from core import hubfeed
        tok = store.bind(tenant)
        try:
            hubfeed.flush_session(project, session_id)
        finally:
            store.unbind(tok)
    except Exception:
        traceback.print_exc()
    # THE COMPACT IS FAST HEAL'S TRIGGER (founder 2026-08-30). The judgement
    # is the hub's (design step 3, 2026-09-10). Fire-and-forget; the queue
    # makes a missed or doubled trigger free.
    try:
        from core import reconcile
        # NOT forced (founder 2026-09-03): the pool is four records, and a
        # compact has nothing to do with it.
        line = reconcile.trigger(tenant, project)
        print(f"[pre-compact] {tenant}/{project}: {line}", flush=True)
    except Exception:
        traceback.print_exc()
    # Two gap signals: the tail (max index behind the client) and HOLES
    # (fewer rows than the contiguous range implies — a mid-stream loss the
    # max alone can never see, e.g. after a backup restore).
    tail_gap = (client_last is not None and int(client_last) > server_last)
    holes = server_count < server_last + 1
    return envelope.ok(live_curate=True, server_last_index=server_last,
                       server_count=server_count,
                       flush_needed=tail_gap or holes,
                       notice=("archive is behind your transcript — "
                               "re-flush needed" if (tail_gap or holes)
                               else ""))


# ── admin: the founder's economics view (2026-07-30) ──────────────────────
# ── self-service export (founder decision 2026-08-09) ─────────────────────
def _export_response(tenant: str, scopes: dict | None = None):
    """RAW CHAT ONLY. Founder: the chat archive is the customer's own data;
    curated records, the graph, briefs AND rules are the engine's work
    product and stay home ("rules I'm treating like a part of our system").
    A customer who wants their rules asks their agent for list_rules.

    SCOPED KEYS (2026-08-09, GLM white-box audit find): a project-scoped key
    exports ONLY the projects it can see — export was the one read door the
    scoped-key completeness pass missed, and in the app-tenant model (a
    project per end-user) an unfiltered export is one user pulling every
    other user's chat. `scopes=None` (full-access key, and the control-plane
    owner path) exports the whole tenant — the boundary exists only where the
    customer drew one.

    One JSONL per project inside a zip, streamed through a server-side
    cursor so a 900MB archive never sits in RAM."""
    import os as _os
    import tempfile
    import zipfile
    from fastapi.responses import FileResponse
    from starlette.background import BackgroundTask

    tok = store.bind(tenant)
    tmp = tempfile.NamedTemporaryFile(prefix="mind-export-", suffix=".zip",
                                      delete=False)
    tmp.close()
    try:
        with store.connection() as conn, \
                zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
            cur = conn.cursor()
            cur.execute("SELECT name FROM project ORDER BY name;")
            projects = [r[0] for r in cur.fetchall()]
            if scopes is not None:
                default = scopes.get("*", "none")
                projects = [p for p in projects
                            if scopes.get(p, default) != "none"]
            zf.writestr("README.txt",
                        "brethof-brain data export\n\n"
                        "One JSONL file per project under chat/ — your full "
                        "raw conversation archive, in order. Fields: "
                        "session_id, turn_index, role, text, ts.\n")
            for i, p in enumerate(projects):
                with zf.open(f"chat/{p}.jsonl", "w") as member:
                    from core import ownership
                    w, a = ownership.chat_where(conn.cursor(), p)
                    with conn.cursor(name=f"export_{i}") as c2:
                        c2.execute(
                            f"SELECT session_id, turn_index, role, text, ts "
                            f"FROM chat WHERE {w} "
                            f"ORDER BY ts, turn_index;", a)
                        for sid, ti, role, text, ts in c2:
                            member.write((json.dumps(
                                {"session_id": sid, "turn_index": ti,
                                 "role": role, "text": text,
                                 "ts": ts.isoformat() if ts else None},
                                ensure_ascii=False) + "\n").encode("utf-8"))
    except Exception:
        _os.remove(tmp.name)
        raise
    finally:
        store.unbind(tok)
    stamp = time.strftime("%Y-%m-%d")
    return FileResponse(
        tmp.name, media_type="application/zip",
        filename=f"brethof-brain-export-{stamp}.zip",
        background=BackgroundTask(_os.remove, tmp.name))


@app.get("/v1/export")
def export_own_data(tenant: str = Depends(resolve_caller),
                    scopes=Depends(resolve_scopes_dep)):
    """Key-authenticated self-service export — a key can only ever export
    its own tenant, and a project-scoped key only the projects it can see.
    The panel gets the whole-tenant zip via the control plane's proxy to
    /v1/internal/export (the account owner, authenticated in the panel)."""
    return _export_response(tenant, scopes)


# ── internal: control plane → data plane (2026-07-28) ─────────────────────
_TENANT_RE = re.compile(r"^[a-z0-9]{4,32}$")


def _norm_tenant(payload: dict) -> str:
    t = str(payload.get("tenant_id") or "").strip().lower()
    t = t[2:] if t.startswith("t_") else t
    if not _TENANT_RE.match(t):
        raise HTTPException(status_code=400, detail="bad tenant_id")
    return t


@app.post("/v1/internal/provision")
def internal_provision(payload: dict, _=Depends(_internal_guard)):
    """Create the tenant's database + schema. Idempotent — provisioning an
    existing tenant is a no-op, which lets the control plane retry freely."""
    tenant = _norm_tenant(payload)
    created = store.provision_tenant(tenant)
    return {"status": "ok", "tenant_id": tenant, "created": created}


@app.post("/v1/internal/deprovision")
def internal_deprovision(payload: dict, _=Depends(_internal_guard)):
    """GDPR account deletion (2026-07-30): DROP the tenant's database.

    The one irreversible endpoint on this API, and deliberately so — the
    customer confirmed in the panel, the control plane authenticated them
    and holds the audit trail; by the time the call reaches here the
    decision is made. Idempotent: deleting a tenant that is already gone
    reports deleted=False, letting the control plane retry freely.
    Residual copies age out of encrypted backups on the borg rotation
    (documented in the privacy policy)."""
    tenant = _norm_tenant(payload)
    deleted = store.deprovision_tenant(tenant)
    return {"status": "ok", "tenant_id": tenant, "deleted": deleted}


@app.post("/v1/internal/projects")
def internal_projects(payload: dict, _=Depends(_internal_guard)):
    """The tenant's project list for the panel's key picker — the customer's
    own project spaces only, never the service's bookkeeping."""
    tenant = _norm_tenant(payload)
    tok = store.bind(tenant)
    try:
        with store.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT name FROM project "
                        "WHERE name NOT IN ('global','rules','state') "
                        "ORDER BY name;")
            return {"projects": [r[0] for r in cur.fetchall()]}
    except Exception:
        traceback.print_exc()
        return {"projects": []}
    finally:
        store.unbind(tok)


@app.post("/v1/internal/redact")
def internal_redact(payload: dict, _=Depends(_internal_guard)):
    """Leaked-text redaction (founder 2026-08-12 design, 2026-08-24 build):
    the panel's session-authenticated HUMAN removes one exact string from
    protected history — replaced with [REDACTED] in chat + curated records,
    at most once per hour. This verb exists on NO agent surface, and the
    needle is never logged; see core/redact.py for the whole doctrine."""
    tenant = _norm_tenant(payload)
    needle = str(payload.get("needle") or "")
    execute = bool(payload.get("execute"))
    tok = store.bind(tenant)
    try:
        return redact.redact(needle, execute=execute)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        store.unbind(tok)


@app.post("/v1/internal/move_session")
def internal_move_session(payload: dict, _=Depends(_internal_guard)):
    """THE PROJECT-SPLIT DOOR on a hosted box (founder 2026-09-09): the
    panel's session-authenticated human moves an archived session, or a
    turn range, to the project it belongs to; the archive is untouched, the
    ownership row and every derived row follow (core/ownership)."""
    from core import ownership
    tenant = _norm_tenant(payload)
    tok = store.bind(tenant)
    try:
        with store.connection() as conn:
            log: list[str] = []
            try:
                r = ownership.move(conn, str(payload.get("session_id") or ""),
                                   str(payload.get("project") or ""),
                                   payload.get("turn_lo"), payload.get("turn_hi"),
                                   who="panel", why=str(payload.get("why") or ""),
                                   log=log)
                conn.commit()
            except ValueError as e:
                conn.rollback()
                raise HTTPException(status_code=400, detail=str(e))
        for line in log:
            print(f"[move] {line}", flush=True)
        return {"status": "ok", **{k: v for k, v in r.items()}}
    finally:
        store.unbind(tok)


@app.post("/v1/internal/usage_snapshot")
def internal_usage_snapshot(payload: dict, _=Depends(_internal_guard)):
    """LIVE usage for the account panel (founder 2026-08-24: statistics must
    be live, not 6-hour-stale). The control plane sends the plan and
    purchased top-ups from the subscription row it owns; we answer with the
    numbers the door actually enforces on — used/caps/percentages for both
    meters, pack arithmetic, and the door state. Read-only: a panel view
    never counts an exchange and never fires the wall push."""
    tenant = _norm_tenant(payload)
    plan = str(payload.get("plan") or "").strip().lower() or None
    pack = payload.get("pack_credits")
    extra = payload.get("extra_storage_mb")
    tok = store.bind(tenant)
    try:
        return quota.snapshot(
            plan=plan,
            pack_credits=int(pack) if pack is not None else None,
            extra_mb=int(extra) if extra is not None else None)
    finally:
        store.unbind(tok)


@app.post("/v1/internal/project_delete")
def internal_project_delete(payload: dict, _=Depends(_internal_guard)):
    """Full project delete — memory, graph AND conversation history.

    Founder ruling 2026-08-12: destructive power belongs on the surface
    where a HUMAN acts. This verb left the agent toolset the same day — an
    agent supplying its own confirm token is a typo guard, not an authority
    check, and this is the one act that reaches the chat archive at any
    age (the store the 90-day cleanup floor exists to protect). The panel
    is where the account owner types the project name; the control plane
    authenticates them and holds the audit trail — the same trust chain as
    /v1/internal/deprovision, one level down. Idempotent: deleting a
    project that is already gone reports deleted=False, letting the
    control plane retry freely. A confirmed delete is final — deleted
    data is not restored for the customer."""
    tenant = _norm_tenant(payload)
    project = _project(payload)
    if not project or project in ("global", "rules", "state"):
        raise HTTPException(status_code=400, detail="bad project")
    from . import customer_tools               # local: mcp_app owns the import
    tok = store.bind(tenant)
    try:
        # (the judge's deletion_blocked flag left with the box-side judge,
        #  2026-09-02; the hub judge brings the fair-use refusal back)
        out = customer_tools.delete_project(project, confirm=project)
    finally:
        store.unbind(tok)
    deleted = out.startswith("Deleted project")
    if not deleted and out != "No such project.":
        # A refusal that is neither success nor absence is a real fault
        # (scope machinery should never trigger here — internal calls are
        # unscoped). Surface it; never let the panel show "deleted" on it.
        raise HTTPException(status_code=500, detail=out[:300])
    return {"status": "ok", "tenant_id": tenant, "project": project,
            "deleted": deleted, "detail": out}


@app.post("/v1/internal/export")
def internal_export(payload: dict, _=Depends(_internal_guard)):
    """Export for the PANEL (control plane proxies; the customer is session-
    authenticated there). Same content as /v1/export below."""
    tenant = _norm_tenant(payload)
    return _export_response(tenant)


@app.post("/v1/internal/revoke")
@app.post("/v1/internal/refresh")
def internal_evict(payload: dict, _=Depends(_internal_guard)):
    """Key revoked or scopes edited — drop the cached resolution so the change
    applies now instead of at the 5-minute TTL."""
    kh = str(payload.get("key_hash") or "").strip()
    if kh:
        auth.evict(kh)
    return {"status": "ok"}


@app.post("/v1/recall")
def recall(payload: dict, tenant: str = Depends(resolve_caller),
         _rl=Depends(rate_gate),
         _=Depends(require_full_access)):
    """Explicit recall. Same fusion as the ambient path, more results."""
    q = (payload.get("query_text") or "").strip()
    if not q:
        return envelope.ok(text="")
    project = _project(payload)
    top_k = max(1, min(int(payload.get("top_k") or 10), 30))
    include_chat = payload.get("include_chat", True)
    tok = store.bind(tenant)
    t0 = time.perf_counter()
    try:
        vec = embed.embed_one(q)
        with store.connection() as conn:
            text = recall_pg.recall_text(conn, vec, q, project=project,
                                         top_k=top_k, include_chat=include_chat)
    except Exception:
        traceback.print_exc()
        return envelope.server_error(notice="recall temporarily unavailable")
    finally:
        store.unbind(tok)
    return envelope.ok(text=text, ms=round((time.perf_counter()-t0)*1000),
                       degraded=vec is None)


# Imported last: these import resolve_caller from this module, so the routers
# can only be attached once that name exists.
from .mcp_app import router as mcp_router          # noqa: E402

app.include_router(mcp_router)

# LAST WORD ON THE ROUTE TABLE. Self-hosted deletes the control-plane routes
# rather than guarding them: on a box we do not operate, /v1/internal is a
# second door onto the customer's own memory. Deleting cannot be bypassed by a
# bad conditional, and 404 is the honest answer to "does this exist here".
mode.announce(app)
