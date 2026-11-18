"""THE GLOBAL LAYER RIDES WITH EVERY PROJECT'S SEARCH (founder 2026-09-12).
Real scratch Postgres: a fact filed in `global` answers a project-scoped
search; a key scoped away from global never sees it; the chat archive
stays the project's own."""
import os
import uuid

import pytest

from core import recall_pg, store

DSN = os.environ.get("BRAIN_TEST_DSN", "")
pg = pytest.mark.skipif(not DSN, reason="BRAIN_TEST_DSN not set")


@pytest.fixture
def tenant():
    parts = dict(kv.split("=", 1) for kv in DSN.split())
    for k, v in (("PG_HOST", parts.get("host")), ("PG_PORT", parts.get("port")),
                 ("PG_USER", parts.get("user")), ("PG_PASS", parts.get("password"))):
        if v:
            setattr(store, k, v)
    t = "glob_" + uuid.uuid4().hex[:10]
    store.provision_tenant(t)
    tok = store.bind(t)
    try:
        with store.connection() as c:
            cur = c.cursor()
            cur.execute("INSERT INTO project (name) VALUES ('brain'), ('global'), ('ms') ON CONFLICT DO NOTHING;")
            cur.execute("INSERT INTO document (project, record_id, type, title, content, meta) VALUES "
                        "('global', 'kopia_backup', 'fact', 'Workstation backup is kopia', 'kopia snapshots the workstation nightly to hetzner', '{}'), "
                        "('ms', 'kopia_note', 'fact', 'kopia mention in ms', 'kopia was mentioned once for the studio renders', '{}'), "
                        "('brain', 'pipe', 'fact', 'the pipe', 'the hub sends commands down the pipe', '{}');")
            c.commit()
        yield t
    finally:
        store.unbind(tok)
        store.deprovision_tenant(t)


@pg
def test_a_global_fact_answers_a_project_scoped_search_and_walls_hold(tenant):
    with store.connection() as c:
        ids = [h[1] for h in recall_pg.recall_hits(c, None, "kopia backup of the workstation", project="brain",
                                                    include_chat=False, fts="idf")]
        assert "global:kopia_backup" in ids and "ms:kopia_note" not in ids
        # a key scoped to brain alone: global is not in its world
        ids = [h[1] for h in recall_pg.recall_hits(c, None, "kopia backup of the workstation", project="brain",
                                                    include_chat=False, fts="idf", allowed_projects=["brain"])]
        assert "global:kopia_backup" not in ids
        # searching global itself asks for global once, not twice
        ids = [h[1] for h in recall_pg.recall_hits(c, None, "kopia backup", project="global",
                                                    include_chat=False, fts="idf")]
        assert ids.count("global:kopia_backup") == 1


@pg
def test_the_decision_lines_ride_under_recall_and_walls_hold(tenant, monkeypatch):
    """2026-09-28: recall reads the decision lines again — a dated section
    under the results; a key scoped away from a project never sees its
    lines; a tenant with no lines gets no section."""
    from core import decisionline
    with store.connection() as c:
        cur = c.cursor()
        assert recall_pg.history_section(cur, None, "unlimited heal add-on", project="brain") == ""
        cur.execute(decisionline.DDL)
        cur.execute("INSERT INTO decision (project, day, kind, what, why, things) VALUES "
                    "('brain', '2026-09-03', 'rejected', 'A flat unlimited-heal add-on is refused', "
                    " 'unlimited heal on our key is the cost hole', ARRAY['unlimited heal']), "
                    "('ms', '2026-09-01', 'decided', 'unlimited heal renders for ms', '', ARRAY['unlimited heal']);")
        c.commit()
        monkeypatch.setattr("core.embed.embed_one", lambda *a, **k: None)
        text = recall_pg.recall_text(c, None, "sell an unlimited heal add-on", project="brain", include_chat=False)
        assert "— HISTORY" in text and "[line 2026-09-03] rejected (brain)" in text and "why: unlimited heal" in text
        assert "(ms)" not in text
        # unscoped: every project; scoped to ms: brain's line never shows
        allp = recall_pg.history_section(cur, None, "unlimited heal", project="")
        assert "(brain)" in allp and "(ms)" in allp
        only_ms = recall_pg.history_section(cur, None, "unlimited heal", project="", allowed_projects=["ms"])
        assert "(ms)" in only_ms and "(brain)" not in only_ms
        c.rollback()
