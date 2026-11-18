#!/usr/bin/env python3
"""brethof-brain cloud embedder sidecar — EmbeddingGemma-300m served from OUR
OWN fp32 ONNX export behind the same trivial loopback HTTP API as before
(GET /health, POST /embed {"texts": [...], "kind": "query"|"document"}).

WHY THIS EXACT ARTIFACT (all measured, 2026-08-10/11, founder corpus + the 46
gate queries — do not re-litigate):
  - EmbeddingGemma-300m beat everything incl. Qwen3-8B on MRR (.918) with
    r@1 .870 / r@5 .957 / r@10 .978; 100+ languages.
  - The ONNX file here is OUR export from founder-verified original weights,
    cosine 1.0000 vs the torch truth. Community ONNX conversions are BANNED
    (onnx-community export measured cosine -0.05 vs truth — orthogonal junk).
  - int8 quantization is BANNED: cosine 0.9839 looked benign but r@1
    collapsed .870 -> .609. Only fp32 ships.

MODULE CHAIN (mirrors the SentenceTransformer modules.json of the export):
  transformer ONNX -> mean pool (mask-weighted, prompt tokens INCLUDED)
  -> Dense 768->3072 (no bias, identity) -> Dense 3072->768 (no bias,
  identity) -> L2 normalize.
The WHOLE chain is composed into onnx/model_full.onnx by
scripts/build_sidecar_graph.py — one sess.run, no numpy tail. Measured why:
the VPS's numpy has no optimized BLAS (dense tail cost 26 ms/query there) and
the gap between ORT calls let its thread pool go cold (51 -> 99 ms).

PROMPTS live HERE and nowhere else, selected by the request's "kind":
  query    -> "task: search result | query: "   (recall / search paths)
  document -> "title: none | text: "            (drainer / write paths)
A row embedded as a document is only findable by a query embedded as a query —
callers pass kind, they never paste prefixes themselves.

TOKENIZER: the raw `tokenizers` runtime reads tokenizer.json directly, i.e.
byte-identical behavior to the transformers fast tokenizer the eval scored
(incl. <bos>/<eos> via the baked-in post-processor). The transformers
"fix_mistral_regex" warning is a flag on a library we do not load — moot here.

Runs on the internal compose network only, CPU-quota'd. Every consumer treats
it as OPTIONAL — down -> callers degrade (BM25-only), never error.
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

MODEL_DIR = os.environ.get("EMBED_MODEL_DIR", "/model")
PORT = int(os.environ.get("EMBED_PORT", "8202"))
MAX_TEXTS = 256
MAX_CHARS = 8000
MAX_TOKENS = 2048          # model max_position_embeddings
# Inference runs one micro-batch at a time under a lock: a 64-row drainer
# batch becomes 8 lock windows, so a 1-text query slips in between them
# instead of waiting ~13 s behind the whole batch (VPS, 3 cores).
MICRO_BATCH = int(os.environ.get("EMBED_MICRO_BATCH", "8"))
# LOCK-HOLD IS TOKENS, NOT TEXTS (2026-08-11, Aurora stress test): a count-
# based micro-batch of 8 SHORT records holds the lock ~0.5 s, but 8 full
# 2048-token chat rows hold it ~10 s — and a live query waits behind whatever
# batch is RUNNING, priority or not. Measured live: query latency 0.9-3.8 s
# under drain (vs 65 ms idle). So background micro-batches are bounded by a
# TOKEN budget too: one max-length row still rides alone (~1.4 s, the floor
# we cannot preempt), but it never gets seven siblings. Queries skip the
# budget — they are the thing being protected, and real query text is short.
MICRO_TOKEN_BUDGET = int(os.environ.get("EMBED_MICRO_TOKEN_BUDGET", "3000"))
# (Load shedding by 503 lived here until the 2026-08-12 slot rewrite. It was
# a treatment for unbounded waits behind the old global lock, and its own
# retry traffic fed the livelock it was meant to prevent. Admission is now
# the slot pool below: interactive always has a reserved slot, background
# waits for one instead of being rejected.)

PROMPTS = {
    "query": "task: search result | query: ",
    "document": "title: none | text: ",
}

print(f"[embedder] loading gemma ONNX from {MODEL_DIR} ...", flush=True)
import onnxruntime as ort              # noqa: E402
from tokenizers import Tokenizer       # noqa: E402

_tok = Tokenizer.from_file(os.path.join(MODEL_DIR, "tokenizer.json"))
_tok.enable_truncation(max_length=MAX_TOKENS)
_tok.no_padding()                      # we pad ourselves, mask-aware

_opts = ort.SessionOptions()
_threads = int(os.environ.get("EMBED_THREADS", "0"))
if _threads:
    _opts.intra_op_num_threads = _threads
# NO SPIN-WAIT (2026-08-11): ORT's thread pool busy-spins between requests.
# With traffic arriving every few seconds the pool never sleeps — docker
# stats showed 300% CPU SUSTAINED while the actual work was a fraction of
# that, and the spinning competes with the request threads on a 3-core
# quota. Cost of disabling: ~a millisecond of wake-up per call — noise
# against a 65 ms inference. (The cold-pool concern that motivated the
# composed graph was about the 26 ms numpy tail, which no longer exists.)
if os.environ.get("EMBED_ALLOW_SPIN", "0") != "1":
    _opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
_sess = ort.InferenceSession(
    os.path.join(MODEL_DIR, "onnx", "model_full.onnx"),
    sess_options=_opts, providers=["CPUExecutionProvider"])
# ── SLOTS, NOT A LOCK (2026-08-12 rewrite, founder-specified) ──────────────
# WHAT WAS HERE: one global _infer_lock serialising every inference, plus a
# "query priority" condition variable that let background work yield between
# micro-batches. It did not work. You cannot preempt an inference that has
# already started, so a live search still waited out whatever batch was in
# flight — measured up to 5.5 s for a full window, and on 2026-08-12 live
# query embeds timed out for hours while the drainer held the lock.
#
# THE FIX IS TO STOP SERIALISING. ORT's Run() is thread-safe, so N requests
# can execute concurrently on one session and the OS shares the cores
# dynamically — nothing is statically partitioned, nothing idles while work
# waits. A live query no longer waits for a LOCK; it starts immediately and
# merely shares CPU, which is the difference between ~200 ms and ~5.5 s.
#
# SLOTS (founder): three total, ONE RESERVED FOR INTERACTIVE WORK that
# background may never occupy. The reservation is a FLOOR, NOT A CAP —
# interactive may use ANY free slot, and when a slot frees, interactive
# waiters are always served before background ones. Background is capped so
# it can never take the last slot.
MAX_SLOTS = int(os.environ.get("EMBED_MAX_SLOTS", "3"))
RESERVED_INTERACTIVE = int(os.environ.get("EMBED_RESERVED_INTERACTIVE", "1"))

_slots = threading.Condition(threading.Lock())
_busy_interactive = 0
_busy_background = 0
_waiting_interactive = 0
# THE RANKED DOOR (founder 2026-09-03, for the SHARED embedder on mind-cf):
# "penalise the ones with too many things in the queue — they go to the
# back; the ones with less go to the front; the more they paid, the higher
# the priority; queries first." Waiters sit in a heap ordered by class
# (interactive first), then a score = plan weight / (1 + own backlog /
# softener), then arrival. The caller sends `tenant` and `weight` with the
# request; a box that omits them ranks as weight 1.0 under one anonymous
# tenant, which is what a single-tenant local box is anyway. The container
# that sets the weight is ours on the hosted box, so the field is trusted.
BACKLOG_SOFTENER = float(os.environ.get("EMBED_BACKLOG_SOFTENER", "8"))
_waiters: list = []                 # heap of (-class, -score, seq, id)
_seq = 0
_backlog: dict = {}                 # tenant -> requests waiting or running


def _score(weight: float, tenant: str) -> float:
    return float(weight) / (1.0 + max(0, _backlog.get(tenant, 0) - 1) / BACKLOG_SOFTENER)


def _acquire_slot(interactive: bool, timeout: float, tenant: str = "",
                  weight: float = 1.0) -> bool:
    """Take a slot. Interactive may use any of MAX_SLOTS; background may only
    use MAX_SLOTS - RESERVED_INTERACTIVE. Among waiters the heap decides who
    gets a freed slot. Returns False on timeout."""
    global _busy_interactive, _busy_background, _waiting_interactive, _seq
    import heapq
    deadline = time.monotonic() + timeout
    with _slots:
        _backlog[tenant] = _backlog.get(tenant, 0) + 1
        _seq += 1
        me = (-(1 if interactive else 0), -_score(weight, tenant), _seq, id(threading.current_thread()))
        heapq.heappush(_waiters, me)
        if interactive:
            _waiting_interactive += 1
        try:
            while True:
                busy = _busy_interactive + _busy_background
                first = _waiters[0] is me
                if interactive:
                    if busy < MAX_SLOTS and first:
                        _busy_interactive += 1
                        heapq.heappop(_waiters)
                        return True
                else:
                    if (_busy_background < MAX_SLOTS - RESERVED_INTERACTIVE
                            and busy < MAX_SLOTS
                            and _waiting_interactive == 0 and first):
                        _busy_background += 1
                        heapq.heappop(_waiters)
                        return True
                left = deadline - time.monotonic()
                if left <= 0:
                    _waiters.remove(me); heapq.heapify(_waiters)
                    _backlog[tenant] = max(0, _backlog.get(tenant, 1) - 1)
                    _slots.notify_all()
                    return False
                _slots.wait(left)
        finally:
            if interactive:
                _waiting_interactive -= 1


def _release_slot(interactive: bool, tenant: str = "") -> None:
    global _busy_interactive, _busy_background
    with _slots:
        if interactive:
            _busy_interactive -= 1
        else:
            _busy_background -= 1
        _backlog[tenant] = max(0, _backlog.get(tenant, 1) - 1)
        if not _backlog[tenant]:
            _backlog.pop(tenant, None)
        _slots.notify_all()


def slot_state() -> dict:
    """Working/idle accounting — the worker state the founder asked to see."""
    with _slots:
        return {"interactive": _busy_interactive,
                "background": _busy_background,
                "max": MAX_SLOTS,
                "reserved_interactive": RESERVED_INTERACTIVE,
                "waiting_interactive": _waiting_interactive,
                "waiting": len(_waiters),
                "backlog": dict(_backlog)}


def _embed_micro(texts: list[str]) -> np.ndarray:
    encs = _tok.encode_batch(texts)
    n = len(encs)
    width = max(len(e.ids) for e in encs)
    ids = np.zeros((n, width), dtype=np.int64)      # pad_token_id = 0
    mask = np.zeros((n, width), dtype=np.int64)
    for i, e in enumerate(encs):
        ids[i, :len(e.ids)] = e.ids
        mask[i, :len(e.ids)] = 1
    (vecs,) = _sess.run(["sentence_embedding"],
                        {"input_ids": ids, "attention_mask": mask})
    return vecs


def _doc_chunks(prefixed: list[str]) -> list[list[str]]:
    """Split background work into micro-batches bounded by BOTH count and
    token budget. With the lock gone this no longer exists to release a lock
    promptly — it exists so a background job occupies its SLOT in short
    units, so the slot turns over quickly for the next queue job. Batch
    composition never changes the vectors — the pooling is mask-weighted, so
    padding companions contribute zero."""
    chunks: list[list[str]] = []
    cur: list[str] = []
    cur_tokens = 0
    for t in prefixed:
        n = len(_tok.encode(t).ids)
        if cur and (len(cur) >= MICRO_BATCH
                    or cur_tokens + n > MICRO_TOKEN_BUDGET):
            chunks.append(cur)
            cur, cur_tokens = [], 0
        cur.append(t)
        cur_tokens += n
    if cur:
        chunks.append(cur)
    return chunks


def embed(texts: list[str], kind: str, interactive: bool = True,
          timeout: float = 300.0, tenant: str = "", weight: float = 1.0) -> np.ndarray:
    """Embed under the slot model. `interactive` decides WHICH slots are
    reachable, not which prompt is used — a customer's save embeds with the
    document prompt but is interactive (an agent is waiting on the vector)."""
    prefix = PROMPTS[kind]
    prefixed = [prefix + t for t in texts]
    out = []
    batches = ([prefixed[i:i + MICRO_BATCH]
                for i in range(0, len(prefixed), MICRO_BATCH)]
               if interactive else _doc_chunks(prefixed))
    for batch in batches:
        # PER-BATCH SLOT: background releases between micro-batches, so a
        # freed slot reaches a waiting interactive request within one short
        # unit instead of at the end of a whole job.
        if not _acquire_slot(interactive, timeout, tenant, weight):
            raise TimeoutError("no embedder slot available")
        try:
            out.append(_embed_micro(batch))
        finally:
            _release_slot(interactive, tenant)
    return np.concatenate(out, axis=0)


_dim = embed(["warmup"], "query").shape[1]
print(f"[embedder] ready — embeddinggemma-300m own fp32 ONNX, dim {_dim}, "
      f"port {PORT}", flush=True)


# THE KEY (2026-11-15). The embedder served only callers on its own bridge
# network and had no auth; the 2026-08-27 decision says auth comes BEFORE
# anything reaches it from another box (first one: the Almanac on prod).
# EMBED_TOKEN set = POST /embed needs "Authorization: Bearer <token>";
# unset = open, as every local install is today. /health stays open: it
# carries no text and the fleet probes it.
# EMBED_AUTH_PORT set too = the token gates ONLY that second listener, the
# door an off-box route points at; the main port stays as it was for the
# boxes on the embedder's own bridge network (never published).
EMBED_TOKEN = os.environ.get("EMBED_TOKEN", "")
AUTH_PORT = int(os.environ.get("EMBED_AUTH_PORT", "0") or 0)
if EMBED_TOKEN:
    print("[embedder] EMBED_TOKEN set — /embed requires the bearer token"
          + (f" on port {AUTH_PORT} only" if AUTH_PORT else ""), flush=True)


def authorized(header: str, port: int = 0) -> bool:
    if not EMBED_TOKEN or (AUTH_PORT and port != AUTH_PORT):
        return True
    import hmac
    return hmac.compare_digest(header or "", f"Bearer {EMBED_TOKEN}")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            self._send(200, {"status": "ok",
                             "model": "embeddinggemma-300m-onnx-fp32",
                             "dim": _dim, "kinds": sorted(PROMPTS),
                             "slots": slot_state()})
        else:
            self._send(404, {"error": "unknown path"})

    def do_POST(self):  # noqa: N802
        if self.path != "/embed":
            self._send(404, {"error": "unknown path"})
            return
        if not authorized(self.headers.get("Authorization", ""), self.server.server_port):
            self._send(401, {"error": "bearer token required"})
            return
        # Read and validate BEFORE shedding: the cap is kind-aware, so we must
        # know whether this is an interactive query or background document
        # work first. Parsing is microseconds; the model call is the cost.
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length))
        except Exception:                                     # noqa: BLE001
            self._send(400, {"error": "body must be JSON"})
            return
        texts = data.get("texts")
        kind = data.get("kind", "query")
        if not isinstance(texts, list) or not texts:
            self._send(400, {"error": "'texts' must be a non-empty list"})
            return
        if kind not in PROMPTS:
            self._send(400, {"error": f"'kind' must be one of {sorted(PROMPTS)}"})
            return
        # ADMISSION ≠ PROMPT (2026-08-12, gate caught it live): a customer's
        # SAVE embeds as a document (prompt asymmetry) but is interactive —
        # an agent is waiting and the near-dup merge check needs the vector
        # NOW. Callers may set "interactive": true to claim the interactive
        # slot class while keeping the document prompt; the drainer never
        # sets it.
        interactive = bool(data.get("interactive", kind == "query"))
        # THE RANKED DOOR reads who is asking and what they weigh (see
        # _acquire_slot). Absent = one anonymous tenant at weight 1.0.
        tenant = str(data.get("tenant") or "")[:64]
        try:
            weight = max(0.01, min(100.0, float(data.get("weight", 1.0))))
        except (TypeError, ValueError):
            weight = 1.0
        # NO 503 SHEDDING ANY MORE (2026-08-12 rewrite). Shedding existed
        # because a full pipe meant an unbounded wait behind the old global
        # lock. With slots, interactive work has a reserved slot it can
        # always reach, and background work simply WAITS for a slot instead
        # of being rejected and retried — the retry storm was itself a cause
        # of the 2026-08-12 livelock. Background waits with a bounded
        # timeout; a timeout leaves the row queued, which is safe because
        # pending is `embedding IS NULL` in the database.
        try:
            texts = [str(t)[:MAX_CHARS] for t in texts[:MAX_TEXTS]]
            vectors = [[float(x) for x in v]
                       for v in embed(texts, kind, interactive=interactive,
                                      tenant=tenant, weight=weight)]
            self._send(200, {"vectors": vectors, "dim": _dim})
        except TimeoutError as e:
            self._send(503, {"error": str(e)})
        except Exception as e:                                # noqa: BLE001
            self._send(500, {"error": str(e)})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    # PID 1 IN ITS CONTAINER (2026-11-18): the kernel gives PID 1 no default
    # SIGTERM, so every stop waited out its timeout and was killed (20 s on a
    # clean stop, a reboot waiting on it). The embedder holds no state — what
    # is not embedded yet stays queued in the database — so it leaves at once.
    import signal as _sig

    def _leave(signum, _frame):
        print(f"[embedder] signal {signum} — leaving", flush=True)
        os._exit(0)
    _sig.signal(_sig.SIGTERM, _leave)
    _sig.signal(_sig.SIGINT, _leave)
    if EMBED_TOKEN and AUTH_PORT:
        import threading as _th
        _th.Thread(target=ThreadingHTTPServer(("0.0.0.0", AUTH_PORT), Handler).serve_forever,
                   daemon=True).start()
        print(f"[embedder] token door on port {AUTH_PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
