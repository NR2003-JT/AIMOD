"""Crew chief on a phone (beta), stage 1: a read-only view of each car as its crew chief would see it.

Built on AIMOD's own tick thread from the data the strategy engine has just worked out, then published as
JSON; the web server thread only hands those bytes out (it never touches the engine or the game). Nothing
here changes a decision: stage 1 only reads.
"""
from __future__ import annotations

from collections import deque

from . import memory_map as mm

FLAG_NAMES = {0: "caution", 1: "caution", 2: "caution", 3: "caution", 4: "caution", 5: "one to go",
              6: "double file", 7: "green"}
LAP_KEEP = 8                    # lap times kept per car


class CrewView:
    def __init__(self):
        self.laps: dict[int, deque] = {}          # slot -> recent (lap, seconds) at the line
        self.seen: dict[int, int] = {}            # slot -> laps done when last seen
        self.session = None

    def _lap_times(self, car) -> list[float]:
        d = self.laps.setdefault(car.idx, deque(maxlen=LAP_KEEP))
        if (self.seen.get(car.idx) != car.laps_done and car.cross_ms and car.prev_cross_ms
                and car.laps_done > 0):
            secs = (car.cross_ms - car.prev_cross_ms) / 1000.0
            if 5 < secs < 600:
                d.append((car.laps_done, round(secs, 3)))
        self.seen[car.idx] = car.laps_done
        return [t for _lap, t in d]

    def build(self, snap, res, engine, radio=None, desk=None) -> dict:
        if snap.session != self.session:
            self.session = snap.session
            self.laps.clear()
            self.seen.clear()
        lap_now = getattr(engine, "lap_now", 0) or 0
        total = getattr(res, "laps_total", 0) or snap.laps_total
        rows = {r.idx: r for r in res.rows}
        states = getattr(engine, "cars", {}) or {}
        chief = getattr(engine, "crew_chief", None)
        field_bal = None
        if chief is not None:
            try:
                field_bal = chief.field_balance(snap.cars)
            except Exception:
                field_bal = None
        people = desk.view() if desk is not None else {}
        cars = []
        for c in snap.cars:
            if not c.valid:
                continue
            r = rows.get(c.idx)
            st = states.get(c.idx)
            car = {"idx": c.idx, "number": str(c.number), "name": c.name, "player": not c.controlled,
                   "team": c.team, "make": c.make, "on_pit_road": bool(c.on_pit_road), "out": bool(c.reason_out),
                   "laps": self._lap_times(c), "damage": round(c.damage or 0.0, 3),
                   "tires": {"lf": round(c.wear_lf, 3), "rf": round(c.wear_rf, 3), "lr": round(c.wear_lr, 3),
                             "rr": round(c.wear_rr, 3)},
                   "black_flag": bool(c.black_flag), "waved": bool(c.waved), "free_pass": bool(c.free_pass)}
            if r is not None:
                car.update({"pos": r.pos, "laps_down": r.laps_down, "gap": r.gap, "status": r.status, "kind": r.kind,
                            "stops": r.stops, "last_pit": r.last_pit, "next_pit": r.next_pit,
                            "tires_left": r.tires_left, "falloff": r.falloff, "pace_delta": r.pace_delta,
                            "fuel": {"laps": r.fuel_laps, "frac": r.fuel_frac, "real": r.fuel_real,
                                     "tank": r.car_tank, "margin": r.fuel_margin, "green_laps": r.green_laps}})
            if st is not None:
                plan = st.planned_lap
                race_lap = None
                if isinstance(plan, int):
                    car_lap = c.laps_done + 1 if c.laps_done > 0 else lap_now
                    race_lap = lap_now + (plan - car_lap)
                    if race_lap < lap_now:
                        race_lap = None                         # due already: the stop itself is next
                car["plan"] = {"lap": race_lap, "stage_break": plan == "STAGE", "style": st.style,
                               "note": st.plan_note, "decision": st.decision, "service": st.service,
                               "fuel_level": st.fuel_level, "call_note": st.note}
            if chief is not None:
                try:
                    setup = chief.state(c.idx)
                    car["setup"] = {"changes": list(setup.history)[-6:], "chassis": round(setup.chassis, 3),
                                    "wedge": setup.wedge, "track_bar": setup.track_bar,
                                    "rr_pressure": setup.rr_pressure, "tape": setup.tape}
                    bal = chief.balance(c)
                    if bal is not None and field_bal:
                        rel = bal / field_bal
                        car["handling"] = "tight" if rel >= 1.03 else "loose" if rel <= 0.97 else "neutral"
                except Exception:
                    pass
            if radio is not None:
                car["radio"] = list(radio.by_car.get(str(c.number), []))
            human = people.get(str(c.number))
            if human is not None or (st is not None and (getattr(st, "human_status", "")
                                                          or getattr(st, "human_plan_status", "")
                                                          or getattr(st, "human_adjust_status", ""))):
                status = getattr(st, "human_status", "") if st is not None else ""
                if not status and r is not None and (r.status or "").startswith("WAITS FOR CREW CHIEF"):
                    status = r.status                    # still waiting for the phone's call
                car["human"] = dict(human or {}, status=status,
                                    deadline_s=getattr(st, "human_deadline_s", None) if st is not None else None,
                                    ai_must=getattr(st, "ai_must", "") if st is not None else "",
                                    plan_status=getattr(st, "human_plan_status", "") if st is not None else "",
                                    ai_plan_lap=getattr(st, "ai_plan_lap", None) if st is not None else None,
                                    adjust_status=getattr(st, "human_adjust_status", "") if st is not None else "")
            cars.append(car)
        cars.sort(key=lambda x: x.get("pos") or 999)
        stage_ends = list(getattr(engine, "stage_list", []) or [])
        return {"enabled": True, "race": snap.session == mm.SESSION_RACE, "calls": desk is not None,
                "caution_no": getattr(engine, "cautions", 0),
                "lap": lap_now, "laps": total, "flag": FLAG_NAMES.get(snap.flag, "caution"),
                "pits_open": getattr(engine, "pits_open_at", None) is not None,
                "pits_closed": bool(getattr(snap, "pits_closed", False)), "stage_ends": stage_ends, "cars": cars}
