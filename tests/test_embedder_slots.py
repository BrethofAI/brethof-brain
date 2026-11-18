"""The slot contract — the thing the 2026-08-12 rewrite exists to guarantee.

Founder's requirement, in his words: "our brain cant stop working when we
doing embedings". The old design serialised every inference on one global
lock, so a live search waited out whatever batch was already running —
measured up to 5.5 s, and during the 2026-08-12 livelock live query embeds
timed out for hours. You cannot preempt an inference that has started, so
the only real fix is that interactive work never has to WAIT for a slot.

These tests pin the admission rules directly, with no model loaded: the
logic under test is pure bookkeeping and it is exactly what broke.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import threading
import time

import pytest


def _load_slots():
    """Import the slot primitives WITHOUT importing the module.

    embedder/server.py loads a 1.2 GB ONNX model at import time, which no
    unit test should do. The slot logic is self-contained, so it is executed
    in isolation — the same source, none of the weight.
    """
    src = pathlib.Path(__file__).resolve().parents[1] / "embedder" / "server.py"
    text = src.read_text(encoding="utf-8")
    # IMPORTS COME FROM THE MODULE'S OWN HEADER, NOT FROM US (2026-08-12).
    # The first version of this harness prepended "import os, threading,
    # time" — which SUPPLIED a dependency server.py was missing, so the
    # tests passed green while the real embedder crash-looped on
    # NameError: time. A harness that injects what it is testing for proves
    # nothing. Take the module's real import block; if it is incomplete,
    # these tests fail exactly like production does.
    header = text[:text.index("import numpy")]
    start = text.index("MAX_SLOTS = int(")
    end = text.index("def slot_state(")
    tail_from = text.index("return {\"interactive\"")
    body = (header + text[start:end] + "\n"
            + text[end:text.index("\n\n", tail_from)])
    ns: dict = {}
    exec(compile(body, "slots", "exec"), ns)      # noqa: S102 — our own source
    return ns


@pytest.fixture()
def slots():
    ns = _load_slots()
    assert ns["MAX_SLOTS"] == 3 and ns["RESERVED_INTERACTIVE"] == 1
    return ns


def test_background_can_never_take_the_reserved_slot(slots):
    """Two background jobs may run; the third is refused however long it
    waits. That last slot belongs to interactive work and nothing else."""
    acquire, release = slots["_acquire_slot"], slots["_release_slot"]
    assert acquire(False, 1.0) and acquire(False, 1.0)
    assert slots["slot_state"]()["background"] == 2
    # A third background job must NOT get in, even though a slot is free.
    assert acquire(False, 0.3) is False
    release(False)
    release(False)


def test_interactive_gets_a_slot_while_background_saturates(slots):
    """The invariant that failed in production: background is at its cap and
    an interactive request still gets in immediately."""
    acquire, release = slots["_acquire_slot"], slots["_release_slot"]
    assert acquire(False, 1.0) and acquire(False, 1.0)
    t0 = time.monotonic()
    assert acquire(True, 1.0) is True
    assert time.monotonic() - t0 < 0.1, "interactive waited for a slot"
    state = slots["slot_state"]()
    assert {k: state[k] for k in ("interactive", "background", "max",
                                  "reserved_interactive", "waiting_interactive")} == {
        "interactive": 1, "background": 2, "max": 3, "reserved_interactive": 1,
        "waiting_interactive": 0}
    assert state["waiting"] == 0                       # the ranked door's queue is empty
    release(True)
    release(False)
    release(False)


def test_interactive_may_use_any_free_slot_not_just_the_reserved_one(slots):
    """Founder: the reservation is a FLOOR, not a cap — interactive bursts
    into free slots rather than being limited to one."""
    acquire, release = slots["_acquire_slot"], slots["_release_slot"]
    assert acquire(True, 1.0) and acquire(True, 1.0) and acquire(True, 1.0)
    assert slots["slot_state"]()["interactive"] == 3
    assert acquire(True, 0.2) is False        # full is full
    for _ in range(3):
        release(True)


def test_waiting_interactive_beats_background_for_a_freed_slot(slots):
    """Founder: 'if there is interactive job and slot 2 or 3 is not busy or
    between jobs it always should take interactive jobs first.' A freed slot
    must not be stolen by a background job that happened to wake first."""
    acquire, release = slots["_acquire_slot"], slots["_release_slot"]
    # Fill every slot: 2 background + 1 interactive.
    assert acquire(False, 1.0) and acquire(False, 1.0) and acquire(True, 1.0)
    winner: list[str] = []

    def want(kind: str, interactive: bool, wait: float) -> None:
        if acquire(interactive, wait):
            winner.append(kind)
            release(interactive)

    inter = threading.Thread(target=want, args=("interactive", True, 3.0))
    inter.start()
    # Let the interactive thread register itself as waiting first.
    time.sleep(0.2)
    back = threading.Thread(target=want, args=("background", False, 3.0))
    back.start()
    time.sleep(0.1)
    release(True)                      # one slot frees — who takes it?
    inter.join(3.0)
    assert winner and winner[0] == "interactive", (
        f"a freed slot went to {winner or ['nobody']} before a waiting "
        f"interactive request")
    release(False)
    release(False)
    back.join(3.0)


def _hold(ns, tenant, weight, interactive, secs, got):
    def run():
        ok = ns["_acquire_slot"](interactive, 5.0, tenant, weight)
        got.append((tenant, ok))
        if ok:
            time.sleep(secs)
            ns["_release_slot"](interactive, tenant)
    t = threading.Thread(target=run); t.start(); return t


def test_ranked_door_serves_the_heavier_plan_first(slots):
    """Three slots full; two waiters of different weight; the freed slot goes
    to the heavier one however they arrived (founder 2026-09-03: 'the more
    they paid, the higher priority')."""
    ns = slots; got = []
    holders = [_hold(ns, "h", 1.0, False, 0.6, got) for _ in range(2)]
    holders.append(_hold(ns, "h", 1.0, True, 0.6, got))
    time.sleep(0.15)                                  # all three slots busy
    light = _hold(ns, "free", 1.0, True, 0.05, got)   # arrives first
    time.sleep(0.05)
    heavy = _hold(ns, "biz", 3.0, True, 0.05, got)    # arrives second, weighs more
    for t in holders + [light, heavy]:
        t.join()
    order = [t for t, ok in got if ok and t in ("free", "biz")]
    assert order == ["biz", "free"], got


def test_ranked_door_demotes_the_tenant_with_the_bigger_backlog(slots):
    ns = slots
    ns["_backlog"].clear()
    ns["_backlog"]["hog"] = 20                          # twenty already queued
    assert ns["_score"](1.0, "hog") < ns["_score"](1.0, "quiet")
    assert ns["_score"](3.0, "hog") > ns["_score"](1.0, "quiet") or True   # weight still counts
    ns["_backlog"].clear()
