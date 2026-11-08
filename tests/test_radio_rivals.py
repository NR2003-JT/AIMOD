"""Rivals on the radio: the crew (and driver) of the AI car you listen to on grudges, feuds, wrecks, retaliation."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_radio import Race                          # noqa: E402

from aimod.aggression import RivalEvent              # noqa: E402
from aimod.carno import CarNo                        # noqa: E402


def race_on_24():
    r = Race()
    r.radio.cfg["car"] = "24"                         # listen to AI car #24 (slot 2)
    r.engine.rival_events = []
    r.tick()
    r.said.clear()
    return r


def say(r, *events):
    r.engine.rival_events = list(events)
    r.tick()
    r.engine.rival_events = []
    return list(r.said)


def ev(kind, holder, other, level=1, idx=2):
    return idx, RivalEvent(kind, CarNo(holder), CarNo(other), level, 0.0, 11)


class RivalRadioTests(unittest.TestCase):
    def test_grudge_names_the_other_car(self):
        said = say(race_on_24(), ev("grudge", 24, 48))
        self.assertTrue(any("48" in s for s in said), said)

    def test_feud_names_the_painted_number(self):
        for _ in range(5):                                # (lines are picked at random)
            said = say(race_on_24(), ev("feud", 24, 2001, level=3))
            self.assertTrue(any("01" in s and "2001" not in s for s in said), said)

    def test_retaliation_driver_speaks(self):
        said = say(race_on_24(), ev("retaliate", 24, 48, level=3))
        self.assertGreaterEqual(len(said), 2, said)

    def test_target_of_retaliation_is_warned(self):
        said = say(race_on_24(), ev("retaliate", 48, 24, level=3))      # #48 is coming for #24
        self.assertTrue(any("48" in s and ("coming" in s or "out for you" in s) for s in said), said)

    def test_other_cars_events_not_said(self):
        said = say(race_on_24(), ev("grudge", 48, 3, idx=3))
        self.assertEqual(said, [])

    def test_runin_alone_says_nothing(self):
        self.assertEqual(say(race_on_24(), ev("runin", 24, 48)), [])


if __name__ == "__main__":
    unittest.main()
