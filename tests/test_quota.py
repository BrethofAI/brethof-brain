"""Pins quota enforcement (founder build order, 2026-07-30).

The verdict function is PURE on purpose — every threshold lives in
decide(), and these tests walk the whole degrade ladder:
  exempt → fine → warn 80% → learning pauses (exchanges OR storage 100%)
  → archive refused (storage 120%). Archive-last is the sacred part.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import quota, tiers  # noqa: E402


class Caps(unittest.TestCase):
    def test_final_ladder(self):
        self.assertEqual(tiers.caps("free"),
                         {"exchanges": 500, "storage_mb": 20})
        self.assertEqual(tiers.caps("pro"),
                         {"exchanges": 8_000, "storage_mb": 2_048})
        self.assertEqual(tiers.caps("business")["exchanges"], 40_000)

    def test_legacy_codes_map_up_never_down(self):
        self.assertEqual(tiers.caps("t19_99"), tiers.caps("pro"))
        self.assertEqual(tiers.caps("t9_99"), tiers.caps("starter"))

    def test_unknown_plan_fails_closed_to_free(self):
        self.assertEqual(tiers.caps("platinum_2027"), tiers.caps("free"))
        self.assertEqual(tiers.caps(""), tiers.caps("free"))


class Verdicts(unittest.TestCase):
    def test_exempt_tenant_passes_everything(self):
        v = quota.decide(None, 10**9, 10**9)
        self.assertTrue(v["learn"] and v["archive"])
        self.assertEqual(v["warn"], "")

    def test_under_cap_is_clean(self):
        v = quota.decide("pro", 100, 50.0)
        self.assertTrue(v["learn"] and v["archive"])
        self.assertEqual(v["warn"], "")

    def test_warn_at_80_pct_but_nothing_pauses(self):
        v = quota.decide("free", 400, 1.0)          # 400/500 = 80%
        self.assertTrue(v["learn"] and v["archive"])
        self.assertIn("heads up", v["warn"])

    def test_exchanges_cap_pauses_learning_archive_lives(self):
        v = quota.decide("free", 500, 1.0)
        self.assertFalse(v["learn"])
        self.assertTrue(v["archive"])               # the sacred part
        self.assertIn("stopped learning", v["warn"])

    def test_storage_100_pct_pauses_learning_archive_lives(self):
        v = quota.decide("free", 0, 20.0)
        self.assertFalse(v["learn"])
        self.assertTrue(v["archive"])
        self.assertIn("cleanup_history", v["warn"])

    def test_storage_120_pct_refuses_archive(self):
        v = quota.decide("free", 0, 24.0)           # 120% of 20 MB
        self.assertFalse(v["learn"])
        self.assertFalse(v["archive"])
        self.assertIn("ON HOLD", v["warn"])

    def test_grace_between_100_and_120(self):
        v = quota.decide("free", 0, 23.9)
        self.assertTrue(v["archive"])               # chat never bounces here


class KillSwitch(unittest.TestCase):
    def test_enforce_off_forces_permissive(self):
        with mock.patch.dict(os.environ, {"BRAIN_ENFORCE_CAPS": "0"}), \
             mock.patch.object(quota, "effective_plan",
                               return_value="free"), \
             mock.patch.object(quota, "used_exchanges", return_value=99999), \
             mock.patch.object(quota, "storage_mb", return_value=99999.0), \
             mock.patch.object(quota.store, "current_tenant",
                               return_value="t_test"):
            v = quota.check()
        self.assertTrue(v["learn"] and v["archive"])

    def test_enforce_on_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch.object(quota, "effective_plan",
                               return_value="free"), \
             mock.patch.object(quota, "used_exchanges", return_value=99999), \
             mock.patch.object(quota, "storage_mb", return_value=1.0):
            os.environ.pop("BRAIN_ENFORCE_CAPS", None)
            v = quota.check()
        self.assertFalse(v["learn"])


class TestTopUps(unittest.TestCase):
    """Lemon Squeezy top-ups (2026-08-20): purchased pack credits extend the
    exchange cap and roll over; purchased storage widens the storage cap.
    decide() stays pure — the ledger math is pack_ledger()'s problem."""

    def test_pack_credits_keep_learning_past_the_plan_cap(self):
        v = quota.decide("free", 500, 1.0, pack_avail=5000)
        self.assertTrue(v["learn"] and v["archive"])
        self.assertIn("top-up credits", v["warn"])

    def test_exhausted_credits_pause_learning(self):
        v = quota.decide("free", 5500, 1.0, pack_avail=5000)
        self.assertFalse(v["learn"])
        self.assertTrue(v["archive"], "archive must survive the pause")

    def test_extra_storage_widens_the_cap(self):
        # 20 MB free cap + 1024 MB purchased: 500 MB is comfortably inside.
        v = quota.decide("free", 0, 500.0, extra_mb=1024)
        self.assertTrue(v["learn"] and v["archive"])

    def test_negative_entitlements_never_shrink_caps(self):
        clean = quota.decide("free", 400, 1.0)
        dirty = quota.decide("free", 400, 1.0, pack_avail=-100, extra_mb=-100)
        self.assertEqual(clean, dirty)

    def test_check_uses_key_entitlements_over_the_row(self):
        with mock.patch.object(quota, "effective_plan",
                               return_value="free"), \
             mock.patch.object(quota, "used_exchanges", return_value=600), \
             mock.patch.object(quota, "storage_mb", return_value=1.0), \
             mock.patch.object(quota, "pack_ledger",
                               return_value=5000) as ledger, \
             mock.patch.object(quota, "stored_entitlements") as row:
            v = quota.check("free", ents={"pack_credits": 5000,
                                          "extra_storage_mb": 0})
        row.assert_not_called()
        ledger.assert_called_once_with(5000, 500)
        self.assertTrue(v["learn"])
        self.assertIn("top-up credits", v["warn"])

    def test_check_falls_back_to_the_stored_row(self):
        with mock.patch.object(quota, "effective_plan",
                               return_value="free"), \
             mock.patch.object(quota, "used_exchanges", return_value=600), \
             mock.patch.object(quota, "storage_mb", return_value=1.0), \
             mock.patch.object(quota, "pack_ledger", return_value=5000), \
             mock.patch.object(quota, "stored_entitlements",
                               return_value=(5000, 0)):
            v = quota.check("free")
        self.assertTrue(v["learn"])


