"""Race results, saved when a race ends (results/ next to the exe): one .json with everything and one .html to read.

Read-only. It is built from the lap line crossings NR2003 stamps in its scoring table (the same table the lap times
and gaps come from elsewhere in AIMOD), so any order, lead, lap time or stage result is worked out from "who
completed lap n when":

  * running order at a moment T = laps completed by T (most first), then who crossed the line first;
  * laps led = the car first across the line at the end of each lap;
  * stage results = the running order when the leader completed the stage's last lap;
  * final order = the same, taken when the winner took the checkered flag; every car is scored on its first crossing
    after that (so a car a lap down finishes a lap down), and cooldown laps don't count;
  * fastest lap = the shortest crossing-to-crossing time (lap 1 and laps under 5 s are left out).

Points are NASCAR's 2026 Chase scale, worked out here, so treat them as unofficial: race 55 for the win, then 35, 34,
33 ... 2 (35th), 1 for 36th and below; stages 10 ... 1 for the top ten in every stage but the last; one bonus point
for the fastest lap, not for a car that was out of the race (the game can't show a car that went to the garage and
came back). There are no playoff points and no win-and-in in 2026, and the Chase seeding, reset and the season's
standings are more than one race, so they aren't part of this file. The starting grid is the running order when the first green flag flies, so it is missing when AIMOD
joins a race that is already running.

The file is written once the winner is home and the field has had a moment to finish (FINAL_HOLD_S), and again when
the session ends, AIMOD closes or the game goes away, but only if the content changed.
"""
from __future__ import annotations

import bisect
import html
import json
import logging
import os
import re
import time

from . import memory_map as mm

log = logging.getLogger("aimod.results")

FINAL_HOLD_S = 20.0           # wall seconds without a new line crossing after the winner finished before saving
MIN_LEADER_LAPS = 3           # a race shorter than this isn't worth a file
MIN_LAP_MS = 5000             # a "lap" shorter than this is a bad read, not a lap
STAGE_POINTS = 10             # the stage winner's points (10 ... 1 for the top ten)
FORMAT = 1
POINTS_SYSTEM = "NASCAR 2026 Chase"
WIN_POINTS = 55
FASTEST_LAP_POINTS = 1


def race_points(pos: int) -> int:
    """2026 race points: 55 for the win, then 35, 34 ... 2 (35th), 1 for 36th and below."""
    return WIN_POINTS if pos == 1 else max(1, 37 - pos)


def stage_points(pos: int) -> int:
    return max(0, STAGE_POINTS + 1 - pos)


class _Car:
    __slots__ = ("idx", "number", "name", "make", "ms", "out", "out_lap")

    def __init__(self, idx: int):
        self.idx = idx
        self.number = 0
        self.name = ""
        self.make = ""
        self.ms: dict[int, int] = {}        # lap -> game ms it completed that lap (0 = the start)
        self.out = 0                        # the game's reason-out code, 0 = running
        self.out_lap: int | None = None     # laps completed when it went out


class _Clock:
    """The crossings of one car as sorted lists, so "laps completed by T" is a bisect."""

    def __init__(self, ms: dict[int, int]):
        keys = sorted(k for k in ms if k >= 1)
        self.laps: list[int] = []
        self.times: list[int] = []
        self.by_lap: dict[int, int] = {}
        last = -1
        for k in keys:                      # (a torn read that goes backwards is skipped)
            if ms[k] > last:
                self.laps.append(k)
                self.times.append(ms[k])
                self.by_lap[k] = ms[k]
                last = ms[k]

    def at(self, t: float) -> tuple[int, int | None]:
        """(laps completed by game ms t, the time of the last of them)."""
        i = bisect.bisect_right(self.times, t)
        return (self.laps[i - 1], self.times[i - 1]) if i else (0, None)

    def first_at_or_after(self, t: float) -> tuple[int, int] | None:
        """(lap, ms) of the first crossing at or after t: the lap a car is scored on when the flag falls at t."""
        i = bisect.bisect_left(self.times, t)
        return (self.laps[i], self.times[i]) if i < len(self.times) else None

    @property
    def last(self) -> tuple[int, int] | None:
        return (self.laps[-1], self.times[-1]) if self.laps else None


