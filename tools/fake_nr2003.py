"""A fake NR2003 process for dry runs of the memory probes (no game needed).

FakeNR2003 looks like a pymem.Pymem to the probes (read_bytes / write_bytes / read_int / read_double / read_float) and
keeps a small world in a sparse memory: AI cars going round an 858 m oval (Bristol-sized), passing, running side by
side, following, touching. Every address the aggression probe reads is filled in where NR2003 keeps it (memory_map):
car blocks (position, velocity, lap, lap distance, damage, the aggression rating triplet, the 8 relation slots and the
max room), driver entries (number, name), flag / session / lap / tick clock, track name, pit road bytes and the
throttle / brake bytes of the car records.

The world is NOT NR2003's AI. It only reacts to each car's working aggression (car + 0x16F6) in the direction the
static disassembly says the game does, so a dry run can show that the probe measures and reports the difference:
  - side by side, a car leaves the room 3.4035 - 0.625 x aggression (centre to centre, at least 1.0 m): more
    aggression = closer, under ~1.9 m (a car width) = contact;
  - the relation slots' room is the game's middle side-by-side threshold 3.9035 - 0.75 x aggression;
  - following, it sits 6 + 4 / aggression metres back and lifts more the less aggressive it is.
The game clock runs `accel` times real time (like the user's time acceleration). reroll_at = game second at which
every working value is re-rolled (mean + a draw from the deviation), as NR2003 does at a session start.
"""
from __future__ import annotations

import math
import os
import random
import struct
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from aimod import memory_map as mm      # noqa: E402

PAGE = 4096
LAP_M = 858.0
TICKS_PER_S = 36
THROTTLE_REC_0, THROTTLE_REC_STRIDE, OFF_THROTTLE = 0xC12030, 5560, 0x604     # +0x605 = brake-ish byte
AGG_MEAN = mm.OFF_DRIVER + 0x30          # car + 0x16EE: mean, deviation, working value (aggression)
REL_BASE, REL_STRIDE, REL_SLOTS = 0x13C3, 0x5D, 8
OFF_ROOM_MAX = 0x1BAD
CAR_WIDTH = 1.9035
TRACK_HALF = 4.5                          # m either side of the centre line


class _Car:
    def __init__(self, slot, number, first, last, mean, dev, rng):
        self.slot, self.number, self.first, self.last = slot, number, first, last
        self.mean, self.dev = mean, dev
        self.s = 0.0                      # distance covered (laps x LAP_M + lap distance)
        self.lat = 0.0                    # m from the centre line (+ = outside)
        self.lat_target = 0.0
        self.v0 = 52.0 + rng.uniform(-1.5, 1.5)
        self.phase = rng.uniform(0, 2 * math.pi)
        self.v = self.v0
        self.damage = 0.0
        self.throttle = 255
        self.brake = 0
        self.hit_until = 0.0