class Snapshot(unittest.TestCase):
    """The panel's LIVE view (founder 2026-08-24: "we are the only cloud
    provider which is doing statistics, not live"). snapshot() must show the
    exact numbers the door enforces on, work for enforcement-exempt tenants
    when the control plane supplies the plan, and never count or wall."""

    def _patch(self, used=100, mb=5.0, pack_avail=0, pack_sum=0):
        row = mock.Mock()
        row.fetchone.return_value = [pack_sum]
        conn = mock.MagicMock()
        conn.__enter__.return_value.cursor.return_value = row
        return [
            mock.patch.object(quota, "used_exchanges", return_value=used),
            mock.patch.object(quota, "storage_mb", return_value=mb),
            mock.patch.object(quota, "pack_ledger",
                              return_value=pack_avail),
            mock.patch.object(quota.store, "connection",
                              return_value=conn),
        ]

    def test_control_plane_plan_beats_the_missing_row(self):
        # The founder's own tenant: static key, no tenant_plan row — the
        # panel still gets team caps because the control plane says team.
        patches = self._patch(used=6490, mb=1512.0)
        with patches[0], patches[1], patches[2], patches[3], \
                mock.patch.object(quota, "effective_plan") as eff:
            s = quota.snapshot(plan="team", pack_credits=0, extra_mb=0)
        eff.assert_not_called()
        self.assertEqual(s["plan"], "team")
        self.assertEqual(s["exchanges"]["used"], 6490)
        self.assertEqual(s["exchanges"]["cap"], 20_000)
        self.assertAlmostEqual(s["exchanges"]["pct"], 32.5, places=1)
        self.assertEqual(s["storage"]["cap_mb"], 5_120)
        self.assertEqual(s["state"], "active")
        self.assertIsNone(s["pack"])

    def test_planless_everywhere_returns_uncapped_view(self):
        patches = self._patch(used=42, mb=3.0)
        with patches[0], patches[1], patches[2], patches[3], \
                mock.patch.object(quota, "effective_plan",
                                  return_value=None):
            s = quota.snapshot()
        self.assertIsNone(s["plan"])
        self.assertEqual(s["exchanges"], {"used": 42, "cap": None})
        self.assertEqual(s["state"], "active")

    def test_pack_arithmetic_is_owned_used_remaining(self):
        # 5000 purchased, 1200 consumed across all ledgered periods.
        patches = self._patch(used=600, mb=1.0, pack_avail=3800,
                              pack_sum=1200)
        with patches[0], patches[1], patches[2], patches[3]:
            s = quota.snapshot(plan="free", pack_credits=5000, extra_mb=0)
        self.assertEqual(s["pack"], {"purchased": 5000, "used": 1200,
                                     "remaining": 3800})
        self.assertEqual(s["exchanges"]["effective_cap"], 500 + 3800)
        # 600 used on a 500 plan cap: running on credits, door still open.
        self.assertEqual(s["state"], "active")

    def test_states_match_the_door(self):
        patches = self._patch(used=999_999, mb=1.0)
        with patches[0], patches[1], patches[2], patches[3]:
            s = quota.snapshot(plan="free", pack_credits=0, extra_mb=0)
        self.assertEqual(s["state"], "exhausted")
        patches = self._patch(used=0, mb=99_999.0)
        with patches[0], patches[1], patches[2], patches[3]:
            s = quota.snapshot(plan="free", pack_credits=0, extra_mb=0)
        self.assertEqual(s["state"], "storage_full")

    def test_view_never_counts_and_never_walls(self):
        patches = self._patch(used=999_999, mb=1.0)
        with patches[0], patches[1], patches[2], patches[3], \
                mock.patch.object(quota, "bump_exchanges") as bump, \
                mock.patch.object(quota, "_wall_push_once") as wall:
            quota.snapshot(plan="free", pack_credits=0, extra_mb=0)
        bump.assert_not_called()
        wall.assert_not_called()


