"""Leaked-text redaction — the ONE door into protected history.

Founder design 2026-08-12: "removal is manual, human-only, via the panel" —
no detection, no scanning, no agent surface, archive otherwise immutable.
Founder mechanics 2026-08-24: from the admin panel, with confirmation, the
account OWNER can remove one exact string (a leaked key, a password) from
chat memory — the memory the 90-day floor protects from every other
deletion path. At most once per hour.

WHAT IT DOES: replaces every occurrence of the text with [REDACTED], in raw
chat history AND curated records (title + content). Case-sensitive exact
match — credentials are exact strings. The tsv columns are GENERATED, so
keyword search forgets the secret in the same statement. Embeddings are
left in place: a vector is a semantic trace, not a ciphertext — the exact
credential cannot be read back out of it.

WHAT IT NEVER DOES: store or log the needle. The needle IS the secret; the
redaction_log keeps only timestamps and row counts, so the log of removing
a leak can never become the leak.

This verb exists on NO agent surface — internal endpoint only, driven by
the panel's session-authenticated human, exactly like project deletion
(2026-08-12: "nothing an agent can reach destroys the archive").
"""
from __future__ import annotations

from . import store

NEEDLE_MIN = 6           # shorter would shred prose ("the", "key", …)
NEEDLE_MAX = 100         # founder 2026-08-24: "max of hundred characters"
COOLDOWN_S = 3600        # founder 2026-08-24: "once an hour, or something"
MARK = "[REDACTED]"

_DDL = """
CREATE TABLE IF NOT EXISTS redaction_log (
    id        bigserial PRIMARY KEY,
    ts        timestamptz NOT NULL DEFAULT now(),
    chat_rows int NOT NULL DEFAULT 0,
    doc_rows  int NOT NULL DEFAULT 0
);
"""


def _like(needle: str) -> str:
    esc = (needle.replace("\\", "\\\\")
                 .replace("%", "\\%")
                 .replace("_", "\\_"))
    return f"%{esc}%"


def redact(needle: str, execute: bool = False) -> dict:
    """Count (execute=False) or remove (execute=True) one exact string from
    this tenant's chat + document rows. Bound tenant context.

    Returns {chat_rows, doc_rows, executed, rate_limited, retry_after_s}.
    Raises ValueError on a needle outside the 6..100 bound — the caller
    turns that into a 400, and the message never echoes the needle."""
    needle = (needle or "").strip()
    if not (NEEDLE_MIN <= len(needle) <= NEEDLE_MAX):
        raise ValueError(
            f"text to redact must be {NEEDLE_MIN}-{NEEDLE_MAX} characters")
    pattern = _like(needle)
    with store.connection() as conn:
        cur = conn.cursor()
        store.ensure_ddl(cur, "redact", _DDL)
        if execute:
            # The hourly valve — one redaction per tenant per hour, so the
            # door can never become a bulk-rewrite tool for the archive.
            cur.execute("SELECT EXTRACT(EPOCH FROM (now() - max(ts))) "
                        "FROM redaction_log;")
            row = cur.fetchone()
            since = row[0] if row and row[0] is not None else None
            if since is not None and since < COOLDOWN_S:
                conn.commit()
                return {"chat_rows": 0, "doc_rows": 0, "executed": False,
                        "rate_limited": True,
                        "retry_after_s": int(COOLDOWN_S - since)}
            cur.execute(
                "UPDATE chat SET text = replace(text, %s, %s) "
                "WHERE text LIKE %s ESCAPE '\\';",
                [needle, MARK, pattern])
            chat_rows = cur.rowcount
            cur.execute(
                "UPDATE document SET title = replace(title, %s, %s), "
                "  content = replace(content, %s, %s), updated_at = now() "
                "WHERE title LIKE %s ESCAPE '\\' "
                "   OR content LIKE %s ESCAPE '\\';",
                [needle, MARK, needle, MARK, pattern, pattern])
            doc_rows = cur.rowcount
            cur.execute("INSERT INTO redaction_log (chat_rows, doc_rows) "
                        "VALUES (%s, %s);", [chat_rows, doc_rows])
            conn.commit()
            print(f"[redact] {store.current_tenant()}: {chat_rows} chat + "
                  f"{doc_rows} document rows redacted (needle length "
                  f"{len(needle)})", flush=True)
            return {"chat_rows": chat_rows, "doc_rows": doc_rows,
                    "executed": True, "rate_limited": False,
                    "retry_after_s": 0}
        cur.execute("SELECT count(*) FROM chat "
                    "WHERE text LIKE %s ESCAPE '\\';", [pattern])
        chat_rows = int(cur.fetchone()[0])
        cur.execute("SELECT count(*) FROM document "
                    "WHERE title LIKE %s ESCAPE '\\' "
                    "   OR content LIKE %s ESCAPE '\\';", [pattern, pattern])
        doc_rows = int(cur.fetchone()[0])
        conn.commit()
        return {"chat_rows": chat_rows, "doc_rows": doc_rows,
                "executed": False, "rate_limited": False, "retry_after_s": 0}
