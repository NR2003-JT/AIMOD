"""Lap pace per car, tire falloff per stint, and wear thresholds learned from them.

Only clean green-flag laps count: no caution during the lap, not an in- or
out-lap, and within a sane range of the field's typical lap. Per car:
  pace     = median of the last 3 clean laps
  baseline = median of the first clean laps of the stint (stint laps 2-4)
  falloff  = (pace - baseline) / baseline
A tire factor turns "how worn" into "how much is it actually costing": a worn
car that keeps pace with the cars around it is less likely to pit for tires, a
car falling off more likely.

Learned thresholds: the wear of each side at the end of every long green run
(the car pitted, or the session ended) is recorded; runs cut short by a caution
are left out (shorter than 80 % of the track's long runs). With enough runs the
median is offered as the track's wear threshold - the wear cars really reach
on a full run, which is what the threshold means. (The point where a run has
lost the "worn" amount of lap time is still found, for the log; it comes much
earlier in a run, so it gave thresholds that were far too low.)
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

MIN_SAMPLES = 5
MIN_RUN_LAPS = 8


def _fit(xs, ys) -> tuple[float, float]:
    """Robust line (Theil-Sen): median of pairwise slopes, so one slow lap in traffic or a
    lapped car doesn't bend it. Returns (slope, intercept)."""
    slopes = [(ys[j] - ys[i]) / (xs[j] - xs[i]) for i in range(len(xs)) for j in range(i + 1, len(xs))
              if xs[j] != xs[i]]
    if not slopes:
        return 0.0, statistics.median(ys)
    b = statistics.median(slopes)
    return b, statistics.median(y - b * x for x, y in zip(xs, ys))


def clamp(x, lo=0.0, hi=1.0):
    return lo if x < lo else hi if x > hi else x


@dataclass
class Stint:
    start: int                                  # car lap the stint started (last stop lap)
    laps: list = field(default_factory=list)    # (stint lap, lap time, wear_l, wear_r)
    worn_logged: bool = False
    reported: bool = False
    learn: bool = True                          # False: a run a person planned - kept out of what AIMOD learns

    @property
    def baseline(self) -> float | None:
        early = [t for n, t, *_ in self.laps if 2 <= n <= 5][:3]
        return statistics.median(early) if len(early) >= 2 else None


