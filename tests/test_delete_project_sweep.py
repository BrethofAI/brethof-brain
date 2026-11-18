"""A deleted project leaves NOTHING behind — proven on a real database.

Why this file exists, and why it is not another mock test: the mock test
beside it (test_delete_project.py) pins the ORDER of the statements the
delete issues, which is exactly what a hand-written table list gets right
while the list itself goes stale. Twice now it has:

  2026-09-10 morning — deleting the emptied 'linux' project left 119
  entity_link rows, one record-link row (a table since dropped) and 15 done graph batches.
  2026-09-10 night — with those three tables added, an end-to-end test
  deleted a throwaway project and left ledger rows, entity_type,
  graph_heal_state and heal_queue behind; the same sweep over the live box
  found 166 entity_type rows and 30 ledger rows belonging to 'harbor*'
  projects deleted long ago.

So the delete now asks the catalogue which tables carry a `project` column
instead of remembering, and this test asks the same question: it builds a
tenant's real schema, puts a row for a doomed project in every table that
takes one, deletes the project, and fails if ANY row survives. A table
added tomorrow is covered the day it is added — by both.
"""
import os
import uuid

import pytest

DSN = os.environ.get("BRAIN_TEST_DSN", "")
psycopg = pytest.importorskip("psycopg")
pg = pytest.mark.skipif(
    not DSN,
    reason="BRAIN_TEST_DSN not set — a project delete is SQL over the whole "
           "schema and needs a real Postgres "
           "(host=… port=… user=… password=… dbname=postgres)")

DOOMED = "doomedproj"
KEEPER = "keeperproj"


def _schema(conn):
    """The tenant's schema as a fresh box gets it: schema.sql, then every
    module's own idempotent DDL (the tables created on first use)."""
    import importlib
    import pkgutil

    import core
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "core", "schema.sql"), encoding="utf-8") as fh:
        conn.execute(fh.read().replace(":dim", "768"))
    conn.commit()
    for m in pkgutil.iter_modules(core.__path__):
        try:
            mod = importlib.import_module("core." + m.name)
        except Exception:                                      # noqa: BLE001
            continue                       # a module needing runtime env
        for attr in dir(mod):
            if attr != "DDL" and not attr.endswith("_DDL"):
                continue
            ddl = getattr(mod, attr)
            if not isinstance(ddl, str) or "CREATE" not in ddl.upper():
                continue
            try:
                conn.execute(ddl)
                conn.commit()
            except Exception:                                  # noqa: BLE001
                conn.rollback()            # not this box's table; fine


def _project_tables(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT table_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND column_name = 'project' "
        "ORDER BY table_name;")]


_DUMMY = {"text": "x", "character varying": "x", "integer": 1, "bigint": 1,
          "smallint": 1, "boolean": False, "jsonb": "{}", "json": "{}",
          "numeric": 1, "double precision": 1.0, "real": 1.0,
          "timestamp with time zone": "2026-01-01T00:00:00+00:00",
          "timestamp without time zone": "2026-01-01T00:00:00",
          "date": "2026-01-01", "ARRAY": "{}"}


def _seed(conn, project: str) -> list[str]:
    """One row per project-keyed table: `project` plus whatever else the
    table insists on (NOT NULL, no default). A table whose shape this cannot
    satisfy is skipped rather than guessed, and the caller asserts on how
    many were really seeded — the test never claims coverage it lacks."""
    seeded = []
    for t in _project_tables(conn):
        cols, vals = ["project"], [project]
        # text values carry the project name: two seeded projects must not
        # claim the SAME session id, or the ownership rows make one the
        # owner of the other's turns (which is the door working correctly).
        dummy = dict(_DUMMY, **{"text": f"x-{project}",
                                "character varying": f"x-{project}"})
        for name, typ in conn.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=%s "
                "  AND is_nullable='NO' AND column_default IS NULL "
                "  AND is_identity='NO' AND column_name <> 'project';", [t]):
            if typ not in dummy:
                cols = []
                break
            cols.append(name)
            vals.append(dummy[typ])
        if not cols:
            continue
        holes = ", ".join(["%s"] * len(cols))
        try:
            with conn.transaction():
                conn.execute(f"INSERT INTO {t} ({', '.join(cols)}) "
                             f"VALUES ({holes});", vals)
            seeded.append(t)
        except Exception:                                      # noqa: BLE001
            pass
    try:
        with conn.transaction():
            conn.execute(
                "INSERT INTO session_proposal (session_id, filed_project, "
                "proposed_project, confidence, why) VALUES (%s,%s,%s,%s,%s);",
                [f"s-{project}", project, "elsewhere", 0.9, "test"])
        seeded.append("session_proposal")
    except Exception:                                          # noqa: BLE001
        pass
    conn.commit()
    return seeded


