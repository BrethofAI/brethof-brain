"""A period of a memory (2026-10-23): core/period — the window parser, the
date-only listings and the report, counted in the database. The report is
SQL over the real schema, so it is proven on a real Postgres: a known week is
seeded and every number the report prints is checked against it."""
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from core import period

DSN = os.environ.get("BRAIN_TEST_DSN", "")


# ── the window ──────────────────────────────────────────────────────────────
def test_a_bare_date_until_is_inclusive():
    s, u, err = period.parse("2026-10-01", "2026-10-07")
    assert not err
    assert s == datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert u == datetime(2026, 10, 8, tzinfo=timezone.utc)


def test_iso_times_are_kept_and_naive_is_utc():
    s, u, err = period.parse("2026-10-01T12:00:00Z", "2026-10-01T18:30:00")
    assert not err and s.hour == 12 and u.minute == 30 and u.tzinfo is not None


def test_bad_or_backwards_windows_say_why():
    assert period.parse("yesterday", "")[2]
    assert period.parse("2026-10-07", "2026-10-01")[2] == "since must be before until"
    assert "at most" in period.parse("2025-01-01", "2026-10-01")[2]
    assert period.parse("", "") == (None, None, "")


def test_the_label_names_the_last_day_not_the_exclusive_end():
    s, u, _ = period.parse("2026-10-01", "2026-10-07")
    assert period.label(s, u) == "2026-10-01 → 2026-10-07"


# ── the report, on a real database ──────────────────────────────────────────
pg = pytest.mark.skipif(not DSN, reason="BRAIN_TEST_DSN not set — the report is SQL "
                                        "over the real schema and needs a real Postgres")


@pytest.fixture
def db():
    psycopg = pytest.importorskip("psycopg")
    from tests.test_delete_project_sweep import _schema
    name = f"period_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(DSN, autocommit=True)
    admin.execute(f"CREATE DATABASE {name}")
    c = psycopg.connect(DSN.replace("dbname=postgres", f"dbname={name}"))
    try:
        _schema(c)
        yield c
    finally:
        c.close()
        admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        admin.close()


def db_id(c, sql, args):
    return c.execute(sql, args).fetchone()[0]


def _seed(c):
    t = lambda d, h=12: datetime(2026, 10, d, h, tzinfo=timezone.utc)   # noqa: E731
    for p in ("shop", "blog"):
        c.execute("INSERT INTO project (name) VALUES (%s) ON CONFLICT DO NOTHING;", [p])
    # chat: 4 shop messages over 2 days in 2 sessions inside the week, 1 outside
    for i, (sid, role, d) in enumerate([("s1", "user", 2), ("s1", "assistant", 2),
                                        ("s2", "user", 3), ("s2", "assistant", 3),
                                        ("s3", "user", 20)]):
        c.execute("INSERT INTO chat (project, session_id, turn_index, role, text, ts) "
                  "VALUES ('shop', %s, %s, %s, 'x', %s);", [sid, i, role, t(d)])
    # decisions, by the writer's links: an old rejection that comes back
    # (reopened), an old decision overturned in the week (a reversal), a
    # refinement that names what it replaced but only continues (neither),
    # plus a tried and a dropped line
    def line(p, day, kind, what, follows=None, how="", replaced=""):
        return db_id(c, "INSERT INTO decision (project, day, kind, what, follows, how, replaced) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id;",
                     [p, day, kind, what, follows, how, replaced])
    rej = line("shop", "2026-09-01", "rejected", "use Stripe")
    line("shop", "2026-10-02", "decided", "use Stripe after all", follows=rej, how="continues")
    old = line("shop", "2026-09-20", "decided", "ship on Monday")
    line("shop", "2026-10-03", "decided", "ship on Friday", follows=old, how="overturns",
         replaced="ship on Monday")
    line("shop", "2026-10-03", "tried", "a cheaper host", follows=old, how="continues",
         replaced="the old host")
    line("blog", "2026-10-04", "dropped", "the newsletter")
    # records: one new in the week, one older changed in it, one untouched
    for rid, created, updated in [("r_new", t(2), t(2)), ("r_old", t(1, 0) - timedelta(days=20), t(3)),
                                  ("r_quiet", t(1, 0) - timedelta(days=20), t(1, 0) - timedelta(days=20))]:
        c.execute("INSERT INTO document (project, record_id, type, title, content, created_at, updated_at) "
                  "VALUES ('shop', %s, 'fact', %s, 'x', %s, %s);", [rid, rid, created, updated])
    c.commit()


