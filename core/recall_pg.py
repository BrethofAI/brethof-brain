"""Hybrid recall on Postgres — a DELIBERATELY FAITHFUL port of v1's fusion.

This file exists to answer one question and no other: **does the engine change
cost recall quality?** So every ranking knob below is copied verbatim from
`mindcore/recall.py` — RRF k, stream weights, the distance cap and proximity
ramp, type weights, the recency half-life, the confidence floor, graph
expansion. Nothing is "improved". If the numbers move, the engine moved them,
not a tuning change, and that is the only way the comparison means anything.

THE THREE PLACES PARITY IS IMPERFECT — stated up front, because a comparison
whose caveats surface later is worthless:

1. **FTS scoring maths differs.** v1 sums SurrealDB BM25 `search::score` over up
   to 6 OR'd keywords; here it is one `ts_rank_cd` over the same keywords as an
   OR'd tsquery. RRF consumes *rank*, not score, so only the ordering matters —
   but the orderings are not guaranteed identical.
2. **`simple` vs SurrealDB's analyzer.** Both are unstemmed; the stopword and
   token-splitting rules are not identical.
3. **halfvec is f16.** Vectors were f64 in v1, so distances differ in about the
   4th decimal. Ranking impact should be nil; it is not provably zero.

Everything else — corpus, embeddings, queries — is byte-identical by
construction (v1's own vectors were copied verbatim by the export).
"""
from __future__ import annotations

import math
import re
import time

from . import store
from datetime import datetime, timezone

# ── ranking knobs. Copied from mindcore/recall.py; retuned ONCE from the
# 100-query measured session of 2026-08-31 (retrieval_tuning_session record):
# the floor was cutting only CORRECT vector-only answers (junk scored 62-78,
# far above any floor), so it is 0 — ranking and top_k are the gate now.
_RRF_K = 60
_RECENCY_HALF_LIFE_DAYS = 180.0
_TYPE_WEIGHTS = {
    "feedback": 1.2, "decision": 1.2, "architecture": 1.15,
    "gotcha": 1.1, "runbook": 1.1, "bug": 1.05, "commit": 0.6,
}
_STREAM_WEIGHTS = {"cur_vec": 1.0, "cur_fts": 1.0, "chat_vec": 0.7, "chat_fts": 0.7,
                   "pb_fts": 0.9}
# THE PLAYBOOKS ARE THE WIKI (founder 2026-09-09) — and until 2026-09-10 recall
# could not see them: "which box hosts the hub and how is it deployed" has
# its whole answer in the atlas plate and recall returned state records.
# Keyword lane over name, annotation and content, a few hits; no vectors
# yet (playbooks are not embedded).
_PLAYBOOK_HITS = 3
# A playbook that matches by keyword is scored on its own floor, like a
# graph-linked record: through the fused RRF a one-stream hit ranked 24th
# behind records two streams agreed on (measured 2026-09-10: the atlas plate
# brain-map-hub sat at rank 24 for "which box hosts the hub and how is it
# deployed"). The floor is below a strong record and above the tail.
# 2026-09-10, measured on 30 real prompts after the fence: playbooks still took
# 34 of 46 slots while records took 12 — at 45 a matching playbook outranked
# every record that scored between the bar and 45 ("the canary value" record
# at 42 lost to a playbook at 45). The floor went to 36, one above the bar.
# 2026-09-12, measured on 15 prompts that have NO answer in the memory (a
# cat, a wine stain, the tides): five of them were handed a playbook at 36 —
# a keyword-only match on a 75 KB plate, over the bar, filling a slot that
# should have stayed empty (the founder: "bloated with playbooks"). The floor
# now sits ONE UNDER the bar: a playbook never takes a prompt slot on its
# keywords alone — it stays in the recall tool's results, right under the
# records — and on the 56-question set the numbers did not move at all.
_PLAYBOOK_FLOOR = 34.0
_CONFIDENCE_FLOOR = 0.0
_BOOST_GATE = 8.0
# THE VECTOR ARM'S CONFIDENCE IS RELATIVE TO THE QUERY'S OWN BEST HIT
# (2026-09-12). The July spike set an absolute scale (strong at 0.40, noise
# past 0.62) on a query set that has since rotted; measured on 56 fresh
# paraphrased questions over 5,074 records the right answer sits at a
# median distance of 0.51 (p75 0.58, max 0.63) and the best WRONG answer
# at 0.55 — the two overlap, so no absolute threshold separates them, and
# the old scale scored a rank-one hit at 0.58 as 9 of 100: under every
# playbook (36) and every graph-expanded row (18). Eleven of the twelve
# answers the fusion lost were exactly those. What does separate right
# from wrong is the gap: when the target is first, the runner-up is a
# median 0.08 behind. So the best hit is worth 1, a hit _VEC_SPREAD behind
# it is worth nothing, and only the best hit's own distance carries an
# absolute term — softly, from _VEC_SOFT down to the cap.
_VEC_DIST_CAP = 0.72
_VEC_SPREAD = 0.15
_VEC_SOFT = 0.62
_GRAPH_WEIGHT = 0.6
_GRAPH_SEEDS = 3
_GRAPH_FLOOR = 18.0

