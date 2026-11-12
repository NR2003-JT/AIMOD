"""NR2003.exe process access: attach, snapshot reads, writes and code patches."""
from __future__ import annotations

import ctypes
import os
import time
import logging
import struct

import pymem
import pymem.process

from . import memory_map as mm
from .carno import CarNo
from .strategy import CarSnapshot, RaceSnapshot, Write

log = logging.getLogger("aimod.game")

PROCESS_NAME = "NR2003.exe"
STILL_ACTIVE = 259
NAME_REFRESH_TICKS = 20


def _cstr(buf: bytes, off: int, length: int) -> str:
    return buf[off:off + length].split(b"\0", 1)[0].decode("latin-1").strip()



RACE_CONTROL_STATE = os.path.join(os.environ.get("LOCALAPPDATA", ""), "NR2003RaceControl", "state.txt")


def race_control_pits_closed() -> bool:
    """Race Control (NR2003 Race Control 2.1.2+) holds pit road closed for the first caution lap without
    touching the game's pit state; it says so in a status file, rewritten every second while it holds."""
    try:
        if time.time() - os.path.getmtime(RACE_CONTROL_STATE) > 3:
            return False
        with open(RACE_CONTROL_STATE, encoding="utf-8", errors="replace") as f:
            return "pits_closed=1" in f.read()
    except (OSError, ValueError):        # (another app's file: never let it break the snapshot)
        return False

