"""aggression: ContactWatcher (contact + aggressor), RivalBook (grudge -> feud -> escalation, wreck -> retaliation,
caution settles, governor, list, teammates, player) and the engine racing rivals."""
import os
import sys
import unittest
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aimod import aggression as ag                       # noqa: E402
from aimod.behavior import Car, Ctx                      # noqa: E402

LT = 30.0


def snap_car(idx, number, x, y, damage=0.0, vx=50.0, vy=0.0, pit=False):
    return NS(idx=idx, number=number, valid=True, pos=(x, y), vel=(vx, vy), damage=damage, reason_out=0,
              on_pit_road=pit)


def contact(t, na, nb, da, db, aggressor_num=None, a=2, b=3):
    agg = a if aggressor_num == na else b if aggressor_num == nb else None
    return ag.Contact(t, a, b, na, nb, da, db, agg, 2.0)


HOT, CALM = 0.8, 0.1


class Watcher(unittest.TestCase):
    def test_rear_hit_names_the_car_behind(self):
        w = ag.ContactWatcher()
        w.update(0.0, [snap_car(2, 24, 0, 0), snap_car(3, 48, 3, 0)], True)
        out = w.update(0.2, [snap_car(2, 24, 0, 0, 0.02), snap_car(3, 48, 3, 0, 0.06)], True)
        self.assertEqual(len(out), 1)
        c = out[0]
        self.assertEqual({c.na, c.nb}, {24, 48})
        self.assertEqual(c.aggressor, 2)                        # #24 ran into the back of #48

    def test_side_contact_less_damage_did_it(self):
        w = ag.ContactWatcher()
        w.update(0.0, [snap_car(2, 24, 0, 0), snap_car(3, 48, 0, 2.0)], True)
        out = w.update(0.2, [snap_car(2, 24, 0, 0, 0.05), snap_car(3, 48, 0, 2.0, 0.01)], True)
        self.assertEqual(out[0].aggressor, 3)

    def test_even_side_contact_unknown(self):
        w = ag.ContactWatcher()
        w.update(0.0, [snap_car(2, 24, 0, 0), snap_car(3, 48, 0, 2.0)], True)
        out = w.update(0.2, [snap_car(2, 24, 0, 0, 0.03), snap_car(3, 48, 0, 2.0, 0.03)], True)
        self.assertIsNone(out[0].aggressor)

    def test_wall_caution_repair_are_not_contact(self):
        w = ag.ContactWatcher()
        w.update(0.0, [snap_car(2, 24, 0, 0), snap_car(3, 48, 50, 0)], True)
        self.assertEqual(w.update(0.2, [snap_car(2, 24, 0, 0, 0.3), snap_car(3, 48, 50, 0)], True), [])  # the wall
        self.assertEqual(w.update(0.4, [snap_car(2, 24, 0, 0, 0.4), snap_car(3, 48, 2, 0, 0.1)], False), [])
        w.update(0.6, [snap_car(2, 24, 0, 0, 0.0, pit=True), snap_car(3, 48, 2, 0, 0.1)], True)      # repaired
        self.assertEqual(w.update(0.8, [snap_car(2, 24, 0, 0, 0.0), snap_car(3, 48, 2, 0, 0.1)], True), [])

    def test_tiny_rise_ignored(self):
        w = ag.ContactWatcher()
        w.update(0.0, [snap_car(2, 24, 0, 0), snap_car(3, 48, 2, 0)], True)
        self.assertEqual(w.update(0.2, [snap_car(2, 24, 0, 0, 0.0005), snap_car(3, 48, 2, 0)], True), [])


