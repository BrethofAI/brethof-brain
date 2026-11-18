"""THE TWO MODEL CONNECTIONS (2026-11-17, the local-first redesign).

The hub runs on the customer's machine and does every judgement with the
customer's own models, through two connections the owner sets on the
settings page:

  AI MODEL        any OpenAI-compatible chat endpoint: base URL (ending in
                  /v1, the way every client library spells it), key, model.
                  Ollama, a local vLLM, OpenAI, OpenRouter, our relay.
  DECISION MODEL  the cheap yes/no judge. Two styles:
                    "decision" — a decision-model endpoint that takes
                       {"model", "input": {"state", "questions"}} and answers
                       probabilities (Workers AI's run door, or our relay);
                    "ai" — no decision model: the AI model answers the same
                       questions. Works with anything; costs more.
                  Off: the window is judged without it, as before Jev.

Nothing here leaves the customer's machine except the calls they point it at.
Settings live in the hub's SQLite (`settings` table); this module keeps them
in CACHE so the many sync call sites (jev.on, the judge) never wait.
"""
from __future__ import annotations

import json
import os

from hub import store

CACHE: dict[str, str] = {}

KEYS = ("ai_url", "ai_key", "ai_model", "ai_reasoning", "ai_extra",
        "decision_style", "decision_url", "decision_key", "decision_model")
SECRET = ("ai_key", "decision_key")


async def load() -> None:
    CACHE.clear()
    for k, v in await store.fetchall("SELECT k, v FROM settings"):
        CACHE[k] = v


async def set_many(values: dict) -> None:
    """Write settings. A secret sent as "" or omitted keeps the old one, so
    the settings page never has to echo a key back to the browser."""
    for k, v in values.items():
        if k not in KEYS and not k.startswith(("_", "upstream:")):
            raise ValueError(f"unknown setting {k}")
        v = "" if v is None else str(v).strip()
        if k in SECRET and not v:
            continue
        if k == "ai_extra" and v:
            try:
                ok = isinstance(json.loads(v), dict)
            except ValueError:
                ok = False
            if not ok:
                raise ValueError('extra request options must be a JSON object, e.g. {"chat_template_kwargs": {"enable_thinking": false}}')
        await store.execute("INSERT INTO settings (k, v) VALUES (?, ?)"
                            " ON CONFLICT (k) DO UPDATE SET v = excluded.v", (k, v))
        CACHE[k] = v


def get(k: str, default: str = "") -> str:
    return CACHE.get(k, default)


def all_settings() -> dict:
    """Every setting as cached — the hub's own use only, never sent whole to a browser."""
    return dict(CACHE)


def ai() -> dict | None:
    """The AI model connection, or None when the owner has not set one (the
    hub then falls back to the environment's provider, if any)."""
    url = get("ai_url") or os.environ.get("HUB_AI_URL", "")
    model = get("ai_model") or os.environ.get("HUB_AI_MODEL", "")
    if not url or not model:
        return None
    return {"url": url.rstrip("/"), "key": get("ai_key") or os.environ.get("HUB_AI_KEY", ""),
            "model": model, "reasoning": get("ai_reasoning") or os.environ.get("HUB_AI_REASONING", ""),
            "extra": _extra()}


def _extra() -> dict:
    """EXTRA REQUEST OPTIONS (2026-11-17): a JSON object merged into every AI
    model request, for what one server needs and others refuse — a vLLM Qwen's
    {"chat_template_kwargs": {"enable_thinking": false}}, for one."""
    raw = get("ai_extra") or os.environ.get("HUB_AI_EXTRA", "")
    try:
        got = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return got if isinstance(got, dict) else {}


def decision() -> dict | None:
    style = (get("decision_style") or os.environ.get("HUB_DECISION_STYLE", "")).lower()
    if style == "ai":
        return {"style": "ai"} if ai() else None
    if style == "decision":
        url = get("decision_url") or os.environ.get("HUB_DECISION_URL", "")
        if not url:
            return None
        return {"style": "decision", "url": url.rstrip("/"),
                "key": get("decision_key") or os.environ.get("HUB_DECISION_KEY", ""),
                "model": get("decision_model") or os.environ.get("HUB_DECISION_MODEL", "")}
    return None


def public() -> dict:
    """What the settings page may show: everything but the keys, which are
    reported only as set or not."""
    out = {k: get(k) for k in KEYS if k not in SECRET}
    out.update({f"{k}_set": bool(get(k)) for k in SECRET})
    return out
