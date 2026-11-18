"""The standing drainer — the missing half of the durable embed queue.

The partial indexes `chat_unembedded` / `document_unembedded` ARE the queue.
Until 2026-07-26 the only thing that ever consumed that queue was the one-off
migration backfill script, so every row archived after migration kept
`embedding IS NULL` forever. The compact rehearsal drill caught it: backlog 12,
four polls, no movement. The queue was durable; the drain did not exist. That
is v1's silent-embedding failure reborn — except the design made it VISIBLE in
one health poll, which is the entire argument for queue-as-partial-index.

Runs as a daemon thread inside the API process (one process, one drainer — a
separate worker container earns its keep only when embedding load does). Every
cycle visits every tenant in the key file and embeds up to BATCH rows per
table. An embedder failure LOGS AND LEAVES THE ROWS QUEUED — the backlog stays
visible on /v1/health and the next cycle retries. Nothing here fails silently.
"""
from __future__ import annotations

import os
import threading
import time
import traceback

from . import embed, quota, ranking, recall_pg, store

# Loop tick for the safety-sweep clock only — an empty tick touches nothing.
INTERVAL_S = 60.0
# 16 (was 64, 2026-08-11): a 64-row gemma batch takes ~10-13 s ALONE on the
# VPS's 3 cores — under concurrent load it blew the 30 s timeout, the rows
# were abandoned server-side but still computed, and the retry amplified the
# pile. 16 rows ≈ 3 s warm: fits the timeout with room even when queries
# interleave.
# 64 again (2026-08-12): the 16-row cut dated from when one batch was one
# all-or-nothing embed call. Groups now bound each call at GROUP_CHAR_BUDGET
# and commit as they land, so a big fetch no longer risks big loss — it just
# keeps the box fed. 64 small rows ≈ 13 s measured; a monster-only fetch
# commits group by group.
BATCH = 32
# COMMIT UNIT. How much we risk on one timeout: rows are embedded and
# committed in groups this size, so a slow group costs only itself and the
# backlog shrinks on every pass. A row larger than this travels alone.
GROUP_CHAR_BUDGET = 6_000
# WALL-CLOCK, NOT COMPUTE. The sidecar no longer serialises on one lock, but
# a group can still share cores with two others, so the budget stays gener.
DOC_EMBED_TIMEOUT = 120.0
STALL_RETRY_S = 60.0

# ── PRIORITY (2026-08-12 rewrite, founder-specified) ───────────────────────
# THREE VARIABLES, and deliberately no fourth:
#     small size  ↑     high plan  ↑     deep personal backlog  ↓
# Size orders rows WITHIN a tenant (shortest-job-first, which minimises
# average wait and keeps live memory current). Plan and backlog choose WHICH
# TENANT is served next, because both are properties of the account.
#
# NO AGE / NO ANTI-STARVATION TERM, on purpose. Shortest-job-first can starve
# big rows only when small-row arrival meets or exceeds throughput — and that
# is exactly the "we need more cores" condition. Under capacity the small
# queue empties between bursts and big rows drain in the gaps. An aging term
# would hide the one signal worth seeing. Founder: "Long wait shouldn't
# happen. If that's happening we need more resources."
#
# BACKLOG IS MEASURED IN THE QUEUE ONLY — rows pending right now, never
# lifetime volume. A busy customer must not be punished today for last
# month's traffic; a customer dumping 50k rows right now goes last. Founder:
# "the things waiting in queue thats what we measure only."
# The weights and softener live in core/ranking.py since 2026-08-24 — the
# turn and gate lanes run the same ordering, and one system means one
# number to tune. score = plan / (1 + pending/SOFTENER): 500 means a
# tenant is barely affected until a few hundred rows deep, then falls away
# steadily — a dump loses the race without ever being frozen.
PLAN_WEIGHT = ranking.PLAN_WEIGHT
DEFAULT_PLAN_WEIGHT = ranking.DEFAULT_PLAN_WEIGHT
BACKLOG_SOFTENER = ranking.BACKLOG_SOFTENER
# Background workers. Small by physics: on 3 cores more concurrency buys
# context switching, not throughput. The embedder reserves a slot for
# interactive work on top of these.
BACKGROUND_SLOTS = int(os.environ.get("BRAIN_DRAIN_SLOTS", "2"))
# The safety pin runs daily. It exists to ASSERT the queue is empty, not to
# find work — every real path pokes, and a poked tenant drains until the
# database says empty. Frequency barely matters for something that should
# always find nothing; if it ever does find work, that is a bug report.
SWEEP_S = float(os.environ.get("BRAIN_DRAIN_SWEEP_S", str(24 * 3600)))

