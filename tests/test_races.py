"""Whole-race simulations (tests/racesim.py): the planner must reach the finish on fuel, keep to the
fewest green stops and not put identical cars laps down by strategy alone. Slower than the unit tests."""
import collections
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from racesim import RaceSim, Track
from aimod.strategy import ROAD_COURSE, SHORT_TRACK, SUPERSPEEDWAY, StrategyParams

SAME = dict(skill_spread=0.0, managers=(1.0,))


class RaceTests(unittest.TestCase):
    def test_charlotte_stage_race(self):
        sim = RaceSim(Track("charlotte", 2414, 33.7, pit_road_s=40.0), 170, n=24, tank_laps=71,
                      params=StrategyParams(tank_laps=71, game_plan_off=True, fuel_margin=2, stages=3,
                                            stage_ends=(84, 168)), cautions=(85,), **SAME).run()
        self.assertEqual(sum(c.out_of_fuel for c in sim.cars), 0)
        self.assertTrue(all(c.green_stops == 2 for c in sim.cars), [c.green_stops for c in sim.cars])
        self.assertEqual(set(sim.laps_down().values()), {0})

    def test_martinsville_no_green_stops_with_cautions(self):
        sim = RaceSim(Track("martinsville", 846, 20.0, pit_road_s=26.0, pit_frac=0.25, caution_lap_s=70.0), 250,
                      n=24, tank_laps=130, slope=0.02, cautions=(60, 140, 220),
                      params=StrategyParams(tank_laps=130, game_plan_off=True, fuel_margin=2, track_type=SHORT_TRACK),
                      **SAME).run()
        self.assertEqual(sum(c.out_of_fuel for c in sim.cars), 0)
        self.assertEqual(sum(c.green_stops for c in sim.cars), 0)

    def test_road_course_final_tank_to_the_finish(self):
        sim = RaceSim(Track("watkins", 3900, 72.0, pit_road_s=55.0, pit_frac=0.08), 90, n=24, tank_laps=32, slope=0.05,
                      params=StrategyParams(tank_laps=32, game_plan_off=True, fuel_margin=1, track_type=ROAD_COURSE,
                                            stages=3, stage_ends=(20, 40)), cautions=(21, 41), **SAME).run()
        self.assertEqual(sum(c.out_of_fuel for c in sim.cars), 0)
        self.assertTrue(all(c.green_stops == 1 for c in sim.cars), [c.green_stops for c in sim.cars])
        self.assertTrue(all(f < 9 for f in sim.finish_fuel_laps()))

    def test_superspeedway_blocks_and_fuel(self):
        sim = RaceSim(Track("talladega", 4280, 50.0, pit_road_s=45.0), 188, n=24, tank_laps=40, slope=0.005,
                      wear_per_lap=0.002, drafting=True, cautions=(61, 121),
                      params=StrategyParams(tank_laps=40, game_plan_off=True, fuel_margin=1, track_type=SUPERSPEEDWAY,
                                            stages=3, stage_ends=(60, 120)), **SAME).run()
        self.assertEqual(sum(c.out_of_fuel for c in sim.cars), 0)
        notes = collections.Counter("block" in e.note for e in sim.events if e.green)
        self.assertGreater(notes[True], 10)
        self.assertFalse(any("emergency" in e.note for e in sim.events))


if __name__ == "__main__":
    unittest.main()
