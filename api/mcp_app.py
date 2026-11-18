"""Remote MCP endpoint — JSON-RPC over Streamable HTTP, no framework.

Written directly rather than via FastMCP so the dependency tree stays small and,
more importantly, so every tool call runs inside the tenant binding that
`resolve_caller` establishes. That binding is the isolation chokepoint: a tool
that somehow ran outside it would hit `store.NoTenantBound` and raise, not quietly
read someone else's memory.

The tool names, arguments and descriptions match v1's exactly. That is what lets
a client point at either server without touching its config.
"""
from __future__ import annotations

import traceback

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from core import metering, mode, notices, quota, store, tools
from . import customer_tools
from .main import (resolve_caller, resolve_ents_dep, resolve_plan_dep,
                   resolve_state_dep,
                   resolve_scopes_dep, resolve_tier)

router = APIRouter()

PROTOCOL_VERSION = "2024-11-05"
# "brain", not "brethof-brain": the harness stamps this name into every
# tool id the model reads (mcp__brain__search_brain) — the server name IS
# the product keyword on the wire (founder, 2026-08-10).
SERVER_INFO = {"name": "brain", "version": "2.0"}

_STR = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


TOOL_SPECS: dict[str, tuple[str, dict]] = {
    "recall": ("THE default search of the Brain: hybrid vector+BM25 over curated records "
               "AND the chat archive, RRF-fused with graph reranking. Start here. "
               "since/until (YYYY-MM-DD, until inclusive) limit it to a period.",
               _schema({"query_text": _STR, "project": _STR, "top_k": _INT,
                        "include_chat": _BOOL, "since": _STR, "until": _STR}, ["query_text"])),
    "save_verdicts": ("Keep a judge's calls on a period so the next run can re-judge them "
                      "(evaluating work: what was wasted, what held). items: a list of "
                      "{project, item, verdict, reason, line_ids} — line_ids are the #ids of the "
                      "decision lines the call rests on. A re-judgement is a new call on the same "
                      "item, never an edit. since required (YYYY-MM-DD), until inclusive.",
                      _schema({"items": {"type": "array", "items": {"type": "object"}},
                               "since": _STR, "until": _STR, "judged_by": _STR}, ["items", "since"])),
    "list_verdicts": ("Earlier calls on periods overlapping since–until, each item's history "
                      "together, oldest first — read them before judging a period again.",
                      _schema({"since": _STR, "until": _STR, "project": _STR}, [])),
    "period_report": ("What a period produced, counted in the memory — facts only: "
                      "decisions by kind, reversals, reopened questions, records new/"
                      "changed/retired, ledger opened/closed, messages and sessions, "
                      "tokens, per project. since required (YYYY-MM-DD), until inclusive.",
                      _schema({"since": _STR, "until": _STR, "project": _STR}, ["since"])),
    "load_project": ("Dump a project's recent curated records.",
                     _schema({"project": _STR}, ["project"])),
    "session_context": ("The complete session-start context (rules, projects, "
                        "notes, plan, last-session recap) in one "
                        "call. Use when a '(context N/M)' part is missing "
                        "from your session start, or to refresh mid-session.",
                        _schema({"project": _STR}, ["project"])),
    "search_memory": ("Keyword (BM25-style, IDF-weighted) search over the curated Brain.",
                      _schema({"query_text": _STR, "project": _STR}, ["query_text"])),
    "semantic_search": ("Vector (meaning) search over the curated Brain.",
                        _schema({"query_text": _STR, "project": _STR, "top_k": _INT},
                                ["query_text"])),
    "search_chat": ("Vector search over the full chat-history archive.",
                    _schema({"query_text": _STR, "project": _STR, "top_k": _INT},
                            ["query_text"])),
    "search_chat_text": ("Keyword search over the chat archive — exact strings "
                         "(paths, errors, hashes). Quoted phrases stay together.",
                         _schema({"query_text": _STR, "project": _STR, "top_k": _INT},
                                 ["query_text"])),
    "get_memory": ("Fetch ONE record's full content by id.",
                   _schema({"record_id": _STR, "project": _STR}, ["record_id"])),
    "list_memory": ("Browse a project's record ids/titles.",
                    _schema({"project": _STR, "memory_type": _STR, "limit": _INT},
                            ["project"])),
    "recent_records": ("Recent structured rows, scoped/filtered. `where` takes ONE "
                       "field=value filter.",
                       _schema({"project": _STR, "days": _INT, "where": _STR,
                                "limit": _INT}, ["project"])),
    "query_raw": ("Read-only SQL (SELECT/WITH only) for graph traversal and "
                  "aggregates. Confined to your database, 5 s timeout, 500 rows.",
                  _schema({"sql": _STR}, ["sql"])),
    "cleanup_history": ("Prune old chat-archive history for one project "
                        "(90-day minimum age — the recent archive is what "
                        "the Brain works from, and nothing younger can be "
                        "deleted through this connection). Preview by "
                        "default; deletes only with confirm=<project>. "
                        "Queued; disk is reclaimed and the metered size "
                        "drops. Saved memories are never touched.",
                        _schema({"project": _STR, "older_than_days": _INT,
                                 "confirm": _STR}, ["project"])),
    "memory_health": ("Coverage per project (records / embedded) and the embedding "
                      "backlog.", _schema({"project": _STR}, [])),
    "save_note": ("LEAVE A HANDOVER — where this thread of work stands, what "
                  "is live, what is next — for whoever picks the project up. "
                  "One note per session per project (writing again replaces "
                  "it); the project keeps its last five sessions' notes and "
                  "starts every session with them. Pass session_id exactly "
                  "as the BRAIN header gives it. A note for ANOTHER project "
                  "files there and greets that project's next session. "
                  "Body capped at 2,000 chars: a pick-up, not an essay. "
                  "Records are written by the curator from what you SAY — "
                  "this is the only thing you write by hand.",
                  _schema({"project": _STR, "session_id": _STR, "title": _STR,
                           "body": _STR},
                          ["project", "session_id", "title", "body"])),
    "delete_note": ("Delete one session's handover note from a project.",
                    _schema({"project": _STR, "session_id": _STR},
                            ["project", "session_id"])),
    "save_memory": ("REMEMBER something — a fact, a decision, a measurement, "
                    "a finding, what a tool showed you. It goes into the "
                    "project's history at once (immutable, searchable within "
                    "a minute) and the Brain decides whether it becomes a "
                    "record every session is handed. This never writes a "
                    "record itself. A RULE goes through save_rule; a "
                    "procedure through save_playbook; your handover through "
                    "save_note.",
                    _schema({"project": _STR, "content": _STR, "title": _STR},
                            ["project", "content"])),
    "move_session": ("THE PROJECT-SPLIT DOOR: move an archived session, or a "
                     "turn range of it (turn_lo/turn_hi), to the project it "
                     "belongs to. The chat rows never change — an ownership row "
                     "says where they belong now and every reader (recall, "
                     "search, the brief, backfill, export) consults it; records "
                     "(by origin), ledger rows and the session's note follow. "
                     "Reversible by moving back. "
                     "With no project: where the session is today and its "
                     "move history.",
                     _schema({"session_id": _STR, "project": _STR, "turn_lo": _STR,
                              "turn_hi": _STR, "why": _STR}, ["session_id"])),
    "dismiss_proposal": ("The proposer suggested another project for a session "
                         "(the brief's SESSIONS THE PROPOSER WOULD FILE ELSEWHERE, "
                         "or the per-prompt line); this keeps the session where it "
                         "is filed and stops the reminder. move_session is the other answer.",
                         _schema({"session_id": _STR, "why": _STR}, ["session_id"])),
    "list_saved": ("What was put away with save_memory in a project, newest "
                   "first — the saved turns of its history. Read-only: a "
                   "correction is another save.",
                   _schema({"project": _STR, "limit": _INT}, ["project"])),
    "update_memory": ("EDIT one EXISTING curated record in place — the "
                      "correction door. It never creates: new records are "
                      "written by the curator from what you SAY in the "
                      "conversation.",
                      _schema({"project": _STR, "record_id": _STR, "title": _STR,
                               "content": _STR, "memory_type": _STR},
                              ["project", "record_id", "title", "content"])),
    "save_rule": ("SAVE A RULE — the one door to the rules that are injected "
                  "into every session and TEACH each project's curator. It "
                  "asks ONE question back (must this text load into every "
                  "session's context?) — answer it in a second call with "
                  "answer= and the token= from the reply, and your answer "
                  "decides where it lands: every-project law, this "
                  "project's law, or knowledge (found when searched). Pass "
                  "project= when you know it. If a very similar rule "
                  "already exists you will be asked whether to merge into "
                  "it. Rules are capped at 1,000 chars — state the rule, "
                  "then the why; detail goes in a record the rule points at.",
                  _schema({"rule_id": _STR, "title": _STR, "rule": _STR,
                           "project": _STR, "answer": _STR, "token": _STR,
                           "merge": _STR},
                          ["rule_id", "title", "rule"])),
    "delete_memory": ("DELETE one curated record and its edges. The curated Brain is "
                      "disposable — a dead record is a deleted record. Cannot "
                      "touch the chat archive.",
                      _schema({"project": _STR, "record_id": _STR}, ["record_id"])),
    "supersede_memory": ("Replace a record with its successor: the OLD record "
                         "is DELETED — the curated Brain holds current truth "
                         "only. History survives in the chat archive.",
                         _schema({"project": _STR, "old_record_id": _STR,
                                  "new_record_id": _STR},
                                 ["project", "old_record_id", "new_record_id"])),
    "save_commit": ("Record a git commit into the project's Brain.",
                    _schema({"project": _STR, "commit_hash": _STR, "message": _STR,
                             "files_changed": _STR, "branch": _STR},
                            ["project", "commit_hash", "message", "files_changed"])),
    "add_project": ("Create a project WITH its onboarding. purpose is "
                    "REQUIRED: 1-2 sentences — what the project IS and what "
                    "should be remembered there; rules optional (what to always track "
                    "/ never record). These become the project's rules and its "
                    "specialist curator brief compiles from them within "
                    "minutes. A name ending in '%' (e.g. 'u_%') creates a "
                    "PATTERN TEMPLATE: every project matching it inherits "
                    "one compiled brief — for apps that create a project "
                    "per end-user.",
                    _schema({"project": _STR, "purpose": _STR, "rules": _STR},
                            ["project", "purpose"])),
    "list_ledger": ("The BRAIN LEDGER — the CATCHER. It fills ITSELF from "
                    "conversation: everything said in passing that nobody "
                    "acted on lands here, so with hundreds of conversations "
                    "nothing voiced is ever lost. Nobody saves to it — not "
                    "you, not the human. READ IT when a chunk of work "
                    "finishes or begins: first delete what is dead "
                    "(completed, abandoned, wrong, duplicate — delete is "
                    "the ONLY operation), then look at what remains: those "
                    "are the things the team missed or forgot — pick up "
                    "what should happen now, leave the rest to wait.",
                    _schema({"project": _STR}, ["project"])),
    "remove_from_ledger": ("DELETE Brain Ledger rows by id (comma-separated) — "
                           "the ledger's only operation. Completed, "
                           "abandoned, wrong, duplicate: all just delete. "
                           "Deleting is how the ledger stays readable; "
                           "an idea deleted too early files itself again "
                           "the next time someone says it.",
                           _schema({"ids": _STR, "project": _STR},
                                   ["ids", "project"])),
    "save_playbook": ("Save a BRAIN PLAYBOOK — the user's own procedure "
                      "(script, command sequence, steps), stored "
                      "tenant-wide so ANY project can fetch and run it. "
                      "Write it as a MARKDOWN document: headings for "
                      "stages, fenced code blocks for commands. Upsert: "
                      "same name = edit, said loudly.",
                      _schema({"name": _STR, "content": _STR,
                               "annotation": _STR}, ["name", "content"])),
    "get_playbook": ("Fetch one Brain playbook (a markdown document, "
                     "served verbatim) to RUN IT YOURSELF — the Brain "
                     "stores procedures, it never executes.",
                     _schema({"name": _STR}, ["name"])),
    "list_playbooks": ("List the Brain playbooks — names, annotations, "
                       "sizes.", _schema({}, [])),
    "delete_playbook": ("Delete one Brain playbook by name.",
                        _schema({"name": _STR}, ["name"])),
}

