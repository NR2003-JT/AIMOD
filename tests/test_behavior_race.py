"""Whole races with driver behavior on (racesim + the behavior engine acting on the simulated cars).

The sim gets the behavior effects the way NR2003 does: grip / power change the lap time, the wear share slows or
speeds the tire aging, the fuel share the burn. Checks the calls happen, fit the track type, and never run a car dry.

Run: python -m unittest discover -s tests
"""
import collections
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from racesim import RaceSim, Track

from aimod import memory_map as mm
from aimod.behavior import COOL, NEUTRAL, PUSH, SAVE_FUEL, SAVE_TIRES, BehaviorConfig, BehaviorEngine, gather
from aimod.strategy import SHORT_TRACK, SUPERSPEEDWAY, StrategyParams


class BehaviorSim(RaceSim):
    def __init__(self, *a, behavior=True, teams_of=3, **kw):
        super().__init__(*a, **kw)
        self.behavior = BehaviorEngine(BehaviorConfig(enabled=behavior))
        self.effect = {}
        self.teams_of = teams_of
        self.mode_laps = collections.Counter()          # (mode) -> car-ticks spent in it
        self.reasons = collections.Counter()
        self.switches = []

    def lap_time(self, c):
        e = self.effect.get(c.idx, NEUTRAL)
        return super().lap_time(c) * (1.0 - 0.6 * (e.traction - 1.0) - 0.4 * (e.power - 1.0))

    def step(self):
        before = {c.idx: (c.fuel, c.age_l, c.age_r, c.road) for c in self.cars}
        super().step()
        for c in self.cars:
            e = self.effect.get(c.idx)
            f0, al, ar, road = before[c.idx]
            if e is None or road or c.road or self.flag != mm.FLAG_GREEN:
                continue
            if c.fuel < f0 and not c.out_of_fuel:
                c.fuel = f0 - (f0 - c.fuel) * e.fuel
            if c.age_l > al:
                c.age_l = al + (c.age_l - al) * e.wear
            if c.age_r > ar:
                c.age_r = ar + (c.age_r - ar) * e.wear

    def snapshot(self):
        snap = super().snapshot()
        for sc in snap.cars:
            sc.team = (sc.number - 1) // self.teams_of if self.teams_of else -1
        return snap

    def run(self, until_lap=None, on_lap=None):
        end = until_lap or self.laps
        self.events = []
        while int(self.leader().progress) < end:
            snap = self.snapshot()
            res = self.engine.tick(snap, self.p)
            ctx, cars = gather(self.engine, res, snap, self.p)
            out = self.behavior.tick(ctx, cars)
            self.engine.behavior_cars = set(out.effects)
            self.effect = out.effects
            self.switches += out.switches
            for mode, why in out.modes.values():
                self.mode_laps[mode] += 1
                self.reasons[why.split(" #")[0].split(" (")[0]] += 1
            self.events += res.events
            self.apply(res)
            for _ in range(int(max(1, 1.0 / self.dt))):
                self.step()
        return self


def charlotte(**kw):
    return BehaviorSim(Track("charlotte", 2414, 33.7, pit_road_s=40.0), 170, n=24, tank_laps=71,
                       params=StrategyParams(tank_laps=71, game_plan_off=True, fuel_margin=2, stages=3,
                                             stage_ends=(84, 168)), cautions=(85,), **kw).run()


def talladega(**kw):
    return BehaviorSim(Track("talladega", 4280, 50.0, pit_road_s=45.0), 188, n=24, tank_laps=40, slope=0.005,
                       wear_per_lap=0.002, drafting=True, cautions=(61, 121),
                       params=StrategyParams(tank_laps=40, game_plan_off=True, fuel_margin=1,
                                             track_type=SUPERSPEEDWAY, stages=3, stage_ends=(60, 120)), **kw).run()


class BehaviorRaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cha = charlotte()
        cls.cha_off = charlotte(behavior=False)
        cls.tal = talladega()

    def test_intermediate_uses_every_mode_and_nobody_runs_dry(self):
        sim = self.cha
        self.assertEqual(sum(c.out_of_fuel for c in sim.cars), 0)
        for m in (SAVE_TIRES, PUSH):
            self.assertGreater(sim.mode_laps[m], 0, (m, sim.reasons))
        self.assertGreater(len(sim.switches), 20)

    def test_intermediate_stops_stay_sane(self):
        on = sorted(c.green_stops for c in self.cha.cars)
        off = sorted(c.green_stops for c in self.cha_off.cars)
        self.assertTrue(all(1 <= s <= 3 for s in on), on)
        self.assertLessEqual(sum(on), sum(off) + 2, (on, off))

    def test_superspeedway_rides_saves_fuel_together_and_never_saves_tires(self):
        sim = self.tal
        self.assertEqual(sum(c.out_of_fuel for c in sim.cars), 0)
        self.assertEqual(sim.mode_laps[SAVE_TIRES] + sim.mode_laps[COOL], 0, sim.reasons)
        self.assertGreater(sim.mode_laps[SAVE_FUEL], 0, sim.reasons)
        self.assertTrue(any(r.startswith("with") for r in sim.reasons), sim.reasons)
        self.assertIn("pushing for the stage end", sim.reasons)

    def test_short_track_runs_clean(self):
        sim = BehaviorSim(Track("martinsville", 846, 20.0, pit_road_s=26.0, pit_frac=0.25, caution_lap_s=70.0), 250,
                          n=24, tank_laps=130, slope=0.02, cautions=(60, 140, 220),
                          params=StrategyParams(tank_laps=130, game_plan_off=True, fuel_margin=2,
                                                track_type=SHORT_TRACK)).run()
        self.assertEqual(sum(c.out_of_fuel for c in sim.cars), 0)
        self.assertGreater(sim.mode_laps[PUSH], 0, sim.reasons)


if __name__ == "__main__":
    unittest.main()
