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

import json
import logging
import math
import os
import struct

from . import aggression as ag
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


AGG_EPS = 1e-6
AGG_READ_MAX = 6.0           # a working value above this (or <= 0, NaN) isn't a rating: the car is left alone


class AggressionWriter:
    """Racing intensity / rivals in game memory: each AI car's working aggression (car + 0x16F6, see aggression.py).

    The game rolls every driver's value once at the session start and never writes it again in the race, so this is
    "set once, put back when done":
      capture()   takes each car's rolled value (call it once the race is under way, after the start roll);
      apply()     puts the targets in - only changes are written, clamped to [0, HARD_MAX] (a driver rolled higher
                  keeps his own value); cars left out go back to their own value; shadow=True writes nothing and
                  returns what it would have written;
      rerolls()   a car whose value is no longer what we left there was rolled again by the game: the new value is
                  his own from now on (nothing is written back over it);
      restore()   every car still holding our value gets his own back (close, disable, mode change, new session).
    Never the player's car (slot 0) or the pace car (slot 1). The originals are also kept in a small JSON file
    (pid + session), so an AIMOD that closed without restoring can put them back with recover() on the next attach
    to the same running game."""

    def __init__(self, restore_path: str | None = None):
        self.restore_path = restore_path
        self._checked: tuple | None = None
        self.reset()

    def reset(self):
        self.held: set[int] = set()              # slots a showdown probe has (not captured, not written)
        self.orig: dict[int, float] = {}         # slot -> the game's rolled value (the driver's own)
        self.last: dict[int, float] = {}         # slot -> what is in memory as we last left / saw it
        self.mean: dict[int, float] = {}         # slot -> the .car mean (car + 0x16EE), for the log
        self.ours: set[int] = set()              # slots holding a value we wrote
        self.shadow_val: dict[int, float] = {}   # SHADOW: what each car would hold
        self.session: tuple = ()
        self.pid = None

    # ---------------------------------------------------------------- checks
    @staticmethod
    def slot_ok(slot: int) -> bool:
        return slot not in (mm.PLAYER_SLOT, mm.PACE_SLOT) and 0 <= slot < mm.NUM_CARS

    @staticmethod
    def addr(slot: int) -> int:
        return mm.CAR_BLOCK_0 + slot * mm.CAR_STRIDE + mm.OFF_AGG_WORK

    def available(self, pm) -> bool:
        """The aggression read is where this NR2003.exe build has it (checked once per attach)."""
        if pm is None:
            return False
        if self._checked is not None and self._checked[0] == id(pm):
            return self._checked[1]
        addr, want = mm.AGG_CODE_CHECK
        try:
            ok = pm.read_bytes(addr, len(want)) == want
        except Exception:
            ok = False
        if not ok:
            log.warning("Racing intensity off: unknown NR2003.exe build (0x%X)", addr)
        self._checked = (id(pm), ok)
        return ok

    def _read(self, pm, slot: int, off: int = mm.OFF_AGG_WORK) -> float | None:
        try:
            v = struct.unpack("<f", pm.read_bytes(mm.CAR_BLOCK_0 + slot * mm.CAR_STRIDE + off, 4))[0]
        except Exception:
            return None
        return v if math.isfinite(v) and 0.0 < v <= AGG_READ_MAX else None

    def _write(self, pm, slot: int, v: float):
        pm.write_bytes(self.addr(slot), struct.pack("<f", v), 4)

    @property
    def captured(self) -> bool:
        return bool(self.orig)

    # ----------------------------------------------------------------- state
    def capture(self, pm, slots, session: tuple = ()) -> int:
        """Take the rolled value of every car not taken yet. Returns how many were taken."""
        if pm is None:
            return 0
        n = 0
        for s in slots:
            if not self.slot_ok(s) or s in self.orig or s in self.held:
                continue
            v = self._read(pm, s)
            if v is None:
                continue
            self.orig[s] = self.last[s] = v
            m = self._read(pm, s, mm.OFF_AGG_MEAN)
            if m is not None:
                self.mean[s] = m
            n += 1
        if n:
            self.session = tuple(session)
            self.pid = getattr(pm, "process_id", None)
        return n

    def hold(self, pm, slots) -> int:
        """A showdown probe is using these cars: each gets his own value back (if it is still ours) and is left alone
        until unhold(). Returns how many values were written back."""
        n = 0
        for s in slots:
            if not self.slot_ok(s):
                continue
            self.held.add(s)
            if s in self.orig:
                if pm is not None and s in self.ours:
                    v = self._read(pm, s)
                    if v is not None and abs(v - self.last[s]) <= AGG_EPS:
                        try:
                            self._write(pm, s, self.orig[s])
                            n += 1
                        except Exception:
                            pass
                self.forget(s)
        if n or slots:
            self._save()
        return n

    def unhold(self):
        """the probe is gone: the held cars are taken again (with whatever value the probe left) next capture"""
        self.held.clear()

    def forget(self, slot: int):
        """A car that left the session (its slot may get another car): nothing of it is kept."""
        for d in (self.orig, self.last, self.mean, self.shadow_val):
            d.pop(slot, None)
        self.ours.discard(slot)

    def rerolls(self, pm) -> list[tuple[int, float, float]]:
        """(slot, old own value, new own value) of every car the game rolled again since we last looked."""
        out = []
        if pm is None:
            return out
        for s in list(self.orig):
            v = self._read(pm, s)
            if v is None or abs(v - self.last[s]) <= AGG_EPS:
                continue
            out.append((s, self.orig[s], v))
            self.orig[s] = self.last[s] = v
            self.ours.discard(s)
            self.shadow_val.pop(s, None)
        if out:
            self._save()
        return out

    def apply(self, pm, targets: dict[int, float], shadow: bool = False) -> list[tuple[int, float, float]]:
        """targets: slot -> value. Captured cars not in it go back to their own value. Returns (slot, old, new) of
        each change written (shadow: each change that would have been written)."""
        changes = []
        if pm is None:
            return changes
        for s in sorted(self.orig):
            own = self.orig[s]
            want = targets.get(s, own)
            if want is None or not math.isfinite(want):
                want = own
            want = _f32(ag.clamp(want, 0.0, max(ag.HARD_MAX, own)))
            if shadow:
                cur = self.shadow_val.get(s, own)
                if abs(want - cur) > AGG_EPS:
                    changes.append((s, cur, want))
                    if abs(want - own) <= AGG_EPS:
                        self.shadow_val.pop(s, None)
                    else:
                        self.shadow_val[s] = want
                continue
            cur = self.last[s]
            if abs(want - cur) <= AGG_EPS:
                continue
            try:
                self._write(pm, s, want)
            except Exception as ex:                  # a car that went away mid-call
                log.debug("aggression write failed car %d: %s", s, ex)
                continue
            self.last[s] = want
            if abs(want - own) <= AGG_EPS:
                self.ours.discard(s)
            else:
                self.ours.add(s)
            changes.append((s, cur, want))
        if changes and not shadow:
            self._save()
        return changes

    def restore(self, pm) -> int:
        """Every car still holding our value gets his own back. Returns how many were written. Without the game
        (pm None) nothing can be written: the restore file stays for recover()."""
        n = 0
        if pm is not None:
            for s in sorted(self.ours):
                v = self._read(pm, s)
                if v is None or abs(v - self.last[s]) > AGG_EPS:      # the game rolled it again: his value stands
                    continue
                try:
                    self._write(pm, s, self.orig[s])
                    n += 1
                except Exception:
                    pass
            self._delete_file()
        self.reset()
        return n

    # ------------------------------------------------------------- the file
    def _save(self):
        if not self.restore_path:
            return
        if not self.ours:
            self._delete_file()
            return
        data = {"pid": self.pid, "session": list(self.session),
                "cars": {str(s): [self.orig[s], self.last[s]] for s in sorted(self.ours)}}
        tmp = self.restore_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self.restore_path)
        except OSError as ex:
            log.debug("aggression restore file not written: %s", ex)

    def _delete_file(self):
        if self.restore_path:
            try:
                os.remove(self.restore_path)
            except OSError:
                pass

    def recover(self, pm, session: tuple = ()) -> int:
        """Values a previous AIMOD left in this same running game (same pid and session) go back to the drivers'
        own. Returns how many were written; the file is removed either way."""
        if pm is None or not self.restore_path or not os.path.isfile(self.restore_path):
            return 0
        n = 0
        try:
            with open(self.restore_path, encoding="utf-8") as f:
                data = json.load(f)
            same = (data.get("pid") == getattr(pm, "process_id", None)
                    and tuple(data.get("session") or ()) == tuple(session))
            if same:
                for k, (own, last) in (data.get("cars") or {}).items():
                    s = int(k)
                    if not self.slot_ok(s):
                        continue
                    v = self._read(pm, s)
                    if v is not None and abs(v - float(last)) <= AGG_EPS and 0.0 < float(own) <= AGG_READ_MAX:
                        self._write(pm, s, _f32(float(own)))
                        n += 1
        except (OSError, ValueError, TypeError, AttributeError) as ex:
            log.debug("aggression restore file unreadable: %s", ex)
        self._delete_file()
        return n
