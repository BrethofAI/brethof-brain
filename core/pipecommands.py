"""THE COMMANDS THE HUB SENDS DOWN THE PIPE — the box's side.

Design `brain-design-hub-brain`, step 1 (founder 2026-09-10: "the hub is
the brain, our secret and everything; the worker which needs power, CPU,
RAM, drive is in the container"). The hub composes the session brief and
the per-prompt memory, judges heal, the decision lines and the proposer;
this box holds the memory and answers a FIXED, small vocabulary of
commands — reads that return rows, writes that apply an answer through the
mechanical guards that already exist. Nothing free-form travels down: an
unknown command is refused by name. A customer reading the pipe sees
commands and rows, never logic.

Every handler is (tenant, body) -> dict, JSON-safe, run under the tenant
binding by `run` (core/pipeclient calls it in a thread, under a deadline
and an in-flight cap). A failure surfaces as {"ok": False, "why"} on the
wire, never as silence. The hub's vocabulary (hub/server.py COMMANDS) and
HANDLERS here are pinned equal by tests/test_pipe_commands.py.
"""
from __future__ import annotations

import datetime as _dt
import decimal
import traceback
from typing import Callable

from . import store

HANDLERS: dict[str, Callable[[str, dict], dict]] = {}

# Bounds a command's body may ask for — the wire is small by design.
TEXT_CHARS = 20_000
EMBED_TEXTS = 8
RECALL_K = 30
LIST_MAX = 50
_PREV_TURN_CHARS = 1500


def command(name: str):
    def deco(fn):
        HANDLERS[name] = fn
        return fn
    return deco


def run(tenant: str, op: str, body: dict) -> dict:
    """One command under the tenant binding. Raises to the caller, who puts
    the failure on the wire."""
    fn = HANDLERS.get(op)
    if fn is None:
        raise KeyError(op)
    tok = store.bind(tenant)
    try:
        return fn(tenant, body if isinstance(body, dict) else {})
    finally:
        store.unbind(tok)


# ── JSON-safe rows ──────────────────────────────────────────────────────────
_DROP_KEYS = frozenset({"embedding", "vector", "vec"})


def _j(v):
    if isinstance(v, dict):
        return {str(k): _j(x) for k, x in v.items() if str(k) not in _DROP_KEYS}
    if isinstance(v, (list, tuple, set)):
        return [_j(x) for x in v]
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


def _s(body: dict, key: str, cap: int = 400) -> str:
    v = body.get(key)
    return (str(v) if v is not None else "")[:cap]


def _text(body: dict, key: str = "text") -> str:
    return _s(body, key, TEXT_CHARS)


def _int(body: dict, key: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(body.get(key, default))))
    except (TypeError, ValueError):
        return default


def _project(body: dict) -> str:
    p = _s(body, "project", 120).strip()
    if not p:
        raise ValueError("no project")
    return p


def _sid(body: dict) -> str:
    from .injection import _SID_RE
    return _SID_RE.sub("", _s(body, "session_id", 160))[:120]


# ── search ──────────────────────────────────────────────────────────────────
@command("embed")
def embed(tenant: str, body: dict) -> dict:
    """The box's embedder makes the vectors — its CPU, not ours."""
    from . import embed as _embed
    texts = body.get("texts")
    if isinstance(texts, str):
        texts = [texts]
    texts = [str(t)[:TEXT_CHARS] for t in (texts or []) if str(t).strip()][:EMBED_TEXTS]
    if not texts:
        return {"vectors": []}
    kind = "document" if _s(body, "kind") == "document" else "query"
    vecs = _embed.embed(texts, kind=kind)
    if not vecs:
        raise RuntimeError("embedder unavailable")
    return {"vectors": [list(map(float, v)) for v in vecs]}


def _last_user_turn(cur, sid: str) -> str:
    if not sid:
        return ""
    try:
        with store.savepoint(cur, "prev"):
            cur.execute("SELECT text FROM chat WHERE session_id = %s AND role = 'user' "
                        "ORDER BY ts DESC, turn_index DESC LIMIT 1;", [sid])
            row = cur.fetchone()
        return (row[0] or "") if row else ""
    except Exception:                                          # noqa: BLE001
        return ""


