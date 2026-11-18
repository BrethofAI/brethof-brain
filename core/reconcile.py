"""RECONCILE ONE RECORD — the stage that replaces the batch sweep.

Founder 2026-09-01: "The curated record we just sent is the newest one, and
deal with it... It should show it the dupes, it should show it some recall
things and the current state. And he have to change other ones, delete other
ones, and fix everything at once."

So: one record in hand, the records it might collide with in front of it, and
heal fixes all of them at once. No watermark, no clustering, no batches.

WHAT GOES IN FRONT OF IT — measured 2026-09-01 on 257 records of real memory
(brain, web, aurora), ground truth being which records the hand pass decided
are about the same specific thing:

    candidates   vector   keywords   fused
    k=20          66.4%      54.9%   67.3%
    k=40          79.7%      70.2%   81.8%
    k=80          90.9%      78.9%   92.7%

Two things follow, and they decide this module's shape. First, similarity ALONE
misses a third of the truth at k=20 — and fusing keywords into vectors adds
almost nothing (66.4 -> 67.3), because both channels find the same records.
Stacking more similarity does not work; only a structurally different channel
does. Second, similarity does get there at k=80 — but that is eighty full
records in the prompt, where the ENTITY channel reaches the same ground at a
mean of 5.6. Same job, ~13x smaller payload.

Hence: THE ENTITY CHANNEL IS THE PRECISE CORE, a similarity tail is the SAFETY
NET. Not entity-only — the entity channel is only as good as the entity
extraction on the INCOMING record, and the hand pass made the stored corpus
perfect while a new record depends on the curator and registrar. The tail is
what catches the record whose entities came out wrong.

The id-stem channel is here and is not calibrated: the corpus this was measured
on has zero date-suffix forks left, because the hand pass deleted them all. It
costs one LIKE and it catches a class similarity demonstrably misses, so it
stays — but nobody should quote a number for it.
"""
from __future__ import annotations

import os

import collections

from . import heal, injection, judge, store
from .recall_pg import fts_keywords







K_SIM = 20              # similarity tail, per channel
CAP = 24                # candidates handed to the model
# THE PASS WIDTHS (2026-09-17). For the arrival heal the predecessor slot
# hands the judge a few old neighbours and a few bodies; for the ONE-TIME
# PASS over the curated records (founder: "fix these records once and for
# all") each record must see its WHOLE neighbourhood of older records with
# their bodies, or the pass peels one layer per round (measured: the same
# twenty records produced 24 ops, then 9 more on a second run). The three
# widths are settable from the environment for the pass and default to the
# arrival heal's values.
ELDER_N = int(os.environ.get("BRAIN_HEAL_ELDER_N", "6") or 6)         # oldest records within reach of the arrival
ELDER_MAX_DIST = 0.50    # an old record further than this from the arrival is another subject
BODY_N = 12             # of those, how many arrive with a body
RRF_K = 60
ENTITY_WEIGHT = 2.0     # the elder and stem channels outrank the similarity tail
SIM_WEIGHT = 1.0


# ── the channels ────────────────────────────────────────────────────────────
def _elder_hits(cur, project: str, rec: dict, exclude: str, limit: int):
    """THE PREDECESSOR SLOT, records only (founder 2026-09-17: the records
    and the graph are two systems; neither depends on the other). The
    similarity channel lists the NEAREST records; the record an arrival
    should retire is the OLDEST standing statement of its subject, and it
    can sit past the nearest twenty on a busy subject. So: the oldest
    records within ELDER_MAX_DIST of the arrival's own vector, oldest first,
    few. Measured 2026-09-17: the true predecessors sit at 0.33–0.45 (the
    DeepSeek/GLM pair at 0.33), unrelated old records at 0.60–0.86 — the
    distance alone separates them; the graph's names added nothing the
    distance did not. No model call."""
    qv = _arrival_vec(cur, project, rec, exclude)
    if qv is None:
        return []
    cur.execute(
        "SELECT record_id FROM document WHERE project = %s "
        "  AND record_id NOT IN (%s, 'memory_index') AND embedding IS NOT NULL "
        "  AND embedding <=> %s::halfvec <= %s "
        "ORDER BY updated_at ASC LIMIT %s;",
        [project, exclude, qv, ELDER_MAX_DIST, limit])
    return [r[0] for r in cur.fetchall()]


