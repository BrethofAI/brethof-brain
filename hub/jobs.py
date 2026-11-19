"""THE JUDGEMENT — every job that needs a prompt, run where the prompts live.

Founder 2026-08-29: "hub.brethof.ai is our new brain, and everything will be
going only through this. It will drive our local containers and VPS
containers. One brain doing both."

THE SPLIT THAT MAKES THIS WORK: the container GATHERS, the hub JUDGES, the
container APPLIES.

Gathering needs the memory — which rules exist, what the last turns were,
which entity names look close to a new one. That has to happen on the box,
and it must, because we cannot read a customer's store and do not want to be
able to. Judging needs the prompts and a model key, both of which live here
and neither of which ships. Applying needs the memory again.

So a job arrives as EVIDENCE ALREADY COLLECTED, is answered as STRUCTURED
JUDGEMENT, and nothing here ever holds a customer's memory — only the piece
of conversation it was asked about, for the length of one call.

WHY THESE FUNCTIONS TAKE NO CONNECTION. Everything here is prompt + parse.
The tenant's data never reaches this process, so there is nothing to connect
to; the only state the hub keeps is its own — rules, type lists, briefs, and
the stage-0 verdict cache.
"""
from __future__ import annotations

import hashlib
import os
import json
import re

# ── parsing ─────────────────────────────────────────────────────────────────
# Deliberately the same contract as core/inference.parse_json_loose: models
# wrap JSON in prose or fences even when told not to, and an unparseable
# answer is DROPPED, never guessed at.


def parse_loose(text: str) -> dict | None:
    if not text:
        return None
    s = text.strip()
    # A reasoning model can leak its think block into the body. Everything
    # before the closing tag is reasoning, never the answer — and leaving it in
    # makes the first "{" land inside the prose, so the {...} span is invalid
    # and a good answer parses as None (measured on glm-5.3, 2026-09-02).
    if "</think>" in s:
        s = s.split("</think>", 1)[1].strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    a, b = s.find("{"), s.rfind("}")
    if a == -1 or b <= a:
        return None
    span = s[a:b + 1]
    try:
        obj = json.loads(span)
    except ValueError:
        # The same three faults the box mends (core.inference.repair_json):
        # a raw quote, newline or tab inside a string, a trailing comma.
        try:
            from core.inference import repair_json
            obj = json.loads(repair_json(span))
        except (ValueError, ImportError):
            return None
    return obj if isinstance(obj, dict) else None


def sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:32]