# ── customer tier: intents in, results out, mechanism invisible ────────────
# Descriptions state CAPABILITY only. Founder 2026-07-28: the customer never
# sees the filing scheme — no index, no id conventions, no storage or
# retrieval vocabulary. Saving is asynchronous because a service files it.
CUSTOMER_SPECS: dict[str, tuple[str, dict]] = {
    "list_projects": ("List your projects and how many memories each holds.",
                      _schema({}, [])),
    "session_context": ("Your complete session-start briefing (rules, "
                        "projects, current state, open loops, last-session "
                        "recap) in one call. Use it if any '(context N/M)' "
                        "part is missing from your session start, or to "
                        "refresh mid-session.",
                        _schema({"project": _STR}, ["project"])),
    "add_project": ("Create a new project space and tell your Brain what "
                    "it is for. purpose is REQUIRED: 1-2 sentences — what "
                    "this project IS and what should be remembered about "
                    "it; rules optional (what to always record / what never "
                    "to record). This teaches your Brain what matters here "
                    "from day one. A name ending in '%' (e.g. 'u_%') "
                    "creates a shared template: every project matching the "
                    "pattern is remembered the same way — for apps that "
                    "keep one project per end-user.",
                    _schema({"project": _STR, "purpose": _STR, "rules": _STR},
                            ["project", "purpose"])),
    # delete_project left this surface 2026-08-12 (founder): deleting a whole
    # project — including its conversation history — is a HUMAN act, done in
    # the account panel. No agent-reachable verb touches the archive.
    "move_session": ("Move an archived session — or a turn range of it (turn_lo, "
                     "turn_hi) — to the project it belongs to. Your conversation "
                     "rows never change; a row says where they belong now and every "
                     "reader follows it: recall, search, the session brief "
                     "and the ledger. With no project: where the session is filed "
                     "today and its move history. Reversible by moving back.",
                     _schema({"session_id": _STR, "project": _STR, "turn_lo": _STR,
                              "turn_hi": _STR, "why": _STR}, ["session_id"])),
    "dismiss_proposal": ("The Brain suggested another project for a session (the "
                         "brief's SESSIONS THE PROPOSER WOULD FILE ELSEWHERE, or the "
                         "line at the top of a prompt). This keeps the session where "
                         "it is filed and stops the reminder; move_session is the "
                         "other answer.",
                         _schema({"session_id": _STR, "why": _STR}, ["session_id"])),
    "save_note": ("LEAVE A HANDOVER for whoever picks this project up — "
                  "where this thread of work stands, what is live, what is "
                  "next. One note per session per project (writing again "
                  "replaces it); the project keeps its last five sessions' "
                  "notes and starts every session with them. Pass "
                  "session_id exactly as the BRAIN header gives it. A note "
                  "for ANOTHER project files there. Body capped at 2,000 "
                  "chars. Your Brain writes its own records from what you "
                  "say — this is the one thing you write by hand.",
                  _schema({"project": _STR, "session_id": _STR, "title": _STR,
                           "body": _STR},
                          ["project", "session_id", "title", "body"])),
    "delete_note": ("Delete one session's handover note from a project.",
                    _schema({"project": _STR, "session_id": _STR},
                            ["project", "session_id"])),
    "update_record": ("EDIT one EXISTING brain record in place — the "
                      "correction door. It never creates: new records are "
                      "written by your Brain from what you say.",
                      _schema({"record_id": _STR, "title": _STR, "content": _STR,
                               "project": _STR}, ["record_id", "title", "content"])),
    "save_general": ("Remember something true across all projects — a fact, "
                     "a decision, a finding. It goes into your history at "
                     "once (searchable within a minute) and your Brain keeps "
                     "a record of it if a future session must be handed it. "
                     "(A standing CONSTRAINT is a RULE: save_rule.)",
                     _schema({"content": _STR}, ["content"])),
    "save_project": ("Remember something about one project — a fact, a "
                     "decision, a measurement, what a tool showed you. It goes "
                     "into the project's history at once (searchable within a "
                     "minute) and your Brain keeps a record of it if a future "
                     "session must be handed it. (A procedure is a PLAYBOOK: "
                     "save_playbook. Your handover is a NOTE: save_note.)",
                     _schema({"content": _STR, "project": _STR},
                             ["content", "project"])),
    "list_saved": ("What was put away with save_project / save_general in a "
                   "project, newest first. Read-only: a correction is "
                   "another save.",
                   _schema({"project": _STR, "limit": _INT}, ["project"])),
    "save_rule": ("Save a RULE — a standing constraint on how work is done. "
                  "One question comes back: must this text load into every "
                  "session's context? Answer it in a second call (answer= "
                  "plus the token= from the reply) and your answer decides "
                  "where it lands — every-project rule, one project's rule, "
                  "or knowledge that is found when searched. Pass project= "
                  "when you know it. Rules are capped and pooled.",
                  _schema({"content": _STR, "project": _STR, "answer": _STR,
                           "token": _STR}, ["content"])),
    "list_rules": ("See every saved rule (optionally those active for one "
                   "project).", _schema({"project": _STR}, [])),
    "list_brain": ("Browse a project's saved brain records — ids and titles.",
                    _schema({"project": _STR, "memory_type": _STR,
                             "limit": _INT}, ["project"])),
    "get_record": ("Read ONE brain record in full by its id.",
                   _schema({"record_id": _STR, "project": _STR},
                           ["record_id"])),
    "delete_record": ("Delete ONE brain record that is wrong or no longer wanted. "
                      "Also the way to remove a RULE: "
                      "project=\"rules\" with the id from list_rules — dead "
                      "rules are yours to clear and deletes are always free. "
                      "Conversation history is never affected.",
                      _schema({"record_id": _STR, "project": _STR},
                              ["record_id"])),
    "begin_turn": ("CALL FIRST ON EVERY USER MESSAGE, before you answer: pass the "
                   "user's message (and session_id from your previous begin_turn, if "
                   "any). Returns what your memory knows that bears on it — use it in "
                   "your answer — and the session_id for end_turn.",
                   _schema({"message": _STR, "project": _STR, "session_id": _STR},
                           ["message"])),
    "end_turn": ("CALL LAST ON EVERY USER MESSAGE, after you answer: pass your full "
                 "reply and the session_id from begin_turn, so this exchange is "
                 "kept in memory.",
                 _schema({"reply": _STR, "session_id": _STR, "project": _STR},
                         ["reply", "session_id"])),
    "search_brain": ("Search the Brain — the current truth, kept clean. "
                      "Start here. since/until (YYYY-MM-DD, until inclusive) limit it to "
                      "records changed in that period; a period with no query_text lists them.",
                      _schema({"query_text": _STR, "project": _STR,
                               "top_k": _INT, "since": _STR, "until": _STR}, [])),
    "search_history": ("Search full conversation history — complete but raw; "
                       "statements may be outdated. Use to recover detail "
                       "search_brain does not hold. since/until (YYYY-MM-DD, until "
                       "inclusive) limit it to what was said in that period; a period with "
                       "no query_text lists the period's decisions in order.",
                       _schema({"query_text": _STR, "project": _STR,
                                "top_k": _INT, "since": _STR, "until": _STR}, [])),
    "save_verdicts": ("Keep a judge's calls on a period so the next run can re-judge them "
                      "(evaluating work: what was wasted, what held). items: a list of "
                      "{project, item, verdict, reason, line_ids} — line_ids are the #ids of the "
                      "decision lines the call rests on. A re-judgement is a new call on the same "
                      "item, never an edit. since required (YYYY-MM-DD), until inclusive.",
                      _schema({"items": {"type": "array", "items": {"type": "object"}},
                               "since": _STR, "until": _STR, "judged_by": _STR}, ["items", "since"])),
    "list_verdicts": ("Earlier calls on periods overlapping since–until, each item's history "
                      "together, oldest first — read them before judging a period again.",
                      _schema({"since": _STR, "until": _STR, "project": _STR}, [])),
    "period_report": ("What a period produced, counted in the memory — facts only: "
                      "decisions by kind (decided/tried/rejected/dropped), reversals, "
                      "reopened questions, records new/changed/retired, ledger opened/"
                      "closed, messages and sessions, per project. For 'what did we do "
                      "this week/month' and evaluating work. since required (YYYY-MM-DD), "
                      "until inclusive, project optional.",
                      _schema({"since": _STR, "until": _STR, "project": _STR}, ["since"])),
    "cleanup_history": ("Free up space: remove one project's conversation "
                        "history older than a cutoff (minimum 90 days — the "
                        "last 3 months always stay, and nothing younger can "
                        "be removed through this connection). Shows a "
                        "preview first; removes only with confirm=<project "
                        "name>. Removal is free and outright. Saved "
                        "memories are never touched.",
                        _schema({"project": _STR, "older_than_days": _INT,
                                 "confirm": _STR}, ["project"])),
    "list_ledger": ("Your BRAIN LEDGER — the CATCHER. It fills ITSELF "
                    "from your conversations: things said in passing that "
                    "nobody acted on land here on their own, so nothing "
                    "voiced is ever lost. Nobody saves to it. READ IT when "
                    "a chunk of work finishes or begins: with your human, "
                    "first delete what is dead (completed, abandoned, no "
                    "longer wanted, duplicate — delete is the ONLY "
                    "operation), then look at what remains — those are the "
                    "things you both missed or forgot. Pick up what should "
                    "happen now; the rest simply waits.",
                    _schema({"project": _STR}, ["project"])),
    "remove_from_ledger": ("DELETE Brain Ledger rows by id (comma-separated, "
                           "from list_ledger) — the ledger's only "
                           "operation. Completed, abandoned, wrong, "
                           "duplicate: all just delete. An idea deleted "
                           "too early is safe — it files itself again the "
                           "next time someone says it.",
                           _schema({"ids": _STR, "project": _STR},
                                   ["ids", "project"])),
    "save_playbook": ("Save a BRAIN PLAYBOOK — your own procedure (a "
                      "script, a command sequence, ordered steps) stored "
                      "once for your whole account, so any project's agent "
                      "can fetch and run it later. Write it as a MARKDOWN "
                      "document: headings for stages, fenced code blocks "
                      "for commands. Same name = edit; an overwrite is "
                      "announced. The Brain stores playbooks; it never "
                      "runs them.",
                      _schema({"name": _STR, "content": _STR,
                               "annotation": _STR}, ["name", "content"])),
    "get_playbook": ("Fetch one Brain playbook in full — then run it "
                     "yourself in your own environment. \"Run the X "
                     "playbook\" means: get it here, execute it where you "
                     "are.",
                     _schema({"name": _STR}, ["name"])),
    "list_playbooks": ("See your Brain playbooks — names, annotations, "
                       "sizes, so any project's agent knows what proven "
                       "procedures exist.", _schema({}, [])),
    "delete_playbook": ("Delete one of your Brain playbooks by name.",
                        _schema({"name": _STR}, ["name"])),
}