class FakeNR2003:
    process_id = 0
    process_handle = None

    def __init__(self, n_ai: int = 20, accel: float = 8.0, seed: int = 1, reroll_at: float | None = None,
                 clock=time.monotonic):
        self.rng = random.Random(seed)
        self.accel, self.clock, self.reroll_at = accel, clock, reroll_at
        self.pages: dict[int, bytearray] = {}
        self.t = 0.0                      # game seconds
        self._real = clock()
        self.writes: list[tuple[int, bytes]] = []
        self.cars: list[_Car] = []
        names = ["Ward Burton", "Sterling Marlin", "Jeff Gordon", "Mark Martin", "Rusty Wallace", "Dale Jarrett",
                 "Bobby Labonte", "Tony Stewart", "Kevin Harvick", "Matt Kenseth", "Ricky Rudd", "Jimmie Johnson",
                 "Kurt Busch", "Ryan Newman", "Jeff Burton", "Bill Elliott", "Michael Waltrip", "Kenny Wallace",
                 "Elliott Sadler", "Jamie McMurray", "Dave Blaney", "Joe Nemechek", "Ken Schrader", "Jeremy Mayfield",
                 "Terry Labonte", "Ward Brown", "Casey Mears", "Greg Biffle", "Robby Gordon", "Kyle Petty"]
        numbers = [22, 40, 24, 6, 2, 88, 18, 20, 29, 17, 21, 48, 97, 12, 99, 9, 15, 23, 38, 42, 77, 1, 49, 19, 5,
                   4, 41, 16, 31, 45]
        for k in range(n_ai):
            mean = round(self.rng.uniform(0.95, 1.63), 3)
            first, last = names[k % len(names)].split(" ", 1)
            self.cars.append(_Car(2 + k, numbers[k % len(numbers)], first, last, mean, 0.05, self.rng))
        # the player (slot 0) drives too; slot 1 = the pace car, parked
        self.player = _Car(0, 98, "Player", "One", 1.0, 0.05, self.rng)
        for k, c in enumerate(self.cars + [self.player]):
            c.s = -k * 9.0 + LAP_M * 3     # a field in line, a few laps into the race
            c.lat = c.lat_target = 1.6 if k % 2 else -1.6
        self._write_static()
        for c in self.cars:
            self._roll(c)
        self._publish()

    # ------------------------------------------------------------------ memory
    def _span(self, addr: int, n: int, create: bool):
        out = []
        while n > 0:
            page, off = divmod(addr, PAGE)
            take = min(n, PAGE - off)
            buf = self.pages.get(page)
            if buf is None and create:
                buf = self.pages[page] = bytearray(PAGE)
            out.append((buf, off, take))
            addr += take
            n -= take
        return out

    def _put(self, addr: int, data: bytes):
        k = 0
        for buf, off, take in self._span(addr, len(data), True):
            buf[off:off + take] = data[k:k + take]
            k += take

    def _get(self, addr: int, n: int) -> bytes:
        parts = []
        for buf, off, take in self._span(addr, n, False):
            parts.append(bytes(take) if buf is None else bytes(buf[off:off + take]))
        return b"".join(parts)

    def read_bytes(self, addr: int, n: int) -> bytes:
        self._advance()
        return self._get(addr, n)

    def write_bytes(self, addr: int, data: bytes, n: int):
        self.writes.append((addr, bytes(data[:n])))
        self._put(addr, bytes(data[:n]))

    def read_int(self, addr: int) -> int:
        self._advance()
        return struct.unpack("<i", self._get(addr, 4))[0]

    def read_double(self, addr: int) -> float:
        self._advance()
        return struct.unpack("<d", self._get(addr, 8))[0]

    def read_float(self, addr: int) -> float:
        self._advance()
        return struct.unpack("<f", self._get(addr, 4))[0]

    def close_process(self):
        pass

    # ------------------------------------------------------------- the session
    @staticmethod
    def base(slot: int) -> int:
        return mm.CAR_BLOCK_0 + slot * mm.CAR_STRIDE

    def agg(self, c: _Car) -> float:
        return struct.unpack("<f", self._get(self.base(c.slot) + AGG_MEAN + 8, 4))[0]

    def _roll(self, c: _Car):
        work = c.mean + self.rng.gauss(0.0, 1.0) * c.dev * 1.9
        self._put(self.base(c.slot) + AGG_MEAN, struct.pack("<fff", c.mean, c.dev, work))

    def _write_static(self):
        self._put(mm.FLAG_STATE, struct.pack("<i", mm.FLAG_GREEN))
        self._put(mm.SESSION_STATE, bytes([mm.SESSION_RACE]))
        self._put(mm.TICK_RATE_FLAG, struct.pack("<i", 1))
        self._put(mm.TRACK_NAME_ADDRS[0], b"fakeoval".ljust(mm.SERIES_NAME_OFFSET, b"\0")
                  + b"fakemod".ljust(mm.SERIES_NAME_OFFSET, b"\0"))
        self._put(mm.ON_PIT_ROAD, bytes(mm.NUM_CARS))
        for c in self.cars + [self.player]:
            e = mm.DRIVER_ENTRY_0 + c.slot * mm.DRIVER_ENTRY_SIZE
            self._put(e + mm.DE_OFF_FIRST, c.first.encode()[:15].ljust(16, b"\0"))
            self._put(e + mm.DE_OFF_LAST, c.last.encode()[:19].ljust(20, b"\0"))
            self._put(e + mm.DE_OFF_CARNUM, struct.pack("<i", c.number))
            self._put(self.base(c.slot) + mm.OFF_DRIVER + mm.DRV_TRACTION, struct.pack("<f", 1.0))   # traction mult
            self._put(self.base(c.slot) + 0x1BEA, struct.pack("<f", 1.0))      # side-padding base
        # the parameter table (144 x 24 bytes from 0x74AA70: key name pointer at +8, live float at +16)
        names = [("ai_pacing_distance", 1.5), ("ai_drafting_distance", 1.10), ("ai_panic_decel", 6.0),
                 ("ai_dlongpad_scale", 2.0), ("ai_dlat_pad", 0.82), ("ai_squeeze_pcnt", 0.0)]
        for k, (nm, v) in enumerate(names):
            sp = 0x6F0000 + k * 0x40
            self._put(sp, nm.encode() + bytes(1))
            self._put(0x74AA70 + (84 + k) * 24 + 8, struct.pack("<I", sp))
            self._put(0x74AA70 + (84 + k) * 24 + 16, struct.pack("<f", v))
        e = mm.DRIVER_ENTRY_0 + mm.PACE_SLOT * mm.DRIVER_ENTRY_SIZE
        self._put(e + mm.DE_OFF_FIRST, b"Pace".ljust(16, b"\0"))
        self._put(e + mm.DE_OFF_LAST, b"Car".ljust(20, b"\0"))
        self._put(e + mm.DE_OFF_CARNUM, struct.pack("<i", 0))

    # ----------------------------------------------------------------- the world
    def _advance(self):
        now = self.clock()
        if now - self._real < 0.01:               # the world moves in 10 ms steps of real time, not on every read
            return
        dt = (now - self._real) * self.accel
        self._real = now
        dt = min(dt, 2.0)
        while dt > 1e-9:
            h = min(dt, 0.05)
            self._step(h)
            dt -= h
        self._publish()

    def _step(self, h: float):
        old_t = self.t
        self.t += h
        if self.reroll_at is not None and old_t < self.reroll_at <= self.t:
            for c in self.cars:
                self._roll(c)
        everyone = self.cars + [self.player]
        order = sorted(everyone, key=lambda c: -c.s)
        for c in everyone:
            a = self.agg(c) if c is not self.player else 1.3
            a = a if math.isfinite(a) and a > 0.05 else 0.05
            frac = (c.s % LAP_M) / LAP_M
            corner = 0.2 <= frac < 0.45 or 0.7 <= frac < 0.95
            v = c.v0 + 2.0 * math.sin(self.t / 25.0 + c.phase) - (4.0 if corner else 0.0)
            thr = 170 if corner else 255
            brk = 80 if corner and ((0.2 <= frac < 0.24) or (0.7 <= frac < 0.74)) else 0
            follow_gap = 6.0 + 4.0 / a
            ahead, side = None, None
            for o in order:
                if o is c:
                    continue
                d = o.s - c.s
                if 0 < d < follow_gap and abs(o.lat - c.lat) < CAR_WIDTH + 0.2 and (ahead is None or d < ahead[0]):
                    ahead = (d, o)
                if abs(d) < 5.0 and (side is None or abs(d) < abs(side[0])):
                    side = (d, o)
            if ahead is not None:
                d, o = ahead
                v = min(v, o.v - (1.0 - d / follow_gap) * 2.0)
                thr -= int(50 / a)
                # pull out to pass, more eagerly the more aggressive
                if self.rng.random() < 0.02 * a:
                    c.lat_target = -1.6 if o.lat > 0 else 1.6
            if side is not None:
                d, o = side
                room = max(1.0, 3.4035 - 0.625 * a)
                want = o.lat + (room if c.lat >= o.lat else -room)
                c.lat_target = max(-TRACK_HALF, min(TRACK_HALF, want))
                thr -= int(30 / a)
                gap = abs(c.lat - o.lat)
                if gap < CAR_WIDTH and self.t >= c.hit_until:
                    hit = self.rng.uniform(0.002, 0.012)
                    c.damage += hit
                    o.damage += hit * self.rng.uniform(0.5, 1.5)
                    c.hit_until = o.hit_until = self.t + 1.5
                    v -= 2.0
            step = 1.5 * h
            c.lat += max(-step, min(step, c.lat_target - c.lat))
            c.v = max(20.0, v)
            c.throttle, c.brake = max(0, min(255, thr)), brk
        for c in everyone:
            c.s += c.v * h

    def _publish(self):
        everyone = self.cars + [self.player]
        leader = max(c.s for c in everyone)
        self._put(mm.LAPS_CURRENT, struct.pack("<d", float(int(leader // LAP_M))))
        self._put(mm.GAME_TICKS, struct.pack("<i", int(self.t * TICKS_PER_S) + 1))
        r = LAP_M / (2 * math.pi)
        for c in everyone:
            b = self.base(c.slot)
            th = 2 * math.pi * (c.s % LAP_M) / LAP_M
            rad = r + c.lat
            self._put(b + mm.OFF_POS_X, struct.pack("<ff", rad * math.cos(th), rad * math.sin(th)))
            self._put(b + mm.OFF_VEL_X, struct.pack("<fff", -math.sin(th) * c.v, math.cos(th) * c.v, 0.0))
            self._put(b + mm.OFF_LAPS_DONE, struct.pack("<i", int(c.s // LAP_M)))
            self._put(b + mm.OFF_LAP_DIST, struct.pack("<f", c.s % LAP_M))
            self._put(b + mm.OFF_DAMAGE_FRONT, struct.pack("<f", c.damage))
            rec = THROTTLE_REC_0 + c.slot * THROTTLE_REC_STRIDE + OFF_THROTTLE
            self._put(rec, bytes([c.throttle, c.brake]))
            if c is self.player:
                continue
            a = self.agg(c)
            near = sorted(((abs(o.s - c.s), o) for o in everyone if o is not c and abs(o.s - c.s) < 30.0),
                          key=lambda x: x[0])[:REL_SLOTS]
            blk = bytearray(REL_SLOTS * REL_STRIDE)
            best = 0.0
            for j, (dist, o) in enumerate(near):
                d = o.s - c.s
                if abs(d) < 5.0:
                    typ = (5 if o.lat > c.lat else 6) + (0 if d >= 0 else 2)
                    room = max(0.0, 3.9035 - 0.75 * a)
                else:
                    typ, room = (9 if d > 0 else 10), 0.0
                best = max(best, room)
                off = j * REL_STRIDE
                struct.pack_into("<I", blk, off + 0x10, 1)
                struct.pack_into("<I", blk, off + 0x18, typ)
                struct.pack_into("<I", blk, off + 0x1C, self.base(o.slot))
                struct.pack_into("<f", blk, off + 0x49, room)
            self._put(b + REL_BASE, bytes(blk))
            self._put(b + OFF_ROOM_MAX, struct.pack("<f", best))
