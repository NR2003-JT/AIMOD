"""Pit stop history, auto-saved as CSV (one file per session) in logs/.

A stop is written when it finishes, so the row carries the running position
before and after the stop, the time stopped in the box
and the pit road time (game seconds). Stops still running when the
session ends are written without them.
"""
from __future__ import annotations

import csv
import os
import re
import time

from .strategy import PitEvent

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
    def __init__(self, directory: str):
        self.dir = directory
        self.pending: dict[int, tuple[PitEvent, str]] = {}
        self._file: str | None = None
        self._track = ""

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
            self._write(row_for(ev, track), track)
        self.pending.clear()

    def _write(self, row: list, track: str):
        if self._file is None:
            os.makedirs(self.dir, exist_ok=True)
            safe = re.sub(r"[^\w.-]+", "_", track or "unknown")
            self._file = os.path.join(self.dir, time.strftime("pitlog_%Y%m%d_%H%M%S_") + safe + ".csv")
            with open(self._file, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(COLUMNS)
        with open(self._file, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(row)
