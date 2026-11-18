"""The BRAIN LEDGER — the pile of undecided intentions, one per project.

THE PROBLEM IT KILLS (founder, 2026-08-22): the leak between talking and
doing. Ideas and commitments voiced in a session evaporate at compact or
shutdown; weeks later it's "oh fuck, that was never done". The ledger's
extractor rides the per-turn curation worker and files every intention it
hears, so a whole day of brainstorming becomes rows instead of vapor.

THE DESIGN IS DELIBERATELY STATUSLESS (founder: "the status is the worst
thing ever"). A row's existence IS its state: undecided. At review the
human decides each row's fate — stays (on hold), deleted (not wanted, or
done), or promoted (an agent writes a goal from it and deletes the row;
the goal lives wherever the executing agent lives, not here). Every
decision REMOVES a row, so the pile cannot rot into a blob.

NO TOMBSTONES, ON PURPOSE: a deleted idea that still matters will be said
again in some future conversation and re-filed — re-judged against the
project as it is THEN, not as it was. Re-extraction is the safety net that
makes brutal deletion safe. For the same reason the extractor does NOT
merge similar ideas (founder: similar ideas are DIFFERENT ideas) — the
review agent clusters them at review time, where the differences between
variants are exactly what reveals the bigger scope.

DELETION IS NEVER GATED and never silent: remove_rows reports what it
removed and names any id it could not find.
"""
from __future__ import annotations

import json
import os
import traceback

from . import inference, metering, store

_DDL = """
CREATE TABLE IF NOT EXISTS ledger (
    id          bigserial PRIMARY KEY,
    project     text NOT NULL,
    content     text NOT NULL,
    origin      text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ledger_project_idx ON ledger (project, id);
ALTER TABLE ledger ADD COLUMN IF NOT EXISTS turn_lo int NOT NULL DEFAULT -1;
ALTER TABLE ledger ADD COLUMN IF NOT EXISTS turn_hi int NOT NULL DEFAULT -1;
ALTER TABLE ledger ADD COLUMN IF NOT EXISTS embedding halfvec(768);
ALTER TABLE ledger ADD COLUMN IF NOT EXISTS cleaner jsonb;
-- WHAT THE CLEANER TOOK, KEPT (2026-10-11): every row the cleaner closed and
-- every new row it kept out, with its verdict, written before the row goes —
-- the retirement sweep's retire_log, for the ledger. Until it existed the
-- cleaner's 446 removals on the founder's memory could be neither read nor
-- put back (the closed rows were hard-deleted, the dropped ones never left
-- the hub). Put one back: INSERT INTO ledger (project, content, origin)
-- SELECT project, content, origin FROM ledger_clean_log WHERE id = …
CREATE TABLE IF NOT EXISTS ledger_clean_log (
    id          bigserial PRIMARY KEY,
    project     text NOT NULL,
    kind        text NOT NULL,             -- 'closed' (was open) | 'dropped' (never filed)
    row_id      bigint,                    -- the ledger id of a closed row
    content     text NOT NULL,
    origin      text NOT NULL DEFAULT '',
    verdict     jsonb NOT NULL DEFAULT '{}'::jsonb,
    at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ledger_clean_log_idx ON ledger_clean_log (project, at);
"""

# Per-row and per-call bounds. Never silent: clipping is reported in the
# extractor log line, and add() refuses instead of truncating.
ROW_CHARS = 1_000
MAX_ADDS_PER_CALL = 8
PILE_SHOW_ROWS = 80
TURNS_CHARS = 40_000
BRIEF_CHARS = 2_000


def _ensure(cur) -> None:
    store.ensure_ddl(cur, "ledger", _DDL)


# ── plain CRUD (no scope checks here — tiers wrap these) ───────────────────

def add(project: str, content: str, origin: str = "", turn_lo: int = -1,
        turn_hi: int = -1, cleaner: dict | None = None) -> int:
    """origin is the session the row was caught in; turn_lo/turn_hi the
    piece's turns in that session (2026-09-09), -1 when unknown. `cleaner`
    is the hub's report-mode verdict on the row (see THE CLEANER below)."""
    content = " ".join((content or "").split())
    if not content:
        raise ValueError("empty ledger row")
    if len(content) > ROW_CHARS:
        raise ValueError(f"ledger row over {ROW_CHARS} chars — a row is an "
                         f"intention, not a document; put detail in memory "
                         f"and reference it")
    with store.connection() as conn:
        cur = conn.cursor()
        _ensure(cur)
        cur.execute("INSERT INTO ledger (project, content, origin, turn_lo, turn_hi, cleaner) "
                    "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id;",
                    [project, content, origin[:120], int(turn_lo), int(turn_hi),
                     json.dumps(cleaner) if cleaner else None])
        rid = cur.fetchone()[0]
        conn.commit()
    return rid