def _arrival_vec(cur, project: str, rec: dict, exclude: str):
    """The arrival's vector as a halfvec literal: its stored one, else a
    fresh embedding of its text (the drainer may be behind); None when
    neither is possible."""
    cur.execute("SELECT embedding FROM document WHERE project = %s AND record_id = %s "
                "AND embedding IS NOT NULL;", [project, exclude])
    row = cur.fetchone()
    if row and row[0] is not None:
        return row[0]
    from . import embed
    from .recall_pg import _vec_lit
    vec = embed.embed_one(f"{rec.get('title') or ''}\n{rec.get('content') or ''}")
    return _vec_lit(vec) if vec else None


def _vector_hits(cur, project: str, rec: dict, exclude: str, k: int):
    cur.execute("SELECT embedding IS NOT NULL FROM document "
                "WHERE project = %s AND record_id = %s;", [project, exclude])
    row = cur.fetchone()
    if row and row[0]:
        cur.execute(
            "SELECT record_id FROM document WHERE project = %s "
            "  AND record_id NOT IN (%s, 'memory_index') "
            "  AND embedding IS NOT NULL "
            "ORDER BY embedding <=> (SELECT embedding FROM document "
            "   WHERE project = %s AND record_id = %s) LIMIT %s;",
            [project, exclude, project, exclude, k])
        return [r[0] for r in cur.fetchall()]
    # Not embedded yet — the drainer is behind, which must not blind heal.
    from . import embed
    from .recall_pg import _vec_lit
    vec = embed.embed_one(f"{rec.get('title') or ''}\n{rec.get('content') or ''}")
    if not vec:
        return []
    cur.execute(
        "SELECT record_id FROM document WHERE project = %s "
        "  AND record_id NOT IN (%s, 'memory_index') AND embedding IS NOT NULL "
        "ORDER BY embedding <=> %s::halfvec LIMIT %s;",
        [project, exclude, _vec_lit(vec), k])
    return [r[0] for r in cur.fetchall()]


def _keyword_hits(cur, project: str, title: str, exclude: str, k: int):
    """Production tokenisation, OR-joined. plainto_tsquery ANDs every word in
    the title and measured 1.1% recall — an instrument reading, not a channel
    reading. The tsv column is built with the `simple` config; querying it with
    `english` matches nothing at all (measured 0.0%)."""
    kws = fts_keywords(title or "", cap=6)
    if not kws:
        return []
    q = " | ".join(kws)
    cur.execute(
        "SELECT record_id FROM document WHERE project = %s "
        "  AND record_id NOT IN (%s, 'memory_index') "
        "  AND tsv @@ to_tsquery('simple', %s) "
        "ORDER BY ts_rank_cd(tsv, to_tsquery('simple', %s)) DESC LIMIT %s;",
        [project, exclude, q, q, k])
    return [r[0] for r in cur.fetchall()]


def _stem_hits(cur, project: str, exclude: str, k: int):
    """The date-fork class: `x_2026_08_11` beside `x_2026_09_01`. Similarity
    often misses these precisely because the two versions say different
    things."""
    import re
    stem = re.sub(r"_\d{4}_\d{2}_\d{2}$", "", exclude)
    if len(stem) < 12 or stem == exclude:
        return []
    cur.execute("SELECT record_id FROM document WHERE project = %s "
                "  AND record_id <> %s AND record_id LIKE %s LIMIT %s;",
                [project, exclude, stem + "%", k])
    return [r[0] for r in cur.fetchall()]


def _exists(project: str):
    """A record-exists check the applier uses to validate a delete's `into`
    when it names a record outside the batch. A short connection per check —
    the applier runs while no other connection is held."""
    def check(rid: str) -> bool:
        with store.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM document WHERE project = %s AND record_id = %s;", [project, rid])
            return cur.fetchone() is not None
    return check


