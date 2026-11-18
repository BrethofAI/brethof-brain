"""THE THREE METHODS EVERY INSTALL STARTS WITH (core/seedplaybooks,
2026-09-29): seeded where missing, updated only while untouched, never over
the customer's own edit, deletion or same-named playbook. Real Postgres."""
import os
import uuid

import pytest

DSN = os.environ.get("BRAIN_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="BRAIN_TEST_DSN not set")


@pytest.fixture
def cur(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    from core import playbooks, store
    name = "t_seedpb_" + uuid.uuid4().hex[:10]
    admin = psycopg.connect(DSN, autocommit=True)
    admin.execute(f"CREATE DATABASE {name}")
    c = psycopg.connect(DSN.replace("dbname=postgres", f"dbname={name}"))
    monkeypatch.setattr(store, "ensure_ddl", lambda cur, key, ddl: cur.execute(ddl))
    c.execute(playbooks._DDL)
    c.commit()
    try:
        yield c.cursor()
    finally:
        c.close()
        admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        admin.close()


def _content(cur, name):
    cur.execute("SELECT content FROM playbook WHERE name = %s;", [name])
    r = cur.fetchone()
    return r[0] if r else None


def test_a_fresh_tenant_gets_all_three_and_a_second_pass_is_a_no_op(cur):
    from core import seedplaybooks as S
    got = S.ensure(cur)
    assert got == {"atlas": "seeded", "plan": "seeded", "prospectus": "seeded"}
    for n, text in S.METHODS.items():
        assert _content(cur, n) == text.strip()
    assert "founder" not in " ".join(S.METHODS.values()).lower()     # customer-neutral
    assert S.ensure(cur) == {"atlas": "current", "plan": "current", "prospectus": "current"}


def test_the_customers_edit_deletion_and_own_playbook_are_never_touched(cur, monkeypatch):
    from core import seedplaybooks as S
    cur.execute("INSERT INTO playbook (name, content) VALUES ('atlas', 'my own atlas');")
    S.ensure(cur)
    assert _content(cur, "atlas") == "my own atlas"                  # never seeded by us: theirs
    cur.execute("UPDATE playbook SET content = 'my plan now' WHERE name = 'plan';")
    cur.execute("DELETE FROM playbook WHERE name = 'prospectus';")
    monkeypatch.setitem(S.METHODS, "plan", S.PLAN + "\nA new line.")
    monkeypatch.setitem(S.METHODS, "prospectus", S.PROSPECTUS + "\nA new line.")
    got = S.ensure(cur)
    assert got["atlas"] == "the customer's own — left"
    assert got["plan"] == "edited by the customer — left" and _content(cur, "plan") == "my plan now"
    assert got["prospectus"] == "deleted by the customer — left" and _content(cur, "prospectus") is None


def test_an_untouched_method_follows_our_new_text(cur, monkeypatch):
    from core import seedplaybooks as S
    S.ensure(cur)
    monkeypatch.setitem(S.METHODS, "atlas", S.ATLAS + "\nA new rule.")
    assert S.ensure(cur)["atlas"] == "updated"
    assert _content(cur, "atlas").endswith("A new rule.")
    assert S.ensure(cur)["atlas"] == "current"
