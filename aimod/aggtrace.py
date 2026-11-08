"""Racing intensity & rivals trace: every change AIMOD decides (shadow or on), every contact and rival event, as a
CSV per race in app\\logs (aggression_<date>_<time>_<track>.csv). aimod.log keeps only the short lines."""
from __future__ import annotations

import csv
import datetime
import logging
import os
import re

log = logging.getLogger("aimod.aggression")

COLUMNS = ["time", "game_s", "lap", "event", "car", "driver", "own", "old", "new", "reasons", "mode", "gap", "other",
           "detail"]


class AggTrace:
    def __init__(self, folder: str):
        self.folder = folder
        self.path: str | None = None
        self._f = None
        self._w = None
        self._failed = False

    def open(self, track: str):
        if self._f is not None or self._failed:
            return
        try:
            os.makedirs(self.folder, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", track or "track")[:40]
            self.path = os.path.join(self.folder, f"aggression_{datetime.datetime.now():%Y%m%d_%H%M%S}_{safe}.csv")
            self._f = open(self.path, "w", newline="", encoding="utf-8")
            self._w = csv.writer(self._f)
            self._w.writerow(COLUMNS)
            self._f.flush()
            log.info("Racing intensity trace: %s", self.path)
        except OSError as e:
            self._failed = True
            log.warning("Racing intensity trace not written (%s)", e)

    def row(self, **kw):
        if self._w is None:
            return
        vals = []
        for c in COLUMNS:
            v = kw.get(c, "")
            vals.append(f"{v:.3f}" if isinstance(v, float) else v)
        try:
            self._w.writerow(vals)
            self._f.flush()
        except OSError:
            pass

    def close(self):
        if self._f is not None:
            try:
                self._f.close()
            except OSError:
                pass
        self._f = self._w = None
        self.path = None
        self._failed = False

    @staticmethod
    def stamp() -> str:
        return f"{datetime.datetime.now():%H:%M:%S}"
