"""Read-only race probe: what actually sends each AI car to pit road, from the game's own memory.

Never writes to NR2003. Run it from the green flag (or before) and leave it going until the unwanted stops show up.
It records, per AI car, every change of:
  - the AI mode (names read from the game: Cruising, PitInLapFast/Slow/Caution, EnteringPits*, InPitStall*,
    LeavingPits*, LimpingToPits*, CallingTow, BeingTowed ...) - the game's own decision to head for pit road
  - the pit request (+8118) and the penalty-stop flag (+8119) - bytes the game, AIMOD and RaceControl all write
  - the game's repair need (+0x1B33)
  - on pit road / stopped on pit road / the pit service timer running
and when a car turns for pit road (or its request rises) the inputs every game pit check reads: fuel, tire wear,
"car condition" (+4943), laps vs the game's planned stop lap (+0x1A7A / +0x1B06), penalty rules owed, damage.
Global: flag, leader lap, pit road state, RaceControl's pit road gate, AIMOD's pit gate refusals, every AIMOD code
patch (on/off) and whether AIMOD / RaceControl are running - so the log says exactly what was changing the game.

A STOP is counted only when the car sits still in its pit box with the service timer running - not from any app's
pit log. The end of the file has a per-car table of stops and what preceded each one; a CSV of the stops is
written beside it.

Run (NR2003 running; admin terminal if the game runs as admin):
    python tools\\pit_probe.py
Logs: AIMOD\\app\\logs\\pit_probe_<date>_<time>.txt and ..._stops.csv   (Ctrl+C to stop and write the summary)
"""
from __future__ import annotations

import csv
import datetime
import os
import struct
import subprocess
import sys
import time

import pymem

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
try:
    from aimod import memory_map as mm          # patch table only (addresses + original/patched bytes)
    PATCHES = mm.PATCHES
except Exception:
    PATCHES = {}

CAR0, STRIDE, SLOTS = 0xACCA30, 8724, 44
PLAYER, PACE = 0, 1
OFF_LAPS, OFF_VEL, OFF_TIMER = 1360, 6634, 1734
OFF_LAP_DIST = 6250                     # float: metres past the stripe ("Dlong", RaceControl's; valid for the pace
                                        # car too - AIMOD's +8668 reads 0 for it)
TRACK_LENGTH = 0xDACF0C                 # double, metres
OFF_MODE, OFF_REQ, OFF_PEN, OFF_REPAIR = 0x13A7, 8118, 8119, 0x1B33
OFF_COND, OFF_OUT = 4943, 4916
OFF_REPAIR_FLAG = 0x73D                 # byte: crew repairs damage this stop ('D' in the game's "AIPIT IN" print)
OFF_LAPCOUNT, OFF_PLANNED = 0x1A7A, 0x1B06
OFF_WEAR = 7342
OFF_DMG = 4903
OFF_FUEL_A, OFF_FUEL_B, OFF_FUEL_CAP_PTR, FUEL_CAP_OFF = 0x518, 0x520, 0x58A, 0x24
DRIVERS, DRIVER_SIZE, DRIVER_NUM = 0xE07B54, 175, 156     # (= AIMOD DRIVER_ENTRY_0 14711636)
FLAG, SESSION, LEADER_LAPS, GAME_TICKS = 0x76F184, 0x977A00, 0xBE0E48, 0xBEBDF5
ON_PIT_ROAD = 0x76B230 + 0x8DC
PIT_STATE = 0x76B2F9                    # u16: 0x100 open to the lead lap only, 0x8 open to all
RC_PIT_GATE = 0x76F563                  # 2 dwords (one per pit lane): 2 all may pit, 1 lead lap only, else none
PENALTY_MGR = 0xBDB9D8
GATE_SITE, GATE_ORIG = 0x52A1B0, bytes.fromhex("568bf1578b7c240c")
FLAGS = {0: "caution (pits closed)", 1: "caution 1", 2: "caution 2", 3: "caution 3", 4: "caution 4",
         5: "one to go", 6: "one to go (double file)", 7: "GREEN"}
INTERVAL = 0.03
STOPPED_MS = 0.6                        # m/s


def now_s() -> str:
    return f"{datetime.datetime.now():%H:%M:%S.%f}"[:-3]


