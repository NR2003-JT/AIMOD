"""Pit gate: AIMOD decides which AI cars head for pit road (runtime only: NR2003.exe is never changed on disk).

Why. NR2003's AI picks its own pit stops in many places - its fuel/tire plan, a "car condition" value it rates as
"pit now" between 0.5 and 1.0 (a stall stop never fixes it, so the car came in every few laps for the rest of the
race: #33 at Bristol 2026-11-02, 126 stops), its caution logic (every car with a reason, at the first chance) and
lap counters. Most of them never touch the pit request byte (+8118): the AI switches itself straight into a
"head for pit road" mode, and from then on the stop goes ahead whatever the request says. So clearing the request
(AIMOD's green guard, the 20 ms pit guard thread) comes too late: "came in without a request", "the game sent it in
unasked".

How. Every AI mode change goes through one routine, 0x52A1B0 (ecx = car block, [esp+4] = the new mode object).
The three modes that take a car off the track towards pit road are 0x760F48, 0x760D94 and 0x760F78 (their entry
routines 0x405AB0 / 0x405D90 / 0x405F20 set +8118 themselves). A 5-byte jump at 0x52A1B0 leads to a small cave that
lets such a change through only when:
  - AIMOD's own allow byte for the car is set (AIMOD called it in or accepted the game's ask, a penalty rule wants
    it in, the tank is nearly empty). Version 1 trusted the pit request +8118 instead, but the game writes that too:
    its strategy re-plan 0x5425FA..0x542626 sets it whenever its new plan says "pit now" (#6 / #88 / #50 at Bristol
    2026-11-02 came in under green 7-9 laps after a caution stop), or
  - +8119 is set (a penalty / repair / tow stop to serve), or
  - the game's repair need +0x1B33 is above 0 (damage the stall repair fixes), or
  - the car is already on pit road, or it is the player / pace car, or
  - the gate is not armed.
Otherwise the change is refused (the car keeps its current mode and stays on track) and a per-car counter goes up.
Modes on pit road itself (0x760F54 / 0x760F90), limp, tow and repair modes are never touched, so a car can't be
trapped on pit road or in its stall.

Armed by a deadline in game ticks that AIMOD moves forward every tick during a race: if AIMOD closes or hangs, the
gate opens by itself within a minute of game time and NR2003 runs its stock pit logic again.
"""
from __future__ import annotations

import ctypes
import logging
import struct
from ctypes import wintypes

from . import memory_map as mm

log = logging.getLogger("aimod.pitgate")

HOOK_SITE = 0x52A1B0
HOOK_ORIG = bytes.fromhex("568bf1578b7c240c")   # push esi / mov esi,ecx / push edi / mov edi,[esp+0Ch]
HOOK_RESUME = HOOK_SITE + len(HOOK_ORIG)       # 0x52A1B8, an instruction boundary in both versions
GATED_MODES = (0x760F48, 0x760D94, 0x760F78)    # "head for pit road" (caution / green variants)

MAGIC = b"AIMGATE2"
OLD_CAVES = ((b"AIMGATE1", 0x140),)   # earlier gate versions (magic, code offset): replaced on install
CAVE_SIZE = 0x400
OFF_DEADLINE = 0x08          # dword: armed while the game's tick counter is below this
OFF_TOTAL = 0x0C             # dword: changes refused so far
OFF_VERSION = 0x10
OFF_COUNTS = 0x20            # 64 dwords: changes refused per car slot
OFF_ALLOW = 0x120            # 64 bytes: 1 = AIMOD lets this car head for pit road (written every tick)
OFF_CODE = 0x180
MAX_SLOTS = 64
VERSION = 2

ON_PIT_ROAD = mm.ON_PIT_ROAD


