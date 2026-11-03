"""The AI's setup for a track: tracks\\<track>\\fast.<series>.sim (every AI car runs it).

Format v13/v14 .sim (2884/2892 bytes). Offsets decoded 2026-10-18 by comparing all 132 installed tracks'
fast.cup.sim against the garage limits in NR2003.exe (Dave Boyle's physics map): every value lands inside its
garage range and on its step.
  0x228..0x234  tire pressure LF, RF, LR, RR (psi, float)
  0x2D8/0x2DC   track bar left / right (metres, float; garage 8.5-14 in, 0.25 steps)
  0x2E4         grille tape (%, float; garage 0-100, 5 steps)
  0x2E8         fuel (game units, float)
  0x2F8..0x304  1st-4th gear (lookup index), 0x308 final drive, 0x338/0x33C 5th/6th (v14)
  0x31C/0x320   left weight / front weight (kg, float; garage lb)
  0x324         wedge (kg, float; garage -150..150 lb, 5 steps)
"""
from __future__ import annotations

import os
import struct

LB = 0.45359237
IN = 0.0254

# garage limits (NR2003.exe garage table): min, max, step
LIMITS = {"wedge": (-150, 150, 5), "track_bar": (8.5, 14.0, 0.25), "tape": (0, 100, 5), "pressure": (8.0, 60.0, 0.5)}


def read(path: str) -> dict | None:
    try:
        with open(path, "rb") as f:
            d = f.read()
    except OSError:
        return None
    if len(d) < 0x328 or d[:4] != b"PGTS":
        return None
    g = lambda o: struct.unpack_from("<f", d, o)[0]
    return {
        "version": struct.unpack_from("<i", d, 0x220)[0],
        "pressure": [round(g(o), 1) for o in (0x228, 0x22C, 0x230, 0x234)],   # LF RF LR RR psi
        "track_bar": [round(g(0x2D8) / IN, 2), round(g(0x2DC) / IN, 2)],         # left, right inches
        "tape": round(g(0x2E4)),
        "left_weight": round(g(0x31C) / LB),
        "front_weight": round(g(0x320) / LB),
        "wedge": round(g(0x324) / LB),
    }


def ai_setup(nr2003_dir: str, track: str, series: str = "cup") -> dict | None:
    """The setup the AI run at this track, or None when the file isn't there."""
    if not nr2003_dir or not track:
        return None
    return read(os.path.join(nr2003_dir, "tracks", track, f"fast.{series}.sim"))
