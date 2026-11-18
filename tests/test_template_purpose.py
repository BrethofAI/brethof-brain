"""A template's purpose reaches every project that matches it (2026-11-10).

Aurora's test tenant: add_project('u_%', purpose=…) stored purpose_u_template
with area 'u_%', yet every fan project's brief said NO PURPOSE SET and the
fan curators read no purpose at all — hub/compose.purpose_of took only an
exact project match."""
from hub import compose, jobs


def _rows(*rules):
    return [(rid, rid, content, area, 0, "2026-11-10") for rid, area, content in rules]


def test_a_fan_project_inherits_the_template_purpose():
    rows = _rows(("purpose_u_template", "u_%", "One fan per project."))
    assert compose.purpose_of(jobs.rules_for(rows, "u_tom"), "u_tom") == "One fan per project."


def test_the_project_s_own_purpose_wins_over_the_template():
    rows = _rows(("purpose_u_template", "u_%", "One fan per project."),
                 ("purpose_u_tom", "u_tom", "Tom's own."))
    assert compose.purpose_of(jobs.rules_for(rows, "u_tom"), "u_tom") == "Tom's own."


def test_the_most_specific_template_wins():
    rows = _rows(("purpose_u_template", "u_%", "Any fan."),
                 ("purpose_u_vip_template", "u_vip_%", "A VIP fan."))
    assert compose.purpose_of(jobs.rules_for(rows, "u_vip_9"), "u_vip_9") == "A VIP fan."
    assert compose.purpose_of(jobs.rules_for(rows, "u_9"), "u_9") == "Any fan."


def test_a_project_outside_the_pattern_gets_no_purpose():
    rows = _rows(("purpose_u_template", "u_%", "One fan per project."))
    assert compose.purpose_of(jobs.rules_for(rows, "brain"), "brain") == ""
