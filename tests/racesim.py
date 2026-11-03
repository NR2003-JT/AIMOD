"""Whole-race simulator for the strategy engine: feeds StrategyEngine.tick the same kind of snapshots NR2003
gives AIMOD and acts on the writes it sends back, so whole races can be checked by track type.

Modelled: lap progress and scoring-table crossing times; fuel burn per lap (game units, real tank size) and
the fuel request levels; tire wear and lap-time falloff (flat for a few laps, then linear, per-car tire
management factor); pit entry / pit road / stall service (timer) / exit, the request cleared at box exit;
cautions that bunch the field behind the pace car, one to go, double file, green; stage cautions.
Not modelled: passing, damage, wave arounds, penalties.
"""
from __future__ import annotations

import os
import random
import sys
import tempfile
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aimod import memory_map as mm
from aimod.strategy import CarSnapshot, RaceSnapshot, StrategyEngine, StrategyParams
from aimod.tiremodel import TireModel

GREEN = mm.FLAG_GREEN
FUEL_ADD_TRUE = {1: 0.12, 2: 0.25, 3: 0.47, 4: 0.72}     # share of a tank each request level really adds


@dataclass
class Track:
    name: str
    length_m: float
    lap_time: float                 # green lap on fresh tires, seconds
    pit_road_s: float               # seconds to drive pit road entry -> exit (without the stop)
    pit_frac: float = 0.06          # share of a lap pit road covers
    entry_frac: float = 0.90        # pit entry, share of the lap past the line
    caution_lap_s: float = 0.0      # pace lap time (default 1.8 x green lap)


@dataclass
class Car:
    idx: int
    number: int
    skill: float                    # lap time multiplier (1.0 = average)
    manager: float                  # tire falloff multiplier (0.7 = saves tires, 1.3 = fades)
    make: str
    progress: float = 0.0           # laps (whole + fraction past the line)
    fuel: float = 0.0
    age_l: float = 0.0              # laps on the tires
    age_r: float = 0.0
    pitting: int = 0
    req_l: int = 0
    req_r: int = 0
    fuel_req: int = 5
    road: bool = False              # on pit road
    road_left: float = 0.0          # seconds of pit road left
    service_left: float = 0.0       # seconds of service left (in the stall)
    timer: float = 0.0
    entered_at: float = 0.0
    cross: list = field(default_factory=list)   # game ms each completed lap
    green_stops: int = 0
    caution_stops: int = 0
    stops: list = field(default_factory=list)   # (race lap, green?, code)
    out_of_fuel: bool = False


