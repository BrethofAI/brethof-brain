#!/usr/bin/env python3
"""Pin the ambient-injection cooldown + whole-or-nothing mechanics.

Born 2026-08-12 from the founder's redesign ("that's not just retrieval
memory — that's a working brain"): the permanent burn-for-the-session
ledger died in favor of turn cooldowns, and records are injected WHOLE or
not at all (wind-tunnel-proven: a model given cut text uses the cut text
as if complete — truncation misleads).

What these tests protect:
- the suppression predicate — the same-turn exemption is what makes the two
  concurrent prompt-submit parts deterministic without shared state; if it
  regresses to `delta < cooldown`, part 2 starts skipping the record part 1
  chose, and doubles silently die under race;
- whole-or-nothing — if _record_block ever returns a cut body again, agents
  go back to acting on half a record without knowing it.
"""
from __future__ import annotations

import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.injection import (_HOOK_BUDGET, _is_cooling,  # noqa: E402
                            _record_block)


def run(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    return bool(cond)


def main() -> int:
    ok = True
    cd = 40

    # ── the predicate ────────────────────────────────────────────────────
    ok &= run("never injected → eligible", not _is_cooling(None, 100, cd))
    ok &= run("stamped THIS turn → still eligible (sibling-part "
              "determinism)", not _is_cooling(100, 100, cd))
    ok &= run("stamped 1 turn ago → suppressed", _is_cooling(99, 100, cd))
    ok &= run("stamped cooldown-1 ago → suppressed",
              _is_cooling(100 - (cd - 1), 100, cd))
    ok &= run("stamped exactly cooldown ago → eligible again",
              not _is_cooling(100 - cd, 100, cd))
    ok &= run("ledger cleared (compact) = never injected → eligible",
              not _is_cooling(None, 5000, cd))

    # ── whole-or-nothing ─────────────────────────────────────────────────
    body = "fact " * 200                       # ~1000 chars, fits easily
    e = {"row": {"title": "A decision", "content": body, "ts": "2026-08-12"}}
    block = _record_block("mind:x", e, _HOOK_BUDGET)
    ok &= run("fitting record injects WHOLE (body intact)",
              body.strip() in block and "FULL record" in block)

    big = {"row": {"title": "Monster", "content": "y" * (_HOOK_BUDGET + 500),
                   "ts": "2026-08-12"}}
    pointer = _record_block("mind:big", big, _HOOK_BUDGET)
    ok &= run("oversized record → pointer, NEVER a cut body",
              "get_memory('mind:big')" in pointer
              and "yyyy" not in pointer)
    ok &= run("pointer names the record and stays tiny",
              "mind:big" in pointer and len(pointer) < 300)

    # A record that fits the whole budget but not the ROOM left after the
    # rule/cards block must also degrade to a pointer, not a trim.
    tight = _record_block("mind:x", e, 200)
    ok &= run("record bigger than remaining room → pointer too",
              "get_memory('mind:x')" in tight and body.strip() not in tight)

    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
