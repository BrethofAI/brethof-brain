"""The VM rig's red seed, 2026-09-10: a DDL memoised after running inside a
transaction that was later rolled back lied for the life of the process,
and a swallowed error left the transaction aborted. The memo now means
'seen durable'; a swallowed read sits in a savepoint."""
import os
import uuid

import pytest

from core import store


class _Cur:
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


def test_the_memo_is_written_only_when_the_structure_is_seen_durable(monkeypatch):
    monkeypatch.setattr(store, "current_tenant", lambda: "t_memo")
    store._ddl_ensured.discard(("t_memo", "k"))
    ddl = "CREATE TABLE IF NOT EXISTS session_owner (id int);"
    cur = _Cur()                                    # a fresh tenant: nothing exists
    store.ensure_ddl(cur, "k", ddl)
    assert any("CREATE TABLE" in q for q in cur.executed)
    assert ("t_memo", "k") not in store._ddl_ensured, "ran is not durable"
    # the transaction was rolled back: the table is still absent — the DDL runs again
    cur = _Cur()
    store.ensure_ddl(cur, "k", ddl)
    assert any("CREATE TABLE" in q for q in cur.executed)
    # once the catalog shows it, the memo is written and the DDL never runs again
    cur = _Cur({"session_owner"})
    store.ensure_ddl(cur, "k", ddl)
    assert not any("CREATE TABLE" in q for q in cur.executed)
    assert ("t_memo", "k") in store._ddl_ensured
    store._ddl_ensured.discard(("t_memo", "k"))


psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("BRAIN_TEST_DSN", "")


@pytest.mark.skipif(not DSN, reason="BRAIN_TEST_DSN not set")
def test_a_swallowed_read_inside_a_savepoint_leaves_the_transaction_alive():
    from core import ownership
    name = "t_svp_" + uuid.uuid4().hex[:10]
    admin = psycopg.connect(DSN, autocommit=True)
    admin.execute(f"CREATE DATABASE {name}")
    try:
        c = psycopg.connect(DSN.replace("dbname=postgres", f"dbname={name}"))
        cur = c.cursor()
        # the savepoint helper itself
        with pytest.raises(Exception):
            with store.savepoint(cur, "x"):
                cur.execute("SELECT * FROM no_such_table")
        cur.execute("SELECT 1")                       # alive
        assert cur.fetchone() == (1,)
        # the door's read with its table absent and a lying memo: swallowed, transaction alive
        store._ddl_ensured.add(("t_svp", "session_owner"))
        orig = store.current_tenant
        store.current_tenant = lambda: "t_svp"
        try:
            w, a = ownership.chat_where(cur, ["p"])
            assert (w, a) == ("project = %s", ["p"])
            cur.execute("SELECT 1")
            assert cur.fetchone() == (1,)
        finally:
            store.current_tenant = orig
            store._ddl_ensured.discard(("t_svp", "session_owner"))
        c.close()
    finally:
        admin.execute(f"DROP DATABASE {name}")
        admin.close()
