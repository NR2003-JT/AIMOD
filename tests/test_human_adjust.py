"""Crew chief on a phone (beta, stage 4): chassis adjustments a person asks for at the car's next stop."""
import os
import random
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aimod import memory_map as mm
from aimod.adjust import CHASSIS_CAP, CrewChief, TIGHT, human_status
from aimod.carno import CarNo
from aimod.humancalls import Desk, HumanAdjust
from aimod.strategy import CarSnapshot, RaceSnapshot, StrategyParams
from racesim import RaceSim
from test_adjust import BASE, car, field
from test_human_strategy import DAY


def chief(seed=1):
    c = CrewChief(random.Random(seed))
    c.reset(dict(BASE))
    return c


TIGHT_CAR = dict(rf=0.7, rr=0.5, lr=0.5)


class HumanAdjustModelTests(unittest.TestCase):
    def test_the_right_fix_helps_and_the_wrong_way_hurts(self):
        for seed in range(30):
            good = chief(seed).human(car(0, **TIGHT_CAR), field(), HumanAdjust(wedge=-2), 200, 60)
            bad = chief(seed).human(car(0, **TIGHT_CAR), field(), HumanAdjust(wedge=2), 200, 60)
            self.assertGreater(good.effect, 0)
            self.assertTrue(good.good_call)
            self.assertLess(bad.effect, 0)
            self.assertFalse(bad.good_call)
            self.assertEqual(good.diagnosis, TIGHT)                    # the driver describes the real car
            self.assertIn(good.driver, ("He's tight in the center, can't get it to turn.", "Pushing up off two.",
                                        "Tight, tight - it won't turn in the middle."))

    def test_too_much_overshoots(self):
        right = chief().human(car(0, **TIGHT_CAR), field(), HumanAdjust(bar=2), 200, 60).effect
        over = chief().human(car(0, **TIGHT_CAR), field(), HumanAdjust(bar=3, air=3), 200, 60).effect
        self.assertGreater(right, over)

    def test_a_balanced_car_knocked_off_balance(self):
        a = chief().human(car(0), field(), HumanAdjust(wedge=1), 200, 60)
        self.assertLess(a.effect, 0)

    def test_all_zero_is_no_changes(self):
        c = chief()
        self.assertIsNone(c.human(car(0, **TIGHT_CAR), field(), HumanAdjust(), 200, 60))
        self.assertEqual(c.state(0).history, [])
        self.assertEqual(human_status(None), "no changes")

    def test_air_needs_tires_tape_does_not(self):
        c = chief()
        self.assertIsNone(c.human(car(0), field(), HumanAdjust(air=2), 200, 60, tires=False))
        a = c.human(car(0), field(), HumanAdjust(air=2, tape=2), 12, 60, tires=False)
        self.assertEqual(a.kind, "tape")
        self.assertGreater(a.effect, 0)                                 # tape on a short run: speed
        self.assertEqual(c.state(0).rr_pressure, 0)

    def test_limits_and_cap_are_shared_with_aimods_calls(self):
        c = chief()
        for _ in range(20):
            c.human(car(0, **TIGHT_CAR), field(), HumanAdjust(wedge=-3, bar=3, air=3, tape=3), 200, 60)
            c.call(car(0, **TIGHT_CAR), field(), 200, 60)
        st = c.state(0)
        self.assertLessEqual(abs(st.chassis), CHASSIS_CAP + 1e-9)
        from aimod.aisetup import LIMITS
        lo, hi, _ = LIMITS["wedge"]
        self.assertTrue(lo <= BASE["wedge"] + st.wedge <= hi)
        lo, hi, _ = LIMITS["tape"]
        self.assertTrue(lo <= BASE["tape"] + st.tape <= hi)

    def test_bad_payload_is_kept_to_three_steps(self):
        a = chief().human(car(0, **TIGHT_CAR), field(), HumanAdjust(wedge=-15), 200, 60)
        self.assertEqual(a.short, "wedge -15 lb")

    def test_status_says_why_nothing_changed(self):
        self.assertEqual(human_status(None, HumanAdjust(wedge=2)), "no changes: at the garage limit")
        self.assertEqual(human_status(None, HumanAdjust(air=2), tires=False), "no changes: no tires, so no air change")

    def test_crew_line_and_status(self):
        a = chief().human(car(0, **TIGHT_CAR), field(), HumanAdjust(wedge=-1, bar=1), 200, 60)
        self.assertEqual(a.crew, "We're gonna take out half a round of wedge and move the track bar up a quarter.")
        self.assertEqual(a.short, "wedge -5 lb, track bar +0.25 in")
        self.assertTrue(human_status(a).startswith("done: wedge -5 lb, track bar +0.25 in; the driver"))


