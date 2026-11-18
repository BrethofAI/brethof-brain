"""Tenant-bound Postgres access. The database IS the isolation boundary.

Mirrors v1's discipline deliberately: the connection comes from a contextvar
that the auth dependency sets per request, and a code path that forgets to bind
a tenant RAISES rather than falling back to something ambient. v1's comment on
this was earned — there is no root connection here to leak into.

The step up from v1 is that isolation is now physical. v1 kept every tenant in
one SurrealDB keyspace and enforced separation in the application layer; here
each tenant is its own PostgreSQL database with its own files, which is also
what makes `pg_database_size()` an exact per-customer number.
"""
from __future__ import annotations

import contextlib
import contextvars
import os
import re
import threading
import traceback

from psycopg_pool import ConnectionPool

_TENANT_RE = re.compile(r"^[a-z0-9_]{3,40}$")

PG_HOST = os.environ.get("BRAIN_PG_HOST", "brain-pg")
PG_PORT = os.environ.get("BRAIN_PG_PORT", "5432")
PG_USER = os.environ.get("BRAIN_PG_USER", "mind")
PG_PASS = os.environ.get("BRAIN_PG_PASS", "")
# pg_tde live on this cluster (Percona PG18 migration, 2026-08-22). When set,
# every provision/restore calls brain_tde_init() — the SECURITY DEFINER door
# in template1 that gives the database its own principal key — and a failure
# there is a HARD error: with default_table_access_method=tde_heap an unkeyed
# database fails loudly anyway, and a tenant silently landing unencrypted is
# the exact failure this flag refuses to allow. Unset on PG17 dev clusters
# (localpg) where pg_tde does not exist.
TDE = bool(os.environ.get("BRAIN_TDE", ""))


def tde_init(db: str) -> None:
    """Give `db` its own pg_tde principal key (no-op when BRAIN_TDE unset).

    Idempotent — an already-keyed database keeps its key. Runs on its own
    autocommit connection: pg_tde key operations are not transactional and
    must be committed before the first CREATE TABLE under tde_heap."""
    if not TDE:
        return
    import psycopg
    with psycopg.connect(
            f"host={PG_HOST} port={PG_PORT} dbname={db} "
            f"user={PG_USER} password={PG_PASS}", autocommit=True) as conn:
        conn.execute("SELECT brain_tde_init();")

_pools: dict[str, ConnectionPool] = {}
# Guards pool CREATION only (2026-08-09, GLM engineering audit). Without it,
# two concurrent first-hits on the same new tenant both saw _pools.get()==None,
# both built a ConnectionPool, and one overwrote the other in the dict — the
# orphan never closed and leaked up to 4 connections for the process lifetime.
# That is the 96/100 connection-saturation class the wake redesign fought.
_pools_lock = threading.Lock()
# Databases dropped by deprovision_tenant in this process — pool_for refuses
# to resurrect them. Cleared on re-provision; bounded by the number of
# deletions per process lifetime, so it never needs eviction.
_dead_dbs: set[str] = set()
_current: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mind_tenant", default=None)


class NoTenantBound(RuntimeError):
    """Raised instead of guessing. See the module docstring."""


class TenantGone(RuntimeError):
    """The tenant's database was deprovisioned in this process's lifetime.

    Raised by pool_for() instead of building a pool for a dropped database.
    Without this, a worker wake already queued at DROP time re-created the
    pool two seconds after deprovision and psycopg_pool retried the dead DB
    for its full 300s reconnect window, PoolTimeout-ing every job on that
    tenant along the way (observed live 2026-08-09, gate GDPR drill)."""


def tenant_db(tenant_id: str) -> str:
    if not _TENANT_RE.match(tenant_id):
        raise ValueError(f"bad tenant id: {tenant_id!r}")
    return f"t_{tenant_id}"


POOL_MAX = int(os.environ.get("BRAIN_PG_POOL_MAX", "32") or 32)
WAIT_LOG_S = 1.0     # a request that waited this long for a connection says so


