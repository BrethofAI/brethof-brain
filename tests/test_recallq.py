"""B+v3, the per-prompt search's starting point (founder 2026-11-17)."""
import asyncio
import itertools

import pytest

from core import composer, recallq

_SCOPES = itertools.count()


@pytest.fixture(autouse=True)
def _breaker_closed(monkeypatch):
    """The question writer's breaker is process-wide; each test starts closed."""
    monkeypatch.setitem(composer._QDOWN, "until", 0.0)


def test_the_reply_is_cut_where_its_topics_change():
    reply = ("## The relay\nIt runs on prod with keys in RAM, pushed from the vault on adm.\n\n"
             "## Recall\n- The prompt alone finds little for short follow-ups like yes do it.\n"
             "- The reply carries the substance of what was discussed today.\n\n"
             "ok")
    t = recallq.topics(reply)
    assert len(t) >= 1 and all(len(x) > 40 for x in t) and "ok" not in t
    assert any("relay" in x for x in t) and any("Recall" in x for x in t)
    long = "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(20))
    cut = recallq.topics(long)
    assert len(cut) == recallq.MAX_TOPICS and "Paragraph 0" in cut[0] and "Paragraph 19" in cut[-1]


def test_queries_are_the_message_each_topic_with_its_lead_and_the_questions():
    qs = recallq.queries("yes, do it", "First topic about the relay and its keys in RAM.\n\n"
                                       "Second topic about recall searching with the reply.", ["What is the relay?"])
    assert qs[0] == "yes, do it" and qs[-1] == "What is the relay?"
    assert all(q.startswith("yes, do it\n") for q in qs[1:-1])


def test_parse_questions_drops_markers_and_short_lines():
    text = "1. How is the relay deployed on prod?\n- What does the gate check?\n\nok\n* Where do keys live?"
    assert recallq.parse_questions(text) == ["How is the relay deployed on prod?", "What does the gate check?",
                                             "Where do keys live?"]


def test_fuse_orders_by_rank_and_keeps_each_records_best_score():
    a = [(40.0, "r1", {}), (38.0, "r2", {})]
    b = [(30.0, "r2", {}), (50.0, "r3", {})]
    out = recallq.fuse([a, b], 5)
    assert [r for _s, r, _e in out][0] == "r2"            # found twice ranks first
    assert dict((r, s) for s, r, _e in out) == {"r1": 40.0, "r2": 38.0, "r3": 50.0}


def _ambient(questions):
    sent = []

    async def ask(cmd, body):
        sent.append((cmd, dict(body)))
        if cmd == "last_reply":
            return {"ok": True, "text": "We built the relay on prod."}
        if cmd == "stamps":
            return {"ok": True, "turn": 1, "stamps": {}}
        return {"ok": True, "hits": []}

    async def match(_t):
        return None, "ok"

    async def purpose():
        return ""
    asyncio.run(composer.ambient(ask, match, purpose, "brain", "s1", "yes, do it and check the gate",
                                 part=0, scope=f"t{next(_SCOPES)}", questions=questions))
    return sent


def test_the_hub_adds_the_models_questions_to_the_search():
    async def q(prompt, reply):
        assert reply == "We built the relay on prod."
        return ["How is the relay deployed?"]
    rec = [b for c, b in _ambient(q) if c == "recall"][0]
    assert rec["reply_topics"] is True and rec["questions"] == ["How is the relay deployed?"]


def test_a_slow_or_failing_question_writer_never_holds_recall(monkeypatch):
    monkeypatch.setattr(composer, "QUESTIONS_TIMEOUT_S", 0.1)

    async def slow(prompt, reply):
        await asyncio.sleep(5)

    async def boom(prompt, reply):
        raise RuntimeError("model down")
    for writer in (slow, boom):
        rec = [b for c, b in _ambient(writer) if c == "recall"][0]
        assert rec["reply_topics"] is True and "questions" not in rec


def test_a_short_follow_up_is_searched_once_the_reply_is_read():
    from core import brieftext as T
    assert not T.trivial_prompt("yes, do it", allow_short=True)
    assert T.trivial_prompt("yes, do it")                        # the old shape stays as it was
    assert T.trivial_prompt("/compact", allow_short=True) and T.trivial_prompt("  ", allow_short=True)
    assert T.trivial_prompt("<task-notification> done", allow_short=True)
    async def q(prompt, reply):
        return ["What does it refer to?"]
    rec = [b for c, b in _ambient_prompt(q, "yes, do it") if c == "recall"]
    assert rec and rec[0]["questions"] == ["What does it refer to?"]


