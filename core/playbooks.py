"""BRAIN PLAYBOOKS — the tenant-wide store of the user's own procedures.

A playbook is the USER'S creation and property: their script, their command
sequence, the schema of how a job runs. We store and serve it — we never
generate one, never judge one, never execute one (there is no execution
backend, no terminal, and that is an architectural fact of the product,
not a missing feature: the agent fetches, the agent executes in its own
harness). Founder 2026-08-22, incl. the explicit rejection of any
system-side "proven" stamp — if the user wants to note "proven, runs
nightly", that is their text in their annotation.

Scoped TENANT-WIDE on purpose: a proven procedure is the one thing that is
genuinely cross-project — born in one project, useful from any. Any agent
in the tenant can "run the deploy playbook" without knowing which project
wrote it. Four doors, nothing more: save (upsert = create AND edit),
get (fetching IS running), list (discovery), delete.
"""
from __future__ import annotations

import re

from . import store

_DDL = """
CREATE TABLE IF NOT EXISTS playbook (
    name        text PRIMARY KEY,
    content     text NOT NULL,
    annotation  text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
"""

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# NO CONTENT INSPECTION, deliberately (founder, twice): 2026-08-12 —
# scanning customer content for credentials is a content-reading problem
# that contradicts zero-trust positioning; 2026-08-23 — "storing
# credentials in the Brain is probably safer than most self-hosted
# setups. That's the user's choice." A credential-shape guard shipped
# here for a few hours and was removed the same day: one door scanning
# while every other door accepts text uninspected was arbitrary, and
# playbooks are editable rows — a user can fully remove anything
# themselves. What adults store is theirs.
# Generous — a playbook is a script or a step list, not a data dump. The
# refusal is loud; nothing is ever silently truncated.
MAX_CHARS = 100_000
MAX_ANNOTATION = 500


_SEEDED: set[str] = set()


def _ensure(cur) -> None:
    store.ensure_ddl(cur, "playbook", _DDL)
    # THE THREE METHODS every install starts with (core/seedplaybooks,
    # 2026-09-29): once per tenant per process, at the store's own door, so
    # a new tenant has them at its first session start and an existing one
    # at its next. Committed on its own here — it runs first in the caller's
    # transaction — and never the caller's failure: said loudly, retried next
    # process.
    t = store.current_tenant()
    if t in _SEEDED:
        return
    from . import seedplaybooks
    try:
        got = seedplaybooks.ensure(cur)
        cur.connection.commit()
        _SEEDED.add(t)
        if any(v in ("seeded", "updated") for v in got.values()):
            print(f"[playbooks] {t}: methods {got}", flush=True)
    except Exception as e:                                   # noqa: BLE001
        cur.connection.rollback()
        store.ensure_ddl(cur, "playbook", _DDL)
        print(f"[playbooks] {t}: seeding the methods FAILED — {type(e).__name__}: {e}", flush=True)


def save(name: str, content: str, annotation: str = "") -> str:
    """Upsert. Returns 'created' or 'updated' so an overwrite of a
    teammate's playbook is always said out loud, never silent."""
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        raise ValueError("playbook name: lowercase letters, digits, '-' and "
                         "'_', up to 64 chars (e.g. 'deploy-brain-site')")
    content = (content or "").rstrip()
    if not content:
        raise ValueError("empty playbook — nothing to save")
    if len(content) > MAX_CHARS:
        raise ValueError(f"playbook is {len(content)} chars; the cap is "
                         f"{MAX_CHARS}. Split it, or store the bulk "
                         f"elsewhere and make the playbook the steps.")
    with store.connection() as conn:
        cur = conn.cursor()
        _ensure(cur)
        cur.execute(
            "INSERT INTO playbook (name, content, annotation) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (name) DO UPDATE SET content = EXCLUDED.content, "
            "  annotation = EXCLUDED.annotation, updated_at = now() "
            "RETURNING (xmax = 0);",
            [name, content, (annotation or "").strip()[:MAX_ANNOTATION]])
        created = cur.fetchone()[0]
        conn.commit()
    return "created" if created else "updated"


def get(name: str, cur=None) -> tuple | None:
    """(name, content, annotation, updated_at) or None. A caller that already
    holds a connection passes its cursor: the brief opened a second one here
    (2026-09-10) and eight parallel session-start parts on a pool of four
    each held one and waited for another until the pool timed out."""
    if cur is not None:
        return _get(cur, name)
    with store.connection() as conn:
        return _get(conn.cursor(), name)


def _get(cur, name: str) -> tuple | None:
    _ensure(cur)
    cur.execute("SELECT name, content, annotation, updated_at "
                "FROM playbook WHERE name = %s;",
                [(name or "").strip().lower()])
    return cur.fetchone()


def listing() -> list[tuple]:
    """(name, annotation, chars, updated_at) alphabetical."""
    with store.connection() as conn:
        cur = conn.cursor()
        _ensure(cur)
        cur.execute("SELECT name, annotation, length(content), updated_at "
                    "FROM playbook ORDER BY name;")
        return cur.fetchall()


def delete(name: str) -> bool:
    with store.connection() as conn:
        cur = conn.cursor()
        _ensure(cur)
        cur.execute("DELETE FROM playbook WHERE name = %s;",
                    [(name or "").strip().lower()])
        gone = cur.rowcount > 0
        conn.commit()
    return gone
