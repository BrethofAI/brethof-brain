"""Agentic curation — the model SEARCHES memory instead of being handed it.

THE FOUNDER'S ARGUMENT, and it is the right one: stuffing all 1,418 records
into every prompt is pre-digesting memory for a model that is perfectly capable
of looking things up. Give it the tools and let it pull what it needs.

Three things that buys, beyond the obvious token saving:

1. IT SCALES. The catalog approach sends ~40k tokens whether or not any of it is
   relevant, and simply stops working somewhere past 10k records. Searching
   costs the same at 1,000 records as at 1,000,000.

2. IT CAN READ A RECORD BEFORE PROPOSING TO DELETE IT. The catalog shows a
   240-character preview. A deletion decided from a preview is a guess; a
   deletion decided after `get_memory` on the full text is a judgement. This is
   the opposite of a guardrail — it is giving the model the information a person
   would insist on having before the same call.

3. IT CAN CHECK ITS OWN WORK. Before inventing an id it can search for one that
   already covers the topic, which is exactly the duplicate-forking problem the
   catalog was meant to solve and only partly did.

The tools are READ-ONLY by construction — the model proposes, it never writes.
Writing stays behind `apply=true` on the endpoint, where a human decides.
"""
from __future__ import annotations

import datetime as _dt
import json

from . import curate as curate_mod
from . import inference, tools as memtools

# A RUNAWAY BACKSTOP, not a budget. Real runs settle in a handful of calls; this
# only fires if the model gets into a loop, and it is logged when it does.
MAX_TOOL_CALLS = 24