def candidates(conn, project: str, rec: dict,
               k_sim: int = K_SIM, cap: int = CAP) -> list[dict]:
    """The entity core plus a similarity tail, fused, with provenance kept.

    `why` travels with each candidate because a candidate the entity channel
    found and one the vector channel guessed are not the same kind of evidence,
    and the model should be told which is which.
    """
    cur = conn.cursor()
    rid = rec.get("record_id") or ""
    # RECORDS ONLY (founder 2026-09-17): the graph's names no longer choose
    # what the judge reads. Four channels over the records themselves.
    chans = {
        "elder": _elder_hits(cur, project, rec, rid, ELDER_N),
        "vector": _vector_hits(cur, project, rec, rid, k_sim),
        "keyword": _keyword_hits(cur, project, rec.get("title") or "", rid, k_sim),
        "stem": _stem_hits(cur, project, rid, k_sim),
    }
    score: dict[str, float] = collections.defaultdict(float)
    why: dict[str, list[str]] = collections.defaultdict(list)
    for chan, hits in chans.items():
        w = ENTITY_WEIGHT if chan in ("elder", "stem") else SIM_WEIGHT
        for i, r in enumerate(hits):
            score[r] += w / (RRF_K + i + 1)
            why[r].append(chan)
    top = [r for r, _ in sorted(score.items(), key=lambda kv: -kv[1])][:cap]
    if not top:
        return []
    cur.execute(
        "SELECT record_id, type, title, content, "
        "       to_char(updated_at, 'YYYY-MM-DD') FROM document "
        "WHERE project = %s AND record_id = ANY(%s);", [project, top])
    rows = {r[0]: r for r in cur.fetchall()}
    out = []
    for r in top:
        if r not in rows:
            continue
        _, typ, title, content, upd = rows[r]
        out.append({"record_id": r, "type": typ, "title": title,
                    "content": content, "upd": upd,
                    "why": ",".join(why[r])})
    return out




def reconcile(project: str, record_id: str, dry: bool = False) -> dict:
    """Reconcile ONE record against what is already stored: the arrival and
    its candidates go up, the hub judges under its prompt, and heal._apply
    applies the ops here — the guards are paid for: ids validated against
    records actually shown, a no-op update refused, REFUSED / NO-OP /
    applied told apart.

    NO CONNECTION IS HELD ACROSS THE ASK (2026-09-17). The reads take a
    pool connection and give it back before the hub is asked; the applier's
    tools open their own, short ones. Measured that night: a batch held its
    connection through a 75-second hub call while the applier opened a
    second one inside it, and with the pool at four the pipe's own saves
    timed out — four curated records the box never wrote."""
    with store.connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT record_id, type, title, content FROM document "
                    "WHERE project = %s AND record_id = %s;", [project, record_id])
        row = cur.fetchone()
        if not row:
            return {"ok": False, "note": "record is gone", "candidates": 0}
        rec = {"record_id": row[0], "type": row[1], "title": row[2], "content": row[3]}
        cands = candidates(conn, project, rec)
        conn.commit()
    if not cands:
        return {"ok": True, "candidates": 0, "applied": 0, "dropped": 0,
                "links": 0, "note": "nothing near it", "log": []}
    try:
        a = judge.ask("heal_judge", {"project": project, "records": [rec], "candidates": cands,
                                     "body_ids": [c["record_id"] for c in cands[:BODY_N]]})
    except judge.NoAnswer as e:
        return {"ok": False, "candidates": len(cands), "note": f"no verdict ({e.why})"}
    obj = a["answer"]
    if obj is None:
        return {"ok": False, "candidates": len(cands), "note": f"no verdict (parse: {a['fault']})"}
    from . import metering
    metering.record("heal", a["input_tokens"], a["output_tokens"])
    log: list[str] = []
    shown = {rec["record_id"]} | {c["record_id"] for c in cands}
    bodies = {c["record_id"]: c["content"] or "" for c in cands}
    bodies[rec["record_id"]] = rec["content"] or ""
    applied, dropped = heal._apply(plain_ops(obj.get("ops") or []), project, shown,
                                   dry, log, current=bodies, gone=set(),
                                   readable={rec["record_id"]} | {c["record_id"] for c in cands[:BODY_N]}, exists=_exists(project))
    for line in log:
        print(f"[reconcile] {project}: {line}", flush=True)
    return {"ok": True, "candidates": len(cands), "applied": applied,
            "dropped": dropped, "links": 0, "log": log}


