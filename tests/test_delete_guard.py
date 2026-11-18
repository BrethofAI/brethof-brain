#!/usr/bin/env python3
"""Prove the delete guard actually fires.

A live curation run may propose no deletions at all, so the guard can pass
untested for a long time and then matter enormously once. These exercise the
validation directly with a stubbed backend — no network, no provider.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import curate, inference  # noqa: E402


class StubBackend:
    """Returns a fixed payload so the parsing and guard are what's under test."""
    def __init__(self, text: str):
        self.text = text

    def generate(self, prompt, system="", **kw):
        return inference.Completion(text=self.text, input_tokens=100,
                                    output_tokens=50, seconds=0.1, model="stub")


TURNS = [{"role": "user", "text": "we dropped the pre-review gate entirely"},
         {"role": "assistant", "text": "understood, recording that decision"}]

PAYLOAD = """{"records":[
 {"id":"skill_autonomy_decision","type":"decision","title":"Agent runs free",
  "content":"Pre-review of skills dropped 2026-07-25."},
 {"id":"BAD ID WITH SPACES","type":"decision","title":"x","content":"y"},
 {"id":"empty_body","type":"note","title":"x","content":"   "},
 {"id":"weird_type","type":"nonsense","title":"z","content":"kept, type normalised"},
 {"id":"mind:qualified_id","type":"note","title":"q","content":"catalog-style id"}],
 "delete":[{"id":"real_record_one","reason":"the session reversed it"},
           {"id":"hallucinated_record","reason":"looks dead"},
           {"id":"REAL_RECORD_TWO","reason":"superseded today"},
           {"id":"real_record_one_nope"}],
 "state_update":"gate removed"}"""

EXISTING = {"real_record_one", "real_record_two"}


def run(name, cond):
    cond = bool(cond)          # callers pass truthy strings/lists, not just bools
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    return cond


def main() -> int:
    res = curate.curate(TURNS, existing_ids=EXISTING, backend=StubBackend(PAYLOAD))
    ok = True

    print("delete guard:")
    ok &= run("real id confirmed", "real_record_one" in res.delete_confirmed)
    ok &= run("case-insensitive match confirmed", "real_record_two" in res.delete_confirmed)
    ok &= run("HALLUCINATED id rejected, not deleted",
              "hallucinated_record" in res.delete_rejected)
    ok &= run("hallucinated id NEVER reaches confirmed list",
              "hallucinated_record" not in res.delete_confirmed)
    # Added 2026-07-26 after a 4-turn transcript talked the model into proposing
    # 18 real-but-alive records for deletion. A reason is now mandatory, and the
    # count is capped — existence was never the same thing as relevance.
    ok &= run("delete with NO reason is ignored",
              "real_record_one_nope" not in res.delete_confirmed
              and "real_record_one_nope" not in res.delete_rejected)
    ok &= run("every confirmed delete carries its reason",
              all(res.delete_reasons.get(i) for i in res.delete_confirmed))
    ok &= run("delete list is capped", len(res.delete_confirmed)
              + len(res.delete_rejected) <= curate.MAX_DELETES)
    ok &= run("catalog-style 'project:id' is KEPT, split into project + id",
              any(r["id"] == "qualified_id" and r.get("project") == "mind"
                  for r in res.records))

    print("record validation:")
    ids = {r["id"] for r in res.records}
    ok &= run("good record kept", "skill_autonomy_decision" in ids)
    ok &= run("malformed id dropped", not any(" " in i for i in ids))
    ok &= run("empty-body record dropped", "empty_body" not in ids)
    ok &= run("unknown type normalised to note, record KEPT",
              any(r["id"] == "weird_type" and r["type"] == "note" for r in res.records))

    print("degradation:")
    class Dead:
        def generate(self, *a, **k):
            return None
    dead = curate.curate(TURNS, existing_ids=EXISTING, backend=Dead())
    ok &= run("provider down -> error, no exception", not dead.ok and dead.error)
    ok &= run("provider down -> no records invented", dead.records == [])

    garbage = curate.curate(TURNS, existing_ids=EXISTING,
                            backend=StubBackend("I'm sorry, I cannot do that."))
    ok &= run("unparseable output -> error, not a crash", not garbage.ok)

    print(f"\n{'ALL PASS' if ok else 'FAILURES PRESENT'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())


def test_delete_guard():
    assert main() == 0
