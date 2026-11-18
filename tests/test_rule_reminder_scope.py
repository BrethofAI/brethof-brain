"""The start-of-message hook asks the HUB which rule the prompt touches, and
the hub's verdict gates the whole ambient block (founder 2026-09-03: rules live
on the hub and in the session's context, nowhere else; the Brain speaking
unasked is the paid feature). Nothing about rules is kept on the box."""
import importlib.util
import inspect
import pathlib

import asyncio

from core import composer, injection, rules_hub

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("hj", ROOT / "hub/jobs.py")
hj = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(hj)


def test_the_box_keeps_no_rule_copy_and_no_memo():
    src = inspect.getsource(rules_hub)
    assert "_memo: dict" not in src and "MEMO_S" not in src and "monotonic" not in src
    # the rule a prompt touches is matched ON THE HUB (design step 2): the box
    # sends the prompt with its vector and injects what comes back
    src = inspect.getsource(injection.ambient_blob)
    assert 'pipeclient.ask("ambient"' in src and '"vector": vec' in src and "rules_hub" not in src


def test_hub_unreachable_means_one_line_at_start_and_silence_on_a_prompt(monkeypatch):
    from core import embed, pipeclient

    def down(op, body, timeout=None):
        raise pipeclient.PipeDown("no pipe to the hub")
    monkeypatch.setattr(pipeclient, "ask", down)
    monkeypatch.setattr(embed, "embed_one", lambda *a, **k: None)
    out = injection.session_blob("web", part=1, session_id="sess")
    assert "NOT AVAILABLE" in out and injection.UNREACHABLE in out and out.startswith("brethof-brain — project: web")
    assert injection.session_blob("web", part=2, session_id="sess") == ""
    assert injection.ambient_blob("web", "sess", "a long enough prompt about deploying the harbor scheduler") == ""


def test_the_hub_composes_from_its_own_law_and_matches_the_rule_by_vector():
    import asyncio
    import importlib.util
    _spec2 = importlib.util.spec_from_file_location("hc", ROOT / "hub/compose.py", submodule_search_locations=[])
    from hub import compose as hc
    rows = [("git", "Local git only", "private repos never go to GitHub", "all", 1, "2026-09-10", hj.pack_vec([1.0, 0.0, 0.0] + [0.0] * 6)),
            ("purpose_web", "★ PROJECT PURPOSE — web", "the website", "web", 1, "2026-09-10", None)]

    async def fetchall(sql, args=()):
        return rows
    asked = []

    async def ask(cmd, body):
        asked.append(cmd)
        if cmd == "projects":
            return {"ok": True, "projects": [{"name": "web", "records": 3}]}
        if cmd == "recall":
            return {"ok": True, "hits": []}
        if cmd == "stamps":
            return {"ok": True, "turn": 7, "stamps": {}}
        return {"ok": True}
    b = asyncio.run(hc.brief(ask, fetchall, {}, "t1", {"project": "web", "part": 0, "session_id": "s"}))
    assert b["ok"] and "## RULES — STANDING LAW" in b["text"] and "- **Local git only**" in b["text"]
    assert "★ PROJECT PURPOSE — web" in b["text"] and "- web (3)  ← this session" in b["text"]
    assert "notes" in asked and "recap" in asked
    # the old inventory graph's reads left the brief and the prompt block (2026-09-27)
    assert not {"since", "graph_summary"} & set(asked)
    vec = [0.9, 0.1, 0.0] + [0.0] * 6
    a = asyncio.run(hc.ambient(ask, fetchall, {}, "t1", {"project": "web", "session_id": "s", "part": 1,
                                                         "prompt": "should this repo be pushed to github tonight?", "vector": vec}))
    assert a["ok"] and "rule in force" in a["text"] and "Local git only" in a["text"]
    assert "stamp" in asked and "cards" not in asked
    a2 = asyncio.run(hc.ambient(ask, fetchall, {}, "t1", {"project": "web", "session_id": "s2", "part": 1,
                                                          "prompt": "tell me about the weather in the harbour"}))
    assert a2["ok"] and a2["text"] == ""


def test_hub_match_needs_the_laws_name_and_ignores_words_every_rule_shares():
    rules = [{"rule_id": "tailscale", "title": "NO TAILSCALE — everything rides Cloudflare Tunnel",
              "content": "never diagnose or route through tailscale; record nothing about it"},
             {"rule_id": "ports", "title": "Never guess a port", "content": "take it from the registry; record the assignment"},
             {"rule_id": "ledger", "title": "The Ledger has ONE operation: DELETE",
              "content": "never rewrite a record; delete rows; record nothing else"},
             {"rule_id": "tidy", "title": "Keep it clean and tidy — DELETE what we do not use",
              "content": "delete files we do not use; never park junk; record what you deleted"}]
    hit = hj.match_rule(rules, "should the tailscale tunnel carry the backup route tonight?")
    assert hit and hit["rule_id"] == "tailscale"        # two title words: tailscale, tunnel
    # "record" and "delete" sit in most rules: they must not carry a match
    assert hj.match_rule(rules, "please record which things we should delete later") is None
    assert hj.match_rule(rules, "hello there") is None and hj.match_rule(rules, "") is None
    # a body-only overlap without the law's name in the title is not a match
    assert hj.match_rule(rules, "the registry assignment looks wrong") is None