_STOPWORDS = frozenset("""
a an and any are as at be been being but by can could did do does else for
from get had has have how i if in into is it its just like may might my new
no not now of on one or our over please shall should so some than that the
their them then there these they this those to too under use very was we
were what when where which who why will with would yes you your
jak czy co gdzie kiedy dlaczego czemu ktory ktora ktore moj moja moje nasz
nasza nasze ten ta te tam jest sa byc byl byla bylo nie tak oraz albo lub
ze na dla przez przy sie zeby aby moze mozna teraz prosze chce chcemy
""".split())


def fts_keywords(text: str, cap: int = 6) -> list[str]:
    """Identical tokenisation to v1 — same regex, same cap, same stopwords."""
    out: list[str] = []
    for tok in re.findall(r"[\w\-\.]{3,}", (text or "").lower()):
        t = tok.strip("-._")
        if len(t) < 3 or t in _STOPWORDS or t in out:
            continue
        out.append(t)
        if len(out) >= cap:
            break
    return out


def _recency_factor(ts) -> float:
    if not ts:
        return 1.0
    try:
        if isinstance(ts, datetime):
            t = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        else:
            t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        age = max(0.0, (datetime.now(timezone.utc) - t).total_seconds() / 86400)
    except Exception:
        return 1.0
    return 0.75 + 0.5 * (0.5 ** (age / _RECENCY_HALF_LIFE_DAYS))


def _vec_prox(dist: float, best: float) -> float:
    """The vector arm's confidence in one hit: relative to the query's best
    hit, times a soft absolute term on the best hit itself (a query whose
    nearest record is far away has no strong answer at all)."""
    if dist > _VEC_DIST_CAP:
        return 0.0
    rel = max(0.0, 1.0 - (dist - best) / _VEC_SPREAD)
    span = _VEC_DIST_CAP - _VEC_SOFT
    soft = 1.0 if best <= _VEC_SOFT or span <= 0 else max(0.0, (_VEC_DIST_CAP - best) / span)
    return rel * soft


def _vec_lit(vec: list[float]) -> str:
    return "[" + ",".join(f"{float(x):.6f}" for x in vec) + "]"


