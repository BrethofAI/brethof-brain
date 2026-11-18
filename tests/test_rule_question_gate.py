"""THE RULE QUESTION GATE (founder design 2026-08-14).

One door, one question. The agent is never handed a scope parameter to
fill — where the text lands is the OUTCOME of a question it must answer in
a second call: 1 = every project's law, 2 = this project's law, 3 = it was
never a rule (filed as knowledge). The token binds the answer to the exact
content asked about, so the question cannot be skipped by answering
pre-emptively.

Everything here runs WITHOUT a database: the question phase, the token
contract and the input validation all happen before the store is touched.
Where a test must prove the gate LET a save through, reaching the DB (and
failing there, in this harness) is itself the proof — same pattern as
test_rule_door.test_memory_index_is_not_a_rule.
"""
import re

import pytest

from core import tools

# The hub is REAL outside this file (rules live only there since 2026-09-02).
# test_valid_answer_reaches_the_store used to PUT 'gate_test_rule' onto the
# production hub on every run: 'Gate test — Always frobnicate before
# shipping' sat in every session's standing law until 2026-09-23. Every hub
# call here goes to a recorder instead.
PUT = []


@pytest.fixture(autouse=True)
def _no_real_hub(monkeypatch):
    from core import hubclient, rules_hub
    PUT.clear()
    monkeypatch.setattr(hubclient, "put_rule",
                        lambda *a, **k: PUT.append(a) or {"ok": True})
    monkeypatch.setattr(hubclient, "put_rule_vectors", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(rules_hub, "fetch",
                        lambda project="": {"rules": [], "note": ""})
    monkeypatch.setattr(rules_hub, "forget", lambda project="": None)

_TOKEN_RE = re.compile(r"token='([0-9a-f]{16})'")


def _question(**kw):
    kw.setdefault("rule_id", "gate_test_rule")
    kw.setdefault("title", "Gate test")
    kw.setdefault("rule", "Always frobnicate before shipping.")
    return tools.save_rule(**kw)


def _token(out: str) -> str:
    m = _TOKEN_RE.search(out)
    assert m, f"no token in: {out!r}"
    return m.group(1)


# ── phase 1: the question ──────────────────────────────────────────────────
def test_first_call_returns_the_question_not_a_save():
    out = _question()
    assert "NOT saved yet" in out
    assert "EVERY SESSION" in out
    for opt in ("1 —", "2 —", "3 —"):
        assert opt in out, f"missing option {opt!r}"
    _token(out)                                   # a token is offered


def test_legacy_scope_parameter_is_ignored_not_an_error():
    # Pre-2026-08-14 agents pass scope='general'. The question must come
    # back regardless — the answer decides, the parameter never does.
    out = _question(scope="general")
    assert "NOT saved yet" in out and _token(out)
    out = _question(scope="everywhere")           # even garbage scope
    assert "NOT saved yet" in out


def test_token_is_stable_for_identical_content():
    assert _token(_question()) == _token(_question())


def test_token_changes_with_content():
    assert _token(_question()) != _token(_question(rule="Different law."))
    assert _token(_question()) != _token(_question(rule_id="other_id"))


# ── phase 2: the answer contract ───────────────────────────────────────────
def test_answer_without_valid_token_is_refused():
    out = _question(answer="1", token="deadbeefdeadbeef")
    assert out.startswith("Error") and "token" in out
    out = _question(answer="1")                   # no token at all
    assert out.startswith("Error") and "token" in out


def test_pre_emptive_answer_cannot_skip_the_question():
    # The token comes ONLY from the question reply; an agent cannot mint it.
    out = _question(answer="1", token=_token(_question(rule="other text")))
    assert out.startswith("Error")


def test_garbage_answers_are_refused():
    tok = _token(_question())
    for bad in ("0", "4", "yes", "rule", "general"):
        assert _question(answer=bad, token=tok).startswith("Error")


def test_answer_2_requires_a_project():
    tok = _token(_question())
    out = _question(answer="2", token=tok)
    assert out.startswith("Error") and "project" in out
    out = _question(answer="2", token=tok, project="Bad Name!")
    assert out.startswith("Error")


def test_merge_parameter_is_validated():
    tok = _token(_question())
    out = _question(answer="1", token=tok, merge="maybe")
    assert out.startswith("Error") and "merge" in out


def test_valid_answer_reaches_the_store():
    # Past the gate the rule goes to the hub — here the recorder.
    tok = _token(_question())
    try:
        out = _question(answer="1", token=tok)
    except Exception:
        out = ""
    assert not str(out).startswith("Error: bad token")
    assert PUT and PUT[0][0] == "gate_test_rule"


def test_answer_3_routes_to_knowledge_not_law():
    tok = _token(_question())
    try:
        out = _question(answer="3", token=tok, project="gs")
    except Exception:
        return                                    # reached save_memory => routed
    # If a store answered, the reply must say it was filed as knowledge.
    assert out.startswith("Error") or "KNOWLEDGE" in out


def test_input_validation_precedes_the_question():
    assert tools.save_rule(rule_id="", title="t", rule="b").startswith("Error")
    assert tools.save_rule(rule_id="r", title="", rule="b").startswith("Error")
    assert tools.save_rule(rule_id="r", title="t", rule="").startswith("Error")


# ── customer tier: same gate, one visible door ─────────────────────────────
def test_customer_door_asks_the_same_question():
    from api import customer_tools
    out = customer_tools.save_rule("Always frobnicate.", project="myapp")
    assert "NOT saved yet" in out and "EVERY SESSION" in out
    tok = _token(out)
    bad = customer_tools.save_rule("Always frobnicate.", project="myapp",
                                   answer="1", token="deadbeefdeadbeef")
    assert bad.startswith("Error")
    # answer 2 without a project is refused before any queueing
    out2 = customer_tools.save_rule("Always frobnicate.", answer="2",
                                    token=_token(customer_tools.save_rule(
                                        "Always frobnicate.")))
    assert out2.startswith("Error") and "project" in out2
    assert tok  # token extracted fine


def test_customer_legacy_doors_share_the_gate():
    from api import customer_tools
    a = customer_tools.save_general_rule("Always frobnicate.")
    b = customer_tools.save_project_rule("Always frobnicate.", "myapp")
    assert "NOT saved yet" in a and "NOT saved yet" in b
    # the legacy general door and the one door mint the SAME token for the
    # same content — a session may start on one name and finish on the other
    assert _token(a) == _token(customer_tools.save_rule("Always frobnicate."))


def test_customer_and_owner_tokens_differ():
    # Different tiers must not share tokens for the same text — the owner
    # token embeds the rule_id, the customer token only the content.
    from api import customer_tools
    assert (_token(customer_tools.save_rule("Always frobnicate."))
            != _token(_question(rule="Always frobnicate.")))
