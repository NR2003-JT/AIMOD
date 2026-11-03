"""Driver behavior in game memory: puts each car's behavior Effect (see behavior.py) into NR2003.

  pace      the driver's own traction (cornering grip) and engine power multipliers, scaled from the value the
            game built. The game rebuilds them when the car starts a new lap; the rebuilt value is taken as the
            new base and the scale put back on it. Changes ramp in over about half a second.
  wear      of each rise in a tire's wear since the last call, only `wear` x the rise is kept (0.75 = 25 % less
            wear, 1.25 = 25 % more). New tires (wear drops) start over.
  fuel      the same for fuel burnt (block A XOR block B, see memory_map). Refuelling starts over.

Wear and fuel are only touched while the car races on track. Never the player's car or the pace car. restore()
writes the game's own pace values back (the wear / fuel saved or spent stays: that is the point).
"""
from __future__ import annotations

import logging
import struct

from . import memory_map as mm
from .behavior import NEUTRAL, Effect

log = logging.getLogger("aimod.behavior")

RAMP = 0.004                 # scale change per call (150 ms): 1.5 % in about half a second
WEAR_OFFS = tuple(mm.OFF_WEAR_LF + 4 * k for k in range(4))     # LF RF LR RR
PACE = (("traction", mm.DRV_TRACTION), ("power", mm.DRV_POWER))


def _f32(v: float) -> float:
    return struct.unpack("<f", struct.pack("<f", v))[0]


class BehaviorWriter:
    def __init__(self):
        self._checked: tuple | None = None       # (pm id, result) of the exe check
        self.reset()

    def reset(self):
        self.nat: dict[tuple, float] = {}         # (idx, field) -> the game's own value
        self.written: dict[tuple, float] = {}     # (idx, field) -> what was last written
        self.scale: dict[tuple, float] = {}       # (idx, field) -> scale in effect (ramping)
        self.wear_last: dict[int, list] = {}
        self.fuel_last: dict[int, float] = {}

    # ---------------------------------------------------------------- checks
    def available(self, pm) -> bool:
        """The driver multipliers are where this NR2003.exe build keeps them (checked once per attach)."""
        if pm is None:
            return False
        if self._checked is not None and self._checked[0] == id(pm):
            return self._checked[1]
        addr, want = mm.DRIVER_MULT_CHECK
        try:
            ok = pm.read_bytes(addr, len(want)) == want
        except Exception:
            ok = False
        if not ok:
            log.warning("Driver behavior off: unknown NR2003.exe build (0x%X)", addr)
        self._checked = (id(pm), ok)
        return ok

    # ----------------------------------------------------------------- write
    def apply(self, pm, effects: dict[int, Effect], on_track: dict[int, bool]) -> None:
        """effects: idx -> Effect for cars with a behavior (others ramp back to the game's values)."""
        idxs = set(effects) | {k[0] for k in self.scale} | set(self.wear_last) | set(self.fuel_last)
        for i in sorted(idxs):
            if i in (mm.PLAYER_SLOT, mm.PACE_SLOT) or not 0 <= i < mm.NUM_CARS:
                continue
            e = effects.get(i, NEUTRAL)
            base = mm.CAR_BLOCK_0 + i * mm.CAR_STRIDE
            try:
                for name, off in PACE:
                    self._pace(pm, i, name, base + mm.OFF_DRIVER + off, getattr(e, name))
                racing = bool(on_track.get(i))
                if racing and e.wear != 1.0:
                    self._wear(pm, i, base, e.wear)
                else:
                    self.wear_last.pop(i, None)
                if racing and e.fuel != 1.0:
                    self._fuel(pm, i, base, e.fuel)
                else:
                    self.fuel_last.pop(i, None)
            except Exception as ex:                 # a car that went away mid-call: forget it
                log.debug("behavior write failed car %d: %s", i, ex)
                self._forget(i)

    def _pace(self, pm, i: int, name: str, addr: int, target: float):
        key = (i, name)
        s = self.scale.get(key, 1.0)
        if s == 1.0 and target == 1.0:
            self.nat.pop(key, None)
            self.written.pop(key, None)
            return
        s = min(target, s + RAMP) if target > s else max(target, s - RAMP)
        cur = struct.unpack("<f", pm.read_bytes(addr, 4))[0]
        if not (0.2 < cur < 5.0):
            return
        w = self.written.get(key)
        if w is None or abs(cur - w) > 1e-6:
            self.nat[key] = cur                       # first look, or the game rebuilt it (new lap)
        val = _f32(self.nat[key] * s)
        if abs(cur - val) > 1e-7:
            pm.write_bytes(addr, struct.pack("<f", val), 4)
        self.written[key] = val
        if s == 1.0 and target == 1.0:
            self.scale.pop(key, None)
            self.nat.pop(key, None)
            self.written.pop(key, None)
        else:
            self.scale[key] = s

    def _wear(self, pm, i: int, base: int, keep: float):
        cur = [struct.unpack("<f", pm.read_bytes(base + o, 4))[0] for o in WEAR_OFFS]
        if not all(v == v and -0.01 <= v < 10 for v in cur):
            self.wear_last.pop(i, None)
            return
        last = self.wear_last.get(i)
        if last is None:
            self.wear_last[i] = cur
            return
        new = list(cur)
        for k, o in enumerate(WEAR_OFFS):
            if cur[k] > last[k]:
                new[k] = _f32(max(0.0, last[k] + (cur[k] - last[k]) * keep))
                pm.write_bytes(base + o, struct.pack("<f", new[k]), 4)
        self.wear_last[i] = new

    def _fuel(self, pm, i: int, base: int, keep: float):
        pa, pb = (struct.unpack("<I", pm.read_bytes(base + o, 4))[0] for o in (mm.OFF_FUEL_A, mm.OFF_FUEL_B))
        a, b = pm.read_bytes(pa, 8), pm.read_bytes(pb, 8)
        fuel = struct.unpack("<d", bytes(x ^ y for x, y in zip(a, b)))[0]
        if not (fuel == fuel and 0.0 <= fuel < 10000.0):
            self.fuel_last.pop(i, None)
            return
        last = self.fuel_last.get(i)
        if last is None or fuel > last + 0.05:        # first look / refuelled
            self.fuel_last[i] = fuel
            return
        if fuel < last:
            new = max(0.0, last - (last - fuel) * keep)
            pm.write_bytes(pa, bytes(x ^ y for x, y in zip(struct.pack("<d", new), b)), 8)
            fuel = new
        self.fuel_last[i] = fuel

    def _forget(self, i: int):
        for d in (self.nat, self.written, self.scale):
            for key in [k for k in d if k[0] == i]:
                del d[key]
        self.wear_last.pop(i, None)
        self.fuel_last.pop(i, None)

    def restore(self, pm) -> int:
        """The game's own pace values back for every car changed. Returns how many were written."""
        n = 0
        if pm is not None:
            for (i, name), v in self.nat.items():
                off = dict(PACE)[name]
                addr = mm.CAR_BLOCK_0 + i * mm.CAR_STRIDE + mm.OFF_DRIVER + off
                try:
                    cur = struct.unpack("<f", pm.read_bytes(addr, 4))[0]
                    if abs(cur - self.written.get((i, name), cur)) <= 1e-6:   # still ours (not rebuilt since)
                        pm.write_bytes(addr, struct.pack("<f", v), 4)
                        n += 1
                except Exception:
                    pass
        self.reset()
        return n
