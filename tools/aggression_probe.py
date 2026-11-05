"""AI aggression probe: checks where NR2003 keeps each AI driver's aggression, then (optionally) changes it for one
car so you can watch what it does.

Static disassembly of NR2003.exe (2026-11-03) says:
  - each driver's 15 .car ratings are loaded (0x4F7820) into the driver block at car + 0x16BE as triplets
    at +0x30 + 12k: mean, deviation, working value (the one the AI uses). Aggression is k = 0, so its working
    value is car + 0x16F6.
  - it is read only by the car-next-to-car code (0x530110 and the classifier around 0x53244A): with
    A = aggression x 0.5 the side-by-side room a driver insists on shrinks (3.4035 - 1.25 A, 3.9035 - 1.5 A,
    5.4035 - 2.25 A; a car is ~1.9035 wide). More aggression = less room left = closer racing, more rubbing.
  - nothing rewrites it during a race (only the session load).
Live 2026-11-03 (Bristol, WinstonCup98, 42 cars): the location matched every .car file (42/42). The game re-rolls
every driver's working value once at the session start (mean + a small random draw from the deviation; all 42 at
the race start, flag 5, lap 0), then left it alone for the 28 minutes of racing that followed. So run the write
test once the race is under way.
This tool checks all of that live.

READ (default, never writes):
    python tools\\aggression_probe.py
  1. a table per AI car: the aggression triplet in memory next to [AIParamMean] / [AIParamDeviation] of its .car
     file, plus chassis / engine as a check that the rating layout is right;
  2. then it keeps watching every AI car's working value and logs any change the game makes (leave it running
     through green laps, a caution and pit stops: there should be none). Ctrl+C to stop.

WRITE TEST (one AI car, always put back):
    python tools\\aggression_probe.py --test 24 [--low 0.2] [--high 2.0]
    python tools\\aggression_probe.py --test 3 --steps 0.5,2.4,3.0 --auto 150
  (without --low / --high: half the field's lowest value and 1.5 x its highest, capped below a car width)
  Steps through: game value -> LOW -> HIGH -> game value (or game value -> each --steps value -> game value), one
  step each time you press Enter, or by itself after --auto seconds of green-flag game time (q = put it back and
  quit). Every step is measured 20 times a second, weighted by game time so time acceleration doesn't skew it:
    - side by side: how much of the time a car is alongside, and the side gap (centre to centre; ~1.9 m = touching)
    - following: how much of the time a car is right ahead in line, and how close it follows
    - the car's 8 relation slots (situation type 0-10, the room value it worked out): time per type, mean room
    - contacts: this car's damage going up, or a car within 6 m taking damage, with who was next to it
  A summary is logged at the end of each step and a comparison table at the end. Only green-flag time on track
  counts (not pit road, not under yellow). Watch the car on track and note what you see at each step.
  The game value is written back when you quit, on Ctrl+C, and if the session changes under it.

Run in an ADMIN terminal (NR2003 runs as administrator), with a session loaded:
    Start-Process powershell -Verb RunAs -ArgumentList '-NoExit','-Command','cd "<AIMOD folder>"; python tools\\aggression_probe.py'
Log: AIMOD\\app\\logs\\aggression_probe_<date>_<time>.txt
"""
from __future__ import annotations

import argparse
import datetime
import math
import os
import struct
import sys
import time
from collections import defaultdict

import pymem

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from aimod import carfiles                       # noqa: E402
from aimod import memory_map as mm               # noqa: E402
from aimod.carno import CarNo, label, raw        # noqa: E402
from aimod.game import Game                      # noqa: E402

RATING_BASE = mm.OFF_DRIVER + 0x30               # car + 0x16EE: triplet k at +12k (mean, deviation, working)
RATINGS = ["DriverAggression", "DriverConsistency", "DriverFinishing", "DriverQualifying", "DriverRoadCourse",
           "DriverShortTrack", "DriverSpeedway", "DriverSuperspeedway", "VehicleAero", "VehicleEngine",
           "VehicleChassis", "VehicleReliability", "PitcrewConsistency", "PitcrewSpeed", "PitcrewStrategy"]
