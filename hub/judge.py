"""EVERY JUDGEMENT RUNS ON THE HUB — design step 3 (founder 2026-09-10).

The box keeps its queues and its cadence and sends DATA up the pipe; the
hub renders the prompt (hub/judgetext), gates the call (the allowance, the
graph add-on, the provider's window), asks the model on our key, meters
it, parses the answer with the one shared parser, and answers by rid with
DATA the box applies through the mechanical guards it already has. The
prompts never leave here; the relay that let a box send a whole prompt is
gone with the modes.

Answer shapes (every op):
  {"ok": True, "answer": <dict or None>, "fault": <why unparseable, or "">,
   "input_tokens", "output_tokens"}      — the model answered (maybe badly)
  {"ok": False, "why": <plain words>, "retry_s"?: <seconds>, "off"?: True}
                                          — refused: over the allowance, the
                                            add-on not on the plan (off), the
                                            provider's window closed (retry_s),
                                            or the provider down
"""
from __future__ import annotations

import datetime as _dt
from typing import Awaitable, Callable

from core import inference
from . import judgetext as J
from core.prompts import TEXTS


def _s(body: dict, key: str, cap: int = 400) -> str:
    v = body.get(key)
    return (str(v) if v is not None else "")[:cap]


def _tokens(res: dict) -> dict:
    return {"input_tokens": int(res.get("input_tokens") or 0),
            "output_tokens": int(res.get("output_tokens") or 0),
            "model": str(res.get("model") or ""), "seconds": float(res.get("seconds") or 0)}


def _parse(text: str, salvage) -> tuple[dict | None, str]:
    """An EMPTY OBJECT IS AN ANSWER (2026-09-21): the 27B answered the
    take-over with {} — "nothing to do" — and the old `or` chain read the
    falsy dict as no answer, sent it to the salvage, and logged a fault."""
    ans = inference.parse_json_loose(text)
    if ans is None and salvage:
        ans = salvage(text)
    if isinstance(ans, dict):
        return ans, ""
    return None, inference.json_fault(text)


# A RETRY IS A NEW DRAW (founder 2026-09-30, "try different seed"): every
# call is greedy (temperature 0) so the same prompt gives the same answer, and
# a retry of an answer that broke (ms 1ceb3777#4995: 57,557 characters that
# never closed its JSON, identical on all fifteen asks) only repeats it. The
# seed means nothing at temperature 0, so the retry samples a little. The
# first ask stays greedy.
RETRY_TEMPERATURE = 0.3


def _retry(n) -> int:
    try:
        return max(0, int(n or 0))
    except (TypeError, ValueError):
        return 0


async def _call(hub, tenant: dict, job: str, prompt: str, system: str, salvage,
                unit: bool = True, think: bool = False, json_mode: bool = False,
                max_tokens: int = 0, retry: int = 0) -> dict:
    """One model call and its answer as data. `hub` carries the server's
    doors: infer, meter, window_closed, graph_allowed, over_cap,
    heal_over_allowance — handed in so this module has no import of the
    server and the tests hand it fakes."""
    ask = {"prompt": prompt, "system": system, "think": bool(think)}
    if json_mode:
        # the provider constrains decoding to valid JSON (2026-09-23: the 27B
        # broke its JSON on 2-3 of 16 ten-exchange windows, each losing ten
        # exchanges, once the answer carried a note per exchange)
        ask["json"] = True
    if max_tokens:
        # a ceiling on the answer (2026-09-24: one bench lines call ran 900s
        # with no answer — a greedy loop that never closed its JSON)
        ask["max_tokens"] = int(max_tokens)
    if retry > 0:
        ask["temperature"] = RETRY_TEMPERATURE
        ask["seed"] = 42 + retry
    res = await hub.infer(tenant, ask, job=job)
    if not res.get("ok"):
        why = str(res.get("why") or "provider")
        out = {"ok": False, "why": why}
        if why.startswith("provider window"):
            out["retry_s"] = int(res.get("retry_s") or hub.window_closed() or hub.WINDOW_CLOSE_S)
        return out
    # heal rides inside the message price; the graph READ and the FOLD are
    # paid at the exchange door as the second unit (design step 4; founder
    # 2026-09-10: "one for curating, one for the graph, that's it" — no
    # other fee) and meter no unit here — only their tokens, which `infer`
    # records under the job. `unit` stays for the proposer and heal counts.
    if unit:
        hub.meter(tenant["id"], job, 0, 0)
    ans, fault = _parse(str(res.get("text") or ""), salvage)
    return {"ok": True, "answer": ans, "fault": fault, **_tokens(res)}


async def _graph_gate(hub, tenant: dict) -> dict | None:
    if await hub.over_cap(tenant):
        return {"ok": False, "why": "over monthly allowance"}
    if not hub.graph_allowed(tenant):
        # THE GRAPH IS AN ADD-ON (founder 2026-09-08): a plan without it gets
        # a plain answer and the box's worker stays quiet for an hour.
        return {"ok": False, "why": "the graph add-on is not on this plan — brethof.ai/account",
                "off": True}
    w = hub.window_closed()
    if w:
        # THE WINDOW IS CLOSED: a probe (one five-token call a minute) may
        # find the provider answering and reopen it now; otherwise no
        # provider call for graph work, and the box asks again in a minute.
        probe = getattr(hub, "window_probe", None)
        if probe is not None and await probe():
            return None
        w = hub.window_closed() or w
        return {"ok": False, "why": "provider window closed", "retry_s": int(w)}
    return None


async def _heal_gate(hub, tenant: dict) -> dict | None:
    if await hub.over_cap(tenant):
        return {"ok": False, "why": "over monthly allowance"}
    if await hub.heal_over_allowance(tenant):
        # ONE HEAL RUN PER MESSAGE (founder 2026-09-03): heal is included in
        # the message price exactly as far as messages were paid.
        return {"ok": False, "why": "heal allowance: heal runs at most once per "
                                    "curated message — it grows with the messages you send"}
    return None


async def graph_read(hub, tenant: dict, purpose: str, body: dict) -> dict:
    """{project, session_id, turn_lo, turn_hi, text, known, kinds} → the
    reader's answer for one stretch (things, relations, movements), as data."""
    project = _s(body, "project", 120)
    text = str(body.get("text") or "")
    if not project or not text.strip():
        return {"ok": False, "why": "no stretch"}
    gate = await _graph_gate(hub, tenant)
    if gate:
        return gate
    known = [k for k in (body.get("known") or []) if isinstance(k, dict) and k.get("name")][:60]
    for k in known:
        k["facts"] = [f for f in (k.get("facts") or []) if isinstance(f, dict)][:6] if isinstance(k.get("facts"), list) else []
    kinds = [tuple(k) for k in (body.get("kinds") or []) if isinstance(k, (list, tuple)) and len(k) == 2][:40]
    about = [str(a) for a in (body.get("about") or []) if str(a).strip()][:40]
    prompt = J.graph(project, _s(body, "session_id", 160), int(body.get("turn_lo") or 0),
                     int(body.get("turn_hi") or 0), text, known, kinds, about)
    rules = [str(r) for r in (body.get("_rules") or []) if str(r).strip()][:40]
    # A VARIANT (the box's graph bench, core/bench): the same stretch under
    # another edition of the reader's prompt, "graph_<variant>" in TEXTS;
    # an unknown name reads under the one reader there is.
    # THINKING ON is a bench rung (founder 2026-09-21: "maybe this would
    # make the model usable"): a bench tenant's flags may ask for it;
    # production reads without, as every hub job does.
    flags = body.get("flags") if isinstance(body.get("flags"), dict) else {}
    return await _call(hub, tenant, "graph", prompt,
                       J.graph_system(purpose, rules, variant=_s(body, "variant", 40)),
                       inference.salvage_graph, unit=False, think=bool(flags.get("think")))


