"""Crew chief chassis adjustments on AI pit stops (decided, logged, voiced, and applied to the car's handling).

Every AI car starts from the track's AI setup (aisetup.py). At a stop the crew chief looks at how the car is
running against the field and may adjust it:
  - balance from tire wear: right-front wearing faster than the field's balance -> tight (pushing);
    the rears wearing faster -> loose
  - how fast its lap times fall off over a run, against the field's falloff
  - laps to go (grille tape for a short run to the finish)
Fixes: wedge (5 lb steps; a "round" = 10 lb here), rear track bar (quarter-inch steps), right-rear air pressure
(half-pound steps), grille tape (5 % steps), each kept inside the game's garage limits. A car running well gets
no changes. The crew rating (aiParamPitcrewSpeed/Consistency from the .car file) decides how often the call is
right; a wrong call overshoots. Recorded for the radio and the pit log; the handling effect is written by app.py.

Crew chief on a phone (beta): human() makes the changes a person asked for instead. Whether they help depends on
how the car really handles (diagnose()): the right fix in the right size helps, too much overshoots, the wrong
way hurts. Same garage limits and asymmetric chassis caps as AIMOD's own calls.
"""
from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, field

from .aisetup import LIMITS

TIGHT, LOOSE = "tight", "loose"
CHASSIS_STEP = 0.12     # chassis rating change per step of adjustment (about 0.02 s/lap)
CHASSIS_CAP = 0.5       # maximum positive change per car (about 0.08 s/lap)
CHASSIS_NEGATIVE_SCALE = 0.5  # setbacks from adjustments are half-strength
CHASSIS_NEGATIVE_CAP = 0.15   # maximum cumulative negative change per car
TOUCH_REL = 0.02        # balance this far off the field's (but inside the 6 % "clearly tight/loose" band) ...
TOUCH_CHANCE = 0.35     # ... gets a one-step "touch" adjustment this often under caution (no pace data needed)
FEEL = 0.02             # chassis change a driver can feel ("that helped" / "that made it worse")
DRIVER_SAYS = {
    TIGHT: ("He's tight in the center, can't get it to turn.", "Pushing up off two.",
            "Tight, tight - it won't turn in the middle."),
    LOOSE: ("He's loose off, can't put the power down.", "Real free in, loose on exit.",
            "Rear's coming around on him off the corner.")}


@dataclass
class Adjustment:
    kind: str                 # "wedge" | "track_bar" | "pressure" | "tape" | ""
    amount: float             # in the garage unit (lb, in, psi, %)
    short: str                # pit log / board: "wedge +10 lb"
    crew: str                 # crew chief to the driver
    driver: str = ""          # what the driver reported first (may be empty)
    diagnosis: str = ""       # tight / loose / falling off / short run
    good_call: bool = True    # False = the crew misjudged it (overshoot)
    extra_s: float = 0.0      # extra time in the box (phase 2)
    effect: float = 0.0       # change to the car's chassis rating (handling) this adjustment makes


@dataclass
class CarSetup:
    wedge: float = 0.0        # change from the AI setup
    track_bar: float = 0.0
    rr_pressure: float = 0.0
    tape: float = 0.0
    chassis: float = 0.0      # total change to the chassis rating from the crew's adjustments
    history: list = field(default_factory=list)


def _round_words(lb: float) -> str:
    r = abs(lb) / 10.0
    if r <= 0.5:
        return "half a round"
    if r <= 1.0:
        return "a round"
    if r <= 1.5:
        return "a round and a half"
    return f"{r:g} rounds"


def _chassis_effect(current: float, requested: float) -> float:
    """Apply a handling change with restrained downside and an unchanged upside."""
    if requested < 0:
        requested *= CHASSIS_NEGATIVE_SCALE
    target = max(-CHASSIS_NEGATIVE_CAP, min(CHASSIS_CAP, current + requested))
    return round(target - current, 3)


