"""THE WORDS OF THE BRIEF AND THE PROMPT BLOCK — pure rendering, no store.

Design `brain-design-hub-brain`, step 2 (2026-09-10): the composition of
what a session is told moves onto the hub, so every renderer here takes
ROWS (what the box's commands answer) and returns TEXT, and imports
nothing that touches a database. The hub image carries this file; the box
uses the same functions through core/injection. One rendering, two callers,
pinned byte for byte by tests/test_brief_goldens.py.

Each function keeps the name and the doctrine it had in core/injection.py,
where the history of every choice below was written; the comments travel
with the code.
"""
from __future__ import annotations

import re

_NONWORD = re.compile(r"[^a-z0-9]+")
# The one line an inactive key hears instead of the block (founder 2026-09-03:
# the Brain speaking unasked is the paid feature). Shared with core/rules_hub.
REFUSED = ("this box's key is not active on the hub — rules and the Brain's "
           "prompt-time help are part of the hub's service and are not served "
           "here. Recall and history keep working.")
_SID_RE = re.compile(r"[^A-Za-z0-9_-]")

# Claude Code truncates each hook's output at a hard 10,000 characters
# (verified empirically 2026-07-30: the cap is PER HOOK COMMAND — three 9.5K
# hooks all arrive whole; an oversized one overflows ALONE to a file the model
# never reads). The 46.7KB single-payload era silently dropped the RULES for
# days — this budget is what makes delivery a guarantee instead of a hope.
PART_BUDGET = 9000
# Per-hook character budget on the prompt side: each prompt-submit part rides
# its own registered hook under the harness's hard 10K per-hook cap. A record
# is injected WHOLE or not at all — wind-tunnel-proven (2026-08-09, reaffirmed
# by the founder 2026-08-12): a model given cut text USES the cut text as if
# complete; it does not fetch the rest. An oversized record degrades to a
# one-line pointer, never a truncated body.
HOOK_BUDGET = 9000
PLAN_LINES = 5


def flat(v) -> str:
    s = " ".join(str(v or "").split())
    return "" if s.lower() in ("none", "null") else s


def clip(s: str, n: int) -> str:
    """Preview-truncate at a WORD boundary with a visible '…'. A hard [:n]
    cut ends previews mid-word ('moderatio', 'The only other chatterbox ') —
    the wind-tunnel transcripts read as data loss even where it is only a
    preview (2026-08-10). The ellipsis tells the model there IS more and the
    full text is one search away."""
    if len(s) <= n:
        return s
    cut = s[:n]
    sp = cut.rfind(" ")
    if sp > n // 2:
        cut = cut[:sp]
    return cut.rstrip() + " …"


def destutter(title: str, body: str) -> str:
    """Models routinely write 'Title: body' INTO content while the gate also
    derives that same first clause as the title — rendered as 'title: content'
    that reads '- X: X: body' (wind-tunnel finding 2026-08-09). Strip the
    body's leading title repeat at RENDER, the one place every writer's
    output converges."""
    if title and body.lower().startswith(title.lower()):
        trimmed = body[len(title):].lstrip(" :—–-")
        if trimmed:
            return trimmed
    return body


def norm(text: str) -> str:
    """Token-boundary form: every run of non-alphanumerics becomes one space,
    and the whole thing is fenced by spaces (the whole-token match the cards
    and `known_here` use; measured 2026-08-31: 15% of cards matched inside a
    longer word before this)."""
    return " " + _NONWORD.sub(" ", (text or "").lower()).strip() + " "


def sid_of(session_id: str) -> str:
    return _SID_RE.sub("", session_id or "")[:120]


# ── the rules ───────────────────────────────────────────────────────────────
def rule_line(row: dict) -> str:
    title = flat(row.get("title"))
    body = flat(row.get("content")) or flat((row.get("meta") or {}).get("rule"))
    if not title and not body:
        return ""
    body = destutter(title, body)
    # Bold titles (founder 2026-08-23): rules fail by being IGNORED, not
    # misread — the salience lever costs 4 chars and makes each law's name
    # land before task pressure buries it. Content byte-identical.
    return (f"- **{title or '(untitled)'}** — {body}" if body
            else f"- **{title}**")


def rule_lines(rules: list[dict]) -> list[str]:
    """Area-scoped behavioral rules, pinned ones only: 'all' + this project's
    own. audience=curator rows are the server-side curator's doctrine — never
    the harness's."""
    return [ln for ln in (rule_line(r) for r in rules if r.get("pin", True)) if ln]


