"""query_raw's guard reads SQL, not the words inside the quotes.

Measured 2026-09-10 on the live box: `SELECT count(*) FROM chat WHERE text
ILIKE '%delete%'` was refused as "SELECT/WITH only — no writes, no DDL",
and `SELECT 'a;b'` as "one statement only". Neither was true, and both are
ordinary questions to ask a memory full of words like delete_project and
CREATE TABLE. The guard now blanks string literals before it matches, so it
stays exactly as strict about statements and says nothing about content.

What still refuses: a real second statement, and a real write — including a
data-modifying CTE, which is the only shape that could otherwise reach a
write through the subquery wrapper."""
import unittest
from unittest import mock

from core import tools


class _Reached(Exception):
    """Raised in place of the database: the guard let this query through."""


class GuardReadsSqlNotProse(unittest.TestCase):
    def _refusal(self, sql: str) -> str:
        """The guard's answer, with the database swapped for a tripwire — so
        an allowed query is proven allowed without a Postgres."""
        def _boom(*a, **k):
            raise _Reached
        with mock.patch.object(tools.store, "connection", _boom):
            try:
                out = tools.query_raw(sql)
            except _Reached:
                return ""
        return out if out.startswith("Error:") else ""

    # ── what must be allowed through the guard ──────────────────────────
    def test_a_keyword_inside_a_literal_is_content(self):
        for sql in ("SELECT 1 WHERE 'delete_project' = 'delete_project'",
                    "SELECT 1 WHERE 'CREATE TABLE foo' <> ''",
                    "SELECT 1 WHERE 'sed -e s/a/b/; echo' <> ''",
                    "SELECT 1 WHERE 'it''s a drop of rain' <> ''"):
            self.assertEqual("", self._refusal(sql), sql)

    # ── what must still be refused ──────────────────────────────────────
    def test_a_real_write_is_still_refused(self):
        for sql in ("DELETE FROM chat",
                    "UPDATE document SET title = 'x'",
                    "WITH x AS (INSERT INTO ledger (project) VALUES ('p') "
                    "RETURNING *) SELECT * FROM x",
                    "SELECT 1; DROP TABLE chat"):
            self.assertTrue(self._refusal(sql).startswith("Error:"), sql)

    def test_two_statements_are_still_refused(self):
        self.assertIn("one statement",
                      self._refusal("SELECT 1 WHERE 'a' = 'a'; SELECT 2"))


if __name__ == "__main__":
    unittest.main()