def build_code(cave: int) -> bytes:
    """Machine code for the cave at address `cave` (code starts at cave + OFF_CODE)."""
    code = bytearray()
    fix: list[tuple[int, str]] = []       # (offset of a rel32, label)
    labels: dict[str, int] = {}

    def emit(b: bytes):
        code.extend(b)

    def jcc(op: bytes, label: str):       # op = b"\x0f\x84" (je) etc., or b"\xe9" (jmp)
        emit(op)
        fix.append((len(code), label))
        emit(b"\0\0\0\0")

    d32 = lambda v: struct.pack("<I", v & 0xFFFFFFFF)
    first_ai = mm.CAR_BLOCK_0 + 2 * mm.CAR_STRIDE            # slot 0 = player, slot 1 = pace car
    past_last = mm.CAR_BLOCK_0 + MAX_SLOTS * mm.CAR_STRIDE

    emit(b"\x8b\x44\x24\x04")                                # mov eax,[esp+4]      new mode
    for m in GATED_MODES:
        emit(b"\x3d" + d32(m))                               # cmp eax, mode
        jcc(b"\x0f\x84", "chk")                              # je chk
    jcc(b"\xe9", "orig")                                     # jmp orig
    labels["chk"] = len(code)
    emit(b"\xa1" + d32(mm.GAME_TICKS))                       # mov eax,[ticks]
    emit(b"\x3b\x05" + d32(cave + OFF_DEADLINE))             # cmp eax,[deadline]
    jcc(b"\x0f\x83", "orig")                                 # jae orig             not armed
    emit(b"\x81\xf9" + d32(first_ai))                        # cmp ecx, first AI car
    jcc(b"\x0f\x82", "orig")                                 # jb orig              player / pace car
    emit(b"\x81\xf9" + d32(past_last))                       # cmp ecx, past the last slot
    jcc(b"\x0f\x83", "orig")                                 # jae orig
    emit(b"\x80\xb9" + d32(mm.OFF_GAME_PENALTY_STOP) + b"\x00")   # cmp byte [ecx+8119],0
    jcc(b"\x0f\x85", "orig")                                 # jne orig             penalty / repair / tow
    emit(b"\x83\xb9" + d32(mm.OFF_GAME_REPAIR) + b"\x00")    # cmp dword [ecx+1B33h],0
    jcc(b"\x0f\x8f", "orig")                                 # jg orig              needs repairs
    emit(b"\x52\x51")                                        # push edx / push ecx
    emit(b"\x8b\xc1")                                        # mov eax,ecx
    emit(b"\x2d" + d32(mm.CAR_BLOCK_0))                      # sub eax, car block 0
    emit(b"\x33\xd2")                                        # xor edx,edx
    emit(b"\xb9" + d32(mm.CAR_STRIDE))                       # mov ecx, stride
    emit(b"\xf7\xf1")                                        # div ecx              eax = slot
    emit(b"\x59\x5a")                                        # pop ecx / pop edx
    emit(b"\x80\xb8" + d32(ON_PIT_ROAD) + b"\x00")           # cmp byte [eax+on_pit_road],0
    jcc(b"\x0f\x85", "orig")                                 # jne orig             already on pit road
    emit(b"\x80\xb8" + d32(cave + OFF_ALLOW) + b"\x00")      # cmp byte [eax+allow],0
    jcc(b"\x0f\x85", "orig")                                 # jne orig             AIMOD lets it in
    emit(b"\xff\x04\x85" + d32(cave + OFF_COUNTS))           # inc dword [eax*4+counts]
    emit(b"\xff\x05" + d32(cave + OFF_TOTAL))                # inc dword [total]
    emit(b"\xc2\x04\x00")                                    # ret 4                refused: mode unchanged
    labels["orig"] = len(code)
    emit(HOOK_ORIG)                                          # the routine's own first instructions
    jcc(b"\xe9", "resume")
    start = cave + OFF_CODE
    labels["resume"] = HOOK_RESUME - start                   # (absolute target as an offset from the code start)
    for at, lab in fix:
        rel = labels[lab] - (at + 4)
        code[at:at + 4] = struct.pack("<i", rel)
    return bytes(code)


def hook_bytes(cave: int) -> bytes:
    target = cave + OFF_CODE
    return b"\xe9" + struct.pack("<i", target - (HOOK_SITE + 5)) + b"\x90" * (len(HOOK_ORIG) - 5)


