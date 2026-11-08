"""aimod/aggression.py: the pure racing-intensity / rivals logic."""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aimod import aggression as ag     # noqa: E402


class PhaseCurve(unittest.TestCase):
    def test_stage_ramp_and_drop_after_the_break(self):
        ends = (80, 160)
        early = ag.phase(10, 300, ends)
        self.assertEqual((early.code, early.stage, early.factor), ("early", 1, ag.EARLY))
        late = ag.phase(79, 300, ends)
        self.assertEqual(late.code, "late")
        self.assertAlmostEqual(late.factor, ag.LATE)
        after = ag.phase(82, 300, ends)                      # the new stage starts patient: the drop
        self.assertEqual((after.code, after.stage, after.factor), ("early", 2, ag.EARLY))
        self.assertEqual(ag.phase(200, 300, ends).stage, 3)

    def test_ramp_is_monotonic_through_a_stage(self):
        vals = [ag.phase(lap, 300, (100, 200)).factor for lap in range(1, 101)]
        self.assertTrue(all(b >= a - 1e-12 for a, b in zip(vals, vals[1:])))
        self.assertAlmostEqual(vals[0], ag.EARLY)
        self.assertAlmostEqual(vals[-1], ag.LATE)

    def test_final_laps(self):
        self.assertEqual(ag.phase(298, 300, (100, 200)).code, "final")
        self.assertAlmostEqual(ag.phase(298, 300, (100, 200)).factor, ag.FINAL)
        self.assertEqual(ag.phase(198, 300, (100, 200)).code, "late")   # a stage end isn't the finish

    def test_no_stages_builds_over_the_race(self):
        self.assertEqual(ag.phase(50, 200).factor, ag.EARLY)
        self.assertEqual(ag.phase(99, 200).factor, ag.EARLY)
        mid = ag.phase(140, 200).factor
        self.assertAlmostEqual(mid, 1.0)
        self.assertAlmostEqual(ag.phase(181, 200).factor, ag.NOSTAGE_FINAL)    # last 10 %
        self.assertAlmostEqual(ag.phase(199, 200).factor, ag.FINAL)
        vals = [ag.phase(lap, 200).factor for lap in range(0, 201)]
        self.assertTrue(all(b >= a - 1e-12 for a, b in zip(vals, vals[1:])))

    def test_unknown_length_is_neutral(self):
        self.assertEqual(ag.phase(10, 0).factor, 1.0)

    def test_stage_ends_outside_the_race_are_ignored(self):
        self.assertEqual(ag.phase(50, 200, (0, 250)), ag.phase(50, 200))


class Windows(unittest.TestCase):
    def test_hysteresis(self):
        self.assertFalse(ag.battle_window(False, 0.7))      # not in yet
        self.assertTrue(ag.battle_window(False, 0.5))
        self.assertTrue(ag.battle_window(True, 0.95))       # stays in until BATTLE_LEAVE (1.0)
        self.assertFalse(ag.battle_window(True, ag.BATTLE_LEAVE + 0.05))
        self.assertTrue(ag.rival_window(False, 0.8))
        self.assertTrue(ag.rival_window(True, 1.4))
        self.assertFalse(ag.rival_window(True, 1.6))

    def test_no_gap(self):
        for g in (None, math.nan, -1.0, math.inf):
            self.assertFalse(ag.window(True, g, 1, 2))


class Terms(unittest.TestCase):
    def test_damage(self):
        self.assertEqual(ag.damage_term(None), (1.0, "", False))
        self.assertEqual(ag.damage_term(0.05), (1.0, "", False))
        self.assertEqual(ag.damage_term(0.3), (1.0, "", False))           # moderate damage: cosmetic, ignored
        self.assertEqual(ag.damage_term(0.6), (ag.DAMAGE_SOFT_K, "damage", False))
        self.assertEqual(ag.damage_term(0.9), (ag.DAMAGE_HARD_K, "damage", True))

    def test_team_and_lapped(self):
        self.assertEqual(ag.team_term(False, False), (1.0, ""))
        self.assertEqual(ag.team_term(True, False), (ag.TEAM_HOLD_K, "team_hold"))
        self.assertEqual(ag.team_term(True, True), (ag.TEAM_SAVE_K, "team_save"))
        self.assertEqual(ag.lapped_term(True), (ag.LAPPED_K, "lapped"))

    def test_battle(self):
        self.assertEqual(ag.battle_term(False, True), 0.0)
        self.assertEqual(ag.battle_term(True), ag.BATTLE_ADD)
        self.assertEqual(ag.battle_term(True, True), ag.LEAD_ADD)

    def test_strength(self):
        self.assertEqual(ag.strength(70), 0.7)
        self.assertEqual(ag.strength(-5), 0.0)
        self.assertEqual(ag.strength(400), 1.5)


