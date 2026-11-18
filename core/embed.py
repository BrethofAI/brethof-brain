"""Embedder-sidecar client. Same model and same contract as v1.

DEGRADE, NEVER FAIL: every path returns None on failure and the caller treats
that as "vector search unavailable", not as an error. Recall then runs on the
keyword stream alone, which is worse but still useful — and a session that
loses vectors for one query must never see a 500.

The one thing this does NOT do is embed on a write path. v1 embedded inline
during archive, swallowed sidecar timeouts, and silently lost embeddings for
months. Here writes land with `embedding IS NULL` and the drainer fills them;
this client is for QUERIES only.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

EMBEDDER_URL = os.environ.get("BRAIN_EMBEDDER_URL", "http://embedder:8202")
# The embedder's bearer token, when it has one (embedder/server.py EMBED_TOKEN).
EMBEDDER_TOKEN = os.environ.get("BRAIN_EMBEDDER_TOKEN", "")
# Matches the sidecar's MAX_CHARS; the tokenizer truncates at gemma's real
# window (2,048 tokens ≈ 7-8K chars of English) anyway. The old 4,000 was a
# v1 MiniLM-era constant that survived the model swap — it silently blinded
# vector search to the tail of ~3% of records and ~1.5% of chat turns
# (measured on the founder tenant 2026-08-12) while using HALF the model's
# window. Anything beyond the model window is still covered by the keyword
# stream (tsvector has no cap). This is THE one cap — other modules import
# it rather than carrying their own copy.
CONTENT_CAP = 8000


def _who() -> dict:
    """tenant + plan weight for the embedder's ranked door (2026-09-03). On a
    single-tenant local box this is decoration; on mind-cf's shared embedder
    it is the order of service. Never raises: an unbound context sends
    nothing and ranks as the anonymous tenant at weight 1.0."""
    try:
        from . import ranking, store
        t = store.current_tenant()
        return {"tenant": t, "weight": ranking.plan_weight(t)}
    except Exception:                                     # noqa: BLE001
        return {}


def _call_sidecar(texts: list[str], timeout: float, kind: str,
                  interactive: bool) -> list[list[float]] | None:
    body = {"texts": [t[:CONTENT_CAP] for t in texts], "kind": kind,
            **_who()}
    if interactive:
        body["interactive"] = True
    payload = json.dumps(body).encode()
    req = urllib.request.Request(EMBEDDER_URL.rstrip("/") + "/embed",
                                 data=payload,
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {EMBEDDER_TOKEN}"}
                                             if EMBEDDER_TOKEN else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r).get("vectors")
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"[embed] {type(e).__name__}: {str(e)[:160]}", flush=True)
        return None


def embed(texts: list[str], timeout: float = 8.0,
          kind: str = "query", interactive: bool = False) -> list[list[float]] | None:
    """kind selects the sidecar's prompt: 'query' for anything used to SEARCH,
    'document' for anything being STORED (gemma is prompt-asymmetric — a doc
    embedded as a query lands in the wrong region of the space). The prompt
    strings themselves live in the sidecar only.
    `interactive` claims query-class ADMISSION for a document embed — an
    agent is waiting on it (the save door's near-dup check). The drainer
    never sets it; queries have it implicitly.

    Query-class calls ride the RANKED LANE (lane.py): tenant-ordered with a
    deadline, so a flood of one account's retrievals thins that account's
    recall — never the box. Background document batches bypass it."""
    if not texts:
        return []
    if kind == "query" or interactive:
        from . import lane
        return lane.ranked(texts, timeout, kind, interactive)
    return _call_sidecar(texts, timeout, kind, interactive)


# Overlap between adjacent windows of a long text: a fact sitting on a cut
# line is whole in at least one window.
WINDOW_OVERLAP = 800


def windows(text: str) -> list[str]:
    """Model-window slices for UNLIMITED-length embedding (founder
    2026-08-12: "the length shouldn't be limited at all — split it and do
    it separately"). Each window is <= CONTENT_CAP chars, cut at a word
    boundary, overlapping the previous by WINDOW_OVERLAP. A text that fits
    returns itself alone — the common case costs nothing."""
    t = text or ""
    if len(t) <= CONTENT_CAP:
        return [t]
    out, start = [], 0
    while start < len(t):
        end = min(start + CONTENT_CAP, len(t))
        if end < len(t):
            sp = t.rfind(" ", start + CONTENT_CAP // 2, end)
            if sp > start:
                end = sp
        out.append(t[start:end])
        if end >= len(t):
            break
        start = max(end - WINDOW_OVERLAP, start + 1)
    return out


def embed_batched(texts: list[str], timeout: float = 30.0,
                  kind: str = "document",
                  call_chars: int = 24_000) -> list[list[float]] | None:
    """embed() in size-bounded sub-calls, so callers can hand over ANY
    number of windows without a single request outgrowing the sidecar's
    timeout. All-or-nothing like embed(): any failed sub-call returns None
    and the caller leaves its rows queued."""
    out: list[list[float]] = []
    batch: list[str] = []
    spent = 0
    for t in texts + [None]:                       # None flushes the tail
        if t is not None and (not batch
                              or spent + len(t) <= call_chars):
            batch.append(t)
            spent += len(t)
            continue
        if batch:
            vecs = embed(batch, timeout=timeout, kind=kind)
            if vecs is None or len(vecs) != len(batch):
                return None
            out.extend(vecs)
        if t is not None:
            batch, spent = [t], len(t)
    return out


def embed_one(text: str, kind: str = "query", timeout: float = 8.0,
              interactive: bool = False) -> list[float] | None:
    # Deliberate MCP searches pass a LONGER timeout (the agent is waiting and
    # keyword fallback is the worse outcome); hook paths keep the 8 s default
    # because the hook itself dies at 12 s and fail-open there loses the
    # whole injection, not just the vector stream (founder call 2026-08-11).
    v = embed([text], timeout=timeout, kind=kind, interactive=interactive)
    return v[0] if v else None
