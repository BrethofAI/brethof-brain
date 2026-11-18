"""THE APPLIER'S GUARDS (2026-09-17, read from one night's heal on 32
arrivals): a delete names the record that now carries what it said and is
refused when that record does not exist; a near-identical update is never
skipped when the batch also deletes — it is the fold's survivor."""
from core import heal, tools


def _run(monkeypatch, ops, shown, current=None, exists=None):
    log = []; deleted = []; written = []
    monkeypatch.setattr(tools, "delete_memory", lambda p, oid: deleted.append(oid) or "ok")
    monkeypatch.setattr(tools, "write_record", lambda p, oid, t, title, content: written.append((oid, content)) or "ok")
    applied, dropped = heal._apply(ops, "p", shown, False, log, current=current, exists=exists)
    return applied, dropped, log, deleted, written


def test_a_delete_naming_a_survivor_that_does_not_exist_is_refused(monkeypatch):
    ops = [{"op": "delete", "id": "old", "into": "standing_gotchas", "reason": "already stated there"}]
    a, d, log, deleted, _ = _run(monkeypatch, ops, {"old"}, exists=lambda rid: rid == "other")
    assert deleted == [] and d == 1 and any("DELETE refused" in l for l in log)
    # naming a record that exists, or one updated in the same batch, is fine
    a, d, log, deleted, _ = _run(monkeypatch, ops, {"old"}, exists=lambda rid: rid == "standing_gotchas")
    assert deleted == ["old"]
    ops2 = [{"op": "update", "id": "surv", "content": "new text", "title": "t", "type": "fact"},
            {"op": "delete", "id": "old", "into": "surv"}]
    a, d, log, deleted, written = _run(monkeypatch, ops2, {"old", "surv"}, exists=lambda rid: False)
    assert deleted == ["old"] and written[0][0] == "surv"
    # an empty into (a passed event) is still allowed and still flagged unpaired
    a, d, log, deleted, _ = _run(monkeypatch, [{"op": "delete", "id": "old", "into": ""}], {"old"}, exists=lambda rid: False)
    assert deleted == ["old"] and any("UNPAIRED" in l for l in log)


def test_a_paired_update_is_never_skipped_as_a_no_op(monkeypatch):
    cur = {"surv": "Cooldowns: records 20, rule reminder 10; description check at 50. " * 3}
    new = cur["surv"].replace("rule reminder 10", "rule reminder 20")
    ops = [{"op": "update", "id": "surv", "content": new, "title": "t", "type": "fact"}]
    # alone: near-identical → skipped, as before
    a, d, log, _, written = _run(monkeypatch, ops, {"surv"}, current=cur)
    assert written == [] and any("SKIPPED no-op" in l for l in log)
    # with a delete folding into it: applied, the 20 lands
    ops2 = ops + [{"op": "delete", "id": "old", "into": "surv"}]
    a, d, log, deleted, written = _run(monkeypatch, ops2, {"surv", "old"}, current=cur)
    assert written and "rule reminder 20" in written[0][1] and deleted == ["old"]


def test_the_pass_widths_come_from_the_environment(monkeypatch):
    import importlib
    from core import reconcile
    monkeypatch.setenv("BRAIN_HEAL_ELDER_N", "24"); monkeypatch.setenv("BRAIN_HEAL_CAP_EACH", "24"); monkeypatch.setenv("BRAIN_HEAL_BODY_EACH", "24")
    importlib.reload(reconcile)
    try:
        assert (reconcile.ELDER_N, reconcile.CAP_BATCH, reconcile.BODY_BATCH) == (24, 24, 24)
    finally:
        monkeypatch.delenv("BRAIN_HEAL_ELDER_N"); monkeypatch.delenv("BRAIN_HEAL_CAP_EACH"); monkeypatch.delenv("BRAIN_HEAL_BODY_EACH")
        importlib.reload(reconcile)
        assert (reconcile.ELDER_N, reconcile.CAP_BATCH, reconcile.BODY_BATCH) == (6, 10, 6)


def test_an_update_from_a_stale_view_is_refused(monkeypatch):
    """The study (2026-09-20): the editor must have read the current text.
    The body shown to the model was 'old text'; meanwhile the record became
    'moved on' — the rewrite is refused, the next round sees the new text."""
    monkeypatch.setattr(heal, "_current_body", lambda p, rid: "moved on")
    ops = [{"op": "update", "id": "r1", "type": "note", "title": "t", "content": "a brand new body, nothing like before"}]
    a, d, log, _, written = _run(monkeypatch, ops, {"r1"}, current={"r1": "old text"})
    assert a == 0 and d == 1 and written == [] and any("stale view" in l for l in log)
    monkeypatch.setattr(heal, "_current_body", lambda p, rid: "old text")
    a, d, log, _, written = _run(monkeypatch, ops, {"r1"}, current={"r1": "old text"})
    assert a == 1 and written and written[0][0] == "r1"


def test_an_update_with_no_title_or_no_content_is_never_written(monkeypatch):
    """2026-09-27: the records replay found 10 records blanked by updates the
    model sent without their fields (one was the EmbeddingGemma decision)."""
    for op in ({"op": "update", "id": "r", "type": "decision", "title": "", "content": ""},
               {"op": "update", "id": "r", "type": "decision", "title": "Embedder", "content": "  "},
               {"op": "update", "id": "r", "type": "decision", "reason": "keep"}):
        a, d, log, _, written = _run(monkeypatch, [op], {"r"}, current={"r": "EmbeddingGemma is the embedder."})
        assert written == [] and d == 1 and any("REFUSED empty update" in l for l in log)
    a, d, log, _, written = _run(monkeypatch, [{"op": "update", "id": "r", "type": "decision", "title": "Embedder",
                                                "content": "EmbeddingGemma-300m, our fp32 ONNX export."}], {"r"},
                                 current={"r": "EmbeddingGemma is the embedder."})
    assert written and written[0][0] == "r"


def test_the_healer_cannot_update_a_record_it_was_shown_as_a_title_only():
    """(From the old graph-hygiene set, 2026-09-04.)"""
    from unittest import mock
    log = []
    ops = [{"op": "update", "id": "brain:x_2026_09_04", "type": "note",
            "title": "Paging a human", "content": "(flagged for reading — no body was sent)",
            "reason": "needs reading"},
           {"op": "update", "id": "brain:y_2026_09_04", "type": "fact",
            "title": "Read one", "content": "a real body the model was shown and rewrote",
            "reason": "merge"}]
    with mock.patch.object(heal.tools, "write_record", return_value="Saved") as wr:
        applied, dropped = heal._apply(ops, "brain", {"x_2026_09_04", "y_2026_09_04"},
                                       False, log, readable={"y_2026_09_04"})
    assert (applied, dropped) == (1, 1)
    assert wr.call_args[0][1] == "y_2026_09_04"
    assert any("REFUSED update of a title-only record: x_2026_09_04" in l for l in log)
