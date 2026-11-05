"""Pit gate (aimod/pitgate.py) against a fake NR2003 memory, plus a small x86 run of the cave's code (no game needed).

The run decodes the generated machine code with capstone (skipped if it isn't installed) and steps through it
with a tiny interpreter for the handful of instructions the cave uses, to check which mode changes it lets
through and which it refuses."""
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aimod import memory_map as mm
from aimod import pitgate as pg

try:
    import capstone
except Exception:              # pragma: no cover - the build machine has it
    capstone = None


class FakePM:
    process_handle = 0

    def __init__(self, pid=1, site=pg.HOOK_ORIG):
        self.process_id = pid
        self.mem = {}
        self.next = 0x10000000
        self.write_bytes(pg.HOOK_SITE, site, len(site))
        self.write_bytes(mm.GAME_TICKS, struct.pack("<i", 5000), 4)
        self.write_bytes(mm.TICK_RATE_FLAG, struct.pack("<i", 1), 4)

    def write_bytes(self, addr, data, n):
        for k in range(n):
            self.mem[addr + k] = data[k]

    def read_bytes(self, addr, n):
        return bytes(self.mem.get(addr + k, 0) for k in range(n))

    def read_int(self, addr):
        return struct.unpack("<i", self.read_bytes(addr, 4))[0]

    def read_uint(self, addr):
        return struct.unpack("<I", self.read_bytes(addr, 4))[0]

    def allocate(self, n):
        a = self.next
        self.next += (n + 0xFFF) & ~0xFFF
        return a


class Cpu:
    """Just enough 32-bit x86 for the cave: returns 'refused' (ret 4 inside the cave) or 'orig' (jumped back)."""
    REGS = ("eax", "ecx", "edx", "esi", "edi", "esp")

    def __init__(self, pm, cave):
        self.pm, self.cave = pm, cave
        self.md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
        self.md.detail = True
        code = pm.read_bytes(cave + pg.OFF_CODE, len(pg.build_code(cave)))
        self.ins = {i.address: i for i in self.md.disasm(code, cave + pg.OFF_CODE)}

    def u32(self, a):
        return struct.unpack("<I", self.pm.read_bytes(a, 4))[0]

    def addr(self, op):
        m = op.mem
        a = m.disp
        if m.base:
            a += self.r[self.ins_reg(m.base)]
        if m.index:
            a += self.r[self.ins_reg(m.index)] * m.scale
        return a & 0xFFFFFFFF

    def ins_reg(self, rid):
        return self.cur.reg_name(rid)

    def val(self, op):
        if op.type == capstone.x86.X86_OP_REG:
            return self.r[self.ins_reg(op.reg)]
        if op.type == capstone.x86.X86_OP_IMM:
            return op.imm & 0xFFFFFFFF
        a = self.addr(op)
        return self.pm.read_bytes(a, 1)[0] if op.size == 1 else self.u32(a)

    def run(self, car, mode):
        esp = 0x00200000
        self.r = dict(eax=0, ecx=car, edx=0x55, esi=0, edi=0, esp=esp)
        self.pm.write_bytes(esp, struct.pack("<II", 0xDEAD, mode), 8)     # return address, the new mode
        pc = self.cave + pg.OFF_CODE
        flags = (0, 0, 0, 0)          # zf, cf, sf, of
        for _ in range(200):
            if pc == pg.HOOK_RESUME:
                return "orig"
            i = self.cur = self.ins[pc]
            ops = i.operands
            nxt = pc + i.size
            mn = i.mnemonic
            if mn == "mov":
                v = self.val(ops[1])
                self.r[self.ins_reg(ops[0].reg)] = v
            elif mn == "cmp":
                a, b = self.val(ops[0]), self.val(ops[1])
                bits = 8 if ops[0].size == 1 else 32
                mask = (1 << bits) - 1
                a, b = a & mask, b & mask
                res = (a - b) & mask
                sa, sb, sr = a >> (bits - 1), b >> (bits - 1), res >> (bits - 1)
                flags = (int(res == 0), int(a < b), sr, int(sa != sb and sr != sa))
            elif mn in ("je", "jne", "jae", "jb", "jg", "jmp"):
                zf, cf, sf, of = flags
                take = {"je": zf, "jne": not zf, "jae": not cf, "jb": cf, "jg": (not zf) and sf == of,
                        "jmp": True}[mn]
                if take:
                    nxt = ops[0].imm & 0xFFFFFFFF
            elif mn == "push":
                self.r["esp"] -= 4
                self.pm.write_bytes(self.r["esp"], struct.pack("<I", self.val(ops[0])), 4)
            elif mn == "pop":
                self.r[self.ins_reg(ops[0].reg)] = self.u32(self.r["esp"])
                self.r["esp"] += 4
            elif mn == "sub":
                reg = self.ins_reg(ops[0].reg)
                self.r[reg] = (self.r[reg] - self.val(ops[1])) & 0xFFFFFFFF
            elif mn == "xor":
                reg = self.ins_reg(ops[0].reg)
                self.r[reg] ^= self.val(ops[1])
            elif mn == "div":
                d = self.val(ops[0])
                num = (self.r["edx"] << 32) | self.r["eax"]
                self.r["eax"], self.r["edx"] = num // d, num % d
            elif mn == "inc":
                a = self.addr(ops[0])
                self.pm.write_bytes(a, struct.pack("<I", (self.u32(a) + 1) & 0xFFFFFFFF), 4)
            elif mn == "ret":
                assert self.r["esp"] == esp, "stack not balanced at ret"
                assert self.r["edx"] == 0x55 and self.r["ecx"] == car, "ecx / edx not kept"
                return "refused"
            else:
                raise AssertionError(f"unexpected instruction {mn} {i.op_str}")
            pc = nxt
        raise AssertionError("ran away")


