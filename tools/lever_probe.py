# Per-car pace lever probe (research, not part of AIMOD).
#
# NR2003 keeps each AI driver's own multipliers in the driver object at car + 0x16BE, built by 0x4F6E90 from the
# driver/car ratings: +0x12C aero drag, +0x130 engine power, +0x134 traction circle (cornering grip), +0x138 chassis
# grip. They are rebuilt at setup and by ASC (adaptive speed control, every update_interval ticks, only while
# [0xBDAA28] != 0). This script checks that live:
#
#   python lever_probe.py                 read-only: table of every car's values + ASC state, then watches 60 s and
#                                         reports every time the game rewrites them (nothing is written)
#   python lever_probe.py test SLOT [--field traction|power|drag|chassis|wear|fuel] [--scale 0.97] [--laps 3]
#                                         baseline laps -> holds that one car's value at original x scale -> restores
#                                         and runs recovery laps; compares its lap times against the field median
#                                         wear / fuel: during the test laps only `scale` of each increase in that
#                                         car's tire wear (or fuel burned) is kept, e.g. 0.8 = 20% less wear
#
# Run from an administrator prompt (NR2003 runs elevated). The pace tests write ONE float of ONE AI car and always
# write the game's own value back when they end (Ctrl+C included). The wear / fuel tests leave the saved wear or
# fuel with the car (that is the point), and stop writing when the test laps end. Never touches slot 0 (you) or 1 (pace car).
# Log: %TEMP%\lever_probe_<time>.txt
import ctypes, struct, subprocess, time, sys, os, statistics, argparse

k = ctypes.windll.kernel32

CAR_BLOCK_0 = 0xACCA30
CAR_STRIDE = 8724
SLOTS = range(2, 48)                # NR2003 has 48 car slots (you, pace car, up to 46 AI)
DRV = 0x16BE                        # driver object inside the car block
FIELDS = {"drag": 0x12C, "power": 0x130, "traction": 0x134, "chassis": 0x138}
OFF_LAPS_DONE = 1360
OFF_REASON_OUT = 4916
OFF_PITTING = 8118
OFF_WEAR = (7342, 7346, 7350, 7354)   # LF RF LR RR floats, grow as the tire wears
OFF_FUEL_A, OFF_FUEL_B = 0x518, 0x520  # fuel = double, block [A] XOR block [B] (game units)
OFF_GRIP = 7418                        # float, the car's current grip (0.01..1.05), read only here
RESOURCES = ("wear", "fuel")
DRIVER_ENTRY_0 = 14711636
DRIVER_ENTRY_SIZE = 175

FLAG_STATE = 0x76F184               # 7 = green
SESSION_STATE = 0x977A00            # 7 = race
GAME_TICKS = 0xBEBDF5
TICK_RATE_FLAG = 0x755A18
TICK_RATE_VALUE = 0x7559B4
ASC_ON = 0xBDAA28                   # byte
ASC_COUNTER = 0xBDAA2C              # int, ticks since the last ASC update
ASC_STRENGTH = 0x748520             # float, global AI strength ASC steers
ASC_INTERVAL = 0x74B650             # float, [ASC] update_interval

# Same build as the static analysis: parameter table entry for ai_fuel_use, and the store of the traction circle.
CHECKS = [(0x74B0C0, struct.pack("<I", 0x749FF4)), (0x749FF4, b"ai_fuel_use\0"),
          (0x4F6FDD, bytes([0xD9, 0x99, 0x34, 0x01, 0x00, 0x00]))]

LOG = None


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    LOG.write(s + "\n")
    LOG.flush()


def attach(write):
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq NR2003.exe", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True).stdout
    if "NR2003" not in out:
        sys.exit("NR2003.exe is not running.")
    pid = int(out.split('","')[1])
    access = 0x0010 | 0x0400 | ((0x0020 | 0x0008) if write else 0)
    h = k.OpenProcess(access, False, pid)
    if not h:
        sys.exit("Could not open NR2003.exe - run this from an administrator prompt.")
    return h


def rd(a, n):
    b = ctypes.create_string_buffer(n)
    r = ctypes.c_size_t()
    if not k.ReadProcessMemory(H, ctypes.c_void_p(a), b, n, ctypes.byref(r)) or r.value != n:
        raise OSError(f"read failed at {a:#x}")
    return b.raw


def wr_f32(a, v):
    b = struct.pack("<f", v)
    r = ctypes.c_size_t()
    if not k.WriteProcessMemory(H, ctypes.c_void_p(a), b, 4, ctypes.byref(r)) or r.value != 4:
        raise OSError(f"write failed at {a:#x}")


u8 = lambda a: rd(a, 1)[0]
i32 = lambda a: struct.unpack("<i", rd(a, 4))[0]
f32 = lambda a: struct.unpack("<f", rd(a, 4))[0]


