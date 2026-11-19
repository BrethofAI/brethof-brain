"""THE COMPOSITION OF WHAT A SESSION IS TOLD — one code, two callers.

Design `brain-design-hub-brain`, step 2 (2026-09-10): the session brief and
the prompt block are composed from ROWS the box's commands answer
(core/pipecommands) and WORDS core/brieftext renders. This module is the
doctrine in between — which sections, in what order, the strong-match bar,
the record slots, the cooldowns, the plan lines, the proposals — and it
never touches a database: it asks. `ask(cmd, body)` is a coroutine
answering {"ok": bool, ...rows}; on the hub it is `command()` down the tenant's pipe, on the box it is the same handlers
called in process. The law (the rules that bear, the project's purpose) is
the hub's and comes in as an argument. Pinned byte for byte against the
box's old rendering by tests/test_brief_goldens.py.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import time
from typing import Awaitable, Callable

from . import brieftext as T

Ask = Callable[[str, dict], Awaitable[dict]]

# Strong-match bar for prompt-time injection. Calibrated 2026-08-05 on 30
# real prompts (55), re-calibrated 2026-09-03 after the corpus deletions
# (40 → the founder: "the scope of the things you will see will be day and
# night"; 35 since), tunable so a week of live reading can move it.
STRONG = float(os.environ.get("BRAIN_STRONG_BAR", "35"))
# Record slots on the prompt hook: parts 2..(1+slots). Measured 2026-09-03
# on 20 real prompts: on a topical prompt relevance runs five deep; on a
# design-talk prompt nothing clears 35 — the bar, per slot, separates the
# two, so five slots cost nothing on quiet prompts.
RECORD_SLOTS = int(os.environ.get("BRAIN_RECORD_SLOTS", "5"))
PARTS = 1 + RECORD_SLOTS          # six hooks: rule-or-record, playbook-or-record, four records
# The history part (2026-09-28): the hook after the record slots hands the
# nearest decision lines, for a tenant with the graph add-on. A client that
# does not register it never asks — nothing changes for it.
HISTORY_PART = PARTS + 1
HISTORY_K = 25
# THE BAR ON THE HISTORY BLOCK (2026-10-15, founder: the block must not fill
# 25 lines whatever they are). The 25 nearest lines are the CANDIDATES; Jev
# asks of each "does it bear on what the prompt asks?" and only lines at or
# above HISTORY_BAR go in — none is fine. Measured on 40 real prompts of
# 2026-10-14, 1,000 lines blind-labelled (scripts/ops/history_bar_bench.py):
# all 25 — 26% of lines helped; Jev ≥ 0.6 — 8.8 lines a prompt, 65% help,
# 88% of the helpful lines kept; top 8 alone — 43% help, 53% kept; a
# distance cut — worse at every point. Without Jev (off for the tenant, or
# it failed) the block takes the top HISTORY_FALLBACK_K: worse, never broken.
HISTORY_BAR = float(os.environ.get("BRAIN_HISTORY_BAR", "0.6"))
HISTORY_FALLBACK_K = 8
# UNITS: a turn is ONE USER PROMPT (an exchange), counted on the box from
# the session's archived user rows. Until 2026-09-11 the unit was a chat
# ROW, and every tool call is a row: measured on three real sessions of
# 2026-09-08..10 the median was 8.5 rows per prompt (12 in a heavy one),
# so the "40 units = 20 exchanges" the old comment promised was three to
# five prompts in practice and the same record came back far more often
# than the founder's law allows (2026-09-10: "cannot put the same thing
# more often than every 20 messages — the agent ignores them"). Every
# injection COOLS DOWN instead of burning for the session (founder
# 2026-08-12): suppressed while 0 < turn - stamped < cooldown.
RECORD_COOLDOWN = int(os.environ.get("BRAIN_RECORD_COOLDOWN_TURNS", "20"))
# Rules re-fire on TOPIC, with a per-session cooldown — law needs re-hearing
# in hour six (founder 2026-08-05). One number for everything (founder
# 2026-09-17: "every ten is too often"): twenty prompts, like a record.
RULE_COOLDOWN_TURNS = 20
RECALL_K = 12
# THE PER-PROMPT SEARCH STARTS FROM THE MESSAGE, THE PREVIOUS REPLY BY TOPIC
# AND THE MODEL'S QUESTIONS (founder 2026-11-17; core/recallq). Off only to
# measure the old shape against it.
RECALL_B_V3 = os.environ.get("BRAIN_RECALL_B_V3", "on").strip().lower() not in ("off", "0", "false")
QUESTIONS_TIMEOUT_S = float(os.environ.get("BRAIN_RECALL_QUESTIONS_TIMEOUT_S", "6"))
QUESTIONS_BACKOFF_S = float(os.environ.get("BRAIN_RECALL_QUESTIONS_BACKOFF_S", "60"))
_QDOWN = {"until": 0.0}
# ONE BUDGET FOR A PROMPT (2026-11-18): the box waits BRAIN_PIPE_ASK_TIMEOUT_S
# for the whole ambient answer; past it the part comes back empty. A slow
# model must cost the model's own extra, never the whole part — so the
# question writer gets what is left after RECALL_RESERVE_S for the search,
# and Jev's bearing check on the history part gets what is left of the
# budget, then the lines fall back to the nearest few. Must stay under the
# box's ask timeout (core/pipeclient.ASK_TIMEOUT_S).
AMBIENT_BUDGET_S = float(os.environ.get("BRAIN_AMBIENT_BUDGET_S", "9"))
RECALL_RESERVE_S = float(os.environ.get("BRAIN_RECALL_RESERVE_S", "3"))
RECAP_TURNS = 12

# ── shared computation for the sibling parts of one prompt ──────────────────
# The harness runs a prompt's parts as concurrent processes; each used to
# embed the prompt, run the search and ask the hub again. A few seconds, in
# process, keyed by session + prompt, gone by itself — never on disk.
_SHARE_TTL_S = 10.0
_share: dict = {}
_share_lock = threading.Lock()


def _evict(now: float) -> None:
    if len(_share) > 512:
        for kk in [kk for kk, v in _share.items() if v[0] <= now]:
            _share.pop(kk, None)


async def shared(key: tuple, what: str, compute: Callable[[], Awaitable]):
    """Compute once per (scope, session, prompt) for the sibling parts that
    arrive within seconds of each other; evict by age."""
    now = time.monotonic()
    k = key + (what,)
    with _share_lock:
        hit = _share.get(k)
        if hit and hit[0] > now:
            return hit[1]
    val = await compute()
    with _share_lock:
        _evict(now)
        _share[k] = (now + _SHARE_TTL_S, val)
    return val


async def _rows(ask: Ask, cmd: str, body: dict, key: str):
    """One read: the rows under `key`, or None when the box refused or the
    wire failed (the section then simply isn't — empty sections vanish)."""
    try:
        ans = await ask(cmd, body)
    except Exception:                                          # noqa: BLE001
        return None
    if not isinstance(ans, dict) or not ans.get("ok"):
        return None
    return ans.get(key) if key else ans


# ── the session brief ───────────────────────────────────────────────────────
async def sections(ask: Ask, law: dict, project: str, tier: str = "owner",
                   source: str = "", session_id: str = "") -> list[tuple[str, str]]:
    """The brief's sections in order. RULES FIRST: the desktop harness has
    been seen delivering only ONE part on resume (2026-08-03) — first-fit
    packs the rule bulk into part 1: if a single part survives, it is the
    law. Continuity sections are recoverable via session_context / recall;
    a rule the agent never saw is not."""
    out: list[tuple[str, str]] = []
    # The law-budget notices FIRST (one line each, only past 80%; "FULL" at
    # 100%): a big law splits across many parts and the notice must land in
    # part 1, not at the tail of part 8 (measured 2026-09-10: ms's law is
    # eight parts). The session's agent is the one who can propose the merge.
    lines = [str(x) for x in (law.get("pool_notes") or []) if x]
    lines += T.rule_lines(law.get("rules") or [])
    if law.get("note"):
        out.append(("RULES — NOT AVAILABLE", f"⚠ {law['note']}"))
    if lines:
        # "STANDING LAW" in the header (founder 2026-08-23): the section must
        # read as binding, not as ambient notes.
        out.append(("RULES — STANDING LAW", "\n".join(lines)))
    out.append(("BRAIN", (T.MEMORY_HEADER_CUSTOMER if tier == "customer"
                          else T.MEMORY_HEADER_OWNER) + T.note_line(project, session_id)))
    plain = project not in ("global", "rules", "state")
    if not law.get("purpose") and not law.get("note") and plain:
        out.append(T.no_purpose_section(project))
    elif plain:
        out.append(T.description_section(project, source, session_id))
    reads = {"projects": ("projects", {}, "projects")}
    if plain:
        reads.update({
            "notes": ("notes", {"project": project, "limit": 5}, "notes"),
            "plan": ("plan_next", {"project": project, "limit": T.PLAN_LINES}, "items"),
            "proposals": ("proposals", {"project": project, "limit": 5}, "proposals"),
        })
    # (The STATE line and the OPEN LOOPS list left the brief on 2026-09-17 —
    #  founder: "I don't think open loops should be there at all … these
    #  things need to be removed." They came from a hand-written store last
    #  touched 2026-08-11; the plan's Next lines and the notes carry what
    #  is unfinished.)
    # (The SINCE delta and the GRAPH orientation left the brief on
    #  2026-09-27 with the old inventory graph — founder: the decision lines
    #  replace it.)
    reads["recap"] = ("recap", {"project": project, "limit": RECAP_TURNS}, "")
    names = list(reads)
    got = dict(zip(names, await asyncio.gather(
        *(_rows(ask, c, b, k) for c, b, k in (reads[n] for n in names)))))
    if plain:
        # THE HANDOVERS (founder 2026-09-03): the project's last five
        # sessions' notes, newest first — where each thread stopped.
        if got.get("notes"):
            out.append(("NOTES — WHERE THE LAST SESSIONS STOPPED",
                        T.render_notes(got["notes"], session_id)))
        # THE PLAN'S NEXT LINES beside the notes (founder 2026-09-09).
        plan = T.render_plan(got.get("plan") or [])
        if plan:
            out.append((f"PLAN — NEXT (from {project}-plan)", plan))
        props = T.render_proposals(got.get("proposals") or [])
        if props:
            out.append(("SESSIONS THE PROPOSER WOULD FILE ELSEWHERE", props))
    if got.get("projects"):
        out.append(("PROJECTS", T.render_projects(got["projects"], project)))
    rc = got.get("recap") or {}
    recap = T.render_recap(rc.get("tail") or [])
    if recap:
        out.append(("LAST SESSION", recap))
    return out


async def brief(ask: Ask, law: dict, project: str, part: int = 0, tier: str = "owner",
                source: str = "", session_id: str = "") -> str:
    return T.pack_parts(project, await sections(ask, law, project, tier, source, session_id), part)


# ── the prompt block ────────────────────────────────────────────────────────
Match = Callable[[str], Awaitable[tuple[dict | None, str]]]
Purpose = Callable[[], Awaitable[str]]


async def ambient(ask: Ask, match: Match, purpose_of: Purpose, project: str, session_id: str,
                  prompt: str, part: int = 0, scope: str = "", graph: bool = False,
                  ephemeral: bool = False, bears=None, questions=None) -> str:
    """ON-TARGET AND WHOLE (founder 2026-08-12: "that's not just retrieval
    memory — that's a working brain"): the hook is the delivery channel for
    what the session needs, and the strong-match bar guards PRECISION. The
    prompt hook speaks with a rule reminder when the prompt touches law
    (20-turn cooldown) and up to RECORD_SLOTS whole records, each
    independently above the bar, one per registered hook. Below the bar: silence.

    `ephemeral` (2026-10-03): the harness adds our text to ONE model call and
    never keeps it in the conversation (OpenClaw's prependContext, OpenCode
    2's context hook) — a record shown once is gone from the model's view on
    the next prompt, so the record cooldown, which assumes the shown text
    stays in history, would hide it for 20 prompts. Such a harness says so
    and its records are not cooled.

    `part`: 0 — legacy single-hook client: everything in one blob; 1 — the
    rule reminder and the lines; 2..1+slots — record N-1 alone.
    Parts run concurrently for the same prompt; a same-turn stamp does NOT
    suppress, which keeps every part's candidate list identical.

    `match(prompt)` answers (the rule this prompt touches, verdict) — the
    hub's own table, by cosine when the prompt's vector travels with it; a
    "refused" verdict is the gate on the paid feature: one line, no recall.
    `purpose_of()` is awaited only when the periodic description check
    fires (every DESC_CHECK_TURNS turns)."""
    t_start = time.monotonic()

    def left() -> float:
        return AMBIENT_BUDGET_S - (time.monotonic() - t_start)

    if T.trivial_prompt(prompt, allow_short=RECALL_B_V3):
        return ""
    sid = T.sid_of(session_id)
    key = (scope, sid, hashlib.sha256((prompt or "").encode("utf-8")).hexdigest()[:24])
    hub_rule, verdict = await shared(key, "hub", lambda: match(prompt))
    if verdict == "refused":
        return "**Brain** (injected context, not the user's words): " + T.REFUSED
    async def _history_lines() -> list[dict]:
        """The decisions near this prompt: its own channel, no cooldown (a
        re-proposal can come back any time), no stamp."""
        if not graph:
            return []
        lines = await _rows(ask, "history_near", {"project": project, "query": prompt,
                                                  "session_id": sid, "k": HISTORY_K}, "lines") or []
        # `bears(prompt, lines)` → one p per line, or None (no Jev here)
        scores = None
        if bears and lines and left() > 0.5:
            try:
                scores = await asyncio.wait_for(bears(prompt, lines), timeout=left() - 0.5)
            except asyncio.TimeoutError:
                print(f"[composer] history check for {project} ran past the prompt budget — "
                      f"the nearest {HISTORY_FALLBACK_K} lines instead", flush=True)
        if scores is not None and len(scores) == len(lines):
            lines = [d for d, s in zip(lines, scores) if s >= HISTORY_BAR]
        else:
            lines = lines[:HISTORY_FALLBACK_K]
        return lines

    if part == HISTORY_PART:
        return T.history_block(await _history_lines())
    # PART 0 CARRIES THE HISTORY TOO (2026-11-19): every single-hook
    # harness (Codex, OpenCode, Cline, Hermes, Amp, OpenClaw, dsh...) asks
    # part 0, and part 0 never had the decision lines that only part 7 did
    # (found 2026-11-18: a decided codename came back in part 7 and
    # nowhere in part 0). Searched alongside the records, added last.
    hist_task = asyncio.create_task(_history_lines()) if part == 0 else None

    async def _with_history(text: str) -> str:
        if hist_task is None:
            return text
        try:
            lines = await hist_task
        except Exception as e:
            print(f"[composer] history for {project} failed: {type(e).__name__}: {e}", flush=True)
            return text
        room = T.HOOK_BUDGET - (len(text) + 2 if text else 0)
        block = T.history_block(lines, room) if lines else ""
        if not block or block.strip() == T.HISTORY_HEAD.strip():
            return text
        return (text + "\n\n" + block) if text else block

    async def _recall():
        # The previous user turn folded in (a follow-up carries its topic)
        # and the prompt scored alone too, each record keeping its best —
        # the box does both searches and the max-fusion (2026-08-12).
        # RECORDS ONLY (2026-09-18): chat rows never fill a slot, yet fused
        # in they pulled every record's score down — a short record scoring
        # 43 alone fell to 29 beside chat rows and missed the bar (the
        # affiliate-terms and embedding-model questions of the 60-question
        # set). Chat stays in the recall tool, where a line of it answers.
        body = {"project": project, "query": prompt, "session_id": sid, "k": RECALL_K, "fts": "idf",
                "fold_previous": True, "include_chat": False}
        if RECALL_B_V3:
            # B+v3 (founder 2026-11-17, core/recallq): the box also searches
            # with the previous reply cut by topic; the hub adds the model's
            # questions when a question writer is wired and answers in time —
            # without one, recall is B alone, never a wait.
            body["reply_topics"] = True
            if questions is not None and time.monotonic() < _QDOWN["until"]:
                pass                    # the breaker: the model failed lately — B alone, no wait
            elif questions is not None:
                reply = await _rows(ask, "last_reply", {"session_id": sid}, "text") or ""
                t0 = time.monotonic()
                try:
                    budget = min(QUESTIONS_TIMEOUT_S, left() - RECALL_RESERVE_S)
                    if budget <= 0.05:
                        raise asyncio.TimeoutError
                    qs = await asyncio.wait_for(questions(prompt, reply), timeout=budget)
                    # None = the call failed (hub/compose.questions); [] = it answered with none
                    why = "" if qs else ("the model gave no questions" if qs is not None
                                         else "the model call failed")
                except asyncio.TimeoutError:
                    qs, why = None, f"the model ran past its share of the prompt budget ({time.monotonic() - t0:.1f}s)"
                except Exception as e:                                # noqa: BLE001
                    qs, why = None, f"{type(e).__name__}: {str(e)[:120]}"
                if qs:
                    body["questions"] = qs
                    _QDOWN["until"] = 0.0
                else:
                    if why != "the model gave no questions":
                        # THE BREAKER: a failed or slow model is skipped for a
                        # minute, so the next prompts don't each wait for it
                        _QDOWN["until"] = time.monotonic() + QUESTIONS_BACKOFF_S
                    # NEVER SILENT: recall carries on with B alone, and says why
                    print(f"[composer] recall questions skipped for {project} ({why}; reply "
                          f"{len(reply)} chars, {time.monotonic() - t0:.1f}s) — searching without them", flush=True)
        return await _rows(ask, "recall", body, "hits") or []

    async def _stamps():
        return await _rows(ask, "stamps", {"session_id": sid}, "") or {"turn": 0, "stamps": {}}

    # The ledger is read PER CALL, never shared: a sibling part's stamp at
    # this turn must be seen (the rule reminder is not same-turn tolerant;
    # records are, by design).
    hits, st = await asyncio.gather(shared(key, "hits", _recall), _stamps())
    turn = int(st.get("turn") or 0)
    stamps = {str(k): int(v) for k, v in (st.get("stamps") or {}).items()}

    def cooling(item_id: str, cooldown: int) -> bool:
        return T.is_cooling(stamps.get(item_id), turn, cooldown)

    # memory_index rows are navigation, not knowledge; rules:* rows have
    # their OWN channel (the reminder) — the first live probe delivered the
    # ZDR rule twice in one prompt, once per channel. Playbooks are their own
    # channel too (hook two), never a record slot.
    above = [h for h in hits
             if not h.get("chat") and float(h.get("score") or 0) >= STRONG
             and not str(h["rid"]).endswith(":memory_index")
             and not str(h["rid"]).startswith("rules:")
             and (ephemeral or not cooling(str(h["rid"]), RECORD_COOLDOWN))]
    records = [h for h in above if not str(h["rid"]).startswith("playbook:")][:PARTS]
    playbook = next((h for h in above if str(h["rid"]).startswith("playbook:")), None)

    # SIX HOOKS, EACH WITH ITS OWN CHANNEL AND A RECORD BEHIND IT (founder
    # 2026-09-18: "one hook is looking for the rule; if there is a rule it
    # puts the rule, if not it looks for the records — and the same with the
    # playbook"): hook 1 hands the rule this prompt touches, else a record;
    # hook 2 the project's own playbook that fits, else a record; hooks 3–6
    # records. One cooldown for all of them, in the one ledger. Every part
    # computes the same assignment from the same hits and stamps: the rule's
    # cooldown is same-turn tolerant like the records', so a sibling that
    # reads part 1's fresh stamp still counts the rule as present.
    rule_id = "rules:" + str(hub_rule.get("rule_id") or "") if (sid and hub_rule) else ""
    rule_present = bool(rule_id) and not cooling(rule_id, RULE_COOLDOWN_TURNS)
    queue = list(records)
    slot: dict[int, dict | None] = {}
    slot[1] = None if rule_present else (queue.pop(0) if queue else None)
    slot[2] = playbook if playbook else (queue.pop(0) if queue else None)
    for n in range(3, PARTS + 1):
        slot[n] = queue.pop(0) if queue else None

    if 2 <= part <= PARTS:
        h = slot.get(part)
        if h is None:
            return ""
        await _rows(ask, "stamp", {"session_id": sid, "turn": turn, "ids": [h["rid"]]}, "")
        return T.record_block(h["rid"], h.get("row") or {}, T.HOOK_BUDGET)

    prop = await _rows(ask, "proposal_line", {"session_id": sid}, "proposal")
    rule_block = T.rule_reminder(hub_rule) if rule_present else ""
    stamp_ids: list[str] = [rule_id] if rule_present else []
    desc_block = ""
    if turn > 0 and turn % T.DESC_CHECK_TURNS == 0 and project not in ("global", "rules", "state"):
        # One hub fetch per fifty turns, never per prompt.
        desc_block = T.description_check(project, await purpose_of(), turn, sid)
    prop_block = T.proposal_line(prop)
    if part == 1:
        strong = [slot[1]] if slot[1] else []
    else:
        # Legacy single-hook client: everything in one blob — the rule, the
        # playbook and the records, capped as before.
        strong = ([playbook] if playbook else []) + records
        strong = strong[:RECORD_SLOTS]
    if not strong and not rule_block and not desc_block and not prop_block:
        return await _with_history("")
    stamp_ids += [h["rid"] for h in strong]
    if stamp_ids:
        await _rows(ask, "stamp", {"session_id": sid, "turn": turn, "ids": stamp_ids}, "")
    lines = []
    if prop_block:
        lines.append(prop_block)
    if desc_block:
        lines.append(desc_block)
    if rule_block:
        lines.append(rule_block)
    for h in strong:
        room = T.HOOK_BUDGET - sum(len(x) + 1 for x in lines)
        lines.append(T.record_block(h["rid"], h.get("row") or {}, room))
    return await _with_history("\n".join(lines))
