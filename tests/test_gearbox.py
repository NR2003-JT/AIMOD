"""Native gearbox: the Cup engine records' gear count (fake NR2003 memory, no game needed)."""
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aimod import gearbox as gb


class FakePM:
    process_handle = 0
    process_id = 1

    def __init__(self, reverse=gb.STOCK_REVERSE):
        self.mem = {}
        for k in range(8):                                  # 5 Cup records + 3 others (Trans-Am: 6 gears)
            a = gb.ENGINE_TABLE + k * gb.STRIDE + gb.OFF_GEARS
            n, rev = (4, reverse) if k in gb.CUP_RECORDS else (6, -3.1)
            self.write_bytes(a, struct.pack("<if", n, rev), 8)

    def write_bytes(self, addr, data, n):
        for i in range(n):
            self.mem[addr + i] = data[i]

    def read_bytes(self, addr, n):
        return bytes(self.mem.get(addr + i, 0) for i in range(n))

    def gears(self, k):
        return struct.unpack("<i", self.read_bytes(gb.ENGINE_TABLE + k * gb.STRIDE + gb.OFF_GEARS, 4))[0]


class GearboxTests(unittest.TestCase):
    def test_sets_and_restores_only_the_cup_records(self):
        pm, box = FakePM(), gb.Gearbox()
        self.assertEqual(box.apply(pm, 5), 5)
        self.assertEqual([pm.gears(k) for k in range(8)], [5, 5, 5, 5, 5, 6, 6, 6])
        self.assertEqual(box.apply(pm, 5), 0)               # already set
        self.assertEqual(box.restore(pm), 5)
        self.assertEqual([pm.gears(k) for k in range(5)], [4] * 5)

    def test_unexpected_engine_table_is_left_alone(self):
        pm, box = FakePM(reverse=-2.0), gb.Gearbox()
        self.assertEqual(box.apply(pm, 5), 0)
        self.assertEqual(pm.gears(0), 4)
        self.assertIn("different NR2003.exe", box.status)

    def test_default_on_only_for_fcrd(self):
        self.assertTrue(gb.default_on("FCRD NCS22 v1"))
        self.assertFalse(gb.default_on("Cup12"))
        self.assertTrue(gb.offered("Cup12"))

    def test_ai_gears_spread_to_the_old_top_speed_not_the_player(self):
        pm = FakePM()
        rec = struct.pack("<iff", 5, -2.6923, 0.0) + struct.pack("<6f", 2.625, 1.9375, 1.375, 1.1034, 0.96, 1.0)
        for s in (0, 2):
            for base, stride in gb.AI_RECORDS:
                pm.write_bytes(base + s * stride, rec, len(rec))
        self.assertEqual(gb.Gearbox().spread_ai(pm, 5), 3)              # 3 records for the AI car only
        ai = struct.unpack("<6f", pm.read_bytes(gb.AI_RECORDS[0][0] + 2 * gb.AI_RECORDS[0][1] + 12, 24))
        self.assertAlmostEqual(ai[4], 1.1034, places=4)                 # 5th = the 4-speed's top gear
        self.assertAlmostEqual(ai[0], 2.625, places=4)
        player = struct.unpack("<6f", pm.read_bytes(gb.AI_RECORDS[0][0] + 12, 24))
        self.assertAlmostEqual(player[1], 1.9375, places=4)
        self.assertEqual(gb.Gearbox().spread_ai(pm, 5), 0)              # already spread: left alone


if __name__ == "__main__":
    unittest.main()