if __name__ == "__main__":
    unittest.main()


def test_no_raw_ddl_on_hot_paths():
    """The DDL-deadlock class (2026-08-11, turn_job): CREATE INDEX IF NOT
    EXISTS takes ShareLock held to commit — any hot path re-running its DDL
    per call deadlocks under concurrent writers. quota.py shipped six raw
    `cur.execute(_DDL)` sites on 2026-08-20 (after the fleet sweep) and one
    deadlocked bump_exchanges under real gate load on 2026-08-22. Every DDL
    on a callable path goes through store.ensure_ddl — this pin keeps the
    class extinct in the modules that regressed."""
    import os
    root = os.path.join(os.path.dirname(__file__), "..")
    for mod in ("core/quota.py", "core/metering.py"):
        src = open(os.path.join(root, mod), encoding="utf-8").read()
        assert "cur.execute(_DDL)" not in src, f"raw DDL execution in {mod}"


class Door(unittest.TestCase):
    """EACH METER CUTS ONLY WHAT IT FUNDS (founder 2026-08-25, replacing
    the full-or-off of 2026-08-24). The allowance funds inference, so it
    stops LEARNING. Storage funds disk, so it stops RECORDING. Neither
    stops RECALL — that is mechanical, runs on our own CPU, and is the
    customer's own memory: "that's the candy which I would give them."
    The only thing that ever ends recall is the database leaving the box
    on the retention ladder, which is lifecycle, not the door."""

    def _door(self, learn, arch, warn="w"):
        from unittest import mock
        with mock.patch.object(quota, "check",
                               return_value={"learn": learn, "archive": arch,
                                             "warn": warn, "plan": "free"}):
            return quota.door("free")

    def test_state_mapping(self):
        self.assertEqual(self._door(True, True)[0], "active")
        self.assertEqual(self._door(False, True)[0], "exhausted")
        self.assertEqual(self._door(True, False)[0], "storage_full")
        self.assertEqual(self._door(False, False)[0], "off")
        # the closed door carries sales copy, not a bare code
        self.assertIn("brethof.ai/account", self._door(False, True)[1])

    def test_every_wall_discloses_the_permanent_gap(self):
        """The gap warning is DISCLOSURE, not a scare line: we never learn
        from old conversation, so the customer inherits the repair. Saying
        so is the honest thing AND the strongest reason to stay in plan —
        if it ever goes missing, both halves are lost."""
        for learn, arch in ((False, True), (True, False), (False, False)):
            notice = self._door(learn, arch)[1].lower()
            self.assertIn("permanent", notice)
            self.assertIn("recall still works", notice)

    def test_no_wall_tells_them_to_free_space(self):
        """Deletion is deliberately gated behind confirmation, login and
        2FA. Pointing someone already at a wall into that is not help."""
        for learn, arch in ((False, True), (True, False), (False, False)):
            self.assertNotIn("free space", self._door(learn, arch)[1].lower())

    def test_the_three_predicates(self):
        self.assertTrue(all(quota.may_recall(s) for s in
                            ("active", "exhausted", "storage_full", "off",
                             "frozen")))
        self.assertTrue(quota.may_record("exhausted"))
        self.assertFalse(quota.may_record("storage_full"))
        self.assertFalse(quota.may_record("off"))
        self.assertFalse(quota.may_record("frozen"))
        self.assertTrue(quota.may_learn("active"))
        self.assertFalse(any(quota.may_learn(s) for s in
                             ("exhausted", "storage_full", "off", "frozen")))

    def _stop(self, state):
        from unittest import mock
        from api import main as api_main
        from core import hubfeed
        with mock.patch.object(api_main.quota, "door",
                               return_value=(state, "PAY-ME")), \
             mock.patch.object(api_main.quota, "note_plan"), \
             mock.patch.object(api_main.quota, "bump_exchanges"), \
             mock.patch.object(api_main.store, "bind", return_value=object()), \
             mock.patch.object(api_main.store, "unbind"), \
             mock.patch.object(hubfeed, "active", return_value=True), \
             mock.patch.object(api_main.archive, "archive_turns",
                               return_value=1) as arch, \
             mock.patch.object(hubfeed, "feed") as enq:
            out = api_main.stop({"project": "p", "session_id": "s",
                                 "turns": [{"index": 0, "text": "x"}]},
                                tenant="t1", plan="free", ents=None)
        return out, arch, enq

    def test_out_of_allowance_still_records_but_never_learns(self):
        """Writing a turn into disk they already bought costs no tokens;
        DISTILLING it costs the allowance they have spent. And because we
        never learn from old conversation afterwards, this recording is
        the only access that period will ever have."""
        out, arch, enq = self._stop("exhausted")
        arch.assert_called_once()
        enq.assert_not_called()
        self.assertNotEqual(out.get("status"), "over_cap")

    def test_full_storage_records_nothing(self):
        out, arch, enq = self._stop("storage_full")
        arch.assert_not_called()
        enq.assert_not_called()
        self.assertEqual(out.get("status"), "over_cap")

    def test_unpaid_records_nothing(self):
        """Not paid, not working — the billing line is not a limit."""
        out, arch, enq = self._stop("frozen")
        arch.assert_not_called()
        self.assertEqual(out.get("status"), "over_cap")

    def test_session_start_serves_the_brief_AND_the_wall(self):
        """The brief IS recall. A limited account still gets its memory —
        with the notice in front of it, not instead of it."""
        from unittest import mock
        from api import main as api_main
        with mock.patch.object(api_main.quota, "door",
                               return_value=("exhausted", "PAY-ME")), \
             mock.patch.object(api_main.store, "bind", return_value=object()), \
             mock.patch.object(api_main.store, "unbind"), \
             mock.patch.object(api_main.injection, "session_blob",
                               return_value="THE-MEMORY") as blob:
            out = api_main.session_start({"project": "p"}, tenant="t1",
                                         plan="free", tier="customer")
        blob.assert_called_once()
        self.assertNotEqual(out.get("status"), "over_cap")
        self.assertIn("THE-MEMORY", out.get("injection"))
        self.assertIn("PAY-ME", out.get("injection"))

    def test_the_notice_survives_an_empty_brief(self):
        """An account with no memory yet must still hear the wall, or it
        learns about it from the invoice."""
        from unittest import mock
        from api import main as api_main
        with mock.patch.object(api_main.quota, "door",
                               return_value=("exhausted", "PAY-ME")), \
             mock.patch.object(api_main.store, "bind", return_value=object()), \
             mock.patch.object(api_main.store, "unbind"), \
             mock.patch.object(api_main.injection, "session_blob",
                               return_value=""):
            out = api_main.session_start({"project": "p"}, tenant="t1",
                                         plan="free", tier="customer")
        self.assertIn("PAY-ME", out.get("injection"))

    def test_recall_answers_through_every_wall(self):
        """THE CANDY. A limit never takes a customer's own memory away —
        not the allowance, not storage, not even non-payment while the
        data is still with us. Only the database leaving the box does."""
        from unittest import mock
        from api import main as api_main
        for state in ("exhausted", "storage_full", "off", "frozen"):
            with mock.patch.object(api_main.quota, "door",
                                   return_value=(state, "PAY-ME")), \
                 mock.patch.object(api_main.store, "bind",
                                   return_value=object()), \
                 mock.patch.object(api_main.store, "unbind"), \
                 mock.patch.object(api_main.metering, "count_tool"), \
                 mock.patch.object(api_main.injection, "ambient_blob",
                                   return_value="THE-MEMORY") as blob:
                out = api_main.prompt_submit(
                    {"project": "p", "prompt": "hello there world",
                     "session_id": "s"}, tenant="t1", plan="free")
            blob.assert_called_once()
            self.assertIn("THE-MEMORY", out.get("injection"), state)

    def test_session_start_briefs_through_every_wall(self):
        """The session brief is gated by exactly the same rule as ambient
        recall — nothing. Asserted separately because a page promising
        "still briefed at session start" needs its own proof."""
        from unittest import mock
        from api import main as api_main
        for state in ("exhausted", "storage_full", "off", "frozen"):
            with mock.patch.object(api_main.quota, "door",
                                   return_value=(state, "PAY-ME")), \
                 mock.patch.object(api_main.store, "bind",
                                   return_value=object()), \
                 mock.patch.object(api_main.store, "unbind"), \
                 mock.patch.object(api_main.injection, "session_blob",
                                   return_value="THE-MEMORY") as blob:
                out = api_main.session_start({"project": "p"}, tenant="t1",
                                             plan="free", tier="customer")
            blob.assert_called_once()
            self.assertIn("THE-MEMORY", out.get("injection"), state)

    def test_the_horizon_names_a_DATE_and_the_right_clock(self):
        """A limited account keeps its recall, so it must be told what that
        recall no longer knows. Two clocks stop in two states: storage-full
        stops RECORDING (nothing exists after the last turn), allowance
        spent stops LEARNING (raw history is current, distilled memory is
        not). 'You are behind' is a shrug; a date is actionable."""
        from unittest import mock
        with mock.patch.object(quota, "_newest", return_value="2026-08-12"), \
             mock.patch.object(quota.store, "current_tenant",
                               return_value="t1"):
            quota._horizon_cache.clear()
            spent = quota.horizon_sentence("exhausted")
            quota._horizon_cache.clear()
            full = quota.horizon_sentence("storage_full")
        self.assertIn("2026-08-12", spent)
        self.assertIn("learned", spent)      # learning stopped, not recording
        self.assertIn("2026-08-12", full)
        self.assertIn("recorded", full)      # recording stopped
        self.assertNotEqual(spent, full, "both states claim the same thing")

    def test_an_active_account_gets_no_horizon(self):
        self.assertEqual(quota.horizon("active"), "")
        self.assertEqual(quota.horizon_sentence("active"), "")

    def test_the_horizon_never_costs_the_customer_their_recall(self):
        """If the date cannot be computed, the line degrades — it never
        raises into the request path."""
        from unittest import mock
        with mock.patch.object(quota, "_newest",
                               side_effect=RuntimeError("db down")), \
             mock.patch.object(quota.store, "current_tenant",
                               return_value="t1"):
            quota._horizon_cache.clear()
            self.assertEqual(quota.horizon("exhausted"), "")
            quota._horizon_cache.clear()
            self.assertTrue(quota.prompt_line("exhausted"))   # still a line

    def test_every_prompt_carries_one_short_line(self):
        """The gap must be visible NOW, not discovered in three weeks —
        and it rides in the CUSTOMER'S context window, so one line."""
        from unittest import mock
        from api import main as api_main
        with mock.patch.object(api_main.quota, "door",
                               return_value=("exhausted", "PAY-ME")), \
             mock.patch.object(api_main.store, "bind", return_value=object()), \
             mock.patch.object(api_main.store, "unbind"), \
             mock.patch.object(api_main.metering, "count_tool"), \
             mock.patch.object(api_main.injection, "ambient_blob",
                               return_value="THE-MEMORY"):
            out = api_main.prompt_submit(
                {"project": "p", "prompt": "hello there world",
                 "session_id": "s"}, tenant="t1", plan="free")
        line = out["injection"].split("\n")[0]
        self.assertIn("brethof.ai/account", line)
        self.assertLess(len(line), 200, "the per-prompt line is taxing the "
                        "customer's own context window")

    def test_door_fails_open(self):
        """A broken door must never lock out a healthy paying tenant."""
        from unittest import mock
        from api import main as api_main
        from core import hubfeed
        with mock.patch.object(api_main.quota, "door",
                               side_effect=RuntimeError("db down")), \
             mock.patch.object(api_main.quota, "note_plan"), \
             mock.patch.object(api_main.quota, "bump_exchanges"), \
             mock.patch.object(api_main.store, "bind", return_value=object()), \
             mock.patch.object(api_main.store, "unbind"), \
             mock.patch.object(api_main.archive, "archive_turns",
                               return_value=1), \
             mock.patch.object(hubfeed, "active", return_value=False):
            out = api_main.stop({"project": "p", "session_id": "s",
                                 "turns": [{"index": 0, "text": "x"}]},
                                tenant="t1", plan="free", ents=None)
        self.assertEqual(out.get("status", "ok"), "ok")


