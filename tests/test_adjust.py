"""Crew chief chassis adjustments (no game needed)."""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aimod.adjust import CrewChief, LOOSE, TIGHT
from aimod.aisetup import ai_setup
from aimod.strategy import CarSnapshot

BASE = {"version": 14, "pressure": [28.0, 48.0, 28.0, 48.0], "track_bar": [9.0, 9.5], "tape": 30,
        "left_weight": 150, "front_weight": 5, "wedge": -110}


def car(i, rf=0.5, rr=0.5, lr=0.5):
    return CarSnapshot(i, True, wear_lf=0.4, wear_rf=rf, wear_lr=lr, wear_rr=rr, number=i + 1)


def field(n=10):
    return [car(i) for i in range(1, n + 1)]


class CrewChiefTests(unittest.TestCase):
    def chief(self, seed=1):
        c = CrewChief(random.Random(seed))
        c.reset(dict(BASE))
        return c

    def test_tight_car_gets_freed_up_by_a_good_crew(self):
        frees = 0
        for seed in range(100):
            c = self.chief(seed)
            a = c.call(car(0, rf=0.7, rr=0.5, lr=0.5), field(), to_go=200, tank=60, crew=2.0)
            self.assertIsNotNone(a)
            self.assertEqual(a.diagnosis, TIGHT)
            frees += ((a.kind == "wedge" and a.amount < 0) or (a.kind == "track_bar" and a.amount > 0)
                      or (a.kind == "pressure" and a.amount > 0))
        self.assertGreater(frees, 80)                     # a strong crew usually reads it right

    def test_weak_crew_misreads_more_often(self):
        def right(crew):
            n = 0
            for seed in range(200):
                a = self.chief(seed).call(car(0, rf=0.4, rr=0.6, lr=0.6), field(), 200, 60, crew=crew)
                n += a is not None and a.good_call
            return n
        self.assertEqual(self.chief().diagnose(car(0, rf=0.4, rr=0.6, lr=0.6), field(), None, None)[0], LOOSE)
        self.assertGreater(right(2.0), right(0.8) + 30)

    def test_good_car_gets_no_changes_but_tape_for_a_short_run(self):
        c = self.chief()
        self.assertIsNone(c.call(car(0), field(), to_go=200, tank=60))
        a = c.call(car(0), field(), to_go=12, tank=60)
        self.assertEqual(a.kind, "tape")
        self.assertIn("40 percent", a.crew)

    def test_adjustments_stay_inside_the_garage_limits(self):
        c = self.chief()
        for _ in range(60):
            c.call(car(0, rf=0.7), field(), to_go=200, tank=60, crew=2.0)
        st = c.state(0)
        self.assertGreaterEqual(BASE["wedge"] + st.wedge, -150)
        self.assertLessEqual(BASE["track_bar"][1] + st.track_bar, 14.0)
        self.assertLessEqual(BASE["pressure"][3] + st.rr_pressure, 60.0)

    def test_handling_effect_follows_the_call_and_is_capped(self):
        c = self.chief(3)
        for _ in range(40):
            a = c.call(car(0, rf=0.8), field(), to_go=200, tank=60, crew=2.0)
            if a is not None and a.effect:
                self.assertEqual(a.effect > 0, a.good_call)       # good call helps, misread hurts
        self.assertLessEqual(abs(c.state(0).chassis), 0.5 + 1e-9)

    def test_slightly_tight_car_gets_a_touch_under_caution_only(self):
        n = 0
        for seed in range(100):
            c = self.chief(seed)
            a = c.call(car(0, rf=0.52, rr=0.5, lr=0.5), field(), to_go=200, tank=60, crew=2.0)   # 4 % off: a touch
            n += a is not None
            if a is not None:
                self.assertEqual(a.diagnosis, TIGHT)
            self.assertIsNone(self.chief(seed).call(car(0, rf=0.52), field(), 200, 60, crew=2.0, green=True))
        self.assertTrue(20 < n < 50, n)

    def test_reads_the_installed_ai_setup(self):
        s = ai_setup(r"C:\Papyrus\NASCAR Racing 2003 Season", "Charlotte_2012_SDT_Night_CE")
        if s is None:
            self.skipTest("NR2003 not installed here")
        self.assertEqual(s["track_bar"], [9.0, 9.5])
        self.assertEqual(s["wedge"], -110)


if __name__ == "__main__":
    unittest.main()