def _idf_fts(cur, table: str, kws: list[str], where: str, args: list,
             per: int, cols: str):
    """BM25's missing half, added back.

    MEASURED 2026-07-25: `ts_rank_cd` has NO inverse-document-frequency term.
    On the query "openclaw security architecture two layer no tailscale" it
    ranked a long document that repeats the common words above the one
    containing the two RARE words that actually identify the target — v1's BM25
    put the target first. Postgres has no built-in BM25, so score each document
    by the summed IDF of the query terms it matches. That restores the property
    that carried v1: rare terms decide, common terms barely move the needle.

    Document frequency comes from N cheap GIN probes (N <= 6 keywords).

    Returns (rows, evidence) where `evidence` is the summed IDF weight of
    EVERY query term — terms absent from the corpus count at maximum weight,
    matched at zero. rows' score / evidence is the FRACTION of the query's
    keyword evidence a document actually matches: the keyword stream's
    proximity. Measured 2026-08-31 (retrieval_tuning_session): without it a
    single shared word ('winter') carried full-confidence credit and junk
    outscored correct answers 62 to 0.9.
    """
    idfs = []
    cur.execute(f"SELECT count(*) FROM {table};")
    total = max(1, cur.fetchone()[0])
    for k in kws:
        cur.execute(f"SELECT count(*) FROM {table} WHERE tsv @@ to_tsquery('simple', %s);", [k])
        df = cur.fetchone()[0]
        idfs.append((k, df, math.log(1.0 + total / max(1, df))))
    live = [(k, w) for k, df, w in idfs if df > 0]
    evidence = sum(w for _, _, w in idfs) or 1.0
    if not live:
        return [], evidence
    score = " + ".join(
        f"(CASE WHEN tsv @@ to_tsquery('simple', %s) THEN {w:.6f} ELSE 0 END)"
        for _, w in live)
    terms = [k for k, _ in live]
    tsq = " | ".join(terms)
    cur.execute(
        f"SELECT {cols}, ({score}) AS score "
        f"FROM {table} WHERE tsv @@ to_tsquery('simple', %s) {where} "
        f"ORDER BY score DESC, ts_rank_cd(tsv, to_tsquery('simple', %s)) DESC "
        f"LIMIT %s;",
        terms + [tsq] + args + [tsq, per])
    return cur.fetchall(), evidence


def _merge_chunks(conn, cur, base, qv, per, join, cols, filt, args, key):
    """Fold windowed-embedding hits into a vector stream: search the chunk
    table, credit each PARENT row with its best window, dedupe against the
    base hits by `key` keeping the smaller distance. Long texts stop being
    invisible past their first model window (founder 2026-08-12: "the
    length shouldn't be limited at all"). Defensive: a tenant whose chunk
    tables aren't provisioned yet just keeps its base hits."""
    try:
        cur.execute(
            f"SELECT {cols}, c.embedding <=> %s::halfvec AS dist "
            f"FROM {join} WHERE true {filt} "
            f"ORDER BY c.embedding <=> %s::halfvec LIMIT %s;",
            [qv] + args + [qv, per])
        extra = cur.fetchall()
    except Exception:
        conn.rollback()
        return base
    if not extra:
        return base
    best = {}
    for r in base + list(extra):
        k = key(r)
        if k not in best or r[6] < best[k][6]:
            best[k] = r
    return sorted(best.values(), key=lambda r: r[6])[:per]


def _window(col: str, since, until) -> tuple[str, list]:
    """A period as SQL: `col` in [since, until) — both optional."""
    sql, args = "", []
    if since:
        sql, args = sql + f" AND {col} >= %s", args + [since]
    if until:
        sql, args = sql + f" AND {col} < %s", args + [until]
    return sql, args


