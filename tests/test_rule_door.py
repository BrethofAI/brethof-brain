"""LAW HAS ONE DOOR — save_rule. The knowledge tools must refuse to
legislate, because an agent that can legislate by naming a project will
(fourteen Krea2 workflow configs became injected law that way)."""
from core import tools


def test_write_record_refuses_to_write_law():
    out = tools.write_record("rules", "some_rule", "rule", "T", "body")
    assert out.startswith("Error")
    assert "save_rule" in out
    assert "THE TEST" in out


def test_save_record_refuses_to_write_law():
    out = tools.save_record("rules", "some_rule", '{"type":"rule"}')
    assert out.startswith("Error") and "save_rule" in out


def test_memory_index_is_not_a_rule():
    # the rules table's own router is bookkeeping, not law — it must still
    # be writable by the normal path (heal/gate refresh it). No DB here, so
    # getting PAST the guard means hitting the store; that IS the proof.
    try:
        out = tools.save_record("rules", "memory_index", '{"type":"note"}')
    except Exception:
        return                      # reached the DB => guard let it through
    assert "save_rule" not in out


def test_save_rule_validates_inputs():
    assert tools.save_rule(rule_id="", title="t", rule="b").startswith("Error")
    assert tools.save_rule(rule_id="bad id!", title="t",
                           rule="b").startswith("Error")
    assert tools.save_rule(rule_id="r", title="", rule="b").startswith("Error")
    assert tools.save_rule(rule_id="r", title="t", rule="").startswith("Error")


def test_save_rule_is_registered_as_a_tool():
    assert tools.TOOLS.get("save_rule") is tools.save_rule