AGG = 0
OFF_AGG_WORK = RATING_BASE + 12 * AGG + 8        # car + 0x16F6
REL_BASE, REL_STRIDE, REL_SLOTS = 0x13C3, 0x5D, 8
REL_ACTIVE, REL_TYPE, REL_OTHER, REL_ROOM = 0x10, 0x18, 0x1C, 0x49
OFF_ROOM_MAX = 0x1BAD                            # the largest room value over the car's relations
CAR_WIDTH = 1.9035
SAFE_MAX = 2 * (3.4035 - CAR_WIDTH) / 1.25       # ~2.4: above this the tightest threshold is under a car width
HARD_MAX = 4.0                                   # all three side thresholds meet at ~0.9 m (half a car) here
WATCH_S = 0.5

# live test sampling
SAMPLE_S = 0.05                                  # 20 a second
SPAN_LO = mm.OFF_DAMAGE_BASE                     # one read per car covers damage, out flag, position, velocity
SPAN_HI = mm.OFF_VEL_X + 8
DAMAGE_OFFS = (mm.OFF_DAMAGE_FRONT, mm.OFF_DAMAGE_REAR, mm.OFF_DAMAGE_LEFT, mm.OFF_DAMAGE_RIGHT)
ALONG_M, SIDE_MIN_M, SIDE_MAX_M = 5.0, 0.9, 4.3  # "alongside", as the spotter calls it (radio.py)
TIGHT_M = 2.4                                    # side gap (centre to centre) under ~0.5 m of daylight
AHEAD_M, IN_LINE_M = 15.0, 1.5                   # a car right ahead in the same line
NEAR_CONTACT_M = 6.0                             # a car this close taking damage counts as contact with this car
CONTACT_MERGE_S = 1.0                            # damage within a game second of the last = the same contact
DMG_EPS = 1e-4
MIN_SPEED = 15.0                                 # m/s: below this the car isn't racing (spin, pit lane)
AGG_TYPES = (5, 6, 7, 8)                         # the relation cases that read aggression (0x530272 / 0x5305F5)
PROGRESS_S = 15.0


def stamp() -> str:
    return f"{datetime.datetime.now():%H:%M:%S.%f}"[:-3]