def recall_hits(conn, query_vec, query_text: str, project: str = "",
                top_k: int = 10, include_chat: bool = True,
                include_curated: bool = True, fts: str = "rank_cd",
                allowed_projects: list[str] | None = None, since=None, until=None):
    """(score, id, entry) tuples, same contract as v1's recall_hits.

    `conn` is a psycopg connection to the tenant database. Ids are rendered
    `project:record_id` so they compare directly against v1's SurrealDB ids.

    `include_curated=False` gives the chat-archive-only search the customer
    tier exposes as `search_history` (founder 2026-07-28: "curated data are
    truth and chat history can be noise full of outdated info" — the split
    keeps the caller aware of which one it is trusting).
    """
    lim = max(1, min(int(top_k), 30))
    per = max(10, lim)
    # RULES ARE NOT SEARCH CANDIDATES (founder 2026-08-31: "they are rules,
    # not knowledge"). Law is delivered by its own three channels — session
    # injection, the per-prompt rule flag, list_rules — and the measured
    # session showed rules outranking the correct answer on 5/30 hard
    # queries purely on length. v1 appended `rules` to every query; cut.
    # THE GLOBAL LAYER RIDES WITH EVERY PROJECT (founder 2026-09-12: "global
    # need to be in hook search"). `global` is the thin shared layer between
    # projects — only facts true everywhere (the workstation, the estate's
    # access, the backups) — and until today a project-scoped search never
    # looked at it: a brain session asking what the workstation runs got
    # nothing, because the answer lived in global. Records only; the chat
    # archive stays the project's own. A scoped key still intersects below,
    # so a key without global sees no global.
    projs = ([project] + (["global"] if project != "global" else [])) if project else None
    chat_projs = [project] if project else None
    # Scoped keys (2026-07-28): the caller may hard-limit which projects exist
    # AT ALL for this search — both stores, both arms. Intersect, don't union:
    # a scoped key asking for a project outside its scope gets nothing.
    if allowed_projects is not None:
        projs = ([p for p in projs if p in allowed_projects] if projs
                 else list(allowed_projects))
        chat_projs = ([p for p in chat_projs if p in allowed_projects]
                      if chat_projs else list(allowed_projects))
        include_curated = include_curated and bool(projs)
        include_chat = include_chat and bool(chat_projs)
        if not include_curated and not include_chat:
            return []
    kws = fts_keywords(query_text)
    tsq = " | ".join(kws) if kws else ""
    qv = _vec_lit(query_vec) if query_vec else None

    fused: dict[str, dict] = {}
    names: list[str] = []
    cur = conn.cursor()

    def add(name, rows, is_vec, evidence=None):
        names.append(name)
        best = float(rows[0]["dist"]) if (is_vec and rows) else 0.0
        for rank, r in enumerate(rows[:per]):
            prox = 1.0
            if evidence:
                # keyword stream: confidence = fraction of the query's IDF
                # evidence this document matches (rare terms decide; one
                # stray shared word no longer scores like a full match)
                prox = max(0.0, min(1.0, r["score"] / evidence))
            if is_vec:
                prox = _vec_prox(float(r["dist"]), best)
            e = fused.setdefault(r["rid"], {"row": r, "rrf": 0.0,
                                            "chat": name.startswith("chat")})
            e["rrf"] += _STREAM_WEIGHTS[name] * prox / (_RRF_K + rank + 1)

    if projs is not None:
        projs = [p for p in projs if p != "rules"]   # scoped keys too
        if not projs:
            include_curated = False
    pfilter = ("AND project = ANY(%s)" if projs
               else "AND project <> 'rules'")
    pargs = [projs] if projs else []
    # A PERIOD (2026-10-23): records by when they last changed, chat by when
    # it was said; the chunk joins get the same window on their parent row
    dsql, dargs = _window("updated_at", since, until)
    dsql_t, _ = _window("t.updated_at", since, until)
    pfilter_t = pfilter.replace("project", "t.project") + dsql_t
    pfilter += dsql
    pargs += dargs

    # HNSW POST-FILTER STARVATION (measured 2026-08-07, founder tenant):
    # a project-scoped vector search whose filter is NOT selective enough
    # makes the planner prefer the HNSW index, which returns ef_search
    # candidates ACROSS ALL PROJECTS and only then applies the filter —
    # asking for 40 hits in 'global' returned 5. Iterative scan (pgvector
    # 0.8) keeps pulling from the index until the filtered set is full;
    # relaxed_order lets it do that without a full re-sort. Per
    # transaction, so nothing leaks into other queries.
    if qv:
        try:
            cur.execute("SET LOCAL hnsw.iterative_scan = relaxed_order;")
            cur.execute("SET LOCAL hnsw.max_scan_tuples = 40000;")
        except Exception:
            conn.rollback()          # older pgvector — exact-scan fallback
            cur = conn.cursor()

    if include_curated and qv:
        cur.execute(
            f"SELECT project, record_id, type, title, content, updated_at, "
            f"       embedding <=> %s::halfvec AS dist "
            f"FROM document WHERE embedding IS NOT NULL {pfilter} "
            f"ORDER BY embedding <=> %s::halfvec LIMIT %s;",
            [qv] + pargs + [qv, per])
        rows = list(cur.fetchall())
        rows = _merge_chunks(conn, cur, rows, qv, per,
                             "document_chunk c JOIN document t "
                             "ON t.id = c.document_id",
                             "t.project, t.record_id, t.type, t.title, "
                             "t.content, t.updated_at",
                             pfilter_t, pargs,
                             key=lambda r: (r[0], r[1]))
        add("cur_vec", [_doc(r) for r in rows
                        if r[6] <= _VEC_DIST_CAP], True)
    DCOLS = "project, record_id, type, title, content, updated_at"
    if include_curated and tsq:
        if fts == "idf":
            rows, ev = _idf_fts(cur, "document", kws, pfilter, pargs, per,
                                DCOLS)
            add("cur_fts", [_doc(r) for r in rows], False, evidence=ev)
        else:
            cur.execute(
                f"SELECT {DCOLS}, ts_rank_cd(tsv, to_tsquery('simple', %s)) AS score "
                f"FROM document WHERE tsv @@ to_tsquery('simple', %s) {pfilter} "
                f"ORDER BY score DESC LIMIT %s;",
                [tsq, tsq] + pargs + [per])
            rows = cur.fetchall()
            add("cur_fts", [_doc(r) for r in rows], False)

    # NON-DIALOGUE ROWS ARE INVISIBLE TO RECALL (2026-08-12). Before the
    # client's tool_result filter landed (2026-07-11) the archive ingested
    # MCP tool responses, raw JSON dumps and whole memory_index payloads
    # echoed back into chat. An echoed index matches almost ANY query, so it
    # crowds real conversation out of both the vector and keyword lanes.
    # Those rows are marked embed_skip and excluded HERE rather than deleted:
    # the archive stays immutable and complete, it simply stops answering.
    # A RECAP IS NEVER SEARCHED (founder 2026-09-22): a compacting session's
    # retelling of turns the archive already holds.
    from .chatkind import not_recap
    cfilter = "AND NOT coalesce(embed_skip, false) AND " + not_recap()
    cargs: list = []
    cfilter_t, cargs_t = "AND NOT coalesce(embed_skip, false) AND " + not_recap("t."), []
    if chat_projs:
        # WHICH ROWS ARE THIS PROJECT'S TODAY (2026-09-09): the ownership
        # table's moves, not the archived column alone (core/ownership).
        from . import ownership
        w, wa = ownership.chat_where(cur, chat_projs)
        wt, wta = ownership.chat_where(cur, chat_projs, alias="t.")
        cfilter += f" AND ({w})"
        cargs = list(wa)
        cfilter_t += f" AND ({wt})"
        cargs_t = list(wta)
    csql, cdargs = _window("ts", since, until)
    csql_t, _ = _window("t.ts", since, until)
    cfilter, cargs = cfilter + csql, cargs + cdargs
    cfilter_t, cargs_t = cfilter_t + csql_t, cargs_t + cdargs
    if include_chat and qv:
        cur.execute(
            f"SELECT project, session_id, turn_index, role, text, ts, "
            f"       embedding <=> %s::halfvec AS dist "
            f"FROM chat WHERE embedding IS NOT NULL {cfilter} "
            f"ORDER BY embedding <=> %s::halfvec LIMIT %s;",
            [qv] + cargs + [qv, per])
        rows = list(cur.fetchall())
        rows = _merge_chunks(conn, cur, rows, qv, per,
                             "chat_chunk c JOIN chat t ON t.id = c.chat_id",
                             "t.project, t.session_id, t.turn_index, "
                             "t.role, t.text, t.ts",
                             cfilter_t, cargs_t,
                             key=lambda r: (r[0], r[1], r[2]))
        add("chat_vec", _relabel(cur, [_chat(r) for r in rows
                                       if r[6] <= _VEC_DIST_CAP]), True)
    CCOLS = "project, session_id, turn_index, role, text, ts"
    if include_chat and tsq:
        if fts == "idf":
            rows, ev = _idf_fts(cur, "chat", kws, cfilter, cargs, per, CCOLS)
            add("chat_fts", _relabel(cur, [_chat(r) for r in rows]), False, evidence=ev)
        else:
            cur.execute(
                f"SELECT {CCOLS}, ts_rank_cd(tsv, to_tsquery('simple', %s)) AS score "
                f"FROM chat WHERE tsv @@ to_tsquery('simple', %s) {cfilter} "
                f"ORDER BY score DESC LIMIT %s;",
                [tsq, tsq] + cargs + [per])
            rows = cur.fetchall()
            add("chat_fts", _relabel(cur, [_chat(r) for r in rows]), False)

    if include_curated and kws and not (since or until):    # playbooks carry no period
        pb = playbook_hits(cur, kws, project=project)
        if pb:
            names.append("pb_fts")
            top_pb = max(x["score"] for x in pb) or 1.0
            for i, r in enumerate(pb):
                fused.setdefault(r["rid"], {"row": r, "rrf": 0.0, "chat": False,
                                            "pb_score": _PLAYBOOK_FLOOR * (r["score"] / top_pb) - 2.0 * i})
    if not names:
        return []
    _graph_expand(cur, fused)
    # (The graph's names no longer rank records — founder 2026-09-17: the
    # records and the graph are two systems, neither reads the other. The
    # entity expansion measured neutral on the 56-question set anyway. The
    # old inventory graph's own "[graph]" lines left recall on 2026-09-27.)

    max_rrf = sum(_STREAM_WEIGHTS[n] for n in names) / (_RRF_K + 1)
    scored = []
    for rid, e in fused.items():
        gs = e.get("graph_score")
        if gs is not None:
            scored.append((max(0.0, gs), rid, e))
            continue
        pbs = e.get("pb_score")
        if pbs is not None:
            scored.append((max(0.0, min(pbs, 100.0)), rid, e))
            continue
        r = e["row"]
        base = 100.0 * e["rrf"] / max_rrf
        if e["chat"]:
            w = 1.0
        else:
            w = _TYPE_WEIGHTS.get(str(r.get("type") or ""), 1.0)
        s = (base * w * _recency_factor(r.get("ts"))
             if base >= _BOOST_GATE else base)
        scored.append((min(s, 100.0), rid, e))
    scored.sort(reverse=True, key=lambda x: x[0])
    return [x for x in scored if x[0] >= _CONFIDENCE_FLOOR][:lim]


