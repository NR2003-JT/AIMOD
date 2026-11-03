"""Crew chief on a phone (beta): people on phones run an AI car's caution calls, pit plans and adjustments.

The Desk sits between the web server thread (phones claim a car and send calls) and AIMOD's tick thread
(which hands the calls to the strategy engine). Phones never touch the engine or the game: the web thread
only records what was asked, under a lock, and the tick thread applies it once per tick through
engine.human_owner / engine.human_calls, where the engine treats a call like its own decision (with the
safety rules still on top).

Who may call for a car: the phone that claimed it, proven by the token the claim returned. One phone per car,
one car per phone. A phone that stops checking in for PHONE_GONE_S (locked, asleep, another app) is "away":
AIMOD runs the car meanwhile, but the car stays that person's, and the phone's next check-in hands it straight
back. Only after HOLD_S does the car go back to AIMOD for good. The same name can take its car back at any time
(a new token, e.g. the home screen app after re-pairing); someone else only once the phone has been away
TAKEOVER_S.
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass

from . import memory_map as mm
from .strategy import FOUR_TIRES, FUEL_ONLY, LEFT_SIDE, RIGHT_SIDE

PHONE_GONE_S = 15.0           # no check-in this long: away (AIMOD runs the car until the phone is back)
HOLD_S = 600.0                # away this long: the car goes back to AIMOD for good
TAKEOVER_S = 120.0            # away this long: another phone may take the car
SERVICES = {"4": FOUR_TIRES, "R": RIGHT_SIDE, "L": LEFT_SIDE, "F": FUEL_ONLY}
MAX_NAME = 20
ADJ_MAX = 3                   # steps each way per part per stop (wedge 5 lb, track bar 1/4 in, RR air 1/2 psi, tape 5 %)
ADJ_PARTS = ("wedge", "bar", "air", "tape")


@dataclass
class HumanCall:
    caution_no: int               # the caution it's for (engine.cautions); any other is stale
    pit: bool
    service: str | None = None    # FOUR_TIRES / RIGHT_SIDE / LEFT_SIDE / FUEL_ONLY, None = AIMOD picks
    fuel_level: int | None = None  # NR2003 fuel request 1-5, None = AIMOD picks
    who: str = ""
    at: float = 0.0


@dataclass
class HumanPlan:
    lap: int                      # race lap (leader's numbering) the car pits at the end of
    service: str | None = None    # FOUR_TIRES / RIGHT_SIDE / LEFT_SIDE / FUEL_ONLY, None = AIMOD picks
    fuel_level: int | None = None  # NR2003 fuel request 1-5, None = AIMOD picks
    who: str = ""
    plan_id: int = 0              # new for every edit: a used plan never applies twice


@dataclass
class HumanAdjust:
    wedge: int = 0                # 5 lb steps (+ tightens)
    bar: int = 0                  # rear track bar, 1/4 inch steps (+ = up, frees the car)
    air: int = 0                  # right-rear air, 1/2 psi steps (+ = in, frees the car)
    tape: int = 0                 # grille tape, 5 % steps
    who: str = ""
    adjust_id: int = 0            # new for every edit: used once, at the car's next stop
    # all zeros = "no changes this stop" (AIMOD's own crew chief leaves the car alone too)


@dataclass
class _Owner:
    token: str
    name: str
    car: str                      # the car's painted number ("01")
    seen: float
    away: bool = False            # quiet past PHONE_GONE_S: AIMOD runs the car until the phone checks in


class Desk:
    def __init__(self, log=None):
        self.lock = threading.Lock()
        self.owners: dict[str, _Owner] = {}       # car label -> owner
        self.calls: dict[str, HumanCall] = {}     # car label -> its call for the current caution
        self.plans: dict[str, HumanPlan] = {}     # car label -> its green-flag pit plan
        self.plan_no = 0
        self.adjusts: dict[str, HumanAdjust] = {}  # car label -> changes for its next stop
        self.lap_now = 0
        self.laps_total = 0
        self.cars: dict[str, int] = {}            # car label -> slot, AI cars on track (from the tick)
        self.caution_no = 0
        self.caution = False
        self.events: list[str] = []               # for the log (tick thread drains)
        self.log = log

    # ------------------------------------------------------------------ web server thread
    def claim(self, car: str, name: str) -> dict:
        car, name = str(car).strip().lstrip("#"), " ".join(str(name).split())[:MAX_NAME] or "Crew chief"
        now = time.monotonic()
        with self.lock:
            if car not in self.cars:
                return {"ok": False, "error": "That car isn't on track (or it's your own car)."}
            held = self.owners.get(car)
            same = held is not None and held.name.casefold() == name.casefold()
            if held is not None and not same and now - held.seen < TAKEOVER_S:
                asleep = " (their phone is asleep)" if held.away else ""
                return {"ok": False, "error": f"{held.name} is already the crew chief for #{car}{asleep}."}
            for other, o in list(self.owners.items()):    # one car per person: taking another gives the old one back
                if other != car and o.name.casefold() == name.casefold():
                    del self.owners[other]
                    self.calls.pop(other, None)
                    self.plans.pop(other, None)
                    self.adjusts.pop(other, None)
                    self.events.append(f"Crew chief: {o.name} gave #{other} back to AIMOD")
            token = secrets.token_urlsafe(12)
            self.owners[car] = _Owner(token, name, car, now)
            if same:                                      # the same person back: their plan and changes stand
                self.events.append(f"Crew chief: {name} is back on #{car}")
            else:
                self.calls.pop(car, None)
                self.plans.pop(car, None)
                self.adjusts.pop(car, None)
                self.events.append(f"Crew chief: {name} took #{car}")
            return {"ok": True, "token": token, "car": car, "back": same}

    def _owner(self, token: str) -> _Owner | None:
        return next((o for o in self.owners.values() if secrets.compare_digest(o.token, str(token))), None)

    def ping(self, token: str) -> dict:
        with self.lock:
            o = self._owner(token)
            if o is None:
                return {"ok": False, "error": "You're not running a car (AIMOD took it back)."}
            o.seen = time.monotonic()
            if o.away:
                o.away = False
                self.events.append(f"Crew chief: {o.name}'s phone is back, #{o.car} is theirs again")
            return {"ok": True, "car": o.car}

    def release(self, token: str) -> dict:
        with self.lock:
            o = self._owner(token)
            if o is None:
                return {"ok": True}
            del self.owners[o.car]
            self.calls.pop(o.car, None)
            self.plans.pop(o.car, None)
            self.adjusts.pop(o.car, None)
            self.events.append(f"Crew chief: {o.name} gave #{o.car} back to AIMOD")
            return {"ok": True}

    def call(self, token: str, caution_no, pit, service="", fuel="") -> dict:
        with self.lock:
            o = self._owner(token)
            if o is None:
                return {"ok": False, "error": "You're not running a car (AIMOD took it back)."}
            o.seen = time.monotonic()
            try:
                caution_no = int(caution_no)
            except (TypeError, ValueError):
                return {"ok": False, "error": "Bad call."}
            if not self.caution or caution_no != self.caution_no:
                return {"ok": False, "error": "There's no caution to call right now."}
            svc = SERVICES.get(str(service).upper()) if service else None
            if service and svc is None:
                return {"ok": False, "error": "Unknown service."}
            lvl = None
            if fuel not in ("", None):
                try:
                    lvl = int(fuel)
                except (TypeError, ValueError):
                    return {"ok": False, "error": "Bad fuel amount."}
                if not 0 <= lvl <= 5:
                    return {"ok": False, "error": "Bad fuel amount."}
            pit = str(pit) in ("1", "true", "True")
            self.calls[o.car] = HumanCall(caution_no, pit, svc if pit else None, lvl if pit else None, o.name,
                                          time.monotonic())
            self.events.append(f"Crew chief: {o.name} calls #{o.car} " +
                               (f"in, {svc or 'AIMOD picks the service'}" if pit else "to stay out"))
            return {"ok": True}

    def plan(self, token: str, lap="", service="", fuel="", clear="") -> dict:
        """a green-flag pit plan: pit at the end of race lap `lap` (now or earlier = box this lap)"""
        with self.lock:
            o = self._owner(token)
            if o is None:
                return {"ok": False, "error": "You're not running a car (AIMOD took it back)."}
            o.seen = time.monotonic()
            if str(clear) in ("1", "true"):
                if self.plans.pop(o.car, None) is not None:
                    self.events.append(f"Crew chief: {o.name} leaves #{o.car}'s next stop to AIMOD")
                return {"ok": True}
            try:
                lap = int(lap)
            except (TypeError, ValueError):
                return {"ok": False, "error": "Which lap?"}
            if self.laps_total and lap > self.laps_total:
                return {"ok": False, "error": f"The race is only {self.laps_total} laps."}
            lap = max(lap, self.lap_now)
            svc = SERVICES.get(str(service).upper()) if service else None
            if service and svc is None:
                return {"ok": False, "error": "Unknown service."}
            lvl = None
            if fuel not in ("", None):
                try:
                    lvl = int(fuel)
                except (TypeError, ValueError):
                    return {"ok": False, "error": "Bad fuel amount."}
                if not 0 <= lvl <= 5:
                    return {"ok": False, "error": "Bad fuel amount."}
            self.plan_no += 1
            self.plans[o.car] = HumanPlan(lap, svc, lvl, o.name, self.plan_no)
            self.events.append(f"Crew chief: {o.name} plans #{o.car} in on lap {lap}"
                               + (f", {svc}" if svc else ""))
            return {"ok": True, "lap": lap, "plan_id": self.plan_no}

    def adjust(self, token: str, wedge=0, bar=0, air=0, tape=0, clear="") -> dict:
        """chassis changes for the car's next stop (all zero = no changes; clear = AIMOD's crew decides)"""
        with self.lock:
            o = self._owner(token)
            if o is None:
                return {"ok": False, "error": "You're not running a car (AIMOD took it back)."}
            o.seen = time.monotonic()
            if str(clear) in ("1", "true"):
                if self.adjusts.pop(o.car, None) is not None:
                    self.events.append(f"Crew chief: {o.name} leaves #{o.car}'s adjustments to AIMOD")
                return {"ok": True}
            steps = []
            for v in (wedge, bar, air, tape):
                try:
                    v = int(v or 0)
                except (TypeError, ValueError):
                    return {"ok": False, "error": "Bad adjustment."}
                if abs(v) > ADJ_MAX:
                    return {"ok": False, "error": f"At most {ADJ_MAX} steps each way per stop."}
                steps.append(v)
            self.plan_no += 1
            self.adjusts[o.car] = HumanAdjust(*steps, who=o.name, adjust_id=self.plan_no)
            what = ", ".join(f"{k} {v:+d}" for k, v in zip(ADJ_PARTS, steps) if v) or "no changes"
            self.events.append(f"Crew chief: {o.name} sets #{o.car}'s next stop: {what}")
            return {"ok": True, "adjust_id": self.plan_no}

    def release_all(self, why: str = "handed back to AIMOD"):
        with self.lock:
            if self.owners:
                self.events.append(f"Crew chief: every car {why}")
            self.owners.clear()
            self.calls.clear()
            self.plans.clear()
            self.adjusts.clear()

    # ------------------------------------------------------------------ tick thread
    def apply(self, engine, snap):
        """before engine.tick: the cars people run and their calls, keyed by slot"""
        now = time.monotonic()
        with self.lock:
            self.cars = {str(c.number): c.idx for c in snap.cars
                         if c.valid and not c.reason_out and c.controlled}
            self.caution = snap.flag != mm.FLAG_GREEN
            self.caution_no = getattr(engine, "cautions", 0)
            for car, o in list(self.owners.items()):
                if now - o.seen > HOLD_S:
                    del self.owners[car]
                    self.calls.pop(car, None)
                    self.plans.pop(car, None)
                    self.adjusts.pop(car, None)
                    self.events.append(f"Crew chief: {o.name}'s phone has been gone {HOLD_S / 60:g} min, "
                                       f"AIMOD has #{car} back")
                elif car not in self.cars:
                    del self.owners[car]
                    self.calls.pop(car, None)
                    self.plans.pop(car, None)
                    self.adjusts.pop(car, None)
                    self.events.append(f"Crew chief: #{car} is out, {o.name} is off the box")
                elif now - o.seen > PHONE_GONE_S and not o.away:
                    o.away = True
                    self.events.append(f"Crew chief: {o.name}'s phone went quiet, AIMOD runs #{car} until it's back")
            for car, c in list(self.calls.items()):
                if c.caution_no != self.caution_no:
                    del self.calls[car]                       # last caution's call
            engine.human_owner = {self.cars[car]: o.name for car, o in self.owners.items() if not o.away}
            engine.human_calls = {self.cars[car]: c for car, c in self.calls.items() if car in self.cars}
            engine.human_plans = {self.cars[car]: pl for car, pl in self.plans.items() if car in self.cars}
            engine.human_adjusts = {self.cars[car]: a for car, a in self.adjusts.items() if car in self.cars}
            events, self.events = self.events, []
        for e in events:
            if self.log:
                self.log(e)

    def sync(self, engine, snap):
        """after engine.tick: the caution number and flag the phones now see (a new caution starts inside the
        tick, so without this a call made straight away would be refused as stale)"""
        with self.lock:
            self.caution = snap.flag != mm.FLAG_GREEN
            self.caution_no = getattr(engine, "cautions", 0)
            self.lap_now = getattr(engine, "lap_now", 0) or 0
            self.laps_total = snap.laps_total or 0
            states = getattr(engine, "cars", {}) or {}
            for car, pl in list(self.plans.items()):          # a plan the engine has used is finished
                st = states.get(self.cars.get(car, -1))
                if (st is not None and getattr(st, "human_plan_id", None) == pl.plan_id
                        and getattr(st, "human_plan_status", "") in ("done", "used under caution")):
                    del self.plans[car]
            for car, a in list(self.adjusts.items()):         # adjustments are used at one stop
                st = states.get(self.cars.get(car, -1))
                if st is not None and a.adjust_id in (getattr(st, "human_adjust_used", None) or ()):
                    del self.adjusts[car]

    def view(self) -> dict:
        """for the crew view: who runs which car and their call (names only, never tokens)"""
        with self.lock:
            return {car: {"owner": o.name, "away": o.away,
                          "call": (None if car not in self.calls else
                                   {"pit": self.calls[car].pit, "service": self.calls[car].service,
                                    "fuel_level": self.calls[car].fuel_level}),
                          "plan": (None if car not in self.plans else
                                   {"lap": self.plans[car].lap, "service": self.plans[car].service,
                                    "fuel_level": self.plans[car].fuel_level, "plan_id": self.plans[car].plan_id}),
                          "adjust": (None if car not in self.adjusts else
                                     {k: getattr(self.adjusts[car], k) for k in ADJ_PARTS})}
                    for car, o in self.owners.items()}

    def summary(self) -> str:
        with self.lock:
            return ", ".join(f"{o.name} #{car}" + (" (away)" if o.away else "")
                             for car, o in sorted(self.owners.items())) or "none"
