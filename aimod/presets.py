"""Preset files: strategy parameters plus the tracks a preset auto-loads for."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields

log = logging.getLogger("aimod.presets")

# v1.0.4 keys -> v2 keys. v2 writes both so presets stay usable in v1.
LEGACY_KEYS = {
    "UMBRAL_WEAR_R": "wear_r",
    "UMBRAL_WEAR_L": "wear_l",
    "FACTOR_URGENCIA": "urgency_factor",
    "UMBRAL_DANIO": "damage_threshold",
    "TANQUE_MANUAL_VALOR": "tank_laps",
    "MAX_TIRE_SETS": "max_tire_sets",
}


@dataclass
class Preset:
    wear_r: float = 0.252
    wear_l: float = 0.314
    urgency_factor: float = 1300.0
    damage_threshold: float = 0.5
    tank_laps: float = 70.0
    tank_source: str = "memory"     # manual | memory (= learned from the cars' real fuel)
    max_tire_sets: int = 0
    sets_include_start: bool = True
    fuel_margin: float = 2
    finish_fuel: float = 3          # final stop timed to finish with at most this many laps of fuel
    plan_margin: float = 5
    dispersion_divisor: float = 5
    plan_multi_stop: bool = True
    guard_game_stops: bool = True
    min_stop_gap: float = 8
    tire_only_stops: bool = True
    free_fuel: bool = True
    position_strategy: bool = True
    lead_pack_gap: float = 2.0
    threat_positions: int = 3
    stint_strategy: bool = True
    track_position_bias: int = 50
    oem_strategy: bool = True
    oem_gap: float = 1.0
    drafting: str = "auto"          # auto | yes | no
    track_type: str = "auto"        # auto | superspeedway | intermediate | short | road
    stint_spread: int = 80
    ss_stint_spread: int = 30
    fuel_reach: float = 15
    tank_extend_pct: float = 0.0
    fuel_threshold_default: float = 0.25
    green_fuel_aggressive: bool = False
    green_fuel_threshold_min: float = 5.0
    green_fuel_threshold_max: float = 10.0
    ss_topoff_pct: int = 75
    lapped_wave: bool = True
    pace_strategy: bool = True
    worn_pct: float = 1.0
    team_strategy: bool = True
    follow_player: bool = True
    value_tires: bool = True
    stages: int = 0                 # 0 = no stages, else 2-4 stages
    stage_by: str = "laps"          # laps | percent
    stage_ends: str = ""            # end of each stage but the last, e.g. "80, 160" or "25, 50"
    stage_early: int = 0            # a caution this many laps before a stage end ends the stage there
    flip_pct: int = 25              # % of eligible cars that flip a stage
    behavior_on: bool = False       # driver behavior: AI cars save fuel / save or cool tires / push
    behavior_strength: int = 70     # % of the behavior effect sizes
    behavior_late_pct: int = 10     # final % of the race in which everyone who can pushes (0 = off)
    behavior_reactions: bool = True # on-the-fly changes: lead battles, staying on the lead lap, tires, dirty air
    behavior_teams: bool = True     # teammates don't fight; drafting packs save / push with their team or make
    tracks: list[str] = field(default_factory=list)
    notes: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "Preset":
        known = {f.name for f in fields(cls)}
        p = cls()
        for old, new in LEGACY_KEYS.items():
            if old in data:
                setattr(p, new, data[old])
        for k, v in data.items():
            if k in known:
                setattr(p, k, v)
        p.max_tire_sets = int(p.max_tire_sets or 0)
        p.track_position_bias = int(max(0, min(100, p.track_position_bias)))
        p.threat_positions = int(p.threat_positions)
        for k in ("stint_spread", "ss_stint_spread", "ss_topoff_pct"):
            setattr(p, k, int(max(0, min(100, getattr(p, k)))))
        if p.drafting not in ("auto", "yes", "no"):
            p.drafting = "auto"
        if p.track_type not in ("auto", "superspeedway", "intermediate", "short", "road"):
            p.track_type = "auto"
        p.stages = int(p.stages) if int(p.stages) in (0, 2, 3, 4) else 0
        if p.stage_by not in ("laps", "percent"):
            p.stage_by = "laps"
        p.stage_ends = str(p.stage_ends or "")
        p.stage_early = int(max(0, p.stage_early))
        p.flip_pct = int(max(0, min(100, p.flip_pct)))
        p.behavior_strength = int(max(0, min(200, p.behavior_strength)))
        p.behavior_late_pct = int(max(0, min(50, p.behavior_late_pct)))
        p.tracks = [str(t) for t in (p.tracks or [])]
        return p

    def to_dict(self) -> dict:
        d = {"format": "AIMOD v2"}
        d.update(asdict(self))
        for old, new in LEGACY_KEYS.items():
            d[old] = getattr(self, new)
        return d


def parse_stage_ends(text: str) -> list[float] | None:
    """'80, 160' -> [80.0, 160.0]; None if anything is not a number."""
    parts = [x for x in str(text).replace(";", ",").replace(" ", ",").split(",") if x.strip()]
    try:
        vals = [float(x.strip().rstrip("%")) for x in parts]
    except ValueError:
        return None
    return vals if all(v > 0 for v in vals) else None


class PresetStore:
    def __init__(self, directory: str):
        self.dir = directory
        os.makedirs(directory, exist_ok=True)

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name + ".json")

    def names(self) -> list[str]:
        return sorted((f[:-5] for f in os.listdir(self.dir) if f.lower().endswith(".json")), key=str.lower)

    def load(self, name: str) -> Preset:
        with open(self.path(name), encoding="utf-8") as f:
            return Preset.from_dict(json.load(f))

    def save(self, name: str, preset: Preset):
        with open(self.path(name), "w", encoding="utf-8") as f:
            json.dump(preset.to_dict(), f, indent=4)

    def find_for_track(self, track: str) -> str | None:
        """Preset whose `tracks` list contains this track folder name."""
        if not track:
            return None
        t = track.strip().lower()
        for name in self.names():
            try:
                if t in (x.strip().lower() for x in self.load(name).tracks):
                    return name
            except Exception as e:
                log.warning("Skipping unreadable preset %s: %s", name, e)
        return None

    def assign_track(self, name: str, track: str):
        """Assign a track to one preset, removing it from any other preset."""
        t = track.strip().lower()
        for other in self.names():
            try:
                p = self.load(other)
            except Exception:
                continue
            had = [x for x in p.tracks if x.strip().lower() == t]
            if other == name and not had:
                p.tracks.append(track)
                self.save(other, p)
            elif other != name and had:
                p.tracks = [x for x in p.tracks if x.strip().lower() != t]
                self.save(other, p)
