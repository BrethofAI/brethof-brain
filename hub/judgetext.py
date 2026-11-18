"""THE WORDS A JUDGEMENT IS ASKED IN — hub only (design step 3, 2026-09-10).

The box sends DATA up the pipe (a stretch of the archive with the names its
graph knows, the fold's candidates, heal's arrivals with their neighbours,
the proposer's head); these functions turn that data into the prompt the
model reads, under the licensed system text in core/prompts.py. Nothing
here ships in the box image: a customer sees the data leave and the answer
return, never how the question is put. The renderers keep the doctrine and
the comments they carried on the box (core/graphwork, core/graphheal,
core/reconcile, core/proposer), where the measurements behind them were
written.
"""
from __future__ import annotations

import datetime as _dt

from core.prompts import TEXTS

NO_PURPOSE = ("(no description written yet — judge by what the records "
              "themselves are about)")


def _today() -> str:
    return _dt.date.today().isoformat()


def _purpose(purpose: str, blank: str) -> str:
    return " ".join((purpose or "").split()) or blank


# ── the graph reader ────────────────────────────────────────────────────────
def graph(project: str, session_id: str, lo: int, hi: int, text: str,
          known: list[dict], kinds: list, about: list | None = None) -> str:
    lines = []
    if known:
        lines.append("KNOWN HERE — things this project already has that this "
                     "stretch may mean (use the name exactly as shown):")
        for k in known:
            aka = [a for a in (k.get("aliases") or []) if a != k["name"]][:6]
            dead = " (superseded)" if k.get("status") == "superseded" else ""
            gloss = f' — {k["gloss"]}' if k.get("gloss") else ""
            lines.append(f'- "{k["name"]}" [{k["kind"]}]{dead}{gloss}'
                         + (f" — also written: {', '.join(aka)}" if aka else ""))
            facts = [f for f in (k.get("facts") or []) if isinstance(f, dict) and f.get("dst")][:6]
            if facts:
                lines.append("    open facts: " + "; ".join(
                    f'{f.get("kind")} {f["dst"]} (since {f.get("since") or "?"})' for f in facts))
    else:
        lines.append("KNOWN HERE: (nothing this stretch mentions is in the "
                     "graph yet)")
    if kinds:
        lines.append("\nKINDS IN USE HERE: " + ", ".join(
            f"{t} ({u})" if u else str(t) for t, u in kinds))
    if about:
        lines.append("\nWHAT THIS STRETCH WAS ABOUT — one line per exchange, written by another "
                     "reader of the same words, with the names written in full where the words "
                     "used a shortcut. Use it to know what a word means here; every quote comes "
                     "from THE STRETCH below, never from these lines:")
        for a in about[:40]:
            lines.append(f"- {str(a)[:600]}")
    lines.append(f"\nTHE STRETCH (project {project}, session {session_id[:12]}, "
                 f"turns {lo}-{hi}):\n\n{text}")
    lines.append("\nName the things this stretch touched and how they connect. "
                 "JSON only.")
    return "\n".join(lines)


def graph_system(purpose: str, rules: list[str] | None = None, variant: str = "") -> str:
    """The reader's system text: today, THIS project's purpose, and — since
    2026-09-17 — the same bearing-rule lines the curator reads, appended to
    the purpose (founder: every job is taught by the purpose and the
    filtered rules; the reader had the purpose only)."""
    p = _purpose(purpose, "(no description written yet — judge by what the stretch is about)")
    if rules:
        p += "\n\nRULES THAT BEAR ON READING HERE:\n" + "\n".join(f"- {r}" for r in rules)
    key = f"graph_{variant}" if variant and f"graph_{variant}" in TEXTS else "graph"
    return TEXTS[key].format(today=_today(), purpose=p)