def car_addr(slot):
    return mm.CAR_BLOCK_0 + slot * mm.CAR_STRIDE


class InstallTests(unittest.TestCase):
    def test_install_writes_the_jump_and_the_cave(self):
        pm = FakePM()
        g = pg.PitGate()
        self.assertTrue(g.install(pm))
        self.assertEqual(pm.read_bytes(pg.HOOK_SITE, 8), pg.hook_bytes(g.cave))
        self.assertEqual(pm.read_bytes(g.cave, 8), pg.MAGIC)
        self.assertEqual(pm.read_uint(g.cave + pg.OFF_DEADLINE), 0, "installed open")

    def test_a_new_aimod_finds_its_own_cave(self):
        pm = FakePM()
        g1 = pg.PitGate()
        g1.install(pm)
        g1.arm(pm)
        g2 = pg.PitGate()
        self.assertTrue(g2.install(pm))
        self.assertEqual(g2.cave, g1.cave)
        self.assertEqual(pm.read_uint(g1.cave + pg.OFF_DEADLINE), 0, "a fresh run starts open")

    def test_unknown_code_is_left_alone(self):
        pm = FakePM(site=bytes.fromhex("e912345678909090"))
        g = pg.PitGate()
        self.assertFalse(g.install(pm))
        self.assertIn("different NR2003.exe", g.status)
        self.assertEqual(pm.read_bytes(pg.HOOK_SITE, 8), bytes.fromhex("e912345678909090"))
        self.assertFalse(g.arm(pm))

    def test_arm_sets_a_deadline_a_minute_of_game_time_ahead(self):
        pm = FakePM()
        g = pg.PitGate()
        self.assertTrue(g.arm(pm))
        self.assertEqual(pm.read_uint(g.cave + pg.OFF_DEADLINE), 5000 + 36 * 60)
        g.disarm(pm)
        self.assertEqual(pm.read_uint(g.cave + pg.OFF_DEADLINE), 0)

    def test_remove_puts_the_routine_back(self):
        pm = FakePM()
        g = pg.PitGate()
        g.arm(pm)
        cave = g.cave
        g.remove(pm)
        self.assertEqual(pm.read_bytes(pg.HOOK_SITE, 8), pg.HOOK_ORIG)
        self.assertEqual(pm.read_uint(cave + pg.OFF_DEADLINE), 0)
        self.assertIsNone(g.cave)

    def test_new_game_process_gets_a_new_cave(self):
        g = pg.PitGate()
        pm1 = FakePM(pid=1)
        g.install(pm1)
        pm2 = FakePM(pid=2)
        self.assertTrue(g.arm(pm2))
        self.assertEqual(pm2.read_bytes(pg.HOOK_SITE, 8), pg.hook_bytes(g.cave))

    def test_an_older_gate_in_the_running_game_is_replaced(self):
        old = 0x20000000                                   # a v1 cave: magic at code - 0x140

        class Guarded(FakePM):                             # memory in front of the old cave isn't mapped (live
            def read_bytes(self, addr, n):                 # 11-02: the v2 magic look there threw, v1 never checked)
                if old - 0x100 <= addr < old:
                    raise OSError("Could not read memory")
                return super().read_bytes(addr, n)

        pm = Guarded()
        pm.write_bytes(old, b"AIMGATE1" + struct.pack("<I", 999999), 12)
        hook = b"\xe9" + struct.pack("<i", old + 0x140 - (pg.HOOK_SITE + 5)) + b"\x90" * 3
        pm.write_bytes(pg.HOOK_SITE, hook, 8)
        g = pg.PitGate()
        self.assertTrue(g.install(pm))
        self.assertNotEqual(g.cave, old)
        self.assertEqual(pm.read_bytes(pg.HOOK_SITE, 8), pg.hook_bytes(g.cave))
        self.assertEqual(pm.read_uint(old + pg.OFF_DEADLINE), 0, "old gate opened")

    def test_a_v2_gate_in_the_running_game_is_replaced(self):
        old = 0x20000000                                   # a v2 cave: magic at code - 0x180
        pm = FakePM()
        pm.write_bytes(old, b"AIMGATE2" + struct.pack("<I", 999999), 12)
        hook = b"\xe9" + struct.pack("<i", old + 0x180 - (pg.HOOK_SITE + 5)) + b"\x90" * 3
        pm.write_bytes(pg.HOOK_SITE, hook, 8)
        g = pg.PitGate()
        self.assertTrue(g.install(pm))
        self.assertNotEqual(g.cave, old)
        self.assertEqual(pm.read_bytes(pg.HOOK_SITE, 8), pg.hook_bytes(g.cave))
        self.assertEqual(pm.read_uint(old + pg.OFF_DEADLINE), 0, "old gate opened")

    def test_arm_records_the_tick_it_was_armed_at(self):
        pm = FakePM()
        g = pg.PitGate()
        g.arm(pm)
        self.assertEqual(pm.read_uint(g.cave + pg.OFF_ARMED_FROM), 5000)

    def test_set_allow_writes_one_byte_per_slot(self):
        pm = FakePM()
        g = pg.PitGate()
        g.install(pm)
        g.set_allow(pm, {3, 40})
        data = pm.read_bytes(g.cave + pg.OFF_ALLOW, pg.MAX_SLOTS)
        self.assertEqual([i for i, b in enumerate(data) if b], [3, 40])

    def test_held_reports_new_refusals_per_slot(self):
        pm = FakePM()
        g = pg.PitGate()
        g.install(pm)
        self.assertEqual(g.held(pm), {})           # first look: baseline
        pm.write_bytes(g.cave + pg.OFF_COUNTS + 4 * 7, struct.pack("<I", 3), 4)
        self.assertEqual(g.held(pm), {7: 3})
        self.assertEqual(g.held(pm), {})


