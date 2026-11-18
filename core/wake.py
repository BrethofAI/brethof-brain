"""In-process wake board — the last metre of the push pipeline.

The client hook already pushes: every exchange POSTs to the API, which writes
the job into the tenant's own database. Until 2026-08-09 the workers then
REDISCOVERED that work by sweeping every tenant database on a 5-30s timer,
which kept ~3 warm connections per tenant forever and made connection count
scale with tenants that EXIST instead of tenants that are TALKING — the
96/100 saturation incident, and a ceiling of ~33 accounts per box. Founder:
"It cannot check all the fucking accounts every fifteen seconds."

This module is the noticeboard. Producers poke(board, tenant) at the moment
they enqueue — enqueuer and worker live in the same process, so the signal
is a set and an Event, no database involved. Each worker blocks on its own
board and only opens the databases that were poked. An idle tenant is never
touched, its pool drains to zero, and its database holds no sessions.

DURABILITY IS NOT HERE. The queue rows in the tenant databases remain the
truth; the board is a hint about where to look. Three nets catch a lost hint:
- BOOT: every worker still does its one recovery sweep at startup and pokes
  every provisioned tenant once, so work enqueued before a restart is found.
- RETRY: a worker that leaves a tenant with a future next_try re-arms itself
  with poke_later().
- SAFETY SWEEP: wait() returns everyone at a slow interval (default 6h,
  BRAIN_SAFETY_SWEEP_S) — a stranded job lags hours, never forever, and the
  touch is transient: pools shrink back between sweeps.
"""
from __future__ import annotations

import os
import threading
import time

SAFETY_SWEEP_S = float(os.environ.get("BRAIN_SAFETY_SWEEP_S", str(6 * 3600)))

_lock = threading.Lock()
_boards: dict[str, set[str]] = {}
_events: dict[str, threading.Event] = {}
# Earliest armed poke_later per (board, tenant) — dedupe so an hourly-backoff
# job re-arms one timer, not one per observation.
_armed: dict[tuple[str, str], float] = {}
_last_sweep: dict[str, float] = {}


def _event(board: str) -> threading.Event:
    with _lock:
        return _events.setdefault(board, threading.Event())


def _alive(tenants) -> list[str]:
    """Drop tenants whose database is gone.

    THE ONE PLACE this is enforced (2026-08-13). A tenant dropped while its
    work was in flight stayed on the board and every worker loop re-poked
    itself over a database that no longer existed — five loops, five
    identical tracebacks, and the noise sat exactly where a REAL metering or
    curation failure would have to be noticed. Filtering here fixes every
    loop without touching any of them: a dead tenant is simply never handed
    out. Imported lazily so this module stays a dependency-free noticeboard.
    """
    try:
        from core import store
    except Exception:                                     # noqa: BLE001
        return list(tenants)
    return [t for t in tenants if not store.is_dead(t)]


def forget(board: str, tenant: str) -> None:
    """Evict a tenant from a board and cancel any armed re-wake. Called when
    a worker learns mid-job that the tenant is gone."""
    with _lock:
        b = _boards.get(board)
        if b:
            b.discard(tenant)
        _armed.pop((board, tenant), None)


def poke(board: str, tenant: str) -> None:
    """Tenant has (or may have) due work on this board. Cheap, thread-safe,
    idempotent — poking a tenant with nothing due costs one short query."""
    with _lock:
        _boards.setdefault(board, set()).add(tenant)
        _events.setdefault(board, threading.Event()).set()


def poke_all(board: str, tenants: list[str]) -> None:
    with _lock:
        _boards.setdefault(board, set()).update(tenants)
        _events.setdefault(board, threading.Event()).set()


def poke_later(board: str, tenant: str, delay_s: float) -> None:
    """Re-arm a future wake (retry backoff, grace period). Deduped: if an
    earlier-or-equal wake is already armed for this tenant, do nothing."""
    delay_s = max(0.05, float(delay_s))
    due = time.monotonic() + delay_s
    key = (board, tenant)
    with _lock:
        armed = _armed.get(key)
        if armed is not None and armed <= due:
            return
        _armed[key] = due

    def _fire() -> None:
        with _lock:
            if _armed.get(key) is not None and _armed[key] <= time.monotonic():
                _armed.pop(key, None)
        poke(board, tenant)

    t = threading.Timer(delay_s, _fire)
    t.daemon = True
    t.start()


def wait(board: str, all_tenants, timeout: float = 60.0) -> list[str]:
    """Block until someone pokes this board (or timeout), then drain it.

    all_tenants: zero-arg callable returning every provisioned tenant — used
    only by the slow safety sweep. Returns the tenants to visit now; an empty
    list means nothing was poked (the worker just loops back into wait)."""
    ev = _event(board)
    ev.wait(timeout)
    now = time.monotonic()
    with _lock:
        pending = _boards.get(board) or set()
        _boards[board] = set()
        ev.clear()
        sweep = now - _last_sweep.get(board, 0.0) >= SAFETY_SWEEP_S
        if sweep:
            _last_sweep[board] = now
    if sweep:
        try:
            everyone = list(all_tenants())
        except Exception:
            everyone = []
        if everyone:
            return _alive(sorted(set(everyone) | pending))
    return _alive(sorted(pending))


def mark_swept(board: str) -> None:
    """The boot recovery sweep counts as a safety sweep — don't do it twice."""
    with _lock:
        _last_sweep[board] = time.monotonic()
