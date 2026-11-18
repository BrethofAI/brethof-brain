"""Onboarding build 2026-08-09 — the pure-unit half.

Covers what needs no database: the flag-class refusal at save_record's door
(founder law: curated memory is CURRENT TRUTH ONLY — no flag survives any
door) and the canonical ★SEED set's shape. The DB-dependent halves (purpose
pinning, the NO PURPOSE nag, seed read-only guards, provision seeding) are
proven live by the fresh-account walkthrough and the gate's conformance
phase.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import tools  # noqa: E402
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("hj", Path(__file__).resolve().parent.parent / "hub/jobs.py")
hj = _ilu.module_from_spec(_spec); _spec.loader.exec_module(hj)
RULE_CAP = 1000  # the hub's MAX_RULE_CHARS


def test_flag_keys_are_refused_at_the_door():
    for bad in ("superseded_by", "obsolete", "on_hold", "SUPERSEDED",
                "deprecated", "parked", "archived"):
        res = tools.save_record(
            "someproj", "some_record",
            json.dumps({"type": "note", "title": "t", "content": "c",
                        bad: "anything"}))
        assert res.startswith("Error"), f"{bad} was not refused: {res[:120]}"
        assert "CURRENT TRUTH ONLY" in res
        assert "delete_memory" in res and "supersede_memory" in res


def test_ordinary_meta_keys_still_pass_the_flag_check():
    """The refusal must be surgical: a normal extra field reaches the write
    path — proven by it failing LATER on the unavailable test DB (raises
    NoTenantBound here), never on the flag check."""
    try:
        res = tools.save_record(
            "someproj", "some_record",
            json.dumps({"type": "note", "title": "t", "content": "c",
                        "severity": "high", "url": "https://x"}))
    except Exception as e:                # no tenant bound in a unit test —
        res = str(e)                      # the write path was reached. Good.
    assert "CURRENT TRUTH ONLY" not in res


def test_seed_set_shape():
    assert len(hj.SEED_RULES) == 5, ("seed set stays deliberately SMALL (the ledger seed joined 2026-09-04; "
                                     "the standing documents 2026-09-29, founder 2026-09-28)")
    for rid, r in hj.SEED_RULES.items():
        assert rid.startswith("seed_"), rid
        assert r["title"].startswith("★SEED — "), rid
        # Each seed obeys the same cap customer law lives under — the
        # service must not exempt its own teaching from its own hygiene.
        assert len(r["title"]) + len(r["content"]) <= RULE_CAP, rid
        assert r["content"].strip(), rid


def test_seed_ids_do_not_collide_with_purpose_namespace():
    """add_project owns 'purpose_*'; the seed set must never squat there."""
    for rid in hj.SEED_RULES:
        assert not rid.startswith("purpose_"), rid