# Self-hosted keeps the local-only delete verb (the agent-task tools went
# with the modes on 2026-09-10 — every judgement is the hub's).
if mode.is_selfhosted():
    if not mode.panel_ops():
        # TRUE-LOCAL ONLY (founder 2026-08-31): on your own machine the
        # agent owns the box anyway — refusing this verb only pushes the
        # same agent into improvised SQL that skips the careful deletion.
        # On our hosted stacks (panel_ops) the boundary is real and this
        # verb belongs to the panel, where a human acts.
        _LOCAL_DELETE_SPEC = {
            "delete_project": (
                "Delete an ENTIRE project from the Brain — its saved "
                "memory, graph, ledger rows AND its full conversation "
                "history, permanently. The one verb that reaches the chat "
                "archive at any age, so it exists only on a self-hosted "
                "box you own. confirm must repeat the project name "
                "exactly — ask your human before calling this.",
                _schema({"project": _STR, "confirm": _STR},
                        ["project", "confirm"])),
        }
        TOOL_SPECS.update(_LOCAL_DELETE_SPEC)
        CUSTOMER_SPECS.update(_LOCAL_DELETE_SPEC)


def _selfhosted_nudge(tool_name: str) -> str:
    """Nothing to nudge since design step 3 (2026-09-10): the agent-task
    machinery (merge, heal offered to the customer's own model) went with
    the modes — every judgement is the hub's. Kept as the one seam the tool
    results pass through, empty."""
    return ""


