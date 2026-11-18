"""No prompt may reach a model still carrying `.format()` escaping.

WHY THIS FILE EXISTS, measured 2026-09-02. The registrar's prompt ended with
its output template written for `.format()`:

    {{"entities":[{{"given":"...","name":"...","minted":true|false}}]}}

`.format()` is never applied to that prompt — registrar._sys() returns
promptsrc.get("registrar") verbatim and the hub's registrar route passes
PROMPTS.get("registrar") straight through — so the doubled braces reached the
model as literal text. GLM copied them faithfully:

    {{"entities":[{{"given":"Brain","name":"brain",...}}]}}

which is not JSON. parse_json_loose returned None, `answers` came back empty,
and resolve() FAILS OPEN by design, so every entity was stored exactly as the
curator wrote it. The registrar had never renamed anything, in silence, for as
long as the prompt had existed — 77 twin pairs put to it answered 71
"distinct", including `Brain`/`brain`. After the fix: 75 merges, 2 refusals,
both correct (`cloudflared` vs `~/.cloudflared`, `vault` vs `vault_*`).

Nothing alarmed, because a fail-open path is supposed to be quiet. The only
signal was a graph slowly filling with twins, which looks like a model being
bad at its job rather than a parser throwing every answer away.

THE INVARIANT: a doubled OPENING brace is always wrong in rendered prompt
text. `}}` alone is fine — that is a nested JSON object closing, which the
designer's template legitimately has ("ambient": {...}}).
"""
from __future__ import annotations

import datetime

import pytest

from core.prompts import TEXTS

TODAY = datetime.date.today().isoformat()

# Each prompt AS ITS CALLER RENDERS IT. A prompt whose caller runs .format()
# is checked post-format, which is the only state the model ever sees.
FORMATTED = {"reconcile", "reconcile_one", "reconcile_clean", "reconcile_T1", "reconcile_T2", "reconcile_T3", "reconcile_T4", "reconcile_T5", "reconcile_T6", "reconcile_T7", "reconcile_T8", "reconcile_U1", "reconcile_U2", "reconcile_U3", "reconcile_U4", "reconcile_U5", "reconcile_U6", "reconcile_U7", "reconcile_U8", "reconcile_W1", "reconcile_Y1", "reconcile_Y2", "reconcile_Y3", "reconcile_Y4", "reconcile_Z1", "reconcile_Z2", "reconcile_Z3", "reconcile_Z4", "reconcile_Z5", "reconcile_prev", "reconcile_G1", "reconcile_G2", "reconcile_G3", "reconcile_G4", "reconcile_G5", "reconcile_Z6", "reconcile_Z7", "graph", "graph_v1", "graph_v2", "graph_v2b", "graph_v4", "graph_v5", "graph_v6", "graph_v7", "graph_v8", "graph_v9", "graphheal", "split", "movement_judge", "gloss_judge",
             "fact_close_judge", "takeover_judge", "same_judge", "same_family", "describe", "decision_check", "decision_check1", "decision_check2", "decision_check3"}


def _as_used(name: str) -> str:
    text = TEXTS[name]
    # the retirement sweep's texts: J.retire_system and judge.retire_decide format them
    if name in FORMATTED or name == "retire" or name.startswith("retire_"):
        return text.format(today=TODAY, purpose="the project")
    if name in ("curator", "curator_graph", "curator_decisions", "curator_simple", "curator_records", "curator_lines",
                "curator_w1", "curator_w2", "curator_w3", "curator_batch_records", "curator_batch_lines", "curator_batch_lines2", "curator_batch_lines3", "curator_route", "curator_batch_records6", "curator_batch_records7", "curator_clean", "curator_split_records", "curator_batch_both", "curator_batch_lines4", "curator_batch_lines5", "curator_batch_both2", "curator_batch_both3", "curator_lines_threadN", "curator_lines_threadV", "curator_lines_threadNV", "curator_lines_threadV3", "curator_lines_threadV4", "curator_lines_threadV5", "curator_lines_threadV6", "curator_lines_threadV7", "curator_lines_threadV8", "curator_write_lines", "curator_write_records",
                "batch_records", "batch_lines", "curator_lines_threadV9", "batch_records_nt", "batch_records_nr", "batch_records_0926",
                "batch_records_nP", "batch_records_nG", "batch_records_nPG", "batch_lines_P", "batch_lines_K", "batch_lines_PK", "batch_lines_P2", "batch_lines_P3", "batch_records_0928", "batch_lines_0928",
                "batch_records_kA", "batch_records_kB", "batch_lines_kA", "batch_lines_kB",
                "batch_records_kC", "batch_lines_kC", "batch_records_kT", "batch_records_kU",
                "batch_records_P", "batch_records_N", "batch_records_PN", "batch_records_PNC", "batch_records_PNQ",
                "batch_records_TN", "batch_records_S", "batch_records_S0", "batch_records_F", "batch_records_FN", "batch_records_PNT", "batch_records_C1", "batch_records_C2", "batch_records_C3", "batch_records_C4", "batch_records_C5", "batch_records_C6", "batch_lines_kC2", "batch_records_E1", "batch_records_E2", "batch_records_E3", "batch_records_E4", "batch_lines_L1", "batch_lines_L2", "batch_lines_L3", "batch_lines_L4", "batch_lines_R1", "batch_lines_R2",
                "curator_batch_records2", "curator_batch_records3",
                "curator_batch_records4", "curator_batch_records5", "batch_records_noledger", "batch_records_noledgerD"):
        from hub import jobs
        return jobs.render_brief(text, "p", "", [], ["p", "q"]).replace("{today}", TODAY)
    return text


@pytest.mark.parametrize("name", sorted(TEXTS))
def test_no_format_escaping_survives_to_the_model(name: str) -> None:
    rendered = _as_used(name)
    assert "{{" not in rendered, (
        f"prompt {name!r} reaches the model with a doubled opening brace. "
        "Either its caller must run .format() on it, or the template's "
        "braces must be single. Left as-is the model copies '{{' into its "
        "reply and parse_json_loose returns None on every call — silently, "
        "because the callers fail open."
    )


@pytest.mark.parametrize("name", sorted(FORMATTED))
def test_formatted_prompts_still_render(name: str) -> None:
    """The opposite error: a lone {key} in a prompt that IS .format()ed."""
    TEXTS[name].format(today=TODAY, purpose="the project")   # raises KeyError/IndexError if wrong


def test_box_parser_skips_a_leaked_think_block():
    """glm-5.3 under reasoning_effort leaks <think>…</think> into the body and
    the reasoning drafts JSON, so the first '{' sits in the prose. The hub's
    parser has split on the closing tag since the GLM switch; the box's did
    not, and heal dropped a record after three good verdicts (2026-09-02)."""
    from core import inference
    leaked = ('<think>I should answer {"ops": []} or maybe delete {x}.\n'
              'Let me draft: {"ops":[{"op":"delete"</think>'
              '{"ops":[{"op":"delete","id":"a","reason":"dup"}]}')
    assert inference.parse_json_loose(leaked) == {
        "ops": [{"op": "delete", "id": "a", "reason": "dup"}]}
    assert inference.parse_json_loose("<think>cut off mid reason {") is None
    assert inference.parse_json_loose('{"ops":[]}') == {"ops": []}