async def fold_judge(hub, tenant: dict, purpose: str, body: dict) -> dict:
    """{project, candidates} → folds, wrong aliases, glosses — as data; the
    box applies them with the movement refusal and the writes-most rule."""
    project = _s(body, "project", 120)
    cands = [c for c in (body.get("candidates") or []) if isinstance(c, dict) and c.get("name")][:80]
    if not project or not cands:
        return {"ok": False, "why": "no candidates"}
    gate = await _graph_gate(hub, tenant)
    if gate:
        return gate
    return await _call(hub, tenant, "graph", J.fold(project, cands), J.fold_system(purpose),
                       inference.salvage_truncated, unit=False)


def _day(body: dict) -> str:
    """TODAY for a judgement: the day the box names (the records replay,
    founder 2026-09-26: "the only thing which we need is a date" — a July
    window is judged on its own day, as it was live) or the clock's."""
    d = str((body or {}).get("today") or "")[:10]
    try:
        return _dt.date.fromisoformat(d).isoformat()
    except ValueError:
        return J._today()


async def heal_judge(hub, tenant: dict, purpose: str, body: dict) -> dict:
    """{project, records, candidates, body_ids} → heal's ops, as data; the
    box applies them through heal._apply and its guards."""
    project = _s(body, "project", 120)
    recs = [r for r in (body.get("records") or []) if isinstance(r, dict) and r.get("record_id")][:20]
    cands = [c for c in (body.get("candidates") or []) if isinstance(c, dict) and c.get("record_id")][:60]
    body_ids = {str(x) for x in (body.get("body_ids") or [])}
    if not project or not recs:
        return {"ok": False, "why": "no records"}
    gate = await _heal_gate(hub, tenant)
    if gate:
        return gate
    # prompt=<name>: the bench's heal texts, TEXTS["reconcile_<name>"]
    # (one, clean 2026-09-24; the target round 2026-09-29)
    today = _day(body)
    rp = str(body.get("prompt") or "")
    # an unknown name is an error, never production's text in silence
    # (2026-09-29: a silent fallback lets a stale image run the wrong text)
    system = (TEXTS[f"reconcile_{rp}"].format(today=today, purpose=J._purpose(purpose, J.NO_PURPOSE))
              if rp else J.heal_system(purpose, today))
    # FILED (2026-09-29, heal with eyes): the project's rules and playbook
    # sections nearest to the arrivals, shown read-only so a record that only
    # repeats them can go
    filed = [f for f in (body.get("filed") or []) if isinstance(f, dict) and f.get("text")][:10]
    return await _call(hub, tenant, "heal", J.heal_batch(project, recs, cands, body_ids, filed),
                       system, salvage_ops)


async def heal_cluster(hub, tenant: dict, purpose: str, body: dict) -> dict:
    """The corpus pass's call (our own tool): {project, records} → ops."""
    project = _s(body, "project", 120)
    recs = [r for r in (body.get("records") or []) if isinstance(r, dict) and r.get("record_id")][:24]
    if not project or not recs:
        return {"ok": False, "why": "no records"}
    gate = await _heal_gate(hub, tenant)
    if gate:
        return gate
    return await _call(hub, tenant, "heal", J.heal_cluster(project, recs), J.heal_system(purpose),
                       salvage_ops)


async def split_judge(hub, tenant: dict, purpose: str, body: dict) -> dict:
    """The split pass's call (our own tool): {project, record} → the records
    this one becomes ([] = keep as is). Rides the heal allowance."""
    project = _s(body, "project", 120)
    rec = body.get("record") if isinstance(body.get("record"), dict) else None
    if not project or not rec or not rec.get("record_id"):
        return {"ok": False, "why": "no record"}
    gate = await _heal_gate(hub, tenant)
    if gate:
        return gate
    return await _call(hub, tenant, "heal", J.split(project, rec, _s(body, "note", 300)), J.split_system(purpose),
                       inference.salvage_truncated)


async def movement_judge(hub, tenant: dict, purpose: str, body: dict) -> dict:
    """The movement verifier's call (our own tool, 2026-09-19): {project,
    movements:[{id,name,was,kind,said}]} → {verdicts:[{id,verdict,why}]}.
    Graph work: the graph gate (the add-on, the window), the graph job."""
    project = _s(body, "project", 120)
    moves = body.get("movements") if isinstance(body.get("movements"), list) else []
    moves = [m for m in moves if isinstance(m, dict) and m.get("id") is not None][:40]
    if not project or not moves:
        return {"ok": False, "why": "no movements"}
    gate = await _graph_gate(hub, tenant)
    if gate:
        return gate
    return await _call(hub, tenant, "graph", J.movement_batch(project, moves),
                       J.movement_system(purpose), None, unit=False)


async def fact_close_judge(hub, tenant: dict, purpose: str, body: dict) -> dict:
    """The closer's call (2026-09-20): {project, groups:[{src, kind, facts:[{id, dst, since, said}]}]}
    → {closes:[{id, by, why}]}. Graph work: the graph gate, the graph job."""
    project = _s(body, "project", 120)
    groups = body.get("groups") if isinstance(body.get("groups"), list) else []
    groups = [g for g in groups if isinstance(g, dict) and g.get("facts")][:30]
    if not project or not groups:
        return {"ok": False, "why": "no groups"}
    gate = await _graph_gate(hub, tenant)
    if gate:
        return gate
    return await _call(hub, tenant, "graph", J.close_groups(project, groups),
                       J.close_system(purpose), salvage_closes, unit=False)


def salvage_list(key: str, required: tuple[str, ...]):
    """A local model answers a list-shaped op with the LIST and no wrapper —
    '{"id":1,"by":2,..}, {"id":3,..}' (the 27B on the bench, 2026-09-21,
    on the closer and again on the take-over: "Extra data at 117/908").
    The objects are the answer; wrap them under `key`. Each object must
    carry every `required` field."""
    import json

    def salvage(text: str) -> dict | None:
        t = str(text or "").strip().strip("`")
        # a stray closing brace or two after the object ('{"items":[]}}' — 43
        # of the 27B's answers on one replay, 2026-09-21)
        core = t.rstrip("} \n")
        for cand in (t, core + "}", core + "}}", "[" + t + "]", "[" + t.rstrip(",") + "]"):
            try:
                v = json.loads(cand)
            except ValueError:
                continue
            if isinstance(v, list) and all(isinstance(x, dict) and all(r in x for r in required) for x in v):
                return {key: v}
            if isinstance(v, dict) and isinstance(v.get(key), list):
                return v
        return None
    return salvage


salvage_closes = salvage_list("closes", ("id", "by"))
_ops_whole = salvage_list("ops", ("id", "op"))


def salvage_ops(text: str) -> dict | None:
    """HEAL'S ANSWER IS AN "ops" LIST, but heal was parsed with the records
    salvage, which only looks for "records" (2026-09-24/25: the 27B answered
    "nothing to do" as '{"ops":[]' — the closing brace missing — and every
    such call counted as a failed heal, 4-9 per replay). A missing or stray
    closing brace is mended; an answer cut off mid-list keeps every op that
    closed (each op carries its own id and passes heal's guards on its own)."""
    v = _ops_whole(text)
    if v is not None:
        return v
    cut = inference.salvage_truncated(str(text or "").replace('"ops"', '"records"', 1))
    if cut and cut.get("records"):
        return {"ops": cut["records"], "_truncated": True}
    return None