# ── the BRAIN header ────────────────────────────────────────────────────────
# What the session agent needs to know about reaching memory, now that the
# indexes are no longer injected (founder 2026-07-30: "agent now navigates
# projects not tables") — recall surfaces knowledge on demand.
# OWNER surface (this estate's own Claude Code / MCP): recall + query_raw etc.
MEMORY_HEADER_OWNER = (
    "If the Brain tools are MISSING from your toolset: STOP — tell "
    "the user the Brain is disconnected (restart the session or reconnect MCP) "
    "and ask for a decision. Never work memoryless in silence.\n"
    "The Brain is navigated BY PROJECT, on demand — nothing is preloaded.\n"
    "- recall(query, project?) — hybrid search over the curated Brain AND the "
    "chat archive. START HERE.\n"
    "- get_memory(id) — one record. search_chat_text — exact strings "
    "(paths/errors/flags). query_raw — read-only SQL.\n"
    "- move_session — the project-split door.\n"
    "- Project indexes exist as records (get_memory <project>:memory_index) "
    "— fetch when you need the map, they are not injected.")

# CUSTOMER surface: only the tools a panel/customer key actually has. Naming
# owner-only tools here (recall/query_raw/search_chat_text) is a leak AND a
# lie — the customer's agent would call a tool that does not exist. Keep this
# to the real customer toolset only.
# NO "STOP IF THE TOOLS ARE MISSING" HERE (2026-10-21): a customer's
# harness may reach the Brain through its hooks alone (dsh, Pi, Cline … carry
# no Brain tools), and there a missing toolset is the normal shape — the line
# made the model halt and ask "reconnect the Brain, or continue memoryless?"
# instead of answering (dsh@win red twice on the weekly rig). This context
# arriving is itself the proof the Brain is connected.
MEMORY_HEADER_CUSTOMER = (
    "This context comes from the Brain: it is handed in at session start and "
    "with every prompt, so the Brain is connected — work from it. If your "
    "harness also has the Brain tools below, use them for more; if one is "
    "missing or fails when you call it, tell the user once and carry on.\n"
    "The Brain is navigated BY PROJECT, on demand — nothing is preloaded.\n"
    "- search_brain(query_text, project?) — the Brain's saved knowledge, the current "
    "truth. START HERE.\n"
    "- search_history(query_text, project?) — the full raw conversation "
    "history, for detail the Brain does not hold.\n"
    "- get_record(record_id) — one record in full. "
    "list_brain / list_projects — browse.")

DESC_CALL = ('save_rule(rule_id="purpose_{p}", title="★ PROJECT PURPOSE — '
             '{p}", rule="<1-3 sentences: what this project IS and what its '
             'memory is for, as of now>", project="{p}") and answer 2')

# Every this many user turns, the session is asked whether the description
# still fits. Deep in a session the agent knows the project; a first message
# knows nothing, which is why this never fires early.
DESC_CHECK_TURNS = 50


def note_line(project: str, session_id: str) -> str:
    """The BRAIN header's last line: how this session leaves its handover."""
    if not session_id or project in ("global", "rules", "state"):
        return ""
    return (f"\n- YOUR SESSION ID is {session_id}. Before a compact and when a "
            f"thread of work stops, leave the handover: save_note(project="
            f"\"{project}\", session_id=\"{session_id}\", title=<one-line "
            f"thread name>, body=<where it stands, what is live, what is "
            f"next — under 2,000 chars>). Writing again replaces your note. "
            f"A note for another project files there. A finding goes through "
            f"save_memory: into history now, a record if the Brain judges it.")


def no_purpose_section(project: str) -> tuple[str, str]:
    """THE MIRROR (teach-by-onboarding, 2026-08-06; sharpened to PURPOSE
    2026-08-09): a project without an owner PURPOSE runs on a self-taught
    curator. The nag recurs EVERY session start until a purpose exists, and it
    instructs the agent to ASK its human — never to invent one (an invented
    purpose reads authoritative forever and teaches the curator a lie; empty +
    nagging beats plausible + wrong, founder 2026-08-09)."""
    return ("NO PURPOSE SET",
            f"Project '{project}' has NO purpose — the Brain is guessing "
            f"what matters here.\n"
            f"ASK the owner for one sentence (what this project IS and "
            f"what its memory is for) and file it with "
            f"add_project(\"{project}\", purpose=\"...\"). Do NOT invent "
            f"a purpose yourself — an invented one teaches the curator a "
            f"lie it repeats forever. This notice repeats every session "
            f"until a purpose exists.")


