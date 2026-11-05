"""Pit stop history, auto-saved as CSV (one file per session) in logs/.

A stop is written when it finishes, so the row carries the running position
before and after the stop, the time stopped in the box
and the pit road time (game seconds). Stops still running when the
session ends are written without them.
"""
from __future__ import annotations

import csv
import logging
import os
import re
import time

from .strategy import PitEvent

log = logging.getLogger("aimod.pitlog")

COLUMNS = ["time", "lap", "car", "driver", "stop", "service", "code", "fuel", "reason", "flag",
           "pos_in", "pos_out", "gained", "service_time", "pit_road_time", "request_to_exit",
           "damage_in", "damage_out", "note", "track", "fuel_box_in_pct", "fuel_box_out_pct", "fuel_added_pct",
           "adjustment"]


def gained(ev: PitEvent):
    if ev.pos_in and ev.pos_out:
        return ev.pos_in - ev.pos_out
    return ""


def _pct(v, cap) -> str:
    return f"{100 * v / cap:.1f}" if v is not None and cap else ""


def row_for(ev: PitEvent, track: str) -> list:
    return [time.strftime("%H:%M:%S", time.localtime(ev.time)), ev.lap, ev.number, ev.name,
            ev.stop_no, ev.service, ev.code, "yes" if ev.fuel else "no", ev.reason,
            "GREEN" if ev.green else "CAUTION", ev.pos_in or "", ev.pos_out or "", gained(ev),
            f"{ev.box_time:.1f}" if ev.box_time > 0.5 else "",
            f"{ev.road_time:.1f}" if ev.road_time > 0.5 else "",
            f"{ev.duration:.1f}" if ev.duration is not None else "",
            f"{ev.dmg_in:.3f}" if ev.dmg_in is not None else "",
            f"{ev.dmg_out:.3f}" if ev.dmg_out is not None else "", ev.note, track,
            _pct(ev.fuel_box_in, ev.fuel_cap), _pct(ev.fuel_box_out, ev.fuel_cap),
            _pct(ev.fuel_box_out - ev.fuel_box_in, ev.fuel_cap)
            if ev.fuel_box_in is not None and ev.fuel_box_out is not None else "",
            ev.adjust.short if ev.adjust is not None else ""]


class PitLog:
    """Never raises from a write: a CSV that can't be written (open in Excel, which locks it) keeps its rows in
    a backlog that is tried again on the next write, retry() or flush(), so the strategy tick never sees it."""
    RETRY_S = 5.0

    def __init__(self, directory: str, clock=time.monotonic):
        self.dir = directory
        self.pending: dict[int, tuple[PitEvent, str]] = {}
        self._file: str | None = None
        self._track = ""
        self._backlog: list[tuple[str, list]] = []    # (csv path, row) not written yet
        self._clock = clock
        self._last_try = float("-inf")
        self._warned = False

    def new_session(self):
        self.flush()
        self._file = None

    def start(self, ev: PitEvent, track: str) -> list:
        self.pending[id(ev)] = (ev, track)
        return row_for(ev, track)

    def finish(self, ev: PitEvent) -> list | None:
        item = self.pending.pop(id(ev), None)
        if item is None:
            return None
        row = row_for(ev, item[1])
        self._write(row, item[1])
        return row

    def flush(self):
        for ev, track in list(self.pending.values()):
            self._queue(row_for(ev, track), track)
        self.pending.clear()
        self._drain()

    @property
    def unwritten(self) -> int:
        return len(self._backlog)

    def retry(self):
        """Try the backlog again (cheap when it's empty; at most every RETRY_S while the file stays locked)."""
        if self._backlog and self._clock() - self._last_try >= self.RETRY_S:
            self._drain()

    def _write(self, row: list, track: str):
        self._queue(row, track)
        self._drain()

    def _queue(self, row: list, track: str):
        if self._file is None:
            safe = re.sub(r"[^\w.-]+", "_", track or "unknown")
            self._file = os.path.join(self.dir, time.strftime("pitlog_%Y%m%d_%H%M%S_") + safe + ".csv")
        self._backlog.append((self._file, row))

    def _drain(self):
        self._last_try = self._clock()
        left: list[tuple[str, list]] = []
        stuck: set[str] = set()                       # files that failed this pass (rows stay in order)
        err = None
        for path, row in self._backlog:
            if path in stuck:
                left.append((path, row))
                continue
            try:
                os.makedirs(self.dir, exist_ok=True)
                new = not os.path.exists(path)
                with open(path, "a", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    if new:
                        w.writerow(COLUMNS)
                    w.writerow(row)
            except OSError as e:
                stuck.add(path)
                left.append((path, row))
                err = e
        self._backlog = left
        if left and not self._warned:
            self._warned = True
            log.warning("Pit log: can't write %s (%s) - is it open in another program? %d row(s) kept, "
                        "tried again every %.0f s", os.path.basename(left[0][0]), err, len(left), self.RETRY_S)
        elif not left and self._warned:
            self._warned = False
            log.info("Pit log: held rows written")
