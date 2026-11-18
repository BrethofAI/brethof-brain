"""THE HUB COMPOSES WHAT A SESSION IS TOLD — design brain-design-hub-brain,
step 2, in the FORWARD shape the founder chose on 2026-09-10: the client
keeps posting its two reading hooks to the box; the box checks the key and
the door, then asks the hub over the pipe ({rid, op: "brief" | "ambient"});
the hub composes the block by commanding the box (core/composer through
`server.command`) and answers by rid; the box hands the text to the
harness. The doctrine — sections, the bar, the slots, the cooldowns — runs
here and nowhere the customer can read it. The law is the hub's own rule
table; the rule a prompt touches is matched here, with the vector the box
sends (its embedder, its CPU).
"""
from __future__ import annotations

import time
from typing import Awaitable, Callable

from core import composer, brieftext as T
from . import jobs


def purpose_of(rules: list[dict], project: str) -> str:
    """The project's own purpose, else the purpose of the most specific
    pattern template it matches (area 'u_%' → every u_<id>). Until
    2026-11-10 only the exact match counted: a template's purpose reached
    none of its projects, so every Aurora fan brief said NO PURPOSE SET and
    the fan curators worked from the ★SEED rules alone."""
    for r in rules:
        rid = str(r.get("rule_id") or "")
        if rid == f"purpose_{project}" or (rid.startswith("purpose_") and r.get("area") == project):
            return " ".join(str(r.get("content") or "").split())
    best, best_len = "", -1
    for r in rules:
        rid, area = str(r.get("rule_id") or ""), str(r.get("area") or "")
        if rid.startswith("purpose_") and "%" in area and project and jobs._like(project, area):
            spec = len(area.replace("%", ""))
            if spec > best_len:
                best, best_len = " ".join(str(r.get("content") or "").split()), spec
    return best


async def law_for(fetchall, seed_vecs: dict, tenant_id: str, project: str,
                  with_vecs: bool = False) -> tuple[dict, list[dict]]:
    """{"rules", "purpose", "note"} as the box's rules_hub.fetch shaped it,
    from the hub's table, plus the rule dicts (with vectors when asked, for
    the match)."""
    rows = await fetchall(
        "SELECT rule_id, title, content, area, pin, updated_at, vec FROM rule"
        " WHERE tenant_id = ? ORDER BY rule_id", (tenant_id,))
    seeds = not await fetchall("SELECT 1 FROM seeds_off WHERE tenant_id = ?", (tenant_id,))
    rules = jobs.rules_for([r[:6] for r in rows], project, seeds=seeds)
    if with_vecs:
        vecs = {r[0]: jobs.unpack_vec(r[6]) for r in rows if r[6]}
        for r in rules:
            v = seed_vecs.get(r["rule_id"]) if r["seed"] else vecs.get(r["rule_id"])
            if v:
                r["vec"] = v
    law = {"rules": [{k: v for k, v in r.items() if k != "vec"} for r in rules],
           "purpose": purpose_of(rules, project), "note": "",
           # the pool notices (80% / full) — the founder's "message at 80% and
           # at 100%"; computed over the WHOLE tenant's rules, not the
           # project's slice, because the pool is per scope, not per view
           "pool_notes": jobs.pool_lines(
               jobs.rules_for([r[:6] for r in rows], "", seeds=seeds), project)}
    return law, rules


def _s(body: dict, key: str, cap: int = 400) -> str:
    v = body.get(key)
    return (str(v) if v is not None else "")[:cap]


async def brief(ask: composer.Ask, fetchall, seed_vecs: dict, tenant_id: str, body: dict) -> dict:
    """op "brief": {project, part, tier, source, session_id} → {"text"}."""
    project = _s(body, "project", 120).strip()
    if not project:
        return {"ok": False, "why": "no project"}
    try:
        part = int(body.get("part") or 0)
    except (TypeError, ValueError):
        part = 0
    tier = "customer" if _s(body, "tier") == "customer" else "owner"
    law, _ = await law_for(fetchall, seed_vecs, tenant_id, project)
    text = await composer.brief(ask, law, project, part, tier, _s(body, "source", 40),
                                _s(body, "session_id", 160))
    return {"ok": True, "text": text}


