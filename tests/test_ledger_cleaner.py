"""THE LEDGER CLEANER (founder 2026-10-03: "it's really worth to build it";
"go with one call per window"). The hub's Jev takes out only the obvious —
a new row that repeats an open one or is a step of the work in hand, and
open rows the window's new history lines and records show done — and the
agent walking the ledger keeps every other call. Report mode marks, on
deletes. The hub side runs on fakes; the box side is SQL and needs a real
Postgres (BRAIN_TEST_DSN, as scripts/ops/pt.sh sets it)."""
import asyncio
import json
import os
import uuid

import pytest

from hub import judge

DSN = os.environ.get("BRAIN_TEST_DSN", "")
pg = pytest.mark.skipif(not DSN, reason="BRAIN_TEST_DSN not set — the ledger lives in SQL")

T = {"id": "t1", "name": "box"}
ANSWER = {"records": [{"title": "Password rotated", "content": "BRAIN_DB_PASSWORD rotated today"}],
          "decisions": [{"kind": "decided", "what": "The backfill starts tonight."}],
          "ledger": [{"content": "Rotate the database password"}, {"content": "Score the run when it finishes"},
                     {"content": "Write the usage guide"}, {"content": "Tell marketing", "project": "marketing"}]}


class Hub:
    def __init__(self, box):
        self.box, self.asked, self.metered = box, [], []

    async def command(self, tenant, name, body, timeout=8):
        self.asked.append((name, body))
        return self.box

    def meter(self, tid, op, i, o):
        self.metered.append((op, i))


def _jev(monkeypatch, answers):
    """Jev faked: the probability per question id, by the words it carries."""
    from hub import jev
    monkeypatch.setattr(jev, "on", lambda tenant: True)
    seen = []

    async def noul(state, questions):
        seen.append(state)
        return {q: answers(q, state) for q in questions}, 100
    monkeypatch.setattr(jev, "noul", noul)
    return seen


def _answers(q, state):
    if q.endswith("_same"):                      # only the password row repeats its open twin
        return 0.9 if "password" in state["new row"] else 0.1
    if q.endswith("_step"):
        return 0.8 if state["ledger rows"][q.split("_")[0]].startswith("Score") else 0.2
    return 0.95 if q == "r0_done" else 0.3       # the backfill row is shown done


BOX = {"ok": True,
       "rows": [[{"id": 7, "content": "Rotate BRAIN_DB_PASSWORD after the leak"}], [], [{"id": 9, "content": "Other"}]],
       "open": [{"id": 11, "content": "Run the backfill from empty"}, {"id": 12, "content": "Fix the dropped window"}]}


def _run(monkeypatch, mode, box=BOX):
    monkeypatch.setenv("HUB_LEDGER_CLEAN", mode)
    monkeypatch.setenv("HUB_LEDGER_CLEAN_TENANTS", "box")
    seen = _jev(monkeypatch, _answers)
    hub, got = Hub(box), json.loads(json.dumps(ANSWER))
    asyncio.run(judge._ledger_clean(hub, T, "p", got, "2026-10-03"))
    return hub, got, seen


def test_report_mode_marks_and_writes_every_row(monkeypatch):
    hub, got, seen = _run(monkeypatch, "report")
    assert [a.get("clean") for a in got["ledger"]] == [
        {"why": "repeat", "of": 7, "p": 0.9}, {"why": "step", "p": 0.8}, None, None]
    assert got["ledger_closed"] == [{"id": 11, "p": 0.95}] and got["ledger_mode"] == "report"
    name, body = hub.asked[0]
    # one round trip: the window's own rows (never another project's) and its new lines and records
    assert name == "ledger_near" and body["rows"] == ["Rotate the database password", "Score the run when it finishes",
                                                       "Write the usage guide"]
    assert body["items"] == ["2026-10-03 Password rotated — BRAIN_DB_PASSWORD rotated today",
                             "2026-10-03 decided: The backfill starts tonight."]
    # the done check is ONE call per window, the window's items and the open rows together
    done = [s for s in seen if "open ledger rows" in s]
    assert len(done) == 1 and len(done[0]["open ledger rows"]) == 2 and len(done[0]["new history lines and records"]) == 2
    assert hub.metered == [("infer:jev", 400)]


def test_on_mode_drops_the_obvious_and_keeps_the_rest(monkeypatch):
    _, got, _ = _run(monkeypatch, "on")
    assert [a["content"] for a in got["ledger"]] == ["Write the usage guide", "Tell marketing"]
    assert got["ledger_dropped"] == 2 and got["ledger_closed"] == [{"id": 11, "p": 0.95}]
    # what was kept out travels to the box with its verdict (ledger_clean_log)
    kept_out = {d["content"]: d["why"] for d in got["ledger_dropped_rows"]}
    assert set(kept_out) == {"Rotate the database password", "Score the run when it finishes"}
    assert all(d.get("p") for d in got["ledger_dropped_rows"])


def test_off_or_another_tenant_or_a_silent_box_changes_nothing(monkeypatch):
    hub, got, _ = _run(monkeypatch, "off")
    assert not hub.asked and got == ANSWER
    monkeypatch.setenv("HUB_LEDGER_CLEAN", "on")
    monkeypatch.setenv("HUB_LEDGER_CLEAN_TENANTS", "someone-else")
    hub, got = Hub(BOX), json.loads(json.dumps(ANSWER))
    asyncio.run(judge._ledger_clean(hub, T, "p", got, "2026-10-03"))
    assert not hub.asked and got == ANSWER
    _, got, _ = _run(monkeypatch, "on", box={"ok": False})
    assert got == ANSWER


