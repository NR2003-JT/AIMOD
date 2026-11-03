"""Pit board data shared by the in-game window and the companion-app/OBS widget.

A car gets a full row once it is ON pit road (NR2003's own pit road flag). Rows
stay in the order cars reached pit road; a car drops off a set time after it
leaves pit road and the rows below move up. Cars that have asked to pit but aren't on pit road yet are listed in a
small "coming in" strip (this time by, or later). Under caution the title reads
"race off pit road" and the lead-lap drivers who stayed out are listed on top.
"""
from __future__ import annotations

import re

from .strategy import PitEvent

MAKE_COLORS = {"chevrolet": "#c99a2e", "dodge": "#b3122e", "ford": "#1f4e9c", "toyota": "#d7261e"}


def last_name(name: str) -> str:
    parts = name.replace(",", " ").split()
    if not parts:
        return ""
    if parts[-1].rstrip(".").lower() in ("jr", "sr", "ii", "iii") and len(parts) > 1:
        return f"{parts[-2]} {parts[-1]}".upper()
    return parts[-1].upper()


def short_track(track: str) -> str:
    """'Charlotte_2012_SDT_Night_CE' / 'Texas Motor_Speedway_2012' -> 'CHARLOTTE' / 'TEXAS'."""
    words = re.split(r"[\s_]+", track or "")
    out = []
    for w in words:
        if not w or w[0].isdigit() or w.lower() in ("motor", "speedway", "international", "raceway",
                                                      "superspeedway", "sdt", "night", "ce"):
            break
        out.append(w)
    return " ".join(out or words[:1]).upper()


def service_label(e: PitEvent) -> str:
    tires = e.code.split("+")[-1]
    t = {"L/R": "4 TIRES", "L": "2 TIRES", "R": "2 TIRES"}.get(tires, "")
    if not t:
        t = "FUEL ONLY" if e.fuel else "NO SERVICE"
    elif not e.fuel:
        t += ", NO FUEL"
    return ("REPAIRS, " + t) if e.damage else t


def car_color(artdata, number, make: str) -> str:
    return (artdata.color(number) if artdata else None) or MAKE_COLORS.get((make or "").lower(), "#3a3a3a")


def stop_dict(e: PitEvent, now: float, pos_now: int | None, artdata=None) -> dict:
    return {
        "idx": e.idx, "number": str(e.number), "name": e.name, "last": last_name(e.name), "make": e.make,
        "code": e.code, "label": service_label(e), "damage": e.damage, "fuel": e.fuel, "reason": e.reason,
        "green": e.green, "lap": e.lap, "stop": e.stop_no, "state": e.state,
        "pos_in": e.pos_in or None, "pos_out": (pos_now if e.pos_pending else e.pos_out), "pos_now": pos_now,
        "pending": e.pos_pending,
        "gained": (e.pos_in - e.pos_out) if e.pos_in and e.pos_out else None,
        "service": round(e.box_time, 1), "road": round(e.road_time, 1),
        "age": round(now - e.t_exit, 1) if e.t_exit is not None else None,
        "t_exit": e.t_exit, "t_start": e.t_game, "note": e.note,
        "t_pit_in": e.t_pit_in, "t_box_in": e.t_box_in, "t_box_out": e.t_box_out,
        "color": car_color(artdata, e.number, e.make),
        "number_img": artdata.rel_number(e.number) if artdata else None,
        "headshot": artdata.rel_headshot(e.number) if artdata else None,
    }


def payload(events: list[PitEvent], now: float, flag_green: bool, lap: int, total: int,
            positions: dict[int, int], artdata=None, stayed_out=(), track: str = "", race: bool = True,
            logo: bool = True) -> dict:
    race_off = race and not flag_green          # "race off pit road" is a race-only (caution) thing
    done = ("out", "retired")
    coming = [e for e in events if e.state in ("requested", "in")]
    stops = [stop_dict(e, now, positions.get(e.idx), artdata) for e in events if e.state not in ("requested", "in")]

    # rows stay in the order cars first reached pit road; finished cars drop off after the hold
    # time and the rows below move up
    stops.sort(key=lambda d: (d["t_pit_in"] if d["t_pit_in"] is not None else d["t_start"], d["t_start"]))
    soon = sorted(coming, key=lambda e: (e.state != "in", positions.get(e.idx) or e.pos_in or 99))
    coming_in = [{"number": str(e.number), "last": last_name(e.name), "this_time_by": e.state == "in",
                  "pos": positions.get(e.idx) or e.pos_in, "color": car_color(artdata, e.number, e.make)} for e in soon]
    stayed = [{"pos": r.pos, "number": str(r.number), "last": last_name(r.name),
               "color": car_color(artdata, r.number, r.make),
               "number_img": artdata.rel_number(r.number) if artdata else None} for r in stayed_out]
    return {"flag": "green" if flag_green else "caution", "lap": lap, "total": total, "now": round(now, 2),
            "mode": "race_off" if race_off else "pit_road",
            "series": artdata.series if artdata else "", "series_name": (artdata.series_name if artdata else "") or "",
            "logo": (artdata.rel_logo()[0] if artdata and logo and hasattr(artdata, "rel_logo") else None),
            "track": short_track(track),
            "on_pit_road": sum(1 for d in stops if d["state"] not in done),
            "stayed_out": stayed if race_off else [], "coming_in": coming_in, "stops": stops}