class Target(unittest.TestCase):
    def test_strength_zero_is_the_game_value(self):
        for b in (0.3, 1.0, 1.6, 2.9):
            self.assertAlmostEqual(ag.target(b, 1.3, 0.45, rival=True, damage_k=0.7, team_k=0.85, s=0.0), b)

    def test_phase_and_battle(self):
        b = 1.3
        self.assertAlmostEqual(ag.target(b, ag.EARLY, s=1.0), b * ag.EARLY)
        self.assertAlmostEqual(ag.target(b, ag.LATE, ag.BATTLE_ADD, s=0.7), b + 0.7 * (b * ag.LATE + ag.BATTLE_ADD - b))
        self.assertGreater(ag.target(b, ag.LATE, ag.LEAD_ADD), ag.target(b, ag.LATE, ag.BATTLE_ADD))

    def test_safe_max_for_everyone_but_rivals(self):
        self.assertAlmostEqual(ag.target(1.6, ag.FINAL, ag.LEAD_ADD, s=1.5), ag.SAFE_MAX)
        r = ag.target(1.6, ag.FINAL, ag.LEAD_ADD, rival=True, s=1.5)
        self.assertAlmostEqual(r, ag.RIVAL_CAP)
        self.assertLessEqual(r, ag.HARD_MAX)

    def test_rival_level(self):
        self.assertAlmostEqual(ag.target(1.3, 1.0, rival=True, s=0.7), 1.3 + ag.RIVAL_ADD * 0.7)
        self.assertAlmostEqual(ag.target(1.3, 1.0, rival=True, s=1.0), ag.RIVAL_CAP)

    def test_damage_cancels_bumps_and_rivals(self):
        hurt = ag.target(1.3, ag.LATE, ag.LEAD_ADD, rival=True, damage_k=ag.DAMAGE_HARD_K, no_bumps=True, s=1.0)
        self.assertAlmostEqual(hurt, 1.3 * ag.LATE * ag.DAMAGE_HARD_K)
        self.assertLess(hurt, 1.3 * ag.LATE)

    def test_floor_and_own_extremes(self):
        self.assertEqual(ag.target(1.0, 0.1, s=1.5), ag.FLOOR)              # never under the floor ...
        self.assertAlmostEqual(ag.target(0.3, 0.1, s=1.5), 0.3)            # ... unless the driver rolled lower
        self.assertAlmostEqual(ag.target(2.6, 1.0, s=1.0), 2.6)            # a driver rolled above SAFE_MAX keeps it
        self.assertAlmostEqual(ag.target(2.6, ag.LATE, ag.LEAD_ADD, s=1.0), 2.6)

    def test_bad_input_passes_through(self):
        self.assertTrue(math.isnan(ag.target(math.nan)))
        self.assertEqual(ag.target(0.0), 0.0)

    def test_deadband(self):
        self.assertTrue(ag.worth_writing(1.3, None))
        self.assertFalse(ag.worth_writing(1.33, 1.3))
        self.assertTrue(ag.worth_writing(1.36, 1.3))


