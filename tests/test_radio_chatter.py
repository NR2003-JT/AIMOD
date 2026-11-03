"""AI drivers talking beyond their car's problems, and the user's own driver lines."""
import random
import unittest

from aimod.radio import DRIVER_LINES
from tests.test_radio import Race


class ChatterTest(unittest.TestCase):
    def setUp(self):
        random.seed(1)

    def race(self, car="48", **cfg):
        r = Race()
        r.radio.cfg["car"] = car
        r.radio.cfg.update(cfg)
        return r

    def test_ai_driver_shouts_when_passed(self):
        r = self.race()
        r.tick()
        for _ in range(3):
            r.green_lap(me_gain=0)
        cr = r.radio.cars[3]
        for _ in range(20):
            cr.chatter("passed")
        lines = set(DRIVER_LINES["passed"][0])
        self.assertTrue(any(s in lines for s in r.said), r.said)

    def test_never_for_your_own_car(self):
        r = self.race(car="player")
        r.tick()
        cr = r.radio.cars[0]
        cr.chatter("lead")
        self.assertFalse(any(s in DRIVER_LINES["lead"][0] for s in r.said), r.said)

    def test_custom_lines(self):
        r = self.race(custom_lines={"driver_win": ["Drinks are on me tonight, boys!"]}, custom_only=True)
        r.tick()
        r.radio.cars[3].chatter("win")
        self.assertIn("Drinks are on me tonight, boys!", r.said)

    def test_custom_lines_mixed_in(self):
        r = self.race(custom_lines={"driver_tight": ["Mine"]})
        r.tick()
        cr = r.radio.cars[3]
        said = {cr.driver_line("tight") for _ in range(200)}
        self.assertIn("Mine", said)
        self.assertTrue(said & set(DRIVER_LINES["tight"][0]))

    def test_scanner_only_tv_car_and_favourites(self):
        r = self.race(mode="scanner", scan="all", favourites="", follow_tv=True)
        r.tick(tv=2)                                            # the 24 is on TV
        r.radio.cars[3].chatter("lead")                         # the 48: not on TV, not a favourite
        r.radio.cars[2].chatter("lead")
        lead = set(DRIVER_LINES["lead"][0])
        self.assertEqual(sum(s in lead for s in r.said), 1, r.said)

    def test_custom_crew_line_with_placeholders(self):
        r = self.race(car="player", custom_lines={"caution": ["Yellow's out for the {car}, we're P {pos}!"]},
                      custom_only=True)
        r.tick()
        r.radio.cars[0].my_pos = 3
        r.flag = 0
        r.tick()
        self.assertIn("Yellow's out for the 9, we're P 3!", r.said)

    def test_zero_numbers(self):
        from aimod.carno import CarNo
        from aimod.radio import car_in, parse_cars
        self.assertTrue(car_in(CarNo(2001), parse_cars("24, 01")))
        self.assertFalse(car_in(CarNo(1), parse_cars("01")))
        self.assertTrue(car_in(CarNo(100), parse_cars("#00")))
        self.assertTrue(car_in(CarNo(2000), parse_cars("00")))

    def test_off(self):
        r = self.race()
        r.radio.cfg["cats"]["driver"] = False
        r.tick()
        r.radio.cars[3].chatter("win")
        self.assertFalse(any(s in DRIVER_LINES["win"][0] for s in r.said), r.said)


if __name__ == "__main__":
    unittest.main()
