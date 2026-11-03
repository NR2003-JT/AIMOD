"""Tire model per NR2003 mod + track: how much lap time tires lose with age, and what
each kind of pit service costs. Feeds the value-based tire calls in strategy.py.

Why per mod + track: the mod brings the physics and tire model, and track.ini
edits change wear again, so numbers from one setup must never drive another.
Falloff is measured in seconds per lap of tire age (not per unit of NR2003's
wear value), which means the same thing whatever the mod's wear scale.

Learning: every clean green-flag run of 8+ laps (practice and race) gives a
falloff slope; every stop gives a service time per service type. The saved
model for this mod + track is a prior worth at most PRIOR_CAP runs; this
session's runs are added on top, so the current session soon dominates. If the
session clearly disagrees with the saved model (other physics, tire edit,
conditions) the prior is cut back at once.
"""
from __future__ import annotations

import json
import logging
import os
import statistics
import time

log = logging.getLogger("aimod.tires")

PRIOR_CAP = 8                    # saved runs count at most this much against this session
DISAGREE = 2.0                   # session slope this many times off the prior -> conditions changed
DEFAULT_STOP = {"F": 7.0, "2": 9.5, "4": 14.0}      # seconds in the box, until measured
DEFAULT_GREEN_LOSS = 25.0        # seconds a green-flag stop costs, until measured
MIN_RUNS = 3                     # runs needed before the model is used


def service_class(code: str) -> str | None:
    """'F' fuel only, '2' two tires, '4' four tires (with or without fuel); None = repairs etc."""
    if code.startswith("D+"):
        return None
    tires = code.split("+")[-1]
    if tires == "L/R":
        return "4"
    if tires in ("L", "R"):
        return "2"
    if code == "F":
        return "F"
    return None


class TireModel:
    def __init__(self, path: str):
        self.path = path
        self.key = ""
        self.prior: dict = {}
        self.runs: list[dict] = []          # this session: {"slope", "laps", "practice"}
        self.stops: dict[str, list[float]] = {"F": [], "2": [], "4": []}
        self.green_losses: list[float] = []
        self._db = self._load()

    # ------------------------------------------------------------- storage
    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save(self):
        if not self.key:
            return
        self._db[self.key] = self.merged()
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._db, f, indent=2)
        except OSError as e:
            log.warning("Could not save tire model: %s", e)

    def use(self, mod: str, track: str, seed_mod: str | None = None, seed_tire_scale: float = 1.0):
        """Switch to the model for this mod + track (saving the current one first). seed_mod: a model to take
        the tire falloff from when this one is new (the modern pit crew keeps its own stop times, but the
        tires are the same)."""
        key = f"{mod or 'unknown mod'}|{track}"
        if key == self.key:
            return
        if self.key:
            self.save()
        self.key = key
        self.prior = dict(self._db.get(key) or {})
        if not self.prior and seed_mod is not None:
            seed = self._db.get(f"{seed_mod or 'unknown mod'}|{track}") or {}
            if seed.get("slope") is not None:
                self.prior = {"slope": seed["slope"], "runs": seed.get("runs", 0)}
            st = seed.get("stops") or {}
            if st.get("4") and seed.get("green_loss"):
                # first guess until this crew's own stops are measured: tire changes x seed_tire_scale
                stops = {c: round(v * (seed_tire_scale if c in ("2", "4") else 1.0), 2) for c, v in st.items()}
                self.prior.update(stops=stops, green_loss=round(seed["green_loss"] - (st["4"] - stops["4"]), 1))
        self.runs, self.green_losses = [], []
        self.stops = {"F": [], "2": [], "4": []}
        if self.prior:
            log.info("Tire model for %s: %.3f s/lap per lap of tire age from %d runs", key.replace("|", " @ "),
                     self.prior.get("slope") or 0.0, self.prior.get("runs") or 0)
        else:
            log.info("No tire model yet for %s - learning from practice and race runs", key.replace("|", " @ "))

    # ------------------------------------------------------------- learning
    def add_run(self, slope: float, laps: int, practice: bool):
        if laps < 8 or not (-0.05 < slope < 2.0):
            return
        self.runs.append({"slope": slope, "laps": laps, "practice": practice})

    def add_stop(self, code: str, box_time: float, road_time: float, green: bool, lap_time: float):
        cls = service_class(code)
        if cls and box_time > 0.5:
            self.stops[cls].append(box_time)
        if green and road_time > 5 and lap_time > 0:
            # time lost to a green-flag stop ~ pit road time minus what that stretch takes at speed
            # (pit road is roughly a fifth of a lap on ovals), plus the service itself is included
            self.green_losses.append(max(5.0, road_time - 0.2 * lap_time))

    # ------------------------------------------------------------- estimates
    def _prior_weight(self) -> float:
        if self.prior.get("slope") is None:
            return 0                          # saved before it had learned anything
        n = min(self.prior.get("runs", 0), PRIOR_CAP)
        p = self.prior.get("slope")
        if n and p and len(self.runs) >= 2:
            s = statistics.median(r["slope"] for r in self.runs)
            if p > 0 and s > 0 and (s / p > DISAGREE or p / s > DISAGREE):
                n = min(n, 1)                 # this session behaves differently: trust it
        return n

    @property
    def slope(self) -> float | None:
        """Seconds per lap lost for each lap the tires have run."""
        w = self._prior_weight()
        vals = [r["slope"] for r in self.runs]
        if not vals and not w:
            return None
        sess = statistics.median(vals) if vals else 0.0
        total = w + len(vals)
        if total < MIN_RUNS:
            return None
        return (((self.prior.get("slope") or 0.0) * w) + sess * len(vals)) / total

    def stop_time(self, cls: str) -> float:
        vals = self.stops.get(cls) or []
        prior = (self.prior.get("stops") or {}).get(cls)
        if len(vals) >= 3:
            return statistics.median(vals)
        if prior:
            return prior
        return statistics.median(vals) if vals else DEFAULT_STOP[cls]

    @property
    def green_loss(self) -> float:
        if len(self.green_losses) >= 3:
            return statistics.median(self.green_losses)
        return self.prior.get("green_loss") or (statistics.median(self.green_losses) if self.green_losses
                                                 else DEFAULT_GREEN_LOSS)

    @property
    def ready(self) -> bool:
        return self.slope is not None

    def merged(self) -> dict:
        slope = self.slope
        return {"slope": round(slope, 5) if slope is not None else self.prior.get("slope"),
                "runs": min(99, int(min(self.prior.get("runs", 0), PRIOR_CAP) + len(self.runs))),
                "stops": {c: round(self.stop_time(c), 2) for c in ("F", "2", "4")},
                "green_loss": round(self.green_loss, 1),
                "updated": time.strftime("%Y-%m-%d")}

    def summary(self) -> str:
        if not self.key:
            return "Tire model: waiting for a session"
        mod, _, track = self.key.partition("|")
        practice = sum(1 for r in self.runs if r["practice"])
        head = f"{track} ({mod})"
        if self.slope is None:
            n = len(self.runs) + min(self.prior.get("runs", 0), PRIOR_CAP)
            return f"Tire model - {head}: learning ({n}/{MIN_RUNS} runs)"
        return (f"Tire model - {head}: {self.slope:.3f} s/lap per lap on tires "
                f"({len(self.runs)} runs this session, {practice} in practice; saved {self.prior.get('runs', 0)}). "
                f"Stops: 4 tires {self.stop_time('4'):.1f}s, 2 tires {self.stop_time('2'):.1f}s, "
                f"fuel {self.stop_time('F'):.1f}s; green stop costs ~{self.green_loss:.0f}s")
