"""The DDL probe (2026-09-09): a fresh process must not take an exclusive
lock to learn that a column already exists. ensure_ddl reads the catalog
first and runs the DDL only when something it names is missing."""
import os
import uuid

import pytest

from core import store

DSN = os.environ.get("BRAIN_TEST_DSN", "")


def test_statements_are_split_with_comments_stripped():
    ddl = """
    -- a comment; with a semicolon
    CREATE TABLE IF NOT EXISTS t (id int);   -- trailing
    ALTER TABLE t ADD COLUMN IF NOT EXISTS c text;
    UPDATE t SET c = '' WHERE c IS NULL;
    CREATE INDEX IF NOT EXISTS t_c_idx ON t (c);
    """
    sts = store.ddl_statements(ddl)
    assert len(sts) == 4 and sts[0].startswith("CREATE TABLE IF NOT EXISTS t")


class _Cur:
    """A cursor that answers the catalog reads from a fixed picture."""
    def __init__(self, tables=(), columns=(), indexes=()):
        self.tables, self.columns, self.indexes = set(tables), set(columns), set(indexes)
        self.executed = []
        self._row = None

    def execute(self, sql, args=None):
        self.executed.append(sql)
        args = args or []
        if "to_regclass" in sql:
            self._row = (args[0] in self.tables or args[0] in self.indexes,)
        elif "information_schema.columns" in sql:
            self._row = (1,) if (args[0], args[1]) in self.columns else None
        else:
            self._row = None

    def fetchone(self):
        return self._row


def test_the_probe_says_applied_only_when_everything_named_exists():
    ddl = ("CREATE TABLE IF NOT EXISTS t (id int); ALTER TABLE t ADD COLUMN IF NOT EXISTS c text; "
           "UPDATE t SET c = ''; CREATE UNIQUE INDEX IF NOT EXISTS t_c_idx ON t (c);")
    assert store.ddl_applied(_Cur({"t"}, {("t", "c")}, {"t_c_idx"}), ddl)
    assert not store.ddl_applied(_Cur({"t"}, set(), {"t_c_idx"}), ddl)          # column missing
    assert not store.ddl_applied(_Cur({"t"}, {("t", "c")}, set()), ddl)         # index missing
    assert not store.ddl_applied(_Cur(set(), set(), set()), ddl)                # table missing
    # an unknown shape is never claimed applied — the DDL runs as before
    assert not store.ddl_applied(_Cur({"t"}), "CREATE OR REPLACE FUNCTION f() RETURNS int AS $$ SELECT 1 $$ LANGUAGE sql")


def test_a_fresh_process_runs_nothing_when_the_structure_stands(monkeypatch):
    monkeypatch.setattr(store, "current_tenant", lambda: "t_probe")
    store._ddl_ensured.discard(("t_probe", "k"))
    ddl = "ALTER TABLE entity ADD COLUMN IF NOT EXISTS gloss text NOT NULL DEFAULT '';"
    cur = _Cur({"entity"}, {("entity", "gloss")})
    store.ensure_ddl(cur, "k", ddl)
    assert not any("ALTER TABLE" in q or "advisory" in q for q in cur.executed)
    assert ("t_probe", "k") in store._ddl_ensured
    # missing column: the DDL runs under the advisory lock, as before
    store._ddl_ensured.discard(("t_probe", "k2"))
    cur = _Cur({"entity"}, set())
    store.ensure_ddl(cur, "k2", ddl)
    assert any("advisory" in q for q in cur.executed) and any("ALTER TABLE" in q for q in cur.executed)
    store._ddl_ensured.discard(("t_probe", "k")); store._ddl_ensured.discard(("t_probe", "k2"))


psycopg = pytest.importorskip("psycopg")


@pytest.mark.skipif(not DSN, reason="BRAIN_TEST_DSN not set")
def test_on_a_real_database_the_second_process_takes_no_table_lock():
    name = "t_ddlprobe_" + uuid.uuid4().hex[:10]
    admin = psycopg.connect(DSN, autocommit=True)
    admin.execute(f"CREATE DATABASE {name}")
    try:
        c = psycopg.connect(DSN.replace("dbname=postgres", f"dbname={name}"))
        cur = c.cursor()
        ddl = ("CREATE TABLE IF NOT EXISTS entity (project text, name text); "
               "ALTER TABLE entity ADD COLUMN IF NOT EXISTS gloss text NOT NULL DEFAULT ''; "
               "CREATE INDEX IF NOT EXISTS entity_name_idx ON entity (name);")
        assert not store.ddl_applied(cur, ddl)
        cur.execute(ddl)
        c.commit()
        assert store.ddl_applied(cur, ddl)
        # the probe took no lock on the table
        cur.execute("SELECT count(*) FROM pg_locks l JOIN pg_class r ON r.oid = l.relation "
                    "WHERE r.relname = 'entity' AND l.pid = pg_backend_pid() AND l.mode = 'AccessExclusiveLock'")
        assert cur.fetchone()[0] == 0
        c.close()
    finally:
        admin.execute(f"DROP DATABASE {name}")
        admin.close()