class Dice(unittest.TestCase):
    def test_seeded_and_symmetric(self):
        self.assertEqual(ag.encounter_dice(7, 24, 48, 1), ag.encounter_dice(7, 48, 24, 1))
        self.assertNotEqual(ag.encounter_dice(7, 24, 48, 1), ag.encounter_dice(7, 24, 48, 2))
        self.assertNotEqual(ag.encounter_dice(7, 24, 48, 1), ag.encounter_dice(8, 24, 48, 1))
        vals = [ag.encounter_dice(1, 3, 24, n) for n in range(2000)]
        self.assertTrue(all(0.0 <= v < 1.0 for v in vals))
        self.assertAlmostEqual(sum(vals) / len(vals), 0.5, delta=0.05)

    def test_grudges(self):
        self.assertFalse(ag.grudge_holds(0.1))
        self.assertTrue(ag.grudge_holds(0.6))
        goes = sum(ag.grudge_goes(5, 24, 48, n, 0.8, 0.7) for n in range(1000)) / 1000
        calm = sum(ag.grudge_goes(5, 24, 48, n, 0.3, 0.0) for n in range(1000)) / 1000
        self.assertGreater(goes, calm)
        self.assertLess(goes, 0.97)
        self.assertEqual(ag.grudge_goes(5, 24, 48, 3, 0.8, 0.7), ag.grudge_goes(5, 24, 48, 3, 0.8, 0.7))


class RivalsList(unittest.TestCase):
    def test_parse(self):
        pairs, bad = ag.parse_rivals("""# my rivals
24 48
#3 vs #24
01,99   # old one
// note
48 24
7
5 5

""")
        self.assertEqual(pairs, {("24", "48"), ("24", "3"), ("01", "99")})
        self.assertEqual(bad, ["7", "5 5"])

    def test_empty(self):
        self.assertEqual(ag.parse_rivals(""), (set(), []))
        self.assertEqual(ag.parse_rivals(None), (set(), []))


class Temperament(unittest.TestCase):
    def test_percentiles(self):
        t = ag.temperaments({1: 0.95, 2: 1.63, 3: 1.2, 4: None}, {1: "a", 2: "b", 3: "c", 4: "Dale"})
        self.assertEqual(t[1], 0.0)
        self.assertEqual(t[2], 0.99)
        self.assertAlmostEqual(t[3], 0.495)
        self.assertEqual(t[4], ag.temper_from_name("Dale"))        # no car file: the name hash

    def test_scale_free(self):
        a = ag.temperaments({1: 0.9, 2: 1.0, 3: 1.1}, {})
        b = ag.temperaments({1: 9.0, 2: 10.0, 3: 11.0}, {})
        self.assertEqual(a, b)

    def test_ties_split_by_name(self):
        t = ag.temperaments({1: 1.2, 2: 1.2}, {1: "Alpha", 2: "Beta"})
        self.assertEqual(sorted(t.values()), [0.0, 0.99])
        self.assertEqual(ag.temperaments({1: 1.2}, {}), {1: 0.5})


class GovernorTest(unittest.TestCase):
    def test_trips_after_two_blamed_cautions(self):
        g = ag.Governor(limit=2)
        g.contact(100.0, (24, 48), boosted=False)
        self.assertIsNone(g.caution(105.0))                     # not a boosted car
        g.contact(200.0, (24, 48), boosted=True)
        self.assertIsNone(g.caution(215.0))                     # too late to blame it
        g.contact(300.0, (24, 48), boosted=True)
        self.assertEqual(g.caution(305.0), (24, 48))
        self.assertFalse(g.tripped)
        g.contact(400.0, (3, 24), boosted=True)
        self.assertEqual(g.caution(401.0), (3, 24))
        self.assertTrue(g.tripped)
        g.reset()
        self.assertFalse(g.tripped)


if __name__ == "__main__":
    unittest.main()


class OnScreenDistance(unittest.TestCase):
    class C:
        def __init__(self, x, y, valid=True):
            self.pos, self.valid = (x, y), valid

    def test_close_pair_shows(self):
        me, o = self.C(0, 0), self.C(12, 0)
        self.assertTrue(ag.close_enough_for_screen(me, o, [me, o]))

    def test_too_far_hides(self):
        me, o = self.C(0, 0), self.C(ag.ONSCREEN_M + 1, 0)
        self.assertFalse(ag.close_enough_for_screen(me, o, [me, o]))

    def test_a_car_between_hides(self):
        me, mid, o = self.C(0, 0), self.C(8, 0.5), self.C(16, 0)
        self.assertFalse(ag.close_enough_for_screen(me, o, [me, mid, o]))

    def test_a_car_beside_the_pair_but_not_between_still_shows(self):
        me, o, far = self.C(0, 0), self.C(10, 0), self.C(40, 3)
        self.assertTrue(ag.close_enough_for_screen(me, o, [me, o, far]))