# ── the fold ────────────────────────────────────────────────────────────────
def fold(project: str, cands: list[dict]) -> str:
    lines = [f"PROJECT {project}. NEW NAMES, each with the existing names that "
             f"look near it (name [kind] ×mentions):"]
    for c in cands:
        aka = f" (also written: {', '.join(c['aliases'][:12])})" if c.get("aliases") else ""
        g = f' — {c["gloss"]}' if c.get("gloss") else " — (no line yet)"
        lines.append(f'- NEW "{c["name"]}" [{c["kind"]}] ×{c["mentions"]}{g}{aka}')
        for q in (c.get("seen") or [])[:2]:
            lines.append(f'    read from: "{str(q)[:340]}"')
        ng = c.get("near_gloss") or {}
        ns = c.get("near_seen") or {}
        for n, t, m in c["near"]:
            gg = f" — {ng[n]}" if ng.get(n) else ""
            lines.append(f'    near: "{n}" [{t}] ×{m}{gg}')
            for q in (ns.get(n) or [])[:2]:
                lines.append(f'        read from: "{str(q)[:340]}"')
    lines.append("\nWhich NEW name is the SAME THING as a name shown near it? "
                 "Which alias shown is not a name for that thing? Which NEW name "
                 "has no line yet — give it one. JSON only.")
    return "\n".join(lines)


def fold_system(purpose: str) -> str:
    return TEXTS["graphheal"].format(today=_today(),
                                      purpose=_purpose(purpose, "(no description written yet)"))


# ── heal (reconcile) ────────────────────────────────────────────────────────
def heal_batch(project: str, recs: list[dict], cands: list[dict], body_ids: set,
               filed: list[dict] | None = None) -> str:
    """ONE CALL PER SAVE (founder 2026-09-03). TWO TIERS, BY RANK — never by
    channel (measured 2026-09-01: for a fresh arrival the true target sat in
    the similarity tail 15 times in 39): the model may only act on what it
    has READ, so what it reads is chosen by its fused rank."""
    parts = [f"PROJECT: {project}",
             f"\nTHE RECORDS THAT JUST ARRIVED ({len(recs)}) — each is the "
             "newest statement of its subject:"]
    for rec in recs:
        body = " ".join((rec.get("content") or "").split())
        parts.append(f"--- id: {rec.get('record_id')} [{rec.get('type') or 'note'}] "
                     f"ARRIVED\ntitle: {rec.get('title')}\n{body}")
    core = [c for c in cands if c["record_id"] in body_ids]
    tail = [c for c in cands if c["record_id"] not in body_ids]
    parts.append(f"\nWHAT IS ALREADY STORED THAT MAY COLLIDE WITH THEM "
                 f"({len(cands)}):")
    for c in core:
        cb = " ".join((c.get("content") or "").split())
        parts.append(f"--- id: {c['record_id']} [{c['type']}] "
                     f"(upd {c['upd']}, found by {c['why']})\n"
                     f"title: {c['title']}\n{cb}")
    if tail:
        # (2026-09-29: this line said "say so in your reason if one of these
        # needs reading" — the heal texts forbid exactly that note)
        parts.append(f"\nALSO NEARBY — titles only, not read ({len(tail)}):")
        for c in tail:
            parts.append(f"--- id: {c['record_id']} [{c['type']}] "
                         f"(upd {c['upd']}) {c['title']}")
    if filed:
        parts.append(f"\nALREADY FILED — the project's rules and playbook sections "
                     f"nearest to these records ({len(filed)}). They are not records "
                     f"and you cannot change them:")
        for f in filed:
            ft = " ".join(str(f.get("text") or "").split())[:900]
            parts.append(f"--- [{str(f.get('kind') or 'playbook')[:20]}: "
                         f"{str(f.get('name') or '')[:120]}]\n{ft}")
    return "\n".join(parts) + "\n\nDecide. JSON only."