def pool_for(tenant_id: str) -> ConnectionPool:
    """One pool per tenant database, created lazily and kept.

    min_size=0 matters: a tenant that never sends traffic must not hold an idle
    connection open. With hundreds of tenants on one box, eager pools would
    exhaust max_connections long before load did.
    """
    db = tenant_db(tenant_id)
    if db in _dead_dbs:
        raise TenantGone(f"tenant {tenant_id} was deprovisioned — "
                         "refusing to rebuild its pool")
    p = _pools.get(db)
    if p is not None:
        return p
    # Double-checked locking: the fast path above is lock-free (the pool
    # almost always already exists); only the rare create path serializes.
    with _pools_lock:
        if db in _dead_dbs:
            raise TenantGone(f"tenant {tenant_id} was deprovisioned — "
                             "refusing to rebuild its pool")
        p = _pools.get(db)
        if p is not None:
            return p
        p = ConnectionPool(
            f"host={PG_HOST} port={PG_PORT} dbname={db} "
            f"user={PG_USER} password={PG_PASS}",
            # max_idle=60: warmth exists ONLY for the burst around a single
            # exchange (archive → curator pickup → writes → embed, ~a
            # minute); human think-time between messages is always longer,
            # so an idle tenant's database holds no sessions between turns
            # (founder 2026-08-09). Shorter would reconnect mid-burst for
            # nothing. This only became true when the workers went
            # wake-driven — the 5-30s all-tenant sweeps used to reset the
            # idle clock forever, which is how 38 test tenants ate 96 of
            # 100 connection slots.
            # max_size 4 -> 8 (2026-09-17): four was exhausted by one heal
            # batch plus the hub's eight brief reads, and the pipe's saves
            # timed out; idle pools still shrink to nothing (max_idle).
            # max_size 8 -> 32 (founder 2026-09-22: "we have the enterprise
            # plans, and many agents connecting to the same database"): one
            # pool per memory serves every agent's hooks, searches and saves
            # AND the box's workers; a bench drain of 8 projects x 3 workers
            # ran it dry (PoolTimeout, a claimed batch orphaned). Idle pools
            # still shrink to nothing, so 32 costs nothing when quiet.
            min_size=0, max_size=POOL_MAX,
            timeout=10, max_idle=60.0, open=True)
        _pools[db] = p
    return p


def bind(tenant_id: str) -> contextvars.Token:
    return _current.set(tenant_id)


def unbind(token: contextvars.Token) -> None:
    _current.reset(token)


def current_tenant() -> str:
    t = _current.get()
    if not t:
        raise NoTenantBound("no tenant bound — refusing to run a query")
    return t


def mark_dead(tenant_id: str) -> None:
    """Tombstone a tenant whose database is gone, from anywhere.

    deprovision_tenant() tombstones the tenants IT drops. This is the other
    half: a database dropped by ANOTHER process (the release gate's own
    control-plane call, a second API replica, the panel) is invisible here,
    so pool_for() happily builds a pool and psycopg retries a database that
    will never come back. Measured 2026-08-12: t_rg86565954p produced 18
    tracebacks in one minute and kept firing in bursts for six, long after
    the 60 s provisioned-tenants cache should have dropped it."""
    global _prov_cache
    db = tenant_db(tenant_id)
    with _pools_lock:
        _dead_dbs.add(db)
        p = _pools.pop(db, None)
    if p is not None:
        try:
            p.close()
        except Exception:                                 # noqa: BLE001
            pass
    _prov_cache = (0.0, [])
    print(f"[store] tenant {tenant_id} tombstoned — its database is gone",
          flush=True)


def is_dead(tenant_id: str) -> bool:
    """Cheap predicate for the wake board — never opens a connection.

    Answers ONLY "is this tombstoned". A malformed id is not dead, it is
    wrong, and it must stay visible: the first version returned True for it
    and the board silently dropped it, which is the swallow-a-fault shape
    this whole change exists to remove. It fails loudly at pool_for instead.
    """
    try:
        return tenant_db(tenant_id) in _dead_dbs
    except ValueError:
        return False


# Postgres says this exactly once per vanished database; matching the text is
# the only way to tell "database dropped" from "server is down", and treating
# the second as the first would tombstone a live tenant during an outage.
_GONE = "does not exist"


