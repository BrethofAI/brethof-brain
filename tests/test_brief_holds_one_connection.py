"""2026-09-10, after a compact: the session-start brief held one pool
connection and asked for a second for the plan block; eight parallel parts
on a pool of four waited on each other until PoolTimeout. Since step 2 of
the design the brief is COMPOSED from commands: the composer and the
renderer never open a connection at all, and every command opens exactly
one, for itself, and closes it before the next."""
import ast


def test_the_composer_and_the_renderer_never_touch_the_store():
    for path in ("core/composer.py", "core/brieftext.py"):
        src = open(path, encoding="utf-8").read()
        assert "store.connection" not in src and "import store" not in src, path
        names = {n.names[0].name for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ImportFrom) and n.module is None}
        assert "store" not in names and "psycopg" not in src, path


def test_the_plan_command_reads_through_its_own_cursor():
    src = open("core/pipecommands.py", encoding="utf-8").read()
    plan = src.split('@command("plan_next")', 1)[1].split("@command(", 1)[0]
    assert "playbooks.get(" in plan and "cur=conn.cursor()" in plan


def test_playbooks_get_reads_through_a_given_cursor(monkeypatch):
    from core import playbooks

    class Cur:
        def __init__(self):
            self.sql = []

        def execute(self, q, args=None):
            self.sql.append(q)

        def fetchone(self):
            return ("x-plan", "body", "", None)

    monkeypatch.setattr(playbooks, "_ensure", lambda cur: None)
    cur = Cur()
    assert playbooks.get("X-Plan", cur=cur) == ("x-plan", "body", "", None)
    assert any("FROM playbook" in q for q in cur.sql)