def mode_names(pm) -> dict:
    out = {}
    blob = pm.read_bytes(0x760B00, 0x800)
    for off in range(0, len(blob) - 12, 4):
        vt, name = struct.unpack_from("<II", blob, off)
        if 0x6E9000 <= vt < 0x6EF000 and 0x700000 <= name < 0x800000:
            try:
                s = pm.read_bytes(name, 40).split(b"\0")[0].decode("ascii")
            except Exception:
                continue
            if s.isprintable() and len(s) > 3:
                out[0x760B00 + off] = s
    return out


def kind(name: str) -> str:
    if name.startswith("PitInLap"):
        return "turn"                   # committed: heading for pit road
    if name.startswith(("Limping", "CallingTow", "BeingTowed")):
        return "limp"
    if name.startswith(("EnteringPits", "InPitStall", "LeavingPits", "Pit")):
        return "pitlane"
    return ""


class Probe:
    def __init__(self):
        names = ["NR2003.exe", "NR2003_clean.exe"]
        if "--process" in sys.argv:
            names.insert(0, sys.argv[sys.argv.index("--process") + 1])
        for name in names:
            try:
                self.pm = pymem.Pymem(name)
                break
            except Exception:
                continue
        else:
            raise SystemExit(f"NR2003 is not running (looked for {', '.join(names)})")
        self.exe_name = name
        self.modes = mode_names(self.pm)
        stamp = f"{datetime.datetime.now():%Y%m%d_%H%M%S}"
        d = os.path.join(HERE, "app", "logs")
        os.makedirs(d, exist_ok=True)
        self.path = os.path.join(d, f"pit_probe_{stamp}.txt")
        self.csv_path = os.path.join(d, f"pit_probe_{stamp}_stops.csv")
        self.out = open(self.path, "a", encoding="utf-8")
        self.last: dict[int, dict] = {}
        self.timer_prev: dict[int, float] = {}
        self.why: dict[int, str] = {}           # last pit-turn / request context per car
        self.why_lap: dict[int, int] = {}
        self.stops: dict[int, list] = {}
        self.in_stop: dict[int, dict] = {}
        self.g = {}
        self.gate_counts: list[int] | None = None
        self.procs = ""
        self.t_procs = 0.0
        self.still_since: dict[int, float] = {}
        self.pace: tuple = (None, 0.0)
        self.pace_prev = None
        self.track_len = 0.0
        self.entry_points: list[float] = []
        self.exit_points: list[float] = []
        self.held: dict[int, float] = {}
        self.holds: list[tuple] = []

    # ------------------------------------------------------------------ output
    def log(self, msg: str):
        line = f"{now_s()} L{self.g.get('lap', '?'):>3} {self.g.get('flagtxt', ''):<9} {msg}"
        print(line)
        self.out.write(line + "\n")
        self.out.flush()

    def num(self, slot: int) -> str:
        try:
            return f"#{self.pm.read_int(DRIVERS + slot * DRIVER_SIZE + DRIVER_NUM)}"
        except Exception:
            return f"slot{slot}"

    # ------------------------------------------------------------------ game reads
    def fuel_pct(self, blk: bytes) -> float | None:
        try:
            pa, pb, pc = (struct.unpack_from("<I", blk, o)[0] for o in (OFF_FUEL_A, OFF_FUEL_B, OFF_FUEL_CAP_PTR))
            a, b = self.pm.read_bytes(pa, 8), self.pm.read_bytes(pb, 8)
            fuel = struct.unpack("<d", bytes(x ^ y for x, y in zip(a, b)))[0]
            cap = struct.unpack("<f", self.pm.read_bytes(pc + FUEL_CAP_OFF, 4))[0]
            return 100 * fuel / cap if cap else None
        except Exception:
            return None

    def dump(self, n: str, slot: int, blk: bytes, what: str):
        """Raw bytes for finding the crew's repair switch: the pit request sub-struct (+1567, 0x110 bytes), the
        damage block (+0x1327, 0x68) and the pit / service area of the car block (+0x1A70..+0x1B40)."""
        self.log(f"{n:>4} DUMP {what} slot {slot} pitreq+1567 {blk[1567:1567 + 0x130].hex()} D={blk[OFF_REPAIR_FLAG]}")
        self.log(f"{n:>4} DUMP {what} slot {slot} dmg+1327 {blk[0x1327:0x1327 + 0x68].hex()}")
        self.log(f"{n:>4} DUMP {what} slot {slot} car+1A70 {blk[0x1A70:0x1B40].hex()}")

    @staticmethod
    def cond(blk: bytes) -> float:
        return struct.unpack_from("<f", blk, OFF_COND)[0]

    @staticmethod
    def dmg(blk: bytes) -> float:
        f = lambda o: struct.unpack_from("<f", blk, o)[0]
        return f(OFF_DMG + 16) + f(OFF_DMG + 20) + f(OFF_DMG + 48) + f(OFF_DMG + 52)

    def rules_owed(self, slot: int) -> list[str]:
        pm = self.pm
        try:
            mgr = pm.read_uint(PENALTY_MGR)
            arr, cnt = pm.read_uint(mgr + 4), pm.read_int(mgr + 8)
            owed = []
            for k in range(max(0, min(cnt, 64))):
                r = pm.read_uint(arr + 4 * k)
                v = pm.read_uint(r + 4 + 4 * slot) if r else 0
                if v & 0xF or v & 0x10000:
                    owed.append(f"rule{k}=0x{v:X}")
            return owed
        except Exception:
            return ["(rules unreadable)"]

    def context(self, slot: int, blk: bytes) -> str:
        f = lambda o: struct.unpack_from("<f", blk, o)[0]
        i = lambda o: struct.unpack_from("<i", blk, o)[0]
        fuel = self.fuel_pct(blk)
        wear = [f(OFF_WEAR + 4 * k) for k in range(4)]
        cond = f(OFF_COND)
        laps, planned = i(OFF_LAPCOUNT), i(OFF_PLANNED)
        repair, pen = i(OFF_REPAIR), blk[OFF_PEN]
        dmg = f(OFF_DMG + 16) + f(OFF_DMG + 20) + f(OFF_DMG + 48) + f(OFF_DMG + 52)
        owed = self.rules_owed(slot)
        cands = []
        if fuel is not None and fuel < 10:
            cands.append("fuel<10%")
        if max(wear) > 0.95:
            cands.append("tire wear>0.95")
        if 0.5 <= cond <= 1.0:
            cands.append("car condition 0.5-1.0")
        if planned > 0 and laps >= planned:
            cands.append("game's planned stop lap reached")
        if repair > 0:
            cands.append(f"repair need {repair}")
        if pen:
            cands.append("penalty-stop flag")
        if any(o for o in owed if not o.startswith("(")):
            cands.append("penalty rule")
        return (f"[fuel {fuel:.1f}%" if fuel is not None else "[fuel ?") \
            + f" wear {'/'.join(f'{w:.2f}' for w in wear)} cond {cond:.3f} laps {laps} planned-stop {planned}" \
            + f" repair {repair} pen {pen} dmg {dmg:.3f} rules {','.join(owed) or '-'}]" \
            + f" -> game reasons that match: {', '.join(cands) or 'NONE of the known checks'}"

    # ------------------------------------------------------------------ workspace
    def workspace(self, first=False):
        pm = self.pm
        parts = []
        for name, p in PATCHES.items():
            try:
                cur = pm.read_bytes(p["addr"], len(p["original"]))
                st = "ON" if cur == p["patched"] else "off" if cur == p["original"] else f"?{cur.hex()}"
            except Exception:
                st = "unreadable"
            parts.append(f"{name}={st}")
        try:
            site = pm.read_bytes(GATE_SITE, 8)
            if site == GATE_ORIG:
                gate = "not installed"
                self.gate_cave = None
            elif site[0] == 0xE9:
                tgt = GATE_SITE + 5 + struct.unpack("<i", site[1:5])[0]
                cave = tgt - 0x180
                try:
                    magic = pm.read_bytes(cave, 8)
                except Exception:
                    magic = b""
                if magic == b"AIMGATE2":
                    dl = pm.read_uint(cave + 8)
                    gate = f"v2 {'ARMED' if pm.read_uint(GAME_TICKS) < dl else 'disarmed'}"
                    self.gate_cave = cave
                else:
                    gate = "hooked (older/unknown cave)"
                    self.gate_cave = None
            else:
                gate = f"? {site.hex()}"
                self.gate_cave = None
        except Exception:
            gate = "unreadable"
            self.gate_cave = None
        parts.append(f"pit_gate={gate}")
        txt = "  ".join(parts)
        if first or txt != self.g.get("ws"):
            self.log(("WORKSPACE at start: " if first else "WORKSPACE changed: ") + txt)
            self.g["ws"] = txt

    def processes(self):
        if time.time() - self.t_procs < 10:
            return
        self.t_procs = time.time()
        try:
            out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True,
                                 creationflags=0x08000000).stdout
            names = sorted({l.split('","')[0].strip('"') for l in out.splitlines()
                            if l.lower().startswith(('"aimod', '"racecontrol', '"racevision'))})
        except Exception:
            names = ["?"]
        txt = ", ".join(names) or "none"
        if txt != self.procs:
            self.log(f"APPS running: {txt}")
            self.procs = txt

    # ------------------------------------------------------------------ scan
    def scan_globals(self):
        pm = self.pm
        flag = pm.read_int(FLAG)
        sess = pm.read_bytes(SESSION, 1)[0]
        try:
            allb = pm.read_bytes(CAR0, SLOTS * STRIDE)
            lap = max(struct.unpack_from("<i", allb, s * STRIDE + OFF_LAPS)[0] for s in range(2, SLOTS)) + 1
        except Exception:
            lap = 0
        pit_state = struct.unpack("<H", pm.read_bytes(PIT_STATE, 2))[0]
        lanes = struct.unpack("<ii", pm.read_bytes(RC_PIT_GATE, 8))
        self.g["lap"] = lap
        self.g["flagtxt"] = "GREEN" if flag == 7 else f"YELLOW{flag}"
        if sess != 7:
            self.g["green_seen"] = False         # (cars rolling out of their stalls before the start: not stops)
        elif (flag == 7 or lap > 2) and not self.g.get("green_seen"):   # (or started with a race under way)
            self.g["green_seen"] = True
        for key, val, txt in (("flag", flag, f"FLAG {flag}: {FLAGS.get(flag, flag)}"),
                              ("sess", sess, f"SESSION {sess} ({'race' if sess == 7 else 'not a race'})"),
                              ("pit_state", pit_state, f"PIT ROAD STATE 0x{pit_state:X} (0x100 lead lap only, "
                                                       f"0x8 all)"),
                              ("lanes", lanes, f"PIT ROAD GATE lanes {lanes[0]}/{lanes[1]} (2 all may pit, 1 lead lap "
                                               f"only, other = closed; RaceControl writes this)")):
            if self.g.get(key) != val:
                self.g[key] = val
                self.log(txt)

    def scan_gate(self):
        cave = getattr(self, "gate_cave", None)
        if cave is None:
            self.gate_counts = None
            return
        try:
            counts = list(struct.unpack("<64I", self.pm.read_bytes(cave + 0x20, 256)))
        except Exception:
            return
        if self.gate_counts is not None:
            for s, (a, b) in enumerate(zip(self.gate_counts, counts)):
                if b > a and s < SLOTS:
                    blk = self.pm.read_bytes(CAR0 + s * STRIDE, STRIDE)
                    self.log(f"{self.num(s):>4} AIMOD PIT GATE REFUSED a turn for pit road (x{b - a}) "
                             + self.context(s, blk))
        self.gate_counts = counts

    # ------------------------------------------------------------------ pace car / pit exit
    def pace_txt(self) -> str:
        """Where the pace car is, and how long until it reaches pit exit (learned from cars leaving pit road)."""
        pd, ps = self.pace
        if pd is None:
            return "pace car ?"
        txt = f"pace car at {pd:.0f} m, {ps * 2.237:.0f} mph"
        ex, tl = self.median(self.exit_points), self.track_len
        if ex is not None and tl > 0:
            ahead = (ex - pd) % tl
            txt += f", {ahead:.0f} m before pit exit ({ex:.0f} m)"
            if ps > 1:
                txt += f" = {ahead / ps:.1f} s"
        return txt

    @staticmethod
    def median(xs):
        if not xs:
            return None
        s = sorted(xs)
        return s[len(s) // 2]

    def scan_cars(self):
        pm = self.pm
        allb = pm.read_bytes(CAR0, SLOTS * STRIDE)
        road = pm.read_bytes(ON_PIT_ROAD, SLOTS)
        try:
            tl = pm.read_double(TRACK_LENGTH)
            if 100 < tl < 20000:
                self.track_len = tl
        except Exception:
            pass
        pd = struct.unpack_from("<f", allb, PACE * STRIDE + OFF_LAP_DIST)[0]
        now = time.time()
        speed = self.pace[1]
        if self.pace_prev is not None and 0 <= pd < 20000 and self.track_len > 0:
            d0, t0 = self.pace_prev
            if now - t0 >= 0.5:                      # speed from the distance covered (the velocity reads 0)
                speed = ((pd - d0) % self.track_len) / (now - t0)
                if speed > 120:
                    speed = 0.0
                self.pace_prev = (pd, now)
        else:
            self.pace_prev = (pd, now)
        self.pace = (pd if 0 <= pd < 20000 else None, speed)
        for s in range(SLOTS):
            if s in (PLAYER, PACE):
                continue
            blk = allb[s * STRIDE:(s + 1) * STRIDE]
            laps = struct.unpack_from("<i", blk, OFF_LAPS)[0]
            vx, vy = struct.unpack_from("<ff", blk, OFF_VEL)
            speed = (vx * vx + vy * vy) ** 0.5
            if laps <= 0 and speed < 1 and s not in self.last:
                continue
            mode_ptr = struct.unpack_from("<I", blk, OFF_MODE)[0]
            timer = struct.unpack_from("<f", blk, OFF_TIMER)[0]
            tprev = self.timer_prev.get(s)
            self.timer_prev[s] = timer
            on_road = bool(road[s])
            cur = {
                "mode": mode_ptr, "req": blk[OFF_REQ], "pen": blk[OFF_PEN],
                "repair": struct.unpack_from("<i", blk, OFF_REPAIR)[0],
                "road": on_road, "still": on_road and speed < STOPPED_MS,
                "service": on_road and tprev is not None and timer > 0.05 and abs(timer - tprev) > 0.001,
                "out": blk[OFF_OUT] > 0,
                "dflag": blk[OFF_REPAIR_FLAG],
            }
            prev = self.last.get(s)
            self.last[s] = cur
            if prev is None:
                continue
            n = self.num(s)
            mname = lambda p: self.modes.get(p, hex(p))
            if cur["mode"] != prev["mode"]:
                new = mname(cur["mode"])
                k = kind(new)
                msg = f"{n:>4} MODE {mname(prev['mode'])} -> {new}"
                if new.startswith("InPitStallWaiting"):
                    self.held[s] = time.time()
                    msg += f"  HELD IN BOX (service done) - {self.pace_txt()}"
                elif mname(prev["mode"]).startswith("InPitStallWaiting") and s in self.held:
                    t = time.time() - self.held.pop(s)
                    self.holds.append((self.g.get("flagtxt"), t))
                    msg += f"  RELEASED after {t:.1f} s held - {self.pace_txt()}"
                if k in ("turn", "limp") and kind(mname(prev["mode"])) not in ("turn", "limp", "pitlane"):
                    ctx = self.context(s, blk)
                    msg += f"  (request {'already set' if prev['req'] else 'not set before'}) " + ctx
                    self.why[s] = f"{new} at L{self.g.get('lap')} {self.g.get('flagtxt')}: " + ctx.split(" -> ")[-1]
                    self.why_lap[s] = self.g.get("lap", 0)
                self.log(msg)
            if cur["req"] != prev["req"]:
                m = mname(cur["mode"])
                if cur["req"]:
                    who = ("set by the game's mode switch" if kind(m) in ("turn", "limp")
                           else "set while still in " + m + " (AIMOD's call, RaceControl, or the game's pit check / "
                                "re-plan - the AI hasn't turned yet)")
                    self.log(f"{n:>4} PIT REQUEST 0 -> 1 {who} " + self.context(s, blk))
                    if s not in self.why or self.why_lap.get(s) != self.g.get("lap"):
                        self.why[s] = f"request set in {m} at L{self.g.get('lap')} {self.g.get('flagtxt')}: " \
                                      + self.context(s, blk).split(" -> ")[-1]
                        self.why_lap[s] = self.g.get("lap", 0)
                else:
                    where = "on pit road" if on_road else "on track (called off before pit road)"
                    self.log(f"{n:>4} PIT REQUEST 1 -> 0 {where}, mode {m}")
            if cur["pen"] != prev["pen"]:
                self.log(f"{n:>4} PENALTY-STOP FLAG {prev['pen']} -> {cur['pen']}, mode {mname(cur['mode'])}, "
                         f"rules {','.join(self.rules_owed(s)) or '-'}")
            if cur["dflag"] != prev["dflag"]:
                self.log(f"{n:>4} CREW REPAIR FLAG (car+0x73D, 'D' in the game's AIPIT print) {prev['dflag']} -> "
                         f"{cur['dflag']}, mode {mname(cur['mode'])}, on pit road {int(on_road)}, tires "
                         f"{blk[1777]}{blk[1778]} fuel {blk[1781]}, damage {self.dmg(blk):.3f}")
            if cur["repair"] != prev["repair"]:
                self.log(f"{n:>4} REPAIR NEED {prev['repair']} -> {cur['repair']}, mode {mname(cur['mode'])}")
            if cur["road"] != prev["road"]:
                here = struct.unpack_from("<f", blk, OFF_LAP_DIST)[0]
                if self.g.get("green_seen") and 0 <= here < 20000:
                    (self.entry_points if on_road else self.exit_points).append(here)
                self.log(f"{n:>4} PIT ROAD {'IN' if on_road else 'OUT'} at {here:.0f} m, mode {mname(cur['mode'])}, "
                         f"request {cur['req']} - {self.pace_txt()}")
                if on_road and self.g.get("green_seen"):
                    self.dump(n, s, blk, "PIT ROAD IN")
            if cur["still"] != prev["still"]:
                self.log(f"{n:>4} {'STOPPED on pit road' if cur['still'] else 'MOVING again'} "
                         f"(timer {timer:.1f}, mode {mname(cur['mode'])}, request {cur['req']}, repair flag "
                         f"{cur['dflag']}, damage {self.dmg(blk):.3f}) - {self.pace_txt()}")
            if cur["still"] and not prev["still"]:
                self.still_since[s] = time.time()
            elif not cur["still"]:
                self.still_since.pop(s, None)
            long_still = cur["still"] and time.time() - self.still_since.get(s, time.time()) > 2.0
            if cur["service"] and not prev["service"]:
                self.log(f"{n:>4} SERVICE STARTED (mode {mname(cur['mode'])})")
            if (cur["service"] or long_still) and s not in self.in_stop and self.g.get("green_seen"):
                if not cur["service"]:
                    self.log(f"{n:>4} STILL on pit road 2 s with no service timer (mode {mname(cur['mode'])}) - "
                             f"counted as a stop")
                self.dump(n, s, blk, "STOP START")
                self.in_stop[s] = {"lap": self.g.get("lap"), "flag": self.g.get("flagtxt"),
                                   "t": time.time(), "why": self.why.get(s, "no turn / request seen"),
                                   "fuel_in": self.fuel_pct(blk), "serviced": bool(cur["service"]),
                                   "cond_in": self.cond(blk), "dmg_in": self.dmg(blk)}
            if cur["service"] and s in self.in_stop:
                self.in_stop[s]["serviced"] = True
            if prev["service"] and not cur["service"] and s in self.in_stop:
                self.in_stop[s]["service_s"] = timer
                self.log(f"{n:>4} SERVICE ENDED after {timer:.1f} s (damage {self.dmg(blk):.3f}, repair flag "
                         f"{cur['dflag']}) - {self.pace_txt()}")
            st = self.in_stop.get(s)
            if st is not None and not on_road:
                st["road_out_t"] = time.time()
                fuel_out = self.fuel_pct(blk)
                st["fuel_out"] = fuel_out
                self.dump(n, s, blk, "STOP END")
                st["cond_out"], st["dmg_out"] = self.cond(blk), self.dmg(blk)
                st["slot"] = s
                self.stops.setdefault(s, []).append(st)
                del self.in_stop[s]
                self.why.pop(s, None)
                fixed = "REPAIRED" if (st["cond_out"] < st["cond_in"] - 0.01 or st["dmg_out"] < st["dmg_in"] - 0.01) \
                    else "not repaired"
                self.log(f"{n:>4} STOP #{len(self.stops[s])} done (lap {st['lap']}, {st['flag']}, service "
                         f"{st.get('service_s', 0):.1f} s, {time.time() - st['t']:.0f} s from service start to "
                         f"pit exit; condition {st['cond_in']:.3f} -> {st['cond_out']:.3f}, damage "
                         f"{st['dmg_in']:.3f} -> {st['dmg_out']:.3f}: {fixed}) - before it: {st['why']}")
            if cur["out"] and not prev["out"]:
                self.log(f"{n:>4} OUT OF THE RACE")

    # ------------------------------------------------------------------ summary
    def summary(self):
        self.log("=" * 30 + " SUMMARY: stops seen (car still in its box, service timer running) " + "=" * 30)
        rows = []
        for s in sorted(self.stops, key=lambda k: -len(self.stops[k])):
            n = self.num(s)
            self.log(f"{n:>4}: {len(self.stops[s])} stop(s)")
            for k, st in enumerate(self.stops[s], 1):
                self.log(f"        {k}. lap {st['lap']} {st['flag']}: condition {st['cond_in']:.3f} -> "
                         f"{st['cond_out']:.3f}, damage {st['dmg_in']:.3f} -> {st['dmg_out']:.3f}; {st['why']}")
                rows.append([n, s, k, st["lap"], st["flag"], f"{st.get('service_s', 0):.1f}",
                             f"{st['fuel_in']:.1f}" if st.get("fuel_in") is not None else "",
                             f"{st['fuel_out']:.1f}" if st.get("fuel_out") is not None else "",
                             f"{st['cond_in']:.3f}", f"{st['cond_out']:.3f}", f"{st['dmg_in']:.3f}",
                             f"{st['dmg_out']:.3f}", st["why"]])
        en, ex = self.median(self.entry_points), self.median(self.exit_points)
        self.log(f"TRACK: length ~{self.track_len:.0f} m, pit road entry ~{en if en is None else round(en)} m "
                 f"({len(self.entry_points)} seen), pit exit ~{ex if ex is None else round(ex)} m "
                 f"({len(self.exit_points)} seen)")
        for flagtxt in sorted({h[0] for h in self.holds}):
            ts = [t for fl, t in self.holds if fl == flagtxt]
            self.log(f"HELD IN BOX after service under {flagtxt}: {len(ts)} time(s), median {self.median(ts):.1f} s, "
                     f"longest {max(ts):.1f} s")
        with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["car", "slot", "stop", "lap", "flag", "service_s", "fuel_in_pct", "fuel_out_pct",
                        "cond_in", "cond_out", "dmg_in", "dmg_out", "before_it"])
            w.writerows(rows)
        self.log(f"stops CSV: {self.csv_path}")

    def run(self, seconds: float | None = None):
        self.log(f"pit probe started (read-only) on {self.exe_name}, {len(self.modes)} AI modes known; log {self.path}")
        self.scan_globals()
        self.workspace(first=True)
        t_ws = time.time()
        t_end = time.time() + seconds if seconds else None
        while t_end is None or time.time() < t_end:
            try:
                self.processes()
                self.scan_globals()
                if time.time() - t_ws > 2:
                    self.workspace()
                    t_ws = time.time()
                self.scan_gate()
                self.scan_cars()
                time.sleep(INTERVAL)
            except pymem.exception.MemoryReadError:
                self.log("read failed (session change or the game closing); retrying")
                time.sleep(1)
            except KeyboardInterrupt:
                break
            except pymem.exception.ProcessError:
                self.log("NR2003 closed")
                break
        self.summary()


def main():
    seconds = None
    if "--seconds" in sys.argv:
        seconds = float(sys.argv[sys.argv.index("--seconds") + 1])
    Probe().run(seconds)


if __name__ == "__main__":
    sys.exit(main())