def pile(project: str, limit: int = 500) -> list[tuple]:
    """(id, content, origin, created_at) oldest first — review walks the
    pile in the order ideas arrived."""
    with store.connection() as conn:
        cur = conn.cursor()
        _ensure(cur)
        cur.execute("SELECT id, content, origin, created_at FROM ledger "
                    "WHERE project = %s ORDER BY id LIMIT %s;",
                    [project, limit])
        return cur.fetchall()


def remove_rows(project: str, ids: list[int]) -> tuple[list[int], list[int]]:
    """Delete rows by id, confined to `project`. Returns (removed, missing)
    so a wrong id is reported, never silently dropped."""
    if not ids:
        return [], []
    with store.connection() as conn:
        cur = conn.cursor()
        _ensure(cur)
        cur.execute("DELETE FROM ledger WHERE project = %s AND id = ANY(%s) "
                    "RETURNING id;", [project, ids])
        removed = sorted(r[0] for r in cur.fetchall())
        conn.commit()
    missing = sorted(set(ids) - set(removed))
    return removed, missing


def close_rows(project: str, closed: dict[int, dict]) -> tuple[list[int], list[int]]:
    """The cleaner's done check, on: each row is copied to ledger_clean_log
    with its verdict, then deleted — one transaction, confined to `project`.
    Returns (removed, missing) like remove_rows."""
    if not closed:
        return [], []
    with store.connection() as conn:
        cur = conn.cursor()
        _ensure(cur)
        ids = sorted(closed)
        cur.execute("SELECT id, content, origin FROM ledger WHERE project = %s AND id = ANY(%s);",
                    [project, ids])
        for rid, content, origin in cur.fetchall():
            cur.execute("INSERT INTO ledger_clean_log (project, kind, row_id, content, origin, verdict) "
                        "VALUES (%s, 'closed', %s, %s, %s, %s);",
                        [project, rid, content, origin or "", json.dumps(closed[rid] or {})])
        cur.execute("DELETE FROM ledger WHERE project = %s AND id = ANY(%s) RETURNING id;", [project, ids])
        removed = sorted(r[0] for r in cur.fetchall())
        conn.commit()
    return removed, sorted(set(closed) - set(removed))


def log_dropped(project: str, rows: list[dict], origin: str = "") -> int:
    """New rows the cleaner kept out of the ledger (a repeat, a step of the
    work in hand), kept with their verdict — they were never filed."""
    keep = [r for r in rows if isinstance(r, dict) and str(r.get("content") or "").strip()]
    if not keep:
        return 0
    with store.connection() as conn:
        cur = conn.cursor()
        _ensure(cur)
        for r in keep:
            v = {k: r[k] for k in ("why", "p", "of") if k in r}
            cur.execute("INSERT INTO ledger_clean_log (project, kind, content, origin, verdict) "
                        "VALUES (%s, 'dropped', %s, %s, %s);",
                        [project, " ".join(str(r["content"]).split())[:ROW_CHARS], origin or "", json.dumps(v)])
        conn.commit()
    return len(keep)


# ── THE CLEANER (founder 2026-10-03: "it's really worth to build it") ──────
# The hub's Jev takes out only the obvious so the agent walking the ledger
# reads less; every other call stays the agent's. Two moments:
#   a new row — is it the same thing to do as one of its 3 nearest open rows
#     (repeat, cut 0.5), or a step of the work in hand (cut 0.6)? Then it is
#     not written;
#   a window — do its new history lines and records show any of the 20 open
#     rows nearest them done, dropped or overtaken (cut 0.8)? Then it goes.
# Measured on brain's 362 rows of 2026-10-03 (scripts/ops/ledger_jev_bench.py,
# bench/ledger_jev): 362 -> 167 on arrival, -> 83 over 1,005 replayed windows,
# no open item lost. REPORT MODE (the hub's HUB_LEDGER_CLEAN=report) writes
# the verdict into `cleaner` and deletes nothing; list_ledger shows it.
# The box only finds the neighbours and applies; the judging is the hub's.

NEAR_ROWS, NEAR_OPEN, EMBED_FILL = 3, 20, 200