@unittest.skipIf(capstone is None, "capstone not installed")
class CaveRunTests(unittest.TestCase):
    def setUp(self):
        self.pm = FakePM()
        self.g = pg.PitGate()
        self.g.arm(self.pm)
        self.cpu = Cpu(self.pm, self.g.cave)

    def go(self, slot=7, mode=0x760F48):
        return self.cpu.run(car_addr(slot), mode)

    def count(self, slot=7):
        return self.pm.read_uint(self.g.cave + pg.OFF_COUNTS + 4 * slot)

    def test_game_made_turn_for_pit_road_is_refused(self):
        for mode in pg.GATED_MODES:
            self.assertEqual(self.go(mode=mode), "refused", hex(mode))
        self.assertEqual(self.count(), 3)
        self.assertEqual(self.pm.read_uint(self.g.cave + pg.OFF_TOTAL), 3)

    def test_other_modes_pass(self):
        for mode in (0x760F54, 0x760F90, 0x760DE4, 0x760D78, 0x760F3C, 0x760DF0, 0x760EA0, 0xBCF80C):
            self.assertEqual(self.go(mode=mode), "orig", hex(mode))
        self.assertEqual(self.count(), 0)

    def test_penalty_and_repair_pass(self):
        base = car_addr(7)
        for off, size in ((mm.OFF_GAME_PENALTY_STOP, 1), (mm.OFF_GAME_REPAIR, 4)):
            self.pm.write_bytes(base + off, (1).to_bytes(size, "little"), size)
            self.assertEqual(self.go(), "orig", off)
            self.pm.write_bytes(base + off, bytes(size), size)
        self.assertEqual(self.go(), "refused")

    def test_the_games_pit_request_alone_does_not_open_it(self):
        # v1 trusted +8118; the game's strategy re-plan (0x542626) writes it too (#6 / #88 / #50 at Bristol 11-02)
        self.pm.write_bytes(car_addr(7) + mm.OFF_PITTING, b"\x01", 1)
        self.assertEqual(self.go(), "refused")

    def test_aimod_allow_list_opens_it_for_that_car_only(self):
        self.g.set_allow(self.pm, {7})
        self.assertEqual(self.go(slot=7), "orig")
        self.assertEqual(self.go(slot=8), "refused")
        self.g.set_allow(self.pm, set())
        self.assertEqual(self.go(slot=7), "refused")

    def test_negative_repair_value_does_not_open_it(self):
        self.pm.write_bytes(car_addr(7) + mm.OFF_GAME_REPAIR, struct.pack("<i", -1), 4)
        self.assertEqual(self.go(), "refused")

    def test_car_already_on_pit_road_passes(self):
        self.pm.write_bytes(mm.ON_PIT_ROAD + 7, b"\x01", 1)
        self.assertEqual(self.go(), "orig")
        self.assertEqual(self.go(slot=8), "refused")
        self.assertEqual(self.count(8), 1)

    def test_player_and_pace_car_pass(self):
        self.assertEqual(self.go(slot=0), "orig")
        self.assertEqual(self.go(slot=1), "orig")
        self.assertEqual(self.go(slot=2), "refused")
        self.assertEqual(self.go(slot=pg.MAX_SLOTS - 1), "refused")
        self.assertEqual(self.count(pg.MAX_SLOTS - 1), 1)

    def test_open_when_not_armed_or_expired(self):
        self.g.disarm(self.pm)
        self.assertEqual(self.go(), "orig")
        self.g.arm(self.pm)
        self.assertEqual(self.go(), "refused")
        self.pm.write_bytes(mm.GAME_TICKS, struct.pack("<i", 5000 + 36 * 60 + 1), 4)   # AIMOD stopped arming
        self.assertEqual(self.go(), "orig")

    def test_tick_counter_restart_opens_it(self):
        # AIMOD died late in a race (deadline far ahead), then the session restarted: the counter starts over
        self.pm.write_bytes(mm.GAME_TICKS, struct.pack("<i", 400000), 4)
        self.g.arm(self.pm)
        self.assertEqual(self.go(), "refused")
        self.pm.write_bytes(mm.GAME_TICKS, struct.pack("<i", 120), 4)
        self.assertEqual(self.go(), "orig")
        self.g.arm(self.pm)                                    # a live AIMOD arms the new session again
        self.assertEqual(self.go(), "refused")

    def test_orig_path_runs_the_routines_first_instructions(self):
        names = [i.mnemonic for a, i in sorted(self.cpu.ins.items())][-5:]
        self.assertEqual(names, ["push", "mov", "push", "mov", "jmp"])


