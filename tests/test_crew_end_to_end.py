"""Crew chief on a phone (beta), end to end: a phone's call through the Desk into the real strategy engine,
and back out in the crew view."""
import os
import random
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aimod import memory_map as mm
from aimod.crewview import CrewView
from aimod.humancalls import Desk
from aimod.strategy import RIGHT_SIDE, StrategyEngine, StrategyParams
from test_strategy import CAUTION, car, race, writes_for


class EndToEnd(unittest.TestCase):
    def setUp(self):
        pin = mock.patch("aimod.strategy.time.time", return_value=1_000_000.0)   # the engine reseeds at flag changes
        pin.start()
        self.addCleanup(pin.stop)

    def test_phone_call_reaches_the_stop(self):
        now = [1000.0]
        eng = StrategyEngine(random.Random(1), clock=lambda: now[0])
        p = StrategyParams(tank_laps=60)
        eng.race_started = True
        desk = Desk()
        field = lambda **kw: [car(0, laps_done=40, lap_dist=300, fuel=60.0, fuel_cap=100.0, **kw)] + \
            [car(i, laps_done=40, lap_dist=300 + 30 * i, fuel=60.0, fuel_cap=100.0) for i in range(1, 6)]

        def tick(flag, **kw):
            snap = race(field(on_pit_road=False, **kw), flag=flag, lap=41, total=200)
            desk.apply(eng, snap)                        # what the app does around every engine tick
            res = eng.tick(snap, p)
            desk.sync(eng, snap)
            return snap, res

        snap, _res = tick(mm.FLAG_GREEN)
        num = str(snap.cars[0].number)
        token = desk.claim(num, "Mike")["token"]
        now[0] += 10
        snap, res = tick(CAUTION)                         # caution: AIMOD waits for Mike
        view = CrewView().build(snap, res, eng, None, desk)
        mine = [c for c in view["cars"] if c["number"] == num][0]
        self.assertTrue(mine["human"]["status"].startswith("WAITS FOR CREW CHIEF (Mike)"))
        self.assertIsNotNone(mine["human"]["deadline_s"])
        self.assertTrue(desk.call(token, view["caution_no"], 1, "R", 3)["ok"])
        snap, res = tick(CAUTION)
        self.assertIn(1, writes_for(res, 0, mm.OFF_PITTING))
        snap, res = tick(CAUTION, pitting=1)
        ev = [e for e in res.events if e.idx == 0][0]
        self.assertEqual(ev.service, RIGHT_SIDE)
        self.assertIn("crew chief call (Mike)", ev.note)
        view = CrewView().build(snap, res, eng, None, desk)
        mine = [c for c in view["cars"] if c["number"] == num][0]
        self.assertEqual(mine["human"]["status"], "accepted")
        self.assertEqual(mine["human"]["owner"], "Mike")

    def test_phone_plan_box_this_lap(self):
        now = [1000.0]
        eng = StrategyEngine(random.Random(1), clock=lambda: now[0])
        p = StrategyParams(tank_laps=60)
        eng.race_started = True
        desk = Desk()
        field = lambda **kw: [car(0, laps_done=40, lap_dist=300, fuel=60.0, fuel_cap=100.0, **kw)] +             [car(i, laps_done=40, lap_dist=300 + 30 * i, fuel=60.0, fuel_cap=100.0) for i in range(1, 6)]

        def tick(**kw):
            snap = race(field(on_pit_road=False, **kw), flag=mm.FLAG_GREEN, lap=41, total=200)
            desk.apply(eng, snap)
            res = eng.tick(snap, p)
            desk.sync(eng, snap)
            return snap, res

        snap, _res = tick()
        num = str(snap.cars[0].number)
        token = desk.claim(num, "Mike")["token"]
        self.assertTrue(desk.plan(token, 0, "R", 2)["ok"])          # box this lap
        snap, res = tick()
        self.assertIn(1, writes_for(res, 0, mm.OFF_PITTING))
        view = CrewView().build(snap, res, eng, None, desk)
        mine = [c for c in view["cars"] if c["number"] == num][0]
        self.assertTrue(mine["human"]["plan_status"].startswith("accepted"), mine["human"])
        self.assertEqual(mine["human"]["plan"]["service"], RIGHT_SIDE)


if __name__ == "__main__":
    unittest.main()