BATCH_MAX = 4           # records of one project per model call


def drain(tenant: str, project: str, dry: bool = False,
          max_items: int = 50, force: bool = False) -> dict:
    """Work this project's heal queue, oldest first, one SAVE at a time: the
    claimed row plus up to BATCH_MAX-1 pending rows behind it, in one model
    call (reconcile_batch).

    There is no watermark: the queue holds exactly the records that landed
    and nothing else, so a run that dies mid-way loses nothing and a run that
    finds nothing costs nothing.

    ONE CONSUMER PER PROJECT is the queue's own guarantee (a unique partial
    index), not something this loop arranges — two of these racing is safe, the
    second simply claims nothing. `max_items` is a courtesy bound so one call
    cannot run forever on a huge backlog; the next trigger takes the rest.
    """
    from . import healqueue
    tok = store.bind(tenant)
    done = applied = dropped = links = failed = lost = 0
    try:
        with store.connection() as conn:
            # A consumer that died holding a claim would block this project
            # for good — the exclusion has to be releasable.
            healqueue.reclaim(conn, project)
            # HEAL BY FOUR: unless forced (a compact), wait for a pool.
            if not force:
                ok, n = healqueue.ready(conn, project)
                if not ok:
                    return {"project": project, "reconciled": 0, "ops_applied": 0,
                            "ops_dropped": 0, "links": 0, "failed": 0,
                            "dropped_forever": 0, "queue_depth": n,
                            "pooling": n}
        for _ in range(max_items):
            with store.connection() as conn:
                claimed = healqueue.claim(conn, project)
            if not claimed:
                break
            qid, rid, _attempts = claimed
            # THE SAVE'S OTHER RECORDS ride in the same call (founder
            # 2026-09-03). The claimed row is the project's lock, so the
            # pending rows behind it cannot be taken by anyone else; if this
            # run dies they are still pending for the next leader.
            with store.connection() as conn:
                extra = healqueue.peek_more(conn, project, qid, BATCH_MAX - 1)
            ids = [rid] + [r for _, r in extra]
            try:
                res = reconcile_batch(project, ids, dry=dry)
                if res.get("note") == "records are gone":
                    # NOT a failure — the ordinary end of merged records: an
                    # earlier op in this run folded them into a survivor and
                    # deleted them; their queue rows then came up and found
                    # nothing. (Reported as data loss on the first day live,
                    # 2026-09-01, when it was retried three times.)
                    with store.connection() as conn:
                        for eid in [qid] + [e for e, _ in extra]:
                            healqueue.finish(conn, eid)
                    continue
                if not res.get("ok"):
                    raise RuntimeError(res.get("note") or "no verdict")
                with store.connection() as conn:
                    for eid in [qid] + [e for e, _ in extra]:
                        healqueue.finish(conn, eid)
                done += int(res.get("records") or len(ids))
                applied += int(res.get("applied") or 0)
                dropped += int(res.get("dropped") or 0)
                links += int(res.get("links") or 0)
            except Exception as e:                             # noqa: BLE001
                failed += 1
                with store.connection() as conn:
                    gone = healqueue.fail(conn, qid, f"{type(e).__name__}: {e}")
                if gone:
                    lost += 1
                    # LOUD: this record is never reconciled. Dropping it is the
                    # decision (a dead-letter pile nobody walks is worse), but
                    # a silent drop would be the failure this system forbids.
                    print(f"[reconcile] {project}:{rid} DROPPED after "
                          f"{healqueue.MAX_ATTEMPTS} attempts — it will never "
                          f"be reconciled: {type(e).__name__}: {e}", flush=True)
        with store.connection() as conn:
            depth = healqueue.depth(conn, project)
    finally:
        store.unbind(tok)
    return {"project": project, "reconciled": done, "ops_applied": applied,
            "ops_dropped": dropped, "links": links, "failed": failed,
            "dropped_forever": lost, "queue_depth": depth}


