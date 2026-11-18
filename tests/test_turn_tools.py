"""The turn tools (2026-10-23): begin_turn / end_turn give a harness with no
hooks (a cloud agent's MCP connector) the hooks' three jobs, through the very
doors the hooks call. Pinned here with the doors faked at their one seam
(customer_tools._hook) — the doors themselves are the hooks' own tests."""
from unittest import mock

import pytest

from api import customer_tools as ct


@pytest.fixture()
def doors():
    calls = []

    def fake_hook(name, payload):
        calls.append((name, payload))
        return {"session_start": {"status": "ok", "injection": "BRIEF"},
                "prompt_submit": {"status": "ok", "injection": "RECALL"},
                "stop": {"status": "ok"}}[name]
    ct._PENDING.clear()
    with mock.patch.object(ct, "_hook", fake_hook), \
            mock.patch.object(ct.store, "current_tenant", lambda: "t_x"), \
            mock.patch.object(ct, "_next_index", lambda sid: 4):
        yield calls


def test_a_new_conversation_gets_the_brief_then_the_recall(doors):
    out = ct.begin_turn("what did we decide about pricing?", project="Shop")
    names = [n for n, _ in doors]
    assert names == ["session_start", "prompt_submit"]
    sid = doors[0][1]["session_id"]
    assert sid.startswith("conn-") and doors[1][1]["session_id"] == sid
    assert doors[1][1]["prompt"] == "what did we decide about pricing?"
    assert doors[0][1]["project"] == "shop"
    assert f"session_id={sid}" in out and out.index("BRIEF") < out.index("RECALL")


def test_a_later_message_gets_the_recall_only(doors):
    out = ct.begin_turn("and the launch date?", session_id="conn-abc", project="shop")
    assert [n for n, _ in doors] == ["prompt_submit"]
    assert "RECALL" in out and "BRIEF" not in out


def test_end_turn_archives_the_whole_exchange_in_order(doors):
    ct.begin_turn("hello", session_id="conn-1", project="shop")
    out = ct.end_turn("hi — the launch is Friday", session_id="conn-1", project="shop")
    name, payload = doors[-1]
    assert name == "stop" and payload["session_id"] == "conn-1"
    assert [(t["index"], t["line_type"], t["text"]) for t in payload["turns"]] == \
        [(4, "user", "hello"), (5, "assistant", "hi — the launch is Friday")]
    assert out.startswith("Saved")


def test_end_turn_without_its_begin_keeps_the_reply_alone(doors):
    ct.end_turn("a reply", session_id="conn-lost")
    turns = doors[-1][1]["turns"]
    assert [t["line_type"] for t in turns] == ["assistant"]


def test_a_refused_archive_is_reported_not_hidden(doors):
    with mock.patch.object(ct, "_hook", lambda n, p: {"status": "over_cap", "notice": "storage full"}):
        assert "Not saved (storage full)" in ct.end_turn("r", session_id="conn-2")


def test_no_project_files_under_chat_and_a_bad_name_is_not_used(doors):
    ct.begin_turn("x")
    assert doors[0][1]["project"] == "chat"
    ct.begin_turn("y", project="../etc")
    assert doors[-1][1]["project"] == "chat"


def test_a_scoped_key_is_refused(doors):
    tok = ct.SCOPES.set({"shop": "rw"})
    try:
        assert "full-access key" in ct.begin_turn("x")
        assert "full-access key" in ct.end_turn("r", session_id="s")
    finally:
        ct.SCOPES.reset(tok)
    assert doors == []


def test_both_are_on_the_listed_surface_and_survive_a_limit():
    from api import main  # noqa: F401 — the app first, as it is loaded in service
    from api import mcp_app
    for n in ("begin_turn", "end_turn"):
        assert n in ct.CUSTOMER_TOOLS and n in mcp_app.CUSTOMER_SPECS and n in ct.LIMITED_TOOLS


def test_the_last_step_closes_what_begin_turn_hands_back(doors):
    out = ct.begin_turn("hello", session_id="conn-9", project="shop")
    assert out.rstrip().splitlines()[-1].startswith("LAST STEP") and 'session_id="conn-9"' in out


def test_a_skipped_end_turn_still_archives_the_users_words_and_says_so(doors):
    ct.begin_turn("first question", session_id="conn-s", project="shop")
    out = ct.begin_turn("second question", session_id="conn-s", project="shop")
    stops = [p for n, p in doors if n == "stop"]
    assert [[(t["line_type"], t["text"]) for t in p["turns"]] for p in stops] == \
        [[("user", "first question")]]
    assert "NOT saved" in out
    ct.end_turn("answer two", session_id="conn-s", project="shop")
    assert [t["text"] for t in doors[-1][1]["turns"]] == ["second question", "answer two"]


def test_an_answered_turn_does_not_warn(doors):
    ct.begin_turn("q", session_id="conn-ok", project="shop")
    ct.end_turn("a", session_id="conn-ok", project="shop")
    assert "NOT saved" not in ct.begin_turn("q2", session_id="conn-ok", project="shop")


def test_every_listed_tool_carries_honest_hints():
    from api import main  # noqa: F401
    from api import mcp_app
    for tier in ("owner", "customer"):
        for n in mcp_app._TIER_SPECS[tier]:
            h = mcp_app._hints(n)
            assert set(h) == {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
            if n.startswith(("delete_", "cleanup_", "remove_", "update_")):
                assert h["destructiveHint"] and not h["readOnlyHint"], n
            if n.startswith(("search_", "list_", "get_")):
                assert h["readOnlyHint"], n
    assert mcp_app._hints("begin_turn")["destructiveHint"] is False
    assert mcp_app._hints("a_tool_nobody_named")["destructiveHint"] is True
