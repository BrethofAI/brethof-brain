"""The decision tables go through the one DDL door (2026-11-10). The DDL's
ALTER TABLE ... ADD COLUMN IF NOT EXISTS takes ACCESS EXCLUSIVE even when the
column is there, so the per-prompt history call and every window's save,
which ran it bare, queued behind any open reader of `decision` and every
reader behind them. Real Postgres."""
import os
import pathlib

import psycopg
import pytest

from tests.test_backfill import conn  # noqa: F401  (the scratch database fixture)

DSN = os.environ.get("BRAIN_TEST_DSN", "")


@pytest.mark.skipif(not DSN, reason="BRAIN_TEST_DSN not set")
def test_a_new_process_does_not_lock_out_a_reader_of_the_decision_table(conn, monkeypatch):
    from core import decisionline, store
    monkeypatch.setattr(store, "current_tenant", lambda: "t_decddl")
    conn.execute(decisionline.DDL)
    conn.commit()
    store._ddl_ensured.discard(("t_decddl", "decision"))    # a fresh process
    db = conn.info.dbname
    reader = psycopg.connect(DSN.replace("dbname=postgres", f"dbname={db}"))
    other = psycopg.connect(DSN.replace("dbname=postgres", f"dbname={db}"))
    try:
        reader.execute("SELECT count(*) FROM decision;")    # an open transaction reading the table
        other.execute("SET lock_timeout = '2s';")
        decisionline.ensure(other.cursor())                 # bare DDL here waited on the reader
        other.commit()
    finally:
        reader.close()
        other.close()


def test_no_pipe_command_runs_the_decision_ddl_bare():
    src = (pathlib.Path(__file__).resolve().parents[1] / "core" / "pipecommands.py").read_text()
    assert "cur.execute(decisionline.DDL)" not in src
    assert "cur.execute(HISTORY_LOG_DDL)" not in src
