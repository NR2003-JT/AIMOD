"""aggression.IntensityEngine: who races whom, phase, damage, teammates, lapped cars, caution / pit road, determinism;
and behavior.temperament from the .car aggression."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aimod import aggression as ag                                   # noqa: E402
from aimod import behavior                                           # noqa: E402
from aimod.behavior import Car, Ctx                                  # noqa: E402

LT = 30.0


def ctx(lap=10, total=100, green=True, race=True, drafting=False, stage_ends=()):
    return Ctx(race=race, green=green, lap_now=lap, total=total, to_go=total - lap, lap_time=LT, drafting=drafting,
               stage_ends=stage_ends)


def car(idx, number, gap_s=0.0, lap=10, laps_down=0, team=-1, damage=0.0, on_track=True, lead_progress=10.5):
    """a car gap_s seconds behind lead_progress (race order), on car lap `lap`"""
    return Car(idx, number, f"Driver {number}", team=team, lap=lap, laps_down=laps_down,
               progress=lead_progress - gap_s / LT, on_track=on_track, damage=damage)


def engine(mode=ag.ON, strength=100, teams=True):
    return ag.IntensityEngine(ag.IntensityConfig(mode, strength, teams))


OWN = {i: 1.2 for i in range(2, 44)}


class Basics(unittest.TestCase):
    def test_off_does_nothing(self):
        e = engine(ag.OFF)
        r = e.tick(ctx(), [car(2, 24), car(3, 48, 0.2)], OWN)
        self.assertEqual((r.targets, r.changes), ({}, []))

    def test_alone_early_in_the_race(self):
        r = engine().tick(ctx(lap=10), [car(2, 24)], OWN)
        self.assertAlmostEqual(r.targets[2], 1.2 * ag.EARLY)
        self.assertEqual(r.reasons[2], "early")
        self.assertEqual(len(r.changes), 1)
        ch = r.changes[0]
        self.assertEqual((ch.old, ch.base, ch.number), (1.2, 1.2, 24))

    def test_strength_zero_leaves_everyone_alone(self):
        r = engine(strength=0).tick(ctx(lap=95), [car(2, 24), car(3, 48, 0.2)], OWN)
        self.assertEqual(r.targets, {})

    def test_no_own_value_no_target(self):
        r = engine().tick(ctx(), [car(5, 24)], {})
        self.assertEqual(r.targets, {})

    def test_deterministic(self):
        cars = [car(2, 24), car(3, 48, 0.3), car(4, 3, 0.6, team=1), car(5, 9, 0.8, team=1), car(6, 11, 5.0)]
        a = engine().tick(ctx(lap=80), cars, OWN)
        b = engine().tick(ctx(lap=80), cars, OWN)
        self.assertEqual((a.targets, a.reasons), (b.targets, b.reasons))


class WhoRaces(unittest.TestCase):
    def test_battle_any_position(self):
        cars = [car(2, 1)] + [car(10 + k, 10 + k, 10.0 + k * 3) for k in range(5)] \
            + [car(30, 77, 30.0), car(31, 78, 30.3)]                  # P7 and P8 battling
        r = engine().tick(ctx(lap=50), cars, OWN)
        self.assertIn("battle:#78", r.reasons[30])
        self.assertIn("battle:#77", r.reasons[31])
        self.assertNotIn("battle", r.reasons[2])
        self.assertGreater(r.targets[30], r.targets.get(2, 1.2))

    def test_lead_battle_is_bigger(self):
        cars = [car(2, 1), car(3, 2, 0.3), car(20, 50, 20.0), car(21, 51, 20.3)]
        r = engine().tick(ctx(lap=50), cars, OWN)
        self.assertIn("lead_battle:#2", r.reasons[2])
        self.assertIn("battle:#50", r.reasons[21])
        self.assertGreater(r.targets[2], r.targets[21])

    def test_hysteresis_and_hold(self):
        e = engine()
        e.tick(ctx(lap=50), [car(2, 1, lap=50), car(3, 2, 0.3, lap=50)], OWN)
        # next lap 0.7 s apart: inside the leave window, still racing
        r = e.tick(ctx(lap=51), [car(2, 1, lap=51), car(3, 2, 0.7, lap=51)], OWN)
        self.assertIn("battle", r.reasons[3])
        # 1.2 s apart: held for MIN_HOLD_LAPS (3) laps from lap 50, then over
        r = e.tick(ctx(lap=51), [car(2, 1, lap=51), car(3, 2, 1.2, lap=51)], OWN)
        self.assertIn("battle", r.reasons[3])                         # (same lap: no new decision)
        r = e.tick(ctx(lap=52), [car(2, 1, lap=52), car(3, 2, 1.2, lap=52)], OWN)
        self.assertIn("battle", r.reasons[3])                         # lap 52: still inside the hold
        r = e.tick(ctx(lap=53), [car(2, 1, lap=53), car(3, 2, 1.2, lap=53)], OWN)
        self.assertNotIn("battle", r.reasons.get(3, ""))
        # a fresh pair at 0.7 s doesn't start a battle (enter at 0.5)
        r = engine().tick(ctx(lap=50), [car(2, 1), car(3, 2, 0.7)], OWN)
        self.assertNotIn("battle", r.reasons[3])

    def test_once_a_lap(self):
        e = engine()
        e.tick(ctx(lap=50), [car(2, 1, lap=50), car(3, 2, 2.0, lap=50)], OWN)
        r = e.tick(ctx(lap=50), [car(2, 1, lap=50), car(3, 2, 0.2, lap=50)], OWN)
        self.assertNotIn("battle", r.reasons[3])                      # waits for his line crossing
        self.assertEqual(r.changes, [])

    def test_different_laps_dont_battle(self):
        r = engine().tick(ctx(lap=50), [car(2, 1), car(3, 2, 0.3, laps_down=1)], OWN)
        self.assertNotIn("battle", r.reasons[3])

    def test_drafting_tracks_only_late(self):
        cars = [car(2, 1), car(3, 2, 0.3)]
        mid = engine().tick(ctx(lap=40, drafting=True), cars, OWN)
        self.assertNotIn("battle", mid.reasons[3])
        late = engine().tick(ctx(lap=95, drafting=True), cars, OWN)
        self.assertIn("battle", late.reasons[3])


class Terms(unittest.TestCase):
    def test_teammates(self):
        r = engine().tick(ctx(lap=50), [car(2, 1, team=4), car(3, 2, 0.3, team=4)], OWN)
        self.assertNotIn("battle", r.reasons[3])
        self.assertIn("team_hold", r.reasons[3])
        r = engine(teams=False).tick(ctx(lap=50), [car(2, 1, team=4), car(3, 2, 0.3, team=4)], OWN)
        self.assertIn("battle", r.reasons[3])

    def test_team_save(self):
        # #2 is a lap down on his teammate #1, running right next to him on track
        cars = [car(2, 1, team=4), car(3, 2, LT + 0.3, laps_down=1, team=4)]
        r = engine().tick(ctx(lap=50), cars, OWN)
        self.assertIn("team_save", r.reasons[3])

    def test_lapped(self):
        cars = [car(2, 1), car(3, 9, LT - 0.4, laps_down=1)]          # the leader 0.4 s behind him on track
        r = engine().tick(ctx(lap=50), cars, OWN)
        self.assertIn("lapped", r.reasons[3])
        self.assertNotIn("lapped", r.reasons.get(2, ""))

    def test_damage(self):
        cars = [car(2, 1), car(3, 2, 0.3, damage=0.9)]
        r = engine().tick(ctx(lap=50), cars, OWN)
        self.assertIn("damage", r.reasons[3])
        self.assertNotIn("battle", r.reasons[3])                     # no bump: not named either
        self.assertLess(r.targets[3], 1.2)

    def test_moderate_damage_is_ignored(self):
        r = engine().tick(ctx(lap=50), [car(2, 1), car(3, 2, 0.3, damage=0.3)], OWN)
        self.assertNotIn("damage", r.reasons[3])
        self.assertIn("battle", r.reasons[3])

    def test_outside_top_10_calmer(self):
        cars = [car(2 + k, 10 + k, k * 3.0) for k in range(12)]
        r = engine().tick(ctx(lap=50), cars, OWN)
        self.assertIn("outside_top10", r.reasons[2 + 11])
        self.assertNotIn("outside_top10", r.reasons.get(2 + 5, ""))
        self.assertLess(r.targets[13], r.targets[7])


class OffTrackAndCaution(unittest.TestCase):
    def test_pit_road_back_at_once(self):
        e = engine()
        e.tick(ctx(lap=95), [car(2, 1, lap=95), car(3, 2, 0.3, lap=95)], OWN)
        r = e.tick(ctx(lap=95), [car(2, 1, lap=95), car(3, 2, 0.3, lap=95, on_track=False)], OWN)
        self.assertNotIn(3, r.targets)
        self.assertTrue(any(c.idx == 3 and c.reasons == "pit" for c in r.changes))

    def test_caution_back_at_once(self):
        e = engine()
        e.tick(ctx(lap=95), [car(2, 1, lap=95), car(3, 2, 0.3, lap=95)], OWN)
        r = e.tick(ctx(lap=95, green=False), [car(2, 1, lap=95), car(3, 2, 0.3, lap=95)], OWN)
        self.assertEqual(r.targets, {})
        self.assertEqual({c.reasons for c in r.changes}, {"caution"})

    def test_shadow_decides_the_same(self):
        cars = [car(2, 1), car(3, 2, 0.3)]
        a = engine(ag.SHADOW).tick(ctx(lap=80), cars, OWN)
        b = engine(ag.ON).tick(ctx(lap=80), cars, OWN)
        self.assertEqual(a.targets, b.targets)


class Temperament(unittest.TestCase):
    def tearDown(self):
        behavior.set_temperaments({})

    def test_table_by_number_else_name(self):
        name_only = behavior.temperament("Some Driver")
        behavior.set_temperaments({24: 0.12})
        self.assertEqual(behavior.temperament("Jeff Gordon", 24), 0.12)
        self.assertEqual(behavior.temperament("Some Driver", 99), name_only)
        self.assertEqual(behavior.temperament("Some Driver"), name_only)
        self.assertEqual(Car(2, 24, "Jeff Gordon").temper, 0.12)
        behavior.set_temperaments(None)
        self.assertEqual(behavior.temperament("Jeff Gordon", 24), behavior.temperament("Jeff Gordon"))


if __name__ == "__main__":
    unittest.main()
