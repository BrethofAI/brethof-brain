"""The founder's account ranking — ONE implementation for every lane.

2026-08-24 port of the embedder drainer's ordering (founder spec
2026-08-12) to the turn and gate lanes: "I don't believe in fair share.
I believe in the ranking. Someone dumps a lot of shit — he's on the end
of the queue, and I don't give a fuck how much he paid."

score = plan_weight / (1 + backlog / 500)

Plan lifts; backlog demotes — and backlog counts rows pending RIGHT NOW,
never lifetime volume, so a dump loses the race only while it lasts and a
customer's history never counts against them. No anti-starvation term on
purpose: the softener means a flooder is slowed, never frozen.
"""
from __future__ import annotations

import traceback

from . import quota, store

PLAN_WEIGHT = {"business": 5.0, "team": 4.0, "pro": 3.0,
               "starter": 2.0, "free": 1.0}
DEFAULT_PLAN_WEIGHT = 4.0        # exempt/static (our own) tenants rank high
BACKLOG_SOFTENER = 500.0


def plan_weight(tenant: str) -> float:
    tok = store.bind(tenant)
    try:
        plan = quota.effective_plan()
    except Exception:                                     # noqa: BLE001
        plan = None
    finally:
        store.unbind(tok)
    return PLAN_WEIGHT.get(plan or "", DEFAULT_PLAN_WEIGHT)


def score(tenant: str, backlog: int) -> float:
    return plan_weight(tenant) / (1.0 + max(0, backlog) / BACKLOG_SOFTENER)


def order(tenants, backlog_of) -> list:
    """Rank one wake batch, best-served first. The common case — a single
    poked tenant — pays nothing. backlog_of(tenant) -> rows pending now in
    the caller's lane. A tenant whose score cannot be computed (deleted
    mid-batch, db hiccup) ranks as an exempt tenant with no backlog rather
    than being dropped: ranking orders service, it must never deny it."""
    ts = list(tenants)
    if len(ts) <= 1:
        return ts

    def _s(t):
        try:
            return score(t, int(backlog_of(t)))
        except Exception:                                 # noqa: BLE001
            traceback.print_exc()
            return DEFAULT_PLAN_WEIGHT

    return sorted(ts, key=_s, reverse=True)
