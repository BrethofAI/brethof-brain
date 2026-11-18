"""Plan → caps. THE enforcement source of truth the control plane's mind.py
has pointed at since 2026-07-24 ("mindcore/tiers.py is the enforcement
source of truth") — it exists now.

THE LADDER (founder-final 2026-07-30, full record in memory
mind:pricing_tiers_decided_2026_07_24): the two meters are EXCHANGES per
month (one stop-hook firing = one exchange; summarize digests spend the
same allowance) and STORAGE (pg_database_size — the whole on-disk reality).
Everything else — recall, search, graph, injections — is unlimited on every
tier, deliberately: it costs us nothing marginal and it is the pitch.

UNKNOWN PLAN FAILS CLOSED TO FREE and logs loudly: with today's tenant
population that can only be a config mistake, and a mistake should cost us
a support message, not unmetered inference.
"""
from __future__ import annotations

# Caps per plan code. Storage is the SOFT cap (MB); the stages in quota.py
# derive warn/pause/refuse thresholds from it.
TIERS: dict[str, dict] = {
    "free":     {"exchanges": 500,    "storage_mb": 20},
    "starter":  {"exchanges": 2_500,  "storage_mb": 500},
    "pro":      {"exchanges": 8_000,  "storage_mb": 2_048},
    "team":     {"exchanges": 20_000, "storage_mb": 5_120},
    "business": {"exchanges": 40_000, "storage_mb": 15_360},
}

# Control-plane plan codes that predate the final ladder (its TIERS dict
# still mints t9_99-style codes). Mapped conservatively upward — a paying
# customer must never be squeezed below what they bought.
PLAN_ALIASES = {
    "t9_99": "starter",
    "t19_99": "pro",
    "t24_99": "pro",
    "t49_99": "team",
}


def caps(plan: str) -> dict:
    p = (plan or "").strip().lower()
    p = PLAN_ALIASES.get(p, p)
    if p not in TIERS:
        print(f"[tiers] UNKNOWN plan '{plan}' — failing closed to free caps",
              flush=True)
        return TIERS["free"]
    return TIERS[p]
