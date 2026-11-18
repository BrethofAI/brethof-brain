"""The leaked-text redaction door (founder 2026-08-12 design, 2026-08-24
build): panel-only, human-only, once per hour, needle 6-100 chars, needle
never stored. These pins keep the door exactly one door."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core import redact  # noqa: E402


class _Cur:
    """Programmable cursor: answers fetchone() from a queue, records every
    execute so the pins can inspect the SQL that actually ran."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []
        self.rowcount = 3

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params or []))

    def fetchone(self):
        return self.answers.pop(0)


def _run(answers, needle="bm_live_deadbeef", execute=False):
    cur = _Cur(answers)
    conn = mock.MagicMock()
    conn.__enter__.return_value.cursor.return_value = cur
    with mock.patch.object(redact.store, "connection", return_value=conn), \
         mock.patch.object(redact.store, "ensure_ddl"), \
         mock.patch.object(redact.store, "current_tenant",
                           return_value="t_test"):
        out = redact.redact(needle, execute=execute)
    return out, cur


class Bounds(unittest.TestCase):
    def test_short_needle_refused(self):
        # 5 chars and under would shred prose ("the", "key ", …).
        with self.assertRaises(ValueError):
            redact.redact("abcde")

    def test_long_needle_refused(self):
        with self.assertRaises(ValueError):
            redact.redact("x" * 101)

    def test_error_message_never_echoes_the_needle(self):
        try:
            redact.redact("leak!")
        except ValueError as e:
            self.assertNotIn("leak!", str(e))


class DryRun(unittest.TestCase):
    def test_counts_and_never_updates(self):
        out, cur = _run([(4,), (2,)])
        self.assertEqual(out, {"chat_rows": 4, "doc_rows": 2,
                               "executed": False, "rate_limited": False,
                               "retry_after_s": 0})
        for sql, _ in cur.calls:
            self.assertNotIn("UPDATE", sql)

    def test_needle_rides_params_never_sql(self):
        out, cur = _run([(0,), (0,)], needle="bm_live_deadbeef")
        for sql, _ in cur.calls:
            self.assertNotIn("bm_live_deadbeef", sql)

    def test_like_wildcards_in_needle_are_escaped(self):
        out, cur = _run([(0,), (0,)], needle="pass%word_1")
        pats = [p for _, params in cur.calls for p in params]
        self.assertIn("%pass\\%word\\_1%", pats)


class Execute(unittest.TestCase):
    def test_replaces_in_chat_and_documents_and_logs_counts_only(self):
        # answers: cooldown check (no prior redaction)
        out, cur = _run([(None,)], execute=True)
        self.assertTrue(out["executed"])
        self.assertEqual(out["chat_rows"], 3)
        sqls = [sql for sql, _ in cur.calls]
        self.assertTrue(any("UPDATE chat SET text = replace" in s
                            for s in sqls))
        self.assertTrue(any("UPDATE document SET title = replace" in s
                            for s in sqls))
        log = [(s, p) for s, p in cur.calls if "redaction_log" in s
               and "INSERT" in s]
        self.assertEqual(len(log), 1)
        # THE PIN THAT MATTERS: the log row carries counts, never the needle.
        self.assertNotIn("bm_live_deadbeef", str(log[0][1]))
        self.assertEqual(log[0][1], [3, 3])

    def test_once_per_hour(self):
        out, cur = _run([(120.0,)], execute=True)   # redacted 2 min ago
        self.assertFalse(out["executed"])
        self.assertTrue(out["rate_limited"])
        self.assertEqual(out["retry_after_s"], 3600 - 120)
        for sql, _ in cur.calls:
            self.assertNotIn("UPDATE chat", sql)

    def test_cooldown_expired_lets_it_through(self):
        out, cur = _run([(7200.0,)], execute=True)  # 2 hours ago
        self.assertTrue(out["executed"])


class NoAgentSurface(unittest.TestCase):
    def test_customer_tools_never_name_the_verb(self):
        """Founder 2026-08-12: 'nothing an agent can reach destroys the
        archive'. The redaction verb must not appear on any agent surface."""
        root = os.path.join(os.path.dirname(__file__), "..")
        src = open(os.path.join(root, "api/customer_tools.py"),
                   encoding="utf-8").read()
        self.assertNotIn("redact", src.lower())


if __name__ == "__main__":
    unittest.main()