# MCP TOOL HINTS (2026-10-23): a cloud agent's client (claude.ai) sorts a
# connector's tools by these hints into read-only and write/delete groups and
# sets each group's approval. Sent without them, all 27 tools fell in one
# "Other tools" group at "Needs approval", and an agent working at night
# stopped on every call. Honest hints: a read is read-only, a save adds and
# never destroys, a delete or overwrite is destructive. Unknown names (new
# tools) fall to the cautious side.
_READ_ONLY = {
    "list_projects", "session_context", "list_saved", "list_rules", "list_brain",
    "get_record", "search_brain", "search_history", "list_ledger", "get_playbook",
    "list_playbooks", "recall", "search_memory", "search_chat", "search_chat_text", "period_report",
    "semantic_search", "get_memory", "list_memory", "recent_records", "memory_health",
    "query_raw", "load_project", "period_report", "list_verdicts",
}
_ADDITIVE = {
    "begin_turn", "end_turn", "add_project", "save_general", "save_project", "save_note", "save_verdicts",
    "save_rule", "save_general_rule", "save_project_rule", "save_playbook", "save_memory",
    "save_commit", "move_session", "dismiss_proposal",
}


def _hints(name: str) -> dict:
    if name in _READ_ONLY:
        return {"readOnlyHint": True, "destructiveHint": False,
                "idempotentHint": True, "openWorldHint": False}
    if name in _ADDITIVE:
        return {"readOnlyHint": False, "destructiveHint": False,
                "idempotentHint": False, "openWorldHint": False}
    return {"readOnlyHint": False, "destructiveHint": True,
            "idempotentHint": False, "openWorldHint": False}