class ResultsRecorder:
    def __init__(self, directory: str, history=None, clock=time.monotonic):
        """history(idx, laps) -> [ms of lap 0, 1 ... laps] from the game's table (fills laps AIMOD didn't see)."""
        self.dir = directory
        self.history = history
        self._clock = clock
        self.reset()

    # ------------------------------------------------------------------ state
    def reset(self):
        self.cars: dict[int, _Car] = {}
        self.track = ""
        self.mod = ""
        self.total = 0
        self.ot_pending = False
        self.stage_ends: tuple = ()
        self.cautions = 0
        self.caution_laps = 0
        self.stops: list = []
        self.grid: dict[int, int] | None = None     # slot -> starting position
        self._grid_done = False
        self.started_at: float | None = None        # wall time of the first snapshot, names the file
        self._base: str | None = None
        self._last_cross = self._clock()
        self._saved_final = False
        self._last_text: str | None = None
        self.last_file: str | None = None

    @property
    def active(self) -> bool:
        return bool(self.cars)

    # ----------------------------------------------------------------- update
    def update(self, snap, *, track: str = "", mod: str = "", total: int = 0, ot_pending: bool = False,
               stage_ends=(), cautions: int = 0, caution_laps: int = 0, stops=()):
        """Called every tick. Cheap: it only notes new line crossings."""
        if snap.session != mm.SESSION_RACE:
            return
        valid = [c for c in snap.cars if c.valid]
        leader = max((c.laps_done for c in valid), default=0)
        self.track, self.mod = track or self.track, mod or self.mod
        self.total, self.ot_pending = int(total or snap.laps_total or 0), ot_pending
        self.stage_ends = tuple(stage_ends or ())
        self.cautions, self.caution_laps, self.stops = cautions, caution_laps, list(stops or ())
        if self.started_at is None and leader >= 0 and valid:
            self.started_at = time.time()
        if not self._grid_done:
            self._grid(snap, valid, leader)
        for c in valid:
            if c.idx == mm.PACE_SLOT:
                continue
            r = self.cars.get(c.idx)
            if r is None:
                r = self.cars[c.idx] = _Car(c.idx)
            r.number, r.name, r.make = c.number, c.name, c.make or r.make
            n = c.laps_done
            if c.reason_out and not r.out:
                r.out, r.out_lap = c.reason_out, n
            if n < 1 or c.cross_ms is None:
                continue
            if n not in r.ms:
                if n >= 2 and (n - 1) not in r.ms and self.history is not None:
                    self._backfill(r, n)
                prev = c.prev_cross_ms
                if prev and (n - 1) not in r.ms:
                    r.ms[n - 1] = prev
                if c.cross_ms > r.ms.get(n - 1, -1):          # (a stale table read repeats the last lap's time)
                    r.ms[n] = c.cross_ms
                    self._last_cross = self._clock()
        self._maybe_save()

    def _backfill(self, r: _Car, n: int):
        try:
            row = self.history(r.idx, n) or []
        except Exception:
            row = []
        last = -1
        for k, v in enumerate(row):
            if v > last and k not in r.ms:
                r.ms[k] = v
            last = max(last, v)

    def _grid(self, snap, valid, leader: int):
        """Starting order = running order (laps + distance round the lap) at the first green of the race."""
        if snap.flag != mm.FLAG_GREEN:
            return
        self._grid_done = True
        if leader > 1 or len(valid) < 5:
            return                                           # joined a race already under way
        prog = {c.idx: (c.laps_done, round(c.lap_dist, 1)) for c in valid if c.idx != mm.PACE_SLOT}
        if len(set(prog.values())) < 0.6 * len(prog):
            return                                           # (distances don't separate the cars: no usable order)
        order = sorted(prog, key=lambda i: (-prog[i][0], -prog[i][1], i))
        self.grid = {idx: k + 1 for k, idx in enumerate(order)}

    # ------------------------------------------------------------- when to save
    def _maybe_save(self):
        if self._saved_final or self._finish_ms() is None:
            return
        if self._clock() - self._last_cross >= FINAL_HOLD_S:
            self._write()
            self._saved_final = True

    def finish(self, reason: str = ""):
        """The session is over (or AIMOD / the game is going away): save what there is, then forget it."""
        try:
            if self._worth_saving():
                self._write(reason)
        except Exception:
            log.exception("Race results could not be saved (%s)", reason or "end of session")
        self.reset()

    def _worth_saving(self) -> bool:
        laps = [max(c.ms, default=0) for c in self.cars.values()]
        return len(self.cars) >= 5 and max(laps, default=0) >= MIN_LEADER_LAPS

    def _finish_ms(self) -> int | None:
        """Game ms the winner took the flag (completed the scheduled distance), or None while the race runs."""
        if self.total <= 0 or self.ot_pending:
            return None
        ts = [c.ms[self.total] for c in self.cars.values() if self.total in c.ms]
        return min(ts) if ts else None

    # ------------------------------------------------------------------ results
    @staticmethod
    def _order_at(clocks: dict[int, _Clock], t: float) -> list[int]:
        def key(idx):
            laps, last = clocks[idx].at(t)
            return (-laps, last if last is not None else float("inf"), idx)
        return sorted(clocks, key=key)

    def build(self) -> dict:
        cars = {i: c for i, c in self.cars.items() if c.number > 0}
        clocks = {i: _Clock(c.ms) for i, c in cars.items()}
        fin = self._finish_ms()
        leader_laps = max((cl.laps[-1] for cl in clocks.values() if cl.laps), default=0)
        n_laps = min(self.total, leader_laps) if fin is not None else leader_laps

        # each car's scored laps and finishing time
        scored: dict[int, tuple[int, int | None]] = {}
        for i, cl in clocks.items():
            hit = cl.first_at_or_after(fin) if fin is not None else None
            scored[i] = hit if hit is not None else (cl.last or (0, None))
        out = {i: bool(c.out) and (fin is None or (c.out_lap or 0) < n_laps) for i, c in cars.items()}

        def class_key(i):
            laps, t = scored[i]
            return (-laps, out[i], (-(t or 0)) if out[i] else (t or 0), i)
        order = sorted(cars, key=class_key)
        winner = order[0] if order else None

        # leaders lap by lap, the order at each leader's line crossing (for average running position)
        led: dict[int, int] = {i: 0 for i in cars}
        lead_changes, prev_leader = 0, None
        rank_sum = {i: 0 for i in cars}
        rank_n = {i: 0 for i in cars}
        for n in range(1, n_laps + 1):
            ts = [(cl.by_lap[n], i) for i, cl in clocks.items() if n in cl.by_lap]
            if not ts:
                continue
            t, lead = min(ts)
            led[lead] += 1
            if prev_leader is not None and lead != prev_leader:
                lead_changes += 1
            prev_leader = lead
            for pos, i in enumerate(self._order_at(clocks, t), 1):
                if scored[i][0] >= n:
                    rank_sum[i] += pos
                    rank_n[i] += 1

        # fastest lap per car (lap, ms)
        best: dict[int, tuple[int, int]] = {}
        for i, c in cars.items():
            for k in range(2, scored[i][0] + 1):
                if k in c.ms and (k - 1) in c.ms:
                    dt = c.ms[k] - c.ms[k - 1]
                    if dt >= MIN_LAP_MS and (i not in best or dt < best[i][1]):
                        best[i] = (k, dt)

        # the fastest-lap point: the quickest counted lap of a car that never went out (ties: the earlier lap)
        bonus_car = min((i for i in best if not cars[i].out), key=lambda i: (best[i][1], best[i][0], i), default=None)

        # stages
        stages = []
        stage_pts: dict[int, int] = {i: 0 for i in cars}
        for s, end in enumerate(self.stage_ends, 1):
            ts = [cl.by_lap[end] for cl in clocks.values() if end in cl.by_lap]
            if not ts:
                continue
            rows = []
            for pos, i in enumerate(self._order_at(clocks, min(ts)), 1):
                pts = stage_points(pos)
                stage_pts[i] += pts
                rows.append({"pos": pos, "number": cars[i].number, "name": cars[i].name, "points": pts})
            stages.append({"stage": s, "end_lap": end, "winner": rows[0]["name"] if rows else "", "results": rows})

        stops_by: dict[int, list[int]] = {}
        for ev in self.stops:
            stops_by.setdefault(ev.number, []).append(1 if ev.green else 0)

        results = []
        for pos, i in enumerate(order, 1):
            c, (laps, t) = cars[i], scored[i]
            if pos == 1:
                interval = "LEADER"
            elif scored[order[0]][0] - laps > 0:
                interval = f"-{scored[order[0]][0] - laps}L"
            else:
                interval = f"-{(t - scored[order[0]][1]) / 1000:.3f}" if t and scored[order[0]][1] else ""
            start = self.grid.get(i) if self.grid else None
            sp = stage_pts[i]
            bp = FASTEST_LAP_POINTS if i == bonus_car else 0
            results.append({
                "pos": pos, "number": c.number, "name": c.name, "make": c.make,
                "start": start, "change": (start - pos) if start else None,
                "laps": laps, "interval": interval,
                "status": ("Out" if out[i] else "Running"),
                "out_code": c.out or None, "out_lap": c.out_lap if out[i] else None,
                "laps_led": led[i],
                "fastest_lap": ({"lap": best[i][0], "time": round(best[i][1] / 1000, 3)} if i in best else None),
                "avg_running_pos": round(rank_sum[i] / rank_n[i], 2) if rank_n[i] else None,
                "pit_stops": len(stops_by.get(c.number, ())),
                "green_pit_stops": sum(stops_by.get(c.number, ())),
                "race_points": race_points(pos), "stage_points": sp, "bonus_points": bp,
                "points": race_points(pos) + sp + bp,
            })
        fastest = min(((r["fastest_lap"]["time"], r) for r in results if r["fastest_lap"]), key=lambda x: x[0],
                      default=None)
        most_led = max(results, key=lambda r: r["laps_led"], default=None)
        movers = [r for r in results if r["change"] is not None]
        mover = max(movers, key=lambda r: r["change"], default=None)
        ms0 = [c.ms[0] for c in cars.values() if 0 in c.ms]
        duration = ((fin - min(ms0)) / 1000.0) if fin is not None and ms0 and min(ms0) > 0 else None
        return {
            "format": FORMAT,
            "generated_by": "AIMOD",
            "track": self.track, "series": self.mod,
            "date": time.strftime("%Y-%m-%d %H:%M", time.localtime(self.started_at or time.time())),
            "complete": fin is not None,
            "laps": {"scheduled": self.total, "run": n_laps},
            "duration_s": round(duration, 1) if duration else None,
            "cautions": {"count": self.cautions, "laps": self.caution_laps},
            "lead_changes": lead_changes,
            "leaders": sum(1 for v in led.values() if v),
            "winner": ({"number": cars[winner].number, "name": cars[winner].name} if winner is not None else None),
            "fastest_lap": ({"number": fastest[1]["number"], "name": fastest[1]["name"],
                             "lap": fastest[1]["fastest_lap"]["lap"], "time": fastest[0]} if fastest else None),
            "fastest_lap_bonus": ({"number": cars[bonus_car].number, "name": cars[bonus_car].name,
                                   "points": FASTEST_LAP_POINTS} if bonus_car is not None else None),
            "most_laps_led": ({"number": most_led["number"], "name": most_led["name"], "laps": most_led["laps_led"]}
                              if most_led and most_led["laps_led"] else None),
            "biggest_mover": ({"number": mover["number"], "name": mover["name"], "places": mover["change"]}
                              if mover and mover["change"] > 0 else None),
            "start_source": "running order at the first green flag" if self.grid else None,
            "points_system": POINTS_SYSTEM,
            "points_note": f"{POINTS_SYSTEM}, calculated by AIMOD (race 55-35-34..1, stages 10..1, fastest lap 1); "
                           "unofficial.",
            "results": results,
            "stages": stages,
        }

    # ------------------------------------------------------------------ files
    def _path(self) -> str:
        if self._base is None:
            safe = re.sub(r"[^\w.-]+", "_", self.track or "unknown")
            stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(self.started_at or time.time()))
            self._base = os.path.join(self.dir, f"results_{stamp}_{safe}")
        return self._base

    def _write(self, reason: str = ""):
        data = self.build()
        text = json.dumps(data, indent=1, ensure_ascii=False)
        if text == self._last_text:
            return
        base = self._path()
        os.makedirs(self.dir, exist_ok=True)
        for ext, body in ((".json", text), (".html", render_html(data))):
            tmp = base + ext + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                f.write(body)
            os.replace(tmp, base + ext)
        self._last_text = text
        self.last_file = base + ".html"
        log.info("Race results saved%s: %s (%d cars, %s)", f" ({reason})" if reason else "",
                 os.path.basename(base), len(data["results"]), "final" if data["complete"] else "race not finished")