def playbook_fence(name: str, project: str, projects: set) -> bool:
    """A playbook belongs in a session's prompt block only when it is the
    session's own (`<project>-…`) or a bare method (no project prefix).
    Measured 2026-09-10 on 30 real prompts: 37 of 55 record slots went to
    OTHER projects' playbooks — a bv session about GPU0 got brain-map-box,
    a marketing session got ms-map-prompting — because the lane searched
    the whole shelf. No project (an owner's cross-project recall) = no fence."""
    if not project:
        return True
    owner = project_of_name(name, projects)
    return owner in ("", project)


def project_of_name(name: str, projects: set) -> str:
    """The project a playbook's name files it under, '' for a bare method."""
    head = (name or "").split("-", 1)[0]
    return head if "-" in (name or "") and head in projects else ""


def playbook_support(kws: list[str], name: str, annotation: str, content: str) -> tuple[int, int]:
    """(keywords found in name+annotation, keywords found in the body). One
    keyword somewhere in a 75 KB plate is not a match — the OR query found
    estate-map-secrets for 'what is the canary value for the bench'."""
    head = f"{name} {annotation}".lower()
    body = (content or "").lower()
    hk = sum(1 for k in kws if k.lower() in head)
    bk = sum(1 for k in kws if k.lower() in body)
    return hk, bk


