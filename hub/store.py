"""THE HUB'S OWN STORE — a file, not a service.

Founder's decision 2026-08-27, and the reasoning is about attack surface
rather than size: this box holds every prompt, the provider key, and every
customer's brief. A database server on it is a second process with its own
auth, its own config and its own patch schedule. SQLite is a file the app
opens. There is nothing listening, nothing to harden, nothing to start in the
right order at boot.

THE SIZE MAKES IT EASY. Everything here is tenants, rules, type lists,
briefs, stage-0 verdicts and counters — a few hundred kilobytes per customer,
no vectors, no archive, no chat. Those live in the customer's container,
which keeps Postgres because it needs pgvector. The founder's own summary:
"we could probably even get away with MD files here." SQLite is that, plus
atomic writes and a query language.

READS ARE FREE, WRITES ARE RARE, AND THAT IS THE WHOLE DESIGN. WAL mode gives
unlimited concurrent readers, which is what this box mostly does — every
curation reads a brief and a type list. Writes are a new kind being minted, a
brief being compiled, a rule verdict cached once per rule, and one batched
usage flush every fifteen seconds. They go through a single lock, so WAL
never sees contention.

WHY THE CALLS ARE NOT THREADED. These are microsecond queries against a file
in the page cache; handing them to an executor would cost more in scheduling
than it saves. If this ever stops being true the lock is the place to change,
not the call sites.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import uuid

DB_PATH = os.environ.get("HUB_DB_PATH", "/srv/hub/hub.sqlite")

_conn: sqlite3.Connection | None = None
_write = asyncio.Lock()


def new_id() -> str:
    return str(uuid.uuid4())


def connect(path: str = "") -> sqlite3.Connection:
    """Open the store. WAL, foreign keys on, and a busy timeout so a writer
    waiting behind another WAITS instead of raising 'database is locked'."""
    global _conn
    if _conn is not None:
        return _conn
    p = path or DB_PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    c = sqlite3.connect(p, check_same_thread=False, isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL;")
    c.execute("PRAGMA synchronous=NORMAL;")
    c.execute("PRAGMA foreign_keys=ON;")
    c.execute("PRAGMA busy_timeout=5000;")
    _conn = c
    return c


def apply_schema(ddl: str) -> None:
    """Every statement is CREATE ... IF NOT EXISTS, so this runs on EVERY
    boot. The Postgres version of this box shipped a schema that ran only on
    first boot; a new table then deployed green and 500'd on first use."""
    connect().executescript(ddl)
    # COLUMN migrations — IF NOT EXISTS cannot add a column to a table that
    # already exists, so each new column is one guarded ALTER, run every
    # boot, no-op once applied. `tier` (2026-09-01, the quota door): the
    # BILLING tier, distinct from `plan` which historically carries the
    # SHAPE (hosted/selfhosted). Default 'internal' = uncapped, because
    # every row existing before the door was ours; the control plane names
    # the real tier on every mint from now on.
    try:
        connect().execute("ALTER TABLE tenants ADD COLUMN tier TEXT "
                          "NOT NULL DEFAULT 'internal'")
    except Exception:
        pass                                     # column already there
    # `packs` on tenants (2026-09-03): message units bought on top of the
    # month's cap; credited by the control plane's webhook, never expiring.
    try:
        connect().execute("ALTER TABLE tenants ADD COLUMN packs INTEGER "
                          "NOT NULL DEFAULT 0")
    except Exception:
        pass
    # `graph` on tenants (2026-09-08): the graph add-on, flipped by the
    # control plane with the plan; the relay refuses graph work without it.
    try:
        connect().execute("ALTER TABLE tenants ADD COLUMN graph INTEGER "
                          "NOT NULL DEFAULT 0")
    except Exception:
        pass
    # THE REGISTRAR'S INDEX IS DEAD (founder 2026-09-08): names and kinds
    # are judged on the box by the graph worker, against the real graph.
    # A shadow index with no aliases, counts or links made most of the
    # twins it was meant to prevent. Dropped on every boot, a no-op once
    # gone.
    for t in ("entity_name", "entity_type"):
        try:
            connect().execute(f"DROP TABLE IF EXISTS {t}")
        except Exception:
            pass
    # `line` on the verdict cache (2026-09-02): the instruction the curator
    # reads in place of the rule, written by the filter in the same call. A
    # bearing verdict from before this column has line='' and is re-asked
    # once, on its next render.
    try:
        connect().execute("ALTER TABLE rule_verdict ADD COLUMN line "
                          "TEXT NOT NULL DEFAULT ''")
    except Exception:
        pass
    # `vec` on rules (2026-09-03): the rule's embedding, sent by the box that
    # saved it (every box has the embedder; the hub has none). The prompt hook
    # sends the prompt's vector with its question and the hub cosines here —
    # the calibrated semantic match, and still nothing stored on a box.
    try:
        connect().execute("ALTER TABLE rule ADD COLUMN vec BLOB")
    except Exception:
        pass
    # THE BRIEF TABLE IS DEAD (2026-09-02): the brief is composed from rules
    # and verdicts at use time, and nothing reads this table. Dropped on
    # every boot (a no-op once gone) — a table nobody reads is a pile
    # (founder law), and the teacher that filled it no longer exists.
    try:
        connect().execute("DROP TABLE IF EXISTS brief")
    except Exception:
        pass


async def fetchone(sql: str, args: tuple = ()) -> tuple | None:
    return connect().execute(sql, args).fetchone()


async def fetchall(sql: str, args: tuple = ()) -> list:
    return connect().execute(sql, args).fetchall()


async def execute(sql: str, args: tuple = ()) -> int:
    """One write. Serialised — see the module docstring."""
    async with _write:
        cur = connect().execute(sql, args)
        return cur.rowcount


async def execute_returning(sql: str, args: tuple = ()) -> tuple | None:
    async with _write:
        return connect().execute(sql, args).fetchone()


async def executemany(sql: str, rows: list) -> None:
    if not rows:
        return
    async with _write:
        connect().executemany(sql, rows)