def _ambient_prompt(questions, prompt):
    sent = []

    async def ask(cmd, body):
        sent.append((cmd, dict(body)))
        if cmd == "last_reply":
            return {"ok": True, "text": "We built the relay."}
        if cmd == "stamps":
            return {"ok": True, "turn": 1, "stamps": {}}
        return {"ok": True, "hits": []}

    async def match(_t):
        return None, "ok"

    async def purpose():
        return ""
    asyncio.run(composer.ambient(ask, match, purpose, "brain", "s2", prompt, part=0,
                                 scope="t" + str(id(sent)), questions=questions))
    return sent


def test_a_failing_model_trips_the_breaker_and_later_prompts_do_not_wait(monkeypatch):
    import time as _t
    monkeypatch.setattr(composer, "QUESTIONS_TIMEOUT_S", 0.2)
    monkeypatch.setitem(composer._QDOWN, "until", 0.0)
    calls = []

    async def slow(prompt, reply):
        calls.append(1)
        await asyncio.sleep(5)
    _ambient(slow)
    t0 = _t.monotonic()
    rec = [b for c, b in _ambient(slow) if c == "recall"][0]
    assert len(calls) == 1 and _t.monotonic() - t0 < 0.15        # second prompt skipped the writer
    assert rec["reply_topics"] is True and "questions" not in rec
    monkeypatch.setitem(composer._QDOWN, "until", 0.0)              # a minute later: tried again

    async def ok(prompt, reply):
        return ["What is it?"]
    assert [b for c, b in _ambient(ok) if c == "recall"][0]["questions"] == ["What is it?"]


def test_the_recall_questions_job_gets_one_try_on_a_short_clock():
    from hub import server
    assert "recall_q" in server.FAST_JOBS and server.FAST_TIMEOUT_S <= 10


def test_a_failed_call_trips_the_breaker_but_an_empty_answer_does_not():
    calls = []

    async def failed(prompt, reply):
        calls.append(1)
        return None
    _ambient(failed)
    _ambient(failed)
    assert len(calls) == 1                                          # the second prompt skipped the writer
    composer._QDOWN["until"] = 0.0
    calls.clear()

    async def empty(prompt, reply):
        calls.append(1)
        return []
    _ambient(empty)
    _ambient(empty)
    assert len(calls) == 2


def test_a_slow_last_reply_shrinks_the_question_writers_share_of_the_budget(monkeypatch):
    """One budget for the prompt (2026-11-18): time spent before the question
    writer comes off its share, so the search always keeps its reserve."""
    import time as _t
    monkeypatch.setattr(composer, "AMBIENT_BUDGET_S", 1.0)
    monkeypatch.setattr(composer, "RECALL_RESERVE_S", 0.5)
    monkeypatch.setattr(composer, "QUESTIONS_TIMEOUT_S", 6.0)
    monkeypatch.setitem(composer._QDOWN, "until", 0.0)
    sent = []

    async def ask(cmd, body):
        sent.append(cmd)
        if cmd == "last_reply":
            await asyncio.sleep(0.3)
            return {"ok": True, "text": "We built the relay."}
        if cmd == "stamps":
            return {"ok": True, "turn": 1, "stamps": {}}
        return {"ok": True, "hits": []}

    async def match(_t):
        return None, "ok"

    async def purpose():
        return ""

    async def slow(prompt, reply):
        await asyncio.sleep(5)
    t0 = _t.monotonic()
    asyncio.run(composer.ambient(ask, match, purpose, "brain", "sb", "which relay?", part=0,
                                 scope="t" + str(id(sent)), questions=slow))
    took = _t.monotonic() - t0
    assert "recall" in sent and took < 0.75, took        # 0.3 s reply + at most 0.2 s of the writer


def test_a_slow_history_check_falls_back_to_the_nearest_lines(monkeypatch):
    import time as _t
    monkeypatch.setattr(composer, "AMBIENT_BUDGET_S", 0.6)

    async def ask(cmd, body):
        if cmd == "history_near":
            return {"ok": True, "lines": [{"kind": "decided", "what": f"line {i}", "day": "2026-11-18"}
                                          for i in range(12)]}
        if cmd == "stamps":
            return {"ok": True, "turn": 1, "stamps": {}}
        return {"ok": True, "hits": []}

    async def match(_t):
        return None, "ok"

    async def purpose():
        return ""

    async def bears(prompt, lines):
        await asyncio.sleep(5)
    t0 = _t.monotonic()
    out = asyncio.run(composer.ambient(ask, match, purpose, "brain", "sh", "what did we decide?",
                                       part=composer.HISTORY_PART, scope="h", graph=True, bears=bears))
    assert _t.monotonic() - t0 < 0.8
    assert "line 0" in out