def description_section(project: str, source: str = "",
                        session_id: str = "") -> tuple[str, str]:
    """THE DESCRIPTION IS THE AGENT'S (founder 2026-09-02): the purpose rule
    is the one thing that teaches this project's curator, and nothing on our
    side can see enough of a project to keep it current. The agent working in
    it can. It is told so every session, and told to act when it KNOWS the
    project: after a compact or late in a session — never from a first
    message."""
    call = DESC_CALL.format(p=project)
    if (source or "").strip().lower() == "compact":
        note = (f" Then refresh your handover: save_note(project=\"{project}\", "
                f"session_id=\"{session_id}\", ...) — the summary you hold is "
                f"the note the next session needs." if session_id else "")
        return ("YOU JUST COMPACTED — CHECK THE DESCRIPTION, LEAVE THE NOTE",
                f"You hold the whole session in your summary, which is the "
                f"one moment you know this project better than its "
                f"description does. Read the rule ★ PROJECT PURPOSE — "
                f"{project} above. If the work has moved — the project is "
                f"about something else now, a stage is over, a claim in it "
                f"is stale — rewrite it NOW: {call}. If it still fits, do "
                f"nothing.{note}")
    return ("PROJECT DESCRIPTION — YOURS TO KEEP CURRENT",
            f"The rule ★ PROJECT PURPOSE — {project} above is this project's "
            f"description. It is the one thing that teaches the curator what "
            f"to keep here, and nothing maintains it but the agent working in "
            f"the project — you. When the work has moved, rewrite it: {call}. "
            f"Do it when you KNOW the project — late in a session or right "
            f"after a compact — never from a first message. Never invent one: "
            f"if you are unsure what the project is for, ask.")


def description_check(project: str, purpose: str, turn: int,
                      session_id: str = "") -> str:
    """The periodic half of description ownership (founder 2026-09-02): the
    compact hook cannot make the model act — there is no turn between the
    hook and the compaction — so the standing instruction at session start
    is paired with a check at a fixed cadence, when the session is deep
    enough to know."""
    if turn <= 0 or turn % DESC_CHECK_TURNS != 0:
        return ""
    if project in ("global", "rules", "state") or not purpose:
        return ""
    return (f"**Brain — description check, turn {turn}** (injected context, "
            f"not the user's words): this project's description reads: "
            f"\"{clip(' '.join(str(purpose).split()), 600)}\" — You are {turn} "
            f"turns into this project. If that no longer describes what it is "
            f"and what its memory is for, rewrite it now: "
            f"{DESC_CALL.format(p=project)}. If it still fits, say nothing."
            + (f" And refresh your handover for whoever picks this up: "
               f"save_note(project=\"{project}\", session_id=\"{session_id}\", "
               f"title=..., body=...)." if session_id else ""))


# ── the continuity sections, from rows ──────────────────────────────────────
def render_notes(notes: list[dict], this_session: str = "") -> str:
    """The session-start block: newest first, each headed by time and
    thread name. The reader's own note (after a compact) is marked."""
    parts = []
    for n in notes:
        mine = " (this session's own note)" if n["session_id"] == this_session else ""
        head = f"— {n['at']}{mine}" + (f" · {n['title']}" if n["title"] else "")
        parts.append(f"{head}\n{n['body']}")
    return "\n\n".join(parts)


def plan_next_lines(content: str, limit: int = PLAN_LINES, clip_at: int = 220) -> list[str]:
    """The first `limit` items of a plan playbook's "## Next" section, each
    clipped — the method (playbook `plan`) writes them as an ordered list
    whose order is its only status. clip_at=0 returns the raw items."""
    lines: list[str] = []
    inside = False
    for raw in (content or "").splitlines():
        ln = raw.strip()
        if ln.startswith("## "):
            if inside:
                break
            inside = ln[3:].strip().lower().startswith("next")
            continue
        if not inside or not ln:
            continue
        if ln[0].isdigit() or ln.startswith(("-", "*")):
            item = " ".join(ln.split())
            lines.append(clip(item, clip_at) if clip_at else item)
            if len(lines) >= limit:
                break
    return lines


def render_plan(items: list[str], limit: int = PLAN_LINES, clip_at: int = 220) -> str:
    """The plan's first lines beside the notes (founder 2026-09-09): intent
    only, the first few lines, clipped here — the box answers raw items."""
    return "\n".join(clip(" ".join(i.split()), clip_at) if clip_at else i
                     for i in items[:limit])