class DeskAdjustTests(unittest.TestCase):
    def setUp(self):
        self.engine = mock.Mock(cautions=0, lap_now=10, cars={})
        self.desk = Desk()
        cars = [CarSnapshot(0, True, number=CarNo(9), controlled=False), CarSnapshot(2, True, number=CarNo(24))]
        self.snap = RaceSnapshot(mm.FLAG_GREEN, 3, 40, 10, 100, cars)
        self.desk.apply(self.engine, self.snap)
        self.tok = self.desk.claim("24", "Dave")["token"]

    def test_set_clear_and_limits(self):
        self.assertTrue(self.desk.adjust(self.tok, wedge=-2, air="1")["ok"])
        self.desk.apply(self.engine, self.snap)
        a = self.engine.human_adjusts[2]
        self.assertEqual((a.wedge, a.bar, a.air, a.tape, a.who), (-2, 0, 1, 0, "Dave"))
        self.assertEqual(self.desk.view()["24"]["adjust"], {"wedge": -2, "bar": 0, "air": 1, "tape": 0})
        self.assertFalse(self.desk.adjust(self.tok, wedge=4)["ok"])
        self.assertFalse(self.desk.adjust(self.tok, bar="x")["ok"])
        self.assertFalse(self.desk.adjust("nope", wedge=1)["ok"])
        self.assertTrue(self.desk.adjust(self.tok, clear=1)["ok"])
        self.desk.apply(self.engine, self.snap)
        self.assertEqual(self.engine.human_adjusts, {})

    def test_used_request_is_dropped(self):
        aid = self.desk.adjust(self.tok, wedge=1)["adjust_id"]
        self.engine.cars = {2: mock.Mock(human_adjust_used={aid}, human_plan_id=None)}
        self.desk.sync(self.engine, self.snap)
        self.desk.apply(self.engine, self.snap)
        self.assertEqual(self.engine.human_adjusts, {})


def _sim(adjusts_for=None, owners=None, seed=3, cautions=(30, 62, 95, 120)):
    sim = RaceSim(DAY, 140, n=20, tank_laps=50, seed=seed, cautions=cautions,
                  params=StrategyParams(tank_laps=50, game_plan_off=True, fuel_margin=2))
    eng, tick = sim.engine, sim.engine.tick
    eng.crew_chief = CrewChief(random.Random(4))
    eng.crew_chief.reset(dict(BASE))
    adjusted = []

    def wrapped(snap, p):
        eng.human_owner = dict(owners or {})
        eng.human_adjusts = adjusts_for(eng, snap) if adjusts_for else {}
        res = tick(snap, p)
        adjusted.extend((ev.idx, ev.lap, ev.adjust.short if ev.adjust else "") for ev in res.events)
        return res
    eng.tick = wrapped
    with mock.patch("aimod.strategy.time.time", return_value=1_000_000.0):
        sim.run()
    return sim, adjusted


class HumanAdjustSimTests(unittest.TestCase):
    def test_unowned_or_no_adjusts_change_nothing(self):
        base, base_adj = _sim()
        other, other_adj = _sim(lambda eng, snap: {5: HumanAdjust(wedge=3, adjust_id=1)})   # car 5 isn't owned
        self.assertEqual([c.stops for c in base.cars], [c.stops for c in other.cars])
        self.assertEqual(base_adj, other_adj)
        self.assertTrue(any(a for i, lap, a in base_adj))                # AIMOD's own crew still adjusts
        owned, owned_adj = _sim(owners={2: "phone"})                      # owned, nothing asked: AIMOD's calls
        other, other_adj = _sim(lambda eng, snap: {5: HumanAdjust(wedge=3, adjust_id=1)}, owners={2: "phone"})
        self.assertEqual([c.stops for c in owned.cars], [c.stops for c in other.cars])
        self.assertEqual(owned_adj, other_adj)

    def test_request_used_at_one_stop_instead_of_aimods_call(self):
        req = HumanAdjust(bar=1, tape=1, who="Mike", adjust_id=7)
        sim, adjusted = _sim(lambda eng, snap: {2: req}, owners={2: "phone"})
        mine = [a for i, lap, a in adjusted if i == 2]
        self.assertTrue(mine)
        self.assertTrue(mine[0].startswith("track bar +0.25 in, tape +5%"), mine)
        self.assertNotIn(mine[0], mine[1:])                              # never made twice
        st = sim.engine.state(2)
        self.assertEqual(st.human_adjust_id, 7)
        self.assertTrue(st.human_adjust_status.startswith("done: track bar"), st.human_adjust_status)

    def test_all_zero_means_no_changes_and_no_aimod_call(self):
        sim, adjusted = _sim(lambda eng, snap: {2: HumanAdjust(adjust_id=3)}, owners={2: "phone"})
        first = next(a for i, lap, a in adjusted if i == 2)
        self.assertEqual(first, "")
        self.assertEqual(sim.engine.state(2).human_adjust_status, "no changes")

    def test_monkey_adjusts_keep_the_race_sane(self):
        rng = random.Random(9)
        reqs, ids = {}, [0]

        def chaos(eng, snap):
            if rng.random() < 0.03:
                ids[0] += 1
                reqs[rng.choice(range(2, 22, 2))] = HumanAdjust(*(rng.randint(-3, 3) for _ in range(4)),
                                                               adjust_id=ids[0])
            return dict(reqs)
        sim, _ = _sim(chaos, owners={i: f"phone {i}" for i in range(2, 22, 2)}, seed=5, cautions=(40, 90))
        self.assertEqual(sum(c.out_of_fuel for c in sim.cars), 0)
        self.assertTrue(all(not c.road for c in sim.cars))
        for i in range(2, 22, 2):
            self.assertLessEqual(abs(sim.engine.crew_chief.state(i).chassis), CHASSIS_CAP + 1e-9)


if __name__ == "__main__":
    unittest.main()
