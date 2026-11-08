"""Which drivers lean differently now that temperament comes from the .car aggression (2026-11-05) instead of a
hash of the driver's name.

    python tools\\temper_diff.py --mod "Nextel Cup 2007" [--log app\\logs\\aggression_probe_<...>.txt] [--all]

--log: an aggression_probe log; only the cars of that session (its table) are used, as AIMOD would see the field.
Without it, every car number in the mod's cars folder (the first file per number).
Lists each driver whose side of a temperament threshold changed:
  short-pit lean   strategy leans to short-pitting at temper >= 0.65 (Driver behavior on, not drafting)
  attacks run      a short-pit run is attacked at temper >= 0.30, else the tires are managed
  stretch manages  a stretch run is managed below 0.90, else run out
  rides (SS)       drafting tracks: rides and saves fuel below 0.88
  grudges          holds a grudge after contact at >= 0.30 (rivals, new)
plus the change in the tire-manager score (0.4 x temper: + = manages more) and in the caution stay-out chance.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from aimod import aggression as ag          # noqa: E402
from aimod import carfiles                   # noqa: E402
from aimod.carno import label, raw           # noqa: E402

NR2003 = r"C:\Papyrus\NASCAR Racing 2003 Season"
THRESHOLDS = (("short-pit lean", 0.65, ">="), ("attacks run", 0.30, ">="), ("stretch manages", 0.90, "<"),
              ("rides (SS)", 0.88, "<"), ("grudges", ag.GRUDGE_TEMPER, ">="))
_ROW = re.compile(r"^\S+\s+\d+\s+#(\S+)\s+")


def side(t: float, cut: float, op: str) -> bool:
    return t >= cut if op == ">=" else t < cut


def session_numbers(log_path: str) -> list[int]:
    out = []
    with open(log_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if "|" not in line:
                continue
            m = _ROW.match(line)
            if m:
                try:
                    out.append(int(raw(m.group(1))))
                except ValueError:
                    pass
    return list(dict.fromkeys(out))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mod", required=True)
    ap.add_argument("--log", help="aggression_probe log: use that session's cars")
    ap.add_argument("--nr2003", default=NR2003)
    ap.add_argument("--all", action="store_true", help="list every driver, not only the changed ones")
    a = ap.parse_args(argv)
    folder = carfiles.cars_folder(a.nr2003, a.mod)
    infos = {}
    for fn in sorted(os.listdir(folder)):
        if fn.lower().endswith(".car"):
            info = carfiles.car_info(os.path.join(folder, fn))
            if info and int(info["number"]) not in infos:
                infos[int(info["number"])] = info
    if a.log:
        want = session_numbers(a.log)
        infos = {n: infos[n] for n in want if n in infos}
        missing = [n for n in want if n not in infos]
        if missing:
            print("not found in the cars folder:", ", ".join(label(n) for n in missing))
    vals = {n: i.get("driver_aggression") for n, i in infos.items()}
    names = {n: i.get("name", "") for n, i in infos.items()}
    new = ag.temperaments(vals, names)
    rows, changed = [], 0
    for n in sorted(infos, key=lambda k: (vals[k] if vals[k] is not None else -1, k)):
        old = ag.temper_from_name(names[n])
        flips = [name for name, cut, op in THRESHOLDS if side(old, cut, op) != side(new[n], cut, op)]
        if flips:
            changed += 1
        if flips or a.all:
            d = new[n] - old
            rows.append(f"#{label(n):<4} {names[n][:22]:<22} agg {_f(vals[n])}  temper {old:.2f} -> {new[n]:.2f} "
                        f"({d:+.2f}; manager score {-0.4 * d:+.2f}, caution stay-out {-0.25 * d:+.0%})  "
                        + (", ".join(f"{x} {'gains' if side(new[n], *_cut(x)) else 'loses'}" for x in flips)
                           or "-"))
    print(f"{a.mod}: {len(infos)} cars, {changed} change side on at least one threshold"
          + (f" (session from {os.path.basename(a.log)})" if a.log else ""))
    for r in rows:
        print("  " + r)


def _cut(name):
    for n, cut, op in THRESHOLDS:
        if n == name:
            return cut, op


def _f(v):
    return f"{v:.2f}" if v is not None else "  - "


if __name__ == "__main__":
    main()
