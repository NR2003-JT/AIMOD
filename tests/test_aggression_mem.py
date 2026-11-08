"""behavior_mem.AggressionWriter against fake memory: capture, write, re-roll, restore, shadow, slot guards, recovery."""
import json
import math
import os
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "tools"))
sys.path.insert(0, HERE)

from fake_nr2003 import FakeNR2003                  # noqa: E402
from aimod import aggression as ag                   # noqa: E402
from aimod import memory_map as mm                   # noqa: E402
from aimod.behavior_mem import AggressionWriter      # noqa: E402


def f32(v):
    return struct.unpack("<f", struct.pack("<f", v))[0]


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "aggression_restore.json")
        self.pm = FakeNR2003(n_ai=6, clock=lambda: 0.0)       # frozen world: memory only changes when we say
        self.pm.process_id = 4242
        # slot 0 (player) and 1 (pace) get values too, so a write to them would show
        for s in (0, 1):
            self.pm._put(AggressionWriter.addr(s), struct.pack("<f", 1.11))
        self.slots = [c.slot for c in self.pm.cars]
        self.w = AggressionWriter(self.path)
        self.w.capture(self.pm, list(range(mm.NUM_CARS)), ("fakeoval", "fakemod"))
        self.pm.writes.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def mem(self, s):
        return struct.unpack("<f", self.pm._get(AggressionWriter.addr(s), 4))[0]

    def reroll_all(self):
        for c in self.pm.cars:
            self.pm._roll(c)


class Capture(Fixture):
    def test_takes_ai_cars_only(self):
        self.assertEqual(sorted(self.w.orig), self.slots)
        for s in self.slots:
            self.assertEqual(self.w.orig[s], self.mem(s))
            self.assertAlmostEqual(self.w.mean[s], self.pm.cars[s - 2].mean, 5)
        self.assertNotIn(0, self.w.orig)
        self.assertNotIn(1, self.w.orig)

    def test_bad_values_are_left_alone(self):
        w = AggressionWriter()
        self.pm._put(AggressionWriter.addr(self.slots[0]), struct.pack("<f", math.nan))
        self.pm._put(AggressionWriter.addr(self.slots[1]), struct.pack("<f", 0.0))
        w.capture(self.pm, self.slots)
        self.assertNotIn(self.slots[0], w.orig)
        self.assertNotIn(self.slots[1], w.orig)

    def test_capture_once(self):
        s = self.slots[0]
        self.w.apply(self.pm, {s: 2.0})
        self.w.capture(self.pm, self.slots)            # a second capture must not take our value as his own
        self.assertNotAlmostEqual(self.w.orig[s], 2.0, 3)


