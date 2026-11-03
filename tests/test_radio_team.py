"""Your crew chief on your AI teammates: pit plans, caution calls, pace."""
import time
import unittest

from aimod.strategy import FOUR_TIRES, STAGE_BREAK, CarState
from tests.test_radio import CAUTION, GREEN, ONE_TO_GO, Race


class TeamRace(Race):
    """#48 is the player's (#9) teammate; #24 isn't."""

    def __init__(self):
        super().__init__()
        self.st48, self.st24 = CarState(), CarState()
        self.engine.cars = {3: self.st48, 2: self.st24}
        self.status48 = ""

    def car(self, slot, number, controlled=True):
        c = super().car(slot, number, controlled)
        c.team = 0 if number in (48, 9) else 1
        return c

    def tick(self, total=100, tv=None):
        real = self.radio.update

        def update(snap, res, engine, p, tv_slot=None):
            for r in res.rows:
                if r.idx == 3:
                    r.status = self.status48
            return real(snap, res, engine, p, tv_slot)
        self.radio.update = update
        try:
            super().tick(total, tv)
        finally:
            self.radio.update = real

    def team_lines(self):
        return [s for s in self.said if "48" in s or "24" in s and "teammate" in s]


class TeammatesTest(unittest.TestCase):
    def test_plan_said_once_and_moves(self):
        r = TeamRace()
        r.st48.planned_lap, r.st24.planned_lap = 40, 30
        r.tick()
        r.tick()
        plans = [s for s in r.said if "teammate" in s]
        self.assertEqual(plans, ["Your teammate the 48 is planning to pit around lap 40."], r.said)
        r.st48.planned_lap = 41                                   # a 1-lap revision isn't news
        for _ in range(3):
            r.green_lap()
        self.assertFalse(any("moved" in s or "pushed" in s for s in r.said), r.said)
        r.st48.planned_lap = 37
        r.tick()
        self.assertIn("The 48 has moved his stop up to lap 37.", r.said)
        self.assertFalse(any("the 24" in s.lower() for s in r.said), r.said)     # not a teammate

    def test_stage_break(self):
        r = TeamRace()
        r.st48.planned_lap = STAGE_BREAK
        r.tick()
        self.assertIn("Your teammate the 48 is running to the stage break.", r.said)

    def test_caution_waits_for_the_teammate(self):
        r = TeamRace()
        r.tick()
        r.flag = CAUTION
        r.tick()
        r.flag, r.engine.pits_open_at = 1, 5.0
        r.status48 = "WAITS FOR #9: teammate's call"
        r.st48.decision, r.st48.service = True, FOUR_TIRES
        r.tick()
        self.assertFalse(any("Under this caution" in s for s in r.said), r.said)
        r.status48 = ""
        r.tick()
        self.assertIn("Under this caution the 48 is pitting for four tires.", r.said)
        r.tick()
        self.assertEqual(sum("the 48 is pitting" in s for s in r.said), 1, r.said)

    def test_stay_out_holds_first(self):
        r = TeamRace()
        r.tick()
        r.flag = CAUTION
        r.tick()
        r.flag, r.engine.pits_open_at = 1, 5.0
        r.st48.decision = False
        r.tick()
        self.assertFalse(any("staying out" in s for s in r.said), r.said)
        r.radio.cars[0].mate_stay[3] = time.monotonic() - 10
        r.tick()
        self.assertIn("Under this caution the 48 is staying out.", r.said)

    def test_pace(self):
        r = TeamRace()
        r.engine.lap_now = 30
        r.tick()
        for _ in range(30):
            r.green_lap(me_gain=200)                              # the 48 runs 0.2 s a lap slower
        self.assertTrue(any(s == "The 48 is running about 2 tenths slower than you." for s in r.said), r.said)

    def test_teammate_adjustment(self):
        from aimod.adjust import Adjustment
        from aimod.strategy import PitEvent
        r = TeamRace()
        r.tick()
        ev = PitEvent(0, 3, 48, "Driver 48", 40, "4 TIRES", "GREEN", False, 1, fuel=True)
        ev.adjust = Adjustment("wedge", -5, "wedge -5 lb", "We're gonna take a round out.", diagnosis="tight")
        ev24 = PitEvent(0, 2, 24, "Driver 24", 40, "4 TIRES", "GREEN", False, 1, fuel=True)
        ev24.adjust = Adjustment("wedge", 5, "wedge +5 lb", "In a round.", diagnosis="loose")
        r.events = [ev, ev24]
        r.tick()
        self.assertIn("The 48's taking wedge out this stop, he's been tight.", r.said)
        self.assertFalse(any(s.startswith("The 24's") for s in r.said), r.said)    # not a teammate

    def test_off(self):
        r = TeamRace()
        r.radio.cfg["cats"]["team"] = False
        r.st48.planned_lap = 40
        r.tick()
        self.assertFalse(any("teammate" in s for s in r.said), r.said)


if __name__ == "__main__":
    unittest.main()