def _last_reply(cur, sid: str, limit: int = 12000) -> str:
    """The agent's whole previous reply in this session: its text rows since
    the user's last message (tool markers dropped). At prompt time the new
    message is not archived yet, so this is the reply the message answers."""
    if not sid:
        return ""
    try:
        with store.savepoint(cur, "reply"):
            cur.execute("SELECT turn_index FROM chat WHERE session_id = %s AND role = 'user' "
                        "ORDER BY ts DESC, turn_index DESC LIMIT 1;", [sid])
            row = cur.fetchone()
            cur.execute("SELECT text FROM chat WHERE session_id = %s AND role = 'assistant' AND turn_index > %s "
                        "AND text NOT LIKE '[tool_use%%' ORDER BY turn_index;", [sid, row[0] if row else -1])
            text = "\n".join(r[0] for r in cur.fetchall() if r[0])
        return text[-limit:]
    except Exception:                                          # noqa: BLE001
        return ""


@command("last_reply")
def last_reply(tenant: str, body: dict) -> dict:
    """The previous reply the new message answers — the hub writes its
    recall questions from the message with this as context (core/recallq)."""
    with store.connection() as conn:
        return {"text": _last_reply(conn.cursor(), _sid(body))}


def _hit(s, rid, e) -> dict:
    return {"score": round(float(s), 3), "rid": rid, "chat": bool(e.get("chat")),
            "row": _j(e.get("row") or {})}


@command("recall")
def recall(tenant: str, body: dict) -> dict:
    """The search the ambient hook runs today, as rows: hybrid vector +
    keyword over the records and the archive, the previous user turn folded
    in when asked (a follow-up carries its topic), each record keeping its
    best score of the two queries. Bars, slots and cooldowns are the hub's."""
    from . import embed as _embed, recall_pg
    project = _project(body)
    query = _text(body, "query").strip()
    if not query:
        return {"hits": [], "folded": False, "vector": False}
    k = _int(body, "k", 12, 1, RECALL_K)
    fts = _s(body, "fts") or "idf"
    include_chat = bool(body.get("include_chat", True))
    include_curated = bool(body.get("include_curated", True))
    with store.connection() as conn:
        cur = conn.cursor()
        q = query
        if body.get("fold_previous"):
            prev = _last_user_turn(cur, _sid(body))
            if prev:
                q = prev[-_PREV_TURN_CHARS:] + "\n" + query
        extra = body.get("questions") if isinstance(body.get("questions"), list) else None
        if body.get("reply_topics") or extra:
            # B+v3 (founder 2026-11-17, core/recallq): the message, the
            # previous reply cut by topic, the model's questions — and the
            # folded query as today — all embedded in one call, each searched,
            # joined by rank, each record keeping its own best score.
            from . import recallq
            reply = _last_reply(cur, _sid(body)) if body.get("reply_topics") else ""
            texts = ([q] if q is not query else []) + recallq.queries(query, reply, [str(x) for x in (extra or [])])
            vecs = _embed.embed(texts, timeout=8.0, kind="query") or [None] * len(texts)
            lists = [recall_pg.recall_hits(conn, v, t, project=project, top_k=max(k, 8), fts=fts,
                                           include_chat=include_chat, include_curated=include_curated)
                     for t, v in zip(texts, vecs)]
            hits = recallq.fuse(lists, k)
            vec = vecs[0] if vecs else None
            sid = _sid(body)
            if sid:
                hits = _hold_back(cur, sid, hits)
            return {"hits": [_hit(s, rid, e) for s, rid, e in hits], "folded": q is not query,
                    "vector": vec is not None, "searches": len(texts)}
        vec = _embed.embed_one(q)
        hits = recall_pg.recall_hits(conn, vec, q, project=project, top_k=k, fts=fts,
                                     include_chat=include_chat, include_curated=include_curated)
        if q is not query:
            v2 = _embed.embed_one(query)
            solo = recall_pg.recall_hits(conn, v2, query, project=project, top_k=k, fts=fts,
                                         include_chat=include_chat, include_curated=include_curated)
            best: dict[str, tuple] = {}
            for s, rid, e in list(hits) + list(solo):
                if rid not in best or s > best[rid][0]:
                    best[rid] = (s, rid, e)
            hits = sorted(best.values(), key=lambda t: -t[0])
        # THE HOLD-BACK (2026-09-28): the prompt hook asks with its session;
        # the records that session wrote since its last compact stay out
        sid = _sid(body)
        if sid:
            hits = _hold_back(cur, sid, hits)
    return {"hits": [_hit(s, rid, e) for s, rid, e in hits], "folded": q is not query,
            "vector": vec is not None}