def heal_cluster(project: str, recs: list[dict]) -> str:
    """THE CORPUS PASS (founder 2026-09-03): a neighbourhood shown in full,
    none just arrived — the model must READ to delete."""
    parts = [f"PROJECT: {project}",
             f"\nRECORDS OF THIS PROJECT — a neighbourhood ({len(recs)}), all "
             f"shown in full, none just arrived:"]
    for c in recs:
        cb = " ".join((c.get("content") or "").split())
        parts.append(f"--- id: {c['record_id']} [{c.get('type') or 'note'}]"
                     f"{' (upd ' + c['upd'] + ')' if c.get('upd') else ''}\n"
                     f"title: {c.get('title')}\n{cb}")
    return "\n".join(parts) + "\n\nDecide. JSON only."


def heal_system(purpose: str, today: str = "") -> str:
    """The healer's system text: today's date and THIS project's purpose
    (2026-09-03: heal judged every project by engineering nouns and cut canon
    from a novel and the open questions from a plan — it never saw what the
    project was). A template without a {purpose} slot still renders. `today`:
    the day the replayed conversation was said (the records replay,
    2026-09-26); the clock's day otherwise."""
    tpl = TEXTS["reconcile"]
    p = _purpose(purpose, NO_PURPOSE)
    try:
        return tpl.format(today=today or _today(), purpose=p)
    except KeyError:
        return tpl.format(today=today or _today())


def retire_system(purpose: str, today: str = "") -> str:
    """The retirement sweep's system text (2026-10-05): the one question —
    does this record still describe the retired thing as current — and none
    of heal's others (worth keeping, repeats), so a sweep removes only what a
    retirement made false."""
    return TEXTS["retire"].format(today=today or _today(), purpose=_purpose(purpose, NO_PURPOSE))


def split(project: str, rec: dict, note: str = "") -> str:
    """The one record the split pass shows: id, type, title, body, whole —
    and, on a second ask, the note saying why the first answer would not do."""
    head = f"PROJECT: {project}\n\n"
    if note:
        head += f"NOTE: {note}\n\n"
    return (head + f"THE RECORD\nid: {rec.get('record_id')}\n"
            f"type: {rec.get('type') or 'note'}\ntitle: {rec.get('title') or ''}\n"
            f"updated: {rec.get('upd') or ''}\n\n{rec.get('content') or ''}\n")


def movement_batch(project: str, moves: list[dict]) -> str:
    """The verifier's page (2026-09-19): each shadow movement by id with its
    ends, its kind and the exact words it was read from."""
    lines = [f"PROJECT {project}. MOVEMENTS TO CHECK:"]
    for m in moves:
        lines.append(f'- id {int(m.get("id") or 0)}: NEW "{str(m.get("name") or "")[:120]}" '
                     f'{str(m.get("kind") or "replaced")} OLD "{str(m.get("was") or "")[:120]}"\n'
                     f'    said: "{str(m.get("said") or "")[:400]}"')
    lines.append("\nJudge each id. JSON only.")
    return "\n".join(lines)


def close_groups(project: str, groups: list[dict]) -> str:
    """The closer's page (2026-09-20): groups of open facts sharing subject and
    kind, oldest first, each fact with its id, other end, date and words."""
    lines = [f"PROJECT {project}. GROUPS OF OPEN FACTS (same subject, same connection), oldest first:"]
    for g in groups:
        lines.append(f'\n"{str(g.get("src") or "")[:120]}" {str(g.get("kind") or "")}:')
        for f in (g.get("facts") or [])[:24]:
            lines.append(f'  - id {int(f.get("id") or 0)}: {str(f.get("dst") or "")[:120]} — since {str(f.get("since") or "")[:10]}'
                         f' — "{str(f.get("said") or "")[:240]}"')
    lines.append("\nWhich older facts did a later fact close? JSON only.")
    return "\n".join(lines)


def close_system(purpose: str) -> str:
    return TEXTS["fact_close_judge"].format(today=_today(), purpose=_purpose(purpose, NO_PURPOSE))


