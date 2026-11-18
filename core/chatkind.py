"""WHAT AN ARCHIVE ROW REALLY IS (founder 2026-09-22).

The archive stores harness text under the role of the founder's own prompts:
agent reports (<task-notification>), command notices, and the recap a
session writes when it compacts. The founder: agent reports and notices
stay in view — "that's where you working, and really the most of the work
is done" — labelled by their real source, never as his words. A recap is a
model's retelling of turns the archive already holds: not stored, not
searched, never read as new conversation (on the bench slice 260 of 3,222
quoted graph facts were read out of recaps, each dated at the recap).

The archive is never edited, so old rows are recognised by how they start;
new recaps are simply not written.
"""
from __future__ import annotations

RECAP = ("This session is being continued from a previous conversation",)
AGENT_REPORT = ("<task-notification",)
SYSTEM_NOTICE = ("<local-command-caveat", "Caveat: The messages", "<command-name>",
                 "<local-command-stdout>", "<local-command-stderr>", "[SYSTEM NOTIFICATION")

def not_recap(alias: str = "", pct: str = "%%") -> str:
    """SQL: a chat row that is not a recap. `pct` is '%%' where the query
    carries params (psycopg), '%' where it does not."""
    return f"NOT (left({alias}text, 64) LIKE 'This session is being continued from a previous conversation{pct}')"


# for queries with params — every read that must never see a recap
NOT_RECAP = not_recap()


def kind(role, text) -> str:
    """'recap' | 'agent report' | 'system notice' | the stored role."""
    r = str(role or "user")
    if r != "user":
        return r
    head = str(text or "").lstrip()[:80]
    if head.startswith(RECAP):
        return "recap"
    if head.startswith(AGENT_REPORT):
        return "agent report"
    if head.startswith(SYSTEM_NOTICE):
        return "system notice"
    return "user"


def is_recap(role, text) -> bool:
    return kind(role, text) == "recap"