salvage_takeover = salvage_list("items", ("i",))


async def takeover_judge(hub, tenant: dict, purpose: str, body: dict) -> dict:
    """The take-over's call (2026-09-21): {project, items:[{i, src, kind, new:{id,dst,since,said}, open:[...]}]}
    → {items:[{i, closes, closed_by, why}]}. Graph work: the graph gate, the graph job."""
    project = _s(body, "project", 120)
    items = [it for it in (body.get("items") or []) if isinstance(it, dict) and it.get("new") and it.get("open")][:30]
    if not project or not items:
        return {"ok": False, "why": "no items"}
    gate = await _graph_gate(hub, tenant)
    if gate:
        return gate
    return await _call(hub, tenant, "graph", J.takeover_items(project, items),
                       J.takeover_system(purpose), salvage_takeover, unit=False)


async def same_judge(hub, tenant: dict, purpose: str, body: dict) -> dict:
    """One question per pair (2026-09-23): {project, pairs:[{i, a:card, b:card}]}
    → {items:[{i, one: yes|no|unsure, why}]}. Graph work: the graph gate, the graph job."""
    project = _s(body, "project", 120)
    pairs = [it for it in (body.get("pairs") or []) if isinstance(it, dict) and it.get("a") and it.get("b")][:20]
    if not project or not pairs:
        return {"ok": False, "why": "no pairs"}
    gate = await _graph_gate(hub, tenant)
    if gate:
        return gate
    return await _call(hub, tenant, "graph", J.same_pairs(project, pairs),
                       J.same_system(purpose), salvage_list("items", ("i", "one")), unit=False)


async def describe(hub, tenant: dict, purpose: str, body: dict) -> dict:
    """What old exchanges were about (founder 2026-09-22: the curator writes
    it live; a filling job asks this for what was archived before):
    {project, items:[{i, when, text}]} → {items:[{i, about}]}."""
    project = _s(body, "project", 120)
    items = [it for it in (body.get("items") or []) if isinstance(it, dict) and it.get("text")][:12]
    if not project or not items:
        return {"ok": False, "why": "no exchanges"}
    gate = await _graph_gate(hub, tenant)
    if gate:
        return gate
    return await _call(hub, tenant, "graph", J.describe_items(project, items),
                       J.describe_system(purpose), salvage_list("items", ("i", "about")), unit=False)


async def decision_check(hub, tenant: dict, purpose: str, body: dict) -> dict:
    """Which remembered decisions the words are about to go against or repeat
    (2026-09-23: the proposal check — one small call over the few lines the
    mechanical search put forward): {project, words, lines:[{n, kind, day,
    what, why}]} → {lines:[n, ...]}."""
    project = _s(body, "project", 120)
    words = str(body.get("words") or "").strip()
    lines = [d for d in (body.get("lines") or []) if isinstance(d, dict) and d.get("what")][:10]
    if not project or not words or not lines:
        return {"ok": False, "why": "nothing to check"}
    gate = await _graph_gate(hub, tenant)
    if gate:
        return gate
    return await _call(hub, tenant, "graph", J.decision_check_items(project, words, lines),
                       J.decision_check_system(purpose, str(body.get("version") or "")), None, unit=False)


def threads_block(opened) -> str:
    """THREADS — the project's open questions with their latest line, the
    view the lines writer takes a question further by number (2026-09-25)."""
    opened = [t for t in (opened or []) if isinstance(t, dict) and t.get("question")][:40]
    if not opened:
        return ""
    return "THREADS — questions of this project so far (the most recently moved, then those nearest this piece), each with its latest line:\n" + "\n".join(
        f"{int(t.get('n') or 0)}. {str(t.get('question'))[:200]} — latest{(' (' + str(t.get('day'))[:10] + ')') if t.get('day') else ''}: "
        f"{str(t.get('kind') or '')}: {' '.join(str(t.get('what') or '').split())[:240]}" for t in opened) + "\n\n"


def near_block(near) -> str:
    """LINES NEAREST THIS PIECE — the existing lines a reversal, a replacement
    or a repeat must be joined to (2026-09-26, V9)."""
    near = [x for x in (near or []) if isinstance(x, dict) and str(x.get("what") or "").strip()][:20]
    if not near:
        return ""
    return "LINES NEAREST THIS PIECE (already in the memory; the number is the question each belongs to):\n" + "\n".join(
        f"- [{int(x.get('n') or 0)}] {str(x.get('kind') or '')} {str(x.get('day') or '')[:10]}: "
        f"{' '.join(str(x.get('what')).split())[:240]}" for x in near) + "\n\n"


def so_far_block(recs) -> str:
    """RECORDS THIS SESSION HAS WRITTEN — up to 200, so a record out of
    sight can still be replaced (2026-09-23: ~14% of a session's records
    restated or overtook one it had already written)."""
    recs = [r for r in (recs or []) if isinstance(r, dict) and r.get("title")][:200]
    if not recs:
        return ""
    return "RECORDS THIS SESSION HAS WRITTEN (context — rewrite one only to change it):\n" + "\n".join(
        (f"- {str(r.get('title'))[:120]} — {' '.join(str(r.get('content') or '').split())[:220]}"
         if str(r.get("content") or "").strip() else f"- {str(r.get('title'))[:120]}") for r in recs) + "\n\n"


def next_block(nxt) -> str:
    nxt = str(nxt or "").strip()[:6000]
    return (f"THE NEXT MESSAGE (what the person said right after this piece — context only):\n\n{nxt}\n\n"
            if nxt else "")


# ── JEV IN THE WINDOW'S JUDGEMENT (founder 2026-10-02: "I think that works,
# and the graph we need to add also"). Only for tenants hub.jev_on names
# (hub/jev.py); every step degrades to today's judgement when Jev or the box
# does not answer.
OVERTURN_CUT, PICK_CUT, LINK_CUT, PICK_MAX = 0.6, 0.5, 0.7, 20
STILL_BATCH, STILL_REC, STILL_CONV = 100, 400, 30000
STILL_EACH, STILL_PICK = 5, 25
OVERTURN = {"true": "the conversation shows the record is no longer true: the bug it names is fixed, the task it "
                    "names is done or dropped, the cause it gives proved wrong, or its plan, value or state was replaced",
            "false": "the conversation does not change what the record says, or does not touch it"}
BEARS = {"true": "the piece continues, reverses, replaces or repeats what this line is about",
         "false": "the piece does not touch what this line is about"}
TAKES = {"true": "B continues, reverses, gives the result of, or replaces the very choice or test A records",
         "false": "B is about a different choice or thing than A, even if the area is the same"}
OVERTAKEN_ASK = ("THESE RECORDS OF THIS SESSION MAY HAVE BEEN OVERTAKEN by the conversation below — a bug fixed, a "
                 "task done or dropped, a cause proved wrong, a plan or value replaced. For each, read the "
                 "conversation: if it is still true as written, leave it out; if part of it is still true, UPDATE it "
                 "to what is true now; if nothing of it is true any more, DELETE it with \"into\" empty. A "
                 "preference is a standing way of working: one instance of it met or fixed never overturns it.")


