"""Crew chief on a phone (beta), stage 1: the read-only view, and that it changes nothing."""
import json
import unittest

from aimod.carno import CarNo
from aimod.crewview import CrewView
from aimod.strategy import CarState
from tests.test_radio import Race


class CrewViewTest(unittest.TestCase):
    def test_view(self):
        r = Race()
        st = CarState()
        st.planned_lap, st.plan_note, st.style = 40, "split", "split"
        r.engine.cars = {3: st}
        r.tick()
        view = CrewView()
        for _ in range(3):
            r.green_lap()
        snap_cars = None

        captured = {}
        real = r.radio.update

        def update(snap, res, engine, p, tv=None):
            captured["v"] = view.build(snap, res, engine, r.radio)
            return real(snap, res, engine, p, tv)
        r.radio.update = update
        r.green_lap()
        v = captured["v"]
        json.dumps(v)                                        # serialisable
        self.assertTrue(v["enabled"])
        self.assertEqual(v["flag"], "green")
        by_num = {c["number"]: c for c in v["cars"]}
        self.assertEqual(set(by_num), {"24", "48", "9"})
        self.assertTrue(by_num["9"]["player"])
        self.assertEqual(by_num["48"]["plan"]["lap"], 40)
        self.assertEqual(by_num["48"]["laps"][-1], 20.0)     # line to line
        self.assertEqual([c["pos"] for c in v["cars"]], [1, 2, 3])

    def test_number_label(self):
        r = Race()
        r.tick()
        view = CrewView()
        snap_cars = []

        class S:                                             # a 01 car
            pass
        from aimod.strategy import CarSnapshot, RaceSnapshot, TickResult
        c = CarSnapshot(2, True, number=CarNo(2001), name="Zero One")
        v = view.build(RaceSnapshot(7, 3, 40, 10, 100, [c]), TickResult(rows=[]), r.engine)
        self.assertEqual(v["cars"][0]["number"], "01")


if __name__ == "__main__":
    unittest.main()