@contextlib.contextmanager
def _guarded(tenant: str, pool: ConnectionPool):
    """Translate 'that database is gone' into a tombstone + TenantGone.

    The failure surfaces on ENTRY, not when connection() is called — psycopg
    hands back a context manager and only reaches the server inside it — so
    the translation has to live here rather than around pool_for()."""
    import time as _t
    t0 = _t.monotonic()
    try:
        with pool.connection() as conn:
            waited = _t.monotonic() - t0
            if waited >= WAIT_LOG_S:
                # NEVER SILENT (2026-09-22): a pool running dry shows itself
                # here long before it times out
                print(f"[store] {tenant}: waited {waited:.1f}s for a database connection "
                      f"(pool max {POOL_MAX})", flush=True)
            yield conn
    except TenantGone:
        raise
    except Exception as e:                                # noqa: BLE001
        if f'database "{tenant_db(tenant)}" {_GONE}' in str(e):
            mark_dead(tenant)
            raise TenantGone(f"tenant {tenant} was deprovisioned — "
                             "refusing to rebuild its pool") from e
        raise


def connection():
    """`with store.connection() as conn:` — the only way to reach the data.

    A first connection to a dropped database TOMBSTONES it rather than
    letting psycopg_pool retry for its full reconnect window: one caller
    pays a failure, every later caller gets TenantGone immediately."""
    tenant = current_tenant()
    # Eager, so an ALREADY-tombstoned tenant raises at the call itself —
    # unchanged from before this guard existed.
    pool = pool_for(tenant)
    return _guarded(tenant, pool)


# (tenant, ddl_key) pairs this process has already ensured — see ensure_ddl.
_ddl_ensured: set = set()


def ensure_ddl(cur, key: str, ddl: str) -> None:
    """Run idempotent DDL at most once per process per tenant — NEVER on a
    hot path unconditionally.

    Why this exists (incident 2026-08-11, turn_job): CREATE INDEX IF NOT
    EXISTS takes ShareLock on the table, held to commit, which conflicts with
    concurrent writers' RowExclusiveLock. Any worker that re-ran its _DDL on
    every call deadlocked the moment two writers overlapped — and the losing
    transaction's work was silently dropped by fail-open callers. First run
    per (tenant, key) is serialized on an advisory lock so concurrent
    first-callers can't interleave either; after that the memo makes the
    call lock-free. The advisory lock is xact-scoped: callers commit soon
    after. Lock ordering stays acyclic as long as callers take ensure_ddl
    BEFORE any row locks, which 'first statement in the transaction' gives
    for free."""
    tenant = current_tenant()
    if (tenant, key) in _ddl_ensured:
        return
    if ddl_applied(cur, ddl):
        # THE PROBE (2026-09-09): a fresh process (a drain, a migrate, a CLI)
        # has an empty memo, and `ALTER TABLE … ADD COLUMN IF NOT EXISTS`
        # takes ACCESS EXCLUSIVE on the table before it looks — so every
        # process start queued an exclusive lock on `entity` behind whatever
        # transaction was open, and while it queued, every reader of the
        # table (the brief, recall) queued behind IT. Measured: a killed
        # drain's connection sat idle in transaction for three minutes, the
        # linker's ALTER waited on it, and the box's graph readers waited on
        # the ALTER. So the catalog is read first, with no lock: when every
        # table, column and index the DDL names already exists, nothing runs.
        _ddl_ensured.add((tenant, key))
        return
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s));", ["ddl:" + key])
    cur.execute(ddl)
    # NOT MEMOISED HERE (2026-09-10). The DDL ran inside the caller's
    # transaction; if that transaction is rolled back later (a lane that
    # swallows an error with conn.rollback(), as recall's chunk merge does),
    # the table is gone and a memo written now would lie for the life of
    # the process — every later read of it raises, the caller swallows,
    # the transaction is left aborted, and the next statement dies with
    # InFailedSqlTransaction. Measured on the VM rig's fresh tenant
    # 2026-09-10 01:38Z: 54 aborted prompt-submits, an empty block every
    # probe, the seed judged red. The memo is written only by the probe
    # above, once the structure is seen durable at the start of a call.


_DDL_COMMENT = re.compile(r"--[^\n]*")
_DDL_TABLE = re.compile(r"^CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([\w.]+)", re.I)
_DDL_INDEX = re.compile(r"^CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+([\w.]+)", re.I)
_DDL_COLUMN = re.compile(r"^ALTER\s+TABLE\s+([\w.]+)\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+([\w.]+)", re.I)
_DDL_SIDE = re.compile(r"^(UPDATE|INSERT|DELETE)\b", re.I)


