"""delete_project is a FULL, permanent project delete — founder intent
(2026-08-09): everything about the project goes, including the raw chat
archive, because keeping history for a project that no longer exists serves
nobody. The 2026-08-09 GLM audit's real finding was a docstring that LIED
("history never touched") while the code deleted it; the fix is honesty +
safety, not keeping history.

PANEL-ONLY since 2026-08-12 (founder ruling): the verb is gone from the
customer tool surface — an agent typing its own confirm token is a typo
guard, not an authority check, and this is the one act that reaches the
chat archive at any age. The function survives for
/v1/internal/project_delete, called by the control plane after the account
owner types the project name in the panel. This file pins BOTH contracts:
the delete is full and honest, and no agent surface exposes it.

Safety property kept from the audit: chat.project is RESTRICT, so chat can
only ever be destroyed by an EXPLICIT `DELETE FROM chat` (never an
incidental cascade). This test pins that delete_project (a) issues that
explicit chat delete, (b) does it BEFORE removing the project row, and
(c) tells the caller history is gone — permanently.
"""
import unittest
from unittest import mock

import api.customer_tools as ct


def _run_delete():
    conn = mock.MagicMock()
    cur = conn.cursor.return_value
    cur.rowcount = 3
    with mock.patch.object(ct.store, "connection") as sc, \
         mock.patch.object(ct, "_writable", return_value=True), \
         mock.patch.object(ct, "_visible", return_value=True), \
         mock.patch("core.rules_hub.fetch", return_value={"rules": [], "purpose": "", "note": ""}), \
         mock.patch("core.hubclient.delete_rule", return_value=True), \
         mock.patch("core.hubclient.delete_project", return_value={"gone": {}}):
        # THE LAST MOCK IS NOT DECORATION (2026-09-10): without it this
        # "offline" test posted a real authenticated project-delete to
        # hub.brethof.ai every time it ran — 120 of them on the day it was
        # found, deleting the rules and purpose of a project called
        # 'myproj' for whichever tenant's key was in the environment. It
        # deleted nothing only because no such project existed.
        sc.return_value.__enter__.return_value = conn
        out = ct.delete_project(project="myproj", confirm="myproj")
    order = [" ".join(str(c.args[0]).split())
             for c in cur.execute.call_args_list if c.args]
    return out, order


class DeleteProjectFull(unittest.TestCase):
    def test_confirm_prompt_warns_history_is_deleted(self):
        out = ct.delete_project(project="myproj", confirm="")
        low = out.lower()
        self.assertIn("conversation history", low)
        self.assertIn("confirm", low)
        # must NOT falsely promise history is safe (the original sin)
        self.assertNotIn("history stays", low)
        self.assertNotIn("never touched", low)

    def test_full_delete_removes_chat_explicitly(self):
        out, order = _run_delete()
        joined = " || ".join(order)
        # since 2026-09-09 the delete takes the project's rows AS OWNED TODAY
        # (core/ownership.chat_where): with no moves that is the plain filter
        self.assertIn("DELETE FROM chat WHERE project = %s", joined)
        self.assertIn("DELETE FROM project WHERE name=%s", joined)
        self.assertIn("DELETE FROM document WHERE project=%s", joined)

    def test_chat_deleted_before_project_row(self):
        """RESTRICT FK demands chat go first, or the project-row delete errors."""
        _, order = _run_delete()
        chat_i = next(i for i, s in enumerate(order)
                      if s.startswith("DELETE FROM chat"))
        proj_i = next(i for i, s in enumerate(order)
                      if s.startswith("DELETE FROM project"))
        self.assertLess(chat_i, proj_i)

    def test_result_states_permanent(self):
        out, _ = _run_delete()
        self.assertIn("cannot be undone", out.lower())


class DeleteProjectSurfaceByShape(unittest.TestCase):
    """Founder 2026-08-12: no agent-reachable verb touches the archive on
    OUR infrastructure. Founder 2026-08-31: on a TRUE-LOCAL box the agent
    owns the machine — the wall is decorative and a clean tool beats the
    improvised SQL its absence produces — so there, and only there, the
    verb exists. Hosted stacks (selfhosted mode WITH panel_ops) keep the
    panel as the only surface."""

    def test_present_exactly_on_true_local(self):
        from core import mode
        expected = mode.is_selfhosted() and not mode.panel_ops()
        self.assertEqual("delete_project" in ct.CUSTOMER_TOOLS, expected)
        from api import mcp_app
        self.assertEqual("delete_project" in mcp_app.CUSTOMER_SPECS, expected)

    def test_confirm_is_still_demanded_when_present(self):
        from core import mode
        if not ("delete_project" in ct.CUSTOMER_TOOLS):
            self.skipTest("not a local surface here")
        out = ct.CUSTOMER_TOOLS["delete_project"]("someproj", confirm="wrong")
        self.assertIn("confirm", out.lower())


if __name__ == "__main__":
    unittest.main()