def playbook_hits(cur, kws: list[str], limit: int = _PLAYBOOK_HITS, project: str = "") -> list[dict]:
    """Keyword hits over the playbook store, shaped like curated records
    (project 'playbook', type 'playbook', rid playbook:<name>). Fenced to the
    session's project plus the bare methods, and a hit needs real support:
    a keyword in the name or annotation and two keywords in all, or two in
    the name/annotation alone (2026-09-10)."""
    if not kws:
        return []
    tsq = " | ".join(kws)
    try:
      with store.savepoint(cur, "pb"):
        cur.execute("SELECT to_regclass('playbook');")
        if not (cur.fetchone() or [None])[0]:
            return []
        cur.execute("SELECT name FROM project;")
        projects = {r[0] for r in cur.fetchall()}
        cur.execute(
            "SELECT name, annotation, content, updated_at, "
            "  ts_rank_cd(to_tsvector('simple', name || ' ' || annotation || ' ' || content), "
            "             to_tsquery('simple', %s), 32) AS score "
            "FROM playbook WHERE to_tsvector('simple', name || ' ' || annotation || ' ' || content) "
            "  @@ to_tsquery('simple', %s) ORDER BY score DESC LIMIT %s;", [tsq, tsq, limit * 8])
        rows = cur.fetchall()
    except Exception:                                          # noqa: BLE001
        return []                     # the transaction is intact (savepoint)
    out = []
    for name, ann, content, upd, score in rows:
        if not playbook_fence(name, project, projects):
            continue
        hk, bk = playbook_support(kws, name, ann or "", content or "")
        # a short playbook IS its text, so its body counts; a plate of tens
        # of thousands of chars needs the name or the line to say so
        short = len(content or "") < 4000
        bare = "-" not in name or project_of_name(name, projects) == ""
        # a bare method's body is generic craft ("check", "delete", "list"):
        # ledger-review answered "Come on! It's taking very long time" — it
        # needs its name or its line to carry a keyword
        if bare and hk < 1:
            continue
        if not (hk >= 2 or (hk >= 1 and bk >= 1) or (short and bk >= 3)):
            continue
        head = " ".join((ann or content or "").split())[:200]
        out.append({"rid": f"playbook:{name}", "project": "playbook", "record_id": name,
                    "type": "playbook", "title": name, "content": head, "ts": upd,
                    "dist": 0.0, "score": float(score or 0)})
        if len(out) >= limit:
            break
    return out