@pg
def test_the_report_counts_the_weeks_output(db):
    _seed(db)
    s, u, _ = period.parse("2026-10-01", "2026-10-07")
    text = period.report(db.cursor(), None, s, u)
    assert "TOTAL: 2 decided, 1 tried, 0 rejected, 1 dropped; 1 reversals, 1 reopened" in text
    assert "records 1 new, 1 changed" in text
    assert "4 messages (2 from the user) in 2 sessions" in text
    assert "- shop: 2/1/0/0 · 1 · 1 · 1/1/0" in text and "- blog: 0/0/0/1" in text
    import re
    plain = re.sub(r" #\d+\]", "]", text)
    assert "ship on Friday — OVERTURNS [2026-09-20] decided: ship on Monday" in plain
    assert "use Stripe after all — BACK AFTER REJECTED [2026-09-01]: use Stripe" in plain
    assert "a cheaper host" not in text.split("REVERSALS", 1)[1]


@pg
def test_a_project_scope_sees_only_its_own(db):
    _seed(db)
    s, u, _ = period.parse("2026-10-01", "2026-10-07")
    text = period.report(db.cursor(), ["blog"], s, u)
    assert "- blog:" in text and "- shop:" not in text and "Stripe" not in text


@pg
def test_the_date_only_listings(db):
    _seed(db)
    s, u, _ = period.parse("2026-10-01", "2026-10-07")
    cur = db.cursor()
    ls = period.lines(cur, None, s, u)
    assert [x.split(" #", 1)[0] for x in ls] == ["[line 2026-10-02", "[line 2026-10-03",
                                                 "[line 2026-10-03", "[line 2026-10-04"]
    recs = period.records(cur, ["shop"], s, u)
    assert [r.split("] ", 1)[1].split(" ")[0] for r in recs] == ["shop:r_old", "shop:r_new"]
    assert recs[0].startswith("[upd") and recs[1].startswith("[new")
    assert period.counts(cur, ["shop"], s, u) == "4 messages in 2 sessions in this period."


@pg
def test_an_empty_period_says_so(db):
    _seed(db)
    s, u, _ = period.parse("2026-06-01", "2026-06-07")
    assert "nothing happened" in period.report(db.cursor(), None, s, u)


@pg
def test_verdicts_are_kept_and_a_rejudgement_adds_a_row(db):
    _seed(db)
    s, u, _ = period.parse("2026-10-01", "2026-10-07")
    cur = db.cursor()
    n, problems = period.save_verdicts(cur, [
        {"project": "shop", "item": "the payment provider came back", "verdict": "wasted",
         "reason": "rejected in September, decided again", "line_ids": [1, 2]},
        {"project": "shop", "item": ""}], s, u, judged_by="test-judge")
    assert n == 1 and problems == ["#2: item and verdict are required"]
    period.save_verdicts(cur, [{"project": "shop", "item": "the payment provider came back",
                                "verdict": "not wasted", "reason": "it needed more time"}], s, u)
    db.commit()
    hist = period.list_verdicts(cur, ["shop"], s, u)
    assert len(hist) == 2 and "— wasted —" in hist[0] and "— not wasted —" in hist[1]
    assert "period 2026-10-01→2026-10-07" in hist[0] and "by test-judge" in hist[0]
    assert period.list_verdicts(cur, ["blog"], s, u) == []
    no, why = period.save_verdicts(cur, [{"project": "shop", "item": "x", "verdict": "y"}], s, u,
                                   writable=lambda p: False)
    assert no == 0 and "no write access" in why[0]
