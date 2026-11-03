"""Absolute pit control (2026-10-20, from PitProbe): the game's own repair stops, and cars already in a pit mode."""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aimod import memory_map as mm
from aimod.game import Game
from aimod.humancalls import HumanCall
from aimod.strategy import StrategyEngine, StrategyParams
from test_strategy import CAUTION, car, race, writes_for


class RepairNeedTests(unittest.TestCase):
    def test_game_repair_need_2_makes_aimod_call_the_repair_stop(self):
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=60)
        eng.race_started = True
        eng.tick(race([car(0, laps_done=30)], lap=31, total=200), p)
        eng.tick(race([car(0, laps_done=30, game_repair=2)], lap=31, total=200), p)
        self.assertTrue(eng.state(0).forced_damage)
        self.assertIs(eng.state(0).decision, True)

    def test_repair_need_1_is_a_damage_stop_like_stock(self):
        # stock NR2003 pits a car the moment its repair need is 1 (control race 2026-10-29)
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=60)
        eng.race_started = True
        eng.tick(race([car(0, laps_done=30)], lap=31, total=200), p)
        res = eng.tick(race([car(0, laps_done=30, game_repair=1)], lap=31, total=200), p)
        self.assertTrue(eng.state(0).forced_damage)
        self.assertIs(eng.state(0).decision, True)
        self.assertIn(1, writes_for(res, 0, mm.OFF_PITTING))      # called in under green

    def test_repair_need_1_under_caution_pits_even_when_aimod_would_keep_it_out(self):
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=60)
        eng.race_started = True
        st = eng.state(0)
        st.last_stop_lap, st.stops = 28, 1                   # just pitted: would stay out
        eng.tick(race([car(0, laps_done=30, fuel=95.0, fuel_cap=100.0)], lap=31, total=200), p)
        res = eng.tick(race([car(0, laps_done=30, fuel=95.0, fuel_cap=100.0, game_repair=1)], flag=CAUTION, lap=31,
                            total=200), p)
        self.assertIs(st.decision, True)
        self.assertNotIn(0, res.keep_out)

    def test_game_stop_flag_without_a_rule_is_a_repair_stop_not_a_penalty(self):
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=60)
        eng.race_started = True
        eng.tick(race([car(0, laps_done=30)], lap=31, total=200), p)
        eng.tick(race([car(0, laps_done=30, game_repair=1, game_penalty=True, pitting=1)], lap=31, total=200), p)
        self.assertNotIn("penalty", eng.state(0).game_hint)


class CommittedCarTests(unittest.TestCase):
    def setUp(self):
        self.eng = StrategyEngine(random.Random(1))
        self.p = StrategyParams(tank_laps=60)
        self.eng.race_started = True
        st = self.eng.state(0)
        st.last_stop_lap, st.stops = 78, 2                   # just pitted: AIMOD keeps it out
        self.cars = lambda **kw: [car(0, laps_done=80, fuel=95.0, fuel_cap=100.0, lap_dist=300, **kw)] + \
            [car(i, laps_done=80, lap_dist=300 + 20 * i) for i in range(1, 6)]
        self.eng.tick(race(self.cars(), lap=81, total=200), self.p)

    def test_pit_guard_leaves_a_car_already_in_a_pit_mode_alone(self):
        res = self.eng.tick(race(self.cars(), flag=CAUTION, lap=81, total=200), self.p)
        self.assertIn(0, res.keep_out)
        res = self.eng.tick(race(self.cars(pit_mode=True), flag=CAUTION, lap=81, total=200), self.p)
        self.assertNotIn(0, res.keep_out)

    def test_late_stay_out_for_a_committed_car_is_too_late(self):
        self.eng.human_owner = {0: "phone"}
        self.eng.tick(race(self.cars(), flag=CAUTION, lap=81, total=200), self.p)
        st = self.eng.state(0)
        st.decision = True                                   # its stop was called and the AI took it
        self.eng.human_calls = {0: HumanCall(self.eng.cautions, False, who="Mike")}
        self.eng.tick(race(self.cars(pit_mode=True), flag=CAUTION, lap=81, total=200), self.p)
        self.assertEqual(st.human_status, "too late: already heading to pit road")


if __name__ == "__main__":
    unittest.main()