def _hold_back(cur, sid: str, hits: list) -> list:
    from . import injection
    keys = [(e["row"].get("project"), e["row"].get("record_id")) for _s, _r, e in hits
            if not e.get("chat") and e.get("row", {}).get("record_id")]
    if not keys:
        return hits
    cur.execute("SELECT project, record_id, meta->'origin'->>'session_id', meta->'origin'->>'turn_hi' "
                "FROM document WHERE (project, record_id) IN (SELECT * FROM unnest(%s::text[], %s::text[]));",
                [[k[0] for k in keys], [k[1] for k in keys]])
    origins = cur.fetchall()
    compact = injection.compact_turn(cur, sid)
    out_ = {(p, r) for p, r, osid, oturn in origins
            if injection.held(sid, compact, osid or "", oturn)
            or (str(osid or "").startswith("save-") and injection.held_save(cur, sid, osid))}
    return [h for h in hits if h[2].get("chat") or
            (h[2]["row"].get("project"), h[2]["row"].get("record_id")) not in out_]


# ── the brief's readers ─────────────────────────────────────────────────────
@command("notes")
def notes(tenant: str, body: dict) -> dict:
    from . import notes as _notes
    project = _project(body)
    with store.connection() as conn:
        ring = _notes.recent(conn, project, limit=_int(body, "limit", 5, 1, 10))
    return {"notes": _j(ring)}


@command("plan_next")
def plan_next(tenant: str, body: dict) -> dict:
    """The raw items of the plan playbook's Next list; the hub clips."""
    from . import injection, playbooks
    project = _project(body)
    with store.connection() as conn:
        row = playbooks.get(f"{project}-plan", cur=conn.cursor())
    if not row:
        return {"items": [], "updated": ""}
    items = injection.plan_next_lines(row[1], limit=_int(body, "limit", 10, 1, 20), clip=0)
    return {"items": items, "updated": _j(row[3])}


@command("proposals")
def proposals(tenant: str, body: dict) -> dict:
    from . import proposer
    project = _project(body)
    with store.connection() as conn:
        rows = proposer.pending(conn.cursor(), project, limit=_int(body, "limit", 5, 1, 20))
        conn.commit()
    return {"proposals": _j(rows)}


@command("projects")
def projects(tenant: str, body: dict) -> dict:
    """Names and record counts; the purposes are the hub's own rules."""
    with store.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT p.name, count(d.record_id) FROM project p "
            "LEFT JOIN document d ON d.project = p.name AND d.record_id <> 'memory_index' "
            "WHERE p.name NOT IN ('global','rules','state') GROUP BY p.name ORDER BY p.name;")
        rows = cur.fetchall()
    return {"projects": [{"name": n, "records": int(c)} for n, c in rows]}


@command("recap")
def recap(tenant: str, body: dict) -> dict:
    """The tail of the project's last session, whole; the hub clips."""
    from . import ownership
    project = _project(body)
    limit = _int(body, "limit", 12, 1, 40)
    with store.connection() as conn:
        cur = conn.cursor()
        w, a = ownership.chat_where(cur, project)
        # the last CONVERSATION (2026-11-10, Aurora): an explicit save is a
        # one-turn session of its own (save-<hex>, role 'saved'), and the
        # newest one stood in as "last session" — an app that saves its state
        # after every sitting saw its own JSON instead of what was said
        cur.execute(f"SELECT session_id FROM chat WHERE {w} AND session_id NOT LIKE 'save-%%' "
                    f"AND role <> 'saved' ORDER BY ts DESC LIMIT 1;", a)
        row = cur.fetchone()
        if not row:
            return {"session_id": "", "tail": []}
        cur.execute("SELECT role, text, turn_index FROM chat WHERE session_id = %s "
                    "ORDER BY ts DESC, turn_index DESC LIMIT %s;", [row[0], limit])
        tail = list(reversed(cur.fetchall()))
    return {"session_id": row[0],
            "tail": [{"role": r or "?", "text": t or "", "turn": i} for r, t, i in tail]}


