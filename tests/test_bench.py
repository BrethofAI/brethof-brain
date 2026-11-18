"""The bench (core/bench, founder 2026-09-20: "we've got 30 cards, so we
can test… with closing, without closing"). The hub's graph prompt variants
stay pinned here while the hub still answers old boxes."""
from __future__ import annotations

import pathlib

from core import bench
from hub import judgetext as J
from core.prompts import TEXTS

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_bench_tenant_names_are_prefixed_and_guarded():
    assert bench.tenant_of("v0") == "bench_v0"
    assert bench.is_bench("bench_v0") and not bench.is_bench("memory")
    for bad in ("", "v 0", "v-0", "../x"):
        try:
            bench.tenant_of(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")


def test_workers_never_iterate_bench_tenants():
    src = (ROOT / "core" / "store.py").read_text(encoding="utf-8")
    assert 'not n.startswith("bench_")' in src


def test_hub_picks_a_variant_prompt_and_falls_back():
    base = J.graph_system("p", [], variant="")
    assert base == J.graph_system("p", [], variant="no_such_variant")
    TEXTS["graph_bench_test"] = "VARIANT {today} {purpose}"
    try:
        assert J.graph_system("p", [], variant="bench_test").startswith("VARIANT ")
    finally:
        del TEXTS["graph_bench_test"]


def test_door_is_owner_only():
    src = (ROOT / "api" / "main.py").read_text(encoding="utf-8")
    body = src.split('"/v1/tools/bench"')[1][:600]
    assert "require_full_access" in body


def test_variant_prompts_change_one_node_each():
    g, v1, v2 = TEXTS["graph"], TEXTS["graph_v1"], TEXTS["graph_v2"]
    assert "MOVEMENTS." in g and '"movements"' in g
    assert "MOVEMENTS." not in v1 and '"movements"' not in v1 and "ENDED" not in v1
    assert "took over another's job" in v1
    assert v2.startswith(v1[:2000])
    assert "ENDED." in v2 and '"ended":[{{"name"' in v2 and '"relations":[],"ended":[]' in v2
    for v in (v1, v2):
        v.format(today="x", purpose="y")          # the braces still balance


def test_worker_switch_has_its_import():
    """The lifespan reads BRAIN_WORKERS through os — a module that forgot
    the import failed at boot (bench-api, 2026-09-20) while the suite,
    which never runs the lifespan, stayed green."""
    src = (ROOT / "api" / "main.py").read_text(encoding="utf-8")
    assert "\nimport os\n" in src and 'os.environ.get("BRAIN_WORKERS"' in src


def test_thinking_is_a_bench_flag_only():
    src = (ROOT / "hub" / "judge.py").read_text(encoding="utf-8")
    assert '"think": bool(think)' in src and 'think=bool(flags.get("think"))' in src
    assert src.count("think=bool(flags.get") == 1, "only the graph read takes the flag"


def test_v2b_carries_the_corrected_ended_teaching():
    v2b = TEXTS["graph_v2b"]
    assert "QUOTE THE DEED, NOT THE ANNOUNCEMENT" in v2b and "a RENAME" in v2b
    assert '"ended":[{{"name"' in v2b and "MOVEMENTS." not in v2b
    v2b.format(today="x", purpose="y")


def test_v4_adds_continues_on_top_of_v2():
    v4 = TEXTS["graph_v4"]
    assert "CONTINUES." in v4 and '"continues":[{{"src"' in v4 and "ENDED." in v4
    v4.format(today="x", purpose="y")
    src = (ROOT / "hub" / "judgetext.py").read_text(encoding="utf-8")
    assert "open facts: " in src


def test_v5_keeps_movements_and_adds_ended():
    v5 = TEXTS["graph_v5"]
    assert "MOVEMENTS." in v5 and '"movements":[{{"name"' in v5 and "ENDED." in v5 and '"ended":[{{"name"' in v5
    v5.format(today="x", purpose="y")


def test_the_old_graph_rigs_left_the_bench():
    """The rigs that read the old inventory graph — load, drain, cards,
    fold, close, facts, fact_cards, describe, probe — left on 2026-09-27;
    the decision lines' rigs stay."""
    for gone in ("load", "load_async", "drain", "cards", "fold", "close", "facts", "fact_cards",
                 "describe", "probe", "keep_answer", "variant_of"):
        assert not hasattr(bench, gone), gone
    for kept in ("decisions", "decision_find", "decision_dump", "heal_replay", "heal_dump",
                 "record_pairs", "record_cross", "status", "drop", "tenant_of", "runaway_guard"):
        assert callable(getattr(bench, kept)), kept