class Book(unittest.TestCase):
    def setUp(self):
        self.b = ag.RivalBook(seed=7)
        self.temper = {24: HOT, 48: HOT, 3: CALM}

    def test_one_runin_victim_holds_a_grudge(self):
        ev = self.b.runin(contact(10, 24, 48, 0.0, 0.05, aggressor_num=24), 5, self.temper)
        kinds = [e.kind for e in ev]
        self.assertEqual(kinds, ["runin", "grudge"])
        self.assertEqual(ev[1].holder, 48)
        self.assertEqual(self.b.relation(48, 24), (ag.GRUDGE, ag.RIVAL_ADDS[1]))
        self.assertIsNone(self.b.relation(24, 48))              # the aggressor holds nothing

    def test_calm_driver_lets_it_go(self):
        ev = self.b.runin(contact(10, 24, 3, 0.0, 0.05, aggressor_num=24), 5, self.temper)
        self.assertEqual([e.kind for e in ev], ["runin"])
        self.assertIsNone(self.b.relation(3, 24))

    def test_same_runin_merges_separate_ones_escalate(self):
        self.b.runin(contact(10, 24, 48, 0.0, 0.05, 24), 5, self.temper)
        self.b.runin(contact(14, 24, 48, 0.01, 0.02, 24), 5, self.temper)          # same run-in
        f = self.b.feuds[(24, 48)]
        self.assertEqual(f.runins, 1)
        ev = self.b.runin(contact(100, 24, 48, 0.0, 0.03, 24), 9, self.temper)
        self.assertIn("feud", [e.kind for e in ev])
        self.assertEqual(self.b.relation(24, 48), (ag.FEUD, ag.RIVAL_ADDS[2]))     # both ways now
        self.assertEqual(self.b.relation(48, 24), (ag.FEUD, ag.RIVAL_ADDS[2]))
        self.b.runin(contact(200, 24, 48, 0.03, 0.0, 48), 14, self.temper)
        self.b.runin(contact(300, 24, 48, 0.03, 0.0, 48), 19, self.temper)
        self.assertEqual(f.level, ag.MAX_LEVEL)                  # to a point
        self.assertEqual(self.b.relation(48, 24)[1], ag.RIVAL_ADDS[3])

    def test_repeat_victim(self):
        self.b.runin(contact(10, 24, 48, 0.0, 0.05, 24), 5, self.temper)
        self.assertFalse(self.b.repeat_victim(48))
        self.b.runin(contact(100, 24, 48, 0.0, 0.05, 24), 9, self.temper)
        self.assertTrue(self.b.repeat_victim(48))
        self.assertFalse(self.b.repeat_victim(24))

    def test_wreck_beyond_repair_retaliates(self):
        ev = self.b.runin(contact(10, 24, 48, 0.0, 0.7, 24), 5, self.temper)
        self.assertIn("wrecked", [e.kind for e in ev])
        self.assertEqual(self.b.update(5, {48: (0.7, False)}), [])          # on pit road
        ev = self.b.update(6, {48: (0.45, True)})                            # repaired, 0.45 left
        self.assertEqual([e.kind for e in ev], [ag.RETALIATE])
        self.assertEqual(self.b.relation(48, 24), (ag.RETALIATE, ag.RETALIATE_ADD))
        self.assertIsNone(self.b.relation(24, 48))                           # only the wrecked car retaliates

    def test_fully_repaired_no_retaliation(self):
        self.b.runin(contact(10, 24, 48, 0.0, 0.7, 24), 5, self.temper)
        self.b.update(5, {48: (0.7, True)})
        ev = self.b.update(6, {48: (0.1, True)})                             # the crew fixed it
        self.assertEqual(ev, [])
        self.assertEqual(self.b.relation(48, 24)[0], ag.GRUDGE)
        self.assertIsNone(self.b.feuds[(24, 48)].pending)

    def test_wrecked_and_stays_out_retaliates_after_two_laps(self):
        self.b.runin(contact(10, 24, 48, 0.0, 0.7, 24), 5, self.temper)
        self.assertEqual(self.b.update(5, {48: (0.7, True)}), [])
        self.assertEqual(self.b.update(6, {48: (0.7, True)}), [])
        ev = self.b.update(7, {48: (0.7, True)})
        self.assertEqual([e.kind for e in ev], [ag.RETALIATE])

    def test_caution_settles_the_pair(self):
        self.b.runin(contact(10, 24, 48, 0.0, 0.05, 24), 5, self.temper)
        self.b.runin(contact(100, 24, 48, 0.0, 0.05, 24), 9, self.temper)
        ev = self.b.caution(104.0, 9)
        self.assertEqual([e.kind for e in ev], ["settled"])
        self.assertIsNone(self.b.relation(24, 48))
        self.assertIsNone(self.b.relation(48, 24))
        self.assertFalse(self.b.repeat_victim(48))
        # a caution nobody caused settles nothing
        self.b.runin(contact(200, 24, 48, 0.0, 0.05, 24), 15, self.temper)
        self.assertEqual(self.b.caution(260.0, 16), [])
        self.assertIsNotNone(self.b.relation(48, 24))

    def test_governor(self):
        for k in range(ag.GOVERNOR_CAUTIONS):
            t = 1000.0 * (k + 1)
            self.b.runin(contact(t, 24, 48, 0.0, 0.05, 24), 5, self.temper)
            ev = self.b.caution(t + 2, 5)
        self.assertTrue(self.b.tripped)
        self.assertIn("governor", [e.kind for e in ev])

    def test_list_pairs(self):
        self.b.set_pairs([(3, 24)])
        self.assertEqual(self.b.relation(3, 24), (ag.LISTED, ag.RIVAL_ADDS[1]))
        self.assertEqual(self.b.relation(24, 3), (ag.LISTED, ag.RIVAL_ADDS[1]))
        self.b.runin(contact(10, 3, 24, 0.0, 0.05, 3), 5, self.temper)
        self.assertEqual(self.b.relation(3, 24), (ag.FEUD, ag.RIVAL_ADDS[2]))      # listed + 1 run-in = level 2
        self.b.caution(12, 5)
        self.assertEqual(self.b.relation(3, 24), (ag.LISTED, ag.RIVAL_ADDS[1]))    # settled back to the list
        self.assertTrue(self.b.goes(3, 24, 1, CALM, 0.7))                          # no dice for list rivals

    def test_teammates_option(self):
        self.b.teams = {24: 5, 48: 5}
        self.b.teammates = False
        self.assertEqual(self.b.runin(contact(10, 24, 48, 0.0, 0.05, 24), 5, self.temper), [])
        self.b.teammates = True
        self.assertNotEqual(self.b.runin(contact(20, 24, 48, 0.0, 0.05, 24), 5, self.temper), [])

    def test_player_only_when_opted_in(self):
        c = ag.Contact(10, 0, 3, 98, 48, 0.05, 0.0, 0, 2.0)        # the player (slot 0) hit #48
        self.assertEqual(self.b.runin(c, 5, self.temper), [])
        self.b.player = 98
        self.assertNotEqual(self.b.runin(c, 5, self.temper), [])
        self.assertEqual(self.b.relation(48, 98)[0], ag.GRUDGE)

    def test_grudge_dice_seeded(self):
        self.b.runin(contact(10, 24, 48, 0.0, 0.05, 24), 5, self.temper)
        a = [self.b.goes(48, 24, n, HOT, 0.7) for n in range(1, 30)]
        b2 = ag.RivalBook(seed=7)
        b2.runin(contact(10, 24, 48, 0.0, 0.05, 24), 5, self.temper)
        self.assertEqual(a, [b2.goes(48, 24, n, HOT, 0.7) for n in range(1, 30)])
        self.assertTrue(any(a) and not all(a))