# THE DUPLICATE JUDGE (2026-10-05, the tightening round; measured 2026-09-30 on
# 334 real record pairs: AUC 0.993, at 0.5 merges 44 of 54 true twins with 0 of
# 280 wrong). A new record that Jev judges the same thing as one of the session's
# records shown to the curator takes that record's title, so the save rewrites
# it instead of writing a twin beside it (the newer wording wins, as decided
# 2026-09-30). On only when the window asks ("twin"), until it is measured.
TWIN_CUT = 0.5
TWIN = {"true": "one thing: a session would look both up under one name, and one record could carry what both say",
        "false": "two different things: each says something the other does not, and a session would look them "
                 "up under different names"}


async def _twins(hub, tenant: dict, new: list, so_far: list) -> int:
    from . import jev
    olds = [r for r in so_far if isinstance(r, dict) and str(r.get("title") or "").strip()]
    if not new or not olds:
        return 0
    pairs = [(i, j) for i in range(len(new)) for j in range(len(olds))
             if str(new[i].get("title") or "").strip() != str(olds[j].get("title") or "").strip()]
    best: dict[int, tuple[float, int]] = {}
    toks = 0
    for b in range(0, len(pairs), 20):
        batch = pairs[b:b + 20]
        ps, t = await jev.noul(
            {"pairs": {f"p{k}": {"A": f"{new[i].get('title')} — {new[i].get('content')}"[:600],
                                 "B": f"{olds[j].get('title')} — {olds[j].get('content')}"[:600]}
                       for k, (i, j) in enumerate(batch)}},
            {f"p{k}_same": (f"Are records A and B of pair p{k} about the same thing, so only one of them should stay?",
                            TWIN) for k in range(len(batch))})
        toks += t
        if ps is None:
            break
        for k, (i, j) in enumerate(batch):
            p = ps[f"p{k}_same"]
            if p >= TWIN_CUT and p > best.get(i, (0.0, -1))[0]:
                best[i] = (p, j)
    hub.meter(tenant["id"], "infer:jev", toks, 0)
    for i, (_, j) in best.items():
        new[i]["title"] = olds[j]["title"]            # the save rewrites that record
    return len(best)


async def _still_true(hub, tenant: dict, project: str, purpose: str, today: str, piece: str,
                      so_far: list, session_id: str = "", new: list | None = None,
                      replay_tenant: str = "") -> list[dict]:
    """THE STILL-TRUE CHECK: Jev asks, of every record this session already
    wrote, whether this window overturns it; the flagged ones go with the
    window to one heal-text call, whose update/delete ops ride down in the
    save (the box applies them only to this session's records). Measured on
    GLM, four whole sessions, two runs each: stale 14 -> 8, key caught +6,
    worth keeping +2 points, same record count."""
    from . import jev
    # THE LIST, SEARCHED (2026-10-02): only the session's records nearest what
    # this window wrote — a window overturns what it is about. Nothing written,
    # nothing asked. A box without the command: the whole session, as before.
    queries = [f"{r.get('title')} — {r.get('content')}" for r in (new or []) if isinstance(r, dict)][:30]
    if not queries:
        return []
    got = (await hub.command(tenant, "records_near_subjects",
                             {"project": project, "session_id": session_id, "queries": queries,
                              "each": STILL_EACH, "cap": STILL_PICK,
                              # the records rebuild's own database (2026-10-05): the box's pipe is
                              # bound to the live memory, the rebuild's records are elsewhere
                              **({"replay_tenant": replay_tenant} if replay_tenant else {})}, timeout=30)
           if session_id else {"ok": False})
    pool = (got.get("records") or []) if got.get("ok") else so_far
    earlier = [r for r in pool if isinstance(r, dict) and str(r.get("id") or "").strip()]
    if not earlier:
        return []
    # COST (2026-10-02): the conversation rides once per batch, so batches of
    # 100 at 400 characters a record and the conversation cut at 30,000 —
    # measured on sheet 33's 48 stale: one call per window 385k Jev tokens
    # against 809k, stale caught 17 against 14 (noise band 13–18). Showing
    # only the 30 nearest records instead lost a third of the catches (9).
    conv, flagged, toks = piece[:STILL_CONV], [], 0
    for b in range(0, len(earlier), STILL_BATCH):
        batch = earlier[b:b + STILL_BATCH]
        ps, t = await jev.noul({"conversation": conv,
                                "records": {f"r{j}": f"{r.get('title')} — {r.get('content')}"[:STILL_REC] for j, r in enumerate(batch)}},
                               {f"r{j}_over": (f"Does this conversation overturn record r{j}?", OVERTURN)
                                for j in range(len(batch))})
        toks += t
        if ps is None:
            hub.meter(tenant["id"], "infer:jev", toks, 0)
            return []
        flagged += [r for j, r in enumerate(batch) if ps[f"r{j}_over"] >= OVERTURN_CUT]
    hub.meter(tenant["id"], "infer:jev", toks, 0)
    if not flagged or await _heal_gate(hub, tenant):
        return []
    user = (f"PROJECT: {project}\n\n{OVERTAKEN_ASK}\n\n"
            + "\n".join(f"--- id: {r['id']} [{r.get('type') or 'fact'}]\ntitle: {r.get('title')}\n"
                        f"{' '.join(str(r.get('content') or '').split())}" for r in flagged)
            + f"\n\nTHE CONVERSATION:\n\n{conv}\n\nDecide. JSON only.")
    ans = await _call(hub, tenant, "heal", user, J.heal_system(purpose, today), salvage_ops, unit=False,
                      json_mode=True, max_tokens=12000)
    ids = {str(r["id"]) for r in flagged}
    kinds = {str(r["id"]): str(r.get("type") or "") for r in flagged}
    out = []
    for o in ((ans.get("answer") or {}).get("ops") or []) if ans.get("ok") else []:
        oid = str((o or {}).get("id") or "")
        if oid in ids and o.get("op") in ("update", "delete") and not (o.get("op") == "delete" and kinds[oid] == "preference"):
            out.append({k: o.get(k) for k in ("id", "op", "reason", "type", "title", "content")})
    return out


# THE RETIREMENT SWEEP (founder 2026-10-05: "we need the real solution, which
# will really clean it … something should be doing this straight away";
# wired 2026-10-07). Heal reads a NEW record's ~10 nearest and the still-true
# check reads the session's own records; neither sees a sentence that retires
# a thing — "the inventory graph is stopped" makes dozens of records of other
# sessions false at once. So the history line that says it is the trigger: the
# box sends the line, the records near the retired thing (each with the lines
# said since it was written) and the lines said after the line; Jev flags the
# records that still describe the retired thing as current, and one model call
# (TEXTS["retire"]) answers which the line made untrue. Delete only. Measured
# on GLM before wiring: 87 of 139 known-stale removed, ~87% of deletes right.
RETIRE_CUT, RETIRE_BATCH, RETIRE_REC = 0.5, 50, 500
RETIRED = {"true": "the record speaks of the retired thing as current: in use, working this way, planned, wanted, "
                   "or true now",
           "false": "the record is about something else, or it already says the thing was dropped, replaced or "
                    "ended, or it mentions it only as past"}


def _line_text(line: dict) -> str:
    out = f"{line.get('day', '')} {line.get('kind', '')}: {line.get('what', '')}"
    if str(line.get("replaced") or "").strip():
        out += f" (replacing: {line.get('replaced')})"
    if str(line.get("why") or "").strip():
        out += f" — why: {line.get('why')}"
    return out