# What gets embedded — must match what the QUERY side embeds against, which is
# `title [type] content` for documents (tools._embed_or_queue) and raw text
# for chat (the migration backfill used the same pair).
_TABLES = (
    ("chat", "text"),
    ("document", "coalesce(title,'') || ' [' || coalesce(type,'note') || '] ' "
                 "|| coalesce(content,'')"),
)

# Windowed-embedding side tables for LIVE tenants (new tenants get them from
# schema.sql). ensure_ddl memoizes per process — this is not hot-path DDL.
_CHUNK_DDL = """
CREATE TABLE IF NOT EXISTS chat_chunk (
    chat_id     bigint NOT NULL REFERENCES chat(id) ON DELETE CASCADE,
    chunk_idx   int NOT NULL,
    embedding   halfvec(768) NOT NULL,
    PRIMARY KEY (chat_id, chunk_idx)
);
CREATE INDEX IF NOT EXISTS chat_chunk_hnsw_idx ON chat_chunk
    USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE TABLE IF NOT EXISTS document_chunk (
    document_id bigint NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    chunk_idx   int NOT NULL,
    embedding   halfvec(768) NOT NULL,
    PRIMARY KEY (document_id, chunk_idx)
);
CREATE INDEX IF NOT EXISTS document_chunk_hnsw_idx ON document_chunk
    USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64);
"""