@pytest.fixture
def conn():
    name = "t_sweep_" + uuid.uuid4().hex[:10]
    admin = psycopg.connect(DSN, autocommit=True)
    admin.execute(f"CREATE DATABASE {name}")
    c = psycopg.connect(DSN.replace("dbname=postgres", f"dbname={name}"))
    _schema(c)
    c.execute("INSERT INTO project (name) VALUES (%s), (%s);", [DOOMED, KEEPER])
    c.commit()
    try:
        yield c
    finally:
        c.close()
        admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        admin.close()


def _delete(conn, project: str) -> str:
    from unittest import mock

    import api.customer_tools as ct
    from core import ownership
    ownership.invalidate()

    class _Conn:                     # store.connection() commits on exit
        def __enter__(self): return conn
        def __exit__(self, *a):
            conn.commit()
            return False
    with mock.patch.object(ct.store, "connection", _Conn), \
         mock.patch.object(ct, "_writable", return_value=True), \
         mock.patch.object(ct, "_visible", return_value=True), \
         mock.patch("core.hubclient.delete_project", return_value={"gone": {}}):
        out = ct.delete_project(project=project, confirm=project)
    conn.commit()
    ownership.invalidate()
    return out


@pg
def test_not_one_row_of_a_deleted_project_survives(conn):
    seeded = _seed(conn, DOOMED)
    assert len(seeded) >= 10, f"seeded too little to prove anything: {seeded}"
    _delete(conn, DOOMED)

    left = []
    for t in _project_tables(conn):
        n = conn.execute(f"SELECT count(*) FROM {t} WHERE project = %s;",
                         [DOOMED]).fetchone()[0]
        if n:
            left.append(f"{t}={n}")
    n = conn.execute("SELECT count(*) FROM session_proposal "
                     "WHERE filed_project = %s OR proposed_project = %s;",
                     [DOOMED, DOOMED]).fetchone()[0]
    if n:
        left.append(f"session_proposal={n}")
    assert not left, "rows survived a project delete: " + ", ".join(left)
    assert not conn.execute("SELECT count(*) FROM project WHERE name = %s;",
                            [DOOMED]).fetchone()[0]


@pg
def test_the_sweep_takes_only_the_doomed_project(conn):
    _seed(conn, DOOMED)
    _seed(conn, KEEPER)
    _delete(conn, DOOMED)
    kept = [t for t in _project_tables(conn)
            if conn.execute(f"SELECT count(*) FROM {t} WHERE project = %s;",
                            [KEEPER]).fetchone()[0]]
    assert len(kept) >= 10, f"the sweep ate the other project's rows: {kept}"


@pg
def test_the_caller_is_told_how_many_RECORDS_went(conn):
    """It said '0 memories' while seven records went (2026-09-10): the count
    was read after the NOTES delete, so it reported notes, not records."""
    conn.execute("INSERT INTO document (project, record_id, title, content) "
                 "VALUES (%s,'r1','t','c'), (%s,'r2','t','c'), (%s,'r3','t','c');",
                 [DOOMED, DOOMED, DOOMED])
    conn.commit()
    out = _delete(conn, DOOMED)
    assert "3 memories" in out, out
