"""NR2003 .car files: team, sponsor, make and AI ratings for each car in a session.

A .car file (series\\<mod>\\cars\\*.car) is a small binary header, then plain INI text (Opponent Manager's
data), then binary paint data. [Driver] holds car_number, first_name, last_name, sponsor, team_name and
car_make (index into the mod's make list); [AIParamMean] holds the AI ratings (e.g. aiParamPitcrewSpeed).

Which .car file each session car uses is in game memory: a 48-char filename at car block +0x11F6
(0xAD204E for slot 2, the first AI car, per a r/Nr2003 post by NR2003_Ryan; same 8724-byte stride, player in
slot 0, pace car in slot 1). The NR2003 folder is the string at 0xF78868.
"""
from __future__ import annotations

import os
import re

from .carno import CarNo, raw

OFF_CAR_FILE = 0x11F6          # in the car block (8724-byte stride)
CAR_FILE_LEN = 48
NR2003_DIR = 0xF78868          # NR2003.exe folder, e.g. C:\\Papyrus\\NASCAR Racing 2003 Season

_SECTION = re.compile(r"^\[([^\]]+)\]\s*$")
_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def parse(path: str) -> dict[str, dict[str, str]]:
    """INI sections of a .car file (binary parts skipped)."""
    with open(path, "rb") as f:
        raw = f.read()
    out: dict[str, dict[str, str]] = {}
    cur = None
    for line in raw.decode("latin-1").splitlines():
        line = line.strip()
        m = _SECTION.match(line)
        if m:
            cur = out.setdefault(m.group(1), {})
            continue
        m = _KEY.match(line)
        if m and cur is not None:
            cur[m.group(1)] = m.group(2).strip()
    return out


def car_info(path: str) -> dict | None:
    try:
        d = parse(path)
    except OSError:
        return None
    drv = d.get("Driver") or {}
    try:
        number = CarNo(raw(drv.get("car_number", "").strip()))   # "01" -> 2001, as NR2003 stores it
    except ValueError:
        return None
    mean = d.get("AIParamMean") or {}

    def num(k):
        try:
            return float(mean.get(k, ""))
        except ValueError:
            return None
    return {"number": number, "team": drv.get("team_name", "").strip(), "sponsor": drv.get("sponsor", "").strip(),
            "make": int(drv["car_make"]) if drv.get("car_make", "").strip().isdigit() else None,
            "name": f"{drv.get('first_name', '').strip()} {drv.get('last_name', '').strip()}".strip(),
            "pitcrew_speed":    num("aiParamPitcrewSpeed"),
            "pitcrew_consistency": num("aiParamPitcrewConsistency"),
            "pitcrew_strategy": num("aiParamPitcrewStrategy"),
            "qual_speed":       num("aiParamDriverQualifying"),
            "driver_aggression": num("aiParamDriverAggression"),
            "driver_consistency": num("aiParamDriverConsistency"),
            "driver_finishing": num("aiParamDriverFinishing"),
            "driver_shorttrack": num("aiParamDriverShortTrack"),
            "driver_speedway":   num("aiParamDriverSpeedway"),
            "driver_superspeedway": num("aiParamDriverSuperspeedway"),
            "driver_roadcourse": num("aiParamDriverRoadCourse"),
            "vehicle_reliability": num("aiParamVehicleReliability")}


def cars_folder(nr2003_dir: str, mod: str) -> str:
    return os.path.join(nr2003_dir, "series", mod, "cars")


def _key(team: str) -> str:
    return re.sub(r"[^a-z0-9]", "", team.lower())


def teams_from(infos: list[dict]) -> list[dict]:
    """Named teams ({name, cars}) from car infos. Spelling variants are merged ("23XI", "23xi", "23XI Racing";
    "HAAS Factory Team", "HAASFactoryTeam"), and a car number that shows up under several teams (old paint
    files in the folder) goes to the team it appears with most. Cars without a team name stay unassigned."""
    from collections import Counter
    spell: dict[str, Counter] = {}
    votes: dict[int, Counter] = {}
    for c in infos:
        if not c or not c.get("team"):
            continue
        k = _key(c["team"])
        if not k:
            continue
        spell.setdefault(k, Counter())[c["team"]] += 1
        votes.setdefault(c["number"], Counter())[k] += 1
    # merge a key into a longer one that starts with it ("23xi" -> "23xiracing")
    keys = sorted(spell, key=len)
    root = {k: k for k in keys}
    for i, k in enumerate(keys):
        for longer in keys[i + 1:]:
            if longer.startswith(k) and len(k) >= 3:
                root[k] = longer
    def final(k):
        while root[k] != k:
            k = root[k]
        return k
    names: dict[str, Counter] = {}
    for k, cnt in spell.items():
        names.setdefault(final(k), Counter()).update(cnt)
    teams: dict[str, set[int]] = {}
    for number, v in votes.items():
        merged = Counter()
        for k, n in v.items():
            merged[final(k)] += n
        teams.setdefault(merged.most_common(1)[0][0], set()).add(number)
    out = []
    for k, nums in teams.items():
        best = max(names[k].items(), key=lambda kv: (kv[1], len(kv[0])))[0]
        out.append({"name": best, "cars": sorted(nums)})
    return sorted(out, key=lambda t: t["name"].lower())