def car(slot):
    return CAR_BLOCK_0 + slot * CAR_STRIDE


def tick_rate():
    return 36 if i32(TICK_RATE_FLAG) != 0 else max(1, i32(TICK_RATE_VALUE))


def driver(slot):
    try:
        buf = rd(DRIVER_ENTRY_0 + slot * DRIVER_ENTRY_SIZE, DRIVER_ENTRY_SIZE)
    except OSError:
        return "?", "?"
    num = struct.unpack_from("<i", buf, 156)[0]
    cs = lambda o, n: buf[o:o + n].split(b"\0")[0].decode("latin1", "replace")
    return str(num % 1000 if num >= 1000 else num), (cs(0, 16) + " " + cs(16, 20)).strip()


def wear(slot):
    return sum(f32(car(slot) + o) for o in OFF_WEAR)


def fuel(slot):
    try:
        pa, pb = (struct.unpack("<I", rd(car(slot) + o, 4))[0] for o in (OFF_FUEL_A, OFF_FUEL_B))
        v = struct.unpack("<d", bytes(x ^ y for x, y in zip(rd(pa, 8), rd(pb, 8))))[0]
    except OSError:
        return None
    return v if v == v and 0 <= v < 10000 else None


def fuel_ptrs(slot):
    return tuple(struct.unpack("<I", rd(car(slot) + o, 4))[0] for o in (OFF_FUEL_A, OFF_FUEL_B))


def wr_fuel(slot, v):
    pa, pb = fuel_ptrs(slot)
    a = bytes(x ^ y for x, y in zip(struct.pack("<d", v), rd(pb, 8)))
    r = ctypes.c_size_t()
    if not k.WriteProcessMemory(H, ctypes.c_void_p(pa), a, 8, ctypes.byref(r)) or r.value != 8:
        raise OSError(f"write failed at {pa:#x}")


class ResourceHold:
    """Keeps only `scale` of every increase in one car's tire wear (each tire) or fuel burned."""

    def __init__(self, slot, kind, scale):
        self.slot, self.kind, self.scale = slot, kind, scale
        self.writes = 0
        self.last = self.read()

    def read(self):
        if self.kind == "wear":
            return [f32(car(self.slot) + o) for o in OFF_WEAR]
        return fuel(self.slot)

    def step(self):
        cur = self.read()
        if self.kind == "wear":
            for i, o in enumerate(OFF_WEAR):
                if cur[i] > self.last[i]:
                    new = self.last[i] + (cur[i] - self.last[i]) * self.scale
                    wr_f32(car(self.slot) + o, new)
                    self.last[i] = new
                    self.writes += 1
                else:
                    self.last[i] = cur[i]          # new tires (or no wear this tick)
        else:
            if cur is None or self.last is None:
                self.last = cur
            elif cur < self.last:
                new = self.last - (self.last - cur) * self.scale
                wr_fuel(self.slot, new)
                self.last = new
                self.writes += 1
            else:
                self.last = cur                    # refuelled


def values(slot):
    base = car(slot) + DRV
    return {name: f32(base + off) for name, off in FIELDS.items()}


def running(slot):
    try:
        v = values(slot)
        return u8(car(slot) + OFF_REASON_OUT) == 0 and all(0.2 < x < 5 for x in v.values())
    except OSError:
        return False


def check_build():
    for addr, want in CHECKS:
        if rd(addr, len(want)) != want:
            sys.exit(f"Unknown NR2003.exe build (bytes at {addr:#x} differ) - stopping, nothing written.")


def status_line():
    return (f"session {u8(SESSION_STATE)}  flag {i32(FLAG_STATE)}  ASC {'ON' if u8(ASC_ON) else 'off'}  "
            f"strength {f32(ASC_STRENGTH):.4f}  interval {f32(ASC_INTERVAL):.0f} ticks  "
            f"tick rate {tick_rate()}/s  game tick {i32(GAME_TICKS)}")