async def ambient(ask: composer.Ask, fetchall, seed_vecs: dict, tenant_id: str, body: dict) -> dict:
    """op "ambient": {project, session_id, prompt, part, vector?} → {"text"}.
    The rule the prompt touches is matched HERE from the hub's table — by
    cosine when the box sent the prompt's vector, by the law's name in the
    title otherwise (hub/jobs, calibrated 2026-09-03)."""
    project = _s(body, "project", 120).strip()
    prompt = _s(body, "prompt", T.HOOK_BUDGET * 4)
    sid = _s(body, "session_id", 160)
    if not project or not prompt or not sid:
        return {"ok": True, "text": ""}
    try:
        part = int(body.get("part") or 0)
    except (TypeError, ValueError):
        part = 0
    ephemeral = bool(body.get("ephemeral"))
    vec = body.get("vector")
    vec = [float(x) for x in vec] if isinstance(vec, list) and len(vec) > 8 else None
    law, rules = await law_for(fetchall, seed_vecs, tenant_id, project, with_vecs=True)

    async def match(text: str) -> tuple[dict | None, str]:
        rule = jobs.match_rule_vec(rules, vec, text) if vec else None
        if rule is None and not vec:
            rule = jobs.match_rule(rules, text)
        if rule is not None:
            rule = {k: v for k, v in rule.items() if k != "vec"}
        return rule, "ok"

    async def purpose() -> str:
        return law["purpose"]

    # THE HISTORY BLOCK'S BAR (2026-10-15, core/composer.HISTORY_BAR): Jev
    # asks of each candidate line whether it bears on the prompt — "_jev" is
    # set by the hub from the tenant (server._serve_ask), never by the box
    spent = {"jev": 0}

    async def bears(text: str, lines: list[dict]) -> list[float] | None:
        from hub import jev
        if time.monotonic() < composer._QDOWN["until"]:
            return None                   # the breaker: the model failed lately — the nearest lines, no wait
        state = {"prompt": text[:4000],
                 "history lines": {f"e{j}": f"{d.get('day')} {d.get('kind')}: {d.get('what')}"[:600]
                                   for j, d in enumerate(lines)}}
        ps, toks = await jev.noul(state, {f"e{j}_bears": (f"Does history line e{j} bear on what the prompt asks?",
                                                          HISTORY_BEARS) for j in range(len(lines))}, fast=True)
        spent["jev"] += toks
        return [float(ps[f"e{j}_bears"]) for j in range(len(lines))] if ps else None

    # "_graph" is set by the hub from the tenant row (server._serve_ask),
    # never taken from the box
    async def questions(text: str, reply: str) -> list[str] | None:
        """B+v3's model half: the AI model writes the memory questions for the
        message, the previous reply as context only (TEXTS['recall_questions'],
        measured 2026-11-17). Metered as the tenant's 'recall_q' job."""
        from core import recallq
        from core.prompts import TEXTS
        from hub import server
        res = await server.infer({"id": tenant_id, "name": tenant_id},
                                 {"system": TEXTS["recall_questions"], "max_tokens": 300, "think": False,
                                  "prompt": f"PREVIOUS REPLY (context only):\n{reply[-9000:]}\n\n"
                                            f"USER'S MESSAGE:\n{text[:3000]}"}, job="recall_q")
        # None when the call failed — the composer's breaker opens on it; a
        # model that answered with no questions is [] and opens nothing
        return recallq.parse_questions(res.get("text") or "") if res.get("ok") else None

    # the questions are written once per prompt: the composer's recall is
    # shared by every part of the same prompt (composer.shared)
    text = await composer.ambient(ask, match, purpose, project, sid, prompt, part, scope=tenant_id,
                                  graph=bool(body.get("_graph")), ephemeral=ephemeral,
                                  bears=bears if body.get("_jev") else None,
                                  questions=questions)
    return {"ok": True, "text": text, "jev_tokens": spent["jev"]}


# the question the bench measured (scripts/ops/history_bar_bench.py, BEARS)
HISTORY_BEARS = {"true": "the line bears on what the prompt asks: an agent answering this prompt should know it — "
                         "a decision, attempt or rejection about the very thing the prompt is about",
                 "false": "the line is about something else, or only shares a word or a general topic with the prompt"}


Handler = Callable[[composer.Ask, object, dict, str, dict], Awaitable[dict]]
OPS: dict[str, Handler] = {"brief": brief, "ambient": ambient}
