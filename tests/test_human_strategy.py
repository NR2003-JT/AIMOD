"""Crew chief on a phone (beta, stage 2): caution calls made by a person, through AIMOD's own decision path."""
import os
import random
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aimod import memory_map as mm
from aimod.humancalls import HumanCall, HumanPlan
from aimod.strategy import (FOUR_TIRES, FUEL_ONLY, HUMAN_WAIT_S, LEFT_SIDE, RIGHT_SIDE, StrategyEngine,
                            StrategyParams)
from racesim import RaceSim, Track
from test_strategy import CAUTION, car, race, writes_for


class HumanCautionCallTests(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.eng = StrategyEngine(random.Random(1), clock=lambda: self.now[0])
        self.p = StrategyParams(tank_laps=60)
        self.eng.race_started = True
        self.eng.human_owner = {0: "Mike's phone"}
        self.field = lambda **kw: [car(0, laps_done=40, lap_dist=300, fuel=60.0, fuel_cap=100.0, **kw)] + \
            [car(i, laps_done=40, lap_dist=300 + 30 * i, fuel=60.0, fuel_cap=100.0) for i in range(1, 6)]
        self.eng.tick(race(self.field(on_pit_road=False), lap=41, total=200), self.p)       # green
        self.now[0] += 10

    def tick(self, **kw):
        return self.eng.tick(race(self.field(on_pit_road=False, **kw), flag=CAUTION, lap=41, total=200), self.p)

    def call(self, pit, service=None, fuel=None, **kw):
        self.tick(**kw)                                     # the caution is out: the phone shows the car waiting
        self.eng.human_calls = {0: HumanCall(self.eng.cautions, pit, service, fuel, who="Mike")}

    def test_waits_for_the_crew_chief_then_makes_its_own_call(self):
        res = self.tick()
        st = self.eng.state(0)
        self.assertEqual(row0(res).status, "WAITS FOR CREW CHIEF (Mike's phone)")
        self.assertNotIn(1, writes_for(res, 0, mm.OFF_PITTING))
        self.assertAlmostEqual(st.human_deadline_s, HUMAN_WAIT_S, places=1)
        self.now[0] += HUMAN_WAIT_S + 1
        res = self.tick()
        self.assertFalse(row0(res).status.startswith("WAITS"))
        self.assertEqual(st.human_status, "no answer, AIMOD's call")
        self.assertIsNotNone(st.decision)

    def test_pit_call_with_service_and_fuel_goes_through_aimods_stop(self):
        self.call(True, RIGHT_SIDE, 2)
        res = self.tick()
        st = self.eng.state(0)
        self.assertEqual(st.human_status, "accepted")
        self.assertIn(1, writes_for(res, 0, mm.OFF_PITTING))
        res = self.tick(pitting=1)                          # the car takes the request: the stop starts
        ev = [e for e in res.events if e.idx == 0][0]
        self.assertEqual(ev.service, RIGHT_SIDE)
        self.assertEqual(st.fuel_level, 2)
        self.assertIn("crew chief call (Mike)", ev.note)

    def test_stay_out_call_is_kept_out(self):
        self.call(False)
        res = self.tick()
        self.assertIs(self.eng.state(0).decision, False)
        self.assertNotIn(1, writes_for(res, 0, mm.OFF_PITTING))
        res = self.tick(pitting=1)                          # the game's own caution request: called off
        self.assertNotIn(1, writes_for(res, 0, mm.OFF_PITTING))
        self.assertEqual([e for e in res.events if e.idx == 0], [])

    def test_late_stay_out_calls_off_aimods_own_request(self):
        # live beta: no answer in time -> AIMOD called the stop; "stay out" sent after that was ignored and he pitted
        self.tick()                                          # waiting for the crew chief
        self.now[0] += HUMAN_WAIT_S + 1
        st = self.eng.state(0)
        for _ in range(3):
            res = self.tick()
            if st.decision:
                break
        if not st.decision:
            self.skipTest("AIMOD kept this car out on its own here")
        self.assertIn(1, writes_for(res, 0, mm.OFF_PITTING))
        self.tick(pitting=1)                                 # the request is in: the stop is under way
        self.assertIsNotNone(st.event)
        self.eng.human_calls = {0: HumanCall(self.eng.cautions, False, who="Mike")}
        res = self.tick(pitting=1)
        self.assertIs(st.decision, False)
        self.assertEqual(st.human_status, "accepted")
        self.assertIn(0, writes_for(res, 0, mm.OFF_PITTING))
        self.assertNotIn(1, writes_for(res, 0, mm.OFF_PITTING))
        res = self.tick(pitting=0)                           # withdrawn before pit road: not a stop
        self.assertEqual(len(res.cancels), 1)

    def test_safety_beats_the_human(self):
        self.field = lambda **kw: [car(0, laps_done=40, lap_dist=300, fuel=1.5, fuel_cap=100.0, **kw)] + \
            [car(i, laps_done=40, lap_dist=300 + 30 * i, fuel=60.0, fuel_cap=100.0) for i in range(1, 6)]
        self.call(False)                                     # stay out on fumes
        self.tick()
        st = self.eng.state(0)
        self.assertIs(st.decision, True)
        self.assertTrue(st.human_status.startswith("overridden:"), st.human_status)

    def test_no_pit_call_on_the_wave_around(self):
        self.call(True, waved=True)
        self.tick(waved=True)
        st = self.eng.state(0)
        self.assertIs(st.decision, False)
        self.assertTrue(st.human_status.startswith("overridden: wave around"), st.human_status)

    def test_stale_call_is_ignored(self):
        self.tick()
        self.eng.human_calls = {0: HumanCall(self.eng.cautions - 1, True, FOUR_TIRES, None, who="Mike")}
        res = self.tick()
        self.assertEqual(row0(res).status, "WAITS FOR CREW CHIEF (Mike's phone)")

    def test_player_car_is_never_held(self):
        self.field = lambda **kw: [car(0, laps_done=40, lap_dist=300, controlled=False, **kw)] + \
            [car(i, laps_done=40, lap_dist=300 + 30 * i) for i in range(1, 6)]
        res = self.tick()
        self.assertFalse(row0(res).status.startswith("WAITS FOR CREW CHIEF"))


def row0(res):
    return next(r for r in res.rows if r.idx == 0)


DAY = Track("charlotte", 2414, 33.7, pit_road_s=40.0)


def _sim(seed=3, owners=None, calls=None, chaos=None):
    sim = RaceSim(DAY, 140, n=20, tank_laps=50, seed=seed, cautions=(30, 62, 95, 120),
                  params=StrategyParams(tank_laps=50, game_plan_off=True, fuel_margin=2))
    eng, tick = sim.engine, sim.engine.tick

    def wrapped(snap, p):
        eng.human_owner = dict(owners or {})
        eng.human_calls = dict(calls or {})
        if chaos:
            chaos(eng, snap)
        return tick(snap, p)
    eng.tick = wrapped
    with mock.patch("aimod.strategy.time.time", return_value=1_000_000.0):   # the engine reseeds at flag changes
        sim.run()
    return sim


class HumanSimTests(unittest.TestCase):
    def test_unowned_cars_and_calls_change_nothing(self):
        base = _sim()
        other = _sim(owners={99: "nobody"}, calls={1: HumanCall(1, True, FOUR_TIRES, 5), 2: HumanCall(2, False)})
        self.assertEqual([c.stops for c in base.cars], [c.stops for c in other.cars])

    def test_monkey_crew_chiefs_keep_the_race_sane(self):
        rng = random.Random(7)
        owners = {i: f"phone {i}" for i in range(2, 22, 2)}
        calls = {}

        def chaos(eng, snap):
            if snap.flag != mm.FLAG_GREEN and rng.random() < 0.3:
                i = rng.choice(list(owners))
                calls[i] = HumanCall(eng.cautions if rng.random() < 0.9 else eng.cautions - 1, rng.random() < 0.5,
                                     rng.choice([None, FOUR_TIRES, RIGHT_SIDE, LEFT_SIDE, FUEL_ONLY]),
                                     rng.choice([None, 1, 2, 3, 4, 5]), who=f"phone {i}")
            eng.human_calls = dict(calls)
        sim = _sim(owners=owners, chaos=chaos)
        self.assertEqual(sum(c.out_of_fuel for c in sim.cars), 0)              # nobody ran dry
        self.assertTrue(all(not c.road for c in sim.cars))                      # no car left on pit road
        for c in sim.cars:                                                      # no duplicate caution stops
            laps = [lap for lap, green, _ in c.stops if not green]
            for k in range(1, len(laps)):
                self.assertGreater(laps[k] - laps[k - 1], 1, (c.number, c.stops))



class HumanPlanSimTests(unittest.TestCase):
    """Stage 3: green-flag pit plans from the phone, checked over whole simulated races."""

    def _run(self, plans_for, seed=3, cautions=()):
        sim = RaceSim(DAY, 140, n=20, tank_laps=50, seed=seed, cautions=cautions,
                      params=StrategyParams(tank_laps=50, game_plan_off=True, fuel_margin=2))
        eng, tick = sim.engine, sim.engine.tick
        owners = {2: "phone"}

        def wrapped(snap, p):
            eng.human_owner = dict(owners)
            eng.human_plans = plans_for(eng, snap)
            return tick(snap, p)
        eng.tick = wrapped
        with mock.patch("aimod.strategy.time.time", return_value=1_000_000.0):
            sim.run()
        return sim

    def test_car_stops_on_the_planned_lap_and_the_plan_is_used_up(self):
        plan = HumanPlan(lap=25, service=RIGHT_SIDE, fuel_level=3, who="Mike", plan_id=1)
        sim = self._run(lambda eng, snap: {2: plan})
        c0 = next(c for c in sim.cars if c.idx == 2)
        green = [(lap, code) for lap, g, code in c0.stops if g]
        self.assertIn(green[0][0], (25, 26, 27), c0.stops)              # pits at the end of lap 25
        self.assertEqual(green[0][1], "F+R")                             # the plan's service
        st = sim.engine.state(2)
        self.assertEqual((st.human_plan_status, st.human_plan_id), ("done", 1))
        self.assertEqual(sum(1 for lap, g, code in c0.stops if g and 25 <= lap <= 27), 1)   # never re-applied
        self.assertEqual(sum(c.out_of_fuel for c in sim.cars), 0)

    def test_plan_past_the_fuel_is_moved_earlier(self):
        sim = self._run(lambda eng, snap: {2: HumanPlan(lap=120, plan_id=2)})
        c0 = sim.cars[0]
        self.assertFalse(c0.out_of_fuel)
        first = min(lap for lap, g, code in c0.stops)
        self.assertLess(first, 60)                                       # a 50-lap tank: well before lap 120

    def test_box_this_lap(self):
        def plans(eng, snap):
            return {2: HumanPlan(lap=1, plan_id=3)} if eng.lap_now >= 10 else {}
        sim = self._run(plans)
        first = min(lap for lap, g, code in sim.cars[0].stops)
        self.assertIn(first, (10, 11, 12))

    def test_no_plans_changes_nothing(self):
        base = self._run(lambda eng, snap: {})
        other = self._run(lambda eng, snap: {5: HumanPlan(lap=30, plan_id=9)})   # car 5 isn't owned
        self.assertEqual([c.stops for c in base.cars], [c.stops for c in other.cars])

    def test_monkey_plans_keep_the_race_sane(self):
        rng = random.Random(11)
        plans, ids = {}, [0]

        def chaos(eng, snap):
            if rng.random() < 0.02:
                ids[0] += 1
                i = rng.choice(range(2, 22, 2))
                lap = rng.choice([eng.lap_now - 5, eng.lap_now, eng.lap_now + rng.randint(1, 80), 999])
                plans[i] = HumanPlan(lap=lap, service=rng.choice([None, FOUR_TIRES, RIGHT_SIDE, FUEL_ONLY]),
                                     fuel_level=rng.choice([None, 1, 3, 5]), plan_id=ids[0])
            if rng.random() < 0.005 and plans:
                plans.pop(rng.choice(list(plans)))
            for i, pl in list(plans.items()):
                st = eng.cars.get(i)
                if st is not None and st.human_plan_id == pl.plan_id and st.human_plan_status in (
                        "done", "used under caution"):
                    plans.pop(i)                                  # what the Desk does once it sees the status
            return dict(plans)
        sim = RaceSim(DAY, 140, n=20, tank_laps=50, seed=5, cautions=(40, 90),
                      params=StrategyParams(tank_laps=50, game_plan_off=True, fuel_margin=2))
        eng, tick = sim.engine, sim.engine.tick

        def wrapped(snap, p):
            eng.human_owner = {i: f"phone {i}" for i in range(2, 22, 2)}
            eng.human_plans = chaos(eng, snap)
            return tick(snap, p)
        eng.tick = wrapped
        with mock.patch("aimod.strategy.time.time", return_value=1_000_000.0):
            sim.run()
        self.assertEqual(sum(c.out_of_fuel for c in sim.cars), 0)
        self.assertTrue(all(not c.road for c in sim.cars))
        for c in sim.cars:
            laps = sorted(lap for lap, g, code in c.stops)
            for k in range(1, len(laps)):
                self.assertGreater(laps[k] - laps[k - 1], 1, (c.number, c.stops))



class HumanPlanLearningTests(unittest.TestCase):
    def test_runs_a_person_planned_teach_nothing(self):
        from aimod.pace import PaceTracker
        pt = PaceTracker(worn_pct=1.0)
        for car_idx, learn in ((0, False), (1, True)):
            for lap in range(1, 30):
                pt.lap(car_idx, 10, 10 + lap, 30.0 + max(0, lap - 4) * 0.02, True, 0.01 * lap, 0.013 * lap, 30.0,
                       learn=learn)
        pt.flush()
        self.assertEqual(len(pt.run_ends), 1)                 # only the AIMOD-planned run
        self.assertEqual(len(pt.take_finished()), 1)          # and only it reaches the tire model


if __name__ == "__main__":
    unittest.main()