def render_proposals(rows: list[dict]) -> str:
    """THE PROPOSER'S PENDING MOVES (2026-09-10): sessions filed here that
    the reader would file elsewhere, until the owner decides."""
    return "\n".join(
        f"- {r['session_id'][:8]} ({r['at']}) looks like {r['project']} ({float(r['confidence']):.2f}) — {r['why']} "
        f"→ move_session('{r['session_id']}', '{r['project']}', why='…') to confirm, "
        f"dismiss_proposal('{r['session_id']}') to keep it here" for r in rows)


def render_projects(rows: list[dict], project: str) -> str:
    """Founder 2026-07-30: the session opens with the LIST OF PROJECTS — the
    navigation units — never an index dump."""
    return "\n".join(f"- {r['name']} ({r['records']})" + ("  ← this session" if r["name"] == project else "")
                     for r in rows)


def render_recap(tail: list[dict]) -> str:
    """The LAST EXCHANGE arrives WHOLE (founder 2026-08-03: "it should load
    whole chat current and previous message") — that pair is what the next
    session resumes from. Older tail turns stay short previews; the full
    transcript is always one search_chat away."""
    lines = []
    for i, t in enumerate(tail):
        text = t.get("text") or ""
        body = text if i >= len(tail) - 2 else clip(text, 300)
        lines.append(f"{t.get('role') or '?'}: {body}")
    return "\n".join(lines)


# ── the parts ───────────────────────────────────────────────────────────────
def pack_parts(project: str, sections: list[tuple[str, str]], part: int = 0) -> str:
    """part=0: the whole payload (legacy clients, tests). part=1..N: the
    payload packed greedily into ≤PART_BUDGET chunks at section boundaries —
    one chunk per registered SessionStart hook, so every part rides UNDER the
    harness's per-hook cap and nothing is ever silently dropped. A part
    beyond the packed count returns "" (the client prints nothing).

    A single section larger than the budget is hard-trimmed WITH A LOUD
    MARKER — visible degradation, never silent."""
    head = f"brethof-brain — project: {project}"
    room = PART_BUDGET - len(head) - 24
    rendered = []
    for name, body in sections:
        text = f"## {name}\n{body}"
        if len(text) <= room:
            rendered.append(text)
            continue
        # A section bigger than one part SPLITS at line boundaries into
        # numbered sub-sections — never trims. Rules are law; dropping the
        # tail is the exact failure this design exists to kill (the first
        # live restart, 2026-07-30, trimmed the git-policy rule mid-line).
        chunks: list[list[str]] = [[]]
        size = 0
        for ln in body.splitlines():
            if len(ln) > room - 20:
                # Only a single runaway LINE ever trims — and loudly.
                ln = (ln[:room - 80].rstrip()
                      + " …⚠ LINE TRIMMED — fetch the full record")
            if chunks[-1] and size + len(ln) + 1 > room - 20:
                chunks.append([])
                size = 0
            chunks[-1].append(ln)
            size += len(ln) + 1
        for i, ch in enumerate(chunks, 1):
            label = f"## {name} ({i}/{len(chunks)})" if len(chunks) > 1 \
                else f"## {name}"
            rendered.append(label + "\n" + "\n".join(ch))
    if part <= 0:
        return "\n\n".join([head] + rendered)
    # First-fit packing: each section joins the FIRST part with room, so a
    # fat RULES section doesn't strand the small continuity sections
    # (STATE/LOOPS/GRAPH/RECAP) in a part beyond the registered hooks. The
    # head reserve includes the "(context N/M …)" label with self-heal hint.
    reserve = len(head) + 90
    packs: list[tuple[list[str], int]] = []
    for text in rendered:
        for i, (secs, size) in enumerate(packs):
            if size + len(text) + 2 <= PART_BUDGET:
                secs.append(text)
                packs[i] = (secs, size + len(text) + 2)
                break
        else:
            packs.append(([text], reserve + len(text) + 2))
    if part > len(packs):
        return ""
    # The self-heal hint rides EVERY part: the desktop harness has dropped a
    # succeeded part on resume (2026-08-03) — whichever part the model DOES
    # see tells it how many exist and how to fetch the rest.
    label = head if len(packs) == 1 else (
        f"{head} (context {part}/{len(packs)} — if any part is missing, "
        f"call session_context('{project}') for the whole payload)")
    return "\n\n".join([label] + packs[part - 1][0])