def ctx(lap=50, total=100, drafting=False):
    return Ctx(race=True, green=True, lap_now=lap, total=total, to_go=total - lap, lap_time=LT, drafting=drafting)


def car(idx, number, gap_s=0.0, lap=50, laps_down=0, damage=0.0, lead=50.5):
    return Car(idx, number, f"Driver {number}", lap=lap, laps_down=laps_down, progress=lead - gap_s / LT,
               damage=damage)


OWN = {i: 1.1 for i in range(2, 44)}


def eng(strength=70):
    return ag.IntensityEngine(ag.IntensityConfig(ag.ON, strength))


class EngineRivals(unittest.TestCase):
    def feud_book(self, runins=2):
        b = ag.RivalBook(seed=3)
        for k in range(runins):
            b.runin(contact(100.0 * (k + 1), 24, 48, 0.0, 0.05, 24), 5, {24: HOT, 48: HOT})
        return b

    def test_feud_pair_near_each_other(self):
        b = self.feud_book(2)
        cars = [car(2, 24), car(3, 48, 0.6), car(4, 9, 20.0)]
        r = eng().tick(ctx(), cars, OWN, b)
        for idx, other in ((2, 48), (3, 24)):
            self.assertIn(f"feud:#{other} L2", r.reasons[idx])
            self.assertGreaterEqual(r.targets[idx], 1.1 + ag.RIVAL_ADDS[2] * 0.7 - 1e-9)
        self.assertNotIn("feud", r.reasons.get(4, ""))

    def test_escalation_raises_the_value(self):
        cars = [car(2, 24), car(3, 48, 0.6)]
        lo = eng().tick(ctx(), cars, OWN, self.feud_book(2)).targets[3]
        hi = eng().tick(ctx(), cars, OWN, self.feud_book(3)).targets[3]
        self.assertGreater(hi, lo)
        self.assertLessEqual(hi, ag.RIVAL_CAP)

    def test_far_apart_no_rival(self):
        r = eng().tick(ctx(), [car(2, 24), car(3, 48, 5.0)], OWN, self.feud_book(2))
        self.assertNotIn("feud", r.reasons.get(3, ""))

    def test_any_lap_counts_on_track(self):
        r = eng().tick(ctx(), [car(2, 24), car(3, 48, LT + 0.5, laps_down=1)], OWN, self.feud_book(2))
        self.assertIn("feud:#24", r.reasons[3])

    def test_retaliation_beyond_the_rival_cap_and_damage(self):
        b = ag.RivalBook(seed=3)
        b.runin(contact(10, 24, 48, 0.0, 0.9, 24), 5, {24: HOT, 48: HOT})
        b.update(5, {48: (0.9, True)})
        b.update(7, {48: (0.9, True)})
        cars = [car(2, 24), car(3, 48, 0.5, damage=0.9)]
        r = eng(100).tick(ctx(), cars, OWN, b)
        self.assertIn("retaliate:#24", r.reasons[3])
        self.assertAlmostEqual(r.targets[3], min(ag.RETALIATE_CAP, 1.1 + ag.RETALIATE_ADD))
        self.assertGreater(r.targets[3], ag.RIVAL_CAP)

    def test_governor_only_logs(self):
        b = self.feud_book(2)
        b.gov.tripped = True
        r = eng().tick(ctx(), [car(2, 24), car(3, 48, 0.6)], OWN, b)
        self.assertIn("(governor)", r.reasons[3])
        self.assertLess(r.targets.get(3, 1.1), 1.1 + ag.RIVAL_ADDS[2] * 0.7 - 0.1)

    def test_pack_tracks_wait_for_the_end(self):
        b = self.feud_book(2)
        cars = [car(2, 24), car(3, 48, 0.6)]
        self.assertNotIn("feud", eng().tick(ctx(lap=40, drafting=True), cars, OWN, b).reasons.get(3, ""))
        self.assertIn("feud", eng().tick(ctx(lap=95, drafting=True), cars, OWN, b).reasons[3])

    def test_repeat_victim_outside_top10_not_calmed(self):
        b = self.feud_book(2)                                   # #48 was the victim twice
        field = [car(10 + k, 100 + k, k * 3.0) for k in range(12)]
        cars = field + [car(2, 24, 40.0), car(3, 48, 50.0)]     # both far back, apart
        r = eng().tick(ctx(), cars, OWN, b)
        self.assertNotIn("outside_top10", r.reasons.get(3, ""))
        self.assertIn("outside_top10", r.reasons.get(2, ""))

    def test_player_rival_is_never_written(self):
        b = ag.RivalBook(seed=3)
        b.player = 98
        b.runin(ag.Contact(10, 0, 3, 98, 48, 0.0, 0.05, 0, 2.0), 5, {48: HOT})
        b.runin(ag.Contact(200, 0, 3, 98, 48, 0.0, 0.05, 0, 2.0), 9, {48: HOT})
        me = car(0, 98, 0.0)
        r = eng().tick(ctx(), [car(3, 48, 0.4)], OWN, b, extra=[me])
        self.assertIn("feud:#98", r.reasons[3])
        self.assertNotIn(0, r.targets)


if __name__ == "__main__":
    unittest.main()