# ── the box side, on a real Postgres ────────────────────────────────────────
VOCAB = ["password", "backfill", "window", "guide", "marketing", "score"]


def _vec(text):
    t = text.lower()
    return [1.0 if w in t else 0.0 for w in VOCAB] + [0.01] * (768 - len(VOCAB))


@pytest.fixture
def conn(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    from core import embed, store
    name = "t_ledger_" + uuid.uuid4().hex[:10]
    admin = psycopg.connect(DSN, autocommit=True)
    admin.execute(f"CREATE DATABASE {name}")
    c = psycopg.connect(DSN.replace("dbname=postgres", f"dbname={name}"))
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "core", "schema.sql"), encoding="utf-8") as fh:
        c.execute(fh.read().replace(":dim", "768"))
    c.execute("INSERT INTO project (name) VALUES ('p'), ('marketing');")
    c.commit()

    class _Conn:
        def __enter__(self):
            return c

        def __exit__(self, *a):
            c.commit()
    monkeypatch.setattr(store, "connection", lambda: _Conn())
    monkeypatch.setattr(store, "ensure_ddl", lambda cur, key, ddl: cur.execute(ddl))
    monkeypatch.setattr(embed, "embed_batched", lambda texts, kind="document", **k: [_vec(t) for t in texts])
    try:
        yield c
    finally:
        c.close()
        admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        admin.close()


@pg
def test_the_box_finds_the_nearest_open_rows_and_fills_missing_vectors(conn):
    from core import ledger
    a = ledger.add("p", "Rotate the database password")
    b = ledger.add("p", "Run the backfill from empty")
    c_ = ledger.add("p", "Fix the dropped window")
    ledger.add("marketing", "Rotate the marketing password")      # another project is never shown
    ledger.mark("p", {c_: {"why": "done", "p": 0.9}})              # a marked row counts as gone
    cur = conn.cursor()
    got = ledger.near(cur, "p", ["rotate the password again"], ["decided: the backfill starts tonight",
                                                                "the window was found"], k_rows=1, k_open=5)
    assert got["rows"] == [[{"id": a, "content": "Rotate the database password"}]]
    assert got["open"][0] == {"id": b, "content": "Run the backfill from empty"}
    assert c_ not in [r["id"] for r in got["open"]]
    cur.execute("SELECT project, count(*) FROM ledger WHERE embedding IS NULL GROUP BY 1;")
    assert cur.fetchall() == [("marketing", 1)]           # the window's own project only


@pg
def test_the_save_files_marks_and_closes_and_never_a_phantom_project(conn):
    from core import ledger, pipecommands, tools
    old = ledger.add("p", "Run the backfill from empty")
    gone = ledger.add("p", "Fix the dropped window")
    out = pipecommands.write_saves("t1", {
        "project": "p", "session_id": "s1",
        "ledger": [{"content": "Rotate the password", "clean": {"why": "repeat", "of": 3, "p": 0.9}},
                   {"content": "Tell marketing", "project": "marketing"},
                   {"content": "Sync the site", "project": "brethof-brain"}],
        "ledger_closed": [{"id": old, "p": 0.95}], "ledger_mode": "report"})
    assert out["ledger"] == 3
    cur = conn.cursor()
    cur.execute("SELECT project, content FROM ledger ORDER BY id;")
    assert cur.fetchall()[2:] == [("p", "Rotate the password"), ("marketing", "Tell marketing"), ("p", "Sync the site")]
    marks = ledger.marks("p")
    assert marks[old] == {"why": "done", "p": 0.95} and marks[old + 2]["why"] == "repeat"
    listing = tools.list_ledger("p")
    assert "cleaner would drop: done or overtaken, p 0.95" in listing and "repeat of [3]" in listing
    # on: the closed row leaves the ledger, confined to the window's project
    pipecommands.write_saves("t1", {"project": "p", "session_id": "s1",
                                    "ledger_closed": [{"id": gone, "p": 0.9}], "ledger_mode": "on"})
    cur.execute("SELECT count(*) FROM ledger WHERE id = %s;", [gone])
    assert cur.fetchone()[0] == 0


@pg
def test_what_the_cleaner_takes_is_kept_with_its_verdict(conn):
    """Found 2026-10-11: the cleaner had removed 446 rows from the founder's
    memory and not one could be read or put back."""
    from core import ledger, pipecommands
    gone = ledger.add("p", "Fix the dropped window", origin="s0")
    pipecommands.write_saves("t1", {"project": "p", "session_id": "s1",
                                    "ledger_closed": [{"id": gone, "p": 0.91}], "ledger_mode": "on",
                                    "ledger_dropped_rows": [{"content": "Score  the run", "why": "step", "p": 0.7},
                                                           {"content": "Rotate it", "why": "repeat", "of": 3, "p": 0.6},
                                                           {"content": "  "}]})
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM ledger WHERE id = %s;", [gone])
    assert cur.fetchone()[0] == 0
    cur.execute("SELECT kind, row_id, content, origin, verdict FROM ledger_clean_log ORDER BY kind, id;")
    rows = cur.fetchall()
    assert rows[0] == ("closed", gone, "Fix the dropped window", "s0", {"why": "done", "p": 0.91})
    assert [r[0] for r in rows[1:]] == ["dropped", "dropped"]
    assert rows[1][2:] == ("Score the run", "s1", {"why": "step", "p": 0.7})
    assert rows[2][4] == {"why": "repeat", "of": 3, "p": 0.6}
    cur.execute("SELECT count(*) FROM ledger WHERE project = 'p';")
    assert cur.fetchone()[0] == 0                        # kept out means never filed
