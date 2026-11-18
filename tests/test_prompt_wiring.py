#!/usr/bin/env python3
"""Pin the parameters that were silently dead.

`curate()` grew `prompt` and `think` in its signature while the call site kept
sending the hardcoded lean SYSTEM and the provider-default think — from the
file's creation until 2026-07-26. Every lean-vs-full A/B ran lean-vs-lean;
every think toggle toggled nothing. No error, no warning, plausible results.

This is the same failure class as the delete_memory registry patch that
matched nothing: a parameter that LOOKS wired is worse than one that does not
exist. These tests fail the moment either stops reaching the backend.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import curate, inference  # noqa: E402

PAYLOAD = '{"records":[],"delete":[],"state_update":"x"}'


class Capture:
    def __init__(self):
        self.system = None
        self.think = "NEVER-SET"

    def generate(self, prompt, system="", think=None, **kw):
        self.system, self.think = system, think
        return inference.Completion(text=PAYLOAD, input_tokens=1,
                                    output_tokens=1, seconds=0.0, model="stub")


TURNS = [{"role": "user", "text": "anything"}]


def run(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    return bool(cond)


def main() -> int:
    ok = True
    for label, want in (("full", curate.SYSTEM_FULL), ("lean", curate.SYSTEM),
                        ("gated", curate.SYSTEM_GATED)):
        cap = Capture()
        curate.curate(TURNS, existing_ids=set(), backend=cap, prompt=label)
        ok &= run(f"prompt='{label}' reaches the backend", cap.system is want)

    cap = Capture()
    curate.curate(TURNS, existing_ids=set(), backend=cap, prompt="no-such")
    ok &= run("unknown prompt falls back to FULL, never silently lean",
              cap.system is curate.SYSTEM_FULL)

    for tv in (True, False, None):
        cap = Capture()
        curate.curate(TURNS, existing_ids=set(), backend=cap, think=tv)
        ok &= run(f"think={tv} reaches the backend", cap.think is tv)

    print(f"\n{'ALL PASS' if ok else 'FAILURES PRESENT'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())


def test_prompt_wiring():
    assert main() == 0