# ------------------------------------------------------------------------ HTML
_CSS = ("body{font-family:Arial,sans-serif;font-size:14px;margin:20px}table{border-collapse:collapse;"
        "margin-bottom:24px}th,td{border:1px solid #ddd;padding:3px 8px}th{background:#f2f2f2;text-align:left}"
        "tr:nth-child(even){background:#f9f9f9}td.n,th.n{text-align:right}.stages{display:flex;gap:24px;"
        "flex-wrap:wrap}")


def _e(v) -> str:
    return "" if v is None else html.escape(str(v))


def _signed(v) -> str:
    return "" if v is None else ("--" if v == 0 else f"{v:+d}")


def render_html(d: dict) -> str:
    laps = d["laps"]
    out = [f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>Race Results</title><style>{_CSS}</style>"
           f"</head><body><h1>Race Results</h1>",
           f"<p>Track: {_e(d['track'])}<br>Series: {_e(d['series'])}<br>Date: {_e(d['date'])}<br>"
           f"Laps: {laps['run']}" + (f" of {laps['scheduled']}" if laps["scheduled"] else "")
           + ("" if d["complete"] else " (race not finished)") + "</p>"]
    if d["duration_s"]:
        s = int(d["duration_s"])
        out.append(f"<p>Length: {s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}</p>")
    out.append("<table><tr><th>Pos</th><th>#</th><th>Driver</th><th class=n>Points</th><th class=n>Stage Pts</th><th class=n>Bonus</th>"
               "<th>Interval</th><th>Status</th><th class=n>Laps</th><th class=n>Laps Led</th><th>Fastest Lap</th>"
               "<th class=n>Start</th><th class=n>+/-</th><th class=n>ARP</th><th class=n>Stops</th></tr>")
    for r in d["results"]:
        fl = r["fastest_lap"]
        fl_txt = f"{fl['time']:.3f} ({fl['lap']})" if fl else ""
        out.append(f"<tr><td>{r['pos']}</td><td>{_e(r['number'])}</td><td>{_e(r['name'])}</td>"
                   f"<td class=n>{r['points']}</td><td class=n>{r['stage_points'] or ''}</td>"
                   f"<td class=n>{r['bonus_points'] or ''}</td>"
                   f"<td>{_e(r['interval'])}</td><td>{_e(r['status'])}</td><td class=n>{r['laps']}</td>"
                   f"<td class=n>{r['laps_led'] or 0}</td>"
                   f"<td>{_e(fl_txt)}</td>"
                   f"<td class=n>{_e(r['start'])}</td><td class=n>{_signed(r['change'])}</td>"
                   f"<td class=n>{_e(r['avg_running_pos'])}</td><td class=n>{r['pit_stops']}</td></tr>")
    out.append("</table><p>")
    out.append(f"Cautions: {d['cautions']['count']} ({d['cautions']['laps']} laps)<br>"
               f"Lead changes: {d['lead_changes']} ({d['leaders']} leaders)<br>")
    for key, label, fmt in (
            ("fastest_lap", "Fastest lap", lambda x: f"{x['time']:.3f} on lap {x['lap']}"),
            ("fastest_lap_bonus", "Fastest lap point", lambda x: f"+{x['points']}"),
            ("most_laps_led", "Most laps led", lambda x: f"{x['laps']}"),
            ("biggest_mover", "Biggest mover", lambda x: f"+{x['places']}")):
        x = d[key]
        if x:
            out.append(f"{label}: {_e(x['name'])} (#{_e(x['number'])}) {_e(fmt(x))}<br>")
    out.append("</p>")
    if d["stages"]:
        out.append("<div class=stages>")
        for s in d["stages"]:
            out.append(f"<div><h3>Stage {s['stage']} (lap {s['end_lap']})</h3><table><tr><th>Pos</th><th>#</th>"
                       f"<th>Driver</th><th class=n>Pts</th></tr>")
            for r in s["results"]:
                out.append(f"<tr><td>{r['pos']}</td><td>{_e(r['number'])}</td><td>{_e(r['name'])}</td>"
                           f"<td class=n>{r['points'] or ''}</td></tr>")
            out.append("</table></div>")
        out.append("</div>")
    notes = [d["points_note"]]
    if d["start_source"]:
        notes.append(f"Start position: {d['start_source']}.")
    out.append("<p style='color:#666'>" + _e(" ".join(notes)) + "<br>Average running position (ARP) is taken at "
               "each lap's line crossing by the leader. Written by AIMOD.</p></body></html>")
    return "".join(out)