def drain_tenant(tenant: str) -> tuple[int, str]:
    """One pass for one tenant. Returns (rows embedded, followup) where
    followup is '' (queue drained), 'more' (a full batch — likely more rows
    behind it), or 'stalled' (embedder down — rows left queued)."""
    done = 0
    followup = ""
    tok = store.bind(tenant)
    try:
        with store.connection() as conn:
            cur = conn.cursor()
            for table, expr in _TABLES:
                # FAIR SHARE ACROSS PROJECTS (2026-08-12). This was
                # `ORDER BY id LIMIT BATCH` — strict FIFO over the whole
                # tenant — and it STARVES a new project completely. Measured:
                # nv_fiction's rows are ids 331,010-331,868, the newest in the
                # tenant, queued behind ~1,890 older unembedded rows (ms from
                # 209,257, mind from 241,187, bv from 252,315). With the
                # embedder at its 3-core cap and live sessions still adding
                # rows, that tail never empties, so nv_fiction sat at 0 of 218
                # embedded — not slow, never ATTEMPTED. Its recall was
                # keyword-only the whole time and nothing said so.
                # Round-robin instead: every project contributes its oldest
                # rows to each pass, so one project's backlog can never make
                # another project's memory unsearchable. Oldest-first still
                # holds WITHIN a project.
                # expr is an EXPRESSION, not a column — alias it inside the
                # subquery and select the alias, or the outer list refers to
                # columns the subquery never exposes.
                # SMALL ROWS FIRST (2026-08-12, founder: 'impossible that it
                # will take so long'). The queue is dominated by the long-tail
                # re-embed backfill — measured 5.4K chars/row average, single
                # rows up to 22K — which is HOURS of honest compute at this
                # box's ~300 tokens/s. Interleaving cheap rows with monsters
                # made every pass slow, so a 28-char live-chat row waited on
                # a 22K historical row's compute. Cheap rows now drain first
                # (live recall current within minutes); the backfill gets the
                # full box whenever nothing cheap is waiting. Within each
                # class, oldest-first still holds.
                # embed_skip: rows kept in the archive but excluded from the
                # embedding queue and from recall (pre-2026-07-11 tool-result
                # leakage). ensure_ddl memoizes per process — live tenants
                # self-heal on first pass, new ones get it from schema.sql.
                store.ensure_ddl(cur, f"embed_skip_{table}",
                                 f"ALTER TABLE {table} ADD COLUMN IF NOT "
                                 f"EXISTS embed_skip boolean NOT NULL "
                                 f"DEFAULT false;")
                # SHORTEST JOB FIRST — the whole within-tenant policy, in one
                # ORDER BY. It replaces the project partitioning, the
                # per-project cap and the small/big class split that accreted
                # across five fixes on 2026-08-12 and fought each other: the
                # cap throttled whichever project held the cheap rows, and the
                # window function's interaction with the batch limit is how a
                # 28-char row became unreachable behind 13-22K rows.
                # Small rows drain first so live conversation is current in
                # seconds; big backfill rows take the gaps. Oldest-first
                # breaks ties, so nothing inside a size class is passed over
                # twice. Fairness BETWEEN accounts is the dispatcher's job
                # (plan + backlog), not this query's.
                cur.execute(
                    f"SELECT id, {expr} FROM {table} "
                    f"WHERE embedding IS NULL AND NOT embed_skip "
                    f"ORDER BY length({expr}) ASC, id ASC LIMIT %s;",
                    [BATCH])
                fetched = cur.fetchall()
                if not fetched:
                    continue
                # Greedy char-budget fill: take rows until the budget is
                # spent (always at least one). Leftovers stay queued and the
                # 'more' followup brings the loop straight back for them.
                # A long row costs its FULL length — it becomes multiple
                # model windows below.
                # NO FETCH-LEVEL TRIM (2026-08-12). This used to greedily fill
                # to BATCH_CHAR_BUDGET and `break` at the first row too big to
                # fit — which threw away every row BEHIND it. That one word
                # was the real reason nv_fiction sat at 0 of 218: the queue
                # head held long rows (measured 13,202 / 4,299 / 22,600
                # chars), the budget blew on row 3, every pass embedded 2
                # rows, and a 28-char row at position 11 was unreachable.
                # Fair-share ordering could not save it, because the
                # truncation happened after the fetch.
                # Changing it to `continue` only moved the starvation onto the
                # big rows (a 22,600-char row behind 17,501 never fits, so it
                # would be skipped forever). The trim is now REDUNDANT: the
                # group loop below already bounds every embed call by
                # GROUP_CHAR_BUDGET and commits per group, and a row larger
                # than the group budget travels alone. So take the whole
                # fetch — LIMIT bounds the pass, groups bound the risk.
                rows = fetched
                # WINDOWED EMBEDDING (founder 2026-08-12: "the length
                # shouldn't be limited at all — split it and do it
                # separately"): each row becomes 1..N model windows. Window
                # 1 is the row's own embedding — exactly what always
                # existed; windows 2..N land in <table>_chunk and the recall
                # vector streams credit the parent with its best window.
                # embed_batched sub-calls by size, so even a monster row
                # can never outgrow one request's timeout.
                store.ensure_ddl(cur, "embed_chunks", _CHUNK_DDL)
                # FORWARD PROGRESS (2026-08-12). This used to be ONE
                # all-or-nothing embed_batched over every fetched row: a
                # single timed-out sub-call threw away every vector already
                # computed and requeued the lot, so the embedder burned CPU
                # completing work the API had already abandoned (the
                # BrokenPipeError flood in the sidecar log) and the same rows
                # came back forever. It starved by ROW LENGTH: nv_fiction
                # (avg 1,967 chars/row vs ~400 fleet-wide) straddled the
                # ceiling every pass and sat at 0/218 embedded while
                # short-row projects drained normally.
                # Two changes, both about never discarding finished work:
                #   1. embed + COMMIT in small row-groups, so a failure can
                #      only cost its own group and the backlog always shrinks;
                #   2. the timeout budgets WALL-CLOCK, not compute — one
                #      global _infer_lock means a group waits behind the whole
                #      fleet's drain, which is what 30 s never accounted for.
                # A group that fails is left queued and the pass CONTINUES.
                stalled = 0
                groups, cur_g, cur_c = [], [], 0
                for r in rows:
                    size = len(r[1] or "") or 1
                    if cur_g and cur_c + size > GROUP_CHAR_BUDGET:
                        groups.append(cur_g)
                        cur_g, cur_c = [], 0
                    cur_g.append(r)
                    cur_c += size
                if cur_g:
                    groups.append(cur_g)
                for group in groups:
                    wins = [embed.windows(r[1] or "") for r in group]
                    vecs = embed.embed_batched(
                        [w for ws in wins for w in ws],
                        timeout=DOC_EMBED_TIMEOUT, kind="document")
                    if vecs is None:
                        # This group only. Its rows stay queued and visible;
                        # every other group in this pass still lands.
                        stalled += len(group)
                        print(f"[drainer] embed failed for {len(group)} "
                              f"{table} rows (tenant {tenant}) — left "
                              f"queued, continuing", flush=True)
                        continue
                    pos = 0
                    for (rid, _), ws in zip(group, wins):
                        vs = vecs[pos:pos + len(ws)]
                        pos += len(ws)
                        cur.execute(
                            f"UPDATE {table} SET embedding = %s::halfvec "
                            f"WHERE id = %s;", [recall_pg._vec_lit(vs[0]), rid])
                        # Always clear old windows (a shrunk update must not
                        # keep stale tail vectors), then write the extras.
                        cur.execute(f"DELETE FROM {table}_chunk "
                                    f"WHERE {table}_id = %s;", [rid])
                        for i, v in enumerate(vs[1:], 1):
                            cur.execute(
                                f"INSERT INTO {table}_chunk "
                                f"({table}_id, chunk_idx, embedding) "
                                f"VALUES (%s, %s, %s::halfvec);",
                                [rid, i, recall_pg._vec_lit(v)])
                    # COMMIT PER GROUP: finished vectors are durable before
                    # the next group risks a timeout.
                    conn.commit()
                    done += len(group)
                if stalled and stalled == len(rows):
                    # Nothing at all landed — the embedder is down or every
                    # group is too heavy. Report it rather than spinning.
                    return done, "stalled"
                # ASK THE QUEUE, NOT THE FETCH SIZE (2026-08-12, founder spotted
                # it from the Hostinger CPU graph: 58% while draining, then 7%
                # for three hours with ~700 rows still queued).
                # Inferring "more work" from `len(fetched) == BATCH` is WRONG
                # once the fetch is fair-shared: PER_PROJECT_SHARE caps each
                # project per pass, so a queue of hundreds spread over a few
                # projects legitimately returns FEWER than BATCH rows. The
                # drainer then reported "" (drained), went back to sleep, and
                # only woke on the next poke — one pass per arriving message,
                # ~1 every 10-18 min instead of continuous. The queue was not
                # stuck and nothing errored; the loop had simply been told it
                # was finished. Silent, and invisible in every log line.
                # EXISTS on the partial index is cheap and authoritative.
                cur.execute(f"SELECT EXISTS(SELECT 1 FROM {table} "
                            f"WHERE embedding IS NULL AND NOT embed_skip);")
                if cur.fetchone()[0] or stalled:
                    followup = "more"
    finally:
        store.unbind(tok)
    return done, followup


