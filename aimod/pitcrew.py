"""Modern pit crew for NR2003 (runtime only: NR2003.exe is never changed on disk).

The "modern" crew is the preset tested at Charlotte (FCRD NCS22, 2026-10-17): one lug nut per wheel, jack
steps x0.5, tire-change steps x0.7 - about 9.9 s for 4 tires instead of ~14.4 s - and fuel flowing x1.8
(a full tank in ~9.4 s, so it fits in the tire change; ~4.5 s a can). The crew-rating spread
still varies the stops. Fixed timings on purpose: the only choice is on/off per NR2003 mod.

How (found by static disassembly of NR2003.exe, checked live with Desktop\\MODS\\PitCrewTest\\pitspeed.py):
- Crew jobs come from the task table at 0x6EC5A8 (task id -> list of 19-byte steps). Wheel tasks 33/35/36/38
  have 5 undo-nut (op 0x0B) and 5 tighten-nut (op 0x0E) steps; "one lug" points those entries at copies in
  allocated memory with one of each. Changed only while no car is on pit road.
- Step durations: 3 floats per opcode at 0x71ECB8 (best crew s, worst crew s, spread); jack and tire-change
  opcodes are scaled.
- Fuel flow: the add-fuel step (0x40D690) adds (G*1.35 + can_left*0.06)*dt per frame. Both constants are
  shared with other code, so the two fmul operands (0x40D6C6, 0x40D6CF) are pointed at scaled copies.
- pitspeed.py's crew-speed hook at 0x522FBE (if the .bat applied one) is set back to x1.0.
Every address is checked against the stock values first; on any mismatch nothing is changed.
"""
from __future__ import annotations

import ctypes
import logging
import struct
from ctypes import wintypes

log = logging.getLogger("aimod.pitcrew")

SPEED_SITE = 0x522FBE
SPEED_ORIG = bytes.fromhex("8b86af0000008bcf50")

TASK_TABLE = 0x6EC5A8
WHEEL_TASKS = {33: 0x6EB2F8, 35: 0x6EB4F0, 36: 0x6EB6A8, 38: 0x6EB8A0}   # task id -> stock step list
STEP = 19
UNDO_NUT, TIGHTEN_NUT = 0x0B, 0x0E

DUR_TABLE = 0x71ECB8
STOCK_DUR = {
    0x08: (0.25, 0.30, 0.015),   # jack: place jack under car
    0x09: (0.50, 1.00, 0.040),   # jack: pump jack up
    0x0A: (0.30, 0.35, 0.015),   # jack: release jack
    0x0C: (0.25, 0.40, 0.015),   # changer: drop air wrench
    0x0D: (0.35, 0.50, 0.040),   # changer: grab air wrench
    0x38: (0.35, 0.40, 0.050),   # changer: kneel beside wheel
    0x39: (0.50, 0.60, 0.050),   # changer: pull off tire
    0x3A: (0.50, 0.60, 0.050),   # changer: put down tire
    0x3D: (0.35, 0.40, 0.050),   # carrier: place tire beside changer
    0x3E: (0.70, 0.90, 0.090),   # carrier: mount tire
}
FUEL_SITES = ((0x40D6C6, 0x6EA698, 1.35), (0x40D6CF, 0x6EA694, 0.06))   # fmul dword [addr] = D8 0D addr

JACK_OPS = (0x08, 0x09, 0x0A)
TIRE_OPS = (0x0C, 0x0D, 0x38, 0x39, 0x3A, 0x3D, 0x3E)

MODERN = {"lugs": 1, "jack": 0.5, "tires": 0.7, "fuel": 1.8}
STOCK = {"lugs": 5, "jack": 1.0, "tires": 1.0, "fuel": 1.0}
MODERN_MOD_PREFIXES = ("FCRD NCS22",)       # mods that get the modern crew unless the user turns it off


def default_modern(mod: str) -> bool:
    return any((mod or "").startswith(p) for p in MODERN_MOD_PREFIXES)