_TIER_SPECS = {"owner": TOOL_SPECS, "customer": CUSTOMER_SPECS}
_TIER_TOOLS = {"owner": tools.TOOLS,
               "customer": customer_tools.CUSTOMER_TOOLS}


def _instructions(tier: str) -> str:
    """Tenant-aware server instructions, built at handshake time. Counts are
    best-effort — a fresh or failing tenant still gets the doctrine."""
    r_count = p_count = None
    try:
        # Rules live only on the hub since 2026-09-02; this counted the old
        # box table, so every handshake said "0 rules" while six pages of
        # law loaded (found 2026-09-23). No hub, no count — never a false 0.
        from core import rules_hub
        law = rules_hub.fetch("")
        if not law["note"]:
            r_count = sum(1 for r in law["rules"] if not r.get("seed")
                          and (r.get("audience") or "session") != "curator")
    except Exception:
        traceback.print_exc()
    try:
        with store.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM project "
                        "WHERE name NOT IN ('global','rules','state');")
            p_count = cur.fetchone()[0]
    except Exception:
        traceback.print_exc()
    have = (f"This Brain currently holds {r_count} rules across {p_count} "
            f"projects. " if r_count is not None and p_count is not None
            else "")
    if tier == "owner":
        return (
            "This server is the team's BRAIN — persistent memory shared by every "
            "agent and session.\n\n"
            "THE MODEL: the Brain is organized into PROJECTS. A project is an "
            "isolated space of its own — its own records, its own knowledge "
            "graph, and its own individually-taught curator that learns "
            "what matters there. For coding agents a project usually maps "
            "to one working folder/repo; an app may instead keep one "
            "project per end-user or topic. 'global' is the thin shared "
            "layer BETWEEN projects — only facts true everywhere.\n\n"
            f"RULES EXIST — read them, write them. {have}There are exactly "
            "TWO KINDS: GENERAL rules (meta.area='all' — a rule in every "
            "project) and PROJECT rules (meta.area=<project> — that "
            "project's own). Both load at session start: the general "
            "set plus the current project's. Rules are conventions agents "
            "must follow and statements of what each project is; they also "
            "TEACH each project's curator — save one and that project's "
            "curator re-learns within minutes.\n"
            "RULES HAVE ONE DOOR: save_rule(rule_id, title, rule, "
            "project=<when known>). It asks ONE question back — must this "
            "text load into every session's context? — and your answer "
            "routes it: every-project law, this project's law, or "
            "knowledge. Answer honestly; option 3 still saves, as "
            "knowledge, found when searched. Nothing else writes rules, on "
            "purpose. Filing knowledge as a rule is "
            "what silts a brain up: it injects forever, teaches every "
            "curator, and outlives the tool it describes. Rules are capped "
            "at 1K chars and pooled per scope; merge twins into one id and "
            "delete dead rules freely — the system never edits your rules "
            "itself.\n\n"
            "NEW PROJECT: add_project(project=\"<name>\", purpose=\"one "
            "sentence: what it is and what should be remembered there\") — the "
            "purpose teaches the Brain what matters there from day one.\n\n"
            "WORKING: recall(query, project) FIRST — the curated Brain is the "
            "current truth; the chat archive holds everything raw.")
    return (
        "This server is your BRAIN — persistent memory shared by every agent and "
        "session on this account.\n\n"
        "THE MODEL: the Brain is organized into PROJECTS. A project is an "
        "isolated space of its own — its own saved knowledge, its own knowledge "
        "graph, and its own individually-taught curator that learns what "
        "matters there. For coding agents a project usually maps to one "
        "working folder/repo; an app may instead keep one project per "
        "end-user or topic. The general space is the thin shared layer BETWEEN "
        "projects — only facts true everywhere.\n\n"
        f"RULES EXIST — read them, write them. {have}There are exactly TWO "
        "KINDS: GENERAL rules (a rule in every project) and PROJECT rules "
        "(one project's own). Both load at session start: the general set "
        "plus the current project's. Rules are conventions your team sets "
        "and statements of what each project is; they also TEACH each "
        "project's curator — save a project rule and that project's "
        "curator re-learns within minutes. See them: list_rules.\n"
        "RULES HAVE ONE DOOR: save_rule(content, project=<when known>). It "
        "asks ONE question back — must this text load into every "
        "session's context? — and your answer routes it: every-project "
        "rule, one project's rule, or knowledge, found the moment it is "
        "searched. Answer "
        "honestly; option 3 still saves. Filing knowledge as a rule is the "
        "classic mistake: it loads into every session forever, teaches "
        "every curator, and outlives the tool it described. Rules have "
        "budgets (1K chars each, pooled per scope) and every char loads "
        "every session — keep rules short and current: merge twins into "
        "one rule, delete dead rules freely (deletes are always free).\n"
        "YOUR RULES ARE YOURS. The service never edits, merges, trims or "
        "deletes a rule of yours — it only ADVISES, and it refuses a write "
        "it cannot accept (over 1K chars, or near-identical to a rule you "
        "already have) with the reason and the fix, while you are still "
        "there to act on it. Cleaning up is yours to do, and it is two "
        "calls: list_rules shows every rule with its id, and "
        "delete_record(record_id=\"<id>\", project=\"rules\") removes one. "
        "To merge twins, write the single combined rule and delete the "
        "others. Nothing is done to your rules behind your back.\n\n"
        "NEW PROJECT: add_project(project=\"<name>\", purpose=\"one "
        "sentence: what it is and what should be remembered there\") — the "
        "purpose teaches the mind what matters there from day one.\n\n"
        "WORKING: search_brain FIRST — the curated Brain is the current "
        "truth; search_history is the raw archive when you need detail. "
        "Save what your team decides and learns; the Brain files it.")


