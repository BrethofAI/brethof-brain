"""Per-ACCOUNT request rate limit — the fence around the compute doors.

The 2026-08-24 audit's biggest gap (founder: "very good catch because
there is zero limits on it... many users can have many API keys — someone
can connect ten agents to one API"): every hook call and MCP tool call
runs real work — ambient recall embeds the prompt INLINE on the shared
3-core CPU — and nothing bounded how fast one account could fire them.

THE SHAPE: one token bucket per TENANT. Every key an account mints draws
from the same bucket, so keys never multiply the budget — the account is
the unit, exactly like the quota. Budgets scale with the plan ladder (the
same ladder that ranks the queues); plan-less tenants (our own static-key
fleet) are exempt, the same class the quota exempts.

Sustained per minute, with a 2x burst bucket so a busy moment never
trips it: free 60, starter 120, pro 240, team 480, business 960. A real
agent turn costs 2-3 calls — a free account at 60/min is not typing, it
is scripting. On refusal the caller gets 429 + Retry-After; the client is
fail-open by contract (a limited hook is a silent no-op, and a limited
stop keeps its spool and retries — data waits, it is never lost).

In-memory by design: the API is one process, and a limiter that survives
a restart is not worth a database round-trip per request.
"""
from __future__ import annotations

import os
import threading
import time

BASE_PER_MIN = float(os.environ.get("BRAIN_RATE_PER_MIN", "60"))
BURST_X = 2.0
_PLAN_X = {"free": 1.0, "starter": 2.0, "pro": 4.0,
           "team": 8.0, "business": 16.0}

_buckets: dict[str, list[float]] = {}     # tenant -> [tokens, last_ts]
_lock = threading.Lock()


def _enabled() -> bool:
    return os.environ.get("BRAIN_RATE_LIMIT", "1").strip() != "0"


def allow(tenant: str, plan: str | None) -> tuple[bool, int]:
    """Spend one request from the account's bucket. Returns
    (allowed, retry_after_s). Plan-less tenants are exempt — enforcement
    applies exactly to accounts whose plan the control plane states, the
    same contract as the quota."""
    if plan is None or not _enabled():
        return True, 0
    per_min = BASE_PER_MIN * _PLAN_X.get(plan, 1.0)
    rate = per_min / 60.0                                 # tokens per second
    cap = per_min * BURST_X                               # bucket capacity
    now = time.monotonic()
    with _lock:
        b = _buckets.get(tenant)
        if b is None:
            b = [cap, now]
            _buckets[tenant] = b
        tokens, last = b
        tokens = min(cap, tokens + (now - last) * rate)
        if tokens < 1.0:
            b[0], b[1] = tokens, now
            return False, max(1, int((1.0 - tokens) / rate))
        b[0], b[1] = tokens - 1.0, now
        return True, 0


NOTICE = ("Too many requests from this account — every API key shares one "
          "request budget. Slow the loop, or upgrade at brethof.ai/account "
          "for a higher rate.")

_refusals: dict[str, int] = {}


def note_refusal(tenant: str) -> int | None:
    """Count a refusal; return the count when it is signal-worthy (the
    first, then every 500th — a sustained flood keeps reminding the judge
    without writing a row per refused request), else None."""
    with _lock:
        n = _refusals.get(tenant, 0) + 1
        _refusals[tenant] = n
    return n if (n == 1 or n % 500 == 0) else None
