"""Driver art and colours for the pit board.

Artwork lives in series folders (AIMOD ships its own copy in art/, taken from
a companion overlay app's public archive; the companion app's AppData folder is an
optional second source):
  series/<id>/numbers/<car number>.png    number art
  series/<id>/headshots/<car number>.png  driver photos (images/defaultheadshot.png otherwise)
  series/<id>/colors.json                 {"<car number>": "#rrggbb"} driver colour
  series/<id>/seriesinfo.ini              seriesname=...
Which series: the one chosen in AIMOD, else the companion app's active series (when
that option is on), else the series whose number art best matches the cars on
track. Everything here is read-only.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

log = logging.getLogger("aimod.art")

REFRESH_S = 15.0
AUTO = "auto"


_found: str | None = None


def companion_dir() -> str:
    """The companion overlay app's AppData folder, found by what it holds (config.json and series/<id>/
    seriesinfo.ini folders) rather than by name. '' when there is none. Looked up once per run."""
    global _found
    if _found is None:
        _found = ""
        base = os.environ.get("APPDATA", "")
        try:
            names = sorted(os.listdir(base))
        except OSError:
            names = []
        for name in names:
            d = os.path.join(base, name)
            series = os.path.join(d, "series")
            if not (os.path.isfile(os.path.join(d, "config.json")) and os.path.isdir(series)):
                continue
            try:
                if any(os.path.isfile(os.path.join(series, x, "seriesinfo.ini")) for x in os.listdir(series)):
                    _found = d
                    break
            except OSError:
                continue
    return _found


class ArtData:
    def __init__(self, roots: list[str] | None = None, companion: bool = True):
        self.own_roots = [r for r in (roots or []) if r]
        self.companion = companion
        self.choice = AUTO
        self.series = ""
        self.series_name = ""
        self.colors: dict[str, str] = {}
        self._loaded = 0.0
        self._sig = None
        self._numbers_cache: dict[str, set[str]] = {}
        self._field: tuple = ()
        self._auto_pick = ""
        self.hint = ""                       # NR2003 mod folder name, e.g. "FCRD NCS22 v1"

    # ------------------------------------------------------------- folders
    @property
    def roots(self) -> list[str]:
        roots = list(self.own_roots)
        if self.companion:
            roots.append(companion_dir())
        return [r for r in roots if os.path.isdir(r)]

    @property
    def base(self) -> str:                   # kept for older callers
        return self.roots[0] if self.roots else ""

    @property
    def available(self) -> bool:
        return bool(self.roots)

    def available_series(self) -> list[str]:
        names = set()
        for r in self.roots:
            d = os.path.join(r, "series")
            if os.path.isdir(d):
                names.update(x for x in os.listdir(d) if os.path.isdir(os.path.join(d, x)))
        return sorted(names, key=str.lower)

    def _series_dir(self, series: str) -> str | None:
        for r in self.roots:
            d = os.path.join(r, "series", series)
            if os.path.isdir(d):
                return d
        return None

    def _numbers(self, series: str) -> set[str]:
        if series not in self._numbers_cache:
            nums = set()
            for r in self.roots:
                d = os.path.join(r, "series", series, "numbers")
                if os.path.isdir(d):
                    nums.update(os.path.splitext(f)[0] for f in os.listdir(d) if f.lower().endswith(".png"))
            self._numbers_cache[series] = nums
        return self._numbers_cache[series]

    def _companion_series(self) -> str:
        if not self.companion:
            return ""
        try:
            if not companion_dir():
                return ""
            with open(os.path.join(companion_dir(), "config.json"), encoding="utf-8") as f:
                return json.load(f).get("series") or ""
        except (OSError, ValueError):
            return ""

    def _colors_of(self, series: str) -> set[str]:
        d = self._series_dir(series)
        try:
            with open(os.path.join(d, "colors.json"), encoding="utf-8") as f:
                return {str(k).strip() for k, v in json.load(f).items() if str(v).lower() != "#888888"}
        except (OSError, ValueError, TypeError):
            return set()

    def _best_match(self) -> str:
        """Series whose number art covers the most car numbers on track; ties broken by the NR2003
        mod's name (e.g. 'NCS' in 'FCRD NCS22 v1') and by driver colours for those numbers."""
        if not self._field:
            return ""
        field = set(self._field)
        tokens = [t.lower() for t in re.findall(r"[A-Za-z]{2,}|\d{2,}", self.hint or "")]
        best, best_score = "", (0, 0, 0)
        for s in self.available_series():
            n = len(self._numbers(s) & field)
            if not n:
                continue
            hint = sum(1 for t in tokens if t in s.lower())
            score = (n, hint, len(self._colors_of(s) & field))
            if score > best_score:
                best, best_score = s, score
        return best

    # ------------------------------------------------------------- refresh
    def set_hint(self, mod: str):
        if mod != self.hint:
            self.hint = mod
            self._auto_pick = ""
            self._loaded = 0.0

    def set_field(self, numbers):
        field = tuple(sorted({str(n) for n in numbers if n is not None and int(n) >= 0}))
        if field != self._field:
            self._field = field
            self._auto_pick = ""
            self._loaded = 0.0

    def refresh(self, force: bool = False):
        now = time.monotonic()
        if not force and now - self._loaded < REFRESH_S:
            return
        self._loaded = now
        self._numbers_cache.clear()
        if self.choice != AUTO and self._series_dir(self.choice):
            series, how = self.choice, "chosen"
        elif self._companion_series() and self._series_dir(self._companion_series()):
            series, how = self._companion_series(), "the companion app's active series"
        else:
            self._auto_pick = self._auto_pick or self._best_match()
            series, how = self._auto_pick, "matched to the cars on track"
        sdir = self._series_dir(series) if series else None
        colors, name = {}, series
        if sdir:
            try:
                with open(os.path.join(sdir, "colors.json"), encoding="utf-8") as f:
                    colors = {str(k).strip(): str(v) for k, v in json.load(f).items()}
            except (OSError, ValueError):
                pass
            try:
                with open(os.path.join(sdir, "seriesinfo.ini"), encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if line.lower().startswith("seriesname="):
                            name = line.split("=", 1)[1].strip()
            except OSError:
                pass
        sig = (series, name, len(colors))
        if sig != self._sig:
            log.info("Artwork: %s (%s), %d driver colours - %s", series or "none", name, len(colors), how)
            self._sig = sig
        self.series, self.series_name, self.colors = series, name, colors
        self.how = how

    # ------------------------------------------------------------- lookups
    def _rel_if_exists(self, rel: str) -> str | None:
        return rel if any(os.path.isfile(os.path.join(r, rel)) for r in self.roots) else None

    def rel_number(self, number) -> str | None:
        if not self.series:
            return None
        return self._rel_if_exists(f"series/{self.series}/numbers/{str(number).strip()}.png")

    def rel_headshot(self, number) -> str | None:
        rel = self._rel_if_exists(f"series/{self.series}/headshots/{str(number).strip()}.png") if self.series else None
        return rel or self._rel_if_exists("images/defaultheadshot.png")

    @staticmethod
    def logo_name(mod: str) -> str:
        """File name of the user's logo slot for an NR2003 mod (art/logos/<mod>.png)."""
        safe = "".join(ch if ch.isalnum() or ch in " -_." else "_" for ch in (mod or "default")).strip(" .")
        return (safe or "default") + ".png"

    def rel_logo(self) -> tuple[str | None, str]:
        """Pit board logo: the user's logo for this mod, else the artwork series' logo. (rel path, source)"""
        rel = self._rel_if_exists("logos/" + self.logo_name(self.hint))
        if rel:
            return rel, "your logo for " + (self.hint or "this mod")
        if self.series:
            rel = self._rel_if_exists(f"series/{self.series}/serieslogo.png")
            if rel:
                return rel, f"the {self.series} artwork's logo"
        return None, "none (series name as text)"

    def color(self, number) -> str | None:
        c = self.colors.get(str(number).strip())
        return c if c and c.lower() not in ("#888888",) else None

    def abs_path(self, rel: str | None) -> str | None:
        if not rel or ".." in rel.replace("\\", "/").split("/"):
            return None
        for r in self.roots:
            p = os.path.normpath(os.path.join(r, rel))
            if p.startswith(os.path.normpath(r)) and os.path.isfile(p):
                return p
        return None


CompanionArt = ArtData        # old name