# ── ONE CALL PER SAVE ──────────────────────────────────────────────────────
# Founder 2026-09-03, on the heal bill being 4-5K tokens a call and ~2 calls a
# message: "if we send them all at once, probably it will do the same job and
# will cost us one third. That's the thing which we should test." A save lands
# ~2 records whose neighbourhoods overlap; reconciling them one by one reads
# the same candidates twice and pays the instruction twice.
CAP_BATCH = int(os.environ.get("BRAIN_HEAL_CAP_EACH", "10") or 10)   # candidates fetched PER arrival (fused rank)
BODY_BATCH = int(os.environ.get("BRAIN_HEAL_BODY_EACH", "6") or 6)   # of each arrival's candidates, how many get a body




def plain_ops(ops: list) -> list[dict]:
    """The judge's ops in the two shapes heal._apply knows (2026-09-29, the
    heal round): "keep" is a verdict that changes nothing and leaves; a
    "merge" names the records about one thing and the one that stays — it
    becomes an update of the survivor (when it carries text) and a delete of
    each other one INTO it, so a merge can never mint a twin."""
    out: list[dict] = []
    for o in ops or []:
        if not isinstance(o, dict):
            continue
        op = str(o.get("op") or "")
        if op == "keep":
            continue
        if op != "merge":
            out.append(o)
            continue
        keep = str(o.get("keep") or o.get("id") or "").strip().split(":")[-1]
        ids = [str(x).strip().split(":")[-1] for x in (o.get("ids") or []) if str(x).strip()]
        if not keep:
            out.append(o)                  # malformed: the applier drops and logs it
            continue
        reason = str(o.get("reason") or "")
        if str(o.get("title") or "").strip() and str(o.get("content") or "").strip():
            out.append({"id": keep, "op": "update", "reason": reason, "type": o.get("type"),
                        "title": o.get("title"), "content": o.get("content")})
        out += [{"id": i, "op": "delete", "into": keep, "reason": f"merged into {keep}: {reason}"}
                for i in dict.fromkeys(ids) if i != keep]
    return out


def reconcile_batch(project: str, record_ids: list[str],
                    dry: bool = False,
                    cap_each: int = CAP_BATCH, body_each: int = BODY_BATCH,
                    prompt: str = "", filed: list[dict] | None = None) -> dict:
    """Reconcile the records ONE SAVE landed, in one model call.

    Each arrival fetches its own fused candidates (cap_each); the union is
    shown once, a body for any candidate that sat inside some arrival's top
    body_each, a title for the rest. Arrivals are shown in full and are in
    scope for every op, exactly as in reconcile(). No connection is held
    across the ask (see reconcile)."""
    recs: list[dict] = []
    best: dict[str, tuple[int, dict]] = {}
    with store.connection() as conn:
        cur = conn.cursor()
        for rid in record_ids:
            cur.execute("SELECT record_id, type, title, content FROM document "
                        "WHERE project = %s AND record_id = %s;", [project, rid])
            row = cur.fetchone()
            if row:
                recs.append({"record_id": row[0], "type": row[1],
                             "title": row[2], "content": row[3]})
        if not recs:
            return {"ok": False, "note": "records are gone", "records": 0,
                    "candidates": 0}
        arrivals = {r["record_id"] for r in recs}
        for rec in recs:
            for i, c in enumerate(candidates(conn, project, rec, cap=cap_each)):
                if c["record_id"] in arrivals:
                    continue
                prev = best.get(c["record_id"])
                if prev is None or i < prev[0]:
                    best[c["record_id"]] = (i, c)
        conn.commit()
    ranked = sorted(best.values(), key=lambda t: t[0])
    cands = [c for _, c in ranked]
    body_ids = {c["record_id"] for i, c in ranked if i < body_each}
    # HEAL'S EYES (founder 2026-09-29): the rules and playbook sections
    # nearest to these records, so a record that only repeats them can go.
    # None = find them (core.filed); [] = none (a bench run without eyes).
    if filed is None and os.environ.get("BRAIN_HEAL_EYES", "1") != "0":
        from . import filed as _filed
        filed = _filed.for_heal(project, [r["record_id"] for r in recs])
    if not cands and len(recs) < 2:
        return {"ok": True, "records": len(recs), "candidates": 0,
                "applied": 0, "dropped": 0, "note": "nothing near it",
                "log": []}
    try:
        a = judge.ask("heal_judge", {"project": project, "records": recs, "candidates": cands,
                                     "body_ids": sorted(body_ids), **({"prompt": prompt} if prompt else {}),
                                     **({"filed": filed} if filed else {})})
    except judge.NoAnswer as e:
        return {"ok": False, "records": len(recs), "candidates": len(cands),
                "note": f"no verdict ({e.why})"}
    obj = a["answer"]
    if obj is None:
        return {"ok": False, "records": len(recs), "candidates": len(cands),
                "note": f"no verdict (parse: {a['fault']})"}
    from . import metering
    metering.record("heal", a["input_tokens"], a["output_tokens"])
    log: list[str] = []
    shown = arrivals | {c["record_id"] for c in cands}
    bodies = {c["record_id"]: c["content"] or "" for c in cands}
    for rec in recs:
        bodies[rec["record_id"]] = rec["content"] or ""
    applied, dropped = heal._apply(plain_ops(obj.get("ops") or []), project, shown,
                                   dry, log, current=bodies, gone=set(),
                                   readable=arrivals | body_ids, exists=_exists(project))
    for line in log:
        print(f"[reconcile] {project}: {line}", flush=True)
    return {"ok": True, "records": len(recs), "candidates": len(cands),
            "applied": applied, "dropped": dropped, "log": log,
            "input_tokens": int(a.get("input_tokens") or 0),
            "output_tokens": int(a.get("output_tokens") or 0)}


