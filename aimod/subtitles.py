"""TV-style subtitle box for the scanner: when to show it, and the radio conversations (scripted back and forths).

The box is optional and rare on purpose. A transmission is tagged with why it might deserve one:

  * "rival"  - a grudge, feud, wreck or retaliation call (the crew's and the driver's lines of one event share a
               group, so the whole situation is on screen, not just its first line);
  * "moment" - a big call (caution, checkered, a stage end, a win, a lead, heavy damage ...), only for the car on the
               TV camera (the game's camera follows a replay too, so a replay is covered by this);
  * "convo"  - a line of one of the user's own conversations (all of its lines show).

SubtitleGate decides at the moment the line goes on air. While the box is up the plain number card stays away (the
box has the number card in it); when the box isn't called on, the number card shows as before."""
from __future__ import annotations

import random
import re



class _Blank(dict):
    def __missing__(self, key):
        return ""


DEFAULTS = {"on": False, "x": None, "y": None, "opacity": 0.95, "size": 1.0,
            "rivals": True,        # grudge / feud / wreck / retaliation calls
            "moments": True,       # big calls from the car on TV
            "convos": True,        # lines of the user's own conversations
            "min_gap": 20,         # seconds between two boxes (lines of one situation don't count)
            "convo_chance": 100}   # % of the time a moment that has a conversation plays it instead of its normal line
GROUP_WINDOW = 25.0                # s: a situation's later lines still show if they come this soon after the last
MOMENT_TRIGGERS = ("caution", "white", "checkered", "stage_end", "black_flag", "damage", "driver_win", "driver_lead",
                   "driver_damage")
ROLE_WORDS = {"driver": "driver", "d": "driver", "crew": "crew", "crew chief": "crew", "chief": "crew", "c": "crew",
              "spotter": "spotter", "s": "spotter"}


class SubtitleGate:
    """allow(...) -> True when this transmission gets the box. Called from the radio's voice thread."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.group = None
        self.group_at = -1e9
        self.shown_at = -1e9

    def allow(self, sub: dict | None, now: float) -> bool:
        c = self.cfg
        if not sub or not c.get("on"):
            return False
        why = sub.get("why")
        if why == "rival" and not c.get("rivals", True):
            return False
        if why == "moment" and not (c.get("moments", True) and sub.get("tv")):
            return False
        if why == "convo" and not c.get("convos", True):
            return False
        if why not in ("rival", "moment", "convo"):
            return False
        group = sub.get("group")
        if group is not None and group == self.group and now - self.group_at <= GROUP_WINDOW:
            self.group_at = now                       # the same situation: every line of it shows
            return True
        if now - self.shown_at < float(c.get("min_gap", 20)):
            return False
        self.group, self.group_at, self.shown_at = group, now, now
        return True


def title_for(role: str, name: str = "") -> str:
    """what the box's small heading says: the driver's name when a driver speaks, else just the job"""
    if role == "driver":
        name = (name or "").strip()
        return f"{name.upper()} - DRIVER" if name else "DRIVER"
    return "SPOTTER" if role == "spotter" else "CREW CHIEF"


def parse_script(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """'Driver: Tight in the middle.' rows -> ([(role, line)], [rows that couldn't be read])"""
    lines, bad = [], []
    for row in str(text or "").splitlines():
        row = row.strip()
        if not row:
            continue
        who, sep, said = row.partition(":")
        role = ROLE_WORDS.get(who.strip().lower())
        if sep and role and said.strip():
            lines.append((role, said.strip()))
        else:
            bad.append(row)
    return lines, bad


def script_text(lines) -> str:
    names = {"driver": "Driver", "crew": "Crew", "spotter": "Spotter"}
    return "\n".join(f"{names.get(role, 'Crew')}: {said}" for role, said in lines)


def pick_conversation(cfg: dict, trig: str, ai: bool):
    """one of the user's conversations for this moment (or None). A conversation with a driver in it only plays for
    an AI car: your own driver never speaks on the radio."""
    if not trig or random.random() * 100 >= float((cfg.get("subtitles") or {}).get("convo_chance", 100)):
        return None
    ok = []
    for conv in cfg.get("conversations") or []:
        if conv.get("trigger") != trig or not conv.get("lines"):
            continue
        if not ai and any(r == "driver" for r, _t in conv["lines"]):
            continue
        ok.append(conv)
    return random.choice(ok) if ok else None


def fill(text: str, vals: dict) -> str:
    try:
        return text.format_map(_Blank({k: v for k, v in vals.items() if v is not None}))
    except (ValueError, IndexError):
        return text