# ── the rules a box is handed: this project's law, plus the service's seeds ──
# THE HUB IS THE ONLY STORE (founder 2026-09-02): a box keeps no copy of any
# rule. Rules reach a session through the hooks, fetched here at session
# start; without the hub there is no curation, no heal and no teaching, so
# there is nothing for a rule to do on the box — and a box that cannot reach
# the hub is, in nearly every case, one that is not paying. The two ★SEED
# rules are OURS, served to every tenant, read-only through every door.
SEED_RULES: dict[str, dict[str, str]] = {
    "seed_organize_by_project": {
        "title": "★SEED — Organize the Brain by PROJECT — one project per "
                 "codebase, product, or long-running topic",
        "content": (
            "The Brain is organized into projects; each session's context is "
            "built from the project being worked in. Create a project the "
            "moment work concerns a distinct codebase, product or ongoing "
            "topic: add_project with a one-sentence purpose — the purpose "
            "teaches the Brain what matters there from day one. What you "
            "learn, save_memory (save_project / save_general): it goes into "
            "that project's history at once, and the Brain keeps a record if a "
            "session must be handed it. Looking something up: search_brain "
            "first (the current truth, kept clean) — the likely project, then "
            "broaden; search_history for what was said in full, raw; "
            "get_record for a whole record. Never pile everything into the general space or one "
            "catch-all project — well-separated projects make finding precise."),
    },
    "seed_four_doors": {
        "title": "★SEED — Four doors — where each kind of thing goes",
        "content": (
            "Four doors, and nothing else writes to the Brain. "
            "1. SAVE IT — save_memory (save_project / save_general): a fact, a "
            "decision, a measurement, what a tool showed you. It goes into the "
            "project's history at once, searchable, and the Brain keeps a "
            "record if a future session must be handed it. A saved turn is "
            "never edited: a correction is another save. Edit or delete a "
            "RECORD only to correct it. "
            "2. NOTE — save_note: where YOUR thread of work stands, for "
            "whoever picks the project up. One per session per project, "
            "rewritten as you go; the project keeps its last five and starts "
            "every session with them. Write it before a compact and when a "
            "thread stops. "
            "3. PLAYBOOK — save_playbook: HOW a thing is done — a procedure, "
            "a command sequence, a prompt shape that took work to find. "
            "Playbooks are never pruned; state records are. "
            "4. RULE — save_rule: a standing constraint every session must "
            "obey without looking it up. "
            "Unsure which door: save_memory."),
    },
    "seed_ledger_is_the_catcher": {
        "title": "★SEED — The Ledger fills itself; you only delete from it — "
                 "finished means empty",
        "content": (
            "The Ledger is the catcher: it fills itself from what is said in "
            "your sessions — a commitment, a 'we will', a thing that needs "
            "doing — and nobody writes to it by hand. Read it at a work "
            "boundary (list_ledger) to catch what was said and meant but not "
            "done. Delete (remove_from_ledger) is its only operation, and "
            "deleting is the maintenance: strike out at once everything done, "
            "dropped, superseded, merely discussed, or a repeat — never "
            "ponder, never rewrite, never refile. A row deleted too early "
            "costs nothing; it files itself again the next time it is said. "
            "What still matters after a deletion lives in the records and the "
            "history, not here. When the work is finished, the Ledger is "
            "empty — a full Ledger is a list of things that were missed."),
    },
    # THE THREE STANDING DOCUMENTS (founder 2026-09-28: ship the atlas, plan
    # and sheet with every install); the method playbooks themselves reach
    # every tenant through core/seedplaybooks
    "seed_standing_documents": {
        "title": "★SEED — Three standing documents per project: its map, its "
                 "plan, and for a product its sheet",
        "content": (
            "Every project can keep three playbooks, each written by a method "
            "playbook that ships with the Brain: the MAP <project>-map — how it "
            "works (use playbook atlas); the PLAN <project>-plan — what is "
            "decided and what comes next (use playbook plan); and, for anything "
            "sold or published, the SHEET <product>-sheet — what it promises "
            "(use playbook prospectus). When a project has none yet and its "
            "work is under way, offer to write them: \"use playbook atlas to "
            "write the map of this project\". Read the map before changing how "
            "something is wired, and update it in the same session that "
            "changes it. The plan's Next opens every session; whoever finishes "
            "an item removes it and puts the result where it belongs. The "
            "methods are the customer's to edit."),
    },
    # THREE KINDS, EACH BY ITS NAME (founder 2026-11-19): an agent in ms
    # quoted a curated line back to him as "a rule", and a curated
    # 'constraint' record held the training dataset back for days.
    "seed_three_kinds": {
        "title": "★SEED — Three kinds of memory, each named: rule, curated record, decision graph",
        "content": (
            "Everything the Brain hands you says what it is. "
            "RULE: standing law, holding from now on in every situation. Only "
            "rules are law; they come from the rules block and lines labelled "
            "rule, written only through save_rule. "
            "CURATED RECORD: what the Brain kept from conversation, as it stood "
            "when said: facts, decisions, traps, preferences. Whoever said it, "
            "person or agent, it is memory, not a rule; a decision in it was "
            "made for its own situation. "
            "DECISION GRAPH: the history, line by line: what was decided, "
            "tried, rejected or dropped on a day, and why. Not a rule either: "
            "it shows what was chosen and what already failed. "
            "CHAT ARCHIVE: everything said, raw. "
            "Never call a record or a line a rule, and never say a rule forbids "
            "something unless a rule says it. When what the person asks now "
            "goes against a record or a line, say what it says and when, then "
            "do what they ask now."),
    },
    "seed_rules_are_law": {
        "title": "★SEED — Rules TEACH the Brain — exactly two kinds",
        "content": (
            "There are exactly two kinds of rules: GENERAL (a rule in every "
            "project) and PROJECT (one project's own). Rules are shown "
            "to every agent at session start, and each project's "
            "curator re-learns from its rules within minutes — a rule "
            "changes both how agents behave and what the Brain keeps. Save one "
            "the moment your human states a lasting convention or corrects "
            "you: save_rule — the one door; it asks where the text must "
            "live, and your honest answer files it. Rules never go "
            "through ordinary saves. Settings, measured results, and "
            "how-something-behaves are KNOWLEDGE, not rules: save_memory. "
            "A dead or twin rule rots every session that loads "
            "it — tell your human and merge or delete instead of writing a "
            "near-copy. Rules titled ★SEED are the service's own teaching: "
            "read-only, updated by the service."),
    },
}