class InstantWall(unittest.TestCase):
    """The first refusal of a period fires the tenant's gauges to the
    control plane NOW (founder 2026-08-24: the suspended email must be
    instant, not on the 6-hour sweep). Claimed atomically, once."""

    def _door_closed(self, claimed_rowcount):
        from unittest import mock
        conn = mock.MagicMock()
        cur = conn.cursor.return_value
        cur.rowcount = claimed_rowcount
        ctx = mock.MagicMock()
        ctx.__enter__.return_value = conn
        with mock.patch.object(quota, "check",
                               return_value={"learn": False, "archive": True,
                                             "warn": "w", "plan": "free"}), \
             mock.patch.object(quota.store, "connection", return_value=ctx), \
             mock.patch.object(quota.store, "ensure_ddl"), \
             mock.patch.object(quota.store, "current_tenant",
                               return_value="t_wall"), \
             mock.patch("core.metering.flush_tenant_async") as fire:
            state, _ = quota.door("free")
        return state, fire

    def test_first_refusal_fires_exactly_once(self):
        state, fire = self._door_closed(claimed_rowcount=1)
        self.assertEqual(state, "exhausted")
        fire.assert_called_once_with("t_wall")

    def test_already_claimed_period_stays_silent(self):
        state, fire = self._door_closed(claimed_rowcount=0)
        self.assertEqual(state, "exhausted")
        fire.assert_not_called()

    def test_active_never_touches_the_claim(self):
        from unittest import mock
        with mock.patch.object(quota, "check",
                               return_value={"learn": True, "archive": True,
                                             "warn": "", "plan": "free"}), \
             mock.patch.object(quota, "_wall_push_once") as push:
            state, _ = quota.door("free")
        self.assertEqual(state, "active")
        push.assert_not_called()

    def test_push_failure_never_breaks_the_refusal(self):
        from unittest import mock
        with mock.patch.object(quota, "check",
                               return_value={"learn": False, "archive": True,
                                             "warn": "w", "plan": "free"}), \
             mock.patch.object(quota, "_wall_push_once",
                               side_effect=RuntimeError("control down")):
            state, notice = quota.door("free")
        self.assertEqual(state, "exhausted")
        self.assertIn("brethof.ai/account", notice)


