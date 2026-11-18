"""THE RECORDS REPLAY (core/records_replay, founder 2026-09-26): the live
records job over the archive into an empty scratch tenant, each window judged
on the day it was said, heal after each save on that same day, windows said
after `until` left to the live curator. Real Postgres; the hub is a fake."""
import datetime as dt
import json
import os

import pytest

from tests.test_backfill import _session, conn  # noqa: F401  (the scratch database fixture)

DSN = os.environ.get("BRAIN_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="BRAIN_TEST_DSN not set — the replay lives in SQL")


def _wire(monkeypatch, conn, answers):
    from core import backfill, curate_apply, embed, judge, ledger, reconcile, store

    class _Conn:
        def __enter__(self):
            return conn

        def __exit__(self, *a):
            conn.commit()

    monkeypatch.setattr(store, "connection", lambda: _Conn())
    monkeypatch.setattr(store, "bind", lambda t: None)
    monkeypatch.setattr(store, "unbind", lambda t: None)
    monkeypatch.setattr(backfill, "_src_connection", lambda source: _Conn())
    monkeypatch.setattr(embed, "embed", lambda texts, kind="", timeout=None: [[0.0] * 768 for _ in texts])
    seen = {"asks": [], "heals": [], "ledger": []}

    def ask(op, body, timeout=None):
        body = dict(body, today=judge.TODAY.get()) if judge.TODAY.get() and "today" not in body else body
        seen["asks"].append((op, body))
        a = answers.pop(0)
        if a == "down":
            raise judge.NoAnswer("hub down")
        return {"answer": a, "fault": "" if a else "not json", "input_tokens": 1, "output_tokens": 1}
    monkeypatch.setattr(judge, "ask", ask)

    def apply_records(c, project, recs, log, today="", origin=None):
        cur = c.cursor()
        seen.setdefault("minted_on", []).append(today)
        for i, r in enumerate(recs):
            rid = f"r{len(seen['asks'])}_{i}"
            cur.execute("INSERT INTO document (project, record_id, type, title, content, source, captured_at, meta) "
                        "VALUES (%s, %s, 'fact', %s, %s, 'curator', now(), %s::jsonb);",
                        [project, rid, r["title"], r["content"], json.dumps({"origin": origin})])
            r["_id"] = f"{project}:{rid}"
        return len(recs), 0
    monkeypatch.setattr(curate_apply, "apply_records", apply_records)
    monkeypatch.setattr(ledger, "add", lambda p, c, **k: seen["ledger"].append((p, c)))
    monkeypatch.setattr(reconcile, "drain", lambda t, p, force=False: seen["heals"].append((p, judge.TODAY.get(), force)) or {})
    return seen


def test_each_window_is_judged_and_healed_on_its_own_day_and_later_ones_are_left(conn, monkeypatch):
    from core import records_replay
    t0 = dt.datetime(2026, 7, 28, 9, 0, tzinfo=dt.timezone.utc)
    _session(conn, "s1", 6, t0)                                     # two windows: 5 + 1 (07-28)
    _session(conn, "s2", 1, t0 + dt.timedelta(days=40))             # 09-06: after `until`
    rec = lambda t: {"records": [{"type": "fact", "title": t, "content": t + " body"}],  # noqa: E731
                     "ledger": [{"content": "we will do X"}], "failed": []}
    monkeypatch.setattr(records_replay.time, "sleep", lambda s: None)
    monkeypatch.setattr(records_replay, "HALT_AFTER", 2)
    # window 2 fails three tries, is asked again in place, and lands
    seen = _wire(monkeypatch, conn, [rec("A"), None, "down", {"records": [], "failed": ["records: not json"]},
                                     {"records": [], "failed": []}])
    log = []
    tot = records_replay.run("p", "x", until="2026-09-01T00:00:00+00:00", log=log.append)
    assert tot["done"] and tot["windows"] == 2 and tot["records"] == 1 and tot["skipped"] == 0
    assert any("asking again in place" in m for m in log)
    op, body = seen["asks"][0]
    assert op == "curate_window" and body["records_only"] is True and body["today"] == "2026-07-28"
    assert body["records_so_far"] == [] and "EXCHANGE 1 of 5" in body["piece"]
    # the session's own, so far — production's lists (2026-10-05): the curator's
    # with id and type, and the still-true check's, so Jev can name what to apply
    assert seen["asks"][1][1]["records_so_far"] == [{"title": "A", "content": "A body", "id": "r1_0", "type": "fact"}]
    assert seen["asks"][1][1]["records_session"][0]["id"] == "r1_0" and seen["asks"][1][1]["session_id"] == "s1"
    assert all("s2" not in b["piece"] for _, b in seen["asks"])                          # left to the live curator
    assert seen["heals"][0] == ("p", "2026-07-28", False) and seen["heals"][-1] == ("p", "2026-07-28", True)
    # the rebuild writes no ledger (founder 2026-10-05): the text it asks has no
    # ledger paragraph, and a row the model sends anyway is dropped
    assert seen["ledger"] == [] and seen["minted_on"] == ["2026-07-28"]
    assert body["records_prompt"] == "noledger"
    cur = conn.cursor()
    cur.execute("SELECT to_char(updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI'), "
                "to_char(captured_at AT TIME ZONE 'UTC', 'YYYY-MM-DD') FROM document;")
    assert cur.fetchall() == [("2026-07-28 09:04", "2026-07-28")]     # the time the window was said
    # resumes: everything finished, nothing asked again
    seen2 = _wire(monkeypatch, conn, [])
    assert records_replay.run("p", "x", until="2026-09-01T00:00:00+00:00", log=log.append)["windows"] == 0
    assert seen2["asks"] == []


def test_a_window_that_keeps_failing_halts_its_project_there_and_the_next_run_starts_at_it(conn, monkeypatch):
    from core import records_replay
    monkeypatch.setattr(records_replay.time, "sleep", lambda s: None)
    monkeypatch.setattr(records_replay, "HALT_AFTER", 2)
    t0 = dt.datetime(2026, 7, 28, 9, 0, tzinfo=dt.timezone.utc)
    _session(conn, "s1", 6, t0)                                     # two windows
    ok = {"records": [{"type": "fact", "title": "A", "content": "A body"}], "failed": []}
    _wire(monkeypatch, conn, [ok, None, None, None, None, None, None])
    log = []
    tot = records_replay.run("p", "x", log=log.append)
    assert not tot["done"] and tot["halted"].startswith("s1#") and tot["windows"] == 1
    cur = conn.cursor()
    cur.execute("SELECT done_hi, finished FROM replay_state;")
    assert cur.fetchall() == [(9, False)]                           # the failed window is not marked done
    seen = _wire(monkeypatch, conn, [{"records": [], "failed": []}])
    tot = records_replay.run("p", "x", log=log.append)
    assert tot["done"] and tot["windows"] == 1 and "u5 of s1" in seen["asks"][0][1]["piece"]


def test_a_session_the_cut_crosses_stays_open_for_the_run_without_a_cut(conn, monkeypatch):
    """2026-10-05: the 27B's part ran to 08-01 and marked a July→August session
    finished, so GLM's part from there would have skipped its August windows."""
    from core import records_replay
    t0 = dt.datetime(2026, 7, 31, 23, 54, tzinfo=dt.timezone.utc)
    _session(conn, "s1", 11, t0)                  # windows 0-4 (July), 5-9 (crosses midnight), 10 (August)
    none = {"records": [], "failed": []}
    _wire(monkeypatch, conn, [none])
    tot = records_replay.run("p", "x", until="2026-08-01T00:00:00+00:00", since="2026-06-30", log=lambda m: None)
    assert tot["done"] and tot["windows"] == 1
    cur = conn.cursor()
    cur.execute("SELECT done_hi, finished FROM replay_state;")
    assert cur.fetchall() == [(9, False)]
    seen = _wire(monkeypatch, conn, [none, none])
    tot = records_replay.run("p", "x", since="2026-06-30", log=lambda m: None)
    assert tot["windows"] == 2 and "u5 of s1" in seen["asks"][0][1]["piece"]
    cur.execute("SELECT done_hi, finished FROM replay_state;")
    assert cur.fetchall() == [(21, True)]