# ---------------------------------------------------------------- survey (read-only)
def survey(watch_s):
    log(status_line())
    log(f"{'slot':>4} {'#':>4}  {'driver':<24} {'drag':>8} {'power':>8} {'traction':>9} {'chassis':>8}")
    cars = [s for s in SLOTS if running(s)]
    snap = {}
    for s in cars:
        num, name = driver(s)
        v = snap[s] = values(s)
        log(f"{s:>4} {num:>4}  {name[:24]:<24} {v['drag']:8.4f} {v['power']:8.4f} {v['traction']:9.4f} {v['chassis']:8.4f}")
    for f in FIELDS:
        xs = [snap[s][f] for s in cars]
        if xs:
            log(f"  {f:<9} min {min(xs):.4f}  max {max(xs):.4f}  spread {max(xs) - min(xs):.4f}")
    log(f"\nWatching {watch_s} s for the game rewriting these values (nothing is written)...")
    t_end = time.time() + watch_s
    rewrites = []
    while time.time() < t_end:
        tick = i32(GAME_TICKS)
        changed = 0
        for s in cars:
            v = values(s)
            if any(abs(v[f] - snap[s][f]) > 1e-7 for f in FIELDS):
                changed += 1
                snap[s] = v
        if changed:
            rewrites.append(tick)
            log(f"  tick {tick}: {changed} cars' values changed   ({status_line()})")
        time.sleep(0.005)
    if len(rewrites) > 1:
        gaps = [b - a for a, b in zip(rewrites, rewrites[1:])]
        log(f"Rewrites: {len(rewrites)}, ticks between them: median {statistics.median(gaps):.0f}")
    elif rewrites:
        log("Rewrites: 1 (not enough to measure an interval)")
    else:
        log("No rewrites seen - writes to these values should stick while nothing reloads the session.")


# ---------------------------------------------------------------- test (one car, one value)
class Laps:
    """Lap time, tire wear and fuel burn per car per lap (laps-completed increments on the game clock);
    green, no-pit laps only."""

    def __init__(self, cars):
        self.cars = cars
        self.last = {s: (i32(car(s) + OFF_LAPS_DONE), i32(GAME_TICKS)) for s in cars}
        self.clean = {s: False for s in cars}    # the lap in progress started before we watched it
        self.start = {s: (wear(s), fuel(s)) for s in cars}
        self.done = []          # (slot, laps_done, seconds, phase, wear used, fuel used or None, grip)

    def poll(self, phase, green):
        tick = i32(GAME_TICKS)
        new = []
        for s in self.cars:
            try:
                laps = i32(car(s) + OFF_LAPS_DONE)
                if not green or u8(car(s) + OFF_PITTING) or u8(car(s) + OFF_REASON_OUT):
                    self.clean[s] = False
            except OSError:
                continue
            l0, t0 = self.last[s]
            if laps != l0:
                w, fu = wear(s), fuel(s)
                if laps == l0 + 1 and self.clean[s]:
                    secs = (tick - t0) / tick_rate()
                    w0, f0 = self.start[s]
                    burn = f0 - fu if f0 is not None and fu is not None and f0 >= fu else None
                    grip = f32(car(s) + OFF_GRIP)
                    self.done.append((s, laps, secs, phase, w - w0, burn, grip))
                    new.append((s, laps, secs, w - w0, burn, grip))
                self.last[s] = (laps, tick)
                self.start[s] = (w, fu)
                self.clean[s] = green
        return new