@command("records")
def records(tenant: str, body: dict) -> dict:
    """Records by type, newest first — the designed sections' shelf."""
    project = _project(body)
    types = [str(t).strip().lower() for t in (body.get("types") or []) if str(t).strip()][:6]
    if not types:
        return {"records": []}
    prefixes = [t.replace("-", "_").replace(" ", "_") + "%" for t in types]
    limit = _int(body, "limit", 6, 1, 12)
    with store.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT record_id, type, coalesce(title,''), coalesce(content,''), updated_at "
            "FROM document WHERE project = %s AND record_id <> 'memory_index' "
            "AND (type = ANY(%s) OR record_id LIKE ANY(%s)) ORDER BY updated_at DESC LIMIT %s;",
            [project, types, prefixes, limit])
        rows = cur.fetchall()
    return {"records": [{"record_id": r, "type": t, "title": ti, "content": c, "updated": _j(u)}
                        for r, t, ti, c, u in rows]}


# ── the prompt hook's ledger ────────────────────────────────────────────────
@command("stamps")
def stamps(tenant: str, body: dict) -> dict:
    """The session's cooldown ledger and its current turn — it names the
    customer's record ids, so it stays here."""
    from . import injection
    sid = _sid(body)
    if not sid:
        return {"turn": 0, "stamps": {}}
    with store.connection() as conn:
        cur = conn.cursor()
        turn = injection._turn_now(cur, sid)
        st = injection._stamps(cur, sid)
    return {"turn": turn, "stamps": st}


@command("stamp")
def stamp(tenant: str, body: dict) -> dict:
    from . import injection
    sid = _sid(body)
    ids = [str(x)[:300] for x in (body.get("ids") or []) if str(x).strip()][:LIST_MAX]
    turn = _int(body, "turn", 0, 0, 10_000_000)
    if not sid or not ids:
        return {"stamped": 0}
    with store.connection() as conn:
        injection._stamp(conn, conn.cursor(), sid, turn, ids)
    return {"stamped": len(ids)}


@command("proposal_line")
def proposal_line(tenant: str, body: dict) -> dict:
    """The session's own pending proposal, counted as shown when returned."""
    from . import proposer
    sid = _sid(body)
    if not sid:
        return {"proposal": None}
    with store.connection() as conn:
        row = proposer.prompt_row(conn.cursor(), sid)
        conn.commit()
    return {"proposal": _j(row)}


# ── the prompt hook's history part ──────────────────────────────────────────
# What the history part handed out, per prompt — the day's reading of the
# graph in use (2026-09-28: on in the founder's sessions first, every
# injection logged and read before it goes further).
HISTORY_LOG_DDL = """
CREATE TABLE IF NOT EXISTS history_hook_log (
    id bigserial PRIMARY KEY,
    at timestamptz NOT NULL DEFAULT now(),
    project text NOT NULL,
    session_id text NOT NULL DEFAULT '',
    prompt text NOT NULL,
    line_ids bigint[] NOT NULL DEFAULT '{}'
);
"""
HISTORY_K = 25


@command("lines_near_subjects")
def lines_near_subjects(tenant: str, body: dict) -> dict:
    """The lines call's view built from the window's records (the history
    design, 2026-10-02): the open questions plus the lines nearest each
    record the records call just wrote. The hub's judge picks from it."""
    from . import decisionline
    project = _project(body)
    piece = str(body.get("piece") or "")[:240000]
    queries = [str(q) for q in (body.get("queries") or []) if str(q).strip()][:60]
    each = _int(body, "each", decisionline.SUBJECT_EACH, 1, 10)
    cap = _int(body, "cap", decisionline.SUBJECT_CAP, 1, 120)
    with store.connection() as conn:
        cur = conn.cursor()
        decisionline.ensure(cur)
        threads, near = decisionline.view_near_subjects(cur, project, piece, queries, each, cap)
        conn.rollback()
    return {"threads": threads, "near": near}


