"""Pins the customer-tier boundary (founder design 2026-07-28).

Three properties, each of which failing would be silent in production:
1. Key parsing: `key:tenant:tier` resolves; missing tier = owner (existing key
   files stay valid); an UNKNOWN tier fails CLOSED to customer.
2. The MCP dispatch enforces the tier — a customer key calling an owner tool
   gets 'unknown tool', and the customer listing carries no mechanism
   vocabulary (the whole point of the tier is that the scheme never crosses
   the wire).
3. The system-record guard: customers can neither read nor delete the
   service's own bookkeeping records, and the refusal is indistinguishable
   from the record not existing.
"""
import importlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import auth  # noqa: E402
import api.main  # noqa: E402,F401  (module init order: main before mcp_app,
#                 same as production — mcp_app alone trips the circular import)


def _reload_auth(keys: str):
    os.environ["BRAIN_V2_KEYS"] = keys
    os.environ.pop("BRAIN_V2_KEYS_FILE", None)
    importlib.reload(auth)
    return auth


def test_tier_parsing_and_default():
    a = _reload_auth("k1:t1,k2:t2:customer,k3:t3:owner")
    assert a.resolve("k1") == "t1" and a.resolve_tier("k1") == "owner"
    assert a.resolve("k2") == "t2" and a.resolve_tier("k2") == "customer"
    assert a.resolve_tier("k3") == "owner"
    assert a.resolve("nope") is None and a.resolve_tier("nope") is None
    assert a.tenants() == ["t1", "t2", "t3"]


def test_unknown_tier_fails_closed():
    a = _reload_auth("k1:t1:admin")          # typo'd tier
    assert a.resolve("k1") == "t1"
    assert a.resolve_tier("k1") == "customer"


def test_customer_cannot_call_owner_tools():
    _reload_auth("k1:t1")
    from api import mcp_app
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "query_raw", "arguments": {"sql": "SELECT 1"}}}
    resp = mcp_app._handle(msg, tier="customer")
    assert "error" in resp and "unknown tool" in resp["error"]["message"]
    # save_memory (raw, id-addressed) is owner-only too
    msg["params"] = {"name": "save_memory", "arguments": {}}
    resp = mcp_app._handle(msg, tier="customer")
    assert "error" in resp
    # An unknown TIER gets the customer surface, not the owner one
    resp = mcp_app._handle(msg, tier="weird")
    assert "error" in resp


def test_customer_listing_is_capability_only():
    from api import mcp_app
    listing = mcp_app._handle({"jsonrpc": "2.0", "id": 1,
                               "method": "tools/list"}, tier="customer")
    tools_ = listing["result"]["tools"]
    names = {t["name"] for t in tools_}
    # ONE rule door (founder 2026-08-14): save_general_rule /
    # save_project_rule left the LISTED surface but stay dispatchable for
    # sessions opened before the change.
    expected = {"save_general", "save_project", "save_note", "delete_note", "list_saved",
                "update_record", "save_rule",
                "list_rules", "list_brain",
                "get_record", "delete_record", "search_brain",
                "search_history", "list_projects", "add_project",
                "cleanup_history",
                "move_session", "dismiss_proposal", "session_context",
                # Brain Ledger + Brain Playbooks (founder GO 2026-08-22)
                "list_ledger", "remove_from_ledger",
                "save_playbook", "get_playbook", "list_playbooks",
                "delete_playbook",
                # the turn tools, hooks for a connector (2026-10-23)
                "begin_turn", "end_turn", "period_report", "save_verdicts", "list_verdicts"}
    from core import mode
    if mode.is_selfhosted() and not mode.panel_ops():
        # TRUE-LOCAL ONLY (founder 2026-08-31): the agent owns that machine,
        # so the clean deletion tool exists there — and only there.
        expected |= {"delete_project"}
    assert names == expected, names.symmetric_difference(expected)
    # delete_project left OUR-infrastructure agent surfaces 2026-08-12
    # (founder): on cloud and hosted stacks, full project deletion is a
    # HUMAN act, panel-only. No agent-reachable verb touches the archive
    # where we enforce anything.
    if not mode.is_selfhosted() or mode.panel_ops():
        assert "delete_project" not in names
    blob = " ".join(t["description"] for t in tools_).lower()
    # "graph" left this list 2026-07-30: the knowledge graph became a
    # customer-facing FEATURE (founder-directed), not hidden mechanism.
    for word in ("vector", "bm25", "rrf", "embed", "hybrid", "fused",
                 "rerank", "index", "catalog", "sql", "upsert", "curat"):
        assert word not in blob, f"mechanism word '{word}' leaks to customers"
    owner_listing = mcp_app._handle({"jsonrpc": "2.0", "id": 1,
                                     "method": "tools/list"}, tier="owner")
    assert {t["name"] for t in owner_listing["result"]["tools"]} \
        == set(mcp_app.TOOL_SPECS)


def test_system_record_guard():
    from api import customer_tools as ct
    assert ct._is_system("global", "memory_index")
    assert ct._is_system("state", "anything")
    assert ct._is_system("global", "changelog")
    assert not ct._is_system("myproj", "api_design_decision")
    # The refusal reads like absence, and refuses BEFORE touching storage
    assert ct.get_memory("global:memory_index") == "No such record."
    assert ct.delete_memory("state:open_loops") == "No such record."


def test_project_scopes_three_stage():
    """Founder 2026-07-28: '3 stage read/write/not seen'. A 'none' project
    reads like absence; 'read' refuses writes with a real reason; scopes=None
    is full access; missing project falls to the '*' default, and no '*' at
    all means none."""
    from api import customer_tools as ct
    tok = ct.SCOPES.set({"web": "rw", "docs": "read", "*": "none"})
    try:
        assert ct._mode("web") == "rw" and ct._writable("web")
        assert ct._mode("docs") == "read" and not ct._writable("docs")
        assert ct._mode("secret") == "none" and not ct._visible("secret")
        # not-seen: every surface reads like absence, refused BEFORE storage
        assert ct.get_memory("secret:anything") == "No such record."
        assert ct.list_memory("secret") == "No such project."
        assert ct.delete_project("secret") == "No such project."
        assert ct.save_project("x", "secret") == "No such project."
        # read-only: visible but write-refused, with an honest reason
        assert "read-only" in ct.save_project("x", "docs")
        assert "read-only" in ct.delete_memory("docs:some_record")
        assert "read-only" in ct.delete_project("docs")
        # rw project: delete_project still demands the confirm token
        # (the function survives for the panel's internal endpoint — the
        # guards stay even though no agent key can reach it any more)
        assert "confirm" in ct.delete_project("web")
    finally:
        ct.SCOPES.reset(tok)
    tok = ct.SCOPES.set(None)
    try:
        assert ct._mode("anything") == "rw"      # full access
    finally:
        ct.SCOPES.reset(tok)
    tok = ct.SCOPES.set({})                      # corrupt caps fail closed
    try:
        assert not ct._visible("anything")
    finally:
        ct.SCOPES.reset(tok)


def test_control_key_parsing():
    """auth._control_lookup response handling is network-bound, but the tier
    default and fail-closed parsing around it are not: a static key resolves
    scopes=None (full), and an unknown key resolves nothing."""
    a = _reload_auth("k1:t1")
    assert a.resolve_scopes("k1") is None
    assert a.resolve_scopes("nope") is None
    assert len(a.key_hash("abc")) == 64


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_"):
            fn()
            print(f"ok {name}")
    print("ALL PASS")
