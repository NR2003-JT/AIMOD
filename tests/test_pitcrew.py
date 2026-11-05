"""Pit crew patch against a fake NR2003 memory (no game needed)."""
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aimod import pitcrew as pc


class FakePM:
    process_handle = 0
    process_id = 1

    def __init__(self):
        self.mem = {}
        self.next = 0x10000000
        for op, d in pc.STOCK_DUR.items():
            self.write_bytes(pc.DUR_TABLE + op * 12, struct.pack("<fff", *d), 12)
        for task, addr in pc.WHEEL_TASKS.items():
            steps = [0x38, 0x0B, 0x0B, 0x0B, 0x0B, 0x0B, 0x0C, 0x39, 0x3A, 0x0D, 0x0E, 0x0E, 0x0E, 0x0E, 0x0E]
            data = b"".join(bytes([op]) + b"\x00" * 3 + struct.pack("<I", task) + bytes(11) for op in steps)
            self.write_bytes(addr, data + bytes(pc.STEP), len(data) + pc.STEP)
            self.write_bytes(pc.TASK_TABLE + 4 * task, struct.pack("<I", addr), 4)
        self.write_bytes(pc.SPEED_SITE, pc.SPEED_ORIG, len(pc.SPEED_ORIG))
        for at, stock, value in pc.FUEL_SITES:
            self.write_bytes(at, b"\xd8\x0d" + struct.pack("<I", stock), 6)
            self.write_bytes(stock, struct.pack("<f", value), 4)

    def write_bytes(self, addr, data, n):
        for k in range(n):
            self.mem[addr + k] = data[k]

    def read_bytes(self, addr, n):
        return bytes(self.mem.get(addr + k, 0) for k in range(n))

    def read_uint(self, addr):
        return struct.unpack("<I", self.read_bytes(addr, 4))[0]

    def allocate(self, n):
        a, self.next = self.next, self.next + n
        return a


class PitCrewTests(unittest.TestCase):
    def test_modern_then_stock(self):
        pm, crew = FakePM(), pc.PitCrew()
        self.assertEqual(crew.lugs(pm), 5)
        self.assertTrue(crew.apply(pm, True))
        self.assertEqual(crew.lugs(pm), 1)
        self.assertTrue(crew.is_modern(pm))
        jack = struct.unpack("<f", pm.read_bytes(pc.DUR_TABLE + 0x09 * 12, 4))[0]
        self.assertAlmostEqual(jack, 0.25)
        tire = struct.unpack("<f", pm.read_bytes(pc.DUR_TABLE + 0x39 * 12, 4))[0]
        self.assertAlmostEqual(tire, 0.35, places=5)
        self.assertAlmostEqual(crew.fuel_factor(pm), 1.8, places=4)
        self.assertAlmostEqual(struct.unpack("<f", pm.read_bytes(pc.FUEL_SITES[0][1], 4))[0], 1.35)  # shared copy untouched
        self.assertTrue(crew.apply(pm, False))
        self.assertAlmostEqual(crew.fuel_factor(pm), 1.0)
        self.assertEqual(crew.lugs(pm), 5)
        self.assertEqual(pm.read_uint(pc.TASK_TABLE + 4 * 33), pc.WHEEL_TASKS[33])
        self.assertAlmostEqual(struct.unpack("<f", pm.read_bytes(pc.DUR_TABLE + 0x09 * 12, 4))[0], 0.5)

    def test_new_nr2003_process_gets_its_own_fuel_block(self):
        # tester: "Pit crew: not changed (Could not write memory at: 124059648 ... GetLastError: 998)" after
        # NR2003 restarted - the fuel constants' block from the old game was reused
        crew = pc.PitCrew()
        old = FakePM()
        self.assertTrue(crew.apply(old, True))
        new = FakePM()
        new.process_id, new.next = 2, 0x20000000
        self.assertTrue(crew.apply(new, True))
        self.assertAlmostEqual(crew.fuel_factor(new), 1.8, places=4)
        target = struct.unpack("<I", new.read_bytes(pc.FUEL_SITES[0][0] + 2, 4))[0]
        self.assertGreaterEqual(target, 0x20000000)

    def test_new_nr2003_process_gets_its_own_lug_lists(self):
        # same 998 as the fuel block: the one-lug step lists were written to the old game's cave address
        crew = pc.PitCrew()
        old = FakePM()
        self.assertTrue(crew.apply(old, True))
        new = FakePM()
        new.process_id, new.next = 2, 0x20000000
        self.assertTrue(crew.apply(new, True))
        self.assertEqual(crew.lugs(new), 1)
        self.assertGreaterEqual(new.read_uint(pc.TASK_TABLE + 4 * 33), 0x20000000)

    def test_lug_change_waits_for_clear_pit_road(self):
        pm, crew = FakePM(), pc.PitCrew()
        self.assertFalse(crew.apply(pm, True, cars_on_pit_road=True))
        self.assertEqual(crew.lugs(pm), 5)
        self.assertTrue(crew.apply(pm, True, cars_on_pit_road=False))
        self.assertEqual(crew.lugs(pm), 1)

    def test_unknown_exe_is_left_alone(self):
        pm, crew = FakePM(), pc.PitCrew()
        pm.write_bytes(pc.DUR_TABLE + 0x09 * 12, struct.pack("<fff", 9, 9, 9), 12)
        self.assertFalse(crew.apply(pm, True))
        self.assertEqual(crew.lugs(pm), 5)
        self.assertIn("different NR2003.exe", crew.status)

    def test_default_per_mod(self):
        self.assertTrue(pc.default_modern("FCRD NCS22 v1"))
        self.assertFalse(pc.default_modern("Cup12"))


if __name__ == "__main__":
    unittest.main()