class RulesPendingTests(unittest.TestCase):
    def test_rule_states_that_want_a_stop(self):
        from aimod.game import Game
        pm = FakePM()
        mgr, arr = 0x30000000, 0x30001000
        pm.write_bytes(mm.PENALTY_MGR, struct.pack("<I", mgr), 4)
        pm.write_bytes(mgr + 4, struct.pack("<Ii", arr, 3), 8)
        rules = [(0x30002000, 0x422BC0, {5: 0x20014, 6: 0x20000}),    # nibble rule: 5 owes, 6 served (0)
                 (0x30003000, 0x422C00, {7: 0x5, 8: 0x3}),            # "== 5" rule: 7 yes, 8 no
                 (0x30004000, 0x422A10, {9: 0xF})]                    # never wants a stop
        for k, (rule, check, states) in enumerate(rules):
            vt = rule + 0x800
            pm.write_bytes(arr + 4 * k, struct.pack("<I", rule), 4)
            pm.write_bytes(rule, struct.pack("<I", vt), 4)
            pm.write_bytes(vt + 0x30, struct.pack("<I", check), 4)
            for slot, v in states.items():
                pm.write_bytes(rule + 4 + 4 * slot, struct.pack("<I", v), 4)
        g = Game()
        g.pm = pm
        self.assertEqual(g.rules_pending(), {5, 7})


if __name__ == "__main__":
    unittest.main()