def test_six_prompt_hooks_rule_or_record_playbook_or_record_then_records():
    src = inspect.getsource(composer.ambient)
    assert "if 2 <= part <= PARTS:" in src and "slot[1] = None if rule_present else" in src
    assert "slot[2] = playbook if playbook else" in src and composer.PARTS == 6
    assert injection._RECORD_SLOTS == 5 and injection._STRONG == 35.0
    import json
    hooks_file = ROOT.parent / "brethof-brain-client/plugin/hooks/hooks.json"
    if not hooks_file.exists():      # the client is its own repo; checked only beside it
        return
    hooks = json.loads(hooks_file.read_text())
    cmds = [h["command"] for g in hooks["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
    # six record/rule/playbook parts plus part 7, the history of decisions
    # (2026-11-18: the plugin registers it; composer.HISTORY_PART == PARTS + 1)
    assert sorted(c.split()[-1] for c in cmds) == ["1", "2", "3", "4", "5", "6", "7"]
    assert composer.HISTORY_PART == 7


def test_sibling_parts_share_one_computation():
    calls = []
    key = ("t", "sess", "abc")

    async def first():
        calls.append(1); return 42

    async def second():
        calls.append(1); return 99
    a = asyncio.run(composer.shared(key, "x", first))
    b = asyncio.run(composer.shared(key, "x", second))
    assert a == b == 42 and calls == [1]


def test_hub_vector_match_uses_the_old_calibration():
    import math
    def unit(*xs):
        n = math.sqrt(sum(x * x for x in xs)); return [x / n for x in xs]
    rules = [{"rule_id": "a", "title": "NO TAILSCALE", "content": "...", "vec": unit(1, 0, 0)},
             {"rule_id": "b", "title": "Never guess a port", "content": "...", "vec": unit(0, 1, 0)}]
    assert hj.match_rule_vec(rules, unit(0.9, 0.1, 0), "anything")["rule_id"] == "a"      # sim .99
    assert hj.match_rule_vec(rules, unit(0.3, 0.3, 0.9), "anything") is None            # both < .43, no title word
    assert hj.match_rule_vec(rules, unit(0.3, 0.3, 0.9), "the tailscale box")["rule_id"] == "a"  # title rescue >= .25
    # a word that names no single law does not rescue
    assert hj.match_rule_vec(rules, unit(0.3, 0.3, 0.9), "please think about anything") is None
    assert hj.match_rule_vec([{"rule_id": "c", "title": "t", "content": ""}], unit(1, 0, 0), "x") is None
    blob = hj.pack_vec([0.5, -1.0, 2.0]); assert hj.unpack_vec(blob) == [0.5, -1.0, 2.0]


def test_an_ephemeral_harness_gets_its_record_again_inside_the_cooldown():
    """A harness that adds our text to ONE model call (OpenClaw's
    prependContext, OpenCode 2's context hook) never keeps it in the
    conversation, so a record shown on turn 6 is gone from the model's view on
    turn 7. The record cooldown assumes the opposite; such a harness says
    "ephemeral" and its records are not cooled (2026-10-03, the Windows rig)."""
    from hub import compose as hc

    async def fetchall(sql, args=()):
        return []

    async def ask(cmd, body):
        if cmd == "recall":
            return {"ok": True, "hits": [{"rid": "web:deploy_window", "score": 80,
                                          "row": {"type": "fact", "title": "Deploy window",
                                                  "content": "deploys run Tuesdays at 14:00"}}]}
        if cmd == "stamps":       # shown on turn 6, this is turn 7: cooling
            return {"ok": True, "turn": 7, "stamps": {"web:deploy_window": 6}}
        return {"ok": True}
    body = {"project": "web", "session_id": "s", "part": 1, "prompt": "when do deploys run on this project?"}
    kept = asyncio.run(hc.ambient(ask, fetchall, {}, "t1", body))
    lifted = asyncio.run(hc.ambient(ask, fetchall, {}, "t1", {**body, "ephemeral": True}))
    assert "deploys run Tuesdays" not in kept["text"]
    assert "deploys run Tuesdays" in lifted["text"]
