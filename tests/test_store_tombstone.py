"""Deprovisioned-tenant tombstone: pool_for must never resurrect a dropped DB.

Live incident 2026-08-09 (gate GDPR drill): deprovision closed the tenant's
pool, but a worker wake queued before the DROP called pool_for() two seconds
later, built a NEW pool for the dead database, and psycopg_pool retried it for
its full 300s reconnect window — PoolTimeout-ing every job routed at that
tenant meanwhile. The tombstone makes the second pool_for raise instantly.

Pure in-process tests — no Postgres needed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import store  # noqa: E402

T = "tombstonetest1"


def _cleanup():
    store._dead_dbs.discard(store.tenant_db(T))
    store._pools.pop(store.tenant_db(T), None)


def test_tombstoned_tenant_refused():
    _cleanup()
    try:
        with store._pools_lock:
            store._dead_dbs.add(store.tenant_db(T))
        with pytest.raises(store.TenantGone):
            store.pool_for(T)
        # And again — refusal is stable, not one-shot.
        with pytest.raises(store.TenantGone):
            store.pool_for(T)
        assert store.tenant_db(T) not in store._pools, \
            "refusal must not leave a half-built pool behind"
    finally:
        _cleanup()


def test_connection_path_propagates_tombstone():
    """store.connection() under a bound tenant hits the same refusal —
    that is the path every worker job actually takes."""
    _cleanup()
    tok = store.bind(T)
    try:
        with store._pools_lock:
            store._dead_dbs.add(store.tenant_db(T))
        with pytest.raises(store.TenantGone):
            store.connection()
    finally:
        store.unbind(tok)
        _cleanup()


def test_reprovision_clears_tombstone():
    """provision_tenant must discard the tombstone before touching PG, so a
    re-used tenant id comes back alive. We only assert the discard happens
    ahead of the connect: with a guaranteed-unreachable PG host the connect
    raises, yet the tombstone must already be gone."""
    _cleanup()
    try:
        with store._pools_lock:
            store._dead_dbs.add(store.tenant_db(T))
        old_host = store.PG_HOST
        store.PG_HOST = "127.0.0.1"
        old_port = store.PG_PORT
        store.PG_PORT = "1"          # nothing listens on port 1
        try:
            with pytest.raises(Exception):
                store.provision_tenant(T)
        finally:
            store.PG_HOST = old_host
            store.PG_PORT = old_port
        assert store.tenant_db(T) not in store._dead_dbs
    finally:
        _cleanup()
