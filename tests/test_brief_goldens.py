"""THE BRIEF AND THE PROMPT BLOCK, BYTE FOR BYTE — the goldens step 2 of
brain-design-hub-brain moves the composition against (2026-09-10). A seeded
tenant with fixed timestamps; the law and the rule match mocked; the
embedder absent (keyword lane only, as in every test run). Regenerate ONLY
when the rendering is meant to change: BRAIN_WRITE_GOLDENS=1."""
import json
import os
import pathlib
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("BRAIN_TEST_DSN", "")
pg = pytest.mark.skipif(not DSN, reason="BRAIN_TEST_DSN not set")
GOLD = pathlib.Path(__file__).parent / "goldens"
WRITE = os.environ.get("BRAIN_WRITE_GOLDENS") == "1"

T0 = "2026-09-01 10:00:00+00"
T1 = "2026-09-05 12:00:00+00"
T2 = "2026-09-09 09:30:00+00"

SEED = f"""
INSERT INTO project (name) VALUES ('brain'), ('estate') ON CONFLICT DO NOTHING;
INSERT INTO chat (project, session_id, turn_index, role, text, ts) VALUES
  ('brain', 'old', 0, 'user', 'we moved the sentinel to adm last week', '{T0}'),
  ('brain', 'old', 1, 'assistant', 'done: sentinel runs on adm now, the watcher restarted', '{T0}'),
  ('brain', 'mid', 0, 'user', 'is pg_tde still in use or did gocryptfs replace it?', '{T1}'),
  ('brain', 'mid', 1, 'assistant', 'gocryptfs replaced pg_tde on the 27th; the ciphertext is what backups see', '{T1}'),
  ('brain', 'cur', 0, 'user', 'what about the hub pipe', '{T2}'),
  ('brain', 'cur', 1, 'assistant', 'the pipe is up and carries commands', '{T2}'),
  ('brain', 'cur', 50, 'user', 'and the fold?', '{T2}');
-- A TURN IS A USER PROMPT (2026-09-11): the description check fires at
-- prompt 50, so the session carries 48 filler prompts (earlier ts, so the
-- recap keeps the real last exchange whole and shows them as its older tail).
INSERT INTO chat (project, session_id, turn_index, role, text, ts)
  SELECT 'brain', 'cur', g, 'user', 'ok', '{T1}' FROM generate_series(2, 49) g;
INSERT INTO document (project, record_id, type, title, content, meta, updated_at, created_at) VALUES
  ('brain', 'pipe_design', 'decision', 'The pipe carries commands', 'hub to box commands with an id, a fixed vocabulary the box answers with rows', '{{}}', '{T1}', '{T1}'),
  ('brain', 'sentinel_on_adm', 'fact', 'The sentinel runs on adm', 'the fleet watcher runs on the admin box adm and probes over ssh', '{{}}', '{T0}', '{T0}'),
  ('brain', 'memory_index', 'index', 'index', 'pipe_design, sentinel_on_adm', '{{}}', '{T0}', '{T0}');
INSERT INTO note (project, session_id, title, body, written_at) VALUES
  ('brain', 'old', 'where it stood', 'the handover from the old session', '{T0}'),
  ('brain', 'mid', 'mid thread', 'the encryption swap is done', '{T1}');
INSERT INTO playbook (name, content) VALUES ('brain-plan', '## Decided\\n- x\\n## Next\\n1. build the pipe\\n2. the hook routes\\n3. the readers\\n## Parked\\n- y');
""".replace("\\n", "\n")

LAW = {"rules": [
    {"rule_id": "seed_one", "title": "★SEED — Four doors", "content": "Four doors, and nothing else writes.", "area": "all", "pin": True},
    {"rule_id": "purpose_brain", "title": "★ PROJECT PURPOSE — brain", "content": "brethof-brain — the memory product.", "area": "brain", "pin": True},
    {"rule_id": "unpinned", "title": "curator doctrine", "content": "never injected", "area": "brain", "pin": False}],
    "purpose": "brethof-brain — the memory product.", "note": ""}
RULE = {"rule_id": "git_policy", "title": "Local git only", "content": "Local git only — private repos never go to GitHub."}


@pytest.fixture
def tenant(monkeypatch):
    from core import embed, notes, playbooks, rules_hub, store
    parts = dict(kv.split("=", 1) for kv in DSN.split())
    for k, v in (("PG_HOST", parts.get("host")), ("PG_PORT", parts.get("port")),
                 ("PG_USER", parts.get("user")), ("PG_PASS", parts.get("password"))):
        if v:
            setattr(store, k, v)
    t = "gold_" + uuid.uuid4().hex[:10]
    store.provision_tenant(t)
    tok = store.bind(t)
    monkeypatch.setattr(rules_hub, "fetch", lambda project: LAW)
    monkeypatch.setattr(rules_hub, "match", lambda project, prompt, vector=None:
                        (RULE, "ok") if "github" in prompt.lower() else (None, "ok"))
    monkeypatch.setattr(embed, "embed_one", lambda *a, **k: None)
    monkeypatch.setattr(embed, "embed", lambda *a, **k: None)
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


def _check(name: str, text: str) -> None:
    path = GOLD / f"{name}.txt"
    if WRITE:
        path.write_text(text, encoding="utf-8")
        return
    assert path.exists(), f"golden {name} missing — run once with BRAIN_WRITE_GOLDENS=1"
    assert text == path.read_text(encoding="utf-8"), f"{name} differs from its golden"


@pg
def test_the_session_brief_matches_its_goldens(tenant):
    from core import injection
    whole = injection.brief_in_process("brain", part=0, tier="owner", source="compact", session_id="cur")
    _check("brief_whole_owner_compact", whole)
    parts = []
    for p in range(1, 12):
        blob = injection.brief_in_process("brain", part=p, tier="owner", source="startup", session_id="cur")
        if not blob:
            break
        parts.append(blob)
    assert 1 <= len(parts) <= 11
    _check("brief_parts_owner_startup", "\n\n=====\n\n".join(parts))
    _check("brief_whole_customer", injection.brief_in_process("brain", part=0, tier="customer", session_id="cur"))


@pg
def test_the_prompt_block_matches_its_goldens(tenant):
    from core import injection
    out = {}
    p1 = "should the private repos go to GitHub or stay on the local git only, and what replaced pg_tde on adm-cf?"
    for part in (0, 1, 2, 3):
        out[f"p1_part{part}"] = injection.ambient_in_process("brain", "cur", p1, part=part)
    p2 = "tell me about the pipe and the sentinel on adm and the fold"
    for part in (0, 1, 2):
        out[f"p2_part{part}"] = injection.ambient_in_process("brain", "cur", p2, part=part)
    out["short"] = injection.ambient_in_process("brain", "cur", "hi", part=1)
    _check("ambient_blocks", json.dumps(out, indent=1, ensure_ascii=False))
