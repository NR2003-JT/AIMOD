"""AI aggression probe: checks where NR2003 keeps each AI driver's aggression, then (optionally) changes it for one
car or a pair of cars so you can watch - and measure - what it does.

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

FIELD (never writes):  python tools\\aggression_probe.py --field [--auto 300]
  Measures EVERY AI car 20 times a second with its own (game-rolled) value, then a table sorted by aggression and
  how strongly aggression goes with room, side gap, time alongside, lifting and contacts across the field.

WRITE TEST (one AI car or a pair, always put back):
    python tools\\aggression_probe.py --test 24 [--low 0.2] [--high 2.0]
    python tools\\aggression_probe.py --test 3 --high 3 --auto 120
    python tools\\aggression_probe.py --pair 24 48 --steps 2.4,3.0 --auto 150
  (without --low / --high: half the field's lowest value and 1.5 x its highest, capped below a car width)
  Steps through: game value -> LOW -> HIGH -> game value (or game value -> each --steps value -> game value), one
  step each time you press Enter, or by itself after --auto seconds of green-flag game time (q = put it back and
  quit). With --pair both cars get the same step value (each its own game value in the GAME VALUE steps).
  Every step is measured 20 times a second, weighted by game time so time acceleration doesn't skew it:
    - side by side: how much of the time a car is alongside, and the side gap (centre to centre; ~1.9 m = touching)
    - following: how much of the time a car is right ahead in line, and how close it follows
    - the car's 8 relation slots (situation type 0-10, the room value it worked out): time per type, mean room;
      the side-by-side cases 5-8 (the ones that read aggression): share of time, mean and min room
    - lifting: throttle (and brake) in traffic minus the car's own clean-air throttle at the same spot of the lap
      (0-255; negative = it lifts for the cars around it)
    - contacts: this car's damage going up, or a car within 6 m taking damage, with who was next to it
    - cautions that came out during the step
    - pair: time within a second of each other, side by side with each other, the room each leaves the other,
      contacts between the two
  A summary is logged at the end of each step and a comparison table at the end. Only green-flag time on track
  counts (not pit road, not under yellow). Watch the car on track and note what you see at each step.
  The game value is written back when you quit, on Ctrl+C, and if the session changes under it; if the game
  re-rolls the value (a new session start) the test stops and the game's new value stands.

Run in an ADMIN terminal (NR2003 runs as administrator), with a session loaded:
    Start-Process powershell -Verb RunAs -ArgumentList '-NoExit','-Command','cd "<AIMOD folder>"; python tools\\aggression_probe.py'
Log: <AIMOD>\\app\\logs\\aggression_probe_<date>_<time>.txt (or --log-dir)
Dry run without the game:  python tools\\aggression_probe.py --fake --pair 24 48 --steps 0.5,2.4,3.0 --auto 60
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

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from aimod import carfiles                       # noqa: E402
from aimod import memory_map as mm               # noqa: E402
from aimod import showdown_lock                  # noqa: E402
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
# pedals: the car record (RaceControl's map: 0xC12030 + slot x 5560): +0x604 throttle 0-255, +0x605 rises braking
PEDAL_REC_0, PEDAL_REC_STRIDE, OFF_THROTTLE = 0xC12030, 5560, 0x604
BRAKE_ON = 10
CLEAN_M = 40.0                                   # no car this close = clean air (the throttle reference)
BIN_M = 25.0                                     # lap distance bins for the clean-air reference
CLEAN_MIN_S = 0.5                                # game seconds of clean air in a bin before it is a reference
NEAR_S = 1.0                                     # pair: "within a second of each other"
SHOWDOWN_STEPS = (2.4, 2.7, 3.0, 3.3, 3.6, 4.0)  # --showdown ramp: the proven range (2.4-3.0) then past it
SHOWDOWN_AUTO_S = 120.0                          # green game seconds per showdown step
HIT_DAMAGE = 0.003                               # damage that makes a pair contact "real" for --stop-on-contact
# the game's parameter table (the track.ini / papy_ai.ini values, live): 144 entries of 24 bytes from 0x74AA70, the live
# float at +16, the section name pointer at +4 and the key name pointer at +8 (live 2026-11-05). The code reads these
# floats directly every time it uses them (e.g. ai_drafting_distance x the 5.025 m car length at 0x530A5F, 0x4087E0,
# 0x526F38, 0x533AAB), so a write takes effect at once. All field-wide: there is no per-car copy.
TABLE_BASE, TABLE_N, TABLE_STRIDE = 0x74AA70, 144, 24
TABLE_PARAMS = {                                  # name: (lowest, highest) value we will write
    "ai_drafting_distance": (0.05, 5.0),          # car lengths the AI holds behind the car in front while racing
    "ai_panic_decel": (0.0, 50.0),                # G's of extra deceleration that makes the AI panic-brake
    "ai_dlongpad_scale": (0.0, 20.0),             # scales the padding in braking zones (smaller = more scaling)
    "ai_dlat_pad": (0.0, 3.0),                    # side padding
    "ai_squeeze_pcnt": (0.0, 1.0),                # 0 at most tracks, 1.0 at Bowman Gray (meaning not confirmed)
}
# per-car side padding: the car-next-to-car code (0x530110) adds ai_dlat_pad x car+0x1BEE to the side room it keeps.
# car+0x1BEE is rebuilt on every AI update as car+0x1BEA + 0.5 x (busy relations squared / 64) (0x52BCFC; live
# 2026-11-05: eight busy relations = 1.5, fewer = 1.0), and car+0x1BEA is a per-car base of 1.0 that mode changes reset
# (0.7 / 1.0 / 1.5). So scaling car+0x1BEA per car scales that car's side padding without touching anyone else.
OFF_SIDE_BASE = 0x1BEA
SIDE_PAD_MIN = 0.0
PACE_SHOWDOWN = 0.975                            # --showdown default: the leader's traction x0.975 (live 10-25: x0.97 = 0.2-0.5 s/lap)
PACE_MIN = 0.90                                  # never slower than this; above 1.0 (faster) is untested live
PACE_SWITCH_M = 3.0                              # the trailing car must be this far ahead before the roles swap
PACE_NEAR_M = 150.0                              # farther apart than this the order isn't read (a lapped car)
LOCK_WAIT_S = 3.0                                # after taking the cars: time for a running AIMOD to give its writes back
HIT_TAIL_S = 4.0                                 # game seconds kept running after the first contact (the aftermath)
MAX_ACCEL = 64.0                                 # game seconds per real second we still believe (time acceleration)


def game_dt(t: float, t_prev: float, real_dt: float) -> float:
    """game seconds since the last sample, 0 for a clock jump (restart, replay, a pause ending). With heavy time
    acceleration a 20 Hz sample can span more than a game second, so the limit follows the real time passed."""
    dt = t - t_prev if t_prev else 0.0
    return dt if 0.0 <= dt <= max(1.0, real_dt * MAX_ACCEL) else 0.0


def stamp() -> str:
    return f"{datetime.datetime.now():%H:%M:%S.%f}"[:-3]


def corr(xs: list[float], ys: list[float]) -> float:
    """Pearson correlation (nan with fewer than 4 pairs or no spread)."""
    pts = [(x, y) for x, y in zip(xs, ys) if x == x and y == y]
    if len(pts) < 4:
        return math.nan
    mx = sum(p[0] for p in pts) / len(pts)
    my = sum(p[1] for p in pts) / len(pts)
    sxy = sum((x - mx) * (y - my) for x, y in pts)
    sxx = sum((x - mx) ** 2 for x, _ in pts)
    syy = sum((y - my) ** 2 for _, y in pts)
    return sxy / math.sqrt(sxx * syy) if sxx > 0 and syy > 0 else math.nan


class StepStats:
    """What one test step saw for one car. Every figure is weighted by game time, so time acceleration doesn't
    skew it."""

    def __init__(self, name: str, value: float):
        self.name, self.value = name, value
        self.green = 0.0                         # game seconds measured (green, on track, at speed)
        self.samples = 0
        self.along = self.lat_sum = self.tight = 0.0
        self.lat_min = math.inf
        self.behind = self.gap_sum = 0.0
        self.gap_min = math.inf
        self.rel_t = defaultdict(float)          # type -> game s with at least one relation of that type
        self.rel_w = defaultdict(float)          # type -> relation-seconds (for the mean room)
        self.rel_room = defaultdict(float)
        self.rel_min: dict[int, float] = {}
        self.agg_min = math.inf                  # min room over the side-by-side cases 5-8
        self.max_t = self.max_sum = 0.0          # car+0x1BAD while it is above 0
        self.contacts: list[list] = []           # [game t of last damage, partner, own dmg, partner dmg, dist m]
        self.last_contact: dict[str, list] = {}
        self.traffic: list[tuple] = []           # (lap bin, throttle, brake, dt) while in traffic
        self.thr_lo, self.thr_hi = 256, -1       # throttle byte range seen (a dead address never moves)
        self.lift = self.brake_d = None          # throttle / braking share vs clean air at the same spots
        self.lift_s = 0.0
        self.cautions = 0
        self.last_rels: list[tuple[int, int, float]] = []
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

    def side_gap(self) -> float:
        return self.lat_sum / self.along if self.along else math.nan

    def per_min(self) -> float:
        return 60.0 * len(self.car_contacts()) / self.green if self.green else math.nan

    def lines(self) -> list[str]:
        g = self.green
        out = [f"SUMMARY [{self.name} {self.value:.3f}]: {g:.0f} s of green-flag game time"
               + (f", {self.laps1 - self.laps0} laps" if self.laps0 is not None and self.laps1 is not None else "")
               + (f", running P{self.pos0} -> P{self.pos1}" if self.pos0 and self.pos1 else "")
               + (f", {self.samples} samples ({self.samples / g:.1f} per game second)" if g else "")]
        if not g:
            return out + ["  nothing measured (no green-flag time on track during this step)"]
        if self.along:
            out.append(f"  side by side: {self.pct(self.along):.1f}% of the time ({self.along:.0f} s); side gap "
                       f"mean {self.side_gap():.2f} m, min {self.lat_min:.2f} m (~{CAR_WIDTH:.1f} = "
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
                out.append(f"  side-by-side cases (types 5-8, read aggression): {share:.1f}% of the time, room mean "
                           f"{room:.2f}, min {self.agg_min:.2f}")
            else:
                out.append("  side-by-side cases (types 5-8): never")
        if self.max_t:
            out.append(f"  max room (car+0x1BAD) while above 0: mean {self.max_sum / self.max_t:.2f}")
        if self.thr_hi < 0:
            out.append("  throttle: not readable")
        elif self.thr_lo == self.thr_hi:
            out.append(f"  throttle byte never moved ({self.thr_hi}) - the pedal address isn't valid for this car")
        elif self.lift is not None:
            out.append(f"  lifting in traffic: throttle {self.lift:+.0f} of 255 vs its own clean-air throttle at "
                       f"the same spots, braking {self.brake_d:+.0f} points ({self.lift_s:.0f} s compared)")
        else:
            out.append("  lifting in traffic: no clean-air reference at the spots it was in traffic yet")
        cc = self.car_contacts()
        walls = len(self.contacts) - len(cc)
        who = defaultdict(int)
        for c in cc:
            who[c[1]] += 1
        dmg = (f"; damage {self.dmg0:.3f} -> {self.dmg1:.3f}" if self.dmg0 is not None and self.dmg1 is not None
               else "")
        out.append(f"  contacts with cars: {len(cc)} ({self.per_min():.2f} a minute)"
                   + (" - " + ", ".join(f"{k} x{v}" for k, v in sorted(who.items())) if who else "")
                   + f"; damage with no car near: {walls}{dmg}")
        if self.cautions:
            out.append(f"  cautions during the step: {self.cautions}")
        return out

    def row(self) -> str:
        g = self.green or math.nan
        side = f"{self.side_gap():5.2f}" if self.along else "    -"
        gap = f"{self.gap_sum / self.behind:5.1f}" if self.behind else "    -"
        room, share = self.agg_room()
        room = f"{room:5.2f}" if room == room else "    -"
        rmin = f"{self.agg_min:5.2f}" if math.isfinite(self.agg_min) else "    -"
        tight = f"{100 * self.tight / self.along:4.0f}%" if self.along else "    -"
        lift = f"{self.lift:+5.0f}" if self.lift is not None else "    -"
        cc = len(self.car_contacts())
        ddmg = (f"{self.dmg1 - self.dmg0:+.3f}" if self.dmg0 is not None and self.dmg1 is not None else "-")
        return (f"{self.name[:22]:<22} {self.value:5.2f} {self.green:6.0f} {self.pct(self.along):6.1f}% {side} "
                f"{tight} {self.pct(self.behind):6.1f}% {gap} {share:6.1f}% {room} {rmin} {lift} {cc:4d} "
                f"{60 * cc / g:6.2f} {ddmg:>7} {self.cautions:3d}")

    HEADER = ("step                   value  green  alongside  gap  tight  behind  gap  agg-case room   min  "
              "lift hits  /min    dmg  yel")


class PairStats:
    """Two cars of a --pair test: how they race each other."""

    def __init__(self, name: str, a: int, b: int, va: float, vb: float):
        self.name, self.a, self.b, self.va, self.vb = name, a, b, va, vb
        self.green = self.near = self.side = self.side_sum = 0.0
        self.side_min = math.inf
        self.a_behind = self.b_behind = 0.0
        # relation slots of a about b and of b about a, side-by-side cases only: [seconds, room x s, min]
        self.rel = {a: [0.0, 0.0, math.inf], b: [0.0, 0.0, math.inf]}
        self.contacts = 0

    def pct(self, t: float) -> float:
        return 100.0 * t / self.green if self.green else 0.0

    def sample(self, states, pit, flag, dt, rels: dict[int, list]):
        sa, sb = states.get(self.a), states.get(self.b)
        if sa is None or sb is None or flag != mm.FLAG_GREEN:
            return
        if sa[5] or sb[5] or pit[self.a] or pit[self.b]:
            return
        sp = math.hypot(sa[2], sa[3])
        if sp < MIN_SPEED or math.hypot(sb[2], sb[3]) < MIN_SPEED:
            return
        self.green += dt
        hx, hy = sa[2] / sp, sa[3] / sp
        dx, dy = sb[0] - sa[0], sb[1] - sa[1]
        along, lat = dx * hx + dy * hy, hx * dy - hy * dx
        if math.hypot(dx, dy) / sp < NEAR_S:
            self.near += dt
        if abs(along) < ALONG_M and SIDE_MIN_M < abs(lat) < SIDE_MAX_M:
            self.side += dt
            self.side_sum += abs(lat) * dt
            self.side_min = min(self.side_min, abs(lat))
        if abs(lat) < IN_LINE_M:
            if 0 < along < AHEAD_M:
                self.a_behind += dt
            elif -AHEAD_M < along < 0:
                self.b_behind += dt
        for me, other in ((self.a, self.b), (self.b, self.a)):
            for o, typ, room in rels.get(me, ()):
                if o == other and typ in AGG_TYPES:
                    r = self.rel[me]
                    r[0] += dt
                    r[1] += room * dt
                    r[2] = min(r[2], room)

    def rel_text(self, me: int) -> str:
        t, rs, mn = self.rel[me]
        return f"{self.pct(t):.1f}% of the time, room {rs / t:.2f} (min {mn:.2f})" if t else "never"

    def lines(self, names) -> list[str]:
        na, nb = names.get(self.a, self.a), names.get(self.b, self.b)
        out = [f"PAIR [{self.name}] {na} {self.va:.3f} / {nb} {self.vb:.3f}: {self.green:.0f} s both racing"]
        if not self.green:
            return out + ["  never both racing under green during this step"]
        gap = f", side gap mean {self.side_sum / self.side:.2f} m, min {self.side_min:.2f} m" if self.side else ""
        out.append(f"  within {NEAR_S:.0f} s of each other {self.pct(self.near):.1f}%; side by side with each other "
                   f"{self.pct(self.side):.1f}%{gap}; {na} right behind {nb} {self.pct(self.a_behind):.1f}%, "
                   f"{nb} right behind {na} {self.pct(self.b_behind):.1f}%")
        out.append(f"  {na}'s side-by-side relation to {nb}: {self.rel_text(self.a)}; {nb}'s to {na}: "
                   f"{self.rel_text(self.b)}")
        out.append(f"  contacts between them: {self.contacts} ({60 * self.contacts / self.green:.2f} a minute)")
        return out

    def row(self) -> str:
        side = f"{self.side_sum / self.side:5.2f}" if self.side else "    -"
        room = lambda r: f"{r[1] / r[0]:5.2f}" if r[0] else "    -"      # noqa: E731
        return (f"{self.name[:22]:<22} {self.va:5.2f} {self.vb:5.2f} {self.green:6.0f} {self.pct(self.near):8.1f}% "
                f"{self.pct(self.side):6.1f}% {side} {room(self.rel[self.a])} {room(self.rel[self.b])} "
                f"{self.contacts:4d}")

    HEADER = "step                   val A val B  green  within 1s  side-by  gap  roomA roomB hits"


class Probe:
    def __init__(self, pm=None, log_dir: str | None = None):
        name = "fake NR2003"
        if pm is None:
            import pymem
            for name in ("NR2003.exe", "NR2003_clean.exe"):
                try:
                    pm = pymem.Pymem(name)
                    break
                except Exception:
                    continue
            else:
                raise SystemExit("NR2003 is not running (or this terminal isn't running as administrator)")
        self.pm = pm
        self.game = Game()
        self.game.pm = self.pm
        d = log_dir or os.path.join(HERE, "app", "logs")
        os.makedirs(d, exist_ok=True)
        self.path = os.path.join(d, f"aggression_probe_{datetime.datetime.now():%Y%m%d_%H%M%S}.txt")
        self.out = open(self.path, "a", encoding="utf-8")
        self.clean: dict[int, dict[int, list]] = {}     # slot -> lap bin -> [throttle x s, braking s, s]
        self.stop_on_contact = False                    # showdown: the first contact between the pair ends the ramp
        self.lock_wait = LOCK_WAIT_S
        self.params: dict[str, float] = {}               # --param NAME=VALUE: parameter-table overrides (field-wide)
        self.param_nat: dict[str, tuple[int, float]] = {}   # name -> (address, the game's value)
        self.param_w: dict[str, float] = {}
        self.side_pad = 1.0                             # --side-pad: the pair's side-padding base scale (1 = off)
        self.side_nat: dict[int, float] = {}            # slot -> the game's own car+0x1BEA
        self.side_w: dict[int, float] = {}              # slot -> what we wrote
        self.pace = 1.0                                 # --pace: the leader of the pair gets this traction scale
        self.pace_lead: int | None = None               # slot of the pair's current leader (the slowed one)
        self.pace_nat: dict[int, float] = {}            # slot -> the game's own traction value
        self.pace_w: dict[int, float] = {}              # slot -> what we wrote
        self._lock_cars: list[int] = []
        self.hit: tuple | None = None                   # (step name, value, green s into the step, damage a, b)
        self.log(f"aggression probe on {name}; log {self.path}")

    def log(self, msg: str):
        line = f"{stamp()} {msg}"
        print(line)
        self.out.write(line + "\n")
        self.out.flush()

    def close(self):
        try:
            self.out.close()
        except Exception:
            pass

    # ---------------------------------------------------------------- memory
    def base(self, slot: int) -> int:
        return mm.CAR_BLOCK_0 + slot * mm.CAR_STRIDE

    def f32(self, addr: int) -> float:
        return struct.unpack("<f", self.pm.read_bytes(addr, 4))[0]

    def triplet(self, slot: int, k: int) -> tuple[float, float, float]:
        return struct.unpack("<fff", self.pm.read_bytes(self.base(slot) + RATING_BASE + 12 * k, 12))

    def pedals(self, slot: int) -> tuple[int, int] | None:
        try:
            b = self.pm.read_bytes(PEDAL_REC_0 + slot * PEDAL_REC_STRIDE + OFF_THROTTLE, 2)
        except Exception:
            return None
        return b[0], b[1]

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
                num_or = lambda d, k: float(d[k]) if k in d and _isfloat(d[k]) else None     # noqa: E731
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

    # --------------------------------------------------------------- sampling
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
        """slot -> (x, y, vx, vy, damage, out, lap distance) for every car block that reads, the pace car left out"""
        out = {}
        for slot in range(mm.NUM_CARS):
            if slot == mm.PACE_SLOT:
                continue
            try:
                buf = self.pm.read_bytes(self.base(slot) + SPAN_LO, SPAN_HI - SPAN_LO)
                dist = self.f32(self.base(slot) + mm.OFF_LAP_DIST)
            except Exception:
                continue
            f = lambda off: struct.unpack_from("<f", buf, off - SPAN_LO)[0]     # noqa: E731
            v = (f(mm.OFF_POS_X), f(mm.OFF_POS_X + 4), f(mm.OFF_VEL_X), f(mm.OFF_VEL_X + 4),
                 sum(f(o) for o in DAMAGE_OFFS))
            if all(math.isfinite(x) for x in v):
                out[slot] = v + (buf[mm.OFF_REASON_OUT - SPAN_LO], dist if math.isfinite(dist) else 0.0)
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

    def _flag_pit(self):
        try:
            return self.pm.read_int(mm.FLAG_STATE), self.pm.read_bytes(mm.ON_PIT_ROAD, mm.NUM_CARS)
        except Exception:
            return 0, bytes(mm.NUM_CARS)

    def _sample(self, st: StepStats, s, me, states, pit, flag, dt, t, prev_dmg, names, talk: bool = True) -> str:
        """add one sample to the step; returns a short line about the cars around (empty when none)"""
        x, y, vx, vy, dmg, out, dist = me
        sp = math.hypot(vx, vy)
        racing = flag == mm.FLAG_GREEN and not out and not pit[s] and sp >= MIN_SPEED
        st.last_rels = []
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
        st.samples += 1
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
        st.last_rels = rels
        seen, in_agg = set(), False
        for _, typ, room in rels:
            seen.add(typ)
            st.rel_w[typ] += dt
            st.rel_room[typ] += room * dt
            st.rel_min[typ] = min(st.rel_min.get(typ, math.inf), room)
            if typ in AGG_TYPES:
                in_agg = True
                st.agg_min = min(st.agg_min, room)
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

        # pedals: clean air builds the car's reference throttle per spot of the lap; traffic is compared to it
        ped = self.pedals(s)
        if ped is not None:
            thr, brk = ped
            st.thr_lo, st.thr_hi = min(st.thr_lo, thr), max(st.thr_hi, thr)
            b = int(dist // BIN_M)
            if nearest is None or nearest[0] > CLEAN_M:
                ref = self.clean.setdefault(s, {}).setdefault(b, [0.0, 0.0, 0.0])
                ref[0] += thr * dt
                ref[1] += (brk > BRAKE_ON) * dt
                ref[2] += dt
            elif side or ahead or in_agg:
                st.traffic.append((b, thr, brk, dt))

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
            ev = st.last_contact.get(who)
            if ev is not None and t - ev[0] <= CONTACT_MERGE_S:
                ev[0] = t
                ev[2] += max(own, 0.0)
                ev[3] += other
            else:
                ev = [t, who, max(own, 0.0), other, d]
                st.last_contact[who] = ev
                st.contacts.append(ev)
                if talk:
                    where = ""
                    if nearest and sl >= 0:
                        o = states[sl]
                        dx, dy = o[0] - x, o[1] - y
                        where = f" (along {dx * hx + dy * hy:+.1f} m, side {hx * dy - hy * dx:+.1f} m)"
                    self.log(f"  [{st.name}] CONTACT? {names.get(s, s)} with {who}: own damage "
                             f"+{max(own, 0.0):.4f} (now {dmg:.3f}), "
                             + (f"{who} +{other:.4f}, " if sl >= 0 else "")
                             + (f"{d:.1f} m apart{where}" if math.isfinite(d) else "no car within 60 m"))
        if not talk:
            return ""
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

    def _finish_lift(self, st: StepStats, s: int):
        ref_map = self.clean.get(s, {})
        w = dl = db = 0.0
        for b, thr, brk, dt in st.traffic:
            ref = ref_map.get(b)
            if not ref or ref[2] < CLEAN_MIN_S:
                continue
            w += dt
            dl += (thr - ref[0] / ref[2]) * dt
            db += ((brk > BRAKE_ON) - ref[1] / ref[2]) * dt
        if w > 0:
            st.lift, st.brake_d, st.lift_s = dl / w, 100.0 * db / w, w

    # ------------------------------------------------------------------ field
    def field(self, auto: float | None):
        """Read only: every AI car with its own value, then how aggression goes with what each car does."""
        cars = self.cars()
        names = {sl: f"#{label(n)}" for sl, n, _ in cars}
        full = {sl: f"#{label(n)} {nm}" for sl, n, nm in cars}
        vals = {sl: self.triplet(sl, AGG)[2] for sl, _, _ in cars}
        stats = {sl: StepStats(full[sl], vals[sl]) for sl in vals}
        session = self.game.session_names()
        self.log(f"FIELD: measuring all {len(stats)} AI cars with their own values (never writes); Enter / q / "
                 "Ctrl+C = end" + (f", or after {auto:.0f} s of green-flag game time" if auto else ""))
        green = 0.0
        t_prev = self.game_s()
        t_check = t_progress = real_prev = time.monotonic()
        prev_flag = None
        try:
            import msvcrt
        except ImportError:
            msvcrt = None
        prev_dmg = {sl: v[4] for sl, v in self.car_states().items()}
        try:
            while True:
                now = time.monotonic()
                if now >= t_check:
                    t_check = now + 1.0
                    if self.game.session_names() != session:
                        self.log("session changed - stopping")
                        break
                    rolled = 0
                    for sl in stats:
                        try:
                            w = self.triplet(sl, AGG)[2]
                        except Exception:
                            continue
                        if abs(w - vals[sl]) > 1e-6:
                            rolled += 1
                            vals[sl] = w
                    if rolled:
                        self.log(f"the game re-rolled {rolled} value(s) - measurement restarted with the new values")
                        stats = {sl: StepStats(full[sl], vals[sl]) for sl in vals}
                        green = 0.0
                t = self.game_s()
                dt = game_dt(t, t_prev, now - real_prev)
                t_prev, real_prev = t, now
                states = self.car_states()
                flag, pit = self._flag_pit()
                if prev_flag == mm.FLAG_GREEN and flag != mm.FLAG_GREEN:
                    self.log(f"  CAUTION (flag {flag}) at {green:.0f} s of green")
                    for st in stats.values():
                        st.cautions += 1
                prev_flag = flag
                if flag == mm.FLAG_GREEN:
                    green += dt
                for sl, st in stats.items():
                    me = states.get(sl)
                    if me is not None:
                        self._sample(st, sl, me, states, pit, flag, dt, t, prev_dmg, names, talk=False)
                prev_dmg = {sl: v[4] for sl, v in states.items()}
                if now >= t_progress + PROGRESS_S:
                    t_progress = now
                    hits = sum(len(st.car_contacts()) for st in stats.values())
                    self.log(f"  {green:.0f} s green measured, contacts seen {hits}")
                if auto and green >= auto:
                    break
                if msvcrt is not None and msvcrt.kbhit():
                    if msvcrt.getwch() in ("\r", "\n", "q", "Q"):
                        break
                time.sleep(SAMPLE_S)
        except KeyboardInterrupt:
            pass
        for sl, st in stats.items():
            self._finish_lift(st, sl)
        meas = sorted((st for st in stats.values() if st.green), key=lambda st: st.value)
        self.log(f"FIELD RESULT ({green:.0f} s of green; side gap = centre to centre, ~{CAR_WIDTH:.1f} m = touching):")
        self.log("  " + StepStats.HEADER.replace("step ", "car  ", 1))
        for st in meas:
            self.log("  " + st.row())
        xs = [st.value for st in meas]
        pairs = [("mean room, types 5-8", [st.agg_room()[0] for st in meas]),
                 ("min room, types 5-8", [st.agg_min if math.isfinite(st.agg_min) else math.nan for st in meas]),
                 ("side gap", [st.side_gap() for st in meas]),
                 ("time alongside", [st.pct(st.along) for st in meas]),
                 ("throttle lift in traffic", [st.lift if st.lift is not None else math.nan for st in meas]),
                 ("contacts a minute", [st.per_min() for st in meas])]
        self.log("  correlation of aggression with (-1..1; |r| < 0.3 = no clear link with this many cars):")
        for what, ys in pairs:
            r = corr(xs, ys)
            self.log(f"    {what:<26} r = {r:+.2f}" if r == r else f"    {what:<26} r = -  (not enough data)")
        self.log("field done")

    # ----------------------------------------------------------------- write
    def test(self, numbers: list[str], steps: list[tuple[str, float]], auto: float | None):
        cars = self.cars()
        chosen = []
        for number in numbers:
            want = raw(number)
            car = next(((s, n, nm) for s, n, nm in cars if int(n) == want), None)
            if car is None:
                raise SystemExit(f"car #{number} isn't an AI car in this session")
            if any(c[0] == car[0] for c in chosen):
                raise SystemExit(f"car #{number} is in the list twice")
            chosen.append(car)
        names = {sl: f"#{label(n)}" for sl, n, _ in cars}
        try:
            pn = self.game._driver(mm.PLAYER_SLOT)[0]
            if pn > 0:
                names[mm.PLAYER_SLOT] = f"#{label(CarNo(pn))} (you)"
        except Exception:
            pass
        slots = sorted(names)
        test_slots = [c[0] for c in chosen]
        if any(s in (mm.PLAYER_SLOT, mm.PACE_SLOT) for s in test_slots):     # cars() never returns them; be sure
            raise SystemExit("the player's car and the pace car are never written")
        addr = {s: self.base(s) + OFF_AGG_WORK for s in test_slots}
        self._lock_cars = [int(n) for _, n, _ in chosen]
        if showdown_lock.write(self._lock_cars):
            self.log(f"took {', '.join('#' + label(n) for _, n, _ in chosen)} from AIMOD ({showdown_lock.lock_path()}): "
                     f"waiting {self.lock_wait:.0f} s for a running AIMOD (new32 or later) to give its values back")
            end = time.monotonic() + self.lock_wait
            while time.monotonic() < end:
                showdown_lock.write(self._lock_cars)
                time.sleep(0.25)
        game_val = {s: self.f32(addr[s]) for s in test_slots}
        session = self.game.session_names()
        for what, v in steps:
            if v > SAFE_MAX:
                self.log(f"NOTE: {what} {v:.2f} is above ~{SAFE_MAX:.2f} - the tightest side-by-side threshold falls "
                         "under a car width: expect it to lean on cars (q puts the game value back at once)")
        full = [("GAME VALUE", None)] + list(steps) + [("GAME VALUE (put back)", None)]
        who = ", ".join(f"#{label(n)} {nm} (slot {s}, 0x{addr[s]:X} = {game_val[s]:.3f})" for s, n, nm in chosen)
        self.log(f"write test: {who}; steps "
                 + " -> ".join(f"{w} {v:.2f}" if v is not None else w for w, v in full))
        if self.side_pad < 1.0:
            self.log(f"side-pad: both cars' side-padding base (car+0x1BEA) x{self.side_pad:.2f} - they leave less "
                     "room beside each other; nobody else changes")
        if self.params:
            self.params_apply()
        if self.pace < 1.0:
            self.log(f"pace: the car in front of the pair gets traction x{self.pace:.3f} (the game's own value times "
                     "that), the one behind keeps his own; they swap when the chaser gets by. Driver behavior in "
                     "AIMOD should be off.")
        self.log("Enter = next step" + (f" (or by itself after {auto:.0f} s of green-flag game time)" if auto else "")
                 + ", q = put it back and quit. Only green-flag laps on track are measured.")
        written: dict[int, float | None] = {s: None for s in test_slots}
        done: list[tuple[dict, PairStats | None]] = []
        cur_step: tuple[dict, PairStats | None] | None = None
        try:
            try:
                import msvcrt
            except ImportError:
                msvcrt = None
            for k, (what, val) in enumerate(full):
                values = {s: (val if val is not None else game_val[s]) for s in test_slots}
                if k:
                    for s in test_slots:
                        self.pm.write_bytes(addr[s], struct.pack("<f", values[s]), 4)
                        written[s] = values[s]
                backs = ", ".join(f"{names[s]} {self.f32(addr[s]):.3f}" for s in test_slots)
                did = "left as the game set it" if not k else "wrote " + ", ".join(
                    f"{names[s]} {values[s]:.3f}" for s in test_slots)
                self.log(f"STEP {k + 1}/{len(full)} {what}: {did}, reads back {backs}")
                if k == len(full) - 1:
                    for s in test_slots:          # the game's values are back
                        written[s] = None
                    break
                sts = {s: StepStats(f"{what} {names[s]}" if len(test_slots) > 1 else what, values[s])
                       for s in test_slots}
                pair = None
                if len(test_slots) == 2:
                    a, b = test_slots
                    pair = PairStats(what, a, b, values[a], values[b])
                cur_step = (sts, pair)
                if not self._run_step(sts, pair, addr, written, session, names, slots, auto, msvcrt):
                    return                        # the session changed or the game rewrote a value
                self._close_step(sts, pair, slots, names, done)
                cur_step = None
                if self.hit is not None:
                    break                         # showdown: contact found - the finally block puts the values back
        except KeyboardInterrupt:
            pass
        finally:
            if cur_step is not None and any(st.green for st in cur_step[0].values()):
                self._close_step(cur_step[0], cur_step[1], slots, names, done)     # quit part-way: keep it
            if self.side_w or self.side_nat:
                self._side_release()
                self.log("put back: the pair's side padding (car+0x1BEA) - the game's own values")
            if self.param_w:
                self.params_restore()
            if self.pace_w or self.pace_nat:
                self._pace_release()
                self.log("put back: the pair's traction (pace) - the game's own values")
            for s in test_slots:
                if written[s] is None:
                    continue
                try:
                    if abs(self.f32(addr[s]) - written[s]) <= 1e-6:      # still ours: put the game's value back
                        self.pm.write_bytes(addr[s], struct.pack("<f", game_val[s]), 4)
                        self.log(f"put back: {names[s]} aggression {game_val[s]:.3f}")
                    else:
                        self.log(f"not put back: {names[s]} was rewritten by the game (its value stands)")
                except Exception as e:
                    self.log(f"could not put {names[s]}'s value back ({e}) - reload the session to be sure")
            if done:
                for s, n, nm in chosen:
                    self.log(f"COMPARISON (#{label(n)} {nm}; side gap = centre to centre, ~{CAR_WIDTH:.1f} m = "
                             "touching; lift = throttle vs clean air at the same spots; hits = contacts with cars; "
                             "yel = cautions):")
                    self.log("  " + StepStats.HEADER)
                    for sts, _ in done:
                        if s in sts:
                            self.log("  " + sts[s].row())
                if any(p is not None for _, p in done):
                    self.log("PAIR COMPARISON (roomA = A's side-by-side room toward B, roomB = B's toward A):")
                    self.log("  " + PairStats.HEADER)
                    for _, p in done:
                        if p is not None:
                            self.log("  " + p.row())
            if self.stop_on_contact:
                if self.hit is None:
                    self.log("SHOWDOWN RESULT: no contact between the pair on any step - they stayed clear of each "
                             "other up to the last value (see the comparison for how close they got)")
                else:
                    w, v, g, da, db = self.hit
                    self.log(f"SHOWDOWN RESULT: first contact at aggression {v:.2f} ({w}), {g:.0f} s into the step; "
                             f"damage {da:.3f} / {db:.3f}")
            showdown_lock.remove()
            self.log("write test done")

    def _close_step(self, sts: dict, pair: PairStats | None, slots, names, done: list):
        states = self.car_states()
        for s, st in sts.items():
            try:
                st.laps1, st.pos1 = self.standing(s, slots)
                st.dmg1 = states.get(s, (0,) * 7)[4]
            except Exception:
                pass
            self._finish_lift(st, s)
            for line in st.lines():
                self.log(line)
        if pair is not None:
            a, b = pair.a, pair.b
            pair.contacts = max(sum(1 for c in sts[a].contacts if c[1] == names.get(b)),
                                sum(1 for c in sts[b].contacts if c[1] == names.get(a)))
            for line in pair.lines(names):
                self.log(line)
        done.append((sts, pair))

    def table_find(self, name: str) -> int | None:
        """address of the live float of a parameter-table entry, found by its key name (never by a fixed address)"""
        want = name.encode()
        for i in range(TABLE_N):
            a = TABLE_BASE + i * TABLE_STRIDE
            try:
                ptr = struct.unpack("<I", self.pm.read_bytes(a + 8, 4))[0]
                if not 0x400000 < ptr < 0x2000000:
                    continue
                if self.pm.read_bytes(ptr, len(want) + 1) == want + b"\0":
                    return a + 16
            except Exception:
                continue
        return None

    def params_apply(self):
        """write the --param overrides (each looked up by name and range-checked); remember the game's values"""
        for name, v in self.params.items():
            lo, hi = TABLE_PARAMS[name]
            addr = self.table_find(name)
            if addr is None:
                self.log(f"param {name}: not found in the game's parameter table - skipped")
                continue
            cur = self.f32(addr)
            if not (math.isfinite(cur) and lo <= cur <= hi * 4 + 1):
                self.log(f"param {name}: the table reads {cur} - not a value I expect, skipped")
                continue
            self.param_nat[name] = (addr, cur)
            val = struct.unpack("<f", struct.pack("<f", v))[0]
            self.pm.write_bytes(addr, struct.pack("<f", val), 4)
            self.param_w[name] = val
            self.log(f"param {name} (0x{addr:X}): {cur:.3f} -> {val:.3f} for the whole field; put back at the end")

    def params_check(self):
        """someone else (AI Enhanced?) rewriting a value we hold ends the override"""
        for name, val in list(self.param_w.items()):
            addr, _ = self.param_nat[name]
            if abs(self.f32(addr) - val) > 1e-6:
                self.log(f"param {name} was rewritten by something else ({self.f32(addr):.3f}) - not holding it any more "
                         "(AI Enhanced or a session reload?)")
                self.param_w.pop(name)

    def params_restore(self):
        for name, val in list(self.param_w.items()):
            addr, nat = self.param_nat[name]
            try:
                if abs(self.f32(addr) - val) <= 1e-6:
                    self.pm.write_bytes(addr, struct.pack("<f", nat), 4)
                    self.log(f"put back: {name} {nat:.3f}")
            except Exception as e:
                self.log(f"could not put {name} back ({e}) - reload the session to be sure")
        self.param_w.clear()
        self.param_nat.clear()

    def _side_set(self, slot: int, scale: float):
        """car+0x1BEA = the game's own x scale (1.0 = the game's own); called on every sample: a value that isn't what we
        wrote is the game's new own value (a mode change reset it)"""
        addr = self.base(slot) + OFF_SIDE_BASE
        cur = self.f32(addr)
        w = self.side_w.get(slot)
        if scale == 1.0:
            if w is not None and abs(cur - w) <= 1e-6 and slot in self.side_nat:
                self.pm.write_bytes(addr, struct.pack("<f", self.side_nat[slot]), 4)
            self.side_w.pop(slot, None)
            return
        if not (0.05 < cur < 5.0):
            return                                       # not the base we know (1.0 / 0.7 / 1.5): leave it
        if w is None or abs(cur - w) > 1e-6:
            self.side_nat[slot] = cur
        val = struct.unpack("<f", struct.pack("<f", self.side_nat[slot] * scale))[0]
        if abs(cur - val) > 1e-7:
            self.pm.write_bytes(addr, struct.pack("<f", val), 4)
        self.side_w[slot] = val

    def _side_release(self):
        for sl in list(self.side_w):
            try:
                self._side_set(sl, 1.0)
            except Exception as e:
                self.log(f"could not put slot {sl}'s side padding back ({e}) - reload the session to be sure")
        self.side_w.clear()

    def _side_tick(self, pair, states, flag, pit):
        a, b = pair.a, pair.b
        sa, sb = states.get(a), states.get(b)
        if flag != mm.FLAG_GREEN or sa is None or sb is None or sa[5] or sb[5] or pit[a] or pit[b]:
            if self.side_w:
                self._side_release()
            return
        self._side_set(a, self.side_pad)
        self._side_set(b, self.side_pad)

    def _tr_addr(self, slot: int) -> int:
        return self.base(slot) + mm.OFF_DRIVER + mm.DRV_TRACTION

    def _pace_set(self, slot: int, scale: float):
        """put the car's traction at scale x the game's own value (1.0 = the game's own). The game rebuilds the value
        at the start of every lap, so this is called on every sample: a value that isn't what we wrote is the game's
        new own value."""
        addr = self._tr_addr(slot)
        cur = self.f32(addr)
        w = self.pace_w.get(slot)
        if scale == 1.0:
            if w is not None and abs(cur - w) <= 1e-6 and slot in self.pace_nat:
                self.pm.write_bytes(addr, struct.pack("<f", self.pace_nat[slot]), 4)
            self.pace_w.pop(slot, None)
            return
        if not (0.2 < cur < 5.0):
            return                                       # not a traction value: leave it
        if w is None or abs(cur - w) > 1e-6:
            self.pace_nat[slot] = cur                    # first look, or the game rebuilt it (a new lap)
        val = struct.unpack("<f", struct.pack("<f", self.pace_nat[slot] * scale))[0]
        if abs(cur - val) > 1e-7:
            self.pm.write_bytes(addr, struct.pack("<f", val), 4)
        self.pace_w[slot] = val

    def _pace_release(self):
        for s in list(self.pace_w):
            try:
                self._pace_set(s, 1.0)
            except Exception as e:
                self.log(f"could not put slot {s}'s traction back ({e}) - reload the session to be sure")
        self.pace_w.clear()
        self.pace_lead = None

    def _pace_tick(self, pair, states, flag, pit, names):
        """--pace: the car in front of the pair gets the slowed traction, the one behind keeps his own - so the
        chaser closes, and when he gets by the roles swap. Only under green with both racing."""
        a, b = pair.a, pair.b
        sa, sb = states.get(a), states.get(b)
        if flag != mm.FLAG_GREEN or sa is None or sb is None or sa[5] or sb[5] or pit[a] or pit[b]:
            if self.pace_w:
                self._pace_release()
            return
        sp = math.hypot(sa[2], sa[3])
        if sp < MIN_SPEED:
            return
        dx, dy = sb[0] - sa[0], sb[1] - sa[1]
        d = dx * sa[2] / sp + dy * sa[3] / sp            # + = b is ahead of a
        if math.hypot(dx, dy) <= PACE_NEAR_M:
            lead = self.pace_lead
            if lead is None:
                lead = b if d > 0 else a
            elif lead == b and d < -PACE_SWITCH_M:
                lead = a
            elif lead == a and d > PACE_SWITCH_M:
                lead = b
            if lead != self.pace_lead:
                self.pace_lead = lead
                other = a if lead == b else b
                self.log(f"  pace: {names[lead]} in front (traction x{self.pace:.3f}), {names[other]} chasing "
                         f"({abs(d):.1f} m apart)")
        if self.pace_lead is None:
            return
        chaser = a if self.pace_lead == b else b
        self._pace_set(self.pace_lead, self.pace)
        self._pace_set(chaser, 1.0)

    def _pair_hit(self, sts: dict, pair) -> tuple[float, float] | None:
        """(damage a took, damage b took) once the two cars have really touched in this step, else None"""
        if pair is None:
            return None
        a, b = pair.a, pair.b
        da = sum(c[2] for c in sts[a].contacts if c[1] == self._names.get(b))
        db = sum(c[2] for c in sts[b].contacts if c[1] == self._names.get(a))
        da = max(da, sum(c[3] for c in sts[b].contacts if c[1] == self._names.get(a)))
        db = max(db, sum(c[3] for c in sts[a].contacts if c[1] == self._names.get(b)))
        return (da, db) if max(da, db) >= HIT_DAMAGE else None

    def _run_step(self, sts: dict, pair, addr, written, session, names, slots, auto, msvcrt) -> bool:
        """measure one step until Enter (or --auto). False = stop the test (the game values stand)."""
        self._names = names
        t_hit = None
        states = self.car_states()
        for s, st in sts.items():
            st.laps0, st.pos0 = self.standing(s, slots)
            st.dmg0 = states.get(s, (0,) * 7)[4]
        prev_dmg = {sl: v[4] for sl, v in states.items()}
        t_prev = self.game_s()
        now0 = time.monotonic()
        t_rel = {s: now0 for s in sts}
        last_rel = {s: "" for s in sts}
        t_check = t_progress = real_prev = now0
        prev_flag = None
        while True:
            now = time.monotonic()
            if now >= t_check:
                t_check = now + 1.0
                showdown_lock.write(self._lock_cars)
                if self.param_w:
                    self.params_check()
                if self.game.session_names() != session:
                    self.log("session changed - stopping (the new session loaded its own values)")
                    return False
                for s in sts:
                    cur = self.f32(addr[s])
                    if written[s] is not None and abs(cur - written[s]) > 1e-6:
                        # the game re-rolls every driver's value at a session start (live 2026-11-03): its new value
                        # stands, putting the old one back would undo the game's roll
                        self.log(f"the game rewrote {names[s]}: {written[s]:.3f} -> {cur:.3f} (session byte "
                                 f"{self.pm.read_bytes(mm.SESSION_STATE, 1)[0]}, flag "
                                 f"{self.pm.read_int(mm.FLAG_STATE)}) - test stopped, the game's value stays; run it "
                                 "again once the session is under way. If the value went to ~3.0 or back to what it was, "
                                 "something else is writing it: AIMOD's Racing intensity (use AIMOD_new32 or later, "
                                 "or switch it off for the test)")
                        return False
            t = self.game_s()
            dt = game_dt(t, t_prev, now - real_prev)     # clock jump (restart, replay): nothing counted
            t_prev, real_prev = t, now
            states = self.car_states()
            flag, pit = self._flag_pit()
            if self.side_pad < 1.0 and pair is not None:
                try:
                    self._side_tick(pair, states, flag, pit)
                except Exception as e:
                    self.log(f"  side-pad: skipped ({e})")
            if self.pace < 1.0 and pair is not None:
                try:
                    self._pace_tick(pair, states, flag, pit, names)
                except Exception as e:                   # a car that went away mid-call
                    self.log(f"  pace: skipped ({e})")
            if prev_flag == mm.FLAG_GREEN and flag != mm.FLAG_GREEN:
                self.log(f"  CAUTION (flag {flag}) during step {next(iter(sts.values())).name}")
                for st in sts.values():
                    st.cautions += 1
            prev_flag = flag
            rels = {}
            for s, st in sts.items():
                me = states.get(s)
                if me is None:
                    continue
                line = self._sample(st, s, me, states, pit, flag, dt, t, prev_dmg, names)
                rels[s] = st.last_rels
                if line and line != last_rel[s] and now >= t_rel[s]:
                    t_rel[s] = now + 1.0
                    self.log(f"  [{st.name}] {line}")
                    last_rel[s] = line
            if pair is not None:
                pair.sample(states, pit, flag, dt, rels)
            prev_dmg = {sl: v[4] for sl, v in states.items()}
            green = max(st.green for st in sts.values())
            if self.stop_on_contact and self.hit is None:
                dm = self._pair_hit(sts, pair)
                if dm is not None:
                    t_hit = t
                    self.hit = (next(iter(sts.values())).name.split(" #")[0], next(iter(sts.values())).value, green,
                                dm[0], dm[1])
                    self.log(f"  FIRST CONTACT between {names[pair.a]} and {names[pair.b]} at aggression "
                             f"{self.hit[1]:.2f}, {green:.0f} s into the step: damage {names[pair.a]} +{dm[0]:.3f}, "
                             f"{names[pair.b]} +{dm[1]:.3f} - watching {HIT_TAIL_S:.0f} s more, then both are put back")
            if t_hit is not None and t - t_hit >= HIT_TAIL_S:
                return True
            if now >= t_progress + PROGRESS_S:
                t_progress = now
                self.log("  " + "; ".join(f"[{st.name}] {st.green:.0f} s green: alongside {st.pct(st.along):.1f}%, "
                                          f"behind a car {st.pct(st.behind):.1f}%, contacts {len(st.car_contacts())}"
                                          for st in sts.values()))
            if auto and green >= auto:
                self.log(f"  {auto:.0f} s of green measured - next step")
                return True
            if msvcrt is not None and msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\r", "\n"):
                    return True
                if ch.lower() == "q":
                    raise KeyboardInterrupt
            time.sleep(SAMPLE_S)


