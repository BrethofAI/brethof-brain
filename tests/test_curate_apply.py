"""The applier for the curator's create-only `records` shape.

It exists because the prompt was rewritten to emit
{"records":[...],"entities":[...]} while every applier still read
{"ops":[...]} — so the new curator could not write at all.

These tests guard the three things this applier does that the old one never
had to: mint the id the schema no longer carries, route by the `project`
field without ever inventing a project, and link an entity to a record only
when it genuinely belongs to it.
"""
from unittest import mock

import core.curate_apply as ca


def test_the_id_is_minted_from_the_title_and_dated():
    """The old shape carried an id; this one deliberately does not — 127 of
    127 real records came back with type/title/content/reason and nothing
    else. Dated because two facts about one subject weeks apart are two
    records; the merge door decides whether they fold, not the id."""
    got = ca.mint_id("The curator is setting the types for the graph",
                     "2026-08-29")
    assert got == "curator_setting_types_graph_2026_08_29"
    assert ca.mint_id("", "2026-08-29") == "note_2026_08_29"
    long = ca.mint_id("a " * 60 + "tail", "2026-08-29")
    assert len(long) <= 60 + len("_2026_08_29")


class FakeCur:
    def __init__(self, projects): self.projects, self._rows = projects, []
    def execute(self, sql, args=None):
        s = " ".join(sql.split())
        if s.startswith("SELECT DISTINCT project"):
            self._rows = [(p,) for p in self.projects]
        elif s.startswith("CREATE TABLE") or s.startswith("CREATE INDEX"):
            pass
    written: list = []
    def fetchall(self): return self._rows


class FakeConn:
    def __init__(self, projects=()): self.projects = list(projects)
    def cursor(self): return FakeCur(self.projects)
    def rollback(self): pass


def test_a_curator_never_creates_a_project_by_naming_one():
    """A curator using its kind vocabulary as a filing cabinet is how the
    junk project 'claim' appeared. An unknown name files LOCALLY and says
    so — it never provisions anything."""
    log = []
    recs = [{"title": "T", "content": "C", "project": "not_a_project"}]
    with mock.patch.object(ca.tools, "write_record",
                           return_value="Saved") as sm:
        ca.apply_records(FakeConn(["brain", "nv"]), "brain", recs, log)
    assert sm.call_args[0][0] == "brain"          # filed HERE, not there
    assert any("ROUTE REFUSED" in l for l in log)


def test_a_real_project_name_routes_there():
    log = []
    recs = [{"title": "T", "content": "C", "project": "nv"}]
    with mock.patch.object(ca.tools, "write_record",
                           return_value="Saved") as sm:
        ca.apply_records(FakeConn(["brain", "nv"]), "brain", recs, log)
    assert sm.call_args[0][0] == "nv"


def test_a_record_with_no_body_is_dropped():
    """A title pretending to be knowledge. The store already carries the
    long-title==content stub shape from a real data-loss bug."""
    log = []
    with mock.patch.object(ca.tools, "write_record") as sm:
        a, d = ca.apply_records(FakeConn(), "brain",
                                [{"title": "T", "content": ""},
                                 {"title": "", "content": "C"}], log)
    assert (a, d) == (0, 2) and sm.call_count == 0


# (the record-to-thing link tests left on 2026-09-17: the records and the
#  graph are two systems — founder; a record points only into the archive)


def test_kind_hint_has_its_patterns():
    """2026-09-02: a slice that removed the dead teach lane from tools.py also
    took _NORMATIVE and _FINDING with it. save_memory then raised AFTER the
    document was written — records landed, nothing after them ran (no heal
    enqueue, no links) — and the suite did not notice. This does."""
    from core import tools
    assert isinstance(tools._kind_hint("t", "never do X", False), str)
    assert isinstance(tools._kind_hint("t", "measured 40 ms", True), str)


def test_link_id_is_bare_even_when_stamped_with_the_project():
    """(From the old graph-hygiene set, 2026-09-04.)"""
    from core import curate_apply as ca
    assert ca.bare_id("brain:some_record_2026_09_04") == "some_record_2026_09_04"
    assert ca.bare_id("some_record_2026_09_04") == "some_record_2026_09_04"
    assert ca.bare_id("") == ""
