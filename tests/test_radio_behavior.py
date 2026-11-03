"""Driver behavior on the radio: the crew chief calls each behavior switch of the car you listen to."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_radio import Race

from aimod.behavior import Switch


def race_on_24():
    r = Race()
    r.radio.cfg["car"] = "24"                     # listen to AI car #24 (slot 2)
    r.engine.behavior_switches = []
    r.tick()
    r.said.clear()
    return r


def sw(new, code, old="", other=None, value=None, detail="", idx=2, number=24):
    return Switch(idx, number, f"Driver {number}", 11, old, new, "why", code, other, value, detail)


class BehaviorRadioTests(unittest.TestCase):
    def say(self, r, *switches):
        r.engine.behavior_switches = list(switches)
        r.tick()
        r.engine.behavior_switches = []
        return list(r.said)

    def test_fuel_short_call_says_how_short(self):
        r = race_on_24()
        said = self.say(r, sw("SAVE FUEL", "fuel_finish", value=2.4))
        self.assertTrue(any("2" in s and "fuel" in s.lower() for s in said), said)

    def test_lead_battle_names_the_other_car(self):
        r = race_on_24()
        said = self.say(r, sw("PUSH", "lead_defend", other=48))
        self.assertTrue(any("48" in s for s in said), said)

    def test_follow_in_the_draft(self):
        r = race_on_24()
        said = self.say(r, sw("SAVE FUEL", "follow", other=48, detail="make"))
        self.assertTrue(any("48" in s and ("sav" in s.lower() or "ride" in s.lower()) for s in said), said)

    def test_tires_gone_driver_calls_it_first(self):
        r = race_on_24()
        said = self.say(r, sw("SAVE TIRES", "tires_gone", value=12))
        self.assertGreaterEqual(len(said), 2, said)
        self.assertIn("tire", said[0].lower())             # the driver: tires are gone
        self.assertTrue(any("copy" in s.lower() or "ten-four" in s.lower() for s in said[1:]), said)

    def test_done_saving_only_under_green(self):
        r = race_on_24()
        said = self.say(r, sw("", "normal", old="SAVE FUEL"))
        self.assertTrue(any("fuel" in s.lower() for s in said), said)
        r = race_on_24()
        r.flag = 0                                          # a caution ends it: nothing to say about saving
        r.said.clear()
        said = self.say(r, sw("", "normal", old="SAVE FUEL"))
        self.assertFalse(any("good on fuel" in s.lower() or "fuel's good" in s.lower() for s in said), said)

    def test_spacing_two_laps_unless_urgent(self):
        r = race_on_24()
        self.say(r, sw("SAVE TIRES", "manage", value=30))
        r.said.clear()
        said = self.say(r, sw("COOL TIRES", "cool", old="SAVE TIRES", other=48))     # same lap: held back
        self.assertEqual(said, [])
        said = self.say(r, sw("PUSH", "lapping", old="COOL TIRES", other=48))        # urgent: said
        self.assertTrue(said, said)

    def test_other_cars_switches_are_not_said(self):
        r = race_on_24()
        said = self.say(r, sw("SAVE FUEL", "fuel_finish", value=2, idx=3, number=48))
        self.assertFalse(any("short" in s.lower() for s in said), said)

    def test_custom_line_replaces_the_built_in(self):
        r = race_on_24()
        r.radio.cfg["custom_lines"] = {"behave_late": ["Bring it home, {laps} to go."]}
        r.radio.cfg["custom_only"] = True
        said = self.say(r, sw("PUSH", "late", value=5))
        self.assertIn("Bring it home, 5 to go.", said)


if __name__ == "__main__":
    unittest.main()