def _isfloat(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def _fmt(v) -> str:
    return "-" if v is None else f"{v:.3f}"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    who = ap.add_mutually_exclusive_group()
    who.add_argument("--test", metavar="CAR", help="AI car number to run the write test on (e.g. 24 or 01)")
    who.add_argument("--pair", nargs=2, metavar=("CAR1", "CAR2"),
                     help="two AI cars: both get each step value; also measures how they race each other")
    who.add_argument("--showdown", nargs=2, metavar=("CAR1", "CAR2"),
                     help=f"--pair with the showdown ramp {','.join(f'{v:g}' for v in SHOWDOWN_STEPS)} (override with "
                          "--steps), --auto 120 by default and --stop-on-contact: ramps the pair up until they touch")
    who.add_argument("--field", action="store_true", help="read only: measure every AI car with its own value")
    ap.add_argument("--low", type=float, help="LOW aggression value (default: half the field's lowest)")
    ap.add_argument("--high", type=float, help=f"HIGH aggression value (default: 1.5 x the field's highest, at most "
                                               f"{SAFE_MAX:.2f}; e.g. --high 3 goes past it on purpose)")
    ap.add_argument("--steps", help="comma list of values to step through instead of LOW / HIGH, e.g. 0.5,2.4,3.0")
    ap.add_argument("--auto", type=float, metavar="SECONDS",
                    help="go to the next step (field: stop) by itself after this many seconds of green-flag game time")
    ap.add_argument("--pace", type=float, metavar="SCALE",
                    help=f"pair only: slow whichever car of the pair is in front by this traction scale "
                         f"({PACE_MIN:g}-1; {PACE_SHOWDOWN:g} is the --showdown default, 1 = off) so the one behind "
                         "has to close and pass; the roles swap after a pass. A 2-car race gives the chaser no reason "
                         "to pass otherwise (Richmond 11-05: 12-15 m apart for 6 minutes). Untick Driver behavior in "
                         "AIMOD first: it writes the same traction value")
    ap.add_argument("--param", action="append", metavar="NAME=VALUE",
                    help="pair / test: set a game parameter-table value for the whole field while the test runs (put "
                         "back at the end), e.g. --param ai_drafting_distance=0.3 (the car lengths the AI keeps "
                         "behind the car in front; Richmond's track.ini has 1.10 = about 10.5 m centre to centre). "
                         "Allowed: " + ", ".join(TABLE_PARAMS) + ". Repeat for several. AI Enhanced writes these "
                         "too - close it for the test")
    ap.add_argument("--side-pad", type=float, metavar="SCALE",
                    help="pair only: scale both cars' side-padding base (car+0x1BEA, normally 1.0) so they leave less "
                         "room beside each other - per car, nobody else changes (0-1; e.g. 0.2). Meant to be used "
                         "with a high aggression step: at 3.3-4.0 the side gap still stayed 3.1-3.6 m (Richmond 11-05)")
    ap.add_argument("--stop-on-contact", action="store_true",
                    help="pair: end the ramp at the first real contact between the two (4 s of aftermath are logged)")
    ap.add_argument("--lock-wait", type=float, default=LOCK_WAIT_S, metavar="SECONDS",
                    help="seconds to wait after taking the cars from a running AIMOD (new32+) before reading their "
                         "values (default %(default)s)")
    ap.add_argument("--no-watch", action="store_true", help="read mode: print the table and quit")
    ap.add_argument("--log-dir", help="folder for the log (default: <AIMOD>\\app\\logs)")
    ap.add_argument("--fake", action="store_true", help="dry run against a fake NR2003 (tools\\fake_nr2003.py)")
    ap.add_argument("--fake-accel", type=float, default=8.0, help="fake: game seconds per real second")
    ap.add_argument("--fake-reroll", type=float, metavar="GAME_S", help="fake: the game re-rolls every value then")
    a = ap.parse_args(argv)
    if a.showdown:
        a.pair, a.stop_on_contact = a.showdown, True
        if not a.steps:
            a.steps = ",".join(f"{v:g}" for v in SHOWDOWN_STEPS)
        if a.auto is None:
            a.auto = SHOWDOWN_AUTO_S
        if a.pace is None:
            a.pace = PACE_SHOWDOWN
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
    params = {}
    for item in a.param or []:
        name, _, val = item.partition("=")
        name = name.strip()
        if name not in TABLE_PARAMS:
            raise SystemExit(f"--param {name}: not one of {', '.join(TABLE_PARAMS)}")
        try:
            v = float(val)
        except ValueError:
            raise SystemExit(f"--param {item}: needs NAME=NUMBER")
        lo, hi = TABLE_PARAMS[name]
        if not lo <= v <= hi:
            raise SystemExit(f"--param {name}={v:g} is outside {lo:g}-{hi:g}")
        params[name] = v
    if a.side_pad is not None:
        if not a.pair:
            raise SystemExit("--side-pad needs two cars (--pair or --showdown)")
        if not SIDE_PAD_MIN <= a.side_pad <= 1.0:
            raise SystemExit(f"--side-pad {a.side_pad} is outside {SIDE_PAD_MIN:g}-1")
    if a.pace is not None:
        if not a.pair:
            raise SystemExit("--pace needs two cars (--pair or --showdown)")
        if not PACE_MIN <= a.pace <= 1.0:
            raise SystemExit(f"--pace {a.pace} is outside {PACE_MIN:g}-1 (faster than the car's own value is untested "
                             "live, slower than that is a different test)")
    if a.auto is not None and a.auto <= 0:
        raise SystemExit("--auto needs a number of seconds above 0")
    pm = None
    if a.fake:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from fake_nr2003 import FakeNR2003
        pm = FakeNR2003(accel=a.fake_accel, reroll_at=a.fake_reroll)
    p = Probe(pm, a.log_dir)
    p.stop_on_contact = a.stop_on_contact
    p.lock_wait = a.lock_wait
    p.pace = a.pace if a.pace is not None else 1.0
    p.params = params
    p.side_pad = a.side_pad if a.side_pad is not None else 1.0
    try:
        work = p.read_table()
        numbers = [a.test] if a.test else list(a.pair or [])
        if numbers:
            if a.steps:
                steps = [(f"STEP {v:.2f}", v) for v in wanted]
            else:
                vals = sorted(work.values())
                low = a.low if a.low is not None else max(0.0, vals[0] * 0.5)
                high = a.high if a.high is not None else min(SAFE_MAX, max(vals[-1] * 1.5, vals[-1] + 0.3))
                steps = [("LOW", low), ("HIGH", high)]
            p.test(numbers, steps, a.auto)
        elif a.field:
            p.field(a.auto)
        elif not a.no_watch:
            p.watch(work)
    finally:
        p.close()
    return p


if __name__ == "__main__":
    main()
