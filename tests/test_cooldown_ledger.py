"""THE COOLDOWN'S UNIT IS A USER PROMPT (2026-09-11), and the ledger counts
re-fires. Founder 2026-09-10: the same thing cannot come back more often
than every 20 messages, and whether 20 is too often must be measured.
Until this change a turn was a chat ROW — every tool call is one — and
the measured 8.5 rows per prompt made the 40-row cooldown three to five
prompts. Real scratch Postgres (BRAIN_TEST_DSN)."""
import os
import uuid

import pytest

from core import composer, injection, store

DSN = os.environ.get("BRAIN_TEST_DSN", "")
pg = pytest.mark.skipif(not DSN, reason="BRAIN_TEST_DSN not set")


def test_the_defaults_are_in_prompts():
    assert (composer.RECORD_COOLDOWN, composer.RULE_COOLDOWN_TURNS) == (20, 20)
    src = open("core/injection.py", encoding="utf-8").read()
    body = src.split("def _turn_now(", 1)[1].split("def _stamps(", 1)[0]
    assert "role = 'user'" in body and "turn_index" not in body


@pytest.fixture
def tenant():
    parts = dict(kv.split("=", 1) for kv in DSN.split())
    for k, v in (("PG_HOST", parts.get("host")), ("PG_PORT", parts.get("port")),
                 ("PG_USER", parts.get("user")), ("PG_PASS", parts.get("password"))):
        if v:
            setattr(store, k, v)
    t = "cool_" + uuid.uuid4().hex[:10]
    store.provision_tenant(t)
    tok = store.bind(t)
    try:
        yield t
    finally:
        store.unbind(tok)
        store.deprovision_tenant(t)


@pg
def test_a_turn_is_a_user_prompt_and_the_ledger_counts_refires(tenant):
    sid = "s1"
    with store.connection() as c:
        cur = c.cursor()
        cur.execute("INSERT INTO project (name) VALUES ('p') ON CONFLICT DO NOTHING;")
        # three prompts, each followed by a handful of tool-call rows
        i = 0
        for _ in range(3):
            cur.execute("INSERT INTO chat (project, session_id, turn_index, role, text) VALUES ('p', %s, %s, 'user', 'q');", [sid, i]); i += 1
            for _ in range(6):
                cur.execute("INSERT INTO chat (project, session_id, turn_index, role, text) VALUES ('p', %s, %s, 'assistant', '[tool_use: Bash]');", [sid, i]); i += 1
        c.commit()
        assert i == 21
        assert injection._turn_now(cur, sid) == 3
        injection._stamp(c, cur, sid, 3, ["p:a", "rules:x"])
        injection._stamp(c, cur, sid, 3, ["p:a"])            # a sibling part, same prompt: not a re-fire
        injection._stamp(c, cur, sid, 25, ["p:a"])           # the cooldown passed: a re-fire
        cur.execute("SELECT item_id, turn_idx, first_turn, fires FROM cooldown_ledger WHERE session_id = %s ORDER BY 1;", [sid])
        assert cur.fetchall() == [("p:a", 25, 3, 2), ("rules:x", 3, 3, 1)]
        st = injection._stamps(cur, sid)
        assert st == {"p:a": 25, "rules:x": 3}
    # the predicate in prompts: stamped at 3, prompt 22 is eligible again; prompt 22 with a 40 would not be
    assert not injection._is_cooling(3, 23, composer.RECORD_COOLDOWN)
    assert injection._is_cooling(3, 22, composer.RECORD_COOLDOWN)
    assert injection._is_cooling(3, 23, 40)
