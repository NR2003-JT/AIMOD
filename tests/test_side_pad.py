"""Side padding (car + 0x1BEA) in AggressionWriter against fake memory: scale, put back, the game's own resets,
shadow, held cars, slot guards."""
import os
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "tools"))
sys.path.insert(0, HERE)

from fake_nr2003 import FakeNR2003                  # noqa: E402
from aimod import memory_map as mm                   # noqa: E402
from aimod.behavior_mem import AggressionWriter      # noqa: E402


def f32(v):
    return struct.unpack("<f", struct.pack("<f", v))[0]


class SidePad(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pm = FakeNR2003(n_ai=4, clock=lambda: 0.0)
        self.pm.process_id = 7
        for s in range(mm.NUM_CARS):
            self.set(s, 1.0)
        self.slots = [c.slot for c in self.pm.cars]
        self.w = AggressionWriter(os.path.join(self.tmp.name, "r.json"))
        self.w.capture(self.pm, list(range(mm.NUM_CARS)), ("o", "m"))
        self.a, self.b = self.slots[0], self.slots[1]

    def tearDown(self):
        self.tmp.cleanup()

    def addr(self, s):
        return AggressionWriter._side_addr(s)

    def set(self, s, v):
        self.pm._put(self.addr(s), struct.pack("<f", v))

    def get(self, s):
        return struct.unpack("<f", self.pm._get(self.addr(s), 4))[0]

    def test_scales_only_the_chosen_cars(self):
        n = self.w.apply_side(self.pm, {self.a: 0.7})
        self.assertEqual(n, 1)
        self.assertAlmostEqual(self.get(self.a), f32(0.7), places=6)
        for s in [self.b] + [x for x in range(mm.NUM_CARS) if x != self.a]:
            self.assertEqual(self.get(s), 1.0, s)

    def test_never_the_player_or_pace_car(self):
        self.w.apply_side(self.pm, {mm.PLAYER_SLOT: 0.5, mm.PACE_SLOT: 0.5})
        self.assertEqual(self.get(mm.PLAYER_SLOT), 1.0)
        self.assertEqual(self.get(mm.PACE_SLOT), 1.0)

    def test_put_back_when_dropped(self):
        self.w.apply_side(self.pm, {self.a: 0.7})
        self.w.apply_side(self.pm, {})
        self.assertEqual(self.get(self.a), 1.0)

    def test_repeat_is_stable_no_compounding(self):
        for _ in range(5):
            self.w.apply_side(self.pm, {self.a: 0.7})
        self.assertAlmostEqual(self.get(self.a), f32(0.7), places=6)

    def test_game_reset_becomes_the_new_own_value(self):
        self.w.apply_side(self.pm, {self.a: 0.7})
        self.set(self.a, 1.5)                          # the game's mode change reset it
        self.w.apply_side(self.pm, {self.a: 0.7})
        self.assertAlmostEqual(self.get(self.a), f32(1.05), places=6)
        self.w.apply_side(self.pm, {})
        self.assertEqual(self.get(self.a), f32(1.5))

    def test_reset_value_is_not_overwritten_on_put_back(self):
        self.w.apply_side(self.pm, {self.a: 0.7})
        self.set(self.a, 1.5)                          # game reset, we drop the scale: leave the game's value
        self.w.apply_side(self.pm, {})
        self.assertEqual(self.get(self.a), f32(1.5))

    def test_unknown_value_left_alone(self):
        self.set(self.a, 9.0)
        self.w.apply_side(self.pm, {self.a: 0.7})
        self.assertEqual(self.get(self.a), 9.0)

    def test_shadow_writes_nothing(self):
        self.pm.writes.clear()
        self.w.apply_side(self.pm, {self.a: 0.7}, shadow=True)
        self.assertEqual(self.get(self.a), 1.0)

    def test_restore_puts_everything_back(self):
        self.w.apply_side(self.pm, {self.a: 0.7, self.b: 0.7})
        self.w.restore(self.pm)
        self.assertEqual(self.get(self.a), 1.0)
        self.assertEqual(self.get(self.b), 1.0)

    def test_hold_releases_the_car(self):
        self.w.apply_side(self.pm, {self.a: 0.7})
        self.w.hold(self.pm, [self.a])
        self.assertEqual(self.get(self.a), 1.0)
        self.w.apply_side(self.pm, {self.a: 0.7})      # held: stays untouched
        self.assertEqual(self.get(self.a), 1.0)

    def test_no_game_no_crash(self):
        self.assertEqual(self.w.apply_side(None, {self.a: 0.7}), 0)


if __name__ == "__main__":
    unittest.main()
