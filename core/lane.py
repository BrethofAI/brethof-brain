"""The RANKED RETRIEVAL LANE — tenant-ordered, deadline-aware query embeds.

Founder design, 2026-08-24, closing the last unranked path: ambient recall
embeds the prompt inline on the shared CPU, and "a thousand free accounts
each politely inside their budget" was the one flood no queue demoted.

THE SHAPE (his words, built literally):
- The lane is VERY SHORT AND FAST — deadlines are 8-12s and an answer
  after the deadline is worthless, so nobody ever waits long. Arithmetic
  sheds at the door instead: if (queue ahead × measured service time)
  cannot fit inside this caller's deadline, the caller gets None NOW and
  recall degrades to the keyword stream — milliseconds, not a timeout.
- Ordering is the SAME plan ranking every lane runs — with one override:
  the judge's demotion flag. "The highest plan is only 99 dollars... if
  someone wants to fuck us up, he will buy the plan." So money buys the
  default courtesy; BEHAVIOR outranks money. A demoted account's weight
  collapses to near zero: if there is anyone else, anyone else is in
  front of them. They get something on quiet moments and mostly thin out
  on busy ones — no refusal, no message, nothing to screenshot.
- The lane cannot judge anyone ("the queue is finished in boom moment")
  — it only serves in order and WRITES DOWN what happened: every embed
  and every shed lands in the per-tenant daily log, and shed streaks
  emit recall_pressure signals. The judge reads logs, never the queue.

Everything fails open: lane trouble = direct sidecar call, exactly the
degrade-never-fail contract recall has always had.
"""
from __future__ import annotations

import heapq
import itertools
import os
import threading
import time
import traceback

from . import ranking, store

SLOTS = max(1, int(os.environ.get("BRAIN_LANE_SLOTS", "2")))
DEMOTED_WEIGHT = 0.05          # below every plan; never exactly zero
SLACK_FRACTION = 0.8           # leave room for the query that follows
_EMA_INIT_MS = 80.0
_CACHE_TTL = 60.0

_heap: list = []
_seq = itertools.count()
_lock = threading.Lock()
_work = threading.Condition(_lock)
_started = False
_ema_ms = _EMA_INIT_MS
_plan_cache: dict[str, tuple[float, float]] = {}    # tenant -> (weight, exp)
_shed_counts: dict[str, int] = {}


class _Job:
    __slots__ = ("prio", "texts", "timeout", "kind", "interactive",
                 "deadline", "result", "done")

    def __init__(self, prio, texts, timeout, kind, interactive):
        self.prio = prio
        self.texts = texts
        self.timeout = timeout
        self.kind = kind
        self.interactive = interactive
        self.deadline = time.monotonic() + timeout
        self.result = None
        self.done = threading.Event()


def _enabled() -> bool:
    return os.environ.get("BRAIN_RANKED_EMBED", "1").strip() != "0"


def _weight(tenant: str) -> float:
    """Plan weight × the judge's demotion — cached 60s so the hot path
    costs a dict lookup. Both reads run in the CALLER's bound context."""
    now = time.monotonic()
    hit = _plan_cache.get(tenant)
    if hit and hit[1] > now:
        return hit[0]
    w = ranking.plan_weight(tenant)
    # (retrieval_demoted — the judge's flag — left with the box-side judge,
    #  2026-09-02; DEMOTED_WEIGHT stays for the hub judge to set again)
    _plan_cache[tenant] = (w, now + _CACHE_TTL)
    return w


def _admit(w: float, timeout: float) -> bool:
    """THE LADDER (founder 2026-08-24: "people who hear nothing is free
    users first, then the people who paid less... flagged accounts are on
    total end"): the projection counts only the queue AHEAD AT YOUR OWN
    RANK — jobs that will be served before you. In a free-tier flood a
    business arrival sees an empty road; the free arrivals see the whole
    flood ahead and shed; a demoted account sees everyone ahead, always.
    The silence falls bottom-up, by arithmetic alone."""
    with _lock:
        ahead = sum(1 for entry in _heap if -entry[0] >= w)
        ema = _ema_ms
    projected_ms = (ahead / SLOTS + 1) * ema
    return projected_ms <= timeout * 1000.0 * SLACK_FRACTION


def _note_shed(tenant: str) -> None:
    """The shed IS the log entry: counted in the daily per-tenant table
    (the judge's raw material) and streaks emit recall_pressure."""
    try:
        from . import metering
        metering.count_tool("recall_shed")
    except Exception:                                     # noqa: BLE001
        pass
    with _lock:
        n = _shed_counts.get(tenant, 0) + 1
        _shed_counts[tenant] = n
    if n == 1 or n % 100 == 0:
        print(f"[lane] recall pressure: {n} sheds for {tenant}", flush=True)


def _worker() -> None:
    global _ema_ms
    from . import embed as _embed
    while True:
        with _work:
            while not _heap:
                _work.wait()
            _, _, job = heapq.heappop(_heap)
        if time.monotonic() >= job.deadline - _ema_ms / 1000.0:
            job.done.set()                # caller has given up — skip
            continue
        t0 = time.monotonic()
        job.result = _embed._call_sidecar(
            job.texts, max(1.0, job.deadline - time.monotonic()),
            job.kind, job.interactive)
        dur_ms = (time.monotonic() - t0) * 1000.0
        with _lock:
            _ema_ms = 0.9 * _ema_ms + 0.1 * dur_ms
        job.done.set()


def _ensure_workers() -> None:
    global _started
    with _lock:
        if _started:
            return
        _started = True
    for i in range(SLOTS):
        threading.Thread(target=_worker, name=f"lane-{i}",
                         daemon=True).start()


def ranked(texts, timeout: float, kind: str, interactive: bool):
    """The one entry: rank, shed-or-serve, log. Fails open to a direct
    sidecar call on any lane trouble."""
    if not _enabled():
        from . import embed as _embed
        return _embed._call_sidecar(texts, timeout, kind, interactive)
    try:
        tenant = store.current_tenant()
        _ensure_workers()
        w = _weight(tenant)
        if not _admit(w, timeout):
            _note_shed(tenant)
            return None               # instant honest nothing — never a wait
        job = _Job((-w, next(_seq)), list(texts),
                   timeout, kind, interactive)
        with _work:
            heapq.heappush(_heap, (*job.prio, job))
            _work.notify()
        if not job.done.wait(timeout=timeout * SLACK_FRACTION):
            _note_shed(tenant)        # overtaken past the deadline
            return None
        return job.result
    except Exception:                                     # noqa: BLE001
        traceback.print_exc()
        from . import embed as _embed
        return _embed._call_sidecar(texts, timeout, kind, interactive)