def _result(req_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": code, "message": message}}


def _handle(msg: dict, tier: str = "owner") -> dict | None:
    """One JSON-RPC message. Returns None for a notification (no id).

    `tier` decides the visible AND callable toolset. The lookup below is the
    enforcement point, not just the listing: a customer key naming an owner
    tool gets 'unknown tool', indistinguishable from a tool that never
    existed. Unknown tiers fail closed to the customer surface."""
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}
    specs = _TIER_SPECS.get(tier, CUSTOMER_SPECS)
    fns = _TIER_TOOLS.get(tier, customer_tools.CUSTOMER_TOOLS)

    if method == "initialize":
        return _result(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            # The FRONT DOOR (teach-by-mirror, 2026-08-06): instructions
            # ride the handshake into the agent's system context on every
            # MCP client — the one teaching surface impossible to miss.
            "instructions": _instructions(tier)})
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return _result(req_id, {})
    if method == "tools/list":
        return _result(req_id, {"tools": [
            {"name": n, "description": d, "inputSchema": s, "annotations": _hints(n)}
            for n, (d, s) in specs.items()]})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = fns.get(name)
        if not fn:
            return _error(req_id, -32602, f"unknown tool '{name}'")
        metering.count_tool(str(name))     # instrumentation, never blocking
        # PER-ACCOUNT RATE (2026-08-24): tools draw the same bucket as the
        # hooks — the account is the unit, keys never multiply the budget.
        # Fails OPEN like the door below.
        try:
            from core import ratelimit
            ok_rl, retry = ratelimit.allow(store.current_tenant(),
                                           customer_tools.PLAN.get())
            if not ok_rl:
                return _result(req_id, {
                    "content": [{"type": "text",
                                 "text": ratelimit.NOTICE +
                                 f" (retry in ~{retry}s)"}],
                    "isError": True})
        except Exception:
            traceback.print_exc()
        # THE DOOR (founder 2026-08-25, replacing full-or-off): a closed
        # account keeps the tools that READ its memory and the tools that
        # DELETE from it — see customer_tools.LIMITED_TOOLS for why each.
        # Everything that writes gets the wall notice. A tool absent from
        # that set is refused by default, so a future tool is closed until
        # someone decides otherwise. The door itself fails OPEN.
        try:
            state, notice = quota.door(customer_tools.PLAN.get(),
                                       ents=customer_tools.ENTS.get(),
                                       state=customer_tools.STATE.get())
            if state != "active" and name not in customer_tools.LIMITED_TOOLS:
                return _result(req_id, {
                    "content": [{"type": "text", "text": notice}],
                    "isError": True})
        except Exception:
            traceback.print_exc()
        try:
            text = fn(**args)
        except TypeError as e:
            return _error(req_id, -32602, f"bad arguments for {name}: {e}")
        except Exception:
            # Loud in the logs, generic to the caller: internal SQL and paths
            # must not travel back over a customer connection.
            traceback.print_exc()
            return _result(req_id, {
                "content": [{"type": "text", "text": "memory backend error"}],
                "isError": True})
        # THE BACK-CHANNEL (2026-08-08). Saves are intents filed later, so
        # when the filing FAILS there is no response left to fail — the caller
        # was answered a minute ago. Any tool result will do as the carrier:
        # the next call the agent makes brings the news, with the fix in it.
        return _result(req_id, {
            "content": [{"type": "text",
                         "text": str(text) + notices.drain()
                                 + _selfhosted_nudge(name)}],
            "isError": False})
    return _error(req_id, -32601, f"method not found: {method}")


