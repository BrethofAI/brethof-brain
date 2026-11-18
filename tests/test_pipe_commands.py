"""The pipe as a two-way command channel (design brain-design-hub-brain,
step 1, 2026-09-10): a FIXED vocabulary the hub speaks down and the box
answers with rows; bounded on both sides; the readers' rendering unchanged."""
import asyncio
import json
import os
import time
import uuid

import pytest

from core import injection, pipeclient, pipecommands


# ── the vocabulary is one list, pinned on both sides ────────────────────────
def test_hub_and_box_speak_the_same_fixed_vocabulary():
    from hub import server
    assert server.COMMANDS == pipecommands.VOCABULARY
    assert "infer" not in server.COMMANDS and "sql" not in server.COMMANDS
    assert pipecommands.HANDLERS["save"] is pipecommands.write_saves
    # the old inventory graph's commands left both sides on 2026-09-27
    for gone in ("cards", "known_here", "since", "graph_summary", "fold_candidates",
                 "merge", "drop_alias", "gloss", "graph_apply"):
        assert gone not in server.COMMANDS and gone not in pipecommands.VOCABULARY, gone


# ── the hub's door ──────────────────────────────────────────────────────────
class _WS:
    def __init__(self, box=None):
        self.sent = []
        self.box = box            # cid -> answer dict, or a callable

    async def send_text(self, s):
        m = json.loads(s)
        self.sent.append(m)
        if self.box is not None:
            ans = self.box(m) if callable(self.box) else self.box
            if ans is not None:
                from hub import server
                fut = server.WAITING.pop(m["cid"], None)
                if fut and not fut.done():
                    fut.set_result({**ans, "cid": m["cid"]})


def test_the_hub_sends_a_hub_chosen_cid_and_returns_the_answer_without_it():
    from hub import server
    tenant = {"id": "t1", "name": "one"}
    ws = _WS(box={"ok": True, "hits": [1, 2]})
    server.PIPES["t1"] = ws
    try:
        ans = asyncio.run(server.command(tenant, "recall", {"project": "p", "query": "q"}))
    finally:
        server.PIPES.pop("t1", None)
    assert ans["ok"] and ans["hits"] == [1, 2] and "cid" not in ans and "ms" in ans
    m = ws.sent[0]
    assert m["op"] == "recall" and m["body"] == {"project": "p", "query": "q"} and len(m["cid"]) == 24


def test_the_hub_refuses_unknown_commands_and_missing_pipes_at_once():
    from hub import server
    tenant = {"id": "t2", "name": "two"}
    assert asyncio.run(server.command(tenant, "drop_table", {}))["why"].startswith("unknown command")
    assert asyncio.run(server.command(tenant, "recall", {}))["why"] == "no pipe"


def test_the_hub_times_out_and_forgets_the_cid():
    from hub import server
    tenant = {"id": "t3", "name": "three"}
    ws = _WS(box=None)                                   # a box that never answers
    server.PIPES["t3"] = ws
    try:
        t0 = time.monotonic()
        ans = asyncio.run(server.command(tenant, "notes", {"project": "p"}, timeout=0.2))
    finally:
        server.PIPES.pop("t3", None)
    assert ans == {"ok": False, "why": "timeout"} and time.monotonic() - t0 < 2
    m = ws.sent[0]
    assert m["op"] == "notes" and m["body"] == {"project": "p"} and len(m["cid"]) == 24
    assert m["cid"] not in server.WAITING


def test_the_in_flight_cap_holds_the_next_command_until_one_is_answered():
    from hub import server
    tenant = {"id": "t4", "name": "four"}
    ws = _WS(box=None)
    server.PIPES["t4"] = ws
    server.CMD_SEMS["t4"] = asyncio.Semaphore(2)

    async def run():
        a = asyncio.create_task(server.command(tenant, "stamps", {}, timeout=1.0))
        b = asyncio.create_task(server.command(tenant, "stamps", {}, timeout=1.0))
        c = asyncio.create_task(server.command(tenant, "stamps", {}, timeout=1.0))
        await asyncio.sleep(0.05)
        assert len(ws.sent) == 2, "the third waits at the cap"
        first = ws.sent[0]["cid"]
        server.WAITING.pop(first).set_result({"ok": True, "cid": first, "turn": 1})
        await asyncio.sleep(0.05)
        assert len(ws.sent) == 3, "answering one lets the next through"
        return await asyncio.gather(a, b, c)
    try:
        ra, rb, rc = asyncio.run(run())
    finally:
        server.PIPES.pop("t4", None)
        server.CMD_SEMS.pop("t4", None)
    assert ra["ok"] and ra["turn"] == 1 and not rb["ok"] and not rc["ok"]


def test_push_save_goes_through_the_command_door():
    src = open("hub/server.py", encoding="utf-8").read()
    body = src.split("async def push_save(", 1)[1].split("\nasync def ", 1)[0]
    assert 'command(tenant, "save", body' in body and "WAITING[cid]" not in body