def race_control_stage_ends() -> tuple[int, ...] | None:
    """Stage end laps from Race Control 2.7.0+ (it runs the stages): a "stage_ends=84,204" line in its status
    file. None when Race Control isn't running the stages (no line, or the file is stale)."""
    try:
        if time.time() - os.path.getmtime(RACE_CONTROL_STATE) > 10:
            return None
        with open(RACE_CONTROL_STATE, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("stage_ends="):
                    return tuple(int(v) for v in line.split("=", 1)[1].strip().replace(" ", "").split(",") if v.isdigit())
    except (OSError, ValueError):
        pass
    return None


def race_control_wave_around() -> bool | None:
    """RaceControl's wave around option, from its status file ("wave_around=1/0", RaceControl 2.8.8+, rewritten
    every 2 s during a race). True/False from that line; True when the file is fresh but has no such line (an
    older RaceControl, which always waved cars around); None when RaceControl isn't running a race."""
    try:
        if time.time() - os.path.getmtime(RACE_CONTROL_STATE) > 10:
            return None
        with open(RACE_CONTROL_STATE, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("wave_around="):
                    return line.split("=", 1)[1].strip() == "1"
        return True
    except (OSError, ValueError):
        return None


class Game:
    def __init__(self):
        self.pm: pymem.Pymem | None = None
        self.module_base = 0
        self.applied_patches: set[str] = set()
        self.patch_status = "not attached"
        self.write_enabled = False
        self._names: dict[int, tuple[int, str]] = {}
        self._name_tick = 0
        self.make_names: list[str] = list(mm.MAKE_NAMES)

    # ------------------------------------------------------------ lifecycle
    @property
    def attached(self) -> bool:
        if self.pm is None:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(self.pm.process_handle, ctypes.byref(code))
        if not ok or code.value != STILL_ACTIVE:
            log.info("NR2003.exe exited")
            self._drop()
            return False
        return True

    def attach(self) -> bool:
        try:
            pm = pymem.Pymem(PROCESS_NAME)
        except Exception:
            return False
        self.pm = pm
        self.applied_patches.clear()
        self._names.clear()
        try:
            self.module_base = pymem.process.module_from_name(pm.process_handle, PROCESS_NAME).lpBaseOfDll
        except Exception:
            self.module_base = 0x400000
        log.info("Attached to NR2003.exe (pid %s, base 0x%X)", pm.process_id, self.module_base)
        return True

    def _drop(self):
        try:
            if self.pm is not None:
                self.pm.close_process()
        except Exception:
            pass
        self.pm = None
        self.applied_patches.clear()
        self.write_enabled = False
        self.patch_status = "not attached"

    def detach(self, restore_patches: bool = True):
        if self.pm is not None and restore_patches:
            self.restore_patches()
        self._drop()

    # -------------------------------------------------------------- patches
    def apply_patches(self, wanted: list[str]) -> bool:
        """Verify and NOP the requested patch sites. Returns False (and disables
        all writes) if any site holds unexpected bytes, i.e. an unknown exe."""
        if self.pm is None:
            return False
        for key in wanted:
            p = mm.PATCHES[key]
            sites = p.get("sites", (p["addr"],))
            new = p.get("patched", mm.NOP2)
            try:
                curs = [self.pm.read_bytes(a, 2) for a in sites]
            except Exception as e:
                self.patch_status = f"cannot read 0x{p['addr']:X}: {e}"
                self.write_enabled = False
                return False
            if all(c == new for c in curs):
                self.applied_patches.add(key)
                continue
            odd = [(a, c) for a, c in zip(sites, curs) if c not in (new, p["original"])]
            if odd and p.get("optional"):
                log.warning("Skipped patch 0x%X: unexpected bytes %s", odd[0][0], odd[0][1].hex(" "))
                continue
            if odd:
                self.patch_status = (f"UNKNOWN NR2003.exe build (0x{odd[0][0]:X} = {odd[0][1].hex(' ')}) "
                                     "- read-only mode, nothing written")
                log.warning(self.patch_status)
                self.write_enabled = False
                return False
            for a, c in zip(sites, curs):
                if c != new:
                    self.pm.write_bytes(a, new, 2)
            self.applied_patches.add(key)
            log.info("Patched %s", p["label"])
        for key in list(self.applied_patches):
            if key not in wanted:
                self._restore_one(key)
        self.write_enabled = True
        self.patch_status = "patched: " + (", ".join(sorted(self.applied_patches)) or "none")
        return True

    def _restore_one(self, key: str):
        p = mm.PATCHES[key]
        for a in p.get("sites", (p["addr"],)):
            try:
                if self.pm.read_bytes(a, 2) == p.get("patched", mm.NOP2):
                    self.pm.write_bytes(a, p["original"], 2)
                    log.info("Restored original bytes at 0x%X", a)
            except Exception as e:
                log.warning("Could not restore 0x%X: %s", a, e)
        self.applied_patches.discard(key)

    def restore_patches(self):
        for key in list(self.applied_patches):
            self._restore_one(key)

    # ---------------------------------------------------------------- reads
    def _read(self, fn, addr, default):
        try:
            return fn(addr)
        except Exception:
            return default

    def read_tank_from_memory(self) -> float:
        return self._read(self.pm.read_double, mm.TANK_CAPACITY, 0.0)

    def _game_seconds(self) -> float:
        """Game time in seconds from the tick clock (the crossing clock); the old session clock if the ticks can't
        be read. The session clock 0x977A04 can stand still (for whole practice sessions)."""
        pm = self.pm
        ticks = self._read(pm.read_int, mm.GAME_TICKS, 0)
        if not ticks or ticks <= 0:
            return self._read(pm.read_double, mm.SESSION_TIME, 0.0)
        rate = 36 if self._read(pm.read_int, mm.TICK_RATE_FLAG, 1) != 0 else self._read(pm.read_int, mm.TICK_RATE_VALUE, 36)
        if not rate or rate <= 0 or rate > 10000:
            rate = 36
        return ticks / float(rate)

    def snapshot(self) -> RaceSnapshot:
        pm = self.pm
        flag = self._read(pm.read_int, mm.FLAG_STATE, 0)
        session = self._read(lambda a: pm.read_bytes(a, 1)[0], mm.SESSION_STATE, 0)
        laps_done = self._read(pm.read_double, mm.LAPS_CURRENT, 0.0)
        session_time = self._game_seconds()
        laps_total = self._read(pm.read_int, self.module_base + mm.LAPS_TOTAL_RVA, 0)
        tank = self.read_tank_from_memory()

        try:
            pit_road = pm.read_bytes(mm.ON_PIT_ROAD, mm.NUM_CARS)
        except Exception:
            pit_road = None
        refresh_names = self._name_tick % NAME_REFRESH_TICKS == 0
        self._name_tick += 1
        try:
            _n, laps_cap, shift, table = struct.unpack("<IIII", pm.read_bytes(mm.SCORING_HDR, 16))
            if not (0 < laps_cap <= 4096 and 0 < shift <= 14 and table):
                table = 0
        except Exception:
            laps_cap = shift = table = 0
        cars = []
        pace_dist = None
        for i in range(mm.NUM_CARS):
            base = mm.CAR_BLOCK_0 + i * mm.CAR_STRIDE
            try:
                buf = pm.read_bytes(base, mm.CAR_STRIDE)
            except Exception:
                cars.append(CarSnapshot(i, valid=False))
                continue
            f = lambda off: struct.unpack_from("<f", buf, off)[0]
            if i == mm.PACE_SLOT:
                pd = f(mm.OFF_LAP_DIST)
                pace_dist = pd if pd == pd and 0 <= pd < 100000 else None
            grip = f(mm.OFF_GRIP)
            grip_real = True
            if not (0.01 < grip <= 1.05):
                if i != mm.PLAYER_SLOT:
                    cars.append(CarSnapshot(i, valid=False))
                    continue
                grip, grip_real = 1.0, False      # the AI grip figure isn't kept for the player's car
                moving = (struct.unpack_from("<i", buf, mm.OFF_LAPS_DONE)[0] > 0
                          or (f(mm.OFF_VEL_X) ** 2 + f(mm.OFF_VEL_X + 4) ** 2) > 1.0)
                if not moving:                    # spectating (no car of your own), or not rolling yet
                    cars.append(CarSnapshot(i, valid=False))
                    continue
            if refresh_names or i not in self._names:
                self._names[i] = self._driver(i)
            number, name = self._names[i]
            if number <= 0 or name.lower() == "pace car":
                cars.append(CarSnapshot(i, valid=False))
                continue
            damage = (f(mm.OFF_DAMAGE_FRONT) + f(mm.OFF_DAMAGE_REAR)
                      + f(mm.OFF_DAMAGE_LEFT) + f(mm.OFF_DAMAGE_RIGHT))
            car_laps = struct.unpack_from("<i", buf, mm.OFF_LAPS_DONE)[0]    # (not laps_done: that's the race's)
            dist = f(mm.OFF_LAP_DIST)
            vel = (f(mm.OFF_VEL_X), f(mm.OFF_VEL_X + 4))
            pos = (f(mm.OFF_POS_X), f(mm.OFF_POS_X + 4))
            speed = (vel[0] ** 2 + vel[1] ** 2) ** 0.5
            fuel, cap = self._fuel(buf, i)
            cross = prev_cross = None
            if table and 1 <= car_laps < laps_cap and i < 48:
                try:
                    prev_cross, cross = (v & mm.CROSS_MASK for v in struct.unpack(
                        "<II", pm.read_bytes(table + (i << (shift + 2)) + (car_laps - 1) * 4, 8)))
                except Exception:
                    cross = prev_cross = None
            cars.append(CarSnapshot(
                i, True, grip=grip, grip_real=grip_real, fuel=fuel, fuel_cap=cap, game_penalty=bool(buf[mm.OFF_GAME_PENALTY_STOP]),
                game_condition=f(mm.OFF_GAME_CONDITION),
                game_repair=struct.unpack_from("<i", buf, mm.OFF_GAME_REPAIR)[0],
                pit_mode=struct.unpack_from("<I", buf, mm.OFF_AI_MODE)[0] in mm.PIT_MODES, cross_ms=cross or None, prev_cross_ms=prev_cross or None, strategy=f(mm.OFF_STRATEGY), reason_out=buf[mm.OFF_REASON_OUT],
                pitting=buf[mm.OFF_PITTING], wear_lf=f(mm.OFF_WEAR_LF), wear_rf=f(mm.OFF_WEAR_RF),
                wear_lr=f(mm.OFF_WEAR_LR), wear_rr=f(mm.OFF_WEAR_RR), damage=damage,
                number=number, name=name,
                laps_done=car_laps if 0 <= car_laps < 10000 else 0,
                lap_dist=dist if dist == dist and 0 <= dist < 100000 else 0.0,
                side_flag=buf[mm.OFF_SIDE_FLAG], controlled=i != mm.PLAYER_SLOT,
                make_id=buf[mm.OFF_MAKE], make=self.make_name(buf[mm.OFF_MAKE]),
                speed=speed if speed == speed and speed < 500 else None,
                on_pit_road=bool(pit_road[i]) if pit_road is not None else None,
                pit_timer=self._timer(f(mm.OFF_PIT_TIMER)),
                pos=pos if all(v == v and abs(v) < 1e6 for v in pos) else None,
                vel=vel if all(v == v and abs(v) < 500 for v in vel) else None))
        snap = RaceSnapshot(flag, session, tank, laps_done, laps_total, cars)
        snap.pace_dist = pace_dist
        snap.pits_closed = flag != mm.FLAG_GREEN and race_control_pits_closed()
        snap.stage_ends = race_control_stage_ends()
        snap.wave_around = race_control_wave_around()
        if session_time == session_time and 0 < session_time < 1e7:
            snap.session_time = session_time
        leader = max((c.laps_done for c in cars if c.valid), default=0)
        if leader > 0:
            # Leader's lap from the per-car counters: works while spectating, keeps
            # counting past the scheduled distance (green-white-checkered), and
            # doesn't depend on the companion app.
            snap.laps_done = float(leader + 1)
            snap.lap_source = "game"
        return snap

    def crossing_history(self, idx: int, laps: int) -> list[int]:
        """Game ms at which car slot `idx` completed laps 0..laps, from NR2003's scoring table (entry n = lap n;
        entry 0 is the start). Empty when the table can't be read. Read-only."""
        pm = self.pm
        try:
            _n, laps_cap, shift, table = struct.unpack("<IIII", pm.read_bytes(mm.SCORING_HDR, 16))
            if not (0 < laps_cap <= 4096 and 0 < shift <= 14 and table and 0 <= idx < 48):
                return []
            n = min(laps + 1, laps_cap)
            raw = pm.read_bytes(table + (idx << (shift + 2)), n * 4)
            return [v & mm.CROSS_MASK for v in struct.unpack(f"<{n}I", raw)]
        except Exception:
            return []

    def _fuel(self, buf, i) -> tuple[float | None, float | None]:
        """(fuel, capacity) in game units, or (None, None). The two blocks are read without the game's
        lock, so a value that doesn't make sense (torn read) is dropped."""
        pm = self.pm
        try:
            pa, pb, pc = (struct.unpack_from("<I", buf, off)[0] for off in
                          (mm.OFF_FUEL_A, mm.OFF_FUEL_B, mm.OFF_FUEL_CAP_PTR))
            a, b = pm.read_bytes(pa, 8), pm.read_bytes(pb, 8)
            fuel = struct.unpack("<d", bytes(x ^ y for x, y in zip(a, b)))[0]
            cap = struct.unpack("<f", pm.read_bytes(pc + mm.FUEL_CAP_OFF, 4))[0]
        except Exception:
            return None, None
        if not (cap == cap and 1.0 < cap < 10000.0) or not (fuel == fuel and -0.01 <= fuel <= cap * 1.05):
            return None, None
        return max(0.0, fuel), cap

    @staticmethod
    def _timer(v: float):
        return v if v == v and 0 <= v < 3600 else None

    def session_names(self) -> tuple[str, str]:
        """(track folder, mod series folder) of the loaded session, from NR2003's memory."""
        for addr in mm.TRACK_NAME_ADDRS:
            try:
                buf = self.pm.read_bytes(addr, mm.SERIES_NAME_OFFSET * 2)
            except Exception:
                continue
            track = _cstr(buf, 0, mm.SERIES_NAME_OFFSET)
            if track and track.isprintable():
                return track, _cstr(buf, mm.SERIES_NAME_OFFSET, mm.SERIES_NAME_OFFSET)
        return "", ""

    def nr2003_dir(self) -> str:
        from . import carfiles
        try:
            return _cstr(self.pm.read_bytes(carfiles.NR2003_DIR, 260), 0, 260)
        except Exception:
            return ""

    def session_car_files(self, mod: str) -> list[str]:
        """Full paths of the .car files of the cars in the loaded session (player slot included)."""
        from . import carfiles
        try:
            base = _cstr(self.pm.read_bytes(carfiles.NR2003_DIR, 260), 0, 260)
        except Exception:
            return []
        if not base or not mod:
            return []
        folder = carfiles.cars_folder(base, mod)
        out = []
        for i in range(mm.NUM_CARS):
            if i == mm.PACE_SLOT:
                continue
            try:
                name = _cstr(self.pm.read_bytes(mm.CAR_BLOCK_0 + i * mm.CAR_STRIDE + carfiles.OFF_CAR_FILE,
                                                carfiles.CAR_FILE_LEN), 0, carfiles.CAR_FILE_LEN)
            except Exception:
                continue
            if name and name.lower().endswith(".car") and name.isprintable():
                path = os.path.join(folder, name)
                if os.path.isfile(path):
                    out.append(path)
        return out

    def black_flags(self) -> set[int]:
        """Car slots currently shown the black flag by any penalty rule."""
        out = set()
        try:
            mgr = self.pm.read_uint(mm.PENALTY_MGR)
            arr, cnt = self.pm.read_uint(mgr + 4), self.pm.read_int(mgr + 8)
            for k in range(min(max(cnt, 0), 64)):
                rule = self.pm.read_uint(arr + 4 * k)
                if not rule:
                    continue
                buf = self.pm.read_bytes(rule + 4, 4 * mm.NUM_CARS)
                for i, (v,) in enumerate(struct.iter_unpack("<I", buf)):
                    if v & mm.PENALTY_BLACK_BIT:
                        out.add(i)
        except Exception:
            return set()
        return out

    def rules_pending(self) -> set[int]:
        """Car slots a penalty rule wants in: the check NR2003's green pit check runs (0x534F40 -> each rule's vtable
        +0x30), read from the rule states. Most rules want a stop while the low 4 bits of the car's state are set,
        rules with 0x422A20 / 0x422C70 at state 3, 0x422C00 at state 5; the rest never. This is set before the black
        flag shows (bit 16), so it catches the penalty stops AIMOD saw as plain game requests."""
        out = set()
        try:
            mgr = self.pm.read_uint(mm.PENALTY_MGR)
            arr, cnt = self.pm.read_uint(mgr + 4), self.pm.read_int(mgr + 8)
            for k in range(min(max(cnt, 0), 64)):
                rule = self.pm.read_uint(arr + 4 * k)
                if not rule:
                    continue
                check = self.pm.read_uint(self.pm.read_uint(rule) + 0x30)
                kind = mm.RULE_CHECKS.get(check)
                if kind is None:
                    continue
                buf = self.pm.read_bytes(rule + 4, 4 * mm.NUM_CARS)
                for i, (v,) in enumerate(struct.iter_unpack("<I", buf)):
                    s = v & 0xF
                    if (s != 0) if kind == 0 else (s == kind):
                        out.add(i)
        except Exception:
            return set()
        return out

    def clear_penalties(self, slots) -> int:
        """Wipe every penalty rule's state for these car slots: the black flag bit and the "owed" nibble. The higher
        bits (served / history) stay. Returns how many rule entries changed."""
        n = 0
        if not self.write_enabled or self.pm is None:
            return 0
        keep = ~(mm.PENALTY_BLACK_BIT | 0xF) & 0xFFFFFFFF
        try:
            mgr = self.pm.read_uint(mm.PENALTY_MGR)
            arr, cnt = self.pm.read_uint(mgr + 4), self.pm.read_int(mgr + 8)
            for k in range(min(max(cnt, 0), 64)):
                rule = self.pm.read_uint(arr + 4 * k)
                if not rule:
                    continue
                for i in slots:
                    addr = rule + 4 + i * 4
                    v = self.pm.read_uint(addr)
                    if v & ~keep & 0xFFFFFFFF:
                        self.pm.write_bytes(addr, struct.pack("<I", v & keep), 4)
                        n += 1
        except Exception as e:
            log.debug("clear penalties failed: %s", e)
        return n

    def make_name(self, make_id: int) -> str:
        names = self.make_names or mm.MAKE_NAMES
        return names[make_id] if 0 <= make_id < len(names) else f"Make {make_id}"

    def _driver(self, i: int) -> tuple[int, str]:
        try:
            buf = self.pm.read_bytes(mm.DRIVER_ENTRY_0 + i * mm.DRIVER_ENTRY_SIZE, mm.DRIVER_ENTRY_SIZE)
        except Exception:
            return 0, "AI"
        number = CarNo(struct.unpack_from("<i", buf, mm.DE_OFF_CARNUM)[0])   # 2001 shows as "01"
        first = _cstr(buf, mm.DE_OFF_FIRST, 16)
        last = _cstr(buf, mm.DE_OFF_LAST, 20) or "AI"
        return number, f"{first} {last}".strip()

    # --------------------------------------------------------------- writes
    def hold_off(self, idxs) -> int:
        """Pit guard: clear the pit request of these cars if the game set it (called many times a second, between
        AIMOD's ticks). Returns how many it cleared."""
        if not self.write_enabled or self.pm is None:
            return 0
        n = 0
        for i in idxs:
            addr = mm.CAR_BLOCK_0 + i * mm.CAR_STRIDE + mm.OFF_PITTING
            try:
                if self.pm.read_bytes(addr, 1) != b"\x00":
                    self.pm.write_bytes(addr, b"\x00", 1)
                    n += 1
            except Exception:
                pass
        return n

    def apply_writes(self, writes: list[Write]):
        if not self.write_enabled or self.pm is None:
            return
        for w in writes:
            try:
                self.pm.write_bytes(mm.CAR_BLOCK_0 + w.idx * mm.CAR_STRIDE + w.offset, bytes([w.value]), 1)
            except Exception as e:
                log.debug("write failed car %d +%d: %s", w.idx, w.offset, e)
