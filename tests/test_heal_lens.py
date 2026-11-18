"""STEP ONE OF THE MEMORY LINE (founder 2026-09-17, "try it and test it"):
heal's lens must see the same subject across wording — the PREDECESSOR
SLOT. Real scratch Postgres. (The movement-name half left with the old
inventory graph, 2026-09-27.)"""
import os
import uuid

import pytest

from core import reconcile, store

DSN = os.environ.get("BRAIN_TEST_DSN", "")
pg = pytest.mark.skipif(not DSN, reason="BRAIN_TEST_DSN not set")


@pytest.fixture
def tenant():
    parts = dict(kv.split("=", 1) for kv in DSN.split())
    for k, v in (("PG_HOST", parts.get("host")), ("PG_PORT", parts.get("port")),
                 ("PG_USER", parts.get("user")), ("PG_PASS", parts.get("password"))):
        if v:
            setattr(store, k, v)
    t = "lens_" + uuid.uuid4().hex[:10]
    store.provision_tenant(t)
    tok = store.bind(t)
    try:
        with store.connection() as c:
            cur = c.cursor()
            cur.execute("INSERT INTO project (name) VALUES ('p') ON CONFLICT DO NOTHING;")
            # the old one-line statement of the subject, linked to few common names
            cur.execute("INSERT INTO document (project, record_id, type, title, content, meta, updated_at, created_at) VALUES "
                        "('p', 'old_model', 'decision', 'Curation model: deepseek, live', 'the curator runs deepseek', '{}', '2026-08-03', '2026-08-03');")
            # thirty recent records that share MORE names with the arrival than the old one does
            for i in range(30):
                rid = f"recent_{i}"
                cur.execute("INSERT INTO document (project, record_id, type, title, content, meta, updated_at, created_at) VALUES "
                            "('p', %s, 'fact', %s, 'hub curator registrar teacher pipe', '{}', %s, %s);",
                            [rid, f"hub note {i}", f"2026-09-{1 + i % 9:02d}", f"2026-09-{1 + i % 9:02d}"])
            # the arrival: the new statement of the same subject, different words
            cur.execute("INSERT INTO document (project, record_id, type, title, content, meta, updated_at, created_at) VALUES "
                        "('p', 'hub_on_glm', 'gotcha', 'Hub on GLM: the reasoning dial is required', "
                        "'the hub moved every job to GLM; DeepSeek is retired; the curator, registrar, teacher and pipe follow', '{}', now(), now());")
            # vectors: the arrival and the old statement point the same way; the
            # thirty neighbours point elsewhere (no embedder in the test)
            same = "[" + ",".join(["1"] + ["0"] * 767) + "]"
            other = "[" + ",".join(["0", "1"] + ["0"] * 766) + "]"
            cur.execute("UPDATE document SET embedding = %s::halfvec WHERE record_id IN ('old_model', 'hub_on_glm');", [same])
            cur.execute("UPDATE document SET embedding = %s::halfvec WHERE record_id LIKE 'recent_%%';", [other])
            c.commit()
        yield t
    finally:
        store.unbind(tok)
        store.deprovision_tenant(t)


@pg
def test_the_predecessor_slot_shows_the_oldest_statement_of_the_subject(tenant):
    with store.connection() as c:
        cur = c.cursor()
        cur.execute("SELECT project, record_id, type, title, content, updated_at FROM document WHERE record_id = 'hub_on_glm';")
        r = cur.fetchone()
        rec = {"_id": f"p:{r[1]}", "project": r[0], "record_id": r[1], "type": r[2], "title": r[3], "content": r[4]}
        cands = reconcile.candidates(c, "p", rec)
        ids = [x["record_id"] for x in cands]
        assert "old_model" in ids, ids
        assert "elder" in next(x["why"] for x in cands if x["record_id"] == "old_model")
        # records only: no channel reads the graph's names
        src = open("core/reconcile.py", encoding="utf-8").read()
        assert "entity_record" not in src.split("def candidates(", 1)[1].split("def _exists(", 1)[0]
