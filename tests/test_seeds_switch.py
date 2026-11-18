"""The ★SEED switch (2026-11-10): a tenant with a row in the hub's seeds_off
gets none of the five service rules — not in the brief's law, not in the
rule match, not in the pool count — and its own rules are untouched. Every
other tenant keeps them."""
import asyncio

from hub import compose, jobs


def _rows():
    return [("app_voice", "App voice", "Stay in character.", "all", 0, "2026-11-10", None)]


def _fetchall(off: bool):
    async def fetchall(sql, args=()):
        if "seeds_off" in sql:
            return [(1,)] if off else []
        return _rows()
    return fetchall


def _ids(law):
    return {r["rule_id"] for r in law["rules"]}


def test_seeds_off_drops_the_five_and_keeps_the_tenant_s_own():
    law, rules = asyncio.run(compose.law_for(_fetchall(True), {}, "t1", "u_fan"))
    assert _ids(law) == {"app_voice"}
    assert not any(r["seed"] for r in rules)


def test_seeds_on_is_unchanged_for_everyone_else():
    law, _ = asyncio.run(compose.law_for(_fetchall(False), {}, "t1", "u_fan"))
    assert set(jobs.SEED_RULES) <= _ids(law)
    assert "app_voice" in _ids(law)


def test_rules_for_default_still_appends_the_seeds():
    rows = [r[:6] for r in _rows()]
    assert len([r for r in jobs.rules_for(rows, "x") if r["seed"]]) == len(jobs.SEED_RULES)
    assert [r for r in jobs.rules_for(rows, "x", seeds=False) if r["seed"]] == []