def test(slot, field, scale, laps):
    if slot < 2:
        sys.exit("Slot 0 is you and 1 is the pace car - pick an AI slot (run the survey to see them).")
    if not running(slot):
        sys.exit(f"Slot {slot} isn't a running car.")
    resource = field in RESOURCES
    if not (0.5 <= scale <= 1.5 if resource else 0.85 <= scale <= 1.15):
        sys.exit("Keep --scale between 0.5 and 1.5 for wear/fuel, 0.85 and 1.15 for the pace values.")
    addr = car(slot) + DRV + FIELDS.get(field, FIELDS["traction"])   # pace tests only
    hold = None
    num, name = driver(slot)
    log(status_line())
    log(f"Test: slot {slot} #{num} {name}  {field} x{scale}  ({laps} laps baseline / test / recovery)")
    if u8(SESSION_STATE) != 7:
        log("Note: not in a race session (ASC only runs in the race).")
    cars = [s for s in SLOTS if running(s)]
    lt = Laps(cars)
    phase, phase_laps = "baseline", 0
    natural = f32(addr)          # the game's own value; follows ASC rewrites while we hold ours
    held = None
    reverts = 0
    if not resource:
        log(f"  {field} now {natural:.5f}")
    try:
        while phase != "end":
            green = i32(FLAG_STATE) == 7
            if hold is not None:
                hold.step()
            cur = f32(addr)
            if held is not None and abs(cur - held) > 1e-7:
                # The game rebuilt it (ASC or a reload): take its new value as the original and re-apply ours.
                reverts += 1
                natural = cur
                held = natural * scale
                wr_f32(addr, held)
                log(f"  tick {i32(GAME_TICKS)}: game rewrote {field} to {cur:.5f} -> re-applied {held:.5f} (#{reverts})")
            for s, n, secs, used, burn, grip in lt.poll(phase, green):
                if s == slot:
                    phase_laps += 1
                    log(f"  [{phase}] lap {n}: {secs:.3f} s   wear +{used:.5f}   fuel "
                        + ("?" if burn is None else f"-{burn:.4f}") + f"   grip {grip:.4f}")
                    if phase_laps >= laps:
                        phase_laps = 0
                        if phase == "baseline" and resource:
                            phase = "test"
                            hold = ResourceHold(slot, field, scale)
                            log(f"  -> TEST: keeping x{scale} of each {field} increase")
                        elif phase == "test" and resource:
                            phase = "recovery"
                            log(f"  -> RECOVERY: {field} back to the game ({hold.writes} writes)")
                            hold = None
                        elif phase == "baseline":
                            phase = "test"
                            natural = f32(addr)
                            held = natural * scale
                            wr_f32(addr, held)
                            log(f"  -> TEST: {field} {natural:.5f} -> {held:.5f}")
                        elif phase == "test":
                            phase = "recovery"
                            wr_f32(addr, natural)
                            held = None
                            log(f"  -> RECOVERY: {field} restored to {natural:.5f}")
                        else:
                            phase = "end"
            time.sleep(0.005)
    finally:
        if held is not None:
            wr_f32(addr, natural)
            log(f"  restored {field} to {natural:.5f}")
    log(f"\nGame rewrites while held: {reverts}")
    log(f"{'phase':<9} {'car med':>9} {'field med':>10} {'car-field':>10}  laps (car / field)")
    rows = {}
    for ph in ("baseline", "test", "recovery"):
        mine = [x[2] for x in lt.done if x[3] == ph and x[0] == slot]
        rest = [x[2] for x in lt.done if x[3] == ph and x[0] != slot]
        if mine and rest:
            rows[ph] = statistics.median(mine) - statistics.median(rest)
            log(f"{ph:<9} {statistics.median(mine):9.3f} {statistics.median(rest):10.3f} {rows[ph]:+10.3f}  "
                f"{len(mine)} / {len(rest)}")
    if "baseline" in rows and "test" in rows:
        log(f"\nEffect of {field} x{scale}: {rows['test'] - rows['baseline']:+.3f} s per lap vs the field "
            f"(positive = slower). Recovery: {rows.get('recovery', float('nan')) - rows['baseline']:+.3f} s.")
    # Wear and fuel per lap as a ratio to the field's median, so track/tire state cancels out.
    for ph in ("baseline", "test", "recovery"):
        mine = [x for x in lt.done if x[3] == ph and x[0] == slot]
        if mine:
            last_lap = mine[-1][1]
            rest = [x[6] for x in lt.done if x[1] == last_lap and x[0] != slot]
            if rest:
                log(f"grip at the end of {ph:<9} car {mine[-1][6]:.4f}  field median {statistics.median(rest):.4f}")
    for label, col in (("tire wear", 4), ("fuel burn", 5)):
        ratio = {}
        for ph in ("baseline", "test", "recovery"):
            mine = [x[col] for x in lt.done if x[3] == ph and x[0] == slot and x[col] is not None]
            rest = [x[col] for x in lt.done if x[3] == ph and x[0] != slot and x[col] is not None]
            if mine and rest and statistics.median(rest) > 0:
                ratio[ph] = statistics.median(mine) / statistics.median(rest)
        if "baseline" in ratio and "test" in ratio:
            log(f"{label:<9} per lap vs field: baseline x{ratio['baseline']:.3f}  test x{ratio['test']:.3f}  "
                f"recovery x{ratio.get('recovery', float('nan')):.3f}  -> test changed it "
                f"{(ratio['test'] / ratio['baseline'] - 1) * 100:+.1f}%")
        else:
            log(f"{label:<9} per lap: not enough readings")


def main():
    global H, LOG
    ap = argparse.ArgumentParser(description="NR2003 per-car pace lever probe")
    ap.add_argument("mode", nargs="?", default="survey", choices=["survey", "test"])
    ap.add_argument("slot", nargs="?", type=int)
    ap.add_argument("--field", default="traction", choices=list(FIELDS) + list(RESOURCES))
    ap.add_argument("--scale", type=float, default=0.97)
    ap.add_argument("--laps", type=int, default=3)
    ap.add_argument("--watch", type=int, default=60, help="survey: seconds to watch for rewrites")
    a = ap.parse_args()
    if a.mode == "test" and a.slot is None:
        ap.error("test needs a SLOT (run the survey first to see the slots)")
    path = os.path.join(os.environ.get("TEMP", "."), time.strftime("lever_probe_%Y%m%d_%H%M%S.txt"))
    LOG = open(path, "w", encoding="utf-8")
    H = attach(write=a.mode == "test")
    check_build()
    try:
        if a.mode == "survey":
            survey(a.watch)
        else:
            test(a.slot, a.field, a.scale, a.laps)
    except KeyboardInterrupt:
        log("Stopped.")
    finally:
        log(f"\nLog: {path}")
        LOG.close()


if __name__ == "__main__":
    main()