class Apply(Fixture):
    def test_writes_only_changes(self):
        a, b = self.slots[:2]
        ch = self.w.apply(self.pm, {a: 2.0, b: 1.5})
        self.assertEqual({c[0] for c in ch}, {a, b})
        self.assertEqual(self.mem(a), f32(2.0))
        n = len(self.pm.writes)
        self.assertEqual(self.w.apply(self.pm, {a: 2.0, b: 1.5}), [])
        self.assertEqual(len(self.pm.writes), n)

    def test_cars_left_out_go_back(self):
        a = self.slots[0]
        own = self.w.orig[a]
        self.w.apply(self.pm, {a: 2.5})
        self.w.apply(self.pm, {})
        self.assertEqual(self.mem(a), own)
        self.assertNotIn(a, self.w.ours)

    def test_clamp_and_bad_targets(self):
        a, b, c = self.slots[:3]
        self.w.apply(self.pm, {a: 9.0, b: math.nan, c: -1.0})
        self.assertEqual(self.mem(a), f32(ag.HARD_MAX))
        self.assertEqual(self.mem(b), self.w.orig[b])        # NaN -> his own value (no write)
        self.assertEqual(self.mem(c), 0.0)

    def test_driver_rolled_above_the_cap_keeps_his_value(self):
        w = AggressionWriter()
        s = self.slots[0]
        self.pm._put(AggressionWriter.addr(s), struct.pack("<f", 3.4))
        w.capture(self.pm, [s])
        w.apply(self.pm, {s: 3.4})
        self.assertEqual(self.mem(s), f32(3.4))

    def test_never_slot_0_or_1(self):
        self.w.apply(self.pm, {0: 2.5, 1: 2.5, self.slots[0]: 2.5})
        written = {(a - mm.CAR_BLOCK_0) // mm.CAR_STRIDE for a, _ in self.pm.writes}
        self.assertEqual(written, {self.slots[0]})
        self.assertEqual(self.mem(0), f32(1.11))
        self.assertEqual(self.mem(1), f32(1.11))

    def test_shadow_writes_nothing(self):
        a = self.slots[0]
        ch = self.w.apply(self.pm, {a: 2.5}, shadow=True)
        self.assertEqual(ch, [(a, self.w.orig[a], f32(2.5))])
        self.assertEqual(self.pm.writes, [])
        self.assertEqual(self.w.apply(self.pm, {a: 2.5}, shadow=True), [])          # no repeat
        self.assertEqual(self.w.apply(self.pm, {}, shadow=True), [(a, f32(2.5), self.w.orig[a])])
        self.assertFalse(os.path.exists(self.path))


class Reroll(Fixture):
    def test_game_roll_is_taken_as_his_own(self):
        a = self.slots[0]
        self.w.apply(self.pm, {a: 2.5})
        self.reroll_all()
        rolled = self.w.rerolls(self.pm)
        self.assertEqual({r[0] for r in rolled}, set(self.slots))
        self.assertEqual(self.w.orig[a], self.mem(a))
        self.assertNotIn(a, self.w.ours)
        n = len(self.pm.writes)
        self.assertEqual(self.w.restore(self.pm), 0)        # nothing written over the game's new roll
        self.assertEqual(len(self.pm.writes), n)

    def test_reapply_after_roll(self):
        a = self.slots[0]
        self.w.apply(self.pm, {a: 2.5})
        self.reroll_all()
        self.w.rerolls(self.pm)
        self.w.apply(self.pm, {a: 2.5})
        self.assertEqual(self.mem(a), f32(2.5))

    def test_no_roll_no_report(self):
        self.w.apply(self.pm, {self.slots[0]: 2.5})
        self.assertEqual(self.w.rerolls(self.pm), [])


class Restore(Fixture):
    def test_puts_every_original_back(self):
        own = {s: self.w.orig[s] for s in self.slots}
        self.w.apply(self.pm, {s: 2.2 for s in self.slots})
        self.assertTrue(os.path.exists(self.path))
        self.assertEqual(self.w.restore(self.pm), len(self.slots))
        for s in self.slots:
            self.assertEqual(self.mem(s), own[s])
        self.assertFalse(os.path.exists(self.path))
        self.assertFalse(self.w.captured)

    def test_restore_skips_a_car_the_game_changed(self):
        a, b = self.slots[:2]
        own_b = self.w.orig[b]
        self.w.apply(self.pm, {a: 2.2, b: 2.2})
        self.pm._put(AggressionWriter.addr(a), struct.pack("<f", 1.7))      # a new session's roll
        self.assertEqual(self.w.restore(self.pm), 1)
        self.assertEqual(self.mem(a), f32(1.7))
        self.assertEqual(self.mem(b), own_b)

    def test_without_the_game_the_file_stays(self):
        self.w.apply(self.pm, {self.slots[0]: 2.2})
        self.assertEqual(self.w.restore(None), 0)
        self.assertTrue(os.path.exists(self.path))


class Recover(Fixture):
    def test_file_holds_originals(self):
        a = self.slots[0]
        self.w.apply(self.pm, {a: 2.2})
        with open(self.path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["pid"], 4242)
        self.assertEqual(data["session"], ["fakeoval", "fakemod"])
        self.assertEqual(data["cars"][str(a)], [self.w.orig[a], f32(2.2)])

    def test_next_aimod_restores_same_game(self):
        a = self.slots[0]
        own = self.w.orig[a]
        self.w.apply(self.pm, {a: 2.2})                 # ... and AIMOD crashes here
        fresh = AggressionWriter(self.path)
        self.assertEqual(fresh.recover(self.pm, ("fakeoval", "fakemod")), 1)
        self.assertEqual(self.mem(a), own)
        self.assertFalse(os.path.exists(self.path))

    def test_other_game_or_session_is_ignored(self):
        a = self.slots[0]
        self.w.apply(self.pm, {a: 2.2})
        self.pm.process_id = 99                          # NR2003 was restarted
        self.assertEqual(AggressionWriter(self.path).recover(self.pm, ("fakeoval", "fakemod")), 0)
        self.assertEqual(self.mem(a), f32(2.2))
        self.assertFalse(os.path.exists(self.path))
        self.pm.process_id = 4242
        self.w.apply(self.pm, {a: 2.3})
        self.assertEqual(AggressionWriter(self.path).recover(self.pm, ("othertrack", "fakemod")), 0)

    def test_garbage_file(self):
        with open(self.path, "w") as f:
            f.write("{nope")
        self.assertEqual(AggressionWriter(self.path).recover(self.pm, ()), 0)
        self.assertFalse(os.path.exists(self.path))


class ExeCheck(unittest.TestCase):
    def test_build_check(self):
        pm = FakeNR2003(n_ai=2, clock=lambda: 0.0)
        self.assertFalse(AggressionWriter().available(pm))
        addr, want = mm.AGG_CODE_CHECK
        pm._put(addr, want)
        self.assertTrue(AggressionWriter().available(pm))
        self.assertFalse(AggressionWriter().available(None))


if __name__ == "__main__":
    unittest.main()


class Showdown(Fixture):
    def test_hold_gives_the_value_back_and_leaves_the_car_alone(self):
        s = self.slots[0]
        own = self.w.orig[s]
        self.w.apply(self.pm, {s: 2.9})
        self.assertAlmostEqual(self.mem(s), 2.9, 5)
        self.assertEqual(self.w.hold(self.pm, [s]), 1)
        self.assertAlmostEqual(self.mem(s), own, 5)                 # his own value is back
        self.assertNotIn(s, self.w.orig)
        self.pm.writes.clear()
        self.assertEqual(self.w.capture(self.pm, self.slots, ("fakeoval", "fakemod")), 0)    # not taken again
        self.w.apply(self.pm, {s: 3.0})
        self.assertEqual(self.pm.writes, [])                         # never written while held
        self.pm._put(AggressionWriter.addr(s), struct.pack("<f", 2.2))       # the probe's value / put back
        self.w.unhold()
        self.assertEqual(self.w.capture(self.pm, self.slots, ("fakeoval", "fakemod")), 1)
        self.assertAlmostEqual(self.w.orig[s], 2.2, 5)               # what the probe left is his own now

    def test_hold_never_takes_the_player_or_pace_slot(self):
        self.w.hold(self.pm, [0, 1])
        self.assertEqual(self.w.held, set())


class LockFile(unittest.TestCase):
    def setUp(self):
        from aimod import showdown_lock
        self.sl = showdown_lock
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("AIMOD_SHOWDOWN_LOCK")
        os.environ["AIMOD_SHOWDOWN_LOCK"] = os.path.join(self.tmp.name, "sub", "showdown.lock")

    def tearDown(self):
        if self.old is None:
            os.environ.pop("AIMOD_SHOWDOWN_LOCK", None)
        else:
            os.environ["AIMOD_SHOWDOWN_LOCK"] = self.old
        self.tmp.cleanup()

    def test_write_read_remove(self):
        self.assertEqual(self.sl.read(), set())
        self.assertTrue(self.sl.write([24, 20]))
        self.assertEqual(self.sl.read(), {24, 20})
        self.sl.remove()
        self.assertEqual(self.sl.read(), set())

    def test_stale_lock_is_ignored(self):
        self.sl.write([24])
        import time
        self.assertEqual(self.sl.read(now=time.time() + self.sl.FRESH_S + 5), set())

    def test_garbage_is_ignored(self):
        self.sl.write([24])
        with open(self.sl.lock_path(), "w") as f:
            f.write("{nope")
        self.assertEqual(self.sl.read(), set())