# ── DISPATCHER + WORKERS (2026-08-12 rewrite, founder-specified) ───────────
# The old loop had every worker decide for itself whether work existed and
# whether to sleep. Five independent guesses, and every failure on
# 2026-08-12 was one of them guessing wrong — the worst simply reported
# "drained" and slept for three hours on 700 rows.
# Now ONE dispatcher owns the queue view and workers are dumb executors.
# The dispatcher has no judgement to get wrong: while a slot is free and a
# tenant has work, assign. It idles only when every slot is idle AND the
# database says empty. "Empty" is never inferred from a batch size.
_pending: dict[str, int] = {}          # tenant -> rows known pending
_pending_lock = threading.Lock()
_idle = threading.Condition(threading.Lock())
_busy: set[str] = set()                # tenants currently held by a worker


def _tenant_score(tenant: str) -> float:
    """plan ↑ / backlog ↓ — which ACCOUNT is served next.

    Size orders rows inside a tenant; these two order the tenants. Backlog
    counts rows pending RIGHT NOW, never lifetime volume, so a dump is
    demoted while it lasts and a customer's history never counts against
    them. This is what makes flooding pointless: the flooder's own queue
    depth is the thing that pushes them back."""
    tok = store.bind(tenant)
    try:
        plan = quota.effective_plan()
    except Exception:                                     # noqa: BLE001
        plan = None
    finally:
        store.unbind(tok)
    weight = PLAN_WEIGHT.get(plan or "", DEFAULT_PLAN_WEIGHT)
    with _pending_lock:
        backlog = _pending.get(tenant, 0)
    return weight / (1.0 + backlog / BACKLOG_SOFTENER)


