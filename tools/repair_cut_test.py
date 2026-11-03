"""Test: can the crew be stopped part way through a damage repair, and how does a repair progress?

Under a caution, for AI cars being serviced in their box with the crew's repair flag on (car +0x73D):
  - every car: the damage (front+rear+left+right) and "car condition" are logged every 0.5 s of the service, so the
    log shows whether a repair takes the damage down gradually or in one step at the end;
  - every OTHER damaged car (alternating): the repair flag is switched off part way through (CUT_AFTER_DROP s after the
    damage first starts to fall, or at FALLBACK_CUT_S of service if it never falls before then), to see whether the
    car then leaves with a partial repair. The others are left to finish, for comparison.
It writes only that one byte, only for those cars, only once per stop. Run pit_probe.py beside it, with AIMOD new17
(its caution_repairs patch lets caution stops repair).

Run (NR2003 running, race under way):
    python tools\\repair_cut_test.py
Log: AIMOD\\app\\logs\\repair_cut_test_<date>_<time>.txt   (Ctrl+C to stop)
"""
from __future__ import annotations

import datetime
import os
import struct
import sys
import time

import pymem

CAR0, STRIDE, SLOTS = 0xACCA30, 8724, 44
OFF_LAPS, OFF_VEL, OFF_TIMER, OFF_DMG, OFF_COND = 1360, 6634, 1734, 4903, 4943
OFF_REPAIR_FLAG = 0x73D
ON_PIT_ROAD = 0x76B230 + 0x8DC
FLAG, SESSION = 0x76F184, 0x977A00
DRIVERS = 0xE07B54
MIN_DAMAGE = 0.2
CUT_AFTER_DROP = 1.0      # s after the damage starts to fall
FALLBACK_CUT_S = 10.0     # s of service, if it hasn't started to fall by then
SAMPLE_S = 0.5


def main():
    pm = name = None
    for name in ("NR2003.exe", "NR2003_clean.exe"):
        try:
            pm = pymem.Pymem(name)
            break
        except Exception:
            continue
    if pm is None:
        raise SystemExit("NR2003 is not running")
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, "app", "logs", f"repair_cut_test_{datetime.datetime.now():%Y%m%d_%H%M%S}.txt")
    out = open(path, "a", encoding="utf-8")

    def log(msg):
        line = f"{datetime.datetime.now():%H:%M:%S.%f}"[:-3] + " " + msg
        print(line)
        out.write(line + "\n")
        out.flush()

    log(f"repair cut test on {name}; log {path}")
    stops: dict[int, dict] = {}       # slot -> state of the stop being watched
    alternate = 0
    while True:
        try:
            if pm.read_bytes(SESSION, 1)[0] != 7:
                time.sleep(0.2)
                continue
            flag = pm.read_int(FLAG)
            allb = pm.read_bytes(CAR0, SLOTS * STRIDE)
            road = pm.read_bytes(ON_PIT_ROAD, SLOTS)
            now = time.time()
            for s in range(2, SLOTS):
                blk = allb[s * STRIDE:(s + 1) * STRIDE]
                if struct.unpack_from("<i", blk, OFF_LAPS)[0] <= 0:
                    continue
                f = lambda o: struct.unpack_from("<f", blk, o)[0]
                dmg = f(OFF_DMG + 16) + f(OFF_DMG + 20) + f(OFF_DMG + 48) + f(OFF_DMG + 52)
                cond = f(OFF_COND)
                vx, vy = struct.unpack_from("<ff", blk, OFF_VEL)
                still = road[s] and (vx * vx + vy * vy) ** 0.5 < 0.6
                timer = f(OFF_TIMER)
                st = stops.get(s)
                if st is None:
                    if (still and flag != 7 and blk[OFF_REPAIR_FLAG] and (dmg >= MIN_DAMAGE or cond >= MIN_DAMAGE)):
                        num = pm.read_int(DRIVERS + s * 175 + 156)
                        cut = alternate % 2 == 0
                        alternate += 1
                        stops[s] = st = {"num": num, "t0": now, "dmg0": dmg, "cond0": cond, "cut": cut,
                                         "drop_t": None, "cut_done": False, "last_sample": 0.0, "timer0": timer}
                        log(f"#{num}: in its box under caution (flag {flag}) with the repair flag on, damage "
                            f"{dmg:.3f} condition {cond:.3f} -> {'CUT the repair part way' if cut else 'let it finish'}")
                    continue
                el = now - st["t0"]
                if not road[s]:
                    log(f"#{st['num']}: left pit road {el:.1f} s after stopping - damage {st['dmg0']:.3f} -> {dmg:.3f}, "
                        f"condition {st['cond0']:.3f} -> {cond:.3f} ({'cut' if st['cut'] else 'full'}"
                        f"{', cut sent' if st['cut_done'] else ''})")
                    del stops[s]
                    continue
                if st["drop_t"] is None and dmg < st["dmg0"] - 0.005:
                    st["drop_t"] = now
                    log(f"#{st['num']}: damage starts to fall {el:.1f} s into the stop (timer {timer:.1f})")
                if now - st["last_sample"] >= SAMPLE_S:
                    st["last_sample"] = now
                    log(f"#{st['num']}:   t+{el:4.1f} s  timer {timer:5.1f}  damage {dmg:.3f}  condition {cond:.3f}  "
                        f"repair flag {blk[OFF_REPAIR_FLAG]}  still {int(bool(still))}")
                if st["cut"] and not st["cut_done"] and blk[OFF_REPAIR_FLAG] and (
                        (st["drop_t"] is not None and now - st["drop_t"] >= CUT_AFTER_DROP) or el >= FALLBACK_CUT_S):
                    pm.write_bytes(CAR0 + s * STRIDE + OFF_REPAIR_FLAG, b"\x00", 1)
                    st["cut_done"] = True
                    log(f"#{st['num']}: REPAIR FLAG SWITCHED OFF at t+{el:.1f} s (damage now {dmg:.3f})")
        except pymem.exception.MemoryReadError:
            time.sleep(1)
        except KeyboardInterrupt:
            log("stopped")
            return
        time.sleep(0.05)


if __name__ == "__main__":
    sys.exit(main())