async def retire_flag(hub, tenant: dict, body: dict) -> tuple[list[dict], dict, int] | None:
    """Step one: Jev asks of every record sent whether it still describes the
    retired thing as current. (flagged, p by id, tokens), or None when Jev
    does not answer."""
    from . import jev
    line = body.get("line") if isinstance(body.get("line"), dict) else {}
    recs = [r for r in (body.get("records") or []) if isinstance(r, dict) and str(r.get("id") or "").strip()][:200]
    said, after = _line_text(line), [_line_text(x) for x in (body.get("later") or []) if isinstance(x, dict)][:20]
    flagged, ps_all, toks = [], {}, 0
    for b in range(0, len(recs), RETIRE_BATCH):
        batch = recs[b:b + RETIRE_BATCH]
        ps, t = await jev.noul({"retired": said, "said_after": after,
                                "records": {f"r{j}": f"{r.get('title')} — {r.get('content')}"[:RETIRE_REC]
                                            for j, r in enumerate(batch)}},
                               {f"r{j}_current": (f"Does record r{j} still describe the retired thing as current?",
                                                  RETIRED) for j in range(len(batch))})
        toks += t
        if ps is None:
            hub.meter(tenant["id"], "infer:jev", toks, 0)
            return None
        for j, r in enumerate(batch):
            ps_all[str(r["id"])] = round(ps[f"r{j}_current"], 3)
            if ps[f"r{j}_current"] >= RETIRE_CUT:
                flagged.append(r)
    hub.meter(tenant["id"], "infer:jev", toks, 0)
    return flagged, ps_all, toks


async def retire_decide(hub, tenant: dict, purpose: str, body: dict, flagged: list[dict]) -> dict:
    """Step two: one model call picks, of the flagged, the records the line
    made history; only deletes come back. `body["retire_prompt"]` names a
    bench text TEXTS["retire_<name>"] (an unknown name is a KeyError, never
    production's text in silence); production never names one."""
    project = _s(body, "project", 120)
    line = body.get("line") if isinstance(body.get("line"), dict) else {}
    said, after = _line_text(line), [_line_text(x) for x in (body.get("later") or []) if isinstance(x, dict)][:20]
    gate = await _heal_gate(hub, tenant)
    if gate:
        return gate
    today = _day(body)
    user = (f"PROJECT: {project}\n\nTHE LINE:\n{said}\n\n"
            + ("SAID AFTER IT:\n" + "\n".join(after) + "\n\n" if after else "")
            + "THE RECORDS:\n"
            + "\n".join(f"--- id: {r['id']} [{r.get('type') or 'fact'}] (last written {r.get('day', '')})\n"
                        f"title: {r.get('title')}\n{str(r.get('content') or '')}"
                        # SHOWING MORE: what was said after the record was written
                        + ("\n  said since it was written:\n" + "\n".join(f"  - {_line_text(x)}" for x in r["since"])
                           if r.get("since") else "") for r in flagged)
            + "\n\nDecide. JSON only.")
    # a named bench text (TEXTS["retire_<name>"]) for prompt rounds; an
    # unknown name is a KeyError, never production's text in silence
    rp = _s(body, "retire_prompt", 40)
    system = (TEXTS[f"retire_{rp}"].format(today=today, purpose=J._purpose(purpose, J.NO_PURPOSE)) if rp
              else J.retire_system(purpose, today))
    ans = await _call(hub, tenant, "retire", user, system, salvage_ops, unit=False, json_mode=True, max_tokens=12000)
    if not ans.get("ok"):
        return ans
    ids = {str(r["id"]) for r in flagged}
    kinds = {str(r["id"]): str(r.get("type") or "") for r in flagged}
    ops = []
    verdicts = [{k: o.get(k) for k in ("id", "op", "reason")}
                for o in ((ans.get("answer") or {}).get("ops") or []) if isinstance(o, dict)]
    for o in ((ans.get("answer") or {}).get("ops") or []):
        oid = str((o or {}).get("id") or "")
        # DELETE ONLY (founder 2026-10-05: "we don't keep history in the record,
        # so pretty much it should be deleting … it just pure deletes"). Measured
        # first: of 113 whole rewrites 27 wrote something false; of 83 quoted
        # edits 12 wrote something false and 13 lost a detail. A wrong delete is
        # cheap (the archive keeps everything; the box logs what it deleted); a
        # stale record is not. Never a preference, never a record not flagged.
        if oid in ids and o.get("op") == "delete" and kinds[oid] != "preference":
            ops.append({"id": oid, "op": "delete", "reason": o.get("reason"), "into": ""})
    return {"ok": True, "ops": ops, "verdicts": verdicts, "fault": ans.get("fault") or "",
            "input_tokens": ans.get("input_tokens", 0), "output_tokens": ans.get("output_tokens", 0)}


async def retire_judge(hub, tenant: dict, purpose: str, body: dict) -> dict:
    """{project, line, records, later, today?} → {ok, answer: {ops, flagged}}:
    Jev flags, one model call decides; the deletes ride back to the box, which
    applies them only to the records it sent (core/retire.apply)."""
    project = _s(body, "project", 120)
    line = body.get("line") if isinstance(body.get("line"), dict) else {}
    if not project or not str(line.get("what") or "").strip() or not body.get("records"):
        return {"ok": True, "answer": {"ops": [], "flagged": []}}
    if getattr(hub, "jev_on", None) is not None and not hub.jev_on(tenant):
        return {"ok": False, "why": "jev off for this tenant", "retry_s": 3600}
    got = await retire_flag(hub, tenant, body)
    if got is None:
        return {"ok": False, "why": "jev", "retry_s": 300}
    flagged, ps_all, toks = got
    answer = {"ops": [], "flagged": [{"id": str(r["id"]), "p": ps_all[str(r["id"])]} for r in flagged],
              "p": ps_all, "jev_tokens": toks}
    if not flagged:
        return {"ok": True, "answer": answer}
    dec = await retire_decide(hub, tenant, purpose, body, flagged)
    if not dec.get("ok"):
        return dec
    answer["ops"], answer["fault"] = dec["ops"], dec["fault"]
    return {"ok": True, "answer": answer, "input_tokens": dec["input_tokens"], "output_tokens": dec["output_tokens"]}


async def _subject_view(hub, tenant: dict, project: str, piece: str, records: list) -> tuple[list, list] | None:
    """THE HISTORY DESIGN'S VIEW: the box hands the lines nearest each record
    the records call just wrote (a wide pool, numbered as today's threads),
    and Jev keeps those this window bears on (at most PICK_MAX). None when
    the box or Jev does not answer — the lines call then gets today's view."""
    from . import jev
    queries = [f"{r.get('title')} — {r.get('content')}" for r in records if isinstance(r, dict)][:60]
    if not queries:
        return None
    got = await hub.command(tenant, "lines_near_subjects", {"project": project, "piece": piece, "queries": queries},
                            timeout=30)
    if not got.get("ok"):
        return None
    threads, pool = got.get("threads") or [], got.get("near") or []
    if not pool:
        return threads, []
    ps, toks = {}, 0
    for b in range(0, len(pool), 40):
        batch = pool[b:b + 40]
        got_ps, t = await jev.noul({"piece": piece[:40000],
                                    "lines": {f"l{j}": f"{x.get('day', '')}: {x.get('what', '')}" for j, x in enumerate(batch)}},
                                   {f"l{j}_bears": (f"Does the piece bear on earlier line l{j}?", BEARS)
                                    for j in range(len(batch))})
        toks += t
        if got_ps is None:
            hub.meter(tenant["id"], "infer:jev", toks, 0)
            return None
        for j, x in enumerate(batch):
            ps[id(x)] = got_ps[f"l{j}_bears"]
    hub.meter(tenant["id"], "infer:jev", toks, 0)
    picked = sorted([x for x in pool if ps.get(id(x), 0) >= PICK_CUT], key=lambda x: -ps[id(x)])[:PICK_MAX]
    return threads, picked