def _worker(name: str) -> None:
    while True:
        tenant = None
        with _idle:
            while True:
                with _pending_lock:
                    ready = [t for t, n in _pending.items()
                             if n > 0 and t not in _busy]
                if ready:
                    tenant = max(ready, key=_tenant_score)
                    _busy.add(tenant)
                    break
                _idle.wait(timeout=INTERVAL_S)
        try:
            done, followup = drain_tenant(tenant)
            with _pending_lock:
                # followup is authoritative: it came from EXISTS on the queue,
                # not from how many rows this pass happened to fetch.
                _pending[tenant] = 1 if followup == "more" else 0
            if done:
                print(f"[drainer:{name}] {tenant} +{done}", flush=True)
            if followup == "stalled":
                with _pending_lock:
                    _pending[tenant] = 1
                time.sleep(STALL_RETRY_S)
        except store.TenantGone:
            # Deleted while we held it. Drop it for good — leaving it in
            # _pending would re-elect it every pass forever.
            with _pending_lock:
                _pending.pop(tenant, None)
            print(f"[drainer:{name}] tenant {tenant} is gone — dropped",
                  flush=True)
        except Exception:                                 # noqa: BLE001
            print(f"[drainer:{name}] tenant {tenant}:", flush=True)
            traceback.print_exc()
            with _pending_lock:
                _pending[tenant] = 0        # a poke or the sweep re-arms it
        finally:
            with _idle:
                _busy.discard(tenant)
                _idle.notify_all()


def note_work(tenant: str) -> None:
    """A poke: 'this tenant may have work'. A hint may only ever ADD work —
    nothing but the database may conclude there is none."""
    with _pending_lock:
        _pending[tenant] = _pending.get(tenant, 0) + 1
    with _idle:
        _idle.notify_all()


def note_work_later(tenant: str, delay_s: float = 5.0) -> None:
    """Poke once the caller's NULL row is actually committed.

    Callers that have not committed yet MUST use this: an immediate poke can
    win the race, find nothing, and mark the tenant clean — after which the
    row is invisible until the daily sweep. The delay is the whole point,
    not a politeness."""
    threading.Timer(delay_s, note_work, args=(tenant,)).start()


def _sweeper() -> None:
    """The safety pin. It should ALWAYS find nothing: every real path pokes,
    and a poked tenant drains until the database says empty. If this ever
    finds work, the queue failed somewhere and that is a defect to fix, not
    a routine catch — so it says so loudly rather than quietly absorbing it.
    Founder: 'the check is just safety pin and it always should find
    nothing.'"""
    while True:
        time.sleep(SWEEP_S)
        try:
            found = []
            for t in store.provisioned_tenants():
                tok = store.bind(t)
                try:
                    with store.connection() as conn:
                        cur = conn.cursor()
                        for table, _ in _TABLES:
                            cur.execute(
                                f"SELECT EXISTS(SELECT 1 FROM {table} WHERE "
                                f"embedding IS NULL AND NOT embed_skip);")
                            if cur.fetchone()[0]:
                                found.append(f"{t}.{table}")
                                break
                except Exception:                         # noqa: BLE001
                    pass
                finally:
                    store.unbind(tok)
            for t in {f.split(".")[0] for f in found}:
                note_work(t)
            if found:
                print(f"[drainer] SWEEP FOUND WORK — the queue missed these, "
                      f"which is a BUG not a catch: {found}", flush=True)
        except Exception:                                 # noqa: BLE001
            traceback.print_exc()


def start() -> None:
    """Idempotent; called from the API's startup hook."""
    try:
        for t in store.provisioned_tenants():
            _pending[t] = 1            # boot: assume work, the DB will decide
    except Exception:                                     # noqa: BLE001
        traceback.print_exc()
    for i in range(BACKGROUND_SLOTS):
        threading.Thread(target=_worker, args=(str(i + 1),),
                         name=f"embed-drainer-{i + 1}", daemon=True).start()
    threading.Thread(target=_sweeper, name="embed-sweeper",
                     daemon=True).start()
    print(f"[drainer] started — {BACKGROUND_SLOTS} slots", flush=True)
