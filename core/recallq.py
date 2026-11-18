"""WHAT THE PER-PROMPT SEARCH STARTS FROM (founder decision 2026-11-17, "B+v3").

A user's message is mostly a scrap — "yes, do it", "the second one" — and the
substance it points at is in the agent's previous reply, which usually covers
several things at once. So the automatic recall searches with:
  B   the message itself, and the previous reply cut where its TOPICS change
      (headings, bullet items, numbered items, blank lines), each topic joined
      with the start of the message — so every thing the reply discussed gets
      its own search;
  v3  questions the AI model writes for the message, with the reply as
      context only (the hub asks; the prompt is TEXTS["recall_questions"]).
Every search is run and the results are JOINED: a record found by several
searches, or high in one, ranks first (reciprocal-rank fusion), while each
record keeps its own best score for the strength bar.

Measured on the founder's last 100 real prompts (blind judge): helpful
records 115 → 156 of 500, prompts helped 66 → 80 of 100 (GLM: 154, 77).
scripts/ops/recall_questions.py, followup_pool.py, recall_judge.py.
"""
from __future__ import annotations

import re

TOPIC_CHARS = 700        # topics are packed up to about this size
MAX_TOPICS = 8           # past this, the first and last four
LEAD_CHARS = 300         # the message's start joined to each topic
MAX_QUESTIONS = 6
RRF_K = 10

_CUT = re.compile(r"\n\s*\n|\n(?=#+ |\s*[-*] |\s*\d+\. |\|)")


def topics(reply: str) -> list[str]:
    """The previous reply cut where its topics change, small neighbours
    packed together, fragments under 40 characters dropped."""
    parts = [p.strip() for p in _CUT.split(reply or "") if p.strip()]
    out, cur = [], ""
    for p in parts:
        if cur and len(cur) + len(p) > TOPIC_CHARS:
            out.append(cur)
            cur = ""
        cur = (cur + "\n" + p).strip()
    if cur:
        out.append(cur)
    out = [o for o in out if len(o) > 40]
    return out if len(out) <= MAX_TOPICS else out[:MAX_TOPICS // 2] + out[-(MAX_TOPICS // 2):]


def queries(prompt: str, reply: str, questions: list[str] | None) -> list[str]:
    """Every search text for one prompt: the message, each topic joined with
    the message's start, and the model's questions."""
    lead = (prompt or "")[:LEAD_CHARS]
    qs = [prompt] + [lead + "\n" + t for t in topics(reply)]
    qs += [q.strip() for q in (questions or []) if q and q.strip()][:MAX_QUESTIONS]
    return [q for q in qs if q.strip()]


def parse_questions(text: str) -> list[str]:
    """One question per line from the model's answer; list markers dropped."""
    out = []
    for line in (text or "").splitlines():
        q = line.strip().lstrip("-*•").strip()
        q = re.sub(r"^\d+[.)]\s*", "", q).strip()
        if len(q) > 8:
            out.append(q[:400])
    return out[:MAX_QUESTIONS]


def fuse(result_lists: list[list[tuple]], k: int) -> list[tuple]:
    """Reciprocal-rank fusion over (score, rid, entry) lists. Order by the
    fused rank; each record keeps its best own score (the strength bar reads
    that, never the fused number)."""
    fused: dict[str, float] = {}
    best: dict[str, tuple] = {}
    for hits in result_lists:
        for rank, (s, rid, e) in enumerate(hits, 1):
            fused[rid] = fused.get(rid, 0.0) + 1.0 / (RRF_K + rank)
            if rid not in best or s > best[rid][0]:
                best[rid] = (s, rid, e)
    order = sorted(fused, key=lambda r: -fused[r])
    return [best[r] for r in order][:k]
