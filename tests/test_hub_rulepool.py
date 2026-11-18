"""THE LAW BUDGETS, back on the hub (founder 2026-08-07; dropped with the box's
rulecap on 2026-09-02; restored 2026-09-10 — "there should be a message at
80% and at 100%"): a pool per scope, seeds never counted, an update never
counting its own old weight, the brief's notice past 80% and FULL at 100%,
and a save that would overfill its pool refused with the fix attached."""
from hub import jobs


def _r(rid, area, n, seed=False):
    return {"rule_id": rid, "title": "t", "content": "x" * (n - 1), "area": area, "seed": seed}


def test_pool_used_counts_its_scope_only_never_seeds_never_the_row_itself():
    rules = [_r("a", "all", 100), _r("b", "global", 50), _r("c", "ms", 700), _r("★seed", "all", 900, seed=True)]
    assert jobs.pool_used(rules, None) == 150
    assert jobs.pool_used(rules, "all", exclude_id="a") == 50
    assert jobs.pool_used(rules, "ms") == 700 and jobs.pool_used(rules, "bv") == 0


def test_the_brief_says_80_percent_and_full(monkeypatch):
    monkeypatch.setattr(jobs, "GENERAL_POOL", 1000)
    monkeypatch.setattr(jobs, "PROJECT_POOL", 500)
    quiet = [_r("a", "all", 700), _r("p", "ms", 300)]
    assert jobs.pool_lines(quiet, "ms") == []
    nag = [_r("a", "all", 850), _r("p", "ms", 420)]
    lines = jobs.pool_lines(nag, "ms")
    assert len(lines) == 2 and "general rule pool is at 85%" in lines[0] and "'ms' rule pool is at 84%" in lines[1]
    full = [_r("a", "all", 1000), _r("p", "ms", 100)]
    lines = jobs.pool_lines(full, "ms")
    assert len(lines) == 1 and "general rule pool is FULL" in lines[0] and "refused" in lines[0]
    assert jobs.pool_lines(full, "") == lines, "no project: the general pool alone"


def test_a_save_that_overfills_is_refused_with_the_fix_and_an_update_replaces_its_weight(monkeypatch):
    monkeypatch.setattr(jobs, "PROJECT_POOL", 500)
    rules = [_r("p1", "ms", 300), _r("p2", "ms", 150)]
    why = jobs.pool_refusal(rules, "p3", "ms", "t", "x" * 100)
    assert why.startswith("rule NOT saved") and "'ms' rule pool is full" in why and "Merge" in why
    assert jobs.pool_refusal(rules, "p1", "ms", "t", "x" * 340) == "", "rewriting p1 frees its old 300"
    assert jobs.pool_refusal(rules, "p3", "ms", "t", "x" * 49) == ""


def test_put_rule_checks_the_pool_before_taking_a_unit():
    src = open("hub/server.py", encoding="utf-8").read()
    body = src.split("async def put_rule(", 1)[1].split("\n@app.", 1)[0]
    assert body.index("jobs.pool_refusal(") < body.index("take_units(tenant, rule_units(rule_id))")
    assert body.index("len(content) > MAX_RULE_CHARS") < body.index("take_units(tenant, rule_units(rule_id))")


def test_the_composed_law_carries_the_notices_first():
    import asyncio
    from core import composer
    law = {"rules": [{"rule_id": "r", "title": "Gate", "content": "always frobnicate", "area": "all", "pin": True}],
           "purpose": "p", "note": "", "pool_notes": ["⚠ the general rule pool is at 90% (x)"]}

    async def ask(op, body):
        return {"ok": True}
    secs = asyncio.run(composer.sections(ask, law, "global", "owner"))
    rules_section = dict(secs)["RULES — STANDING LAW"]
    assert "always frobnicate" in rules_section and rules_section.startswith("⚠ the general rule pool is at 90% (x)")
