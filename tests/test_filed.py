"""core.filed — heal's eyes (2026-09-29): how rules and playbooks are cut
into sections, and which playbooks a project's heal may see."""
from core import filed


def test_pieces_cut_at_headings_and_stay_under_the_cap():
    body = "# A\n\none\n\ntwo\n\n## B\n\n" + ("x" * 500 + "\n\n") * 4
    got = list(filed.pieces(body, cap=900))
    assert got[0] == ("A", "# A\n\none\n\ntwo")
    assert all(h == "B" for h, _ in got[1:]) and len(got) >= 3
    assert all(len(t) <= 1800 for _, t in got)


def test_a_project_sees_its_own_and_the_shared_playbooks_never_another_projects():
    projects = {"brain", "ms", "m", "global"}
    assert filed.scope_of("brain-map-hub", "brain", projects) == "brain"
    assert filed.scope_of("ms-map", "brain", projects) is None
    assert filed.scope_of("ms-map", "ms", projects) == "ms"          # not project "m"'s
    assert filed.scope_of("global-testing-gotchas", "brain", projects) == filed.SHARED
    assert filed.scope_of("atlas", "brain", projects) == filed.SHARED
    assert filed.scope_of("working-on-a-vps", "ms", projects) == filed.SHARED
