"""What an archive row really is (founder 2026-09-22): agent reports and
notices stay in view under their own label; a compaction recap is never
stored, searched or read as conversation."""
from __future__ import annotations

from unittest import mock

from core import chatkind, hubfeed

RECAP = "This session is being continued from a previous conversation that ran out of context. Summary: ..."
REPORT = "<task-notification>\n<task-id>a1</task-id>\n<result>Part 1: same 8</result></task-notification>"
NOTICE = "<local-command-stdout>Set model to `claude-opus-5`</local-command-stdout>"


def test_rows_are_known_by_their_real_source():
    assert chatkind.kind("user", RECAP) == "recap"
    assert chatkind.kind("user", REPORT) == "agent report"
    assert chatkind.kind("user", NOTICE) == "system notice"
    assert chatkind.kind("user", "Yes, run the 36 pairs on GLM") == "user"
    assert chatkind.kind("assistant", RECAP) == "assistant"          # only the founder's rows are re-read


def test_the_curators_pieces_skip_recaps_and_keep_reports_inside_the_work():
    turns = [{"index": 1, "role": "user", "text": "build it"},
             {"index": 2, "role": "assistant", "text": "building"},
             {"index": 3, "role": "user", "text": REPORT},
             {"index": 4, "role": "assistant", "text": "the judge is in"},
             {"index": 5, "role": "user", "text": RECAP},
             {"index": 6, "role": "user", "text": "next thing"}]
    pieces = hubfeed._pieces(turns)
    assert [(lo, hi) for _, lo, hi in pieces] == [(1, 4), (6, 6)]    # the report did not start a new exchange
    assert "[agent report] <task-notification>" in pieces[0][0]
    assert "continued from a previous conversation" not in "".join(p for p, _, _ in pieces)


def test_a_recap_is_not_written_to_the_archive():
    sent = []

    class Cur:
        rowcount = 1
        def executemany(self, sql, rows): sent.extend(rows)
        def execute(self, sql, params=None): pass      # nothing held yet
        def fetchall(self): return []
    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return Cur()
        def commit(self): pass
    from core import archive
    from core import drainer, proposer
    with mock.patch.object(archive.store, "connection", lambda: Conn()), \
         mock.patch.object(archive.store, "current_tenant", lambda: "t"), \
         mock.patch.object(drainer, "note_work", lambda t: None), \
         mock.patch.object(proposer, "trigger", lambda *a, **k: None), \
         mock.patch.object(archive, "ensure_project", lambda c, p: None):
        archive.archive_turns("p", "s", [{"index": 1, "role": "user", "text": RECAP},
                                         {"index": 2, "role": "user", "text": "real prompt"}])
    assert [r[2] for r in sent] == [2]


def test_every_search_filter_carries_the_recap_test():
    assert "This session is being continued" in chatkind.NOT_RECAP and "%%" in chatkind.NOT_RECAP
    assert chatkind.not_recap("t.").startswith("NOT (left(t.text")