def _dispatch(tenant: str, tier: str, scopes, plan, ents, sub_state, body):
    """Runs in a worker thread. The tenant is bound HERE, inside the thread that
    will actually execute the query — a contextvar set on the event loop does
    not reliably follow work into a threadpool, and an unbound tenant must never
    become an ambient one. Scopes and the billing plan ride the same thread the
    tools execute on."""
    tok = store.bind(tenant)
    stok = customer_tools.SCOPES.set(scopes)
    ptok = customer_tools.PLAN.set(plan)
    etok = customer_tools.ENTS.set(ents)
    sttok = customer_tools.STATE.set(sub_state)
    try:
        if isinstance(body, list):                       # JSON-RPC batch
            return [r for r in (_handle(m, tier) for m in body)
                    if r is not None]
        return _handle(body, tier)
    finally:
        customer_tools.STATE.reset(sttok)
        customer_tools.ENTS.reset(etok)
        customer_tools.PLAN.reset(ptok)
        customer_tools.SCOPES.reset(stok)
        store.unbind(tok)


@router.post("/v1/mcp")
@router.post("/mcp")
async def mcp_endpoint(request: Request, tenant: str = Depends(resolve_caller),
                       tier: str = Depends(resolve_tier),
                       scopes=Depends(resolve_scopes_dep),
                       plan=Depends(resolve_plan_dep),
                       ents=Depends(resolve_ents_dep),
                       sub_state=Depends(resolve_state_dep)):
    """Raw Request, not a typed body: FastAPI reads a `dict | list` annotation
    as form data and refuses to start without python-multipart."""
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse(_error(None, -32700, "parse error"), status_code=400)
    # The tool handlers are blocking psycopg calls — off the event loop they go.
    resp = await run_in_threadpool(_dispatch, tenant, tier, scopes, plan,
                                   ents, sub_state, body)
    if resp is None:
        return JSONResponse(None, status_code=202)       # notification accepted
    return JSONResponse(resp)