class BillingStates(unittest.TestCase):
    """THE BILLING LINE (founder, final 2026-08-24): "Paid works. Not paid
    stopped. No cadency, no cooldown period, nothing." Subscription state
    is read BEFORE the meters, and there is no grace branch to test —
    that is the point."""

    def test_unpaid_states_close_without_touching_the_meters(self):
        for st in ("frozen", "past_due", "unpaid", "expired", "cancelled"):
            with mock.patch.object(quota, "check") as chk:
                state, notice = quota.door("team", state=st)
            chk.assert_not_called()      # no meter read, no wall push
            self.assertEqual(state, "frozen", st)
            self.assertIn("not active", notice)
            self.assertIn("intact", notice)
            self.assertIn("export or delete", notice)

    def test_no_grace_branch_exists(self):
        """A cadence window is a bill nobody wants — pinned dead."""
        import inspect
        src = inspect.getsource(quota)
        self.assertNotIn("DUNNING", src)
        self.assertNotIn("keeps working for", src)

    def test_active_and_unknown_states_fail_open(self):
        for st in (None, "", "active", "something_new"):
            with mock.patch.object(quota, "check",
                                   return_value={"learn": True,
                                                 "archive": True,
                                                 "warn": "", "plan": "free"}):
                self.assertEqual(quota.door("free", state=st)[0], "active")


def test_session_start_greets_with_the_doors_notice():
    """The dunning warning must REACH the customer (2026-08-24 live-fire:
    session-start re-ran check() and dropped the door's billing half).
    The door is the one source of the greeting notice."""
    import inspect
    from api import main
    src = inspect.getsource(main.session_start)
    # Pinned by INTENT, not by spelling: the greeting is built FROM the
    # door's notice (2026-08-25 it also carries the horizon sentence, so
    # the exact f-string moved). What must never come back is a second
    # computation of the warning.
    assert "head = f\"{notice}" in src or 'f"⚠ {notice}' in src, (
        "the greeting must be built from the DOOR's notice")
    assert "quota.horizon_sentence" in src, (
        "a limited account keeps its recall, so the greeting must also say "
        "what that recall no longer knows")
    assert "v = quota.check(plan)" not in src, (
        "session-start must not recompute the warning — that path dropped "
        "the billing state")