def _card_lines(c: dict, tag: str) -> list[str]:
    al = [str(x) for x in (c.get("aliases") or [])[:30]]
    out = [f'  {tag}: "{str(c.get("name") or "")[:120]}" [{str(c.get("kind") or "")[:30]}] ×{int(c.get("mentions") or 0)}'
           + (f' — {str(c.get("gloss"))[:200]}' if c.get("gloss") else "")
           + (f' (also written: {", ".join(al)})' if al else "")]
    for l in (c.get("links") or [])[:6]:
        out.append(f"       {str(l)[:140]}")
    for q in (c.get("seen") or [])[:2]:
        out.append(f'       read from: "{str(q)[:620]}"')
    return out


def _pair_extra(it: dict) -> list[str]:
    # No "BOTH are written" line (2026-09-22, the second test): a shared
    # spelling is mostly the reader's own guess that the two are one
    # ("Docker" written on Docker Desktop's card, "Postgres" on Percona
    # Server's) and printing it merged both. Every spelling stays on the
    # cards; the shared ones only group names into a family.
    out = []
    for q in (it.get("together") or [])[:2]:
        out.append(f'  SAID TOGETHER: "{str(q)[:620]}"')
    return out


def same_pairs(project: str, pairs: list[dict]) -> str:
    lines = [f"PROJECT {project}. PAIRS — are these two names one thing?"]
    for it in pairs:
        lines.append(f"\nPAIR {int(it.get('i') or 0)}:")
        lines += _card_lines(it.get("a") or {}, "A")
        lines += _card_lines(it.get("b") or {}, "B")
        lines += _pair_extra(it)
    lines.append("\nOne answer per pair: yes, no or unsure. JSON only.")
    return "\n".join(lines)


def same_family(project: str, fam: dict) -> str:
    lines = [f"PROJECT {project}. A FAMILY of names that share spellings — which of them are one thing?"]
    for k, c in enumerate(fam.get("cards") or []):
        lines.append("")
        lines += _card_lines(c, f"NAME {k + 1}")
    ties = fam.get("shared") or []
    if ties:
        lines.append("\nSHARED SPELLINGS:")
        for a, b, sh in ties[:30]:
            lines.append(f'  "{str(a)[:80]}" and "{str(b)[:80]}" are both written: {", ".join(str(x) for x in sh[:6])}')
    tog = fam.get("together") or []
    if tog:
        lines.append("\nSAID TOGETHER:")
        for q in tog[:12]:
            lines.append(f'  "{str(q)[:360]}"')
    lines.append("\nGroup the names that are one thing. JSON only.")
    return "\n".join(lines)


def same_system(purpose: str) -> str:
    return TEXTS["same_judge"].format(today=_today(), purpose=_purpose(purpose, NO_PURPOSE))


def describe_items(project: str, items: list[dict]) -> str:
    lines = [f"PROJECT {project}. EXCHANGES:"]
    for it in items:
        lines.append(f"\nEXCHANGE {int(it.get('i') or 0)} ({str(it.get('when') or '')[:10]}):")
        lines.append(str(it.get("text") or "")[:12000])
    lines.append("\nOne about per exchange. JSON only.")
    return "\n".join(lines)


def describe_system(purpose: str) -> str:
    return TEXTS["describe"].format(today=_today(), purpose=_purpose(purpose, NO_PURPOSE))


def decision_check_items(project: str, words: str, lines: list[dict]) -> str:
    out = [f"PROJECT {project}.", "", "WORDS:", str(words or "")[:4000], "", "LINES:"]
    for d in lines:
        why = f" — why: {str(d.get('why') or '')[:300]}" if d.get("why") else ""
        out.append(f"{int(d.get('n') or 0)}. {str(d.get('kind') or '')} {str(d.get('day') or '')[:10]}: "
                   f"{str(d.get('what') or '')[:400]}{why}")
    out.append("\nWhich lines belong? JSON only.")
    return "\n".join(out)


def decision_check_system(purpose: str, version: str = "") -> str:
    # version "2" / "3": the bench's variants of the check (2026-09-25)
    key = {"1": "decision_check1", "2": "decision_check2", "3": "decision_check3"}.get(version, "decision_check")
    return TEXTS[key].format(today=_today(), purpose=_purpose(purpose, NO_PURPOSE))


