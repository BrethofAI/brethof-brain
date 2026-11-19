"""The mechanical brief — no teacher, nothing compiled, nothing that can grow.

Founder 2026-09-02, after two A/Bs on real exchanges: a teacher-compiled
category layer measured equal to the purpose plus one filter-written line per
bearing rule, and the teacher's maintainer was the source of 176 of brain's
188 track lines (48k chars, 91% of every curator call). These guard the
properties that decision bought:

  - the section is rendered as RULES, never as an allowlist;
  - its size is the rule table's, not a model's;
  - the filter's verdict carries the instruction line;
  - the hub no longer knows how to teach.
"""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("hj", ROOT / "hub/jobs.py")
jobs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jobs)

BASE = ("You curate {project}. TODAY is {today}.\n\n{project_section}\n\n"
        "Projects: {project_list}\nJSON: {{\"records\":[]}}")


def test_section_is_purpose_plus_rule_lines_and_nothing_else():
    out = jobs.render_brief(BASE, "brain", "  A memory   product. ",
                            ["Record X as law", "  Never file Y  "], ["a", "b"])
    assert "ABOUT THIS PROJECT: A memory product." in out
    assert "RULES THAT BEAR ON RECORDING HERE:\n- Record X as law\n- Never file Y" in out
    assert "Projects: a, b" in out
    assert 'JSON: {"records":[]}' in out          # base braces collapsed
    assert "{today}" in out                        # survives to load time
    for banned in ("TRACK", "allowlist", "silence default", "WORKED EXAMPLES"):
        assert banned not in out, banned


def test_no_description_is_said_in_the_prompt_not_hidden():
    out = jobs.render_brief(BASE, "p", "", [], [])
    assert "ABOUT THIS PROJECT: (no description yet)" in out
    assert "RULES THAT BEAR" not in out


def test_size_is_bounded_by_the_rule_table():
    """A line longer than LINE_MAX is cut; N lines render N lines. There is
    no path by which the section gets longer than what the rules put in."""
    lines = ["x" * 1000] * 5
    out = jobs.render_brief(BASE, "p", "purpose", lines, [])
    sec = out.split("RULES THAT BEAR ON RECORDING HERE:\n", 1)[1]
    sec = sec.split("\n\nProjects:", 1)[0]
    assert sec.count("\n- ") + 1 == 5
    assert all(len(l) <= jobs.LINE_MAX + 2 for l in sec.splitlines())


def test_purpose_braces_are_evidence_not_format():
    out = jobs.render_brief(BASE, "p", "code like {x: 1} and {{y}}", [], [])
    assert "code like {x: 1} and {{y}}" in out


def test_verdict_carries_the_line_and_fails_open_without_one():
    bears, why, line = jobs.rule_verdict(
        {"bears": True, "why": "w", "line": "  Record   it  "})
    assert (bears, line) == (True, "Record it")
    bears, why, line = jobs.rule_verdict({"bears": False, "why": "conduct"})
    assert (bears, line) == (False, "")
    bears, why, line = jobs.rule_verdict(None)
    assert bears is True and line == ""
    bears, why, line = jobs.rule_verdict({"bears": True, "line": "z" * 500})
    assert len(line) == 200


def test_the_hub_cannot_teach():
    src = (ROOT / "hub/server.py").read_text(encoding="utf-8")
    jsrc = (ROOT / "hub/jobs.py").read_text(encoding="utf-8")
    for dead in ("async def _teach(", "async def teach2(", "rule_wake(",
                 "BIRTH_BUF", "LAST_SESSION", "FROM brief"):
        assert dead not in src, dead
    for dead in ("add_track", "apply_teach2_ops", "TEACH2_OPS"):
        assert dead not in jsrc, dead
    from core.prompts import TEXTS
    assert "teacher_sys" not in TEXTS and "teacher2" not in TEXTS
    assert '"line"' in TEXTS["rulefilter"]