class StepStats:
    """What one test step saw. Every figure is weighted by game time, so time acceleration doesn't skew it."""

    def __init__(self, name: str, value: float):
        self.name, self.value = name, value
        self.green = 0.0                         # game seconds measured (green, on track, at speed)
        self.along = self.lat_sum = self.tight = 0.0
        self.lat_min = math.inf
        self.behind = self.gap_sum = 0.0
        self.gap_min = math.inf
        self.rel_t = defaultdict(float)          # type -> game s with at least one relation of that type
        self.rel_w = defaultdict(float)          # type -> relation-seconds (for the mean room)
        self.rel_room = defaultdict(float)
        self.rel_min: dict[int, float] = {}
        self.max_t = self.max_sum = 0.0          # car+0x1BAD while it is above 0
        self.contacts: list[list] = []           # [game t of last damage, partner, own dmg, partner dmg, dist m]
        self.dmg0 = self.dmg1 = None
        self.laps0 = self.laps1 = None
        self.pos0 = self.pos1 = None

    def pct(self, t: float) -> float:
        return 100.0 * t / self.green if self.green else 0.0

    def agg_room(self) -> tuple[float, float]:
        """mean room over the relation cases that read aggression, and the share of green time in them"""
        w = sum(self.rel_w[t] for t in AGG_TYPES)
        room = sum(self.rel_room[t] for t in AGG_TYPES) / w if w else math.nan
        # time in any of them can't be summed from rel_t (types overlap); rel_t[-1] holds it
        return room, self.pct(self.rel_t[-1])

    def car_contacts(self) -> list[list]:
        return [c for c in self.contacts if c[1] != "no car near"]

    def lines(self) -> list[str]:
        g = self.green
        out = [f"SUMMARY [{self.name} {self.value:.3f}]: {g:.0f} s of green-flag game time"
               + (f", {self.laps1 - self.laps0} laps" if self.laps0 is not None and self.laps1 is not None else "")
               + (f", running P{self.pos0} -> P{self.pos1}" if self.pos0 and self.pos1 else "")]
        if not g:
            return out + ["  nothing measured (no green-flag time on track during this step)"]
        if self.along:
            out.append(f"  side by side: {self.pct(self.along):.1f}% of the time ({self.along:.0f} s); side gap "
                       f"mean {self.lat_sum / self.along:.2f} m, min {self.lat_min:.2f} m (~{CAR_WIDTH:.1f} = "
                       f"touching); tight (< {TIGHT_M} m) {100 * self.tight / self.along:.0f}% of that")
        else:
            out.append("  side by side: never")
        if self.behind:
            out.append(f"  right behind a car (< {AHEAD_M:.0f} m, same line): {self.pct(self.behind):.1f}% of the "
                       f"time; gap mean {self.gap_sum / self.behind:.1f} m, min {self.gap_min:.1f} m")
        else:
            out.append("  right behind a car: never")
        types = sorted(t for t in self.rel_t if t >= 0 and self.rel_t[t] > 0)
        if types:
            parts = []
            for t in types:
                w = self.rel_w[t]
                parts.append(f"type {t} {self.pct(self.rel_t[t]):.1f}% room {self.rel_room[t] / w:.2f} "
                             f"(min {self.rel_min[t]:.2f})" if w else f"type {t} {self.pct(self.rel_t[t]):.1f}%")
            out.append("  relation slots: " + " | ".join(parts))
            room, share = self.agg_room()
            if share:
                out.append(f"  aggression cases (types 5-8): {share:.1f}% of the time, mean room {room:.2f}")
        if self.max_t:
            out.append(f"  max room (car+0x1BAD) while above 0: mean {self.max_sum / self.max_t:.2f}")
        cc = self.car_contacts()
        walls = len(self.contacts) - len(cc)
        who = defaultdict(int)
        for c in cc:
            who[c[1]] += 1
        dmg = (f"; damage {self.dmg0:.3f} -> {self.dmg1:.3f}" if self.dmg0 is not None and self.dmg1 is not None
               else "")
        out.append(f"  contacts with cars: {len(cc)} ({60 * len(cc) / g:.2f} a minute)"
                   + (" - " + ", ".join(f"{k} x{v}" for k, v in sorted(who.items())) if who else "")
                   + f"; damage with no car near: {walls}{dmg}")
        return out

    def row(self) -> str:
        g = self.green or math.nan
        side = f"{self.lat_sum / self.along:5.2f}" if self.along else "    -"
        gap = f"{self.gap_sum / self.behind:5.1f}" if self.behind else "    -"
        room, share = self.agg_room()
        room = f"{room:5.2f}" if room == room else "    -"
        tight = f"{100 * self.tight / self.along:4.0f}%" if self.along else "    -"
        cc = len(self.car_contacts())
        ddmg = (f"{self.dmg1 - self.dmg0:+.3f}" if self.dmg0 is not None and self.dmg1 is not None else "-")
        return (f"{self.name[:22]:<22} {self.value:5.2f} {self.green:6.0f} {self.pct(self.along):6.1f}% {side} "
                f"{tight} {self.pct(self.behind):6.1f}% {gap} {share:6.1f}% {room} {cc:4d} {60 * cc / g:6.2f} "
                f"{ddmg:>7}")

    HEADER = ("step                   value  green  alongside  gap  tight  behind  gap  agg-case room  "
              "hits  /min    dmg")