# ── the box's side ──────────────────────────────────────────────────────────
def test_the_box_refuses_unknown_ops_and_says_why_on_failure(monkeypatch):
    ans = asyncio.run(pipeclient.answer_command("t", "sql", {}))
    assert ans == {"ok": False, "why": "unknown op"}
    monkeypatch.setitem(pipecommands.HANDLERS, "boom", lambda tenant, body: 1 / 0)
    ans = asyncio.run(pipeclient.answer_command("t", "boom", {}))
    assert not ans["ok"] and ans["why"].startswith("ZeroDivisionError")


def test_the_box_answers_late_to_nobody_and_says_so(monkeypatch, capsys):
    monkeypatch.setattr(pipeclient, "DEADLINE_S", 0.2)
    monkeypatch.setitem(pipecommands.HANDLERS, "slow", lambda tenant, body: time.sleep(0.6) or {"x": 1})
    ans = asyncio.run(pipeclient.answer_command("t", "slow", {}))
    assert not ans["ok"] and "deadline" in ans["why"]
    assert "late result is dropped" in capsys.readouterr().out


def test_a_slow_command_does_not_hold_a_fast_one(monkeypatch):
    monkeypatch.setitem(pipecommands.HANDLERS, "slow", lambda tenant, body: time.sleep(0.5) or {"who": "slow"})
    monkeypatch.setitem(pipecommands.HANDLERS, "fast", lambda tenant, body: {"who": "fast"})

    class WS:
        def __init__(self):
            self.out = []

        async def send(self, s):
            self.out.append(json.loads(s))

    async def run():
        ws = WS()
        sem = asyncio.Semaphore(4)
        await asyncio.gather(
            pipeclient._handle(ws, sem, "t", {"cid": "a", "op": "slow", "body": {}}),
            pipeclient._handle(ws, sem, "t", {"cid": "b", "op": "fast", "body": {}}))
        return ws.out
    out = asyncio.run(run())
    assert [m["cid"] for m in out] == ["b", "a"] and out[0]["who"] == "fast" and out[1]["ok"]


def test_every_handler_runs_under_the_tenant_binding(monkeypatch):
    from core import store
    seen = []
    monkeypatch.setitem(pipecommands.HANDLERS, "who", lambda tenant, body: seen.append(store.current_tenant()) or {})
    pipecommands.run("t_bound", "who", {})
    assert seen == ["t_bound"]
    with pytest.raises(KeyError):
        pipecommands.run("t_bound", "nope", {})


# ── the readers render as before, from rows ─────────────────────────────────
def test_plan_next_lines_can_return_raw_items_for_the_hub_to_clip():
    content = "## Decided\n- x\n## Next\n1. " + "a" * 300 + "\n2. two\n## Parked\n- y"
    assert len(injection.plan_next_lines(content)[0]) < 230 and injection.plan_next_lines(content)[0].endswith("…")
    assert injection.plan_next_lines(content, clip=0)[0] == "1. " + "a" * 300
    assert injection.plan_next_lines(content, limit=1, clip=0) == ["1. " + "a" * 300]


# ── the handlers on a real tenant ───────────────────────────────────────────
psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("BRAIN_TEST_DSN", "")
pg = pytest.mark.skipif(not DSN, reason="BRAIN_TEST_DSN not set")

SEED = """
INSERT INTO project (name) VALUES ('brain'), ('estate'), ('state') ON CONFLICT DO NOTHING;
INSERT INTO chat (project, session_id, turn_index, role, text, ts) VALUES
  ('brain', 'old', 0, 'user', 'we moved the sentinel to adm', now() - interval '2 days'),
  ('brain', 'old', 1, 'assistant', 'done: sentinel runs on adm now', now() - interval '2 days'),
  ('brain', 'cur', 0, 'user', 'what about the hub pipe', now()),
  ('brain', 'cur', 1, 'assistant', 'the pipe is up', now());
INSERT INTO document (project, record_id, type, title, content, meta) VALUES
  ('brain', 'pipe_design', 'decision', 'The pipe carries commands', 'hub to box commands with an id, a fixed vocabulary', '{}'),
  ('state', 'brain_status', 'state', 'brain', 'building step 1', '{"project": "brain"}'),
  ('state', 'open_loops', 'state', 'loops', '- [ ] ship the pipe (brain:pipe_design)', '{}');
INSERT INTO note (project, session_id, title, body) VALUES ('brain', 'old', 'where it stood', 'the handover');
INSERT INTO playbook (name, content) VALUES ('brain-plan', '## Decided\n- x\n## Next\n1. build the pipe\n2. the hook routes\n## Parked\n- y');
"""