class PaceTracker:
    def __init__(self, worn_pct: float = 1.0):
        self.worn_pct = worn_pct                # lap time loss (% of the early-run pace) that means "worn"
        self.stints: dict[int, Stint] = {}
        self.worn_points: list[tuple[float, float]] = []   # (wear_l, wear_r) when a run lost `worn_pct`
        self.run_ends: list[tuple[float, float, int]] = []  # (wear_l, wear_r, run laps) at the end of each run
        self.finished: list[tuple[float, int]] = []    # (falloff slope s/lap per lap of tire age, laps) per run

    def reset(self):
        self.stints.clear()
        self.worn_points.clear()
        self.run_ends.clear()

    def lap(self, idx: int, stint_start: int, car_lap: int, lap_time: float, clean: bool,
            wear_l: float, wear_r: float, typical: float, learn: bool = True):
        st = self.stints.get(idx)
        if st is None or st.start != stint_start:
            if st is not None:
                self._finish(st)                        # previous run is over: report its falloff
            st = self.stints[idx] = Stint(stint_start)
        if not learn:
            st.learn = False
        n = car_lap - stint_start
        if not clean or n < 2 or lap_time <= 0:
            return
        if typical > 0 and not (0.85 * typical <= lap_time <= 1.25 * typical):
            return                              # wreck avoidance, slowed by a crash, etc.
        st.laps.append((n, lap_time, wear_l, wear_r))
        st.laps = st.laps[-40:]
        if not st.worn_logged and st.learn:
            hit = self._trend_crossing(st)
            if hit is not None:
                st.worn_logged = True
                self.worn_points.append(hit)
                self.worn_points = self.worn_points[-200:]

    def _trend_crossing(self, st: Stint) -> tuple[float, float] | None:
        """Fit lap time against laps on the tires over the whole run (robust to one slow lap in
        traffic); once the fitted line has lost `worn_pct` of the early-run pace, return the wear
        of each side at that point (also from fitted lines)."""
        if len(st.laps) < MIN_RUN_LAPS:
            return None
        ns = [x[0] for x in st.laps]
        b_t, a_t = _fit(ns, [x[1] for x in st.laps])
        if b_t <= 0:
            return None
        n0 = ns[0]
        base = a_t + b_t * n0
        target = base * self.worn_pct / 100.0
        n_star = n0 + target / b_t
        if n_star > ns[-1] - 2:
            return None                                  # not (clearly) worn yet on the trend
        b_l, a_l = _fit(ns, [x[2] for x in st.laps])
        b_r, a_r = _fit(ns, [x[3] for x in st.laps])
        return (a_l + b_l * n_star, a_r + b_r * n_star)

    def _finish(self, st: Stint):
        if st.reported or len(st.laps) < MIN_RUN_LAPS or not st.learn:
            return
        st.reported = True
        slope, _ = _fit([x[0] for x in st.laps], [x[1] for x in st.laps])
        self.finished.append((slope, len(st.laps)))
        n, _, wl, wr = st.laps[-1]
        self.run_ends.append((wl, wr, n))
        self.run_ends = self.run_ends[-200:]

    def flush(self):
        """Session over: report every run long enough to measure."""
        for st in self.stints.values():
            self._finish(st)

    def take_finished(self) -> list[tuple[float, int]]:
        out, self.finished = self.finished, []
        return out

    def run_slope(self, idx: int, skip: int = 0) -> float | None:
        """Falloff slope of the car's current run so far (s/lap per lap), once it has 6+ clean laps.
        skip: ignore the run's first laps (cars get faster for ~4 laps as the field spreads out)."""
        st = self.stints.get(idx)
        if st is None:
            return None
        laps = [x for x in st.laps if x[0] > skip]
        if len(laps) < 6:
            return None
        return _fit([x[0] for x in laps], [x[1] for x in laps])[0]

    def pace(self, idx: int) -> float | None:
        st = self.stints.get(idx)
        if st is None or len(st.laps) < 2:
            return None
        return statistics.median(t for _, t, *_ in st.laps[-3:])

    def falloff(self, idx: int) -> float | None:
        st = self.stints.get(idx)
        if st is None:
            return None
        base, pace = st.baseline, self.pace(idx)
        if base is None or pace is None or len(st.laps) < 4:
            return None
        return (pace - base) / base

    @property
    def samples(self) -> list[tuple[float, float, int]]:
        """The long runs (at least 80 % of the track's long-run length): wear at the end of a full run."""
        if not self.run_ends:
            return []
        lens = sorted(r[2] for r in self.run_ends)
        long_run = lens[min(len(lens) - 1, int(0.9 * len(lens)))]
        return [r for r in self.run_ends if r[2] >= 0.8 * long_run]

    def learned(self) -> tuple[float, float, int] | None:
        """(left, right, runs) median wear at the end of a long green run, once there is enough data."""
        runs = self.samples
        if len(runs) < MIN_SAMPLES:
            return None
        return (statistics.median(r[0] for r in runs), statistics.median(r[1] for r in runs), len(runs))


def tire_factor(delta: float | None, falloff: float | None, lap_time: float) -> float:
    """Multiplier on tire-change likelihood. delta = seconds slower than the cars around it
    (negative = faster); falloff = fraction slower than its own early run. ~0.6 when keeping
    pace on worn tires, 1.0 with no data, up to 1.4 when clearly falling off."""
    if delta is None and falloff is None:
        return 1.0
    d = (delta or 0.0) / lap_time * 100 if lap_time > 0 else 0.0      # % slower than neighbours
    f = (falloff or 0.0) * 100                                          # % slower than its early run
    return clamp(0.6 + 0.35 * max(d, 0.0) + 0.25 * max(f - 0.5, 0.0), 0.6, 1.4)
