"""Test: does setting the crew's repair flag make a CAUTION pit stop repair the car?

NR2003's own AI pit debug line ("AIPIT IN %d: %d, %s%s%s ...") prints tires, fuel and a 'D' taken from the byte at
car + 0x73D. In the logs, caution stops never repaired anything; the stops that did were the game's own damage stops.
This tool writes ONLY that byte (car + 0x73D = 1) for an AI car that, under a caution flag,
  - is heading for pit road (pit request +8118 set) or already on pit road, and
  - has damage (front+rear+left+right >= MIN_DAMAGE) or a "car condition" (+4943) of 0.5 or more.
It changes nothing else: not the pit request, the tires, the fuel or any code. Run pit_probe.py beside it: each stop
it touched should come out "REPAIRED".

Run (NR2003 running, race under way):
    python tools\\repair_flag_test.py
Log: AIMOD\\app\\logs\\repair_flag_test_<date>_<time>.txt   (Ctrl+C to stop)
"""
from __future__ import annotations

import datetime
import os
import struct
import sys
import time

import pymem

CAR0, STRIDE, SLOTS = 0xACCA30, 8724, 44
OFF_LAPS, OFF_REQ, OFF_DMG, OFF_COND = 1360, 8118, 4903, 4943
OFF_REPAIR_FLAG = 0x73D
ON_PIT_ROAD = 0x76B230 + 0x8DC
FLAG, SESSION = 0x76F184, 0x977A00
DRIVERS = 0xE07B54
MIN_DAMAGE = 0.25


def main():
    pm = None
    for name in ("NR2003.exe", "NR2003_clean.exe"):
        try:
            pm = pymem.Pymem(name)
            break
        except Exception:
            continue
    if pm is None:
        raise SystemExit("NR2003 is not running")
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, "app", "logs", f"repair_flag_test_{datetime.datetime.now():%Y%m%d_%H%M%S}.txt")
    out = open(path, "a", encoding="utf-8")

    def log(msg):
        line = f"{datetime.datetime.now():%H:%M:%S.%f}"[:-3] + " " + msg
        print(line)
        out.write(line + "\n")
        out.flush()

    log(f"repair flag test on {name}: sets car+0x73D=1 for damaged AI cars pitting under caution; log {path}")
    touched: dict[int, float] = {}
    while True:
        try:
            flag = pm.read_int(FLAG)
            if pm.read_bytes(SESSION, 1)[0] != 7 or flag == 7:
                time.sleep(0.05)
                continue
            allb = pm.read_bytes(CAR0, SLOTS * STRIDE)
            road = pm.read_bytes(ON_PIT_ROAD, SLOTS)
            for s in range(2, SLOTS):
                blk = allb[s * STRIDE:(s + 1) * STRIDE]
                if struct.unpack_from("<i", blk, OFF_LAPS)[0] <= 0:
                    continue
                f = lambda o: struct.unpack_from("<f", blk, o)[0]
                dmg = f(OFF_DMG + 16) + f(OFF_DMG + 20) + f(OFF_DMG + 48) + f(OFF_DMG + 52)
                cond = f(OFF_COND)
                heading = blk[OFF_REQ] or road[s]
                if heading and (dmg >= MIN_DAMAGE or cond >= 0.5) and not blk[OFF_REPAIR_FLAG]:
                    pm.write_bytes(CAR0 + s * STRIDE + OFF_REPAIR_FLAG, b"\x01", 1)
                    num = pm.read_int(DRIVERS + s * 175 + 156)
                    first = s not in touched or time.time() - touched[s] > 60
                    touched[s] = time.time()
                    if first:
                        log(f"#{num} (slot {s}): repair flag set - flag {flag}, on pit road {road[s]}, request "
                            f"{blk[OFF_REQ]}, damage {dmg:.3f}, condition {cond:.3f}, tires {blk[1777]}{blk[1778]} "
                            f"fuel {blk[1781]}")
        except pymem.exception.MemoryReadError:
            time.sleep(1)
        except KeyboardInterrupt:
            log("stopped")
            return
        time.sleep(0.05)


if __name__ == "__main__":
    sys.exit(main())