@pytest.fixture
def tenant():
    """A tenant provisioned the real way (schema.sql), seeded with a few
    rows of each kind, dropped after."""
    from core import notes, playbooks, store
    parts = dict(kv.split("=", 1) for kv in DSN.split())
    for k, v in (("PG_HOST", parts.get("host")), ("PG_PORT", parts.get("port")),
                 ("PG_USER", parts.get("user")), ("PG_PASS", parts.get("password"))):
        if v:
            setattr(store, k, v)
    t = "pipe_" + uuid.uuid4().hex[:10]
    store.provision_tenant(t)
    tok = store.bind(t)
    try:
        with store.connection() as c:
            cur = c.cursor()
            notes.ensure(cur)
            playbooks._ensure(cur)
            cur.execute(SEED)
            c.commit()
        yield t
    finally:
        store.unbind(tok)
        store.deprovision_tenant(t)


def run(t, op, **body):
    return pipecommands.run(t, op, body)


@pg
def test_the_readers_answer_rows(tenant):
    assert run(tenant, "projects")["projects"] == [{"name": "brain", "records": 1}, {"name": "estate", "records": 0}]
    n = run(tenant, "notes", project="brain")["notes"]
    assert n[0]["session_id"] == "old" and n[0]["body"] == "the handover"
    assert run(tenant, "plan_next", project="brain")["items"] == ["1. build the pipe", "2. the hook routes"]
    r = run(tenant, "recap", project="brain")
    assert r["session_id"] == "cur" and [t["role"] for t in r["tail"]] == ["user", "assistant"]
    recs = run(tenant, "records", project="brain", types=["decision"])["records"]
    assert recs[0]["record_id"] == "pipe_design"


@pg
def test_recall_reads_the_memory(tenant):
    h = run(tenant, "recall", project="brain", query="commands with a fixed vocabulary", k=5, include_chat=False)
    assert h["hits"] and h["hits"][0]["rid"] == "brain:pipe_design" and not h["folded"]
    h2 = run(tenant, "recall", project="brain", query="and the hub?", session_id="cur", fold_previous=True, include_chat=False)
    assert h2["folded"]


@pg
def test_recall_from_the_reply_and_the_questions_b_v3(tenant):
    """B+v3 (founder 2026-11-17): a scrap of a message finds what the model's
    question names; the box reports how many searches it joined; the previous
    reply is read back for the hub's question writer."""
    assert run(tenant, "last_reply", session_id="cur")["text"] == "the pipe is up"
    assert run(tenant, "last_reply", session_id="nobody")["text"] == ""
    h = run(tenant, "recall", project="brain", query="yes, do it", session_id="cur", k=5,
            fold_previous=True, include_chat=False, reply_topics=True,
            questions=["How do the hub and box commands with a fixed vocabulary work?"])
    assert h["searches"] >= 2 and "brain:pipe_design" in [x["rid"] for x in h["hits"]]
    assert all(isinstance(x["score"], float) for x in h["hits"])


@pg
def test_the_ledger_and_the_proposal_write_through_commands(tenant):
    assert run(tenant, "stamps", session_id="cur") == {"turn": 1, "stamps": {}}
    assert run(tenant, "stamp", session_id="cur", turn=1, ids=["brain:pipe_design", "ent:adm"])["stamped"] == 2
    assert run(tenant, "stamps", session_id="cur")["stamps"] == {"brain:pipe_design": 1, "ent:adm": 1}
    assert run(tenant, "proposal_line", session_id="cur")["proposal"] is None
    assert run(tenant, "write_proposal", session_id="cur", filed="brain", proposed="estate", confidence=0.8, why="adm work")["written"] == 1
    assert run(tenant, "write_proposal", session_id="cur", filed="brain", proposed="estate", confidence=0.9, why="again")["written"] == 0
    p = run(tenant, "proposals", project="brain")["proposals"]
    assert p[0]["session_id"] == "cur" and p[0]["project"] == "estate"
    line = run(tenant, "proposal_line", session_id="cur")["proposal"]
    assert line["proposed"] == "estate" and line["shown"] == 1
    hc = run(tenant, "heal_candidates", project="brain", record_id="pipe_design")
    assert hc["record"]["record_id"] == "pipe_design" and isinstance(hc["candidates"], list)
    assert run(tenant, "heal_candidates", project="brain", record_id="nope")["record"] is None


@pg
def test_last_session_is_the_last_conversation_not_a_save(tenant):
    """2026-11-10, Aurora: an explicit save is a one-turn session of its own
    (save-<hex>, role 'saved'); the newest one stood in as LAST SESSION, so an
    app that saves its state after every sitting saw its own JSON there."""
    from core import store
    with store.connection() as c:
        c.cursor().execute(
            "INSERT INTO chat (project, session_id, turn_index, role, text, ts) "
            "VALUES ('brain', 'save-ab12cd34ef56ab78', 0, 'saved', 'SAVED TO MEMORY\n{\"stage\": 2}', "
            "now() + interval '1 day');")
        c.commit()
    r = run(tenant, "recap", project="brain")
    assert r["session_id"] == "cur", r