@contextlib.contextmanager
def savepoint(cur, name: str = "sp"):
    """A swallowed error must not abort the transaction (2026-09-10): a lane
    that catches an exception and carries on leaves Postgres in
    'current transaction is aborted' and every later statement fails. Wrap
    the risky statements: on error the savepoint is rolled back and the
    transaction continues; the caller still decides what to do."""
    cur.execute(f"SAVEPOINT {name};")
    try:
        yield
    except Exception:
        try:
            cur.execute(f"ROLLBACK TO SAVEPOINT {name};")
        except Exception:                                    # noqa: BLE001
            pass
        raise
    else:
        try:
            cur.execute(f"RELEASE SAVEPOINT {name};")
        except Exception:                                    # noqa: BLE001
            pass


def ddl_statements(ddl: str) -> list[str]:
    """The statements of a DDL string, comments stripped, blanks dropped."""
    text = _DDL_COMMENT.sub("", ddl)
    return [" ".join(st.split()) for st in text.split(";") if st.strip()]


def ddl_applied(cur, ddl: str) -> bool:
    """True when every table, column and index the DDL names already exists
    — read from the catalog, no lock taken. A statement of any other shape
    (a function, a type, a plain CREATE) makes the answer False, so the DDL
    runs as before; an UPDATE/INSERT/DELETE inside a DDL is a one-time
    side effect of the structure and is skipped once the structure stands."""
    for st in ddl_statements(ddl):
        m = _DDL_TABLE.match(st)
        if m:
            cur.execute("SELECT to_regclass(%s) IS NOT NULL;", [m.group(1)])
            if not cur.fetchone()[0]:
                return False
            continue
        m = _DDL_INDEX.match(st)
        if m:
            cur.execute("SELECT to_regclass(%s) IS NOT NULL;", [m.group(1)])
            if not cur.fetchone()[0]:
                return False
            continue
        m = _DDL_COLUMN.match(st)
        if m:
            table, col = m.group(1).split(".")[-1], m.group(2)
            cur.execute("SELECT 1 FROM information_schema.columns WHERE table_name = %s "
                        "AND column_name = %s AND table_schema = current_schema();", [table, col])
            if not cur.fetchone():
                return False
            continue
        if _DDL_SIDE.match(st):
            continue
        return False
    return True


def close_all() -> None:
    for p in _pools.values():
        p.close()
    _pools.clear()


def mark_alive(tenant_id: str) -> None:
    """Clear the deprovision tombstone — EVERY path that (re)creates a tenant
    database must call this or pool_for keeps refusing the tenant. Callers:
    provision_tenant (new/re-used ids) and lifecycle.restore_tenant (whose
    own post-restore verification goes through pool_for — without this the
    restore drill kills itself, found by the gate 2026-08-09)."""
    with _pools_lock:
        _dead_dbs.discard(tenant_db(tenant_id))


def deprovision_tenant(tenant_id: str) -> bool:
    """DROP the tenant's database — the data-plane half of account deletion.

    Order matters: close OUR pool first (an open pool holds connections that
    make DROP DATABASE fail), then terminate any strays server-side, then
    drop. Returns False when the database was already gone — idempotent for
    control-plane retries. The provisioned-tenants cache is invalidated so
    background workers stop iterating the tenant on their next poll."""
    global _prov_cache
    # ERASURE REACHES THE BACKUPS TOO (founder 2026-08-24: "we can't say
    # deleted and keep copy in borg"). Queued BEFORE the drop, so a crash
    # mid-deletion still leaves the intent for the drain to finish.
    #
    # THIS BELONGS HERE AND NOWHERE NEARBY: it was first written into
    # mark_dead(), which fires whenever a database is found already gone —
    # including after a lifecycle ARCHIVE. That would have deleted the
    # backups of a merely-archived customer, i.e. the only copy of the
    # account they were about to restore. Deprovision is deletion;
    # mark_dead is a tombstone. Only deletion erases backups.
    try:
        from . import backup_purge
        backup_purge.queue(tenant_id)
    except Exception:                                     # noqa: BLE001
        traceback.print_exc()
    db = tenant_db(tenant_id)
    # Tombstone BEFORE closing, under the creation lock: a worker wake racing
    # us either sees the tombstone (raises TenantGone) or finishes creating a
    # pool that we then pop and close here. Either way no pool survives.
    with _pools_lock:
        _dead_dbs.add(db)
        p = _pools.pop(db, None)
    if p is not None:
        p.close()
    import psycopg
    with psycopg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER,
                         password=PG_PASS, dbname="postgres",
                         autocommit=True, connect_timeout=10) as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s;", [db])
        if not cur.fetchone():
            return False
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid();", [db])
        # Identifier, not a parameter — db came through tenant_db()'s
        # validation, so it is [a-z0-9_] by construction.
        cur.execute(f'DROP DATABASE "{db}";')
    _prov_cache = (0.0, [])
    print(f"[store] DEPROVISIONED tenant {tenant_id} — database {db} dropped",
          flush=True)
    return True