def _doc(r) -> dict:
    return {"rid": f"{r[0]}:{r[1]}", "project": r[0], "record_id": r[1],
            "type": r[2], "title": r[3], "content": r[4], "ts": r[5],
            "dist": float(r[6]), "score": float(r[6])}


def _relabel(cur, hits: list[dict]) -> list[dict]:
    """A moved session's rows show the project they belong to today
    (core/ownership), not the column the archive filed them under."""
    try:
        from . import ownership
        return ownership.relabel(cur, hits)
    except Exception:                                          # noqa: BLE001
        return hits


def _chat(r) -> dict:
    return {"rid": f"{r[0]}_chat:{r[1]}#{r[2]}", "project": r[0],
            "session_id": r[1], "turn_index": r[2],
            "role": r[3], "text": r[4], "ts": r[5],
            "dist": float(r[6]), "score": float(r[6])}


def _graph_expand(cur, fused: dict) -> None:
    """5th signal — follow refs both ways from the top curated hits. Same seed
    count, same weight, same floor as v1; a join table instead of a graph
    engine, which is all a 1-hop neighbourhood ever needed."""
    seeds = [rid for rid, ent in
             sorted(fused.items(), key=lambda kv: -kv[1]["rrf"])
             if not ent["chat"]][:_GRAPH_SEEDS]
    if not seeds:
        return
    # Two parallel text[] unnested into a pair set — psycopg cannot bind a list
    # of tuples ("input of anonymous composite types is not implemented").
    ps = [s.split(":", 1)[0] for s in seeds]
    rs = [s.split(":", 1)[1] for s in seeds]
    seed_rank = {s: i for i, s in enumerate(seeds)}
    cur.execute(
        "SELECT src_project||':'||src_record, dst_project||':'||dst_record "
        "FROM edge WHERE kind='refs' AND ("
        "  (src_project, src_record) IN (SELECT * FROM unnest(%s::text[], %s::text[]))"
        "  OR (dst_project, dst_record) IN (SELECT * FROM unnest(%s::text[], %s::text[])));",
        [ps, rs, ps, rs])
    neigh: dict[str, int] = {}
    for src, dst in cur.fetchall():
        for a, b in ((src, dst), (dst, src)):
            if a in seed_rank and b not in seed_rank:
                hr = seed_rank[a]
                if b not in neigh or hr < neigh[b]:
                    neigh[b] = hr
    if not neigh:
        return
    missing = [n for n in neigh if n not in fused]
    rowmap: dict[str, dict] = {}
    if missing:
        cur.execute(
            "SELECT project, record_id, type, title, content, updated_at "
            "FROM document WHERE (project, record_id) IN "
            "(SELECT * FROM unnest(%s::text[], %s::text[]));",
            [[m.split(":", 1)[0] for m in missing],
             [m.split(":", 1)[1] for m in missing]])
        for r in cur.fetchall():
            d = {"rid": f"{r[0]}:{r[1]}", "project": r[0], "record_id": r[1],
                 "type": r[2], "title": r[3], "content": r[4], "ts": r[5]}
            rowmap[d["rid"]] = d
    for nid, hr in neigh.items():
        if nid in fused:
            fused[nid]["rrf"] += _GRAPH_WEIGHT / (_RRF_K + hr + 1)
        elif nid in rowmap:
            fused[nid] = {"row": rowmap[nid], "rrf": 0.0, "chat": False,
                          "graph_score": _GRAPH_FLOOR - 2.0 * hr}