class Probe:
    def __init__(self):
        for name in ("NR2003.exe", "NR2003_clean.exe"):
            try:
                self.pm = pymem.Pymem(name)
                break
            except Exception:
                continue
        else:
            raise SystemExit("NR2003 is not running (or this terminal isn't running as administrator)")
        self.game = Game()
        self.game.pm = self.pm
        d = os.path.join(HERE, "app", "logs")
        os.makedirs(d, exist_ok=True)
        self.path = os.path.join(d, f"aggression_probe_{datetime.datetime.now():%Y%m%d_%H%M%S}.txt")
        self.out = open(self.path, "a", encoding="utf-8")
        self.log(f"aggression probe on {name}; log {self.path}")

    def log(self, msg: str):
        line = f"{stamp()} {msg}"
        print(line)
        self.out.write(line + "\n")
        self.out.flush()

    # ---------------------------------------------------------------- memory
    def base(self, slot: int) -> int:
        return mm.CAR_BLOCK_0 + slot * mm.CAR_STRIDE

    def f32(self, addr: int) -> float:
        return struct.unpack("<f", self.pm.read_bytes(addr, 4))[0]

    def triplet(self, slot: int, k: int) -> tuple[float, float, float]:
        return struct.unpack("<fff", self.pm.read_bytes(self.base(slot) + RATING_BASE + 12 * k, 12))

    def cars(self) -> list[tuple[int, CarNo, str]]:
        """(slot, number, name) of the AI cars in the session."""
        out = []
        for s in range(mm.NUM_CARS):
            if s in (mm.PLAYER_SLOT, mm.PACE_SLOT):
                continue
            try:
                number, name = self.game._driver(s)
            except Exception:
                continue
            if number > 0 and name and name.lower() != "pace car":
                out.append((s, CarNo(number), name))
        return out

    def car_file(self, slot: int, mod: str) -> str | None:
        base = self.game.nr2003_dir()
        if not base or not mod:
            return None
        try:
            fn = self.pm.read_bytes(self.base(slot) + carfiles.OFF_CAR_FILE, carfiles.CAR_FILE_LEN)
        except Exception:
            return None
        fn = fn.split(b"\0", 1)[0].decode("latin-1").strip()
        if not fn.lower().endswith(".car"):
            return None
        path = os.path.join(carfiles.cars_folder(base, mod), fn)
        return path if os.path.isfile(path) else None

    # ------------------------------------------------------------------ read
    def read_table(self) -> dict[int, float]:
        track, mod = self.game.session_names()
        self.log(f"session: track '{track}', mod '{mod}', session byte {self.pm.read_bytes(mm.SESSION_STATE, 1)[0]}")
        cars = self.cars()
        if not cars:
            raise SystemExit("no AI cars found - load a session first")
        self.log("slot  car   driver                    | aggression mem: mean   dev    work | file: mean   dev   "
                 "| match | chassis mem/file | engine mem/file")
        work, matches, checked = {}, 0, 0
        for s, num, name in cars:
            mean, dev, w = self.triplet(s, AGG)
            work[s] = w
            fmean = fdev = None
            ch = en = ""
            path = self.car_file(s, mod)
            if path:
                ini = carfiles.parse(path)
                m, dv = ini.get("AIParamMean") or {}, ini.get("AIParamDeviation") or {}
                num_or = lambda d, k: float(d[k]) if k in d and _isfloat(d[k]) else None
                fmean, fdev = num_or(m, "aiParamDriverAggression"), num_or(dv, "aiParamDriverAggression")
                ch = f"{self.triplet(s, 10)[2]:.3f}/{_fmt(num_or(m, 'aiParamVehicleChassis'))}"
                en = f"{self.triplet(s, 9)[2]:.3f}/{_fmt(num_or(m, 'aiParamVehicleEngine'))}"
            ok = ""
            if fmean is not None:
                checked += 1
                if abs(mean - fmean) < 1e-3:
                    matches += 1
                    ok = "yes"
                else:
                    ok = "NO"
            self.log(f"{s:>4}  #{label(num):<4} {name[:24]:<24}  |              {mean:6.3f} {dev:6.3f} {w:6.3f} "
                     f"|       {_fmt(fmean):>6} {_fmt(fdev):>6} | {ok:>5} | {ch:>16} | {en}")
        self.log(f"RESULT: aggression mean in memory matches the .car file for {matches} of {checked} cars with a "
                 f"readable car file ({len(cars)} AI cars)")
        vals = sorted(work.values())
        if vals:
            self.log(f"working values: min {vals[0]:.3f}  median {vals[len(vals) // 2]:.3f}  max {vals[-1]:.3f}  "
                     f"(threshold under a car width above ~{SAFE_MAX:.2f})")
        return work

    def watch(self, last: dict[int, float]):
        self.log("watching every AI car's working aggression (car+0x16F6) for changes - Ctrl+C to stop")
        names = {s: (n, nm) for s, n, nm in self.cars()}
        session = self.game.session_names()
        changes = 0
        try:
            while True:
                time.sleep(WATCH_S)
                if self.game.session_names() != session:
                    self.log("session changed - stopping the watch (run again for the new session)")
                    break
                for s in list(last):
                    try:
                        w = self.triplet(s, AGG)[2]
                    except Exception:
                        continue
                    if abs(w - last[s]) > 1e-6:
                        changes += 1
                        n, nm = names.get(s, (0, "?"))
                        self.log(f"CHANGE slot {s} #{label(n)} {nm}: {last[s]:.4f} -> {w:.4f}  (session byte "
                                 f"{self.pm.read_bytes(mm.SESSION_STATE, 1)[0]}, flag "
                                 f"{self.pm.read_int(mm.FLAG_STATE)}, leader lap {self.f32_double_laps():.0f})")
                        last[s] = w
        except KeyboardInterrupt:
            pass
        self.log(f"watch ended: {changes} change(s) made by the game")

    def f32_double_laps(self) -> float:
        try:
            return self.pm.read_double(mm.LAPS_CURRENT)
        except Exception:
            return 0.0

    # ----------------------------------------------------------------- write
    def relations(self, slot: int) -> list[tuple[int, int, float]]:
        """the car's active relation slots: (other car's slot or -1, situation type, room)"""
        try:
            blk = self.pm.read_bytes(self.base(slot) + REL_BASE, REL_SLOTS * REL_STRIDE)
        except Exception:
            return []
        out = []
        for j in range(REL_SLOTS):
            o = j * REL_STRIDE
            active, typ, other = (struct.unpack_from("<I", blk, o + f)[0] for f in (REL_ACTIVE, REL_TYPE, REL_OTHER))
            if not active or not other:
                continue
            room = struct.unpack_from("<f", blk, o + REL_ROOM)[0]
            o_slot = (other - mm.CAR_BLOCK_0) // mm.CAR_STRIDE if mm.CAR_BLOCK_0 <= other else -1
            out.append((o_slot if 0 <= o_slot < mm.NUM_CARS else -1, typ, room if math.isfinite(room) else 0.0))
        return out

    def car_states(self) -> dict[int, tuple]:
        """slot -> (x, y, vx, vy, damage, out) for every car block that reads, the pace car left out"""
        out = {}
        for slot in range(mm.NUM_CARS):
            if slot == mm.PACE_SLOT:
                continue
            try:
                buf = self.pm.read_bytes(self.base(slot) + SPAN_LO, SPAN_HI - SPAN_LO)
            except Exception:
                continue
            f = lambda off: struct.unpack_from("<f", buf, off - SPAN_LO)[0]     # noqa: E731
            v = (f(mm.OFF_POS_X), f(mm.OFF_POS_X + 4), f(mm.OFF_VEL_X), f(mm.OFF_VEL_X + 4),
                 sum(f(o) for o in DAMAGE_OFFS))
            if all(math.isfinite(x) for x in v):
                out[slot] = v + (buf[mm.OFF_REASON_OUT - SPAN_LO],)
        return out

    def standing(self, slot: int, slots) -> tuple[int | None, int | None]:
        """(laps done, running position by laps + lap distance) of a car"""
        def prog(s):
            b = self.base(s)
            return self.pm.read_int(b + mm.OFF_LAPS_DONE), self.f32(b + mm.OFF_LAP_DIST)
        try:
            me = prog(slot)
        except Exception:
            return None, None
        ahead = 0
        for s in slots:
            if s == slot:
                continue
            try:
                if self.pm.read_bytes(self.base(s) + mm.OFF_REASON_OUT, 1)[0]:
                    continue
                if prog(s) > me:
                    ahead += 1
            except Exception:
                continue
        return me[0], ahead + 1

    def game_s(self) -> float:
        """game seconds (follows time acceleration, stops when paused); real time if the clock can't be read"""
        try:
            t = self.game._game_seconds()
        except Exception:
            t = 0.0
        return t if t > 0 else time.monotonic()

    def test(self, number: str, steps: list[tuple[str, float]], auto: float | None):
        want = raw(number)
        cars = self.cars()
        car = next(((s, n, nm) for s, n, nm in cars if int(n) == want), None)
        if car is None:
            raise SystemExit(f"car #{number} isn't an AI car in this session")
        s, num, name = car
        names = {sl: f"#{label(n)}" for sl, n, _ in cars}
        try:
            pn = self.game._driver(mm.PLAYER_SLOT)[0]
            if pn > 0:
                names[mm.PLAYER_SLOT] = f"#{label(CarNo(pn))} (you)"
        except Exception:
            pass
        slots = sorted(names)
        addr = self.base(s) + OFF_AGG_WORK
        game_val = self.f32(addr)
        session = self.game.session_names()
        for what, v in steps:
            if v > SAFE_MAX:
                self.log(f"NOTE: {what} {v:.2f} is above ~{SAFE_MAX:.2f} - the tightest side-by-side threshold falls "
                         "under a car width: expect it to lean on cars")
        steps = [("GAME VALUE", game_val)] + steps + [("GAME VALUE (put back)", game_val)]
        self.log(f"write test: #{label(num)} {name} (slot {s}), aggression at 0x{addr:X} = {game_val:.3f}; steps "
                 + " -> ".join(f"{w} {v:.2f}" for w, v in steps))
        self.log("Enter = next step" + (f" (or by itself after {auto:.0f} s of green-flag game time)" if auto else "")
                 + ", q = put it back and quit. Only green-flag laps on track are measured.")
        written = None
        done: list[StepStats] = []
        st = None
        try:
            import msvcrt
            for k, (what, val) in enumerate(steps):
                if k:
                    self.pm.write_bytes(addr, struct.pack("<f", val), 4)
                    written = val
                back = self.f32(addr)
                did = "left as the game set it" if not k else f"wrote {val:.3f}"
                self.log(f"STEP {k + 1}/{len(steps)} {what}: {did}, reads back {back:.3f}")
                if k == len(steps) - 1:
                    break
                st = StepStats(what, val)
                if not self._run_step(st, s, addr, written, session, names, slots, auto, msvcrt):
                    written = None                   # the session changed or the game rewrote the value
                    return
                self._close_step(st, s, slots, done)
                st = None
            written = None                           # the last step put the game value back
        except KeyboardInterrupt:
            pass
        finally:
            if st is not None and st.green:          # quit part-way: keep what that step measured
                self._close_step(st, s, slots, done)
            if written is not None:
                try:
                    if abs(self.f32(addr) - written) <= 1e-6:     # still ours: put the game's value back
                        self.pm.write_bytes(addr, struct.pack("<f", game_val), 4)
                        self.log(f"put back: #{label(num)} aggression {game_val:.3f}")
                except Exception as e:
                    self.log(f"could not put the value back ({e}) - reload the session to be sure")
            if done:
                self.log(f"COMPARISON (#{label(num)} {name}; side gap = centre to centre, ~{CAR_WIDTH:.1f} m = "
                         "touching; hits = contacts with cars):")
                self.log("  " + StepStats.HEADER)
                for d in done:
                    self.log("  " + d.row())
            self.log("write test done")

    def _close_step(self, st: StepStats, s: int, slots, done: list):
        try:
            st.laps1, st.pos1 = self.standing(s, slots)
            st.dmg1 = self.car_states().get(s, (0,) * 6)[4]
        except Exception:
            pass
        for line in st.lines():
            self.log(line)
        done.append(st)

    def _run_step(self, st: StepStats, s, addr, written, session, names, slots, auto, msvcrt) -> bool:
        """measure one step until Enter (or --auto). False = stop the test (the game value stands)."""
        st.laps0, st.pos0 = self.standing(s, slots)
        states = self.car_states()
        st.dmg0 = states.get(s, (0,) * 6)[4]
        prev_dmg = {sl: v[4] for sl, v in states.items()}
        last_contact: dict[str, list] = {}
        t_prev = self.game_s()
        t_rel = t_check = t_progress = time.monotonic()
        last_rel = ""
        while True:
            now = time.monotonic()
            if now >= t_check:
                t_check = now + 1.0
                if self.game.session_names() != session:
                    self.log("session changed - stopping (the new session loaded its own values)")
                    return False
                cur = self.f32(addr)
                if written is not None and abs(cur - written) > 1e-6:
                    # the game re-rolls every driver's value at a session start (live 2026-11-03): its new value
                    # stands, putting the old one back would undo the game's roll
                    self.log(f"the game rewrote it: {written:.3f} -> {cur:.3f} (session byte "
                             f"{self.pm.read_bytes(mm.SESSION_STATE, 1)[0]}, flag {self.pm.read_int(mm.FLAG_STATE)})"
                             " - test stopped, the game's value stays; run it again once the session is under way")
                    return False
            t = self.game_s()
            dt = t - t_prev if t_prev else 0.0
            t_prev = t
            if not 0.0 <= dt <= 1.0:             # clock jump (restart, replay): count nothing for this sample
                dt = 0.0
            states = self.car_states()
            try:
                flag = self.pm.read_int(mm.FLAG_STATE)
                pit = self.pm.read_bytes(mm.ON_PIT_ROAD, mm.NUM_CARS)
            except Exception:
                flag, pit = 0, bytes(mm.NUM_CARS)
            me = states.get(s)
            if me is not None:
                rel_line = self._sample(st, s, me, states, pit, flag, dt, t, prev_dmg, last_contact, names)
                if rel_line and rel_line != last_rel and now >= t_rel:
                    t_rel = now + 1.0
                    self.log(f"  [{st.name}] {rel_line}")
                    last_rel = rel_line
            prev_dmg = {sl: v[4] for sl, v in states.items()}
            if now >= t_progress + PROGRESS_S:
                t_progress = now
                self.log(f"  [{st.name}] {st.green:.0f} s green measured: alongside {st.pct(st.along):.1f}%, "
                         f"behind a car {st.pct(st.behind):.1f}%, contacts {len(st.car_contacts())}")
            if auto and st.green >= auto:
                self.log(f"  [{st.name}] {auto:.0f} s of green measured - next step")
                return True
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\r", "\n"):
                    return True
                if ch.lower() == "q":
                    raise KeyboardInterrupt
            time.sleep(SAMPLE_S)

    def _sample(self, st: StepStats, s, me, states, pit, flag, dt, t, prev_dmg, last_contact, names) -> str:
        """add one sample to the step; returns a short line about the cars around (empty when none)"""
        x, y, vx, vy, dmg, out = me
        sp = math.hypot(vx, vy)
        racing = flag == mm.FLAG_GREEN and not out and not pit[s] and sp >= MIN_SPEED
        if not racing:
            return ""
        hx, hy = vx / sp, vy / sp
        nearest, side, ahead = None, None, None
        near: list[tuple[float, int]] = []
        for sl, o in states.items():
            if sl == s or o[5] or pit[sl] or math.hypot(o[2], o[3]) < 5.0:
                continue
            dx, dy = o[0] - x, o[1] - y
            d = math.hypot(dx, dy)
            if d > 60.0:
                continue
            along, lat = dx * hx + dy * hy, hx * dy - hy * dx
            if nearest is None or d < nearest[0]:
                nearest = (d, sl, along, lat)
            if d < NEAR_CONTACT_M:
                near.append((d, sl))
            if abs(along) < ALONG_M and SIDE_MIN_M < abs(lat) < SIDE_MAX_M and (side is None or abs(lat) < side[0]):
                side = (abs(lat), sl)
            if 0 < along < AHEAD_M and abs(lat) < IN_LINE_M and (ahead is None or along < ahead[0]):
                ahead = (along, sl)
        st.green += dt
        if side:
            st.along += dt
            st.lat_sum += side[0] * dt
            st.lat_min = min(st.lat_min, side[0])
            if side[0] < TIGHT_M:
                st.tight += dt
        if ahead:
            st.behind += dt
            st.gap_sum += ahead[0] * dt
            st.gap_min = min(st.gap_min, ahead[0])
        rels = self.relations(s)
        seen, in_agg = set(), False
        for _, typ, room in rels:
            seen.add(typ)
            st.rel_w[typ] += dt
            st.rel_room[typ] += room * dt
            st.rel_min[typ] = min(st.rel_min.get(typ, math.inf), room)
            in_agg |= typ in AGG_TYPES
        for typ in seen:
            st.rel_t[typ] += dt
        if in_agg:
            st.rel_t[-1] += dt
        try:
            mx = self.f32(self.base(s) + OFF_ROOM_MAX)
        except Exception:
            mx = 0.0
        if math.isfinite(mx) and mx > 0:
            st.max_t += dt
            st.max_sum += mx * dt

        # contact: this car's damage going up, or a car within NEAR_CONTACT_M taking damage
        own = dmg - prev_dmg.get(s, dmg)
        hit = sorted((d, sl, states[sl][4] - prev_dmg.get(sl, states[sl][4])) for d, sl in near)
        hit = [h for h in hit if h[2] > DMG_EPS]
        if own > DMG_EPS or hit:
            if hit:
                d, sl, other = hit[0]
            elif nearest and nearest[0] < NEAR_CONTACT_M:
                d, sl, other = nearest[0], nearest[1], 0.0
            else:
                d, sl, other = (nearest[0] if nearest else math.inf), -1, 0.0
            who = names.get(sl, f"slot {sl}") if sl >= 0 else "no car near"
            ev = last_contact.get(who)
            if ev is not None and t - ev[0] <= CONTACT_MERGE_S:
                ev[0] = t
                ev[2] += max(own, 0.0)
                ev[3] += other
            else:
                ev = [t, who, max(own, 0.0), other, d]
                last_contact[who] = ev
                st.contacts.append(ev)
                where = ""
                if nearest and sl >= 0:
                    o = states[sl]
                    dx, dy = o[0] - x, o[1] - y
                    where = f" (along {dx * hx + dy * hy:+.1f} m, side {hx * dy - hy * dx:+.1f} m)"
                self.log(f"  [{st.name}] CONTACT? with {who}: own damage +{max(own, 0.0):.4f} (now {dmg:.3f}), "
                         + (f"{who} +{other:.4f}, " if sl >= 0 else "")
                         + (f"{d:.1f} m apart{where}" if math.isfinite(d) else "no car within 60 m"))

        parts = []
        if side:
            parts.append(f"alongside {names.get(side[1], side[1])} {side[0]:.2f} m")
        if ahead:
            parts.append(f"behind {names.get(ahead[1], ahead[1])} {ahead[0]:.1f} m")
        rel_parts = [f"{names.get(o, '?') if o >= 0 else '?'} type {typ} room {room:.2f}"
                     for o, typ, room in rels if typ]
        if rel_parts:
            parts.append("relations: " + "; ".join(rel_parts) + (f"; max room {mx:.2f}" if mx else ""))
        return " | ".join(parts)