@command("records_near_subjects")
def records_near_subjects(tenant: str, body: dict) -> dict:
    """The still-true check's list (2026-10-02): this session's records
    nearest each record the window's records call just wrote."""
    from . import hubwindow
    project = _project(body)
    session_id = str(body.get("session_id") or "")[:200]
    queries = [str(q) for q in (body.get("queries") or []) if str(q).strip()][:30]
    each = _int(body, "each", 5, 1, 20)
    cap = _int(body, "cap", 25, 1, 100)
    if not session_id or not queries:
        return {"records": []}
    # THE RECORDS REBUILD (2026-10-05): its records live in its own bench
    # database, not in the tenant this pipe is bound to. Read-only, and only a
    # bench database (core.bench.PREFIX) — never another customer's memory.
    from .bench import PREFIX
    other = str(body.get("replay_tenant") or "")
    tok = store.bind(other) if other.startswith(PREFIX) and other[len(PREFIX):].replace("_", "").isalnum() else None
    try:
        with store.connection() as conn:
            cur = conn.cursor()
            recs = hubwindow.near_subjects(cur, project, session_id, queries, each, cap)
            conn.rollback()
    finally:
        if tok is not None:
            store.unbind(tok)
    return {"records": recs}


@command("ledger_near")
def ledger_near(tenant: str, body: dict) -> dict:
    """THE LEDGER CLEANER'S NEIGHBOURS (2026-10-03): for each new row the
    curator caught, its nearest open rows; for the window's new history
    lines and records, the open rows nearest any of them. The hub judges."""
    from . import ledger
    project = _project(body)
    rows = [str(r)[:2000] for r in (body.get("rows") or []) if str(r).strip()][:16]
    items = [str(r)[:2000] for r in (body.get("items") or []) if str(r).strip()][:60]
    k_rows = _int(body, "k_rows", ledger.NEAR_ROWS, 1, 10)
    k_open = _int(body, "k_open", ledger.NEAR_OPEN, 1, 40)
    with store.connection() as conn:
        cur = conn.cursor()
        got = ledger.near(cur, project, rows, items, k_rows, k_open)
        conn.commit()                    # the vectors filled for rows that had none
    return got


@command("history_near")
def history_near(tenant: str, body: dict) -> dict:
    """The decision lines nearest the prompt — the three-arm mechanical
    find (core/decisionline.candidates), no bar: candidates for the
    session's own agent to judge. Logged, every call."""
    from . import decisionline
    project, text = _project(body), _text(body, "query")
    k = _int(body, "k", HISTORY_K, 1, HISTORY_K)
    if not text.strip():
        return {"lines": []}
    sid = _sid(body)
    with store.connection() as conn:
        cur = conn.cursor()
        decisionline.ensure(cur)
        # THE HOLD-BACK (2026-09-28): lines this session wrote since its last
        # compact stay out; ask for more so the part still hands k
        lines = decisionline.candidates(cur, project, text, k=k + 20)
        if sid:
            from . import injection
            compact = injection.compact_turn(cur, sid)
            lines = [d for d in lines if not injection.held(sid, compact, d.get("session_id", ""), d.get("turn_hi"))
                     and not (str(d.get("session_id", "")).startswith("save-")
                              and injection.held_save(cur, sid, d["session_id"]))]
        lines = lines[:k]
        store.ensure_ddl(cur, "history_hook_log", HISTORY_LOG_DDL)
        cur.execute("INSERT INTO history_hook_log (project, session_id, prompt, line_ids) VALUES (%s, %s, %s, %s);",
                    [project, sid, text, [d["id"] for d in lines]])
        conn.commit()
    return {"lines": [{"id": d["id"], "day": d["day"], "kind": d["kind"], "what": d["what"], "why": d["why"]}
                      for d in lines]}