async def _check_links(hub, tenant: dict, decisions: list, threads: list) -> int:
    """A line that says it takes a question further keeps that link only when
    Jev agrees (>= LINK_CUT) that it takes the question's latest line
    further; otherwise it starts a question of its own. Wrong links measured
    16 of 63 -> 5 of 42 at this cut. Returns the links cut."""
    from . import jev
    by_n = {int(t.get("n") or 0): t for t in threads if isinstance(t, dict)}
    linked = [d for d in decisions if isinstance(d, dict) and int(d.get("thread") or 0) in by_n
              and str(by_n[int(d.get("thread") or 0)].get("what") or "").strip()]
    if not linked:
        return 0
    cut, toks = 0, 0
    for b in range(0, len(linked), 20):
        batch = linked[b:b + 20]
        ps, t = await jev.noul({"pairs": {f"p{j}": {"A (earlier line)": f"[{by_n[int(d['thread'])].get('kind')}] "
                                                                           f"{by_n[int(d['thread'])].get('what')}",
                                                     "B (new line)": f"[{d.get('kind')}] {d.get('what')}"}
                                          for j, d in enumerate(batch)}},
                               {f"p{j}_link": (f"In pair p{j}, does new line B take earlier line A further — the same "
                                               f"question moving on?", TAKES) for j in range(len(batch))})
        toks += t
        if ps is None:
            break
        for j, d in enumerate(batch):
            if ps[f"p{j}_link"] < LINK_CUT:
                things = [str(x) for x in (d.get("things") or []) if str(x).strip()]
                d.update(thread=0, how="", question=d.get("question") or (things[0] if things else str(d.get("what") or "")[:60]))
                cut += 1
    hub.meter(tenant["id"], "infer:jev", toks, 0)
    return cut


# ── THE LEDGER CLEANER (founder 2026-10-03: "it's really worth to build it";
# "go with one call per window"). Jev takes out only the obvious, so the
# agent walking the ledger reads less; every other call stays the agent's.
# The questions are word for word the measured ones
# (scripts/ops/ledger_jev_bench.py; brain's 362 rows: 362 -> 167 on arrival,
# -> 83 over 1,005 replayed windows, no open item lost). HUB_LEDGER_CLEAN =
# off | report (the box marks, deletes nothing) | on; for the tenants
# HUB_LEDGER_CLEAN_TENANTS names ("*" = all), and only where Jev runs.
LEDGER_REPEAT_CUT, LEDGER_STEP_CUT, LEDGER_DONE_CUT = 0.5, 0.6, 0.8
LEDGER_SAME = {"true": "the same thing to do: doing one would finish the other",
               "false": "a different thing to do, even on the same subject: another step, a variant, or another part of the work"}
LEDGER_STEP = {"true": "a step of the work in hand: run, score, read, check, wait for or compare something — the next move of the task going on now",
               "false": "a commitment that outlives the task in hand: something to build, fix, decide, deploy, rotate or come back to later"}
LEDGER_DONE = {"true": "the line shows this very work done, finished, measured, dropped or rejected, or overtaken: a later decision replaced it or the work moved on past it",
               "false": "the line is about something else, or only plans, proposes or asks for it, and the work it names is still to do"}


def ledger_clean_mode(tenant: dict) -> str:
    import os
    mode = (os.environ.get("HUB_LEDGER_CLEAN") or "off").strip().lower()
    names = {t.strip() for t in (os.environ.get("HUB_LEDGER_CLEAN_TENANTS") or "").split(",") if t.strip()}
    if mode not in ("report", "on") or not ("*" in names or str(tenant.get("name") or "") in names):
        return "off"
    return mode


async def _ledger_clean(hub, tenant: dict, project: str, got_: dict, today: str) -> None:
    """Two moments, one round trip to the box: the window's new rows (repeat
    of one of their 3 nearest open rows? a step of the work in hand?) and
    the 20 open rows nearest the window's new lines and records (done,
    dropped or overtaken? one Jev call). Report mode puts the verdicts on
    the save; on, dropped rows are never sent and closed rows are deleted.
    Any failure: the window is saved as before."""
    from . import jev
    mode = ledger_clean_mode(tenant)
    if mode == "off" or not jev.on(tenant):
        return
    new = [a for a in got_.get("ledger") or [] if str(a.get("content") or "").strip()
           and str(a.get("project") or project).strip() in ("", project)]
    items = ([f"{today} {r.get('title')} — {str(r.get('content') or '')[:800]}" for r in got_.get("records") or []]
             + [f"{today} {d.get('kind')}: {d.get('what')}" for d in got_.get("decisions") or []])
    if not new and not items:
        return
    got = await hub.command(tenant, "ledger_near", {"project": project, "items": items[:60],
                                                    "rows": [str(a["content"]) for a in new][:16]}, timeout=30)
    if not got.get("ok"):
        return
    toks, verdict = 0, {}
    for i, (a, near) in enumerate(zip(new, got.get("rows") or [])):
        if not near:
            continue
        ps, t = await jev.noul({"new row": str(a["content"]), "earlier rows": {f"e{j}": n["content"] for j, n in enumerate(near)}},
                               {f"e{j}_same": (f"Is earlier row e{j} the same thing to do as the new row?", LEDGER_SAME)
                                for j in range(len(near))})
        toks += t
        if ps:
            j = max(range(len(near)), key=lambda j: ps[f"e{j}_same"])
            if ps[f"e{j}_same"] >= LEDGER_REPEAT_CUT:
                verdict[i] = {"why": "repeat", "of": near[j]["id"], "p": round(ps[f"e{j}_same"], 3)}
    if new:
        ps, t = await jev.noul({"ledger rows": {f"r{j}": str(a["content"]) for j, a in enumerate(new)}},
                               {f"r{j}_step": (f"Is ledger row r{j} a step of the work in hand?", LEDGER_STEP)
                                for j in range(len(new))})
        toks += t
        for i in range(len(new)):
            if ps and i not in verdict and ps[f"r{i}_step"] >= LEDGER_STEP_CUT:
                verdict[i] = {"why": "step", "p": round(ps[f"r{i}_step"], 3)}
    if mode == "on":
        drop = {id(new[i]) for i in verdict}
        got_["ledger"] = [a for a in got_["ledger"] if id(a) not in drop]
        got_["ledger_dropped"] = len(drop)
        # the box keeps what was kept out, with why (2026-10-11); a box
        # older than that ignores the key and behaves as before
        got_["ledger_dropped_rows"] = [{"content": str(new[i]["content"]), **v} for i, v in verdict.items()]
    else:
        for i, v in verdict.items():
            new[i]["clean"] = v
    open_ = got.get("open") or []
    if open_ and items:
        ps, t = await jev.noul({"new history lines and records": {f"e{j}": x for j, x in enumerate(items[:60])},
                                "open ledger rows": {f"r{j}": r["content"] for j, r in enumerate(open_)}},
                               {f"r{j}_done": (f"Does any of the new history lines and records show the work in ledger "
                                               f"row r{j} was done, dropped or overtaken?", LEDGER_DONE)
                                for j in range(len(open_))})
        toks += t
        if ps:
            got_["ledger_closed"] = [{"id": r["id"], "p": round(ps[f"r{j}_done"], 3)} for j, r in enumerate(open_)
                                     if ps[f"r{j}_done"] >= LEDGER_DONE_CUT]
    got_["ledger_mode"] = mode
    hub.meter(tenant["id"], "infer:jev", toks, 0)


