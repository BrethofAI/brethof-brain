"""One heal call per save (founder 2026-09-03: "if we send them all at once,
probably it will do the same job and will cost us one third"). Dry A/B on 10
real saves / 18 records that day: per-record at cap 10 / body 6 = 2,922
input tokens per record; one call per save = 1,857 per record with the same
ops read piece by piece; against the live cap 24 / body 12 that is ~60%
less. These pin the render, which is the whole payload."""
from core import reconcile
from hub import judgetext


def _c(rid, body=None, upd="2026-09-01", why="entity"):
    return {"record_id": rid, "type": "fact", "title": f"t-{rid}",
            "content": body or f"BODY-{rid}", "upd": upd, "why": why}


def test_batch_render_shows_every_arrival_in_full_and_splits_bodies_from_titles():
    recs = [{"record_id": "a1", "type": "fact", "title": "A1", "content": "arrival one"},
            {"record_id": "a2", "type": "note", "title": "A2", "content": "arrival two"}]
    cands = [_c("c1"), _c("c2"), _c("c3")]
    head = judgetext.heal_batch("p", recs, cands, body_ids={"c1", "c2"})
    assert head.count("] ARRIVED") == 2
    assert "arrival one" in head and "arrival two" in head
    assert "THE RECORDS THAT JUST ARRIVED (2)" in head
    assert "MAY COLLIDE WITH THEM (3)" in head
    # c1, c2 carry bodies; c3 is a title only
    assert head.index("id: c1") < head.index("ALSO NEARBY") < head.index("id: c3")
    assert "BODY-c1" in head and "BODY-c2" in head and "BODY-c3" not in head


def test_batch_constants_are_the_measured_budget():
    assert reconcile.CAP_BATCH == 10 and reconcile.BODY_BATCH == 6


def test_reconcile_prompt_speaks_of_arrivals_plural():
    from core.prompts import TEXTS
    assert "ARRIVED" in TEXTS["reconcile"]
    assert "ONE record has just arrived" not in TEXTS["reconcile"]


def test_drain_reconciles_the_claimed_row_and_the_save_behind_it_in_one_call():
    """The claimed row is the lock; the pending rows behind it ride in the
    same call and are finished with it."""
    from unittest import mock
    from core import reconcile as rc
    finished, calls = [], []
    claims = [(7, "r7", 1), None]

    def fake_batch(project, ids, dry=False, **kw):
        calls.append(list(ids))
        return {"ok": True, "records": len(ids), "applied": 2, "dropped": 0, "log": []}

    with mock.patch.object(rc, "store") as st, \
         mock.patch.object(rc, "reconcile_batch", side_effect=fake_batch), \
         mock.patch("core.healqueue.reclaim"), \
         mock.patch("core.healqueue.ready", return_value=(True, 3)), \
         mock.patch("core.healqueue.depth", return_value=0), \
         mock.patch("core.healqueue.claim", side_effect=claims), \
         mock.patch("core.healqueue.peek_more", return_value=[(8, "r8"), (9, "r9")]), \
         mock.patch("core.healqueue.finish", side_effect=lambda c, q: finished.append(q)), \
         mock.patch("core.healqueue.fail") as fail:
        st.connection.return_value.__enter__.return_value = mock.MagicMock()
        r = rc.drain("t", "brain")
    assert calls == [["r7", "r8", "r9"]]
    assert finished == [7, 8, 9]
    fail.assert_not_called()
    assert r["reconciled"] == 3 and r["ops_applied"] == 2


def test_drain_failure_puts_back_only_the_leader_and_leaves_the_rest_pending():
    from unittest import mock
    from core import reconcile as rc
    finished, failed = [], []
    claims = [(7, "r7", 1), None]
    with mock.patch.object(rc, "store") as st, \
         mock.patch.object(rc, "reconcile_batch", return_value={"ok": False, "note": "no verdict (inference/parse)"}), \
         mock.patch("core.healqueue.reclaim"), \
         mock.patch("core.healqueue.ready", return_value=(True, 3)), \
         mock.patch("core.healqueue.depth", return_value=2), \
         mock.patch("core.healqueue.claim", side_effect=claims), \
         mock.patch("core.healqueue.peek_more", return_value=[(8, "r8")]), \
         mock.patch("core.healqueue.finish", side_effect=lambda c, q: finished.append(q)), \
         mock.patch("core.healqueue.fail", side_effect=lambda c, q, e: failed.append(q) or False):
        st.connection.return_value.__enter__.return_value = mock.MagicMock()
        r = rc.drain("t", "brain")
    assert finished == [] and failed == [7]        # r8 stays pending for the next leader
    assert r["failed"] == 1 and r["dropped_forever"] == 0


def test_cluster_render_shows_every_record_in_full_and_no_arrivals():
    recs = [_c("r1"), _c("r2"), {"record_id": "r3", "type": "note", "title": "T3", "content": "third"}]
    head = judgetext.heal_cluster("p", recs)
    assert "a neighbourhood (3)" in head and "none just arrived" in head
    assert "BODY-r1" in head and "BODY-r2" in head and "third" in head
    assert "ARRIVED" not in head.replace("none just arrived", "")


def test_heal_prompt_is_the_target_heal():
    """Production heal is Z4 (founder 2026-09-29, "lock Z4"): what a record
    is for, the worth and not-worth lists, the unread-title guard, never age
    alone, the twin rule, and the eyes."""
    from core.prompts import TEXTS
    t = TEXTS["reconcile"]
    flat = " ".join(t.split())
    assert "handed to every new session unasked" in flat and "WORTH A RECORD" in flat and "NOT WORTH A RECORD" in flat
    assert "A title you have not read in full is never updated or deleted" in flat
    assert "never age alone" in flat
    assert "never update it into a copy of the other" in flat           # the twin rule
    assert "ALREADY FILED" in flat                                         # the eyes
    assert "THIS PROJECT: {purpose}" in flat and "In a story: its canon" in flat
    t.format(today="2026-09-03", purpose="a novel")   # still renders


def test_heal_system_text_carries_the_purpose_or_says_there_is_none():
    assert "a novel about auditors" in judgetext.heal_system("a novel about auditors")
    assert judgetext.NO_PURPOSE in judgetext.heal_system("")
    assert "{today}" not in judgetext.heal_system("anything")


def test_the_healer_never_folds_a_different_subject_and_leaves_bloat_to_the_split():
    from core.prompts import TEXTS
    flat = " ".join(TEXTS["reconcile"].split())
    assert "never fold one thing into another thing's record" in flat
    assert "what a session would look up under one name" in flat and "under about 800 characters" in flat
