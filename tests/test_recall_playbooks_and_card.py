"""Using the Brain as a session does (2026-09-10): the playbooks — the
wiki — are a lane of recall. (The old graph card's tests left with that
graph, 2026-09-27.)"""
import os
import uuid

import pytest

from core import recall_pg


def test_recall_carries_no_old_graph_lines():
    """The old inventory graph's "[graph] …" lines left recall on 2026-09-27;
    a playbook's project is read from its name alone."""
    src = open("core/recall_pg.py", encoding="utf-8").read()
    assert "def graph_lines(" not in src and "entitylink" not in src
    projects = {"brain", "estate"}
    assert recall_pg.project_of_name("brain-map-box", projects) == "brain"
    assert recall_pg.project_of_name("estate-plan", projects) == "estate"
    assert recall_pg.project_of_name("atlas", projects) == ""
    assert recall_pg.project_of_name("working-on-a-vps", projects) == ""      # no project 'working'
    assert recall_pg.playbook_fence("brain-map-box", "brain", projects)
    assert not recall_pg.playbook_fence("estate-plan", "brain", projects)
    assert recall_pg.playbook_fence("atlas", "brain", projects)


psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("BRAIN_TEST_DSN", "")


@pytest.mark.skipif(not DSN, reason="BRAIN_TEST_DSN not set")
def test_the_playbooks_are_a_lane_of_recall():
    name = "t_pbrecall_" + uuid.uuid4().hex[:10]
    admin = psycopg.connect(DSN, autocommit=True)
    admin.execute(f"CREATE DATABASE {name}")
    try:
        c = psycopg.connect(DSN.replace("dbname=postgres", f"dbname={name}"))
        cur = c.cursor()
        cur.execute("CREATE TABLE playbook (name text PRIMARY KEY, content text NOT NULL, annotation text NOT NULL DEFAULT '', "
                    "created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now());")
        cur.execute("CREATE TABLE project (name text PRIMARY KEY);")
        cur.execute("INSERT INTO project (name) VALUES ('brain'), ('nv'), ('ms'), ('estate');")
        big = "a plate about many things. " * 2000 + " the hub is deployed with deploy.sh from a box"
        cur.execute("INSERT INTO playbook (name, content, annotation) VALUES "
                    "('brain-map-hub', 'hub.brethof.ai is one FastAPI process on its own box behind its own Cloudflare tunnel. Deploy: push then deploy.sh', 'Plate of brain-map: the hub deployed'), "
                    "('nv-render', 'How a chapter is rendered.', 'Novels rendering: chapter rendered'), "
                    "('atlas', 'How to map a project: the hub, the box, every wire.', 'the atlas method: map a project'), "
                    "('ms-map-prompting', %s, 'Plate of ms-map: prompting')", [big])
        c.commit()
        kws = recall_pg.fts_keywords("which box hosts the hub and how is it deployed")
        hits = recall_pg.playbook_hits(cur, kws, project="brain")
        names = [h["rid"] for h in hits]
        assert names and names[0] == "playbook:brain-map-hub" and hits[0]["type"] == "playbook"
        assert hits[0]["content"].startswith("Plate of brain-map")
        # THE FENCE (2026-09-10): another project's plate never enters this
        # session's block, even when its 50 KB body contains the words; a
        # bare method may.
        assert "playbook:ms-map-prompting" not in names
        assert "playbook:brain-map-hub" not in [h["rid"] for h in recall_pg.playbook_hits(cur, kws, project="nv")]
        assert "playbook:atlas" in [h["rid"] for h in recall_pg.playbook_hits(cur, recall_pg.fts_keywords("map the project hub box"), project="nv")]
        # no project (an owner's cross-project recall): no fence — another
        # project's playbook may answer; the support rule still holds, so a
        # big plate matched only by its body stays out
        assert "playbook:nv-render" in [h["rid"] for h in recall_pg.playbook_hits(cur, recall_pg.fts_keywords("chapter rendered"))]
        assert "playbook:ms-map-prompting" not in [h["rid"] for h in recall_pg.playbook_hits(cur, kws)]
        # SUPPORT: one keyword buried in a big body is not a match
        assert recall_pg.playbook_hits(cur, recall_pg.fts_keywords("what is the canary value for the deploy"), project="ms") == []
        assert recall_pg.playbook_hits(cur, recall_pg.fts_keywords("chapter rendered"), project="nv")[0]["rid"] == "playbook:nv-render"
        assert recall_pg.playbook_hits(cur, []) == []
        c.close()
    finally:
        admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        admin.close()