class RaceSim:
    def __init__(self, track: Track, laps: int, n: int = 36, tank_laps: float = 71.0, cap: float = 62.47,
                 slope: float = 0.06, plateau: int = 4, cautions=(), caution_len: int = 4, seed: int = 1,
                 params: StrategyParams | None = None, drafting: bool = False, makes=("Chevrolet", "Ford", "Toyota"),
                 dt: float = 1.0, wear_per_lap: float = 0.006, skill_spread: float = 0.012,
                 managers=(0.75, 0.9, 1.0, 1.0, 1.1, 1.25)):
        self.t = track
        self.laps = laps
        self.rng = random.Random(seed)
        self.cap = cap
        self.rate = cap / tank_laps            # fuel per green lap
        self.slope, self.plateau = slope, plateau
        self.wear_per_lap = wear_per_lap
        self.caution_starts = sorted(cautions)
        self.caution_len = caution_len
        self.drafting = drafting
        self.dt = dt
        self.now = 0.0
        self.flag = GREEN
        self.caution_end_lap = None
        self.cars = []
        for i in range(n):
            c = Car(i + 2, i + 1, skill=1.0 + self.rng.uniform(-skill_spread, skill_spread),
                    manager=self.rng.choice(list(managers)), make=makes[i % len(makes)])
            c.progress = -0.002 * i                 # single file at the start
            c.fuel = cap
            self.cars.append(c)
        tmp = tempfile.mkdtemp()
        self.model = TireModel(os.path.join(tmp, "tm.json"))
        self.model.use("sim", track.name)
        self.engine = StrategyEngine(random.Random(seed), clock=lambda: self.now, tire_model=self.model)
        self.p = params or StrategyParams(tank_laps=tank_laps, game_plan_off=True, fuel_margin=2)
        self.leader_laps_at_stage = {}

    # ------------------------------------------------------------------ physics
    def lap_time(self, c: Car) -> float:
        age = max(c.age_l, c.age_r)
        fall = max(0.0, age - self.plateau) * self.slope * c.manager
        return self.t.lap_time * c.skill + fall

    def leader(self) -> Car:
        return max(self.cars, key=lambda c: c.progress)

    def step(self):
        dt = self.dt
        lead = self.leader()
        caution = self.flag != GREEN
        pace_lap = self.t.caution_lap_s or self.t.lap_time * 1.8
        order = sorted(self.cars, key=lambda c: -c.progress)
        for k, c in enumerate(order):
            if c.out_of_fuel:
                continue
            before = c.progress
            if c.road:
                if c.service_left > 0 and c.road_left <= self.t.pit_road_s / 2:
                    c.service_left -= dt                    # stationary in the stall
                    c.timer += dt
                    if c.service_left <= 0:
                        self.finish_service(c)
                    continue
                c.road_left -= dt
                c.progress += self.t.pit_frac * dt / self.t.pit_road_s
                if c.road_left <= 0 and c.service_left <= 0:
                    c.road = False
            else:
                if caution:
                    speed = 1.0 / pace_lap
                    if c is not lead:
                        # close up behind the car ahead (keeps laps down)
                        ahead = order[k - 1]
                        gap = (ahead.progress - c.progress) % 1.0       # distance to the car ahead on track
                        if 0.006 < gap < 0.9 and not ahead.road:
                            speed *= 1.8
                else:
                    speed = 1.0 / self.lap_time(c)
                c.progress += speed * dt
                frac_before, frac_now = before % 1.0, c.progress % 1.0
                passed_entry = (frac_before < self.t.entry_frac <= frac_now) or (
                    frac_now < frac_before and self.t.entry_frac > frac_before)
                pits_open = not caution or self.flag in (1, 2, 3, 4, 5)
                if c.pitting and passed_entry and pits_open:
                    self.enter_pits(c)
            burn = self.rate * (c.progress - before) * (0.55 if caution else 1.0)
            c.fuel -= burn
            if c.fuel <= 0:
                c.fuel, c.out_of_fuel = 0.0, True
            if int(c.progress) > int(before) and int(c.progress) >= 1:
                for lap in range(int(before) + 1, int(c.progress) + 1):
                    c.cross.append((lap, int(self.now * 1000)))
                    if not c.road:
                        c.age_l += 1
                        c.age_r += 1
        self.now += dt
        self.update_flag()

    def enter_pits(self, c: Car):
        c.road = True
        c.road_left = self.t.pit_road_s
        tires = 14.0 if (c.req_l and c.req_r) else 9.5 if (c.req_l or c.req_r) else 0.0
        fuel_s = 4.0 + 8.0 * (1.0 if c.fuel_req >= 5 else FUEL_ADD_TRUE.get(c.fuel_req, 0)) if c.fuel_req else 0.0
        c.service_left = max(tires, fuel_s, 2.0)
        c.timer = 0.0
        lap = int(self.leader().progress) + 1
        green = self.flag == GREEN
        code = ("F" if c.fuel_req else "") + ("+L/R" if c.req_l and c.req_r else "+L" if c.req_l else
                                            "+R" if c.req_r else "")
        c.stops.append((lap, green, code.strip("+")))
        if green:
            c.green_stops += 1
        else:
            c.caution_stops += 1

    def finish_service(self, c: Car):
        if c.fuel_req:
            add = self.cap if c.fuel_req >= 5 else FUEL_ADD_TRUE.get(c.fuel_req, 0) * self.cap
            c.fuel = min(self.cap, c.fuel + add)
        if c.req_l:
            c.age_l = 0
        if c.req_r:
            c.age_r = 0
        c.pitting = 0                               # the game clears the request at box exit

    def update_flag(self):
        lead_lap = int(self.leader().progress) + 1
        if self.flag == GREEN:
            if self.caution_starts and lead_lap >= self.caution_starts[0]:
                self.caution_starts.pop(0)
                self.flag = 0
                self.caution_end_lap = lead_lap + self.caution_len
                self.caution_started = self.now
            return
        if self.flag == 0 and self.now - self.caution_started > 6:
            self.flag = 1
        elif 1 <= self.flag < 5 and lead_lap >= self.caution_end_lap - 1:
            self.flag = 5
        elif self.flag == 5 and lead_lap >= self.caution_end_lap:
            self.flag = GREEN
        elif 1 <= self.flag < 4 and self.now - self.caution_started > 20:
            self.flag = 2

    # ------------------------------------------------------------------ AIMOD side
    def snapshot(self) -> RaceSnapshot:
        cars = []
        for c in self.cars:
            laps_done = max(0, int(c.progress))
            cross = c.cross[-1][1] if c.cross else None
            prev = c.cross[-2][1] if len(c.cross) >= 2 else None
            wl = min(1.0, c.age_l * self.wear_per_lap * 0.8 * c.manager)
            wr = min(1.0, c.age_r * self.wear_per_lap * c.manager)
            in_stall = c.road and c.service_left > 0 and c.road_left <= self.t.pit_road_s / 2
            lt = self.lap_time(c)
            speed = 0.0 if in_stall else (self.t.length_m * self.t.pit_frac / self.t.pit_road_s if c.road
                                           else self.t.length_m / lt)
            cars.append(CarSnapshot(
                c.idx, True, grip=max(0.02, 1.0 - (wl + wr) / 2), strategy=1.6, pitting=c.pitting,
                wear_lf=wl, wear_lr=wl, wear_rf=wr, wear_rr=wr, number=c.number, name=f"Driver {c.number}",
                laps_done=laps_done, lap_dist=(c.progress % 1.0) * self.t.length_m, make=c.make,
                on_pit_road=c.road, pit_timer=c.timer, speed=speed, fuel=c.fuel, fuel_cap=self.cap,
                cross_ms=cross, prev_cross_ms=prev, reason_out=1 if c.out_of_fuel else 0))
        snap = RaceSnapshot(self.flag, mm.SESSION_RACE, 0.0, float(int(self.leader().progress) + 1), self.laps, cars)
        snap.session_time = self.now
        snap.drafting = self.drafting
        if self.flag != GREEN:
            snap.pace_dist = ((self.leader().progress + 0.05) % 1.0) * self.t.length_m
        return snap

    def apply(self, res):
        by = {c.idx: c for c in self.cars}
        for w in res.writes:
            c = by.get(w.idx)
            if c is None:
                continue
            if w.offset == mm.OFF_PITTING:
                if not (c.road and c.service_left <= 0):   # the game ignores a new request leaving the pits
                    c.pitting = w.value
            elif w.offset == mm.OFF_REQ_LEFT:
                c.req_l = w.value
            elif w.offset == mm.OFF_REQ_RIGHT:
                c.req_r = w.value
            elif w.offset == mm.OFF_FUEL_REQ:
                c.fuel_req = w.value

    def run(self, until_lap: int | None = None, on_lap=None):
        end = until_lap or self.laps
        last = 0
        self.events = []
        self.rows = None
        while int(self.leader().progress) < end:
            res = self.engine.tick(self.snapshot(), self.p)
            self.rows = res.rows
            for ev in res.events:
                self.events.append(ev)
            self.apply(res)
            for _ in range(int(max(1, 1.0 / self.dt))):
                self.step()
            lap = int(self.leader().progress)
            if on_lap and lap != last:
                on_lap(self, lap)
            last = lap
        return self

    # ------------------------------------------------------------------ reports
    def laps_down(self) -> dict[int, int]:
        lead = self.leader().progress
        return {c.number: int(lead - c.progress) for c in self.cars}

    def finish_fuel_laps(self) -> list[float]:
        return [c.fuel / self.rate for c in self.cars if not c.out_of_fuel]