def is_seed_id(rule_id: str) -> bool:
    return str(rule_id or "").startswith("seed_")


def rules_for(rows: list, project: str = "", seeds: bool = True) -> list[dict]:
    """The rules that bear on `project`: general law ('all'), the project's
    own, a pattern template the project matches (area like 'u_%'), and the
    seeds. No project = everything the tenant has, plus the seeds.
    `seeds=False` — the tenant switched them off (hub table seeds_off).

    `rows` are (rule_id, title, content, area, pin, updated_at)."""
    out: list[dict] = []
    for rid, title, content, area, pin, upd in rows:
        area = str(area or "all")
        if project and not (area == "all" or area == project
                            or ("%" in area and _like(project, area))):
            continue
        out.append({"rule_id": rid, "title": title, "content": content,
                    "area": area, "pin": bool(pin), "seed": False,
                    "updated_at": str(upd)})
    for rid, seed in (SEED_RULES.items() if seeds else ()):
        out.append({"rule_id": rid, "title": seed["title"],
                    "content": seed["content"], "area": "all", "pin": True,
                    "seed": True, "updated_at": ""})
    return out


def match_rule(rules: list[dict], prompt: str) -> dict | None:
    """The rule a prompt touches, by words — answered from the hub's own
    table at the moment of the prompt (founder 2026-09-02/03: rules live in
    the session's context and on the hub, nowhere else).

    FIRST VERSION FIRED ON EVERY PROMPT (measured 2026-09-03, 20 real
    prompts: 20/20 fired, about a third right — the ledger rule five times,
    because its long body shares "record", "delete", "project" with any
    prompt; "no thinking mode" got the VOICE pricing rule). A wrong "rule in
    force" on every prompt is the crying-wolf failure. Two mechanical fixes,
    no word list: a word that appears in many of THIS tenant's rules is
    worth nothing (document frequency, derived from the rules themselves),
    and the match must land in the rule's TITLE — titles are short and carry
    the law's name (tailscale, ledger, markdown, tidy)."""
    words = {w for w in re.findall(r"[a-z0-9]+", (prompt or "").lower())
             if len(w) >= 5}
    if not words or not rules:
        return None
    docs = []
    for r in rules:
        title = set(re.findall(r"[a-z0-9]+", str(r.get("title") or "").lower()))
        body = set(re.findall(r"[a-z0-9]+", str(r.get("content") or "").lower()))
        docs.append((r, title, title | body))
    df: dict[str, int] = {}
    for _, _, all_words in docs:
        for w in all_words:
            df[w] = df.get(w, 0) + 1
    common = max(2, len(docs) // 4)          # in a quarter of the rules = noise
    distinct = {w for w in words if df.get(w, 0) and df[w] <= common}
    if not distinct:
        return None
    best, score = None, 0.0
    for r, title, all_words in docs:
        t_hits = len(distinct & title)
        b_hits = len(distinct & all_words) - t_hits
        if t_hits == 0:
            continue                          # the name of the law must be there
        sc = t_hits * 2.0 + b_hits
        if sc > score:
            best, score = r, sc
    # ONE title word plus one body word (score 3) still fired on 15 of 20 —
    # a short title always has one word a prompt can share ("memory",
    # "prompts", "store"). Two title words, or one plus three in the body.
    return best if score >= 5.0 else None


import array as _array
import math as _math

# The calibration the box-side reminder ran on (2026-08-15, 30 real prompts):
# a cosine at or above .43 is on-target; below it, a distinctive prompt word
# in the rule's TITLE rescues the named-thing class (tailscale, powershell)
# down to a .25 sanity floor. Ported to the hub unchanged.
# Re-read 2026-09-03 on 20 real prompts against the 51-rule brain set: the
# on-target hits sat at .60 / .54 / .48, the wrong ones clustered at .43-.46
# (general "how to work" rules resemble any design-talk prompt), and the
# named-thing case (tailscale, .27) is the title rescue's job. So .50 here,
# tunable, and a week of live reading decides where it settles.
RULE_SIM_FLOOR = float(os.environ.get("HUB_RULE_SIM_FLOOR", "0.50"))
RULE_TITLE_FLOOR = float(os.environ.get("HUB_RULE_TITLE_FLOOR", "0.25"))


def pack_vec(vec) -> bytes | None:
    try:
        a = _array.array("f", [float(x) for x in vec])
    except (TypeError, ValueError):
        return None
    return a.tobytes() if len(a) else None


def unpack_vec(blob) -> list[float] | None:
    if not blob:
        return None
    a = _array.array("f"); a.frombytes(bytes(blob))
    return list(a)


def _cos(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = _math.sqrt(sum(x * x for x in a)); nb = _math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def match_rule_vec(rules: list[dict], vec: list[float], prompt: str) -> dict | None:
    """The semantic match, on the hub. `rules` carry "vec" (a list) where a
    box has sent one; rules without a vector cannot be matched this way and
    are left to the word matcher by the caller."""
    scored = [(_cos(vec, r["vec"]), r) for r in rules if r.get("vec")]
    if not scored:
        return None
    scored.sort(key=lambda t: -t[0])
    top_sim, top = scored[0]
    if top_sim >= RULE_SIM_FLOOR:
        return top
    # THE TITLE RESCUE IS FOR NAMED THINGS (tailscale, powershell, OCI) —
    # words that name exactly one law. Re-read 2026-09-03: with any 5-letter
    # prompt word allowed it fired on 16 of 20 prompts through "memory",
    # "check", "before". A rescue word must appear in ONE rule's title and
    # in few rules at all.
    words = {w for w in re.findall(r"[a-z0-9]+", (prompt or "").lower())
             if len(w) >= 5}
    if not words:
        return None
    title_df: dict[str, int] = {}
    all_df: dict[str, int] = {}
    for r in rules:
        tw = set(re.findall(r"[a-z0-9]+", str(r.get("title") or "").lower()))
        aw = tw | set(re.findall(r"[a-z0-9]+", str(r.get("content") or "").lower()))
        for w in tw:
            title_df[w] = title_df.get(w, 0) + 1
        for w in aw:
            all_df[w] = all_df.get(w, 0) + 1
    rare = max(2, len(rules) // 10)
    naming = {w for w in words if title_df.get(w, 0) == 1 and all_df.get(w, 0) <= rare}
    if not naming:
        return None
    for sim, r in scored[:5]:
        if sim < RULE_TITLE_FLOOR:
            break
        title = set(re.findall(r"[a-z0-9]+", str(r.get("title") or "").lower()))
        if naming & title:
            return r
    return None


def _like(value: str, pattern: str) -> bool:
    """SQL LIKE with % only, the way the data plane matched template areas."""
    parts = pattern.split("%")
    if not value.startswith(parts[0]) or not value.endswith(parts[-1]):
        return False
    pos = len(parts[0])
    for mid in parts[1:-1]:
        i = value.find(mid, pos)
        if i < 0:
            return False
        pos = i + len(mid)
    return True


# ── stage 0: does this rule bear on what gets recorded here? ────────────────
def rule_prompt(project: str, rule_id: str, title: str, body: str,
                purpose: str = "") -> str:
    """The PURPOSE grounds the verdict (founder 2026-08-31): a commercial
    project's name carries nothing — 'phoenix' says less than 'brain' —
    and a filter judging what a curator HERE would record must know what
    HERE is. The purpose is the one sentence every project already has."""
    head = f"PROJECT: {project}\n"
    if purpose.strip():
        head += f"WHAT THIS PROJECT IS: {purpose.strip()}\n"
    return (f"{head}\nRULE ID: {rule_id}\n"
            f"RULE TITLE: {title}\n\nRULE TEXT:\n{body}\n\n"
            f"Does this bear on what this project records? JSON only.")


def rule_verdict(answer: dict | None) -> tuple[bool, str, str]:
    """(bears, why, line). FAILS OPEN on the verdict: losing a rule that shapes
    recording is worse than carrying one that does not. The LINE is what the
    curator reads in place of the rule (founder 2026-09-02: no teacher — the
    filter writes the instruction inside the verdict it already gives). An
    unusable answer keeps the rule with no line; the caller falls back to the
    rule's title and asks again next time."""
    if not isinstance(answer, dict) or "bears" not in answer:
        return True, "unusable verdict — kept", ""
    line = " ".join(str(answer.get("line") or "").split())[:200]
    return bool(answer["bears"]), str(answer.get("why", ""))[:300], line


# ── brief rendering: mechanical, no model call, bounded by the rule table ──
LINE_MAX = 200


def render_brief(base: str, project: str, purpose: str, lines: list,
                 projects: list | tuple = ()) -> str:
    """Base doctrine + the project section, composed from what the project's
    owner wrote and nothing else.

    NO TEACHER (founder 2026-09-02). Two A/Bs on real exchanges, 20 then 50
    pieces: a teacher-compiled category layer measured equal to this — the
    purpose verbatim plus one instruction per bearing rule, written by the
    rule filter in its verdict (teacher 150 records / 457 entities on 50
    pieces, this 162 / 473; read piece by piece, the same mix). The teacher's
    lines were the purpose and the rules restated; its maintainer could only
    ever append instances, and 176 of brain's 188 track lines were that. A
    list mined from conversation cannot be complete (no observer sees enough)
    and can only grow. This cannot grow: its length is the rule table's.

    The section is rendered as RULES, never as an allowlist — "a category
    named here is recorded, everything else is silence" was the growth
    engine, because an allowlist that must be complete is never finished.
    """
    sec: list[str] = []
    if (purpose or "").strip():
        sec.append("ABOUT THIS PROJECT: " + " ".join(purpose.split()))
    else:
        # Loud in the prompt, not just the log: the curator should know it is
        # working without a description, and the owner's agent writes one at
        # its next compact.
        sec.append("ABOUT THIS PROJECT: (no description yet)")
    kept = [" ".join(str(x).split())[:LINE_MAX] for x in (lines or [])
            if str(x).strip()]
    if kept:
        sec.append("RULES THAT BEAR ON RECORDING HERE:")
        sec += [f"- {x}" for x in kept]
    # Collapse the BASE's {{...}} JSON escapes FIRST, then substitute the
    # section. Order is load-bearing (found live 2026-08-31): the section is
    # evidence and gets no format pass, ever — a purpose quoting code braces
    # must not be format-scanned.
    # THE PROJECT LIST, because the routing clause demands a name "that
    # exists" and the curator had never been shown which do (measured
    # 2026-09-02: 15 of 15 routes wrong before it was added).
    names = ", ".join(str(p) for p in projects if str(p).strip())
    return _collapse(base).replace("{project_section}", "\n".join(sec)) \
                          .replace("{project_list}", names or "(none known)") \
                          .replace("{project}", project)


class _Keep(dict):
    def __missing__(self, k):
        return "{" + k + "}"


def _collapse(raw: str) -> str:
    return raw.format_map(_Keep(today="{today}"))


# ── THE LAW BUDGETS (founder 2026-08-07; restored on the hub 2026-09-10) ────
# 1K chars per rule, 48K for the general pool, 20K per project pool — all
# CHARACTERS (title + content). A session start loads the generals plus ONE
# project's rules, so the worst case is 68K of law inside a ~108K envelope
# and the briefing always keeps room. The box carried these until 2026-09-02
# (core/rulecap); when the rules moved here they were dropped by accident —
# the founder: "there should be a message at 80% and at 100%". Enforcement
# style stands: REFUSE THE WRITE WITH THE FIX ATTACHED; nothing existing is
# ever trimmed or deleted by us — the owner merges or trims, and the brief
# says so once the pool passes 80%.
GENERAL_POOL = int(os.environ.get("HUB_RULE_GENERAL_POOL", "48000"))
PROJECT_POOL = int(os.environ.get("HUB_RULE_PROJECT_POOL", "20000"))
NAG_AT = 0.8


def is_general_area(area) -> bool:
    return str(area or "all").strip().lower() in ("all", "global", "")


def rule_weight(title, content) -> int:
    return len(str(title or "")) + len(str(content or ""))


def pool_for(area) -> tuple[str, int]:
    """(scope word, pool size) for a rule's area."""
    return ("general", GENERAL_POOL) if is_general_area(area) else (f"'{area}'", PROJECT_POOL)


def pool_used(rules: list[dict], area, exclude_id: str = "") -> int:
    """Chars in the pool `area` belongs to, over rule dicts (rule_id, title,
    content, area, seed). Seeds never count; an update never counts its own
    old weight."""
    gen = is_general_area(area)
    used = 0
    for r in rules:
        if r.get("seed") or str(r.get("rule_id") or "") == exclude_id:
            continue
        a = r.get("area")
        if (gen and is_general_area(a)) or (not gen and str(a or "") == str(area)):
            used += rule_weight(r.get("title"), r.get("content"))
    return used


def pool_refusal(rules: list[dict], rule_id: str, area, title, content) -> str:
    """The reason a save must be refused, or "". A save that would push its
    pool past its size is refused with the fix in the sentence."""
    scope, pool = pool_for(area)
    used = pool_used(rules, area, exclude_id=rule_id)
    new = used + rule_weight(title, content)
    if new <= pool:
        return ""
    return (f"rule NOT saved — the {scope} rule pool is full ({used:,}/{pool:,} chars used, "
            f"this rule would make {new:,}). Merge two rules that say the same thing, or trim "
            f"one, then resubmit; deleting is free (the rules list shows every rule).")


def pool_lines(rules: list[dict], project: str = "") -> list[str]:
    """The brief's one-line notices: a pool past 80% says so; a full pool says
    new rules are refused. General always; the project's own when it has one."""
    out: list[str] = []
    for area in ((None,) + ((project,) if project and not is_general_area(project) else ())):
        scope, pool = pool_for(area)
        used = pool_used(rules, area)
        if used >= pool:
            out.append(f"⚠ the {scope} rule pool is FULL ({used:,}/{pool:,} chars) — new rules are "
                       f"refused until two are merged or one is trimmed (list the rules, save_rule "
                       f"the merged wording under one id and delete the other).")
        elif used > pool * NAG_AT:
            out.append(f"⚠ the {scope} rule pool is at {100 * used // pool}% ({used:,}/{pool:,} chars) "
                       f"— merge or trim rules before it refuses new law.")
    return out
