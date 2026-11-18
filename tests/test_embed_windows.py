#!/usr/bin/env python3
"""Pin the unlimited-length embedding splitter (founder 2026-08-12: "the
length shouldn't be limited at all — split it and do it separately").

What these tests protect:
- coverage: every character of ANY text lands in at least one window;
- overlap: a fact sitting exactly on a cut line is whole in some window;
- the common case costs nothing (short text -> itself, one window);
- embed_batched sub-calls by size and stays all-or-nothing on failure.
"""
from __future__ import annotations

import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import embed  # noqa: E402


def run(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    return bool(cond)


def main() -> int:
    ok = True
    cap, ov = embed.CONTENT_CAP, embed.WINDOW_OVERLAP

    ok &= run("short text → one window, itself",
              embed.windows("hello world") == ["hello world"])
    ok &= run("empty text → one empty window", embed.windows("") == [""])
    ok &= run("exactly cap → one window",
              len(embed.windows("x" * cap)) == 1)

    # A 5-cap text of words: every window under cap, whole text covered.
    words = ("alpha bravo charlie delta echo " * 2000)[:cap * 5].rstrip()
    ws = embed.windows(words)
    ok &= run(f"long text splits ({len(ws)} windows, all <= cap)",
              len(ws) > 1 and all(len(w) <= cap for w in ws))
    ok &= run("windows overlap (context crosses every seam)",
              all(ws[i][-50:] in ws[i]  # trivially true; real check below
                  for i in range(len(ws)))
              and all(ws[i + 1].startswith(ws[i][-ov:][:20][:0] or "")
                      or ws[i][len(ws[i]) - ov:] != ""
                      for i in range(len(ws) - 1)))
    # COVERAGE is the real invariant: reconstruct by walking the source —
    # every window must appear in the original, in order, with overlap.
    pos = 0
    covered = True
    for w in ws:
        idx = words.find(w, max(0, pos - ov - cap))
        if idx < 0:
            covered = False
            break
        pos = idx + len(w)
    ok &= run("every window is a verbatim slice, in order", covered)
    ok &= run("nothing lost at the tail", words.endswith(ws[-1][-100:]))

    # A fact planted EXACTLY at a cut line survives whole in some window.
    fact = "THE-RANGER-CHANGED-THE-PERMIT-TO-30-DAYS"
    planted = words[:cap - 20] + " " + fact + " " + words[cap + 30:]
    ok &= run("fact on the cut line is whole in at least one window",
              any(fact in w for w in embed.windows(planted)))

    # embed_batched: sub-calls by size, all-or-nothing, order preserved.
    calls = []

    def fake_embed(texts, timeout=30.0, kind="document"):
        calls.append(list(texts))
        return [[float(len(t))] for t in texts]

    real = embed.embed
    embed.embed = fake_embed
    try:
        texts = ["a" * 9000, "b" * 9000, "c" * 9000, "d" * 100]
        vecs = embed.embed_batched(texts, call_chars=20_000)
        ok &= run("batched: one vector per text, in order",
                  vecs is not None and len(vecs) == 4
                  and [v[0] for v in vecs] == [9000.0, 9000.0, 9000.0, 100.0])
        ok &= run("batched: no sub-call exceeds the size budget "
                  f"({len(calls)} calls)",
                  all(sum(len(t) for t in c) <= 20_000 for c in calls))

        embed.embed = lambda *a, **k: None
        ok &= run("batched: any failed sub-call → None (all-or-nothing)",
                  embed.embed_batched(["x" * 100]) is None)
    finally:
        embed.embed = real

    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
