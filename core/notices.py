"""PENDING NOTICES — the back-channel for work that finished after the caller
was already answered.

WHY THIS EXISTS (2026-08-08, found by the release gate on its first real run):
a customer save is an INTENT. The tool answers "Remembering it — filed into
memory within a minute" and the gate files it later, on a queue. That contract
is right, but it had a hole with no floor under it: when the gate could NOT
file the intent, nobody was ever told. The queue row went to status='failed',
the reason went to the server log, and the customer's agent — which had been
told the opposite — carried on believing its law was saved. Silent data loss
behind a success message is the worst failure this product can have, so the
asynchronous path now owes the caller an answer it cannot skip.

HOW IT WORKS: the gate writes a one-line notice here; the next tool call from
that tenant, whatever tool it is, carries the notice back appended to its
result. The customer's agent is an LLM — it reads the sentence, and the notice
carries the fix (merge into that id, trim the rule, free the pool), so it can
act on the next turn. Delivery is one round trip late by construction; that is
the price of the intent design and it is worth paying.

Notices are advisory and drain-on-read: they are deleted as they are handed
over. If that response is lost in transit the notice is lost with it — the
alternative (acknowledged delivery) would need state the MCP protocol has no
place for, and the durable record of what happened is the gate_job row, which
keeps its payload and its error forever.
"""
from __future__ import annotations

from . import store

# A notice queue is a symptom, not a mailbox: past this many the tenant has a
# systemic problem and the oldest lines are the least useful.
MAX_PENDING = 20
MAX_PER_CALL = 3

_DDL = """
CREATE TABLE IF NOT EXISTS gate_notice (
    id         bigserial PRIMARY KEY,
    text       text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
"""


def add(text: str) -> None:
    """Queue one notice for the bound tenant. Never raises: a notice that
    cannot be stored must not also destroy the job that produced it."""
    text = (text or "").strip()
    if not text:
        return
    try:
        with store.connection() as conn:
            cur = conn.cursor()
            store.ensure_ddl(cur, "notices", _DDL)
            # Identical text already waiting = the same problem twice; one
            # line says it just as well as five.
            cur.execute("SELECT 1 FROM gate_notice WHERE text=%s;", [text])
            if not cur.fetchone():
                cur.execute("INSERT INTO gate_notice (text) VALUES (%s);",
                            [text])
                cur.execute(
                    "DELETE FROM gate_notice WHERE id NOT IN "
                    "  (SELECT id FROM gate_notice ORDER BY id DESC LIMIT %s);",
                    [MAX_PENDING])
            conn.commit()
    except Exception:                                    # noqa: BLE001
        pass


def drain() -> str:
    """Take the pending notices for the bound tenant and return them as text
    to append to a tool result, or "" when there are none. Never raises — this
    runs on the response path of every customer tool call."""
    try:
        with store.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT to_regclass('public.gate_notice');")
            if not (cur.fetchone() or [None])[0]:
                return ""                    # nothing has ever queued a notice
            cur.execute("DELETE FROM gate_notice WHERE id IN "
                        "  (SELECT id FROM gate_notice ORDER BY id LIMIT %s) "
                        "RETURNING text;", [MAX_PER_CALL])
            rows = [r[0] for r in cur.fetchall()]
            conn.commit()
    except Exception:                                    # noqa: BLE001
        return ""
    if not rows:
        return ""
    head = ("\n\n⚠ FROM MEMORY, about an earlier save (it was accepted, then "
            "could not be filed — please act on this):\n")
    return head + "\n".join(f"- {t}" for t in rows)