# Scratch databases that look like tenants but are not — never iterate them.
_NOT_TENANTS = {"spike"}
_prov_cache: tuple[float, list[str]] = (0.0, [])


def provisioned_tenants() -> list[str]:
    """Every tenant DATABASE on the box — what background workers iterate.

    THE GAP THIS CLOSES (found 2026-07-29): the workers used to iterate
    auth.tenants(), which is the STATIC key file only. A panel-minted
    customer's key resolves via the control plane and never appears there —
    so their turn/curate/gate jobs enqueued into a database no worker ever
    polled. The databases themselves are the truth about who exists.

    60s cache: workers poll every few seconds and pg_database changes only
    at provision time.
    """
    global _prov_cache
    import time
    ts, cached = _prov_cache
    if time.monotonic() - ts < 60.0 and cached:
        return cached
    import psycopg
    try:
        with psycopg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER,
                             password=PG_PASS, dbname="postgres",
                             connect_timeout=5) as conn:
            cur = conn.cursor()
            cur.execute("SELECT datname FROM pg_database "
                        "WHERE datname LIKE 't\\_%' ORDER BY datname;")
            names = [r[0][2:] for r in cur.fetchall()]
    except Exception:
        return cached           # degraded: last known list beats an empty one
    # bench_* are the graph bench's scratch tenants (core/bench): real
    # databases the doors drive by name; no worker iterates them.
    out = [n for n in names if n not in _NOT_TENANTS and _TENANT_RE.match(n)
           and not n.startswith("bench_")]
    _prov_cache = (time.monotonic(), out)
    return out


def provision_tenant(tenant_id: str) -> bool:
    """Create the tenant's database + schema if absent. Returns True when the
    database was actually created. Idempotent by construction: CREATE DATABASE
    is guarded by a catalog check and the schema is all IF NOT EXISTS.

    This is the v2 provisioning path the control plane calls when a customer
    subscribes — the brain_app role has CREATEDB, no superuser involved
    (true since the 2026-08-16 role split; before that the "no superuser"
    claim here was aspirational — the app ran as the bootstrap superuser).
    CREATE EXTENSION in schema.sql is a no-op: vector+pg_trgm live in
    template1, because vector is not a trusted extension."""
    import psycopg

    db = tenant_db(tenant_id)
    mark_alive(tenant_id)       # a re-provisioned id is alive again
    admin_dsn = (f"host={PG_HOST} port={PG_PORT} dbname=postgres "
                 f"user={PG_USER} password={PG_PASS}")
    created = False
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s;", [db])
        if not cur.fetchone():
            # Identifier, not a parameter — guarded by tenant_db()'s regex.
            cur.execute(f'CREATE DATABASE "{db}" OWNER {PG_USER};')
            created = True
    tde_init(db)                # before any CREATE TABLE lands in the db
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    with open(schema_path, encoding="utf-8") as fh:
        # One knob for the whole stack: compose sets it for the sidecar and
        # the API alike (768 = EmbeddingGemma; was hardcoded 384 for MiniLM).
        ddl = fh.read().replace(":dim", os.environ.get("BRAIN_EMBED_DIM", "768"))
    with psycopg.connect(
            f"host={PG_HOST} port={PG_PORT} dbname={db} "
            f"user={PG_USER} password={PG_PASS}") as conn:
        conn.execute(ddl)
        # ★SEED rules — the fleet-managed teaching every fresh tenant starts
        conn.commit()
    return created