def line(s: float, rid: str, e: dict) -> str:
    """One rendered hit. Format is v1's byte-for-byte — the client shows this
    text to the user, so a change here is a change they see."""
    r = e["row"]
    if e["chat"]:
        ts = str(r.get("ts") or "")[:16]
        txt = " ".join((r.get("text") or "").split())[:240]
        return (f"[{s:.0f}] [chat archive {r.get('project')}_chat/{r.get('role', '-')}] "
                f"{ts} {(r.get('session_id') or '')[:8]}: {txt}")
    title = r.get("title") or rid
    content = " ".join((r.get("content") or "").split())[:200]
    upd = str(r.get("ts") or "")[:10]
    tag = "↪linked " if e.get("graph_score") is not None else ("📖 " if e.get("pb_score") is not None else "")
    return (f"[{s:.0f}] {tag}[curated record {r.get('project')}/{r.get('type', '')}] {rid} — "
            f"{title}: {content} (upd {upd})")


# THE DECISION LINES IN THE SEARCH (founder 2026-09-28: "fix recall to
# search the lines"). The recall tool had not read the graph since the old
# inventory graph left (09-27); measured the same day on 90 real memory
# searches, a decision line already answered 32. A dated section UNDER the
# results, never mixed into them: records are the truth of now, a line is
# what was decided, tried or dropped on its day. Only a tenant with the
# graph add-on has lines; everyone else sees no section.
HISTORY_LINES = 5


def history_section(cur, query_vec, query_text: str, project: str = "",
                    allowed_projects: list[str] | None = None, k: int = HISTORY_LINES,
                    since=None, until=None) -> str:
    from . import decisionline
    cur.execute("SELECT to_regclass('decision') IS NOT NULL;")
    if not cur.fetchone()[0]:
        return ""
    if project:
        projects = [project] + ([] if project == "global" else ["global"])
    else:
        cur.execute("SELECT DISTINCT project FROM decision;")
        projects = [r[0] for r in cur.fetchall()]
    if allowed_projects is not None:
        projects = [p for p in projects if p in set(allowed_projects)]
    lines = decisionline.candidates(cur, project, query_text, k=k, projects=projects, vec=query_vec,
                                    since=since, until=until)
    if not lines:
        return ""
    out = ["— DECISION GRAPH (what was decided, tried, rejected or dropped, each on its day and "
           "for its own situation; history, not a rule — check a line is still current before acting on it):"]
    for d in lines:
        why = " ".join((d.get("why") or "").split())
        what = " ".join((d.get("what") or "").split())
        out.append(f"[decision graph {d['day'] or '?'}] {d['kind']} ({d['project']}): {what[:300]}"
                   + (f" — why: {why[:200]}" if why else ""))
    return "\n".join(out)


def recall_text(conn, query_vec, query_text: str, project: str = "",
                top_k: int = 10, include_chat: bool = True,
                include_curated: bool = True, since=None, until=None) -> str:
    top = recall_hits(conn, query_vec, query_text, project=project,
                      top_k=top_k, include_chat=include_chat,
                      include_curated=include_curated, fts="idf", since=since, until=until)
    hist = history_section(conn.cursor(), query_vec, query_text, project=project,
                           since=since.date() if since else None,
                           until=until.date() if until else None) if include_curated else ""
    if not top and not hist:
        return (f"Memory has NOTHING on '{query_text}' — no record or "
                f"conversation matched by meaning or by keyword.")
    out = [line(s, rid, e) for s, rid, e in top]
    return "\n".join(out + ([hist] if hist else []))


def timed_recall(conn, query_vec, query_text, **kw):
    """Returns (hits, milliseconds). Latency excludes embedding, which is the
    same sidecar call on both engines and would only add noise to the delta."""
    t0 = time.perf_counter()
    hits = recall_hits(conn, query_vec, query_text, **kw)
    return hits, (time.perf_counter() - t0) * 1000.0
