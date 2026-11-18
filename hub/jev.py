"""JEV — a decision model on Cloudflare Workers AI (typesafe/jev): a state and
typed questions in, a probability for every answer out, no text written.
Measured 2026-09-30 → 10-02 as the memory chain's cheap judge (never a
writer): $0.042 per M input tokens, output free.

Two jobs ride on it in the window's judgement (hub/judge.curate_window):
- the STILL-TRUE CHECK: does this window overturn an earlier record of the
  session? (GLM, four sessions, two runs each: stale 14 -> 8, key +6);
- the HISTORY DESIGN: which earlier lines the window bears on, and whether a
  new line really takes the linked question further.

Zero data retention through Cloudflare (catalog), AI Gateway logging off
(founder 2026-09-30). Customer memory flows only once the privacy page names
TypeSafe and Cloudflare — until then `HUB_JEV_TENANTS` names the tenants it
runs for (ours). Every failure degrades: the caller gets None and the window
is judged as before. The token is `HUB_JEV_TOKEN` in hub.env.
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request

ACCOUNT = os.environ.get("HUB_JEV_ACCOUNT", "3ab1da3c2109fff68aa0ad046dfa705c")
MODEL = os.environ.get("HUB_JEV_MODEL", "typesafe/jev")
TIMEOUT_S = float(os.environ.get("HUB_JEV_TIMEOUT_S", "60"))
TENANTS = frozenset(t.strip() for t in os.environ.get("HUB_JEV_TENANTS", "").split(",") if t.strip())


def on(tenant: dict) -> bool:
    """THE OWNER'S DECISION MODEL (2026-11-17, local-first): set on the settings
    page, it runs for every memory behind this hub. Without one, the old
    switch: `HUB_JEV_TENANTS=*` runs it for every tenant, otherwise only the
    tenants it names."""
    from hub import models
    if models.decision():
        return True
    return bool(os.environ.get("HUB_JEV_TOKEN")) and ("*" in TENANTS or str(tenant.get("name") or "") in TENANTS)


def _post(state, questions: dict, cfg: dict | None = None) -> dict | None:
    if cfg:
        url, key, model = cfg["url"], cfg["key"], cfg["model"] or MODEL
    else:
        url = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/ai/run"
        key, model = os.environ.get("HUB_JEV_TOKEN", ""), MODEL
    body = json.dumps({"model": model, "input": {"state": state, "questions": questions}}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "brethof-brain-hub/1",
                                          **({"Authorization": "Bearer " + key} if key else {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        # NEVER SILENT (2026-10-05: a bench hub with no resolver failed every
        # Jev call for a whole sample and said nothing — the check simply
        # never ran). The reason goes to the log; the caller still fails open.
        print(f"[jev] call failed: HTTP {e.code} {e.read()[:200]!r}", flush=True)
        return None
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"[jev] call failed: {type(e).__name__}: {str(e)[:200]}", flush=True)
        return None
    res = d.get("result") or {}
    res = res.get("result", res)
    return res if isinstance(res, dict) and "answers" in res else None


async def noul(state, questions: dict[str, tuple[str, dict]],
               fast: bool = False) -> tuple[dict[str, float] | None, int]:
    """{qid: (instructions, criteria)} -> ({qid: p(true)}, input tokens), or (None, 0) on any failure.
    `fast` (2026-11-18): a question asked while a person waits (the history
    part of a prompt) — one try on the short clock, never the retry ladder."""
    from hub import models
    cfg = models.decision()
    if cfg and cfg["style"] == "ai":
        return await _noul_ai(state, questions, fast)
    qs = {q: {"type": "noul", "instructions": ins, "criteria": crit} for q, (ins, crit) in questions.items()}
    for attempt in range(2):
        got = await asyncio.to_thread(_post, state, qs, cfg)
        if got is not None:
            ans = got.get("answers") or {}
            return ({q: float((ans.get(q) or {}).get("noul") or 0.0) for q in questions},
                    int((got.get("usage") or {}).get("input_tokens") or 0))
        await asyncio.sleep(2 * (attempt + 1))
    return None, 0


_AI_SYSTEM = ("You answer yes/no questions about a STATE. For every question id, give the "
              "probability (a number from 0 to 1) that the answer is TRUE, judged by that "
              "question's instructions and its criteria for true and false. Reply with one "
              "JSON object mapping each question id to its probability, and nothing else.")


async def _noul_ai(state, questions: dict[str, tuple[str, dict]],
                   fast: bool = False) -> tuple[dict[str, float] | None, int]:
    """NO DECISION MODEL: the owner's AI model answers the same questions
    (2026-11-17). Same contract as noul(); any failure is (None, 0) and the
    caller judges as it would without a decision model."""
    from hub import server
    asked = {q: {"instructions": ins, "criteria": crit} for q, (ins, crit) in questions.items()}
    prompt = ("STATE:\n" + json.dumps(state, ensure_ascii=False)[:60000]
              + "\n\nQUESTIONS:\n" + json.dumps(asked, ensure_ascii=False))
    res = await server.infer({"id": "_decision", "name": "decision"},
                             {"system": _AI_SYSTEM, "prompt": prompt, "json": True},
                             job="decision_fast" if fast else "decision")
    if not res.get("ok"):
        print(f"[jev] AI-model decision failed: {res.get('why')}", flush=True)
        return None, 0
    try:
        from core.inference import parse_json_loose
        got = parse_json_loose(res.get("text") or "")
    except Exception:                                               # noqa: BLE001
        got = None
    if not isinstance(got, dict):
        print("[jev] AI-model decision: no JSON object in the answer", flush=True)
        return None, 0
    out = {}
    for q in questions:
        try:
            out[q] = min(1.0, max(0.0, float(got.get(q, 0.0))))
        except (TypeError, ValueError):
            out[q] = 0.0
    return out, int(res.get("input_tokens") or 0)
