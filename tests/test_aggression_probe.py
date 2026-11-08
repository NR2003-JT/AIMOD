"""tools/aggression_probe.py against the fake NR2003 (tools/fake_nr2003.py): put back, slot guards, re-roll stop."""
import contextlib
import io
import os
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "tools"))
sys.path.insert(0, HERE)

import aggression_probe as ap          # noqa: E402
from aimod import memory_map as mm     # noqa: E402


def work(pm, slot):
    return struct.unpack("<f", pm._get(mm.CAR_BLOCK_0 + slot * mm.CAR_STRIDE + ap.OFF_AGG_WORK, 4))[0]


def slot_of(pm, number):
    return next(c.slot for c in pm.cars if c.number == number)


class ProbeDryRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lock = os.path.join(self.tmp.name, "showdown.lock")
        self.old_lock = os.environ.get("AIMOD_SHOWDOWN_LOCK")
        os.environ["AIMOD_SHOWDOWN_LOCK"] = self.lock

    def tearDown(self):
        if self.old_lock is None:
            os.environ.pop("AIMOD_SHOWDOWN_LOCK", None)
        else:
            os.environ["AIMOD_SHOWDOWN_LOCK"] = self.old_lock
        self.tmp.cleanup()

    def run_probe(self, *args):
        with contextlib.redirect_stdout(io.StringIO()):          # the probe prints its log; the file has it all
            return ap.main(["--fake", "--fake-accel", "40", "--lock-wait", "0", "--log-dir", self.tmp.name, *args])

    def test_pair_steps_and_put_back(self):
        p = self.run_probe("--pair", "24", "48", "--steps", "0.5,3.0", "--auto", "8")
        pm = p.pm
        a, b = slot_of(pm, 24), slot_of(pm, 48)
        vals_written = {(addr, data) for addr, data in pm.writes}
        for s in (a, b):
            addr = mm.CAR_BLOCK_0 + s * mm.CAR_STRIDE + ap.OFF_AGG_WORK
            self.assertIn((addr, struct.pack("<f", 0.5)), vals_written)
            self.assertIn((addr, struct.pack("<f", 3.0)), vals_written)
        # the game's values are back, and nothing else was ever written
        targets = {mm.CAR_BLOCK_0 + s * mm.CAR_STRIDE + ap.OFF_AGG_WORK for s in (a, b)}
        self.assertTrue(all(addr in targets for addr, _ in pm.writes))
        with open(p.path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("PAIR COMPARISON", text)
        self.assertIn("side-by-side cases", text)
        # the last write per car is the put back of its own game value (not a step value), and it is in memory now
        for s in (a, b):
            addr = mm.CAR_BLOCK_0 + s * mm.CAR_STRIDE + ap.OFF_AGG_WORK
            last = struct.unpack("<f", [d for ad, d in pm.writes if ad == addr][-1])[0]
            self.assertNotIn(round(last, 3), (0.5, 3.0))
            self.assertTrue(0.8 < last < 1.8)
            self.assertAlmostEqual(work(pm, s), last, 6)
        self.assertIn("GAME VALUE (put back)", text)

    def test_showdown_stops_at_first_contact_and_puts_back(self):
        p = self.run_probe("--showdown", "24", "48", "--steps", "0.5,3.0,4.0", "--auto", "30")
        a, b = slot_of(p.pm, 24), slot_of(p.pm, 48)
        with open(p.path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("SHOWDOWN RESULT", text)
        if p.hit is not None:
            self.assertIn("FIRST CONTACT", text)
            self.assertNotIn("STEP 4/5", text.replace("STEP 4/5 GAME VALUE", ""))   # the ramp ended at the contact
        # either way the game's own values are back in both cars
        for s in (a, b):
            addr = mm.CAR_BLOCK_0 + s * mm.CAR_STRIDE + ap.OFF_AGG_WORK
            last = struct.unpack("<f", [d for ad, d in p.pm.writes if ad == addr][-1])[0]
            self.assertTrue(0.8 < last < 1.8)
            self.assertAlmostEqual(work(p.pm, s), last, 6)

    def test_pair_hit_needs_real_damage_between_the_pair(self):
        probe = ap.Probe.__new__(ap.Probe)
        probe._names = {4: "#24", 13: "#20", 7: "#5"}
        sts = {4: ap.StepStats("s #24", 3.0), 13: ap.StepStats("s #20", 3.0)}
        pair = ap.PairStats("s", 4, 13, 3.0, 3.0)
        self.assertIsNone(probe._pair_hit(sts, pair))
        sts[4].contacts.append([10.0, "#5", 0.02, 0.02, 4.0])          # a hit with another car: not the pair
        self.assertIsNone(probe._pair_hit(sts, pair))
        sts[4].contacts.append([12.0, "#20", 0.0, 0.0, 3.0])           # near, no damage: not a contact
        self.assertIsNone(probe._pair_hit(sts, pair))
        sts[13].contacts.append([14.0, "#24", 0.012, 0.009, 2.0])
        dm = probe._pair_hit(sts, pair)
        self.assertIsNotNone(dm)
        self.assertGreaterEqual(max(dm), 0.009)
        self.assertIsNone(probe._pair_hit(sts, None))

    def test_lock_is_taken_for_the_pair_and_removed_at_the_end(self):
        p = self.run_probe("--pair", "24", "48", "--steps", "0.5", "--auto", "5")
        self.assertEqual(p._lock_cars, [24, 48])
        self.assertFalse(os.path.exists(self.lock))                 # gone: AIMOD has the cars back
        with open(p.path, encoding="utf-8") as f:
            self.assertIn("took #24, #48 from AIMOD", f.read())

    def test_pace_slows_the_leader_only_and_puts_the_game_values_back(self):
        p = self.run_probe("--pair", "24", "48", "--steps", "1.0", "--auto", "15", "--pace", "0.96")
        a, b = slot_of(p.pm, 24), slot_of(p.pm, 48)
        tr = lambda sl: mm.CAR_BLOCK_0 + sl * mm.CAR_STRIDE + mm.OFF_DRIVER + mm.DRV_TRACTION       # noqa: E731
        wrote = [(addr, struct.unpack("<f", d)[0]) for addr, d in p.pm.writes if addr in (tr(a), tr(b))]
        self.assertTrue(any(abs(v - 0.96) < 1e-4 for _, v in wrote))                 # somebody was slowed ...
        self.assertFalse(any(addr in (tr(a), tr(b)) and abs(v - 1.0) > 0.05 and abs(v - 0.96) > 1e-4
                             for addr, v in wrote))                                   # ... by exactly that, nothing else
        for sl in (a, b):                                                             # the game's own values are back
            self.assertAlmostEqual(struct.unpack("<f", p.pm._get(tr(sl), 4))[0], 1.0, 6)
        for addr, _ in p.pm.writes:                                                   # never any other car
            slot = (addr - mm.CAR_BLOCK_0) // mm.CAR_STRIDE
            self.assertIn(slot, (a, b))
        with open(p.path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("pace:", text)
        self.assertIn("put back: the pair's traction", text)

    def test_pace_roles_swap_and_follow_the_game_rebuilding_the_value(self):
        class Pm:
            def __init__(self):
                self.mem = {}
                self.writes = []

            def read_bytes(self, addr, n):
                return self.mem.get(addr, struct.pack("<f", 1.5))

            def write_bytes(self, addr, data, n):
                self.mem[addr] = data
                self.writes.append((addr, struct.unpack("<f", data)[0]))

        probe = ap.Probe.__new__(ap.Probe)
        probe.pm = Pm()
        probe.pace, probe.pace_lead, probe.pace_nat, probe.pace_w = 0.9, None, {}, {}
        probe.log = lambda *_: None
        probe.f32 = lambda addr: struct.unpack("<f", probe.pm.read_bytes(addr, 4))[0]
        probe.base = lambda slot: slot * 0x10000
        pair = ap.PairStats("s", 4, 13, 1.0, 1.0)
        names = {4: "#24", 13: "#20"}
        pit = {4: False, 13: False}
        green = ap.mm.FLAG_GREEN

        def st(x):          # x, y, vx, vy, dmg, out, dist: heading +x at 80 m/s
            return (x, 0.0, 80.0, 0.0, 0.0, 0, 0.0)

        addr4, addr13 = probe._tr_addr(4), probe._tr_addr(13)
        probe._pace_tick(pair, {4: st(0.0), 13: st(14.0)}, green, pit, names)          # #20 is in front
        self.assertAlmostEqual(probe.f32(addr13), 1.5 * 0.9, 5)
        self.assertAlmostEqual(probe.f32(addr4), 1.5, 6)
        probe.pm.mem[addr13] = struct.pack("<f", 1.6)                                    # the game rebuilt it (new lap)
        probe._pace_tick(pair, {4: st(0.0), 13: st(14.0)}, green, pit, names)
        self.assertAlmostEqual(probe.f32(addr13), 1.6 * 0.9, 5)                          # slowed from the new own value
        probe._pace_tick(pair, {4: st(10.0), 13: st(8.0)}, green, pit, names)           # 2 m: not a swap (hysteresis)
        self.assertEqual(probe.pace_lead, 13)
        probe._pace_tick(pair, {4: st(20.0), 13: st(8.0)}, green, pit, names)           # #24 got by
        self.assertEqual(probe.pace_lead, 4)
        self.assertAlmostEqual(probe.f32(addr4), 1.5 * 0.9, 5)
        self.assertAlmostEqual(probe.f32(addr13), 1.6, 5)                                # back to his own
        probe._pace_tick(pair, {4: st(20.0), 13: st(8.0)}, 2, pit, names)               # caution (not green): released
        self.assertAlmostEqual(probe.f32(addr4), 1.5, 5)
        self.assertEqual(probe.pace_w, {})

    def test_param_override_is_found_by_name_applied_to_the_field_and_put_back(self):
        p = self.run_probe("--pair", "24", "48", "--steps", "1.0", "--auto", "5",
                           "--param", "ai_drafting_distance=0.3", "--param", "ai_squeeze_pcnt=0.5")
        addr = p.table_find("ai_drafting_distance")
        self.assertIsNotNone(addr)
        got = lambda a: struct.unpack("<f", p.pm._get(a, 4))[0]                      # noqa: E731
        self.assertAlmostEqual(got(addr), 1.10, 5)                                    # the game's own value is back
        self.assertAlmostEqual(got(p.table_find("ai_squeeze_pcnt")), 0.0, 6)
        self.assertTrue(any(abs(struct.unpack("<f", d)[0] - 0.3) < 1e-6 for a, d in p.pm.writes if a == addr))
        with open(p.path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("ai_drafting_distance", text)
        self.assertIn("put back: ai_drafting_distance", text)
        self.assertIsNone(p.table_find("no_such_param"))

    def test_param_rejects_unknown_names_and_wild_values(self):
        for args in (("--param", "ai_grip_modifier=2"), ("--param", "ai_drafting_distance=0"),
                     ("--param", "ai_drafting_distance=9"), ("--param", "ai_drafting_distance")):
            with self.assertRaises(SystemExit):
                self.run_probe("--pair", "24", "48", *args)

    def test_param_held_by_someone_else_is_not_overwritten_back(self):
        probe = ap.Probe.__new__(ap.Probe)
        mem = {0x1000: struct.pack("<f", 0.3)}
        probe.pm = type("P", (), {"write_bytes": lambda self, a, d, n: mem.__setitem__(a, d),
                                  "read_bytes": lambda self, a, n: mem[a]})()
        probe.f32 = lambda a: struct.unpack("<f", mem[a])[0]
        probe.log = lambda *_: None
        probe.param_nat = {"ai_drafting_distance": (0x1000, 1.1)}
        probe.param_w = {"ai_drafting_distance": 0.3}
        mem[0x1000] = struct.pack("<f", 0.9)                                          # AI Enhanced wrote its own
        probe.params_check()
        self.assertEqual(probe.param_w, {})
        probe.params_restore()
        self.assertAlmostEqual(probe.f32(0x1000), 0.9, 5)                             # its value stands

    def test_side_pad_scales_only_the_pair_and_puts_the_game_values_back(self):
        p = self.run_probe("--pair", "24", "48", "--steps", "1.0", "--auto", "10", "--side-pad", "0.2")
        a, b = slot_of(p.pm, 24), slot_of(p.pm, 48)
        sb = lambda sl: mm.CAR_BLOCK_0 + sl * mm.CAR_STRIDE + ap.OFF_SIDE_BASE          # noqa: E731
        wrote = [(addr, struct.unpack("<f", d)[0]) for addr, d in p.pm.writes if addr in (sb(a), sb(b))]
        self.assertTrue(any(abs(v - 0.2) < 1e-6 for _, v in wrote))
        for sl in (a, b):
            self.assertAlmostEqual(struct.unpack("<f", p.pm._get(sb(sl), 4))[0], 1.0, 6)    # the game's base is back
        for addr, _ in p.pm.writes:                                                     # never any other car
            self.assertIn((addr - mm.CAR_BLOCK_0) // mm.CAR_STRIDE, (a, b))
        with open(p.path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("side-pad:", text)
        self.assertIn("put back: the pair's side padding", text)

    def test_side_pad_follows_a_mode_reset_and_releases_on_caution(self):
        class Pm:
            def __init__(self):
                self.mem = {}

            def read_bytes(self, addr, n):
                return self.mem.get(addr, struct.pack("<f", 1.0))

            def write_bytes(self, addr, data, n):
                self.mem[addr] = data

        probe = ap.Probe.__new__(ap.Probe)
        probe.pm = Pm()
        probe.side_pad, probe.side_nat, probe.side_w = 0.2, {}, {}
        probe.log = lambda *_: None
        probe.f32 = lambda addr: struct.unpack("<f", probe.pm.read_bytes(addr, 4))[0]
        probe.base = lambda slot: slot * 0x10000
        pair = ap.PairStats("s", 4, 13, 1.0, 1.0)
        pit = {4: False, 13: False}
        st = (0.0, 0.0, 80.0, 0.0, 0.0, 0, 0.0)
        a4, a13 = probe.base(4) + ap.OFF_SIDE_BASE, probe.base(13) + ap.OFF_SIDE_BASE
        probe._side_tick(pair, {4: st, 13: st}, ap.mm.FLAG_GREEN, pit)
        self.assertAlmostEqual(probe.f32(a4), 0.2, 6)
        probe.pm.mem[a4] = struct.pack("<f", 1.5)                                       # a mode change reset it to 1.5
        probe._side_tick(pair, {4: st, 13: st}, ap.mm.FLAG_GREEN, pit)
        self.assertAlmostEqual(probe.f32(a4), 0.3, 6)                                   # scaled from the new base
        probe._side_tick(pair, {4: st, 13: st}, 2, pit)                                 # caution: released
        self.assertAlmostEqual(probe.f32(a13), 1.0, 6)
        self.assertAlmostEqual(probe.f32(a4), 1.5, 6)
        self.assertEqual(probe.side_w, {})

    def test_side_pad_needs_a_pair_and_a_range(self):
        for args in (("--test", "24", "--side-pad", "0.5"), ("--pair", "24", "48", "--side-pad", "1.5"),
                     ("--pair", "24", "48", "--side-pad", "-0.1")):
            with self.assertRaises(SystemExit):
                self.run_probe(*args)

    def test_pace_needs_a_pair_and_a_safe_range(self):
        for args in (("--test", "24", "--pace", "0.97"), ("--pair", "24", "48", "--pace", "1.05"),
                     ("--pair", "24", "48", "--pace", "0.5")):
            with self.assertRaises(SystemExit):
                self.run_probe(*args)

    def test_showdown_defaults(self):
        self.assertEqual(ap.SHOWDOWN_STEPS[0], 2.4)
        self.assertLessEqual(max(ap.SHOWDOWN_STEPS), ap.HARD_MAX)

    def test_never_player_or_pace(self):
        p = self.run_probe("--test", "24", "--high", "3", "--auto", "5")
        for addr, _ in p.pm.writes:
            slot = (addr - mm.CAR_BLOCK_0) // mm.CAR_STRIDE
            self.assertNotIn(slot, (mm.PLAYER_SLOT, mm.PACE_SLOT))

    def test_reroll_stops_and_leaves_game_value(self):
        p = self.run_probe("--test", "24", "--steps", "0.4", "--auto", "40", "--fake-reroll", "60")
        s = slot_of(p.pm, 24)
        self.assertNotAlmostEqual(work(p.pm, s), 0.4, 3)          # the game's new roll stands
        with open(p.path, encoding="utf-8") as f:
            self.assertIn("the game rewrote", f.read())

    def test_field_is_read_only(self):
        p = self.run_probe("--field", "--auto", "20")
        self.assertEqual(p.pm.writes, [])

    def test_game_dt(self):
        self.assertAlmostEqual(ap.game_dt(10.0, 9.95, 0.05), 0.05)
        self.assertAlmostEqual(ap.game_dt(11.5, 10.0, 0.05), 1.5)       # 30x time acceleration counts
        self.assertEqual(ap.game_dt(100.0, 10.0, 0.05), 0.0)            # a jump
        self.assertEqual(ap.game_dt(5.0, 10.0, 0.05), 0.0)              # a restart / replay

    def test_corr(self):
        self.assertAlmostEqual(ap.corr([1, 2, 3, 4], [2, 4, 6, 8]), 1.0)
        self.assertAlmostEqual(ap.corr([1, 2, 3, 4], [8, 6, 4, 2]), -1.0)
        self.assertNotEqual(ap.corr([1, 2], [1, 2]), ap.corr([1, 2], [1, 2]))   # nan


if __name__ == "__main__":
    unittest.main()
