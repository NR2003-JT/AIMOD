"""Crew chief beta: a car run from a phone is always on the radio, so its crew chief hears it."""
import random
import unittest

from aimod.radio import DRIVER_LINES
from tests.test_radio import Race


class CrewRadioTest(unittest.TestCase):
    def setUp(self):
        random.seed(1)

    def test_scanner_adds_the_phones_car_and_it_talks(self):
        r = Race()
        r.radio.cfg.update(mode="scanner", scan="numbers", scan_numbers="24", follow_tv=False)
        r.tick()
        self.assertNotIn(3, r.radio.cars)                       # the 48 isn't on the scanner
        r.engine.human_owner = {3: "Dave"}
        r.tick()
        self.assertIn(3, r.radio.cars)
        r.radio.cars[3].chatter("passed")                       # chatty like a favourite
        self.assertIn(48, r.radio.voice.numbers)

    def test_one_car_mode_adds_it_without_its_flag_calls(self):
        r = Race()
        r.radio.cfg["car"] = "player"
        r.engine.human_owner = {3: "Dave"}
        r.tick()
        self.assertEqual(set(r.radio.cars), {0, 3})
        cr = r.radio.cars[3]
        cr.say("flags", "spotter", "Green, green, green.")
        self.assertNotIn(48, r.radio.voice.numbers)              # one spotter calls the flags
        cr.chatter("lead")
        self.assertIn(48, r.radio.voice.numbers)
        self.assertTrue(any(s in DRIVER_LINES["lead"][0] for s in r.said))

    def test_switch_off_and_asleep_phones(self):
        r = Race()
        r.radio.cfg.update(mode="scanner", scan="numbers", scan_numbers="24", follow_tv=False, crew_scan=False)
        r.engine.human_owner = {3: "Dave"}
        r.tick()
        self.assertNotIn(3, r.radio.cars)
        r.radio.cfg["crew_scan"] = True
        r.engine.human_owner = {}                               # the phone is away: nobody to hear it
        r.tick()
        self.assertNotIn(3, r.radio.cars)


if __name__ == "__main__":
    unittest.main()
