"""Read-only watcher: why NR2003 black-flags AI cars into the pits while AIMOD runs ("phantom penalties").

What the exe shows (2026-10-28):
  - The "penalty stop" flag (car +8119) is set in only two ways: a penalty rule in state 8/9 (0x40B6C7), or the
    car entering a tow / limp mode (BeingTowed 0x406240, CallingTow 0x4063B0, LimpingToPits 0x406490). Limping
    also sets the pit request (+8118) and cuts the driver to 70 % pace.
  - The green-flag mode switch (0x40A4D0) sends a car to LimpingToPits when its repair need (+0x1B33) is > 0, at
    0x40A7ED / 0x40A84B / 0x40A8CB - sites AIMOD's "no game repair stops" patch does not cover.
  - The repair need is rated by 0x52FAD0 from the car's damage block (car +0x1327), two failure bits, and two
    car values (+0x1D52 tire condition total, +0x1D56).
Live race logs: RaceControl saw no rule in any state when the flag rose, and several flagged cars had no damage.

This logs, for every AI car, the moment its repair need, AI mode, +8118 or +8119 changes, with every input the
game's rating reads, so one race shows which one trips. It never writes to the game.

Run (admin terminal, NR2003 running, during a race):
    python tools\\limp_probe.py
Log: AIMOD\\app\\logs\\limp_probe_<date>_<time>.txt  (Ctrl+C to stop)
"""
from __future__ import annotations

import datetime
import os
import struct
import sys
import time

import pymem

CAR0, STRIDE, SLOTS = 0xACCA30, 8724, 44
PLAYER, PACE = 0, 1
OFF_MODE, OFF_REQ, OFF_PEN, OFF_REPAIR = 0x13A7, 8118, 8119, 0x1B33
OFF_DMG, DMG_LEN = 0x1327, 0x64
OFF_TIRESUM, OFF_1D56 = 0x1D52, 0x1D56
OFF_WEAR = 0x1CAE                     # 4 floats LF RF LR RR (AIMOD OFF_WEAR_LF)
PENALTY_MGR = 0xBDB9D8
MODES = {}                            # mode object address -> name, read from the game at start
INTERVAL = 0.02                       # s between scans


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


def rules_owed(pm, slot: int) -> list[str]:
    mgr = pm.read_uint(PENALTY_MGR)
    arr, cnt = pm.read_uint(mgr + 4), pm.read_int(mgr + 8)
    owed = []
    for k in range(max(0, min(cnt, 64))):
        r = pm.read_uint(arr + 4 * k)
        if r:
            v = pm.read_uint(r + 4 + 4 * slot)
            if v & 0xF:
                owed.append(f"rule{k}={v & 0xF}(0x{v:X})")
    return owed


def snapshot(pm, slot: int) -> dict:
    b = CAR0 + slot * STRIDE
    dmg = pm.read_bytes(b + OFF_DMG, DMG_LEN)
    w0, w2 = struct.unpack_from("<HH", dmg, 0)
    flags = struct.unpack_from("<I", dmg, 0xC)[0]
    floats = {f"d{o:02X}": round(struct.unpack_from("<f", dmg, o)[0], 4) for o in range(0x3C, 0x64, 4)}
    return {
        "num": pm.read_ushort(0xAD207A - 2 * STRIDE + slot * STRIDE),
        "mode": MODES.get(pm.read_uint(b + OFF_MODE), hex(pm.read_uint(b + OFF_MODE))),
        "req": pm.read_uchar(b + OFF_REQ), "pen": pm.read_uchar(b + OFF_PEN),
        "repair": pm.read_int(b + OFF_REPAIR),
        "levels": [(w0 >> 12) & 3, (w0 >> 10) & 3, (w0 >> 8) & 3, (w0 >> 6) & 3,
                   (w2 >> 6) & 3, (w2 >> 4) & 3, (w2 >> 2) & 3, w2 & 3],
        "fail_bits": f"{(flags >> 29) & 1}{(flags >> 30) & 1}",
        "tiresum": round(struct.unpack("<f", pm.read_bytes(b + OFF_TIRESUM, 4))[0], 6),
        "v1D56": round(struct.unpack("<f", pm.read_bytes(b + OFF_1D56, 4))[0], 4),
        "wear": [round(x, 3) for x in struct.unpack("<4f", pm.read_bytes(b + OFF_WEAR, 16))],
        **floats,
    }


def main():
    global MODES
    pm = pymem.Pymem("NR2003.exe")
    MODES = mode_names(pm)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, "app", "logs", f"limp_probe_{datetime.datetime.now():%Y%m%d_%H%M%S}.txt")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    out = open(path, "a", encoding="utf-8")

    def log(msg):
        line = f"{datetime.datetime.now():%H:%M:%S.%f}"[:-3] + " " + msg
        print(line)
        out.write(line + "\n")
        out.flush()

    log(f"limp probe started, {len(MODES)} AI modes known; logging to {path}")
    last: dict[int, tuple] = {}
    peak: dict[int, int] = {}
    while True:
        try:
            for s in range(SLOTS):
                if s in (PLAYER, PACE):
                    continue
                b = CAR0 + s * STRIDE
                key = (pm.read_uint(b + OFF_MODE), pm.read_uchar(b + OFF_REQ), pm.read_uchar(b + OFF_PEN),
                       pm.read_int(b + OFF_REPAIR))
                prev = last.get(s)
                last[s] = key
                if prev is None or key == prev:
                    continue
                snap = snapshot(pm, s)
                what = []
                if key[0] != prev[0]:
                    what.append(f"mode {MODES.get(prev[0], hex(prev[0]))} -> {snap['mode']}")
                if key[3] != prev[3]:
                    what.append(f"repair need {prev[3]} -> {key[3]}")
                if key[2] != prev[2]:
                    what.append(f"penalty-stop flag {prev[2]} -> {key[2]}")
                if key[1] != prev[1]:
                    what.append(f"pit request {prev[1]} -> {key[1]}")
                owed = rules_owed(pm, s) if key[2] and not prev[2] else []
                if key[3] > peak.get(s, 0) or key[2] != prev[2] or key[0] != prev[0]:
                    peak[s] = max(peak.get(s, 0), key[3])
                    log(f"#{snap['num']} slot {s}: {'; '.join(what)}"
                        + (f" | rules owed: {', '.join(owed)}" if key[2] and not prev[2] else "")
                        + f" | {snap}")
                elif "pit request" in " ".join(what):
                    log(f"#{snap['num']} slot {s}: {'; '.join(what)} | mode {snap['mode']}")
                if key[3] == 0:
                    peak[s] = 0
        except pymem.exception.MemoryReadError:
            log("read failed (session change or game closing); retrying")
            time.sleep(1)
        except KeyboardInterrupt:
            log("stopped")
            return
        time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