class PitCrew:
    def __init__(self):
        self._cave: tuple | None = None      # (NR2003 pid, address) of our one-lug step lists (reused)
        self._fuel_cave: tuple | None = None  # (NR2003 pid, address) of our scaled fuel-flow constants
        self.status = ""

    # ------------------------------------------------------------- memory helpers
    @staticmethod
    def _write(pm, addr: int, data: bytes):
        k32 = ctypes.windll.kernel32
        old = wintypes.DWORD()
        h = pm.process_handle
        k32.VirtualProtectEx(h, ctypes.c_void_p(addr), len(data), 0x40, ctypes.byref(old))
        try:
            pm.write_bytes(addr, data, len(data))
        finally:
            k32.VirtualProtectEx(h, ctypes.c_void_p(addr), len(data), old.value, ctypes.byref(old))

    @staticmethod
    def _steps(pm, addr: int) -> list[bytes]:
        out = []
        while pm.read_uint(addr) and len(out) < 64:
            out.append(pm.read_bytes(addr, STEP))
            addr += STEP
        return out

    def _dur_factor(self, pm, op: int) -> float | None:
        cur = struct.unpack("<fff", pm.read_bytes(DUR_TABLE + op * 12, 12))
        stock = STOCK_DUR[op]
        k = cur[0] / stock[0]
        if not 0.05 < k < 5 or any(abs(c - s * k) > 1e-4 for c, s in zip(cur, stock)):
            return None
        return k

    def _check(self, pm) -> str:
        """'' if every address holds what we expect (stock, or changed by us / pitspeed.py)."""
        for op in STOCK_DUR:
            if self._dur_factor(pm, op) is None:
                return f"unexpected step-time table (op {op:02X}) - different NR2003.exe?"
        for task, stock in WHEEL_TASKS.items():
            ops = [s[0] for s in self._steps(pm, stock)]
            if ops.count(UNDO_NUT) != 5 or ops.count(TIGHTEN_NUT) != 5:
                return f"unexpected wheel task {task} - different NR2003.exe?"
        site = pm.read_bytes(SPEED_SITE, len(SPEED_ORIG))
        if site != SPEED_ORIG and site[0] != 0xE9:
            return "unexpected code at the crew-speed site - different NR2003.exe?"
        for at, stock, value in FUEL_SITES:
            if pm.read_bytes(at, 2) != b"\xd8\x0d":
                return "unexpected fuel-flow code - different NR2003.exe?"
            if abs(struct.unpack("<f", pm.read_bytes(stock, 4))[0] - value) > 1e-4:
                return "unexpected fuel-flow constant - different NR2003.exe?"
        return ""

    def fuel_factor(self, pm) -> float:
        at, stock, value = FUEL_SITES[0]
        target = struct.unpack("<I", pm.read_bytes(at + 2, 4))[0]
        return 1.0 if target == stock else struct.unpack("<f", pm.read_bytes(target, 4))[0] / value

    def _set_fuel(self, pm, k: float):
        if abs(k - 1.0) < 1e-6:
            for at, stock, _ in FUEL_SITES:
                self._write(pm, at + 2, struct.pack("<I", stock))
            return
        targets = [struct.unpack("<I", pm.read_bytes(at + 2, 4))[0] for at, _, _ in FUEL_SITES]
        if targets[0] == FUEL_SITES[0][1]:       # still the stock constants: use our own copies
            # (one block per NR2003 process: after the game restarts, the old address is gone - writing it
            # failed with "Could not write memory ... GetLastError: 998")
            if self._fuel_cave is None or self._fuel_cave[0] != pm.process_id:
                self._fuel_cave = (pm.process_id, pm.allocate(16))
            targets = [self._fuel_cave[1], self._fuel_cave[1] + 4]
        for (at, _, value), target in zip(FUEL_SITES, targets):
            pm.write_bytes(target, struct.pack("<f", value * k), 4)
            self._write(pm, at + 2, struct.pack("<I", target))

    # ------------------------------------------------------------- state
    def lugs(self, pm) -> int:
        p = pm.read_uint(TASK_TABLE + 4 * 33)
        if p == WHEEL_TASKS[33]:
            return 5
        return sum(1 for s in self._steps(pm, p) if s[0] == UNDO_NUT) or 5

    def is_modern(self, pm) -> bool:
        k = self._dur_factor(pm, 0x09)
        return self.lugs(pm) == MODERN["lugs"] and k is not None and abs(k - MODERN["jack"]) < 1e-3

    # ------------------------------------------------------------- apply
    def apply(self, pm, modern: bool, cars_on_pit_road: bool = False) -> bool:
        """Set the crew. Returns False when it has to wait (lug change with a car on pit road) or can't."""
        want = MODERN if modern else STOCK
        try:
            bad = self._check(pm)
            if bad:
                self.status = "Pit crew: not changed (" + bad + ")"
                log.warning(self.status)
                return False
            # step times can change any time (a step already under way keeps its length)
            for op in JACK_OPS:
                self._write(pm, DUR_TABLE + op * 12, struct.pack("<fff", *(v * want["jack"] for v in STOCK_DUR[op])))
            for op in TIRE_OPS:
                self._write(pm, DUR_TABLE + op * 12, struct.pack("<fff", *(v * want["tires"] for v in STOCK_DUR[op])))
            site = pm.read_bytes(SPEED_SITE, len(SPEED_ORIG))
            if site[0] == 0xE9:                  # pitspeed.py's crew-speed hook: back to x1.0
                cave = (SPEED_SITE + 5 + struct.unpack("<i", site[1:5])[0] - 4) & 0xFFFFFFFF
                self._write(pm, cave, struct.pack("<f", 1.0))
            if abs(self.fuel_factor(pm) - want["fuel"]) > 1e-3:
                self._set_fuel(pm, want["fuel"])
            if self.lugs(pm) != want["lugs"]:
                if cars_on_pit_road:
                    self.status = "Pit crew: lug change waits until pit road is clear"
                    return False
                self._set_lugs(pm, want["lugs"])
            self.status = ("Pit crew: modern (1 lug, ~10 s for 4 tires, fuel x1.8)" if modern
                           else "Pit crew: stock")
            return True
        except Exception as e:                    # never let this break the tick
            self.status = f"Pit crew: not changed ({e})"
            log.warning(self.status)
            return False

    def _set_lugs(self, pm, n: int):
        if n == 5:
            for task, stock in WHEEL_TASKS.items():
                self._write(pm, TASK_TABLE + 4 * task, struct.pack("<I", stock))
            return
        if self._cave is None or self._cave[0] != pm.process_id:     # (a restarted NR2003 needs its own)
            self._cave = (pm.process_id, pm.allocate(4096))   # zero-filled: trailing zeros end each step list
        at = self._cave[1]
        for task, stock in WHEEL_TASKS.items():
            kept, seen = [], {UNDO_NUT: 0, TIGHTEN_NUT: 0}
            for s in self._steps(pm, stock):
                if s[0] in seen:
                    seen[s[0]] += 1
                    if seen[s[0]] > n:
                        continue
                kept.append(s)
            data = b"".join(kept) + b"\0" * STEP
            pm.write_bytes(at, data, len(data))
            self._write(pm, TASK_TABLE + 4 * task, struct.pack("<I", at))
            at += (len(data) + 15) & ~15
