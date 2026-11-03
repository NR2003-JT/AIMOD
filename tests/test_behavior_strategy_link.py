"""Pit strategy and driver behavior agree (2026-10-28): the run plan follows the stop style, the stop style leans on
the driver's tire tendency, and a car with good tires weighs track position at a caution.

Run: python -m unittest discover -s tests
"""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aimod.behavior import ATTACK, MANAGE, PUSH, RUN_OUT, SAVE_TIRES, temperament
from aimod.strategy import SHORT_PIT, SPLIT, STRETCH, CarInfo, StrategyEngine, StrategyParams
from test_behavior import ctx, engine, manager, runner


def named(temper_lo, temper_hi, prefix="Driver"):
    k = 0
    while not temper_lo <= temperament(f"{prefix} {k}") < temper_hi:
        k += 1
    return f"{prefix} {k}"


class RunPlanFollowsStopStyle(unittest.TestCase):
    def plan(self, c):
        e = engine()
        e.tick(ctx(), [c])
        return e.plan(c.idx)

    def test_stretch_is_run_as_a_tire_manager_even_by_a_runner(self):
        c = runner(1, stop_style=STRETCH)
        c.name = named(0.6, 0.9, "Runner")          # aggressive, but not the most aggressive ~10 %
        self.assertEqual(self.plan(c).style, MANAGE)
        c.name = named(0.9, 1.0, "Hothead")         # the exception: runs a stretch out
        self.assertEqual(self.plan(c).style, RUN_OUT)

    def test_short_pit_is_attacked_by_an_aggressive_driver(self):
        c = runner(2, stop_style=SHORT_PIT)
        p = self.plan(c)
        self.assertEqual(p.style, ATTACK)
        e = engine()
        c.lap, c.stint_laps = 52, 2
        e.tick(ctx(), [c])
        self.assertEqual(e.mode(c.idx), PUSH)

    def test_short_pit_by_a_patient_driver_manages(self):
        c = manager(3, stop_style=SHORT_PIT)
        self.assertEqual(self.plan(c).style, MANAGE)

    def test_split_keeps_the_drivers_own_leaning(self):
        self.assertEqual(self.plan(manager(4, stop_style=SPLIT)).style, MANAGE)
        self.assertEqual(self.plan(runner(5, stop_style=SPLIT)).style, RUN_OUT)

    def test_plan_follows_a_style_that_comes_early_in_the_run(self):
        c = runner(6)
        e = engine()
        e.tick(ctx(), [c])
        self.assertEqual(e.plan(c.idx).style, RUN_OUT)
        c.stop_style, c.stint_laps, c.lap = STRETCH, 1, 51
        e.tick(ctx(), [c])
        self.assertEqual(e.plan(c.idx).style, MANAGE)


class StopStyleLeansOnTheDriver(unittest.TestCase):
    def share(self, name, rating, on=True, n=400):
        s = StrategyEngine(rng=random.Random(7))
        s.behavior_on = on
        p = StrategyParams()
        got = {STRETCH: 0, SHORT_PIT: 0, SPLIT: 0}
        for _ in range(n):
            s._style_name = name
            got[s.choose_style(None, rating, 0.0, p)] += 1
        return {k: v / n for k, v in got.items()}

    def test_managers_stretch_more_and_attackers_short_pit_more(self):
        patient, aggressive = named(0.0, 0.2, "Patient"), named(0.8, 1.0, "Hot")
        base = self.share(patient, 100, on=False)
        mgr = self.share(patient, 100)
        att = self.share(aggressive, 0)
        self.assertGreater(mgr[STRETCH], base[STRETCH] + 0.1)
        self.assertLess(mgr[SHORT_PIT], base[SHORT_PIT])
        self.assertGreater(att[SHORT_PIT], self.share(aggressive, 0, on=False)[SHORT_PIT] + 0.1)

    def test_off_changes_nothing(self):
        name = named(0.0, 0.2, "Patient")
        a = self.share(name, 100, on=False)
        s = StrategyEngine(rng=random.Random(7))
        p = StrategyParams()
        b = {STRETCH: 0, SHORT_PIT: 0, SPLIT: 0}
        for _ in range(400):
            b[s.choose_style(None, 100, 0.0, p)] += 1
        self.assertEqual(a, {k: v / 400 for k, v in b.items()})


class GoodTiresAtACaution(unittest.TestCase):
    def setup(self, worn, falloff_ratio, style=STRETCH, seed=1):
        s = StrategyEngine(rng=random.Random(seed))
        s.behavior_on = True
        s.tire_ratio = lambda car, p: worn
        s.tire_manager_ratio = lambda car: falloff_ratio
        st = s.state(9)
        st.style, st.decision = style, True

        class C:
            idx, name = 9, named(0.0, 0.2, "Patient")
        return s, st, C(), CarInfo(rank=3, down=0, progress=40.0, gap=1.0, age=0.3), StrategyParams()

    def test_managed_tires_stay_out_or_take_two(self):
        outcomes = set()
        for seed in range(40):
            s, st, c, me, p = self.setup(0.3, 0.7, seed=seed)
            s.good_tires_call(c, st, me, p, to_go=150)
            outcomes.add("out" if st.decision is False else "two" if st.tires_good else "four")
        self.assertEqual(outcomes, {"out", "two"})

    def test_worn_tires_pit_as_usual(self):
        s, st, c, me, p = self.setup(0.9, 0.7)
        s.good_tires_call(c, st, me, p, to_go=150)
        self.assertTrue(st.decision)
        self.assertFalse(st.tires_good)

    def test_falling_off_car_on_a_split_run_pits_as_usual(self):
        s, st, c, me, p = self.setup(0.3, 1.3, style=SPLIT)
        s.good_tires_call(c, st, me, p, to_go=150)
        self.assertTrue(st.decision)
        self.assertFalse(st.tires_good)

    def test_off_does_nothing(self):
        s, st, c, me, p = self.setup(0.3, 0.7)
        s.behavior_on = False
        s.good_tires_call(c, st, me, p, to_go=150)
        self.assertTrue(st.decision)


if __name__ == "__main__":
    unittest.main()