# ── the prompt block ────────────────────────────────────────────────────────
def is_cooling(last: int | None, turn: int, cooldown: int) -> bool:
    """The suppression predicate, pure and pinned by tests: never injected →
    eligible; stamped THIS turn → eligible (sibling-part determinism);
    stamped 1..cooldown-1 turns ago → suppressed; older → eligible again."""
    return last is not None and 0 < turn - last < cooldown


def trivial_prompt(prompt: str, allow_short: bool = False) -> bool:
    """Gate trivial prompts, as v1 does — and SYNTHETIC turns: task
    notifications and command caveats reach the hook as user prompts, and
    matching rules against machine boilerplate produced random injections
    (ledger audit 2026-08-06: purpose_b3d fired on '<task-notification>'
    text). Silence."""
    # SHORT IS NOT TRIVIAL ONCE THE SEARCH READS THE REPLY (2026-11-17): "yes,
    # do it" is what most follow-ups are, and with B+v3 (core/recallq) the
    # previous reply supplies what it points at — the under-25 cut silenced
    # exactly those prompts. Empty and slash commands stay silent.
    if not prompt.strip() or prompt.startswith("/") or (len(prompt) < 25 and not allow_short):
        return True
    head = prompt.lstrip()[:40]
    return head.startswith(("<task-notification", "[SYSTEM NOTIFICATION",
                            "<local-command-caveat", "Caveat: The messages",
                            "<command-name>"))


def rule_reminder(rule: dict | None) -> str:
    """The rule the hub says this prompt touches, full body (founder
    2026-08-05: "if subject is touching some rule this rule should be also
    then fully injected... the model very often just skips rules")."""
    if not rule:
        return ""
    body = flat(rule.get("content"))
    title = flat(rule.get("title")) or str(rule.get("rule_id") or "")
    if not body:
        return ""
    return (f"**Brain — rule in force, this prompt touches it** "
            f"(rule: standing law for every situation; injected context, not the user's words):\n"
            f"- **{title}** — {destutter(title, body)}")


def proposal_line(r: dict | None) -> str:
    """The session's own reminder: this session looks like another project —
    a few times, then quiet; the owner decides."""
    if not r:
        return ""
    return (f"THIS SESSION LOOKS LIKE PROJECT '{r['proposed']}' ({float(r['confidence']):.2f}): {r['why']} — "
            f"it is filed under '{r['filed']}'. move_session('{r['session_id']}', '{r['proposed']}', why='…') "
            f"moves it; dismiss_proposal('{r['session_id']}') keeps it here.")


# THE HISTORY BLOCK (2026-09-28): the nearest decision lines, candidates
# not answers — the session's own agent judges which bear on the prompt.
# Measured before it shipped (scripts/ops/graph_ab.py, headless Claude Code
# on the 27B, 97 re-proposals, blind): caught 41 → 76, missed 26 → 7,
# controls misled 2 → 0 of 20.
HISTORY_HEAD = ("**Brain — decision graph, lines near this prompt** (decision graph: what was decided, "
                "tried, rejected or dropped on a day, each for its own situation, found by meaning, most "
                "relevant first; most will not bear on this prompt. History, not rules.) "
                "If one does, say so before acting — what was decided or tried, when, and why.")


def history_block(lines: list[dict], room: int = HOOK_BUDGET) -> str:
    """The lines in the order found, each whole or not at all — a line that
    does not fit ends the block (never a cut line)."""
    out = [HISTORY_HEAD]
    used = len(HISTORY_HEAD)
    for n, d in enumerate(lines or [], 1):
        why = flat(d.get("why"))
        line = (f"{n}. {d.get('kind') or 'decided'} {d.get('day') or '?'}: {flat(d.get('what'))}"
                + (f" — why: {why}" if why else ""))
        if used + 1 + len(line) > room:
            break
        out.append(line)
        used += 1 + len(line)
    return "\n".join(out) if len(out) > 1 else ""


def record_block(rid: str, row: dict, room: int) -> str:
    """One injected record: WHOLE if it fits in `room`, else a one-line
    pointer — never a cut body (a model uses cut text as if complete)."""
    title = row.get("title") or rid
    head = (f"**Brain — curated record {rid}** "
            f"(curated record: kept from conversation, true as of upd {str(row.get('ts') or '')[:10]}; "
            f"not a rule; verify currency, recall for more):")
    body = " ".join((row.get("content") or "").split())
    block = f"{head}\n{title}\n{body}"
    if len(block) <= room:
        return block
    return (f"**Brain — curated record {rid} is too large to inject; "
            f"READ IT NOW: get_memory('{rid}')**\n{title}")