def near(cur, project: str, rows: list[str], items: list[str],
         k_rows: int = NEAR_ROWS, k_open: int = NEAR_OPEN) -> dict:
    """{"rows": [[{id, content}] nearest each new row], "open": [{id,
    content}] the k_open open rows nearest any item, best first}. Rows with
    no vector yet are embedded first (at most EMBED_FILL a call, a log line
    when more wait); the embedder down -> empty lists, the window is saved
    as before. A row the cleaner already marked (report mode) counts as gone,
    as it would be with the cleaner on."""
    from . import embed
    from .decisionline import _vec
    _ensure(cur)
    cur.execute("SELECT id, content FROM ledger WHERE project = %s AND embedding IS NULL ORDER BY id LIMIT %s;",
                [project, EMBED_FILL + 1])
    todo = cur.fetchall()
    if len(todo) > EMBED_FILL:
        print(f"[ledger] {project}: more than {EMBED_FILL} rows wait for a vector — the rest next window", flush=True)
        todo = todo[:EMBED_FILL]
    if todo:
        vs = embed.embed_batched([c for _, c in todo], kind="document") or []
        for (rid, _), v in zip(todo, vs):
            if v:
                cur.execute("UPDATE ledger SET embedding = %s::halfvec WHERE id = %s;", [_vec(v), rid])
    qs = [q[:2000] for q in rows] + [q[:2000] for q in items]
    vecs = embed.embed_batched(qs, kind="document") if qs else []
    if not vecs or len(vecs) != len(qs):
        return {"rows": [[] for _ in rows], "open": []}

    def nearest(v, k):
        cur.execute("SELECT id, content, embedding <=> %s::halfvec AS d FROM ledger "
                    "WHERE project = %s AND embedding IS NOT NULL AND cleaner IS NULL ORDER BY d LIMIT %s;", [_vec(v), project, k])
        return cur.fetchall()

    out_rows = [[{"id": i, "content": c} for i, c, _ in nearest(v, k_rows)] if v else [] for v in vecs[:len(rows)]]
    best: dict[int, tuple[float, str]] = {}
    for v in vecs[len(rows):]:
        for i, c, d in (nearest(v, k_open) if v else []):
            if i not in best or d < best[i][0]:
                best[i] = (float(d), c)
    top = sorted(best.items(), key=lambda x: x[1][0])[:k_open]
    return {"rows": out_rows, "open": [{"id": i, "content": c} for i, (_, c) in top]}


def mark(project: str, marks: dict[int, dict]) -> int:
    """Report mode: the cleaner's verdict on rows already filed."""
    if not marks:
        return 0
    with store.connection() as conn:
        cur = conn.cursor()
        _ensure(cur)
        n = 0
        for rid, m in marks.items():
            cur.execute("UPDATE ledger SET cleaner = %s WHERE project = %s AND id = %s;",
                        [json.dumps(m), project, int(rid)])
            n += cur.rowcount
        conn.commit()
    return n


def marks(project: str) -> dict[int, dict]:
    """{id: verdict} of the rows the cleaner marked (report mode)."""
    with store.connection() as conn:
        cur = conn.cursor()
        _ensure(cur)
        cur.execute("SELECT id, cleaner FROM ledger WHERE project = %s AND cleaner IS NOT NULL;", [project])
        return {int(i): (c if isinstance(c, dict) else json.loads(c)) for i, c in cur.fetchall()}


def mark_line(m: dict | None) -> str:
    """How list_ledger shows a verdict: ' ⟨cleaner: … p⟩'."""
    if not m:
        return ""
    why = {"repeat": f"repeat of [{m.get('of')}]", "step": "a step of the work in hand",
           "done": "done or overtaken"}.get(str(m.get("why")), str(m.get("why")))
    return f"  ⟨cleaner would drop: {why}, p {float(m.get('p') or 0):.2f}⟩"


# ── the extractor lane ─────────────────────────────────────────────────────

def enabled_for(tenant: str) -> bool:
    """BRAIN_LEDGER = 'all' | comma-separated tenant ids | '' (off) —
    same rollout dial shape as BRAIN_LIVE_CURATE: our tenant first,
    everyone once proven. BRAIN_LEDGER_EXCLUDE opts tenants out."""
    excl = (os.environ.get("BRAIN_LEDGER_EXCLUDE") or "").strip()
    if excl and tenant in {t.strip() for t in excl.split(",")}:
        return False
    v = (os.environ.get("BRAIN_LEDGER") or "").strip()
    if not v:
        return False
    return v == "all" or tenant in {t.strip() for t in v.split(",")}


# LICENSED TEXT — lives in core/prompts.py on our cloud and is
# fetched from the hub on a customer's box, where that file is
# not shipped. Resolved at the point of use, never at import:
# a hub blip at container start must not strand the process.
# The box-side extractor (extract_for_job, its prompt and its pile brief) is
# gone (2026-09-02): the ledger is extracted by the CURATOR on the hub, in the
# same call as records and entities, under its own bar. This module keeps
# the store: add / pile / remove_rows / enabled_for.