TOOLS_SPEC = [
    {"type": "function", "function": {
        "name": "search_memory",
        "description": "Keyword search over existing curated memory. Use this "
                       "FIRST to find whether a record already covers a topic, "
                       "so you can reuse its id instead of creating a duplicate.",
        "parameters": {"type": "object", "properties": {
            "query_text": {"type": "string"},
            "project": {"type": "string",
                        "description": "optional; omit to search everything"}},
            "required": ["query_text"]}}},
    {"type": "function", "function": {
        "name": "semantic_search",
        "description": "Meaning-based search over curated memory. Use when the "
                       "existing record might describe the same thing in "
                       "different words.",
        "parameters": {"type": "object", "properties": {
            "query_text": {"type": "string"}, "project": {"type": "string"},
            "top_k": {"type": "integer"}}, "required": ["query_text"]}}},
    {"type": "function", "function": {
        "name": "get_memory",
        "description": "Read ONE record in full. Do this before proposing to "
                       "delete or replace a record — a decision made from a "
                       "search snippet is a guess.",
        "parameters": {"type": "object", "properties": {
            "record_id": {"type": "string",
                          "description": "'project:id' or just 'id' with project"},
            "project": {"type": "string"}}, "required": ["record_id"]}}},
    {"type": "function", "function": {
        "name": "list_memory",
        "description": "Browse a project's record ids and titles, newest first. "
                       "Use to learn the naming conventions actually in use.",
        "parameters": {"type": "object", "properties": {
            "project": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["project"]}}},
]

_ALLOWED = {"search_memory", "semantic_search", "get_memory", "list_memory"}

AGENT_PREAMBLE = """You have TOOLS for reading the existing memory. Use them.

Work in this order:
1. SEARCH FIRST. For each significant thing the session established, search
   memory for an existing record covering it. If one exists, reuse its EXACT id
   so the record is updated rather than duplicated.
2. READ BEFORE YOU DELETE. Never propose deleting a record you have not read in
   full with get_memory. A search snippet is not enough to judge whether a
   record is wrong or merely old.
3. LEARN THE CONVENTIONS. list_memory on the project shows how ids are actually
   named here. Match what you see.
4. THEN answer with the JSON. Your FINAL message must be the JSON object and
   nothing else — no tool calls, no prose, no markdown fence.

Search costs almost nothing. Guessing costs a duplicate record or a wrong
deletion. Prefer searching."""


def curate_agentic(turns: list[dict], existing_ids: set[str],
                   project: str = "", backend=None,
                   today: str = "") -> curate_mod.CurationResult:
    """Same contract and same result type as curate.curate(), so the two are
    directly comparable on identical input."""
    backend = backend or inference.backend_from_env()
    today = today or _dt.date.today().isoformat()
    transcript = curate_mod.build_transcript(turns)
    if not transcript:
        return curate_mod.CurationResult(error="empty transcript")

    system = curate_mod.SYSTEM_FULL + "\n\n" + AGENT_PREAMBLE
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content":
         f"TODAY IS {today}. The current project is '{project or 'global'}'.\n\n"
         f"SESSION TRANSCRIPT:\n\n{transcript}\n\n"
         f"Search memory as needed, then return the JSON."},
    ]

    in_tok = out_tok = calls = 0
    trace: list[str] = []
    t0 = _dt.datetime.now()

    for _ in range(MAX_TOOL_CALLS):
        res = backend.chat(messages, tools=TOOLS_SPEC)
        if res is None:
            return curate_mod.CurationResult(error="inference unavailable")
        in_tok += int(res.get("prompt_eval_count") or 0)
        out_tok += int(res.get("eval_count") or 0)
        msg = res.get("message") or {}
        tcs = msg.get("tool_calls") or []

        if not tcs:
            text = msg.get("content") or ""
            obj = inference.parse_json_loose(text)
            if obj is None:
                return curate_mod.CurationResult(
                    error="unparseable output", model=backend.model,
                    input_tokens=in_tok, output_tokens=out_tok,
                    raw_head=text[:400],
                    seconds=(_dt.datetime.now() - t0).total_seconds())
            return _finish(obj, existing_ids, backend.model, in_tok, out_tok,
                           (_dt.datetime.now() - t0).total_seconds(), trace)

        messages.append(msg)
        for tc in tcs:
            fn = (tc.get("function") or {})
            name = fn.get("name") or ""
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            calls += 1
            if name not in _ALLOWED:
                # Read-only by construction: a model that asks to write gets a
                # refusal, not a write. It is free to ask; the answer is no.
                out = f"tool '{name}' is not available — memory is read-only here"
            else:
                try:
                    out = str(memtools.TOOLS[name](**args))[:6000]
                except Exception as e:
                    out = f"tool error: {type(e).__name__}: {str(e)[:200]}"
            trace.append(f"{name}({json.dumps(args, default=str)[:90]}) -> "
                         f"{len(out)}ch")
            messages.append({"role": "tool", "tool_name": name, "content": out})
    print(f"[curate_agent] hit MAX_TOOL_CALLS={MAX_TOOL_CALLS} without a final "
          f"answer — {calls} calls made", flush=True)
    return curate_mod.CurationResult(
        error=f"no final answer after {MAX_TOOL_CALLS} tool calls",
        input_tokens=in_tok, output_tokens=out_tok, model=backend.model)


def _finish(obj, existing_ids, model, in_tok, out_tok, secs, trace):
    """Identical validation to the one-shot path — same delete guard, same id
    cleaning — so a quality difference cannot be an artefact of looser checks."""
    raw = obj.get("records")
    kept = curate_mod._clean_records(raw)
    n_raw = len(raw) if isinstance(raw, list) else 0

    proposed, reasons = [], {}
    for x in (obj.get("delete") or [])[:curate_mod.MAX_DELETES]:
        if isinstance(x, dict):
            rid, why = str(x.get("id") or "").strip().lower(), str(x.get("reason") or "").strip()
        else:
            rid, why = str(x).strip().lower(), ""
        if rid and why:
            proposed.append(rid)
            reasons[rid] = why
    return curate_mod.CurationResult(
        records=kept, records_dropped=n_raw - len(kept),
        delete_confirmed=[i for i in proposed if i in existing_ids],
        delete_rejected=[i for i in proposed if i not in existing_ids],
        delete_reasons=reasons,
        state_update=str(obj.get("state_update") or "").strip(),
        input_tokens=in_tok, output_tokens=out_tok, seconds=secs, model=model,
        raw_head="; ".join(trace)[:400])
