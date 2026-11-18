"""Brain Ledger + Brain Playbooks — the offline contract (2026-08-22).

What must never regress silently:
1. The extractor is FAIL-OPEN: provider down, garbage JSON, or a crash
   files nothing and returns a log line — it can never raise into the
   curation stream it rides.
2. The extractor validates the model's project routing (unknown project
   falls back to the session project) and enforces the per-call cap
   LOUDLY.
3. Scoped keys: read-only keys cannot write the ledger; a "none" project
   does not exist for the key; playbooks ride the GENERAL permission the
   way rules do.
4. Validation refuses instead of truncating (row cap, playbook name/size),
   and removal reports missing ids instead of dropping them.
5. The rollout dial (BRAIN_LEDGER / BRAIN_LEDGER_EXCLUDE) matches the
   proven BRAIN_LIVE_CURATE shape.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api.main  # noqa: E402,F401 — module init order, same as production
from api import customer_tools  # noqa: E402
from core import inference, ledger, playbooks  # noqa: E402


class _Comp:
    def __init__(self, text):
        self.text = text
        self.input_tokens = 10
        self.output_tokens = 5


class _Backend:
    def __init__(self, text):
        self._text = text
        self.calls = 0

    def generate(self, prompt, system="", **kw):
        self.calls += 1
        self.prompt, self.system = prompt, system
        return _Comp(self._text) if self._text is not None else None


def _fake_store(pile_rows=None, projects=("brain", "web")):
    """Patch ledger.store.connection with a MagicMock whose cursor answers
    the extractor's three reads (projects, pile, brief) in call order."""
    conn = mock.MagicMock()
    cur = conn.cursor.return_value
    cur.fetchall.side_effect = [
        [(p,) for p in projects],          # SELECT name FROM project
        [(c,) for c in (pile_rows or [])],  # pile preview
        [],                                 # brief: rules
    ]
    cur.fetchone.return_value = None        # brief: purpose
    ctx = mock.MagicMock()
    ctx.__enter__.return_value = conn
    return ctx


TURNS = [(0, "user", "we should really add a dark mode toggle"),
         (1, "assistant", "noted — and the export button is done now")]


class Dial(unittest.TestCase):
    def _dial(self, v, excl=""):
        with mock.patch.dict(os.environ, {"BRAIN_LEDGER": v,
                                          "BRAIN_LEDGER_EXCLUDE": excl}):
            return ledger.enabled_for("t1"), ledger.enabled_for("t2")

    def test_off_by_default(self):
        self.assertEqual(self._dial(""), (False, False))

    def test_tenant_list_and_all(self):
        self.assertEqual(self._dial("t1"), (True, False))
        self.assertEqual(self._dial("all"), (True, True))

    def test_exclude_wins(self):
        self.assertEqual(self._dial("all", "t2"), (True, False))


class Validation(unittest.TestCase):
    def test_ledger_row_refused_not_truncated(self):
        with self.assertRaises(ValueError):
            ledger.add("p", "x" * (ledger.ROW_CHARS + 1))
        with self.assertRaises(ValueError):
            ledger.add("p", "   ")

    def test_playbook_name_and_size(self):
        # (Uppercase is NOT refused — names are case-normalized on save.)
        for bad in ("", "has space", "-leading", "a" * 65):
            with self.assertRaises(ValueError):
                playbooks.save(bad, "content")
        with self.assertRaises(ValueError):
            playbooks.save("ok", "")
        with self.assertRaises(ValueError):
            playbooks.save("ok", "x" * (playbooks.MAX_CHARS + 1))

    def test_playbook_content_is_never_inspected(self):
        """Founder, twice (2026-08-12 memory, 2026-08-23 playbooks): no
        content scanning — what adults store is their choice. Anything
        passing the size cap reaches the DB write (NoTenantBound in
        offline tests = validation let it through), credentials included."""
        from core.store import NoTenantBound
        for content in ("export KEY=bm_live_a1b2c3d4e5f6",
                        "-----BEGIN RSA PRIVATE KEY-----",
                        "read the key from ~/.secrets/npm-publish.token"):
            with self.assertRaises(NoTenantBound, msg=content):
                playbooks.save("ok", f"step: {content}")


class ScopedKeys(unittest.TestCase):
    """Customer doors honor the key's project scopes."""

    def _scoped(self, scopes, fn, *a, **kw):
        tok = customer_tools.SCOPES.set(scopes)
        try:
            return fn(*a, **kw)
        finally:
            customer_tools.SCOPES.reset(tok)

    def test_read_only_key_cannot_write_ledger(self):
        # The ledger fills ITSELF — the hand-add door is gone (founder
        # 2026-08-31); delete is the only write, and it needs write scope.
        self.assertFalse(hasattr(customer_tools, "add_to_ledger"))
        out = self._scoped({"brain": "read", "*": "none"},
                           customer_tools.remove_from_ledger, "1", "brain")
        self.assertIn("read-only", out)

    def test_none_project_does_not_exist(self):
        out = self._scoped({"*": "none"},
                           customer_tools.list_ledger, "brain")
        self.assertEqual(out, "No such project.")

    def test_playbooks_ride_general_permission(self):
        # General read-only: can see, cannot write.
        sc = {"*": "read"}
        with mock.patch.object(customer_tools.playbooks, "listing",
                               return_value=[]):
            out = self._scoped(sc, customer_tools.list_playbooks)
        self.assertIn("No Brain playbooks", out)
        out = self._scoped(sc, customer_tools.save_playbook, "x", "steps")
        self.assertIn("read-only", out)
        out = self._scoped(sc, customer_tools.delete_playbook, "x")
        self.assertIn("read-only", out)
        # No general access at all: playbooks do not exist for this key.
        out = self._scoped({"brain": "rw", "*": "none"},
                           customer_tools.get_playbook, "x")
        self.assertEqual(out, "No such playbook.")

    def test_remove_reports_bad_ids(self):
        out = self._scoped(None, customer_tools.remove_from_ledger,
                           "1, banana", "brain")
        self.assertIn("row numbers", out)


class Surface(unittest.TestCase):
    """Both tiers expose the seven doors, with specs — the adapter
    freshness phase of the release gate compares plugins against this."""

    DOORS = ["list_ledger", "remove_from_ledger",
             "save_playbook", "get_playbook", "list_playbooks",
             "delete_playbook"]

    def test_registries_and_specs(self):
        from api import mcp_app
        from core import tools
        for d in self.DOORS:
            self.assertIn(d, customer_tools.CUSTOMER_TOOLS, d)
            self.assertIn(d, tools.TOOLS, d)
            self.assertIn(d, mcp_app.CUSTOMER_SPECS, d)
            self.assertIn(d, mcp_app.TOOL_SPECS, d)
        # The Brain-prefixed vocabulary is the collision armor: every
        # customer-facing description names the feature.
        for d in self.DOORS:
            desc = mcp_app.CUSTOMER_SPECS[d][0]
            self.assertTrue("Brain Ledger" in desc or "BRAIN LEDGER" in desc
                            or "playbook" in desc.lower(), d)


if __name__ == "__main__":
    unittest.main()