def test_the_curator_no_longer_names_entities_and_the_hub_has_no_registrar():
    """2026-09-08: the graph is read on the box by its own worker, the
    registrar and its shadow index are gone. 2026-09-17: the curator answers
    records and ledger only — movements are the graph reader's (founder:
    "only the graph needs the movement")."""
    from core.prompts import TEXTS
    t = TEXTS["curator"]
    assert '"entities"' not in t and '"moved":[' not in t and "MOVEMENTS" not in t
    assert '"movements":[' in TEXTS["graph"] and "MOVEMENTS." in TEXTS["graph"]
    assert "registrar" not in TEXTS
    src = (ROOT / "hub/server.py").read_text(encoding="utf-8")
    assert "_register(" not in src and "/v1/index/" not in src and "/v1/types/" not in src
    assert "entity_name" not in src and "entity_type" not in src
    assert not hasattr(jobs, "apply_registrar") and not hasattr(jobs, "near_names")


def test_the_ledger_rides_in_the_curator_answer():
    from core.prompts import TEXTS
    assert "ledger" not in TEXTS                      # no separate extractor
    assert '"ledger":[' in TEXTS["curator"]           # the curator carries it
    src = (ROOT / "hub/server.py").read_text(encoding="utf-8")
    assert "_extract_ledger" not in src
    assert 'obj.get("ledger")' in src


def test_rules_for_hands_a_box_its_law_and_the_seeds():
    rows = [("r_all", "T", "c", "all", 1, "2026-09-02"),
            ("r_web", "T", "c", "web", 1, "2026-09-02"),
            ("r_tpl", "T", "c", "u_%", 1, "2026-09-02"),
            ("purpose_web", "★ PROJECT PURPOSE — web", "what web is", "web", 1, "x")]
    got = {r["rule_id"] for r in jobs.rules_for(rows, "web")}
    assert got == {"r_all", "r_web", "purpose_web", "seed_organize_by_project", "seed_four_doors", "seed_ledger_is_the_catcher", "seed_standing_documents", "seed_three_kinds", "seed_rules_are_law"}
    assert {r["rule_id"] for r in jobs.rules_for(rows, "u_alice")} >= {"r_all", "r_tpl"}
    assert "r_web" not in {r["rule_id"] for r in jobs.rules_for(rows, "u_alice")}
    assert len(jobs.rules_for(rows, "")) == len(rows) + len(jobs.SEED_RULES)
    seeds = [r for r in jobs.rules_for(rows, "web") if r["seed"]]
    assert all(r["title"].startswith("★SEED") and r["area"] == "all" for r in seeds)
    assert jobs.is_seed_id("seed_rules_are_law") and not jobs.is_seed_id("rules_are_law")


def test_a_deleted_project_takes_its_hub_footprint_with_it():
    src = (ROOT / "hub/server.py").read_text(encoding="utf-8")
    assert '@app.delete("/v1/project/{project}")' in src
    # (the registrar's index tables are gone, 2026-09-08 — nothing to delete)
    for tbl in ("DELETE FROM rule WHERE", "DELETE FROM rule_verdict WHERE"):
        assert tbl in src.split('@app.delete("/v1/project/{project}")', 1)[1].split("@app.", 1)[0], tbl
    assert "/v1/retrieve" not in src                    # the dead socket is gone
    ct = (ROOT / "api/customer_tools.py").read_text(encoding="utf-8")
    assert "hubclient.delete_project(project)" in ct


def test_plan_next_lines_take_the_ordered_items_of_the_next_section_only():
    """The plan's Next lines ride the brief beside the notes (2026-09-09)."""
    from core.injection import plan_next_lines
    content = ("# x-plan\n\n## Decided\n- a decision\n\n## Next\n\n"
               "1. First thing to do. Finished when done.\n"
               "2. Second thing.\n"
               "3. Third.\n\n## Parked\n- parked idea\n")
    assert plan_next_lines(content, limit=2) == [
        "1. First thing to do. Finished when done.", "2. Second thing."]
    assert plan_next_lines("## Parked\n- x\n") == []
    assert plan_next_lines("") == []