async def curate_window(hub, tenant: dict, purpose: str, body: dict, rules: list | None = None,
                        names=()) -> dict:
    """The window's judgement, then the ledger cleaner on what it wrote."""
    out = await _curate_window(hub, tenant, purpose, body, rules, names)
    if out.get("ok") and isinstance(out.get("answer"), dict) and not body.get("records_only"):
        try:
            await _ledger_clean(hub, tenant, _s(body, "project", 120), out["answer"], _day(body))
        except Exception as e:                               # noqa: BLE001
            print(f"[judge] ledger cleaner failed: {type(e).__name__}: {e}", flush=True)
    return out


async def _curate_window(hub, tenant: dict, purpose: str, body: dict, rules: list | None = None,
                         names=()) -> dict:
    """THE SWITCH'S TWO CALLS (founder 2026-09-25) on one window of a
    session: the records call (`batch_records`, with the ledger) under the
    project's brief, and the graph-lines call (`batch_lines`, V6) under the
    purpose with the threads view the box sent up. {piece, threads,
    records_so_far, next, n} → {records, ledger, decisions, questions,
    tokens…}; a lines answer that comes back unparseable or without a
    decisions list is asked once more (2026-09-25: a runaway notes list
    loses a whole window). The graph call only when the add-on is on."""
    from . import jobs
    project = _s(body, "project", 120)
    piece = str(body.get("piece") or "")[:240000]
    if not project or not piece.strip():
        return {"ok": False, "why": "no piece"}
    tail = next_block(body.get("next"))
    today = _day(body)
    got_: dict = {"records": [], "ledger": [], "decisions": [], "questions": [], "failed": []}
    out: dict = {"ok": True, "answer": got_, "input_tokens": 0, "output_tokens": 0}
    # 1. the records and the ledger
    # the records text under test (bench): only a named variant, never free text
    # named bench texts only (2026-09-28 adds the curator's three fixes:
    # nP the sorting, nG one record per finding, nPG both); production
    # never names one
    # (2026-09-28: any named bench text batch_records_<name>, so many can run
    # side by side; an unknown name is a KeyError, never production's text
    # in silence — 2026-09-29)
    rp = str(body.get("records_prompt") or "")
    rtext = f"batch_records_{rp}" if rp else "batch_records"
    system = jobs.render_brief(TEXTS[rtext], project, purpose, rules or [], names).replace("{today}", today)
    # lines_only (bench, 2026-09-29): the lines call alone, for a lines text test
    ans = {"ok": True, "answer": {}} if body.get("lines_only") else await _call(hub, tenant, "curate", f"{so_far_block(body.get('records_so_far'))}NEW PIECE:\n\n{piece}\n\n"
                      f"{tail}Decide. JSON only.", system, None, unit=False, json_mode=True, max_tokens=12000,
                      retry=_retry(body.get("retry")))
    if not ans.get("ok"):
        return ans
    out["input_tokens"] += int(ans.get("input_tokens") or 0)
    out["output_tokens"] += int(ans.get("output_tokens") or 0)
    got = ans.get("answer")
    if isinstance(got, dict):
        got_["records"] = [r for r in (got.get("records") or []) if isinstance(r, dict)]
        got_["ledger"] = [a for a in (got.get("ledger") or []) if isinstance(a, dict)]
    else:
        got_["failed"].append(f"records: {str(ans.get('fault'))[:120]}")
    jev_on = bool(getattr(hub, "jev_on", None)) and hub.jev_on(tenant)
    if jev_on and body.get("twin"):
        got_["twins"] = await _twins(hub, tenant, got_.get("records") or [], body.get("records_so_far") or [])
    if jev_on:
        got_["overtaken"] = await _still_true(hub, tenant, project, purpose, today, piece,
                                              body.get("records_session") or body.get("records_so_far") or [],
                                              str(body.get("session_id") or ""), got_.get("records"),
                                              replay_tenant=str(body.get("replay_tenant") or ""))
    # 2. the graph lines, under the purpose alone (as measured), with the view;
    # never for a records_only ask (the records replay: the lines are written)
    if body.get("records_only") or not hub.graph_allowed(tenant):
        return out
    # any named bench text batch_lines_<name> (2026-09-29); production never names one
    lp = str(body.get("lines_prompt") or "")
    ltext = f"batch_lines_{lp}" if lp else "batch_lines"     # unknown name: KeyError, never a silent fallback
    system = jobs.render_brief(TEXTS[ltext], project, purpose, [], ()).replace("{today}", today)
    threads, near = body.get("threads") or [], body.get("near_lines") or []
    if jev_on:
        view = await _subject_view(hub, tenant, project, piece, got_["records"])
        if view is not None:
            threads, near = view
    got_["threads_used"] = threads
    prompt = (f"{threads_block(threads)}{near_block(near)}"
              f"NEW PIECE:\n\n{piece}\n\n{tail}Decide. JSON only.")
    for attempt in (1, 2):
        # the second ask is a new draw, never the same greedy answer again
        ans = await _call(hub, tenant, "graph", prompt, system, None, unit=False, json_mode=True, max_tokens=20000,
                          retry=2 * _retry(body.get("retry")) + attempt - 1)
        if not ans.get("ok"):
            got_["failed"].append(f"lines: {ans.get('why')}")
            break
        out["input_tokens"] += int(ans.get("input_tokens") or 0)
        out["output_tokens"] += int(ans.get("output_tokens") or 0)
        got = ans.get("answer")
        if isinstance(got, dict) and isinstance(got.get("decisions"), list):
            got_["decisions"] = [d for d in got["decisions"] if isinstance(d, dict)]
            got_["questions"] = [str(q) for q in (got.get("questions") or []) if str(q).strip()]
            if jev_on:
                got_["links_cut"] = await _check_links(hub, tenant, got_["decisions"], threads)
            break
        why = str(ans.get("fault"))[:120] if not isinstance(got, dict) else "no decisions list"
        if attempt == 2:
            got_["failed"].append(f"lines: {why}")
    return out