# ── THE CORPUS PASS ────────────────────────────────────────────────────────
# Founder 2026-09-03: "why is the doctor not fixing the curated records and
# cutting them?" Because nothing walked the corpus: heal ran only on arrivals
# against ten neighbours, and the sweep left with the watermark on 09-01.
# Measured that day on brain: 6,216 records stored, 107 distinct ones reached
# a session in seven days, one record written per user turn. The records
# exist to be pushed at sessions; the rest is the archive's job in a record's
# costume. This walks every record of a project once, in the neighbourhood
# reconcile already knows how to find, under the same prompt and the same
# applier guards, and cuts.
CLUSTER = 8             # records per call, all with bodies — the model must READ to delete




def corpus_pass(tenant: str, project: str, dry: bool = False,
                cluster: int = CLUSTER,
                limit: int | None = None, start_after: str = "",
                say=print) -> dict:
    """Walk a project's records once, cluster by cluster, and apply heal's
    verdicts. Resumable: records are visited in id order and `start_after`
    skips those before it; a deleted record simply is not there next time.
    `limit` caps the number of model calls (the dry sample)."""
    tok = store.bind(tenant)
    calls = applied = dropped = deletes = updates = 0
    visited: set[str] = set()
    gone: set[str] = set()
    last = ""
    try:
        with store.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT record_id FROM document WHERE project = %s "
                        "AND record_id <> 'memory_index' AND record_id > %s "
                        "ORDER BY record_id;", [project, start_after])
            ids = [r[0] for r in cur.fetchall()]
        say(f"[corpus] {project}: {len(ids)} records to walk"
            + (f" after {start_after}" if start_after else ""))
        for rid in ids:
            if rid in visited or rid in gone:
                continue
            if limit is not None and calls >= limit:
                break
            # THE READS take a connection and give it back before the ask
            # (2026-09-17: a connection held through the hub call starved
            # the pool and the pipe's own saves timed out).
            with store.connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT record_id, type, title, content, "
                            "to_char(updated_at, 'YYYY-MM-DD') FROM document "
                            "WHERE project = %s AND record_id = %s;", [project, rid])
                row = cur.fetchone()
                group = []
                if row:
                    rec = {"record_id": row[0], "type": row[1], "title": row[2],
                           "content": row[3], "upd": row[4]}
                    group = [rec]
                    for c in candidates(conn, project, rec, cap=cluster - 1):
                        if c["record_id"] in visited or c["record_id"] in gone:
                            continue
                        group.append(c)
                        if len(group) >= cluster:
                            break
                conn.commit()
            if not group:
                continue
            visited.update(g["record_id"] for g in group)
            try:
                a_ = judge.ask("heal_cluster", {"project": project, "records": group})
                obj = a_["answer"]
            except judge.NoAnswer as e:
                obj = None
                say(f"[corpus] {project}: the hub said no — {e.why}")
            calls += 1
            last = rid
            if obj is None:
                say(f"[corpus] {project}: no verdict for the cluster at {rid}")
                continue
            from . import metering
            metering.record("heal", a_["input_tokens"], a_["output_tokens"])
            log: list[str] = []
            shown = {g["record_id"] for g in group}
            bodies = {g["record_id"]: g.get("content") or "" for g in group}
            a, d = heal._apply(obj.get("ops") or [], project, shown, dry,
                               log, current=bodies, gone=gone, exists=_exists(project))
            applied += a
            dropped += d
            for line in log:
                if line.startswith(("DELETE ", "DRY DELETE ")):
                    deletes += 1
                    gone.add(line.split(" — ", 1)[0].split(":")[-1].strip())
                elif line.startswith(("UPDATE ", "DRY UPDATE ")):
                    updates += 1
                say(f"[corpus] {project} #{calls} ({len(group)} shown): {line}")
    finally:
        store.unbind(tok)
    out = {"project": project, "calls": calls, "visited": len(visited),
           "applied": applied, "dropped": dropped, "deletes": deletes,
           "updates": updates, "last": last, "dry": dry}
    say(f"[corpus] {project}: done {out}")
    return out