# ── the judgements' material and their answers ──────────────────────────────
@command("heal_candidates")
def heal_candidates(tenant: str, body: dict) -> dict:
    """One record and the records it might collide with, provenance kept."""
    from . import reconcile
    project = _project(body)
    rid = _s(body, "record_id", 300).strip()
    with store.connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT record_id, type, title, content FROM document "
                    "WHERE project = %s AND record_id = %s;", [project, rid])
        row = cur.fetchone()
        if not row:
            return {"record": None, "candidates": []}
        rec = {"record_id": row[0], "type": row[1], "title": row[2], "content": row[3]}
        cands = reconcile.candidates(conn, project, rec)
    return {"record": rec, "candidates": _j(cands)}


@command("apply_updates")
def apply_updates(tenant: str, body: dict) -> dict:
    """Heal's ops through heal._apply and its guards: an id must have been
    shown, a title-only record is never updated, a no-op update is refused.
    The current bodies are read here, never shipped back down."""
    from . import heal
    project = _project(body)
    ops = [o for o in (body.get("ops") or []) if isinstance(o, dict)][:LIST_MAX]
    shown = {str(x).split(":")[-1] for x in (body.get("shown") or [])}
    readable = body.get("readable")
    readable = ({str(x).split(":")[-1] for x in readable} if isinstance(readable, list) else None)
    log: list[str] = []
    current: dict[str, str] = {}
    if shown:
        with store.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT record_id, coalesce(content, '') FROM document "
                        "WHERE project = %s AND record_id = ANY(%s);", [project, list(shown)])
            current = dict(cur.fetchall())
    applied, dropped = heal._apply(ops, project, shown, False, log, current=current,
                                   gone=set(), readable=readable)
    for line in log:
        print(f"[pipe] {project}: heal {line}", flush=True)
    return {"applied": applied, "dropped": dropped, "log": log}


