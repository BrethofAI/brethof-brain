"""A burst of hooks on one session never deadlocks the pending table
(2026-10-05, 04:21 UTC: after a brain-api restart the client replayed ~30 stop
hooks in one second; every hook ran the table's DDL ahead of its insert, 39
deadlocked, and their pieces never reached the curator). Real Postgres."""
import os
import threading

import pytest

from tests.test_backfill import conn  # noqa: F401  (the scratch database fixture)

DSN = os.environ.get("BRAIN_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="BRAIN_TEST_DSN not set")


def test_thirty_hooks_at_once_on_a_fresh_process_all_land(conn, monkeypatch):
    import psycopg
    from core import hubwindow, store
    monkeypatch.setattr(store, "current_tenant", lambda: "t_burst")
    db = conn.info.dbname
    hubwindow.ensure(conn.cursor())                    # the table exists, as on a box that ran before
    conn.commit()
    store._ddl_ensured.discard(("t_burst", "hub_pending"))   # ... and the process is new (a restart)
    errors, go = [], threading.Barrier(30)

    def hook(i):
        c = psycopg.connect(DSN.replace("dbname=postgres", f"dbname={db}"))
        try:
            go.wait()
            hubwindow.add(c.cursor(), "p", "s1", [(f"exchange {i}", 2 * i, 2 * i + 1)])
            c.commit()
        except Exception as e:                         # noqa: BLE001
            errors.append(f"{type(e).__name__}: {e}")
        finally:
            c.close()
    for _round in range(3):
        store._ddl_ensured.discard(("t_burst", "hub_pending"))
        ts = [threading.Thread(target=hook, args=(i,)) for i in range(30)]
        [t.start() for t in ts]
        [t.join() for t in ts]
    assert errors == [], errors[:3]
    assert conn.execute("SELECT count(*) FROM hub_pending;").fetchone()[0] == 90


def test_a_rolled_back_first_call_does_not_leave_a_fresh_box_without_its_table(conn, monkeypatch):
    """2026-11-10, found on the Aurora test box: on a FRESH database the first
    prompt hook's _push() ran ensure() inside a transaction it never
    committed (nothing to send yet). The DDL rolled back, but the process had
    already cached the table as made, so every exchange after it failed with
    'relation "hub_pending" does not exist' and nothing ever reached the
    curator."""
    from core import hubwindow, store
    monkeypatch.setattr(store, "current_tenant", lambda: "t_fresh")
    store._ddl_ensured.discard(("t_fresh", "hub_pending"))
    conn.execute("DROP TABLE IF EXISTS hub_pending;")
    conn.commit()
    hubwindow.ensure(conn.cursor())            # the fresh box's first call ...
    conn.rollback()                            # ... whose caller never commits
    n = hubwindow.add(conn.cursor(), "p", "s1", [("the first exchange", 0, 1)])
    conn.commit()
    assert n == 1
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM hub_pending;")
    assert cur.fetchone()[0] == 1


def test_two_windows_built_at_once_do_not_deadlock_on_the_decision_table(conn, monkeypatch):
    """2026-11-10, the Aurora test box: build() ran the decision DDL bare on
    every window, and its ALTER TABLE ... ADD COLUMN IF NOT EXISTS takes
    ACCESS EXCLUSIVE even when the columns exist; two windows at once
    deadlocked and the loser's exchanges were never curated."""
    import psycopg
    from core import decisionline, store
    monkeypatch.setattr(store, "current_tenant", lambda: "t_dec")
    db = conn.info.dbname
    store._ddl_ensured.discard(("t_dec", "decision"))
    errors, go = [], threading.Barrier(12)

    def window(i):
        c = psycopg.connect(DSN.replace("dbname=postgres", f"dbname={db}"))
        try:
            go.wait()
            cur = c.cursor()
            store.ensure_ddl(cur, "decision", decisionline.DDL)
            cur.execute("SELECT count(*) FROM decision;")   # the window's read
            import time; time.sleep(0.2)                     # held open, as build() is
            c.commit()
        except Exception as e:                               # noqa: BLE001
            errors.append(f"{type(e).__name__}: {e}")
        finally:
            c.close()
    for _round in range(2):
        ts = [threading.Thread(target=window, args=(i,)) for i in range(12)]
        [t.start() for t in ts]
        [t.join() for t in ts]
    assert errors == [], errors[:3]