async def curate_probe(hub, tenant: dict, purpose: str, body: dict) -> dict:
    """The curator with the graph section, asked about one exchange — the
    bench's way to read what the one-read design writes (2026-09-23).
    {project, piece, prompt?} → {records, ledger, about, facts | decisions}.
    Writes nothing."""
    from . import jobs
    project = _s(body, "project", 120)
    # 240k: the bench's window of up to twenty exchanges (2026-09-23)
    piece = str(body.get("piece") or "")[:240000]
    if not project or not piece.strip():
        return {"ok": False, "why": "no piece"}
    gate = await _graph_gate(hub, tenant)
    if gate:
        return gate
    # body.prompt picks the add-on: "graph" (dated facts, 09-23 round one)
    # or "decisions" (the memory of decisions and attempts, 09-23)
    text = {"decisions": TEXTS["curator_decisions"], "simple": TEXTS["curator_simple"],
            "records": TEXTS["curator_records"], "lines": TEXTS["curator_lines"],
            "w1": TEXTS["curator_w1"], "w2": TEXTS["curator_w2"], "w3": TEXTS["curator_w3"],
            # the LIVE curator, as production runs it today — the baseline
            "live": TEXTS["curator"],
            "brec": TEXTS["curator_batch_records"], "blines": TEXTS["curator_batch_lines"],
            "blines2": TEXTS["curator_batch_lines2"], "blines3": TEXTS["curator_batch_lines3"], "blines4": TEXTS["curator_batch_lines4"],
            "blines5": TEXTS["curator_batch_lines5"],
            "thN": TEXTS["curator_lines_threadN"], "thV": TEXTS["curator_lines_threadV"],
            "thNV": TEXTS["curator_lines_threadNV"], "thV3": TEXTS["curator_lines_threadV3"],
            "thV4": TEXTS["curator_lines_threadV4"], "thV5": TEXTS["curator_lines_threadV5"], "thV6": TEXTS["curator_lines_threadV6"],
            "thV7": TEXTS["curator_lines_threadV7"], "thV8": TEXTS["curator_lines_threadV8"],
            # the production pair (2026-09-26): brecl = the records call with the ledger
            "brecl": TEXTS["batch_records"], "plines": TEXTS["batch_lines"], "thV9": TEXTS["curator_lines_threadV9"],
            "route": TEXTS["curator_route"], "wlines": TEXTS["curator_write_lines"],
            "wrecs": TEXTS["curator_write_records"],
            "brec2": TEXTS["curator_batch_records2"], "brec3": TEXTS["curator_batch_records3"],
            "brec4": TEXTS["curator_batch_records4"], "brec5": TEXTS["curator_batch_records5"],
            "brec6": TEXTS["curator_batch_records6"],
            "brec7": TEXTS["curator_batch_records7"], "clean": TEXTS["curator_clean"],
            "splitrec": TEXTS["curator_split_records"],
            "both": TEXTS["curator_batch_both"],
            "both2": TEXTS["curator_batch_both2"],
            "both3": TEXTS["curator_batch_both3"]}.get(
        str(body.get("prompt") or ""), TEXTS["curator_graph"])
    rules = [str(x) for x in (body.get("_rules") or []) if str(x).strip()][:200]
    system = jobs.render_brief(text, project, purpose, rules, ()).replace("{today}", _day(body))
    nxt = str(body.get("next") or "").strip()[:6000]
    tail = (f"THE NEXT MESSAGE (what the person said right after this piece — context only):\n\n{nxt}\n\n"
            if nxt else "")
    # THREADS (2026-09-23): the session's latest decision lines and the
    # project's thread names, so a new line can name its thread and the
    # earlier line it continues, results from or overturns
    head = ""
    earlier = [e for e in (body.get("earlier") or []) if isinstance(e, dict) and e.get("what")][:12]
    names = [str(x)[:80] for x in (body.get("threads") or []) if str(x).strip()][:30]
    if earlier:
        head += "EARLIER LINES IN THIS SESSION (oldest first; context — never write them again):\n" + "\n".join(
            f"{int(e.get('n') or 0)}. [{str(e.get('thread') or '')[:80]}] {str(e.get('kind') or '')}: "
            f"{str(e.get('what') or '')[:300]}" for e in earlier) + "\n\n"
    if names:
        head += "THREADS IN THIS PROJECT: " + "; ".join(names) + "\n\n"
    # THREADS AS THINGS (2026-09-25): the project's open questions, each with
    # its latest line — a line takes one further by its number
    head += threads_block(body.get("open_threads"))
    head += near_block(body.get("near_lines"))
    # THE SPLIT (2026-09-24): the router's items handed to a writer — the
    # writer writes these and nothing else
    items = [i for i in (body.get("items") or []) if isinstance(i, dict) and str(i.get("what") or "").strip()][:120]
    if items:
        label = "THE FACTS TO WRITE" if body.get("prompt") == "wrecs" else "THE MOVES TO WRITE"
        head += f"{label} (from the piece below; exchange, then the item):\n" + "\n".join(
            f"- ex {int(i.get('ex') or 0)}: {' '.join(str(i.get('what')).split())[:400]}" for i in items) + "\n\n"
    # RECORDS SO FAR (2026-09-23 long-run test: ~14% of a session's records
    # restated or overtaken what the same session had already recorded)
    # up to 200: the batch call lists the whole session, older records by title
    # alone (2026-09-23: a record out of sight could not be replaced)
    head += so_far_block(body.get("records_so_far"))
    return await _call(hub, tenant, "graph", f"{head}NEW PIECE:\n\n{piece}\n\n{tail}Decide. JSON only.", system,
                       None, unit=False,
                       json_mode=bool(body.get("json")), max_tokens=int(body.get("max_tokens") or 0),
                       retry=_retry(body.get("retry")))


async def same_family(hub, tenant: dict, purpose: str, body: dict) -> dict:
    """Names tied by shared spellings, asked together (2026-09-22):
    {project, family:{names, cards, shared, together}} → {groups:[{why, names}]}."""
    project = _s(body, "project", 120)
    fam = body.get("family") if isinstance(body.get("family"), dict) else {}
    cards = [c for c in (fam.get("cards") or []) if isinstance(c, dict) and c.get("name")][:10]
    if not project or len(cards) < 3:
        return {"ok": False, "why": "no family"}
    gate = await _graph_gate(hub, tenant)
    if gate:
        return gate
    fam = {"cards": cards, "shared": fam.get("shared") or [], "together": fam.get("together") or []}
    return await _call(hub, tenant, "graph", J.same_family(project, fam),
                       J.family_system(purpose), salvage_list("groups", ("names",)), unit=False)


async def gloss_judge(hub, tenant: dict, purpose: str, body: dict) -> dict:
    """The gloss pass's call (2026-09-19): {project, nodes:[{name,kind,gloss,quotes}]}
    → {lines:[{name,gloss}]}. Graph work: the graph gate, the graph job."""
    project = _s(body, "project", 120)
    nodes = body.get("nodes") if isinstance(body.get("nodes"), list) else []
    nodes = [n for n in nodes if isinstance(n, dict) and n.get("name")][:40]
    if not project or not nodes:
        return {"ok": False, "why": "no nodes"}
    gate = await _graph_gate(hub, tenant)
    if gate:
        return gate
    return await _call(hub, tenant, "graph", J.gloss_batch(project, nodes),
                       J.gloss_system(purpose), None, unit=False)


async def propose(hub, tenant: dict, purposes: Callable[[str], Awaitable[str]], body: dict) -> dict:
    """{filed, session_id, projects: [names], text} → {project, confidence,
    why} as the model wrote it; the box keeps only a listed project."""
    filed = _s(body, "filed", 120)
    names = [str(n)[:120] for n in (body.get("projects") or []) if str(n).strip()][:200]
    text = str(body.get("text") or "")
    if not filed or not names or not text.strip():
        return {"ok": False, "why": "nothing to judge"}
    gate = await _heal_gate(hub, tenant)
    if gate:
        return gate
    projects = [(n, await purposes(n)) for n in names]
    return await _call(hub, tenant, "heal", J.proposal(filed, projects, text), J.proposer_system(), None)


# THE OLD INVENTORY GRAPH'S OPS — graph_read, fold_judge, movement_judge,
# gloss_judge, fact_close_judge, takeover_judge, same_judge, same_family,
# describe — stay only because the hosted boxes on mind-cf still run code
# that asks them; the box itself dropped that graph on 2026-09-27. They go
# (with their prompts) when mind-cf runs that commit.
OPS = {"retire_judge": retire_judge, "graph_read": graph_read, "fold_judge": fold_judge, "heal_judge": heal_judge,
       "heal_cluster": heal_cluster, "split_judge": split_judge, "propose": propose,
       "movement_judge": movement_judge, "gloss_judge": gloss_judge,
       "fact_close_judge": fact_close_judge, "takeover_judge": takeover_judge, "same_judge": same_judge, "same_family": same_family, "describe": describe, "curate_probe": curate_probe, "decision_check": decision_check,
       # the switch's two calls as ONE ask, so the bench drives production's judgement (2026-09-26)
       "curate_window": curate_window}