class PitGate:
    def __init__(self):
        self.cave: int | None = None
        self.pid: int | None = None
        self.status = ""
        self.failed = ""                  # why it can't be installed in this NR2003 process
        self._counts: list[int] | None = None

    # ------------------------------------------------------------- memory helpers
    @staticmethod
    def _write_code(pm, addr: int, data: bytes):
        k32 = ctypes.windll.kernel32
        old = wintypes.DWORD()
        h = pm.process_handle
        k32.VirtualProtectEx(h, ctypes.c_void_p(addr), len(data), 0x40, ctypes.byref(old))
        try:
            pm.write_bytes(addr, data, len(data))
        finally:
            k32.VirtualProtectEx(h, ctypes.c_void_p(addr), len(data), old.value, ctypes.byref(old))

    def _find_ours(self, pm, site: bytes) -> tuple[int | None, int | None]:
        """(this version's cave, an older version's cave) behind an existing jump at the hook site (AIMOD restarted
        with the game open)."""
        if site[0] != 0xE9:
            return None, None
        target = (HOOK_SITE + 5 + struct.unpack("<i", site[1:5])[0]) & 0xFFFFFFFF

        def has(addr: int, magic: bytes) -> bool:
            # (each look on its own: an older, smaller cave puts this version's magic offset in front of its
            # allocation - unreadable memory - and that must not stop the look for the older magic)
            try:
                return pm.read_bytes(addr, len(magic)) == magic
            except Exception:
                return False

        if has(target - OFF_CODE, MAGIC):
            return target - OFF_CODE, None
        for magic, off in OLD_CAVES:
            if has(target - off, magic):
                return None, target - off
        return None, None

    # ------------------------------------------------------------- install / remove
    def install(self, pm) -> bool:
        if self.cave is not None and self.pid == pm.process_id:
            return True
        if self.failed and self.pid == pm.process_id:
            return False
        self.cave, self.pid, self.failed, self._counts = None, pm.process_id, "", None
        try:
            site = pm.read_bytes(HOOK_SITE, len(HOOK_ORIG))
            ours, old = self._find_ours(pm, site)
            if ours is not None:
                self.cave = ours
                pm.write_bytes(ours + OFF_DEADLINE, struct.pack("<I", 0), 4)
                pm.write_bytes(ours + OFF_ALLOW, b"\0" * MAX_SLOTS, MAX_SLOTS)
                self.status = "Pit gate: ready (kept from the last AIMOD run)"
                log.info(self.status)
                return True
            if old is not None:
                pm.write_bytes(old + OFF_DEADLINE, struct.pack("<I", 0), 4)    # open the old one, then replace it
                log.info("Pit gate: replacing an older AIMOD gate (cave 0x%X)", old)
            elif site != HOOK_ORIG:
                self.failed = f"unexpected code at 0x{HOOK_SITE:X} ({site.hex(' ')}) - different NR2003.exe?"
                self.status = "Pit gate: off (" + self.failed + ")"
                log.warning(self.status)
                return False
            cave = pm.allocate(CAVE_SIZE)
            header = MAGIC + struct.pack("<III", 0, 0, VERSION)
            pm.write_bytes(cave, header, len(header))
            pm.write_bytes(cave + OFF_COUNTS, b"\0" * (4 * MAX_SLOTS), 4 * MAX_SLOTS)
            pm.write_bytes(cave + OFF_ALLOW, b"\0" * MAX_SLOTS, MAX_SLOTS)
            code = build_code(cave)
            pm.write_bytes(cave + OFF_CODE, code, len(code))
            self._write_code(pm, HOOK_SITE, hook_bytes(cave))
            self.cave = cave
            self.status = "Pit gate: ready"
            log.info("Pit gate installed at 0x%X (cave 0x%X): only AIMOD sends AI cars to pit road", HOOK_SITE, cave)
            return True
        except Exception as e:
            self.failed = str(e)
            self.status = f"Pit gate: off ({e})"
            log.warning(self.status)
            return False

    def remove(self, pm):
        """Disarm and put the routine's own bytes back (the cave stays allocated: a game thread may be in it)."""
        if pm is None or self.cave is None or self.pid != pm.process_id:
            return
        try:
            pm.write_bytes(self.cave + OFF_DEADLINE, struct.pack("<I", 0), 4)
            if pm.read_bytes(HOOK_SITE, len(HOOK_ORIG)) == hook_bytes(self.cave):
                self._write_code(pm, HOOK_SITE, HOOK_ORIG)
                log.info("Pit gate removed (stock pit logic)")
        except Exception as e:
            log.warning("Could not remove the pit gate: %s", e)
        self.cave, self._counts = None, None

    def disarm(self, pm):
        if pm is None or self.cave is None or self.pid != pm.process_id:
            return
        try:
            pm.write_bytes(self.cave + OFF_DEADLINE, struct.pack("<I", 0), 4)
        except Exception:
            pass

    # ------------------------------------------------------------- per tick
    def arm(self, pm, game_seconds: float = 60.0) -> bool:
        """Keep the gate closed for the next `game_seconds` of game time."""
        if not self.install(pm):
            return False
        try:
            ticks = pm.read_int(mm.GAME_TICKS)
            rate = 36 if pm.read_int(mm.TICK_RATE_FLAG) != 0 else pm.read_int(mm.TICK_RATE_VALUE)
            if not rate or not 0 < rate <= 10000:
                rate = 36
            if ticks < 0:
                return False
            pm.write_bytes(self.cave + OFF_DEADLINE, struct.pack("<I", ticks + int(rate * game_seconds)), 4)
            return True
        except Exception as e:
            self.status = f"Pit gate: off ({e})"
            return False

    def set_allow(self, pm, slots) -> bool:
        """The cars AIMOD lets head for pit road right now (everything else is held on track while armed)."""
        if pm is None or self.cave is None or self.pid != pm.process_id:
            return False
        data = bytes(1 if i in slots else 0 for i in range(MAX_SLOTS))
        try:
            pm.write_bytes(self.cave + OFF_ALLOW, data, MAX_SLOTS)
            return True
        except Exception:
            return False

    def held(self, pm) -> dict[int, int]:
        """Slots whose pit-road change the gate refused since the last call, with how many times."""
        if pm is None or self.cave is None or self.pid != pm.process_id:
            return {}
        try:
            now = list(struct.unpack(f"<{MAX_SLOTS}I", pm.read_bytes(self.cave + OFF_COUNTS, 4 * MAX_SLOTS)))
        except Exception:
            return {}
        last, self._counts = self._counts, now
        if last is None:
            return {}
        return {i: n - o for i, (n, o) in enumerate(zip(now, last)) if n > o}
