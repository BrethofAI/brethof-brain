"""The uniform response envelope — byte-compatible with v1 on purpose.

Keeping the exact shape is what lets the SAME client talk to either server by
changing one base URL. That is the whole side-by-side test plan: v1 keeps
serving live memory while v2 answers the same calls, and switching back is a
config change rather than a rollback.

The envelope itself exists because a memory system that fails silently is worse
than one that is down — the client must be able to tell "nothing to inject" from
"over cap" from "auth failed".
"""
from __future__ import annotations

from typing import Any

# ok | over_cap | read_only | auth_failed | provisioning | unknown_project | server_error
Status = str


def envelope(status: Status, injection: str = "", notice: str = "",
             retry_after: int | None = None, **extra: Any) -> dict:
    body = {"status": status, "injection": injection, "notice": notice,
            "retry_after": retry_after}
    body.update(extra)
    return body


def ok(injection: str = "", **extra: Any) -> dict:
    return envelope("ok", injection=injection, **extra)


def over_cap(notice: str, retry_after: int | None = None, **extra: Any) -> dict:
    return envelope("over_cap", notice=notice, retry_after=retry_after, **extra)


def read_only(notice: str, **extra: Any) -> dict:
    return envelope("read_only", notice=notice, **extra)


def server_error(notice: str = "") -> dict:
    return envelope("server_error", notice=notice)