# ── the trigger (the box's cadence; the judgement is the hub's) ─────────────
import threading as _threading

_running: set[str] = set()
_lock = _threading.Lock()


def trigger(tenant: str, project: str, force: bool = False) -> str:
    """Fire fast heal for a project: from the compact hook and from every
    landing of curated records. Never blocks the caller: the run goes to a
    daemon thread, one per project at a time (the queue makes a skipped
    trigger free — the next one finds the same rows). Until 2026-09-10 a
    mode chose whose model judged; now every box judges through the hub, or
    not at all (a box without a pipe queues and waits)."""
    key = f"{tenant}/{project}"
    with _lock:
        if key in _running:
            return "heal: already running for this project"
        _running.add(key)

    def _run() -> None:
        try:
            r = drain(tenant, project, force=force)
            # Then any project whose queue was orphaned — a drain thread that
            # died with a container restart leaves rows nothing will come back
            # for, because a trigger only knows the project that just saved.
            from . import healqueue
            _tok = store.bind(tenant)
            try:
                with store.connection() as _c:
                    others = [p for p in healqueue.pending_projects(_c) if p != project]
            finally:
                store.unbind(_tok)
            for other in others:
                o = drain(tenant, other)
                if o.get("reconciled") or o.get("failed"):
                    print(f"[reconcile] {tenant}/{other} (orphaned queue): "
                          f"{o.get('reconciled', 0)} records, {o.get('ops_applied', 0)} ops, "
                          f"{o.get('failed', 0)} failed, queue {o.get('queue_depth', 0)}", flush=True)
                for k in ("reconciled", "ops_applied", "ops_dropped", "links", "failed", "dropped_forever"):
                    r[k] = r.get(k, 0) + o.get(k, 0)
            if not (r.get("reconciled") or r.get("failed") or r.get("queue_depth")):
                return                      # nothing landed; say nothing
            # FAILED, DROPPED_FOREVER and QUEUE_DEPTH belong on this line:
            # without them "0 ops" reads as "nothing needed doing" when it can
            # equally mean the hub never answered, the applier refused every
            # op, or the queue is backing up.
            print(f"[reconcile] {key}: {r.get('reconciled', 0)} records, "
                  f"{r.get('ops_applied', 0)} ops, {r.get('ops_dropped', 0)} dropped, "
                  f"{r.get('links', 0)} links, {r.get('failed', 0)} failed, "
                  f"{r.get('dropped_forever', 0)} lost, queue {r.get('queue_depth', 0)}", flush=True)
        except Exception as e:                                 # noqa: BLE001
            import traceback
            traceback.print_exc()
            print(f"[heal] {key} FAILED: {type(e).__name__}: {e}", flush=True)
        finally:
            with _lock:
                _running.discard(key)

    _threading.Thread(target=_run, name=f"heal-{project}", daemon=True).start()
    return "heal: started"