def family_system(purpose: str) -> str:
    return TEXTS["same_family"].format(today=_today(), purpose=_purpose(purpose, NO_PURPOSE))


def takeover_items(project: str, items: list[dict]) -> str:
    lines = [f"PROJECT {project}. ITEMS — one NEW FACT each, beside the subject's other facts of that kind:"]
    for it in items:
        n = it.get("new") or {}
        sg = f' — {str(it.get("subject_gloss") or "")[:200]}' if it.get("subject_gloss") else ""
        lines.append(f'\nITEM {int(it.get("i") or 0)}: "{str(it.get("src") or "")[:120]}" {str(it.get("kind") or "")}{sg}')
        lines.append(f'  NEW: {str(n.get("dst") or "")[:120]} — since {str(n.get("since") or "")[:10]} — "{str(n.get("said") or "")[:240]}"')
        if n.get("gloss"):
            lines.append(f'       what it is: {str(n.get("gloss"))[:200]}')
        if n.get("around"):
            lines.append(f'       around: "{str(n.get("around"))[:660]}"')
        for f in (it.get("open") or [])[:24]:
            lines.append(f'  - id {int(f.get("id") or 0)}: {str(f.get("dst") or "")[:120]} — since {str(f.get("since") or "")[:10]}'
                         f' — "{str(f.get("said") or "")[:240]}"')
            if f.get("gloss"):
                lines.append(f'       what it is: {str(f.get("gloss"))[:200]}')
            if f.get("around"):
                lines.append(f'       around: "{str(f.get("around"))[:660]}"')
        for r in (it.get("records") or [])[:3]:
            lines.append(f'  record: {str(r)[:320]}')
    lines.append("\nFor each item: took the place of which older facts, or the same thing as which, or alongside (nothing)? JSON only.")
    return "\n".join(lines)


def takeover_system(purpose: str) -> str:
    return TEXTS["takeover_judge"].format(today=_today(), purpose=_purpose(purpose, NO_PURPOSE))


def gloss_batch(project: str, nodes: list[dict]) -> str:
    lines = [f"PROJECT {project}. THINGS AND THEIR LINES:"]
    for n in nodes:
        lines.append(f'- "{str(n.get("name") or "")[:120]}" [{str(n.get("kind") or "")[:40]}]\n'
                     f'    line now: "{str(n.get("gloss") or "")[:220]}"')
        for q in (n.get("quotes") or [])[:3]:
            lines.append(f'    read from: "{str(q)[:260]}"')
    lines.append("\nWrite each line again. JSON only.")
    return "\n".join(lines)


def gloss_system(purpose: str) -> str:
    return TEXTS["gloss_judge"].format(today=_today(), purpose=_purpose(purpose, NO_PURPOSE))


def movement_system(purpose: str) -> str:
    return TEXTS["movement_judge"].format(today=_today(), purpose=_purpose(purpose, NO_PURPOSE))


def split_system(purpose: str) -> str:
    tpl = TEXTS["split"]
    p = _purpose(purpose, NO_PURPOSE)
    try:
        return tpl.format(today=_today(), purpose=p)
    except KeyError:
        return tpl.format(today=_today())


# ── the proposer ────────────────────────────────────────────────────────────
def proposal(filed: str, projects: list[tuple[str, str]], text: str) -> str:
    lines = [f"- {n}: {p}" if p else f"- {n}" for n, p in projects]
    return (f"FILED UNDER: {filed}\n\nPROJECTS:\n" + "\n".join(lines)
            + f"\n\nTHE SESSION'S FIRST TURNS:\n{text}\n")


def proposer_system() -> str:
    # {today} is the one placeholder; the answer shape in the prompt carries
    # braces of its own, so this is a replace, never str.format (2026-09-10:
    # the first live run died on KeyError '"project"').
    return TEXTS["proposer"].replace("{today}", _today())
