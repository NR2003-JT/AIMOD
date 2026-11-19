"""Game reader: car #0 is a real car (bug report 2026-11-18: #0 never showed on the monitor) and AI pit road speeding
penalties are forgiven (same report: every green-flag stop at Daytona was held 30 s for "Speeding in pits")."""
import struct
import unittest

from aimod import memory_map as mm
from aimod.game import Game


class FakePM:
    process_handle = 0

    def __init__(self):
        self.mem = {}

    def write_bytes(self, addr, data, n):
        for k in range(n):
            self.mem[addr + k] = data[k]

    def read_bytes(self, addr, n):
        get = self.mem.get
        return bytes(get(addr + k, 0) for k in range(n))

    def read_int(self, addr):
        return struct.unpack("<i", self.read_bytes(addr, 4))[0]

    def read_uint(self, addr):
        return struct.unpack("<I", self.read_bytes(addr, 4))[0]

    def read_double(self, addr):
        return struct.unpack("<d", self.read_bytes(addr, 8))[0]

    def read_float(self, addr):
        return struct.unpack("<f", self.read_bytes(addr, 4))[0]

    def put(self, addr, fmt, v):
        b = struct.pack(fmt, v)
        self.write_bytes(addr, b, len(b))


def game_with(pm):
    g = Game()
    g.pm = pm
    g.write_enabled = True
    return g


def add_driver(pm, slot, number, first="", last="", grip=0.9):
    pm.put(mm.CAR_BLOCK_0 + slot * mm.CAR_STRIDE + mm.OFF_GRIP, "<f", grip)
    entry = mm.DRIVER_ENTRY_0 + slot * mm.DRIVER_ENTRY_SIZE
    pm.write_bytes(entry + mm.DE_OFF_FIRST, first.encode(), len(first))
    pm.write_bytes(entry + mm.DE_OFF_LAST, last.encode(), len(last))
    pm.put(entry + mm.DE_OFF_CARNUM, "<i", number)


class CarZeroTest(unittest.TestCase):
    def test_car_zero_is_a_valid_car(self):
        pm = FakePM()
        add_driver(pm, 2, 0, "Zero", "Driver")
        add_driver(pm, 3, 7, "Seven", "Driver")
        add_driver(pm, 4, 0)                       # blank entry: no name, number 0
        add_driver(pm, mm.PACE_SLOT, 0, "Safety", "Car")
        snap = game_with(pm).snapshot()
        valid = {c.idx: c for c in snap.cars if c.valid}
        self.assertIn(2, valid)
        self.assertEqual(int(valid[2].number), 0)
        self.assertEqual(valid[2].name, "Zero Driver")
        self.assertIn(3, valid)
        self.assertNotIn(4, valid)
        self.assertNotIn(mm.PACE_SLOT, valid)


class PitSpeedingTest(unittest.TestCase):
    MGR, ARR = 0x20000000, 0x20001000

    def setUp(self):
        self.pm = pm = FakePM()
        pm.put(mm.PENALTY_MGR, "<I", self.MGR)
        pm.put(self.MGR + 4, "<I", self.ARR)
        pm.put(self.MGR + 8, "<i", 15)
        self.rules = {}
        for k in range(15):
            rule = 0x21000000 + k * 0x1000
            self.rules[k] = rule
            pm.put(self.ARR + 4 * k, "<I", rule)
            pm.put(rule, "<I", 0x6E0000 + k)        # some other vtable
        pm.put(self.rules[6], "<I", mm.PIT_SPEEDING_VTABLES[0])

    def val(self, k, slot):
        return self.pm.read_uint(self.rules[k] + 4 + slot * 4)

    def test_clears_only_speeding_for_given_slots(self):
        pm, g = self.pm, game_with(self.pm)
        pm.put(self.rules[6] + 4 + 5 * 4, "<I", 0x1E4)          # #? in slot 5: held 30 s
        pm.put(self.rules[6] + 4 + 0 * 4, "<I", 0x1E4)          # the player: not in the list
        pm.put(self.rules[14] + 4 + 5 * 4, "<I", 0x20024)       # another rule: left alone
        self.assertEqual(g.clear_pit_speeding({5, 6}), {5})
        self.assertEqual(self.val(6, 5), 0)
        self.assertEqual(self.val(6, 0), 0x1E4)
        self.assertEqual(self.val(14, 5), 0x20024)
        self.assertEqual(g.clear_pit_speeding({5, 6}), set())   # nothing left: nothing reported

    def test_found_by_name_when_the_vtable_differs(self):
        pm, g = self.pm, game_with(self.pm)
        rule = self.rules[9]
        name = mm.PIT_SPEEDING_NAME.encode()
        pm.write_bytes(rule + mm.RULE_NAME_OFF, name, len(name))
        pm.put(rule + 4 + 3 * 4, "<I", 0x1E4)
        self.assertEqual(g.clear_pit_speeding({3}), {3})
        self.assertEqual(self.val(9, 3), 0)

    def test_read_only_does_nothing(self):
        pm, g = self.pm, game_with(self.pm)
        g.write_enabled = False
        pm.put(self.rules[6] + 4 + 5 * 4, "<I", 0x1E4)
        self.assertEqual(g.clear_pit_speeding({5}), set())
        self.assertEqual(self.val(6, 5), 0x1E4)


if __name__ == "__main__":
    unittest.main()
