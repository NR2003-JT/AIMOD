"""AggressionWriter.apply_side against fake memory: the side-room base (car + OFF_SIDE_BASE) of the cars being pushed
is scaled, and goes back to his own when the battle ends, the mode changes, the game resets it or AIMOD restores."""
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
from aimod.presets import Preset                     # noqa: E402

OWN = 0.8


def f32(v):
    return struct.unpack("<f", struct.pack("<f", v))[0]


class SideRoom(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pm = FakeNR2003(n_ai=6, clock=lambda: 0.0)
        self.pm.process_id = 4242
        self.w = AggressionWriter(os.path.join(self.tmp.name, "r.json"))
        self.w.capture(self.pm, list(range(mm.NUM_CARS)), ("fakeoval", "fakemod"))
        self.slots = sorted(self.w.orig)
        for s in range(mm.NUM_CARS):
            self.pm._put(AggressionWriter._side_addr(s), struct.pack("<f", OWN))
        self.pm.writes.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def side(self, s):
        return struct.unpack("<f", self.pm._get(AggressionWriter._side_addr(s), 4))[0]

    def put(self, s, v):
        self.pm._put(AggressionWriter._side_addr(s), struct.pack("<f", v))

    def test_scales_only_the_cars_asked_for(self):
        a, b = self.slots[:2]
        self.w.held.clear()
        self.assertEqual(self.w.apply_side(self.pm, {a: 0.7}), 1)
        self.assertAlmostEqual(self.side(a), f32(0.7 * OWN), 6)
        self.assertAlmostEqual(self.side(b), OWN, 6)

    def test_nothing_written_in_shadow_or_when_steady(self):
        a = self.slots[0]
        self.assertEqual(self.w.apply_side(self.pm, {a: 0.7}, shadow=True), 0)
        self.assertEqual(self.w.apply_side(self.pm, {}), 0)
        self.assertEqual(self.pm.writes, [])
        self.w.apply_side(self.pm, {a: 0.7})
        n = len(self.pm.writes)
        self.w.apply_side(self.pm, {a: 0.7})               # steady: no repeated writes
        self.assertEqual(len(self.pm.writes), n)

    def test_back_to_his_own_when_the_battle_ends(self):
        a = self.slots[0]
        self.w.apply_side(self.pm, {a: 0.5})
        self.assertAlmostEqual(self.side(a), 0.5 * OWN, 5)
        self.w.apply_side(self.pm, {})
        self.assertAlmostEqual(self.side(a), OWN, 6)

    def test_a_value_the_game_changed_is_his_new_own(self):
        a = self.slots[0]
        self.w.apply_side(self.pm, {a: 0.5})
        self.put(a, 1.2)                                   # the game set it again
        self.w.apply_side(self.pm, {a: 0.5})
        self.assertAlmostEqual(self.side(a), f32(0.5 * 1.2), 5)
        self.w.apply_side(self.pm, {})
        self.assertAlmostEqual(self.side(a), 1.2, 6)

    def test_put_back_does_not_overwrite_a_value_the_game_set(self):
        a = self.slots[0]
        self.w.apply_side(self.pm, {a: 0.5})
        self.put(a, 1.5)
        self.w.apply_side(self.pm, {})
        self.assertAlmostEqual(self.side(a), 1.5, 6)

    def test_implausible_values_are_left_alone(self):
        a = self.slots[0]
        for bad in (0.0, 9.0, float("nan")):
            self.put(a, bad)
            self.pm.writes.clear()
            self.w.apply_side(self.pm, {a: 0.5})
            self.assertEqual(self.pm.writes, [])

    def test_restore_puts_it_back(self):
        a, b = self.slots[:2]
        self.w.apply_side(self.pm, {a: 0.5, b: 0.5})
        self.w.restore(self.pm)
        self.assertAlmostEqual(self.side(a), OWN, 6)
        self.assertAlmostEqual(self.side(b), OWN, 6)

    def test_a_car_a_showdown_probe_takes_gets_his_own_back(self):
        a, b = self.slots[:2]
        self.w.apply_side(self.pm, {a: 0.5, b: 0.5})
        self.w.hold(self.pm, [a])
        self.assertAlmostEqual(self.side(a), OWN, 6)
        self.assertAlmostEqual(self.side(b), 0.5 * OWN, 5)

    def test_player_and_pace_car_never_touched(self):
        self.w.apply_side(self.pm, {0: 0.5, 1: 0.5})
        self.assertAlmostEqual(self.side(0), OWN, 6)
        self.assertAlmostEqual(self.side(1), OWN, 6)


class SidePadPreset(unittest.TestCase):
    def test_default_and_clamps(self):
        self.assertEqual(Preset().agg_side_pad, 70)
        self.assertEqual(Preset.from_dict({"agg_side_pad": 5}).agg_side_pad, 30)
        self.assertEqual(Preset.from_dict({"agg_side_pad": 400}).agg_side_pad, 100)
        self.assertEqual(Preset.from_dict({"agg_side_pad": "x"}).agg_side_pad, 70)
        self.assertEqual(Preset.from_dict(Preset(agg_side_pad=55).to_dict()).agg_side_pad, 55)


if __name__ == "__main__":
    unittest.main()