class CrewChief:
    def __init__(self, rng: random.Random | None = None):
        self.rng = rng or random.Random()
        self.base: dict | None = None             # the AI setup for this track
        self.cars: dict[int, CarSetup] = {}

    def reset(self, base: dict | None):
        self.base = base
        self.cars = {}

    def state(self, idx: int) -> CarSetup:
        return self.cars.setdefault(idx, CarSetup())

    # ------------------------------------------------------------- diagnosis
    @staticmethod
    def balance(car) -> float | None:
        """Right-front wear over the rears' average (>1: the front is working harder = tight)."""
        rear = (car.wear_rr + car.wear_lr) / 2
        if rear <= 0.02 or car.wear_rf <= 0.02:
            return None
        return car.wear_rf / rear

    def field_balance(self, cars) -> float | None:
        vals = [b for b in (self.balance(c) for c in cars if c.valid and c.controlled) if b]
        return statistics.median(vals) if len(vals) >= 5 else None

    def diagnose(self, car, cars, falloff: float | None, field_falloff: float | None) -> tuple[str, float]:
        """(tight | loose | '' , severity 0..1)."""
        b, fb = self.balance(car), self.field_balance(cars)
        sev_fall = 0.0
        if falloff is not None and field_falloff and field_falloff > 0:
            sev_fall = max(0.0, min(1.0, (falloff / field_falloff - 1.0) / 0.6))
        if b is None or fb is None:
            return "", sev_fall
        rel = b / fb
        if rel >= 1.06:
            return TIGHT, max(sev_fall, min(1.0, (rel - 1.0) / 0.2))
        if rel <= 0.94:
            return LOOSE, max(sev_fall, min(1.0, (1.0 - rel) / 0.2))
        return "", sev_fall

    # ------------------------------------------------------------- the call
    def call(self, car, cars, to_go: float, tank: float, falloff: float | None = None,
             field_falloff: float | None = None, crew: float | None = None, tires: bool = True,
             green: bool = False) -> Adjustment | None:
        if self.base is None:
            return None
        st = self.state(car.idx)
        handling, sev = self.diagnose(car, cars, falloff, field_falloff)
        short_run = 0 < to_go <= max(15.0, 0.4 * tank)
        if not handling and sev < 0.25:
            if short_run and st.tape < 10 and tires:
                return self._tape(st, +10, "short run to the finish")
            # a touch: slightly off the field's balance. Real crews tweak at most caution stops on the driver's
            # word; without this, a track where every car wears evenly (or a session with no lap-time data yet,
            # e.g. AIMOD restarted mid-race) got no adjustments at all
            b, fb = self.balance(car), self.field_balance(cars)
            if green or not b or not fb or abs(b / fb - 1.0) < TOUCH_REL or self.rng.random() >= TOUCH_CHANCE:
                return None
            handling, sev = (TIGHT if b > fb else LOOSE), 0.3
        if green and sev < 0.5:
            return None                                   # a green stop: only worth the time when it's bad
        if not handling:
            handling = self.rng.choice((TIGHT, LOOSE))    # falling off, balance unclear: the driver's call
        # crew rating ~0.8 (weak) .. 2.0 (strong): how often the call is right
        good = self.rng.random() < (0.55 + 0.2 * max(0.0, min(2.0, (crew or 1.4) - 0.8)))
        fix = handling if good else (LOOSE if handling == TIGHT else TIGHT)   # wrong read = wrong way
        options = ["wedge", "track_bar"] + (["pressure"] if tires else [])
        kind = self.rng.choice(options)
        steps = 1 + (sev > 0.5) + (sev > 0.85)
        adj = {"wedge": self._wedge, "track_bar": self._track_bar, "pressure": self._pressure}[kind](st, fix, steps)
        if adj is None:
            return None
        adj.diagnosis, adj.good_call = handling, good
        # the handling result: NR2003's AI reads chassis + consistency live in its car-control code (live test
        # 2026-10-18: halving both cost ~0.27 s/lap at Charlotte). A good call helps, a misread hurts.
        size = CHASSIS_STEP * steps * (0.75 + 0.5 * self.rng.random())
        want = size if good else -size
        adj.effect = _chassis_effect(st.chassis, want)
        st.chassis += adj.effect
        adj.driver = self.rng.choice(DRIVER_SAYS[handling])
        st.history.append(adj.short)
        return adj

    # ------------------------------------------------------------- a person's call (crew chief on a phone)
    def human(self, car, cars, req, to_go: float, tank: float, falloff: float | None = None,
              field_falloff: float | None = None, tires: bool = True) -> Adjustment | None:
        """The changes a person asked for (req: humancalls.HumanAdjust, steps). None = no changes made
        (all zero, or nothing left inside the garage limits)."""
        if self.base is None:
            return None
        st = self.state(car.idx)
        handling, sev = self.diagnose(car, cars, falloff, field_falloff)
        step = lambda v: max(-3, min(3, int(v or 0)))   # the phone's limit, kept here too
        net = 0.0                                         # balance steps made: + tighter, - freer
        shorts, says, sizes = [], [], []
        w = self._clamp("wedge", self.base["wedge"], st.wedge, 5 * step(req.wedge))
        if w:
            st.wedge += w
            net += w / 5
            shorts.append(f"wedge {w:+g} lb")
            says.append(f"{'put in' if w > 0 else 'take out'} {_round_words(w)} of wedge")
            sizes.append(("wedge", w, abs(w) / 5))
        b = self._clamp("track_bar", self.base["track_bar"][1], st.track_bar, 0.25 * step(req.bar))
        if b:
            st.track_bar += b
            net -= b / 0.25
            q = {0.25: "a quarter", 0.5: "a half", 0.75: "three quarters"}.get(abs(b), f"{abs(b):g}")
            shorts.append(f"track bar {b:+g} in")
            says.append(f"move the track bar {'up' if b > 0 else 'down'} {q}")
            sizes.append(("track_bar", b, abs(b) / 0.25))
        a = self._clamp("pressure", self.base["pressure"][3], st.rr_pressure,
                        0.5 * step(req.air)) if tires else 0.0
        if a:
            st.rr_pressure += a
            net -= a / 0.5
            words = {0.5: "half a pound", 1.0: "a pound", 1.5: "a pound and a half"}.get(abs(a), f"{abs(a):g} pounds")
            shorts.append(f"RR air {a:+g} psi")
            says.append(f"{'put' if a > 0 else 'take'} {words} of air {'in' if a > 0 else 'out of'} the right rear")
            sizes.append(("pressure", a, abs(a) / 0.5))
        t = self._clamp("tape", self.base["tape"], st.tape, 5 * step(req.tape))
        if t:
            st.tape += t
            shorts.append(f"tape {t:+g}% ({self.base['tape'] + st.tape:g}%)")
            says.append(f"{'add' if t > 0 else 'pull off'} {abs(t):g} percent of tape")
            sizes.append(("tape", t, abs(t) / 5))
        if not sizes:
            return None
        # how much the balance change helps: the car wants `want` steps (tight: freer = -, loose: tighter = +)
        want = 0 if not handling else (1 + (sev > 0.5) + (sev > 0.85)) * (-1 if handling == TIGHT else 1)
        if not net:
            gain = 0.0
        elif not want:
            gain = -0.5 * abs(net)                        # a balanced car knocked off balance
        elif (net > 0) == (want > 0):
            helpful = min(abs(net), abs(want))
            gain = helpful - (abs(net) - helpful)         # the right way; too much overshoots
        else:
            gain = -abs(net)                              # the wrong way
        short_run = 0 < to_go <= max(15.0, 0.4 * tank)
        gain += (0.25 if short_run else -0.25) * (t / 5)  # tape: speed on a short run, heat on a long one
        size = CHASSIS_STEP * gain * (0.75 + 0.5 * self.rng.random()) if gain else 0.0
        effect = _chassis_effect(st.chassis, size)
        st.chassis += effect
        kind, amount, _ = max(sizes, key=lambda x: x[2])
        crew = "We're gonna " + (", ".join(says[:-1]) + " and " + says[-1] if len(says) > 1 else says[0]) + "."
        adj = Adjustment(kind, amount, ", ".join(shorts), crew[0].upper() + crew[1:],
                         driver=self.rng.choice(DRIVER_SAYS[handling]) if handling else "",
                         diagnosis=handling or "crew chief's call", good_call=effect >= 0, effect=effect)
        st.history.append(adj.short)
        return adj

    # ------------------------------------------------------------- fixes (garage limits kept)
    def _clamp(self, kind: str, base: float, cur: float, delta: float) -> float:
        lo, hi, _ = LIMITS[kind]
        return max(lo - base, min(hi - base, cur + delta)) - cur

    def _wedge(self, st: CarSetup, fix: str, steps: int) -> Adjustment | None:
        lb = 5 * steps * (1 if fix == LOOSE else -1)      # more wedge tightens, less frees it up
        lb = self._clamp("wedge", self.base["wedge"], st.wedge, lb)
        if not lb:
            return None
        st.wedge += lb
        verb = "put in" if lb > 0 else "take out"
        why = "tighten him up" if lb > 0 else "free him up"
        return Adjustment("wedge", lb, f"wedge {lb:+g} lb",
                          f"We're gonna {verb} {_round_words(lb)} of wedge, should {why}.")

    def _track_bar(self, st: CarSetup, fix: str, steps: int) -> Adjustment | None:
        inch = 0.25 * steps * (1 if fix == TIGHT else -1)   # raising the rear track bar frees the car up
        base = self.base["track_bar"][1]
        inch = self._clamp("track_bar", base, st.track_bar, inch)
        if not inch:
            return None
        st.track_bar += inch
        way = "up" if inch > 0 else "down"
        q = {0.25: "a quarter", 0.5: "a half", 0.75: "three quarters"}.get(abs(inch), f"{abs(inch):g}")
        why = "free you up" if inch > 0 else "tighten you up"
        return Adjustment("track_bar", inch, f"track bar {inch:+g} in",
                          f"Track bar {way} {q} of an inch, that'll {why}.")

    def _pressure(self, st: CarSetup, fix: str, steps: int) -> Adjustment | None:
        psi = 0.5 * steps * (1 if fix == TIGHT else -1)     # air in the right rear frees the car up
        base = self.base["pressure"][3]
        psi = self._clamp("pressure", base, st.rr_pressure, psi)
        if not psi:
            return None
        st.rr_pressure += psi
        words = {0.5: "half a pound", 1.0: "a pound", 1.5: "a pound and a half"}.get(abs(psi), f"{abs(psi):g} pounds")
        verb = "in" if psi > 0 else "out of"
        return Adjustment("pressure", psi, f"RR air {psi:+g} psi",
                          f"Four tires, {words} of air {verb} the right rear.")

    def _tape(self, st: CarSetup, pct: float, why: str) -> Adjustment | None:
        pct = self._clamp("tape", self.base["tape"], st.tape, pct)
        if not pct:
            return None
        st.tape += pct
        total = self.base["tape"] + st.tape
        return Adjustment("tape", pct, f"tape {pct:+g}% ({total:g}%)",
                          f"We're adding tape for the run to the finish, {total:g} percent on the grille.",
                          diagnosis=why)


def human_status(adj: Adjustment | None, req=None, tires: bool = True) -> str:
    """what the phone shows after the stop"""
    if adj is None:
        asked = req is not None and any(getattr(req, k, 0) for k in ("wedge", "bar", "tape"))
        if asked:
            return "no changes: at the garage limit"
        if req is not None and getattr(req, "air", 0):
            return "no changes: no tires, so no air change" if not tires else "no changes: at the garage limit"
        return "no changes"
    feel = ("the driver says that helped" if adj.effect >= FEEL else
            "the driver says that made it worse" if adj.effect <= -FEEL else "the driver can't feel much change")
    return f"done: {adj.short}; {feel}"