def _isfloat(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def _fmt(v) -> str:
    return "-" if v is None else f"{v:.3f}"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--test", metavar="CAR", help="AI car number to run the write test on (e.g. 24 or 01)")
    ap.add_argument("--low", type=float, help="LOW aggression value (default: half the field's lowest)")
    ap.add_argument("--high", type=float, help=f"HIGH aggression value (default: 1.5 x the field's highest, at most "
                                               f"{SAFE_MAX:.2f})")
    ap.add_argument("--steps", help="comma list of values to step through instead of LOW / HIGH, e.g. 0.5,2.4,3.0")
    ap.add_argument("--auto", type=float, metavar="SECONDS",
                    help="go to the next step by itself after this many seconds of green-flag game time")
    ap.add_argument("--no-watch", action="store_true", help="read mode: print the table and quit")
    a = ap.parse_args()
    if a.steps:
        try:
            wanted = [float(v) for v in a.steps.split(",") if v.strip()]
        except ValueError:
            raise SystemExit(f"--steps: '{a.steps}' isn't a comma list of numbers")
    else:
        wanted = [v for v in (a.low, a.high) if v is not None]
    for v in wanted:
        if not 0.0 <= v <= HARD_MAX:
            raise SystemExit(f"{v} is outside 0-{HARD_MAX:.1f} (at {HARD_MAX:.1f} all the side thresholds are already "
                             "down to half a car)")
    if a.auto is not None and a.auto <= 0:
        raise SystemExit("--auto needs a number of seconds above 0")
    p = Probe()
    work = p.read_table()
    if a.test:
        if a.steps:
            steps = [(f"STEP {v:.2f}", v) for v in wanted]
        else:
            vals = sorted(work.values())
            low = a.low if a.low is not None else max(0.0, vals[0] * 0.5)
            high = a.high if a.high is not None else min(SAFE_MAX, max(vals[-1] * 1.5, vals[-1] + 0.3))
            steps = [("LOW", low), ("HIGH", high)]
        p.test(a.test, steps, a.auto)
    elif not a.no_watch:
        p.watch(work)


if __name__ == "__main__":
    main()