@command("write_proposal")
def write_proposal(tenant: str, body: dict) -> dict:
    from . import proposer
    sid = _sid(body)
    filed = _s(body, "filed", 120).strip()
    proposed = _s(body, "proposed", 120).strip()
    why = " ".join(_s(body, "why", 600).split())
    try:
        conf = max(0.0, min(1.0, float(body.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        conf = 0.0
    if not sid or not filed or not proposed:
        raise ValueError("write_proposal needs session_id, filed and proposed")
    with store.connection() as conn:
        n = proposer.file(conn.cursor(), sid, filed, proposed, conf, why)
        conn.commit()
    return {"written": int(n)}


@command("write_saves")
def write_saves(tenant: str, body: dict) -> dict:
    """The curator's finished writes: records, decision lines, ledger rows.
    Mechanical, transactional per section — a failed line write must not
    take the records with it. (The old inventory graph's sections — the
    exchange's `about`, `entities` and `moved` — left on 2026-09-27; an older
    hub that still sends them is ignored.)"""
    from . import curate_apply, ledger
    project = str(body.get("project") or "global")
    session_id = str(body.get("session_id") or "")
    # THE PIECE'S TURNS (2026-09-09): the hub hands back the turn range the
    # box sent up with the exchange, so every record, line and ledger
    # row this save carries points at the archive it came from. An old hub
    # sends none; the box then files what it can (the session) and checks
    # nothing.
    lo, hi = body.get("turn_lo"), body.get("turn_hi")
    have_turns = (isinstance(lo, int) and isinstance(hi, int)
                  and not isinstance(lo, bool) and 0 <= lo <= hi)
    origin = ({"session_id": session_id, "turn_lo": lo, "turn_hi": hi}
              if have_turns and session_id else
              ({"session_id": session_id} if session_id else None))
    recs = [r for r in (body.get("records") or []) if isinstance(r, dict)]
    adds = [a for a in (body.get("ledger") or []) if isinstance(a, dict)]
    log: list[str] = []
    n_rec = n_led = 0
    if recs:
        with store.connection() as conn:
            n_rec, _ = curate_apply.apply_records(conn, project, recs, log, origin=origin)
            conn.commit()
    # THE GRAPH LINES (2026-09-26, the switch): the lines call's decisions
    # land as decision lines under the threads view the box sent up with the
    # window — the hub hands both back, plus the window's per-exchange turn
    # ranges so each line points at its own exchange. History is never
    # healed (founder 2026-09-24).
    decs = [d for d in (body.get("decisions") or []) if isinstance(d, dict)]
    n_dec = 0
    if decs and session_id:
        with store.connection() as conn:
            try:
                from . import decisionline
                cur = conn.cursor()
                decisionline.ensure(cur)
                day = None
                if have_turns:
                    cur.execute("SELECT min(ts) FROM chat WHERE session_id = %s AND turn_index BETWEEN %s AND %s;",
                                [session_id, lo, hi])
                    w = cur.fetchone()
                    day = w[0].date() if w and w[0] else None
                threads = [t for t in (body.get("threads") or []) if isinstance(t, dict) and t.get("id")]
                n_dec = decisionline.write(
                    cur, project, decs, session_id, lo if have_turns else -1, hi if have_turns else -1, day,
                    "", threads=threads,
                    questions=[str(q) for q in (body.get("questions") or []) if str(q).strip()],
                    ranges=[r for r in (body.get("ranges") or []) if isinstance(r, list) and len(r) == 2],
                    days=[str(x) for x in (body.get("days") or [])])
                # THE RETIREMENT SWEEP (2026-10-07): a line that drops or
                # replaces a thing goes into the project's queue the moment it
                # is written; the sweep is woken below
                n_ret = 0
                if n_dec:
                    # a savepoint: a queue fault must never cost the lines
                    try:
                        from . import retire
                        with conn.transaction():
                            n_ret = retire.enqueue_session(conn, project, session_id, lo if have_turns else -1)
                    except Exception as e:                   # noqa: BLE001
                        log.append(f"RETIRE queue FAILED (lines kept): {type(e).__name__}: {e}")
                conn.commit()
                log.append(f"LINES {n_dec} of {len(decs)} written" + (f", {n_ret} to the retirement sweep" if n_ret else ""))
            except Exception as e:                           # noqa: BLE001
                conn.rollback()
                log.append(f"LINES FAILED: {type(e).__name__}: {e}")
    # THE STILL-TRUE CHECK'S OPS (2026-10-02): records of THIS session a
    # later window overtook, judged on the hub (Jev flags, the heal text
    # rewrites or deletes). Applied through heal's own applier and its
    # guards, only to this project's records of this session, and never a
    # delete of a preference — one instance met or fixed does not overturn
    # a standing way of working.
    n_over = apply_overtaken(project, session_id,
                             [o for o in (body.get("overtaken") or []) if isinstance(o, dict)], log)
    # A ROW NAMES ITS PROJECT ONLY WHEN THAT PROJECT EXISTS (2026-10-03):
    # rows filed under names that are no project ("brethof-brain", "linux")
    # sat where no session lists them — 42 of brain's rows. An unknown name
    # files under the window's project, said in the log.
    known: set[str] = {"global"}
    if adds:
        with store.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT name FROM project;")
            known |= {r[0] for r in cur.fetchall()}
            conn.rollback()
    for a in adds:
        content = " ".join(str(a.get("content") or "").split())
        if not content:
            continue
        named = str(a.get("project") or "").strip()
        if named and named not in known:
            log.append(f"LEDGER row named project '{named[:60]}', which does not exist — filed under {project}")
            named = ""
        clean = a.get("clean") if isinstance(a.get("clean"), dict) else None
        try:
            ledger.add(named or project, content[:ledger.ROW_CHARS], origin=session_id,
                       turn_lo=lo if have_turns else -1, turn_hi=hi if have_turns else -1, cleaner=clean)
            n_led += 1
        except Exception as e:                               # noqa: BLE001
            log.append(f"LEDGER row refused: {e}")
    # THE ROWS THE CLEANER KEPT OUT (2026-10-11): never filed, but kept with
    # their verdict in ledger_clean_log so they can be read and put back.
    dropped_rows = body.get("ledger_dropped_rows")
    if isinstance(dropped_rows, list) and dropped_rows:
        try:
            n = ledger.log_dropped(project, dropped_rows, origin=session_id)
            log.append(f"LEDGER cleaner: {n} new rows kept out (repeat or a step of the work), logged")
        except Exception as e:                               # noqa: BLE001
            log.append(f"LEDGER cleaner log FAILED: {type(e).__name__}: {e}")
    # THE CLEANER'S DONE CHECK (2026-10-03): open rows this window's new
    # history lines and records show done, dropped or overtaken. Report mode
    # marks them; on, they leave the ledger — confined to this project.
    closed = [c for c in (body.get("ledger_closed") or []) if isinstance(c, dict) and str(c.get("id") or "").isdigit()]
    if closed:
        try:
            if body.get("ledger_mode") == "on":
                # copied to ledger_clean_log with its verdict, then deleted
                removed, missing = ledger.close_rows(project, {int(c["id"]): {"why": "done", "p": c.get("p")}
                                                               for c in closed})
                log.append(f"LEDGER cleaner: {len(removed)} rows done or overtaken removed"
                           + (f", {len(missing)} already gone" if missing else ""))
            else:
                n = ledger.mark(project, {int(c["id"]): {"why": "done", "p": c.get("p")} for c in closed})
                log.append(f"LEDGER cleaner (report): {n} rows marked done or overtaken")
        except Exception as e:                               # noqa: BLE001
            log.append(f"LEDGER cleaner FAILED: {type(e).__name__}: {e}")
    for line in log:
        print(f"[pipe] {project}: {line}", flush=True)
    # HEAL STARTS WHERE THE RECORDS LAND (founder 2026-09-01: "the heal
    # will start straight away when the new curated records are saved").
    # Here the new records are already in the table — this is the ground
    # fast heal is meant to read. Gated on n_rec: a save that carried only
    # lines or ledger rows has nothing new for heal to seed from. The
    # judgement is the hub's (design step 3): the box asks up the pipe.
    if n_rec:
        try:
            from . import reconcile
            reconcile.trigger(tenant, project)
        except Exception:                                    # noqa: BLE001
            traceback.print_exc()
    if n_dec:
        try:
            from . import retire
            retire.trigger(tenant, project)
        except Exception:                                    # noqa: BLE001
            traceback.print_exc()
    return {"records": n_rec, "ledger": n_led, "lines": n_dec, "overtaken": n_over}


# The hub's name for the curator's writes since 2026-08-30; kept as the same
# handler so a hub of either age is answered.
HANDLERS["save"] = write_saves

VOCABULARY = frozenset(HANDLERS)


def apply_overtaken(project: str, session_id: str, over: list, log: list) -> int:
    """THE STILL-TRUE CHECK'S OPS (2026-10-02): records of THIS session a
    later window overtook, judged on the hub (Jev flags, the heal text
    rewrites or deletes). Applied through heal's own applier and its guards,
    only to this project's records of this session, and never a delete of a
    preference — one instance met or fixed does not overturn a standing way
    of working. The live save and the records rebuild both call this."""
    n_over = 0
    if over and session_id:
        try:
            from . import heal
            with store.connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT record_id, coalesce(type,''), coalesce(content,'') FROM document "
                            "WHERE project = %s AND meta->'origin'->>'session_id' = %s AND record_id = ANY(%s);",
                            [project, session_id, [str(o.get("id") or "") for o in over]])
                mine = {r: (k, c) for r, k, c in cur.fetchall()}
                conn.rollback()
            ops = []
            for o in over:
                oid = str(o.get("id") or "")
                if oid not in mine:
                    log.append(f"OVERTAKEN refused: {oid} is not this session's record")
                elif o.get("op") == "delete" and mine[oid][0] == "preference":
                    log.append(f"OVERTAKEN refused: {oid} is a preference — never deleted for one instance")
                else:
                    ops.append({**o, "type": o.get("type") or mine[oid][0], "into": ""})
            applied, _ = heal._apply(ops, project, set(mine), False, log,
                                     current={k: v[1] for k, v in mine.items()}, readable=set(mine))
            n_over = applied
        except Exception as e:                               # noqa: BLE001
            log.append(f"OVERTAKEN FAILED: {type(e).__name__}: {e}")
    return n_over
