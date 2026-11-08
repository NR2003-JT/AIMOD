"""Pit strategy engine.

A port of ControlEstrategiaMaestra from AIMOD v1.0.4, restructured so it has
no GUI or memory access of its own: each tick it takes a RaceSnapshot and
returns the memory writes to perform plus display rows and pit events.

On top of v1: tire-set limits, multi-stop planning, guards against repeat
stops, per-lap running order, gaps between cars, and running-order tactics
(lead pack, late-race tire/track-position calls, manufacturer drafting
partners). All new behaviour is controlled by StrategyParams.
"""
from __future__ import annotations

import dataclasses
import logging
import math
import random
import statistics
import time
from dataclasses import dataclass, field

from . import memory_map as mm
from .adjust import human_status as adjust_status
from .behavior import MANAGE_SCORE, manager_score, temperament
from .pace import PaceTracker, tire_factor

log = logging.getLogger("aimod.strategy")

GAME_DECIDE = "GAME DECIDE"

FOUR_TIRES = "4 TIRES"
RIGHT_SIDE = "RIGHT SIDE"
LEFT_SIDE = "LEFT SIDE"
FUEL_ONLY = "FUEL ONLY"

FUEL_PRACTICE = "PRACTICE"
FUEL_MAKES_IT = "MAKES IT"
FUEL_WINDOW = "ON WINDOW"
FUEL_SHORT = "SHORT ON FUEL"

# A game-requested stop can only be called off before the car commits to pit
# road; after this many ticks (150 ms each) it is let through.
CANCEL_TICKS = 20
DEFAULT_LAP_TIME = 30.0
STALL_SPEED = 0.5       # m/s: below this the car is stopped in its stall
STUCK_S = 150.0         # game s parked in the stall (a real stop, repairs included, is far shorter): stuck, release it
OT_CAUTION_LAPS = 3        # caution laps to plan for before an overtime (green-white-checkered) restart
OT_GREEN_LAPS = 2          # green + white
PIT_EXIT_CONFIRM_M = 400.0  # off pit road this far before a stop counts as finished (some tracks' pit road
                            # flag drops for a stretch mid-lane, e.g. between Bristol's two pit-lane sections)
PIT_EXIT_TIMEOUT = 25.0  # game seconds after box exit to give up waiting for the pit road flag to clear
REPAIR_MIN = 0.2         # damage (front+rear+left+right) or "car condition" from which a stop includes the repair
                         # (a repair brings both down to ~0.15)
PIT_HOLD_MAX = 600.0     # ...but a serviced car sitting still on pit road (held at the exit until the field is lined
                         # up) is waited for this long: closing the stop early logged the hold as a second stop
APPROACH_LAP = 0.30     # within this fraction of a lap before pit road entry: pitting this time by
FLAG_ONE_TO_GO = 5      # NR2003 flag states: 0 caution out (field racing back), 1-4 pacing, 5 one to go,
                        # 6 double file, 7 green
MIN_TANK_LAPS = 5.0     # a tank shorter than this is a bad reading (game value / mileage), never a real one
HUMAN_WAIT_S = 20.0     # crew chief on a phone: real seconds AIMOD waits for the caution call once pits open
HUMAN_APPROACH_M = 250.0  # ... or until the car is this close to pit entry
HOLD_ROOM_M = 150.0     # caution: a teammate behind the player stops waiting for the player's call this close to pit entry
CAUTION_SETTLE = 3.0    # s at the start of a caution with no pit calls (lets Race Control close the pits)
MOVING_SPEED = 15.0     # m/s: a car seen this fast off pit road has been out racing
BLOWOUT_RATIO = 1.5     # drafting tracks: wear this far past the threshold risks a blowout

# Stint styles: where in its fuel window a team puts a green-flag stop.
STAGE_BREAK = "STAGE"
BORDERLINE = 0.10           # a plan this share of a tank short of one stop fewer runs to the fuel window       # planned_lap: no green stop, pits at the coming stage break

# Track types: each has its own pit strategy playbook.
SUPERSPEEDWAY, INTERMEDIATE, SHORT_TRACK, ROAD_COURSE = "superspeedway", "intermediate", "short", "road"
TRACK_TYPE_NAMES = {SUPERSPEEDWAY: "superspeedway", INTERMEDIATE: "intermediate", SHORT_TRACK: "short track",
                    ROAD_COURSE: "road course"}

# NR2003's fuel request (+214): 1 splash, 2 half a can, 3 one can, 4 a can and a half, 5 two cans (full).
FUEL_LEVELS = {1: "a splash", 2: "half a can", 3: "1 can", 4: "1.5 cans", 5: "full"}
FUEL_ADD_GUESS = {1: 0.10, 2: 0.22, 3: 0.42, 4: 0.65}   # share of a full tank each adds, until measured (low side)

SHORT_PIT = "short-pit"     # as soon as the car can make it to the end / undercut
SPLIT = "split"             # even stints, balanced tires
STRETCH = "stretch"         # as late as fuel allows, hoping for a caution


def service_code(tires: str, fuel: bool) -> str:
    """F = fuel, L = left side, R = right side, L/R = four tires."""
    t = {FOUR_TIRES: "L/R", LEFT_SIDE: "L", RIGHT_SIDE: "R"}.get(tires, "")
    parts = (["F"] if fuel else []) + ([t] if t else [])
    return "+".join(parts) or "-"


@dataclass
class TeamRules:
    """How teammates and same-make cars work together; one set for drafting tracks, one for the rest."""
    priority: str = "team"          # team = teammates before same make | make = same make first | team_only | make_only
    team_pits: str = "split"        # split = teammates pick different plans | together = they pit on the same lap
    team_gap: float = 2.0           # s: teammates this close line up green stops (0 = at any distance)
    team_follow: int = 70           # % chance to follow the best-placed teammate's caution call
    make_gap: float = 1.0           # s: same-make cars this close count as drafting partners
    make_pits: int = 35             # % chance a same-make partner lines up its green stop
    make_follow: int = 35           # % chance to follow a same-make partner's caution call
    window: int = 2                 # laps: plans this far apart can be lined up (teammates +2)

    @classmethod
    def from_dict(cls, d: dict | None, base: "TeamRules | None" = None) -> "TeamRules":
        r = TeamRules(**vars(base)) if base is not None else cls()
        for k, v in (d or {}).items():
            if hasattr(r, k):
                try:
                    setattr(r, k, type(getattr(r, k))(v))
                except (TypeError, ValueError):
                    pass
        if r.priority not in ("team", "make", "team_only", "make_only"):
            r.priority = "team"
        if r.team_pits not in ("split", "together"):
            r.team_pits = "split"
        return r


DRAFT_RULES = TeamRules(team_pits="together", make_pits=85, make_follow=70, window=5)
OTHER_RULES = TeamRules()


@dataclass
class StrategyParams:
    wear_r: float = 0.252
    wear_l: float = 0.314
    urgency_factor: float = 1300.0
    damage_threshold: float = 0.5
    repair_on_stops: bool = True    # a damaged car's stop includes the repair, caution stops too (car +0x73D)
    tank_laps: float = 70.0
    tank_extend_pct: float = 0.0    # % to extend tank range (0 = no extension, 10 = +10% range)
    fuel_margin: float = 2          # v1 MARGEN_SEGURIDAD
    plan_margin: float = 5          # v1 MARGEN_DEJ
    dispersion_divisor: float = 5   # v1 DIVISOR_DISPERSION
    max_tire_sets: int = 0          # 0 = unlimited
    sets_include_start: bool = True
    plan_multi_stop: bool = True    # v1 only planned races needing exactly 2 stints
    guard_game_stops: bool = True   # call off pointless game-requested green-flag stops
    min_stop_gap: float = 8         # ...including any within this many laps of the last stop (0 = off)
    tire_only_stops: bool = True    # skip fuel when the car makes it to the end anyway (only if free_fuel is off)
    free_fuel: bool = True          # tire stops always add the fuel that fits in the tire change
    free_fuel_four: int = 4         # fuel level that fits in a 4-tire change (5 = full: modern crew)
    free_fuel_two: int = 3          # ... in a 2-tire change
    position_strategy: bool = True  # running order changes caution calls and pit windows
    lead_pack_gap: float = 2.0      # green flag: seconds behind the leader that count as the lead pack
    stint_strategy: bool = True     # green flag: teams pick short-pit / split / stretch and react to others
    threat_positions: int = 3       # caution: cars this many positions behind threaten a car that stays out
    track_position_bias: int = 50   # 0 = always favour tires, 100 = always favour track position
    oem_strategy: bool = True       # same-make cars running together pit together
    oem_gap: float = 1.0            # seconds: same-make cars this close count as drafting partners
    stint_spread: int = 80          # % of the fuel window between short-pit and stretch stops
    ss_stint_spread: int = 30       # ...at drafting tracks, where teams pit in tight groups
    fuel_reach: float = 15          # caution: within this many laps of making the finish -> pit
    ss_topoff_pct: int = 75         # drafting tracks, caution: pit for fuel unless the tank is fuller than this
    green_fuel_aggressive: bool = False # green flag: pit for fuel more aggressively on fuel stretches
    green_fuel_threshold_min: float = 5.0   # green flag: pit when fuel drops to this % on safe stretches
    green_fuel_threshold_max: float = 10.0  # green flag: pit when fuel drops to this % on aggressive stretches
    fuel_threshold_default: float = 0.25    # default fuel pit threshold: 0.25=25%, 0.20=20%, 0.30=30%, etc
    lapped_wave: bool = True        # caution: lapped cars in line for the wave around stay out to get the lap back
    pace_strategy: bool = True      # lap pace vs the cars around (and own early run) scales tire calls
    worn_pct: float = 1.0           # lap time loss (% of the early-run pace) that counts as worn tires
    team_strategy: bool = True      # teammates split plans, follow their lead car, pit together at drafting tracks
    follow_player: bool = True      # AI teammates of the player wait for his caution call and line up with his green stops
    value_tires: bool = True        # tire calls weigh seconds gained on fresh tires against the time / places they cost
    game_plan_off: bool = False     # the game's own planned stops are patched out: AIMOD plans every fuel stop
    finish_fuel: float = 3.0        # the final stop is timed to finish with at most this many laps of fuel left
    track_type: str = INTERMEDIATE  # superspeedway | intermediate | short | road (resolved by the app)
    rules_draft: TeamRules = field(default_factory=lambda: TeamRules(**vars(DRAFT_RULES)))
    rules_other: TeamRules = field(default_factory=lambda: TeamRules(**vars(OTHER_RULES)))
    stages: int = 0                 # stages in the race (0/1 = no stages)
    stage_by: str = "laps"          # laps | percent: how stage_ends are given
    stage_ends: tuple = ()          # end of each stage but the last (race lap or % of the race)
    stage_early: int = 0            # a caution this many laps before a stage end ends the stage (0 = automatic)
    stage_call_lead: float = 1.0    # laps the crew chief needs to get the stage-break stop called and on pit road
    stage_double_back_pct: int = 10 # % of cars that already serviced just before the break and don't need to,
                                    # but come back at it anyway (freshen up / top off); 0 = nobody
    flip_pct: int = 25              # % of eligible cars that flip a stage (pit just before the break)
    crew_brain: bool = False        # crew chief brain: cc_rating noise, aggression, teammate offset


@dataclass
class CarSnapshot:
    idx: int
    valid: bool                     # grip in (0.01, 1.05]
    grip: float = 0.0
    strategy: float = 0.0
    reason_out: int = 0
    pitting: int = 0
    wear_lf: float = 0.0
    wear_rf: float = 0.0
    wear_lr: float = 0.0
    wear_rr: float = 0.0
    damage: float = 0.0
    number: int = 0
    name: str = ""
    laps_done: int = 0              # laps this car has completed (0 = unknown)
    lap_dist: float = 0.0           # metres past the start/finish line
    side_flag: int = 1              # pit request side mapping, see memory_map
    controlled: bool = True         # False for the player's car
    grip_real: bool = True          # False: the game keeps no AI grip figure for this car (the player's)
    make: str = ""                  # manufacturer name
    make_id: int = -1               # manufacturer index from game memory
    black_flag: bool = False        # from the companion app's feed
    on_pit_road: bool | None = None # NR2003's own per-car pit road flag (None = unknown)
    pit_timer: float | None = None  # NR2003's own pit stop timer (counts during the service)
    waved: bool = False             # given the wave around (bc_wave log)
    free_pass: bool = False         # Race Control's free pass (lucky dog): gets its lap back this caution
    going_around: bool = False      # Race Control is sending it around the pace car (not back across the line)
    team: int = -1                  # team index from the Teams window (-1 = none)
    speed: float | None = None      # m/s from the velocity vector (None = unknown)
    fuel: float | None = None       # fuel in the tank, game units (None = not readable)
    fuel_cap: float | None = None   # tank capacity, game units
    game_penalty: bool = False      # the game wants it in to serve a penalty
    rule_pending: bool = False      # a penalty rule wants it in (state set, black flag maybe not shown yet)
    game_condition: float = 0.0     # the game's own damage / mechanical condition value
    game_repair: int = 0            # the game's own repair need: 2 = must pit, 1 = should pit (AIMOD decides)
    pit_mode: bool = False          # the AI is already in a pit mode: the stop goes ahead whatever the request says
    cross_ms: int | None = None     # game ms when it completed its last lap (NR2003's scoring table)
    prev_cross_ms: int | None = None  # ...and the lap before
    pos: tuple | None = None        # world position (x, y), metres - team radio spotter (alongside / clear)
    vel: tuple | None = None        # velocity (x, y), m/s


@dataclass
class RaceSnapshot:
    flag: int
    session: int
    tank_laps: float
    laps_done: float                # lap the leader is on
    laps_total: int
    cars: list[CarSnapshot]
    lap_source: str = "player car"
    session_time: float = 0.0       # game clock, seconds (0 = unknown)
    drafting: bool = False          # superspeedway: drafting partners matter more
    pace_dist: float | None = None  # pace car's metres past the start/finish line
    pits_closed: bool = False       # Race Control holds pit road closed (first caution lap), from its status file
    stage_ends: tuple | None = None # stage end laps from Race Control when it runs the stages (else the preset's)
    wave_around: bool | None = True # something gives lapped cars the wave around (False: never hold for one)


@dataclass
class Write:
    idx: int
    offset: int
    value: int


@dataclass
class PitEvent:
    time: float                     # wall clock, for the log
    idx: int
    number: int
    name: str
    lap: int
    service: str
    reason: str
    green: bool
    stop_no: int
    pos_in: int = 0
    pos_out: int | None = None
    pos_pending: bool = False       # caution stop: pos_out is filled at the green, once every car has stopped
    adjust: object = None           # crew chief chassis adjustment on this stop (adjust.Adjustment) or None
    duration: float | None = None   # game seconds, pit request to back on track (CSV only)
    fuel: bool = True
    box_time: float = 0.0           # game seconds stationary in the stall (service time)
    road_time: float = 0.0          # game seconds on pit road, entry to exit
    damage: bool = False            # stop includes repairs
    dmg_in: float | None = None     # car damage when the stop started
    dmg_out: float | None = None    # ...and when it rejoined
    note: str = ""
    t_game: float = 0.0
    make: str = ""
    caution_no: int = 0             # caution it was made under, or cautions so far for a green-flag stop
    t_pit_in: float | None = None   # game times of pit road entry, box entry, box exit, pit road exit
    t_box_in: float | None = None
    t_box_out: float | None = None
    t_pit_out: float | None = None
    timer0: float | None = None     # game pit timer when the request went in (it holds the last stop's value)
    state: str = "requested"        # requested (pit request in, not on its final approach yet) / in (pitting
                                    # this time by) / road / stall / exit / out / retired
    t_exit: float | None = None     # game time the car rejoined
    fuel_level: int = 5             # fuel requested (1 splash ... 5 full)
    fuel_in: float | None = None    # real fuel when the stop started
    fuel_box_in: float | None = None   # real fuel when the car stopped in its box (fuel flow, pit log)
    fuel_box_out: float | None = None  # real fuel when the service ended
    fuel_cap: float | None = None
    unrequested: bool = False       # seen stopping in its stall with no pit request (service read from the tires)
    wear_in: tuple | None = None    # (left, right) wear when an unrequested stop started

    @property
    def code(self) -> str:
        c = service_code(self.service, self.fuel)
        return "D+" + c if self.damage else c


@dataclass
class CarState:
    decision: bool | None = None
    post_stop_lock: bool = False
    service: str | None = None
    in_box: bool = False
    done_service: str | None = None
    planned_lap: int | str | None = None     # in this car's own laps
    urgency: float | None = None
    prev_grip: float | None = None
    damage_ref: float | None = None
    forced_damage: bool = False
    fuel_state: str | None = None
    last_stop_lap: int = 0                   # in this car's own laps
    left_pairs_used: int = 0
    right_pairs_used: int = 0
    left_since: int = 0                      # car lap the left tires went on
    right_since: int = 0
    stops: int = 0
    req_ticks: int = 0
    blocked: str = ""
    blocked_count: int = 0
    calloffs: list = field(default_factory=list)     # car laps a game request was called off on
    game_hint: str = ""                      # why the game seems to want the car in
    note: str = ""
    plan_note: str = ""
    fuel: bool = True                        # fuel on the current/next stop
    fuel_level: int = 5                      # how much (NR2003 fuel request 1-5)
    fuel_note: str = ""
    event: PitEvent | None = None
    last_event: PitEvent | None = None
    prev_dist: float | None = None
    prev_still: bool = False
    prev_timer: float | None = None
    undo: tuple | None = None                # state before a stop, to roll back a withdrawn request
    lap_dirty: bool = True                   # caution / pit road during the current lap: not a clean lap
    green_laps: int = 0                      # green-flag laps on this tank
    seen_laps: int = -1
    green_gap: float = 0.0                   # seconds behind the leader, last green tick
    oem_lap: int = -1                        # car lap the drafting-partner check last ran
    style: str = ""                          # stint style of the planned stop
    window: tuple[int, int] | None = None    # (earliest, latest) car lap for the planned stop
    reactive: float = 1.0                    # how readily the team answers other teams' stops
    react_lap: int = -1
    topoff_lap: int = -1                     # drafting tracks: car lap the second-stop check last ran
    topoffs: int = 0                         # extra caution stops this caution
    wave_hold: bool = False                  # staying out because it is in line for the wave around
    tires_good: bool = False                 # this caution's stop: its tires are still good (2 tires will do)
    pre_break: int | None = None             # stage end it fuelled past at a caution just before that break
    stage_serviced: int | None = None        # stage end whose break service it already took (caution stop at or
                                             # just before the break): no flip, no second stop from the stage plan
    borderline: int = 0                      # laps short of one stop fewer: runs to the fuel window
    game_accepted: bool = False              # AIMOD let a stop the game asked for go ahead
    road_prev: bool = False                  # player's car: on pit road last tick (its stops for the AI)
    released_at: float | None = None         # game time AIMOD last cleared a stuck car's pit flags
    # crew chief on a phone (beta): this caution's call, how it went, and the countdown while AIMOD waits
    human_caution: int = -1                  # caution the fields below belong to
    human_status: str = ""                   # "accepted" / "overridden: ..." / "no answer, AIMOD's call" / "too late"
    human_deadline_s: float | None = None    # seconds left to make the call (None = not waiting)
    human_wait_since: float | None = None    # wall clock when AIMOD started waiting
    human_key: tuple | None = None           # the call last applied (pit, service, fuel level)
    human_service: str | None = None         # service from the call, kept through AIMOD's caution service logic
    human_fuel: int | None = None            # fuel level from the call (1-5)
    ai_must: str = ""                        # why AIMOD's own caution call had to pit (safety beats the human)
    # green-flag pit plan from the phone (beta, stage 3)
    human_plan: int | None = None            # plan_id now driving this car's stop (None = AIMOD's own plan)
    human_plan_id: int | None = None         # the plan_id human_plan_status belongs to
    human_plan_status: str = ""              # "accepted: lap N" / "overridden: ..." / "done" / "used under caution"
    human_plan_used: set = field(default_factory=set)   # plan_ids already used up (never applied again)
    human_plan_stops: int = 0                # st.stops when the plan was taken on
    plan_service: str | None = None          # service / fuel level for the planned stop, from the plan
    plan_fuel: int | None = None
    ai_planned: object = None                # AIMOD's own plan for the car while a person plans it
    ai_note: str = ""
    ai_plan_lap: int | None = None           # ... as a race lap ("AIMOD would pit on lap N")
    human_learn: bool = True                 # False while the run was planned by a person (not learned from)
    # chassis adjustments from the phone (beta, stage 4)
    human_adjust_used: set = field(default_factory=set)  # adjust_ids already made at a stop (never again)
    human_adjust_id: int | None = None       # the adjust_id human_adjust_status belongs to
    human_adjust_status: str = ""            # "done: wedge +10 lb; the driver says ..." / "no changes" / "waiting..."
    player_dist: float | None = None         # player's car: lap distance last tick (passing pit road under caution)
    wear_seen: bool = False                  # player's car: its wear figures have moved (the game keeps them for it)
    hold_since: tuple | None = None          # caution: (pits open at, time) a teammate began waiting for the player
    off_road_at: tuple | None = None         # (game time, lap distance) the open stop's car last left pit road
    adopted: str = ""                        # the game sent the car down pit road unasked: AIMOD took the stop over
    withdrawn_hint: str = ""                 # why the game last asked for a stop that was then called off
    track_m: float = 0.0                     # metres driven on the track (off pit road) since last on pit road
    track_prev: float | None = None          # lap distance last tick (for track_m)
    road_still: int = 0                      # ticks stopped on pit road with no stop open
    exit_wait: bool = False                  # its last stop was closed while it was still on pit road: no new stop
                                             # until it has left pit road (a hold at the exit is not another stop)
    fuel_start: float | None = None          # car lap its tank was (equivalently) full, from the real fuel level
    fuel_rate: float | None = None           # learned fuel used per green lap (game units)
    fuel_samples: int = 0
    fuel_hist: list = field(default_factory=list)     # fuel per lap, last clean laps
    fuel_mark: tuple | None = None           # (laps done, fuel) at the last line crossing
    fuel_dirty: bool = True                  # caution / pit road during the current lap
    plan_tank: float = 0.0                   # tank (laps) the current plan was made with
    revised: str = ""                        # why the plan was just thrown out (shown on the new plan)
    tire_lap: int = -1                       # car lap the green tire-stop check last ran
    caution_next: str = ""                   # "next pit" shown when the caution came out
    caution_call: tuple | None = None        # (caution no., note) last caution's "pit" call, for a stop after the green
    tire_adjusted: int | None = None         # run (last stop lap) the undercut / overcut was decided for
    partner_later: int | None = None         # run a partner line-up already moved the stop a lap later
    flip_stage: int | None = None            # stage end (race lap) this car plans to flip
    seen_moving: bool = False                # seen at speed off pit road (a car parked on pit road at the start
                                             # is not making a stop)
    cancel_at_green: bool = False            # caution pit request still pending when the green came out
    flipped: int | None = None               # stage end it flipped (pitted under green just before it)
    stage_checked: int | None = None         # stage end the flip decision was rolled for


@dataclass
class CarInfo:
    rank: int
    down: int
    progress: float
    gap: float                      # seconds behind the leader, now
    age: float                      # 0 = grip like new tires, 1 = worst in the lead-lap field


@dataclass
class CarRow:
    idx: int
    number: int
    name: str
    rating: int
    out: bool
    grip: float = 0.0
    wear_l: float = 0.0
    wear_r: float = 0.0
    prob_l: int = 0
    prob_r: int = 0
    damage: float = 0.0
    urgency: float = 0.0
    fuel_state: str = ""
    fuel_laps: float | None = None
    fuel_frac: float | None = None
    fuel_real: bool = False         # fuel values come from the car's real fuel level
    car_tank: float | None = None   # laps a full tank lasts for this car
    own_mileage: bool = False       # car_tank from this car's own learned mileage (not the field's)
    green_laps: int = 0             # green-flag laps run on this tank
    fuel_margin: float | None = None  # laps of fuel over (+) or short (-) of the finish
    tires_left: str = ""
    stops: int = 0
    last_pit: str = ""
    next_pit: str = ""
    status: str = ""
    kind: str = ""          # out / pitting / stayout / finished / green / damage / player
    pos: int = 0
    lap: int = 0
    laps_down: int = 0
    gap: float | None = None
    make: str = ""
    black_flag: bool = False
    waved: bool = False
    free_pass: bool = False
    pace_delta: float | None = None     # seconds per lap slower (+) / faster (-) than the cars around it
    falloff: float | None = None        # fraction slower than its own early run on these tires
    team: int = -1
    behavior: str = ""                  # driver behavior mode (SAVE FUEL / SAVE TIRES / COOL TIRES / PUSH), set by the app
    grip_est: bool = False              # grip estimated from the field (the game keeps none for the player)
    wear_known: bool = True             # False: the car's wear figures never moved (not kept for it)


@dataclass
class TickResult:
    writes: list[Write] = field(default_factory=list)
    rows: list[CarRow] = field(default_factory=list)
    events: list[PitEvent] = field(default_factory=list)   # stop started
    exits: list[PitEvent] = field(default_factory=list)    # stop finished (pos_out, times filled)
    cancels: list[PitEvent] = field(default_factory=list)  # request withdrawn before the car reached pit road
    resolved: list[PitEvent] = field(default_factory=list) # caution stops whose pos_out was filled at the green
    laps_to_go: float = 0.0
    laps_total: int = 0
    keep_out: set = field(default_factory=set)   # caution: cars AIMOD keeps out - the app's pit guard holds their
                                                 # request off between ticks (the game sets it on its own)


def clamp(x, lo=0.0, hi=1.0):
    return lo if x < lo else hi if x > hi else x


def fuel_pit_fraction(p, fuel_state, tire_threat):
    """Fraction of a tank at or below which a car that can't reach the finish gets a fuel stop.
    Normally p.fuel_threshold_default. With aggressive green-flag fuel (and not MAKES IT / SHORT), a car with
    tire wear probability under 80 waits longer: green_fuel_threshold_min % on fresh tires, rising linearly to
    green_fuel_threshold_max % as the wear probability reaches 80."""
    if p.green_fuel_aggressive and fuel_state not in (FUEL_MAKES_IT, FUEL_SHORT) and tire_threat < 80:
        lo = min(p.green_fuel_threshold_min, p.green_fuel_threshold_max)
        hi = max(p.green_fuel_threshold_min, p.green_fuel_threshold_max)
        return (lo + (hi - lo) * max(0.0, tire_threat) / 80.0) / 100.0
    return p.fuel_threshold_default


class StrategyEngine:
    def __init__(self, rng: random.Random | None = None, clock=time.time, tire_model=None):
        self.tire_model = tire_model
        self.rng = rng or random.Random()
        self.clock = clock
        self.cars: dict[int, CarState] = {}
        self.reset()

    def hand_off_runs(self):
        """Give the tire model every run measured so far, the open ones too (they are closed: each is
        reported once). Call it while the model is still on the session's track and mod: the app does this
        before switching the model to a new track, so the old track's last runs aren't learned as the new one's."""
        model = getattr(self, "tire_model", None)
        if model is not None and hasattr(self, "pace"):
            self.pace.flush()
            for slope, laps in self.pace.take_finished():
                model.add_run(slope, laps, practice=not getattr(self, "_race", True))

    def reset(self):
        self.hand_off_runs()                              # session over: keep what its runs taught us
        self._race = True
        self.teams = {}
        self.crew_chief = getattr(self, "crew_chief", None)   # chassis adjustments (set up by the app)
        if self.crew_chief is not None:
            self.crew_chief.reset(self.crew_chief.base)
        self.crew_rating: dict[int, float] = getattr(self, "crew_rating", {})   # car number -> pit crew rating
        self.cc_profiles: dict[int, object] = getattr(self, "cc_profiles", {})  # car number -> CrewChiefProfile
        self.ot_pending = False                          # caution at the end of the race: overtime coming
        self.ot_total: int | None = None                 # lap the overtime restart finishes on
        self.cars.clear()
        self.last_flag = mm.FLAG_GREEN
        self.track_len = 0.0
        self.leader_call: bool | None = None
        self.green_lap: int | None = None
        self.race_started = False
        self.pits_open_at: float | None = None
        self.field_rate: float | None = None      # median fuel per green lap over the field
        self._field_slope: float | None = None
        self._field_slope_t = -1e9
        self.block_plans: dict[str, int] = {}
        self._stage_plan = False
        self.field_tank: float | None = None      # laps a full tank lasts, field median
        self.fuel_adds: dict[int, float] = {}       # fuel request level -> share of a tank it added (measured)
        self.caution_wall = 0.0
        self.stage_list: list[int] = []      # race laps where stages end (all but the last stage)
        self.stage_done: set[int] = set()
        self.stage_break: int | None = None  # this caution is the break after the stage ending on that lap
        self.caution_lens: list[int] = []    # laps each caution of this race lasted (yellow to green)
        self._caution_lap0: int | None = None
        self.lap_total = 0
        self.scored: dict[int, list] = {}     # idx -> [laps, crossing sequence, crossing game time]
        self.seq = 0
        self.lap_times: dict[int, float] = {}
        self.lap_time = 0.0
        self.grip_ref = 0.0
        self.now = 0.0
        self.last_now = None
        self._clock = 0.0
        self._clock_wall = None
        self._clock_game = 0.0
        self._prev_dists: dict[int, float] = {}
        self._in_tick = False
        self.prog_state: dict[int, list] = {}   # idx -> [lap counter, lap fraction, adjustment]
        self.cautions = 0
        self.lap_now = 0
        self.run_rank: dict[int, int] = {}       # running order on track (by distance covered)
        self.pending_pos: dict[int, list] = {}   # car idx -> caution stops waiting for their position out
        self.stage_source = "preset"
        self.green_stops: list[tuple] = []      # (idx, number, race lap, green gap, laps down)
        self.board: list[PitEvent] = []          # stops in progress and recently finished, for the pit board
        self.caution_start: float | None = None
        self.pace = PaceTracker()
        self._crossed: dict[int, float] = {}       # cars that crossed the line this tick -> lap time
        self.pace_info: dict[int, dict] = {}
        self._team_styles: list[str] = []
        self.pit_entry: float | None = None       # lap distance where pit road starts (learned)
        self.human_owner: dict[int, str] = {}     # crew chief on a phone (beta): car idx -> device name (set by app)
        self.human_calls: dict = {}               # car idx -> humancalls.HumanCall (set by app before each tick)
        self.human_plans: dict = {}               # car idx -> humancalls.HumanPlan (green-flag pit plan)
        self.human_adjusts: dict = {}             # car idx -> humancalls.HumanAdjust (changes at the next stop)
        self.entry_samples: list[float] = []
        self.drafting = False
        self.common_start: int | None = None     # drafting: stint start most of the lead lap shares
        # driver behavior on at a drafting track: laps gained per lap of saving fuel (set by the app; 0 = off)
        self.fuel_save_gain = 0.0
        # driver behavior on (set by the app): the stop style leans on the driver's tire tendency, and a car whose
        # tires are still good weighs staying out / 2 tires at a caution instead of following the field in
        self.behavior_on = False
        self._style_name = ""                    # the car being planned (its driver's temperament)
        self._style_car_number = -1
        self.behavior_cars: set[int] = set()     # cars whose pace / fuel / wear the driver behavior module changes:
                                                 # their laps don't teach pace, falloff or fuel mileage

    def state(self, idx: int) -> CarState:
        st = self.cars.get(idx)
        if st is None:
            st = self.cars[idx] = CarState()
        return st

    def condition_call(self, car: CarSnapshot) -> bool:
        """The game's 'car condition' value (+4943) is in the range its pit check asks a car in for (0.5-1.0) - and
        that check is still live. With the no_game_condition_stops patch on (condition_off) the game never asks for
        it, so AIMOD doesn't give in to it either: a stall stop never changes the value (#33 at Bristol pitted
        every lap for it)."""
        return car.game_condition >= 0.5 and not getattr(self, "condition_off", False)

    def gate_allow(self, snap: RaceSnapshot) -> set[int]:
        """Cars the pit gate lets head for pit road: AIMOD called them in (or took the game's ask), they have a
        penalty, repair or tow stop to serve, or the tank is nearly empty (the game's own < 10 % stop)."""
        out = set()
        for c in snap.cars:
            if not c.valid:
                continue
            st = self.cars.get(c.idx)
            gauge = c.fuel / c.fuel_cap if c.fuel is not None and c.fuel_cap else None
            if (c.rule_pending or c.game_penalty or c.game_repair >= 1 or (gauge is not None and gauge < 0.12)
                    or (st is not None and (st.decision is True or st.in_box or st.forced_damage))):
                out.add(c.idx)
        return out

    def note_gate_hold(self, idx: int, green: bool):
        """The pit gate refused a game-made turn for pit road for this car (the app reads the gate's counters)."""
        st = self.cars.get(idx)
        if st is not None and green and not st.blocked:
            st.blocked = "the game's call held by the pit gate"

    # ------------------------------------------------------------------ tires
    def tire_sets_available(self, p: StrategyParams) -> int | None:
        if p.max_tire_sets <= 0:
            return None
        return max(0, p.max_tire_sets - (1 if p.sets_include_start else 0))

    def limit_service(self, service: str, st: CarState, p: StrategyParams) -> str:
        avail = self.tire_sets_available(p)
        if avail is None:
            return service
        left = service in (FOUR_TIRES, LEFT_SIDE) and st.left_pairs_used < avail
        right = service in (FOUR_TIRES, RIGHT_SIDE) and st.right_pairs_used < avail
        if left and right:
            return FOUR_TIRES
        if left:
            return LEFT_SIDE
        if right:
            return RIGHT_SIDE
        return FUEL_ONLY

    def tires_left_text(self, st: CarState, p: StrategyParams) -> str:
        avail = self.tire_sets_available(p)
        if avail is None:
            return "-"
        return f"L{avail - st.left_pairs_used} R{avail - st.right_pairs_used}"

    def fuel_level(self, car: CarSnapshot, to_go: float, p: StrategyParams,
                   st: CarState | None = None, service: str = FUEL_ONLY, caution: bool = False) -> tuple[int, str]:
        lv, note = self._fuel_need(car, to_go, p, st, caution)
        if p.free_fuel and lv < 5:
            # the tire change takes this long anyway: take at least the fuel that fits in it
            floor = {FOUR_TIRES: p.free_fuel_four, LEFT_SIDE: p.free_fuel_two,
                     RIGHT_SIDE: p.free_fuel_two}.get(service, 1)
            if floor > lv:
                what = "4 tires" if service == FOUR_TIRES else "2 tires"
                return floor, f"fuel: {FUEL_LEVELS[floor]} (fits in the {what} change)"
        return lv, note

    def break_near(self, p: StrategyParams) -> int | None:
        """The coming stage end when it is only a few laps off (a caution now restarts just before it)."""
        end = self.next_stage_end()
        if end is None or self.stage_break is not None or p.tank_laps <= 0:
            return None
        return end if 0 <= end + 2 - self.lap_now <= max(10.0, 0.2 * p.tank_laps) else None

    def _fuel_need(self, car: CarSnapshot, to_go: float, p: StrategyParams,
                   st: CarState | None = None, caution: bool = False) -> tuple[int, str]:
        """Fuel to ask for: just enough to reach the next sure chance to pit (stage break or finish) with
        'Fuel left at the finish' laps to spare, else a full tank. Needs the car's real fuel level.
        A car flipping the stage stays out at that break, so it fuels for the chance after it."""
        if car.fuel is None or not car.fuel_cap or p.tank_laps <= 0:
            return 5, ""
        rate = car.fuel_cap / p.tank_laps                    # this car's fuel per lap
        laps = self.laps_to_next_chance(to_go)
        end = self.next_stage_end()
        where = "stage break" if end is not None else "finish"
        if st is not None and end is not None and st.flip_stage == end and st.flipped != end:
            later = [e for e in self.stage_list if e > end]
            laps = (later[0] + 2 - self.lap_now) if later else to_go
            where = "next stage break (flipping this one)" if later else "finish (flipping the stage)"
        elif st is not None and caution and self.break_near(p) is not None:
            # a caution stop a few laps before the stage end: fuel past the break so the car need not stop
            # again at it (it stays out there and gains the spots of the cars that do stop)
            st.pre_break = end
            later = [e for e in self.stage_list if e > end]
            laps = (later[0] + 2 - self.lap_now) if later else to_go
            where = "next stage break" if later else "finish"
        spare = max(1.5, p.finish_fuel)
        want = (laps + spare) * rate - car.fuel
        room = car.fuel_cap - car.fuel
        if want >= 0.9 * room:
            return 5, ""
        for lv in (1, 2, 3, 4):
            add = min(room, self.fuel_adds.get(lv, FUEL_ADD_GUESS[lv]) * car.fuel_cap)
            if add >= max(want, 0.0):
                return lv, f"fuel: {FUEL_LEVELS[lv]} - enough for the {where} with ~{spare:.0f} laps spare"
        return 5, ""

    def learn_fuel_add(self, ev: "PitEvent", car: CarSnapshot):
        """How much each fuel request level really adds (the tank filling up hides the rest)."""
        if ev.fuel_in is None or car.fuel is None or not car.fuel_cap or not ev.fuel or ev.fuel_level >= 5:
            return
        added = car.fuel - ev.fuel_in
        if added <= 0 or car.fuel >= 0.97 * car.fuel_cap:
            return
        share = added / car.fuel_cap
        old = self.fuel_adds.get(ev.fuel_level)
        self.fuel_adds[ev.fuel_level] = share if old is None else 0.7 * old + 0.3 * share

    @staticmethod
    def service_writes(idx: int, side_flag: int, service: str, fuel: bool | int = True) -> list[Write]:
        left = 1 if service in (FOUR_TIRES, LEFT_SIDE) else 0
        right = 1 if service in (FOUR_TIRES, RIGHT_SIDE) else 0
        a, b = (left, right) if side_flag else (right, left)
        return [Write(idx, mm.OFF_REQ_LEFT, left), Write(idx, mm.OFF_REQ_RIGHT, right),
                Write(idx, mm.OFF_CREW_A, a), Write(idx, mm.OFF_CREW_B, b),
                Write(idx, mm.OFF_FUEL_REQ, (mm.FUEL_FULL if fuel is True else int(fuel)) if fuel else 0)]

    @staticmethod
    def likely_service(prob_l: float, prob_r: float) -> str:
        """Most likely outcome of v1's service dice for these wear levels."""
        c_l = prob_l >= 50
        c_r = prob_r >= 60
        if c_l and c_r:
            return FOUR_TIRES
        if c_r:
            return FOUR_TIRES if prob_l >= 70 else RIGHT_SIDE
        if c_l:
            return LEFT_SIDE if prob_l >= 70 else FUEL_ONLY
        return FUEL_ONLY

    @staticmethod
    def needs_fuel(fuel_left: float, to_go: float, tires: str, p: StrategyParams) -> bool:
        if tires == FUEL_ONLY or not p.tire_only_stops:
            return True
        if p.free_fuel:
            return fuel_left < p.tank_laps - 1.0          # tires take time anyway: fuel unless nearly full
        return fuel_left < to_go + 2 * p.fuel_margin

    # ---------------------------------------------------------- running order
    def score_laps(self, snap: RaceSnapshot):
        """Scoring order: by laps completed, then by who crossed the line first.
        A car's position only changes when it crosses the start/finish line."""
        new = []
        for c in snap.cars:
            if not (c.valid and c.laps_done > 0):
                continue
            rec = self.scored.get(c.idx)
            if rec is None or c.laps_done < rec[0]:
                new.append(c)
            elif c.laps_done != rec[0]:
                if c.laps_done == rec[0] + 1:
                    if c.cross_ms and c.prev_cross_ms and 3000 < c.cross_ms - c.prev_cross_ms < 900000:
                        # NR2003's own crossing times: exact at any time acceleration
                        lt = (c.cross_ms - c.prev_cross_ms) / 1000.0
                        self.lap_times[c.idx] = lt
                        self._crossed[c.idx] = lt
                    elif self.now > rec[2] > 0:
                        self.lap_times[c.idx] = self.now - rec[2]
                        self._crossed[c.idx] = self.now - rec[2]
                self.seq += 1
                self.scored[c.idx] = [c.laps_done, self.seq, self.now, c.cross_ms]
        # first sight (AIMOD started mid-race): further round the lap = crossed earlier
        tl = self.track_len or 1.0
        for c in sorted(new, key=lambda c: c.laps_done + min(c.lap_dist / tl, 0.999), reverse=True):
            self.seq += 1
            self.scored[c.idx] = [c.laps_done, self.seq, 0.0]
        times = sorted(self.lap_times.values())
        if times:
            med = statistics.median(times)
            green = [t for t in times if t < med * 1.15]
            self.lap_time = statistics.median(green) if green else med

    def advance_clock(self, snap: RaceSnapshot, wall: float) -> float:
        """Engine time in game seconds. Follows NR2003's session clock while it runs (so time
        acceleration is honoured); if that clock stalls while cars are still moving (seen in a
        live race: frozen for minutes), real time is used instead. Nothing moving = paused."""
        moved = 0.0
        dists = {}
        for c in snap.cars:
            if c.valid:
                dists[c.idx] = c.lap_dist
                prev = self._prev_dists.get(c.idx)
                if prev is not None:
                    moved = max(moved, abs(c.lap_dist - prev))
        self._prev_dists = dists
        if self._clock_wall is None:
            self._clock_wall, self._clock_game = wall, snap.session_time
            self._clock = snap.session_time if snap.session_time > 0 else 0.0
            return self._clock
        dw = max(0.0, wall - self._clock_wall)
        dg = snap.session_time - self._clock_game if snap.session_time > 0 and self._clock_game else 0.0
        if 0 < dg <= 120:                          # game clock running (any time acceleration)
            self._clock += dg
        elif moved > 0.05:                         # game clock stalled but the cars are moving
            self._clock += dw
        self._clock_wall, self._clock_game = wall, snap.session_time
        return self._clock

    def field_info(self, snap: RaceSnapshot) -> dict[int, CarInfo]:
        if not self._in_tick:                      # called on its own (tests, tools)
            self.now = self.advance_clock(snap, self.clock())
        cars = [c for c in snap.cars if c.valid and c.laps_done > 0]
        for c in cars:
            if c.lap_dist > self.track_len:
                self.track_len = c.lap_dist
        self.score_laps(snap)
        if not cars:
            return {}
        tl = self.track_len or 1.0
        lt = self.lap_time or DEFAULT_LAP_TIME
        prog = {c.idx: self.car_progress(c, tl) for c in cars}
        lead = max(prog.values())
        exact = all(len(self.scored[c.idx]) > 3 and self.scored[c.idx][3] for c in cars)
        # same lap: whoever crossed the line first (NR2003's crossing times when known, else the order seen)
        ranked = sorted(cars, key=lambda c: (-self.scored[c.idx][0],
                                             self.scored[c.idx][3] if exact else self.scored[c.idx][1]))
        running = [c for c in cars if c.reason_out == 0 and c.controlled]
        for c in running:
            if c.grip > self.grip_ref:
                self.grip_ref = min(c.grip, 1.05)
        lead_lap = [c.grip for c in running if lead - prog[c.idx] < 1]
        floor = min(lead_lap) if lead_lap else self.grip_ref
        scale = max(0.02, self.grip_ref - floor)
        self.run_rank = {c.idx: n + 1 for n, c in enumerate(sorted(cars, key=lambda c: -prog[c.idx]))}
        out = {}
        for n, c in enumerate(ranked):
            behind = lead - prog[c.idx]
            out[c.idx] = CarInfo(n + 1, int(behind), prog[c.idx], behind * lt,
                                 clamp((self.grip_ref - c.grip) / scale))
        return out

    def compute_pace(self, snap: RaceSnapshot, infos, p: StrategyParams) -> dict[int, dict]:
        """Per car: pace delta to the cars around it (+-3 positions, same lap), falloff from its own
        early run, the tire factor, and whether it is matching the lead pack from further back."""
        out = {}
        paces = {c.idx: self.pace.pace(c.idx) for c in snap.cars if c.valid and c.idx in infos}
        lead = [paces[i] for i, inf in infos.items() if inf.rank <= 5 and paces.get(i)]
        lead_pace = statistics.median(lead) if len(lead) >= 2 else None
        lt = self.lap_time or DEFAULT_LAP_TIME
        by_rank = sorted(infos.items(), key=lambda kv: kv[1].rank)
        for n, (idx, inf) in enumerate(by_rank):
            mine = paces.get(idx)
            near = [paces[j] for j, oi in by_rank[max(0, n - 3):n + 4]
                    if j != idx and oi.down == inf.down and paces.get(j)]
            delta = (mine - statistics.median(near)) if mine and len(near) >= 2 else None
            fall = self.pace.falloff(idx)
            k = tire_factor(delta, fall, lt) if p.pace_strategy else 1.0
            keeping_lead = bool(p.pace_strategy and mine and lead_pace and inf.down == 0 and inf.rank > 5
                                and inf.gap > p.lead_pack_gap and (mine - lead_pace) / lead_pace <= 0.003)
            fading_leader = bool(p.pace_strategy and delta is not None and inf.rank <= 3 and delta / lt > 0.01)
            out[idx] = {"delta": delta, "falloff": fall, "k": k, "keeping_lead": keeping_lead,
                        "fading_leader": fading_leader}
        return out

    def car_progress(self, c: CarSnapshot, tl: float) -> float:
        """Laps + fraction of the lap. The lap counter and the lap distance don't
        roll over on the same frame, which for a moment puts a car a whole lap
        ahead or behind; this holds the lap count steady until both agree."""
        frac = min(c.lap_dist / tl, 0.999)
        rec = self.prog_state.get(c.idx)
        adj = 0
        if rec is not None:
            last_count, last_frac, adj = rec
            wrapped = last_frac - frac > 0.5
            counted = c.laps_done - last_count
            if wrapped and counted <= 0:
                adj = 0 if adj < 0 else 1           # distance crossed the line first
            elif counted > 0 and not wrapped:
                adj = 0 if adj > 0 else (-1 if frac > 0.5 else 0)   # counter ticked first
            elif counted < 0 or abs(counted) > 1:
                adj = 0
            if (adj > 0 and frac > 0.5) or (adj < 0 and frac < 0.5):
                adj = 0
        self.prog_state[c.idx] = [c.laps_done, frac, adj]
        return c.laps_done + adj + frac

    # --------------------------------------------------------------- planning
    def plan_stop(self, st: CarState, p: StrategyParams, rating: int, laps_done: float,
                  laps_to_go: float, laps_total: int, tank: float, me: CarInfo | None = None,
                  wear_need: float = 0.0, anchor: str = "split"):
        """anchor 'split': even stints (a stage needing one stop is split exactly in half);
        'roadmap': the classic plan - the fewest stops, counted back from the finish with full tanks
        (the slack is used up first, the later stints are full)."""
        follows = self.rng.randint(1, 100) <= rating
        if laps_to_go <= tank - p.fuel_margin:
            return GAME_DECIDE                       # makes it on fuel: no stop needed
        if not follows and not p.game_plan_off:
            return GAME_DECIDE                       # the game pits it (its own plan is still on)
        stint_start = self.fuel_from(st)
        if self.drafting and self.common_start is not None and abs(stint_start - self.common_start) <= 2:
            stint_start = self.common_start          # pitted with the pack: plan with the pack
        remaining = laps_total - stint_start
        usable = self.usable_stint(tank, p.fuel_margin)    # laps a stint can really run, with the cushion
        n_stints = math.ceil(remaining / usable)
        if n_stints < 2 or (n_stints > 2 and not p.plan_multi_stop and not p.game_plan_off):
            return GAME_DECIDE
        ideal_stint = remaining / n_stints
        if not p.stint_strategy and not ideal_stint < tank - p.plan_margin and not p.game_plan_off:
            return GAME_DECIDE                       # v1 rule
        ideal_pit = math.floor(stint_start + ideal_stint)
        # Earliest lap this stop can happen and still finish with the stops left
        # (v1's earliestPitToFinish for a 2-stint race), latest = fuel runs out.
        earliest = laps_total - (n_stints - 1) * usable      # every later stint runs a usable tank
        lo = math.ceil(max(laps_done + 1, earliest))
        hi = min(math.floor(stint_start + usable), self.max_pit(st, tank, p.fuel_margin))   # never past its fuel
        if lo > hi:
            return GAME_DECIDE
        st.window = (lo, hi)
        st.plan_tank = tank
        max_var = math.floor((100 - rating) / p.dispersion_divisor) if p.dispersion_divisor > 0 else 0
        if not follows:
            max_var = max(max_var, 3)
        short = remaining - (n_stints - 1) * usable          # laps short of doing it with one stop fewer
        # Superspeedways (2026-10-27): with driver behavior on, the pack rides and saves fuel the whole run, so a
        # stop fewer is planned whenever saving over the laps left covers it (most of what the saving gives, with a
        # reserve). Without it Talladega split every 56-lap stage into three stints: two green stops a stage, each
        # costing a lap. As fuel is saved the stop moves later (plan_still_good) or goes for the stage break.
        save_ok = self.drafting and self.fuel_save_gain > 0
        limit = self.fuel_save_gain * max(0.0, remaining - 3) * 0.7 if save_ok else max(2.0, BORDERLINE * tank)
        if (n_stints >= 2 and short <= limit and wear_need <= 1.5 and hi > lo
                and (not self.drafting or save_ok)):
            # only a few laps short of one stop fewer (about what the mileage estimate can be off by): run to
            # the fuel window saving fuel, and re-check there - never short-pit into the extra stop
            st.style = STRETCH
            st.reactive = 0.2
            st.borderline = round(short)
            return hi
        st.borderline = 0
        if p.stint_strategy and hi > lo:
            # window_frac: 0 = just pitted, 1 = fuel deadline — informs brain style bias
            w_frac = clamp((laps_done - stint_start) / max(1.0, usable))
            # has_stages: True when called from plan_stage() (a free break is coming),
            # False when final stint or pre-stage era (no free stops ahead)
            st.style = self.choose_style(me, rating, wear_need, p,
                                         window_frac=w_frac,
                                         has_stages=self._stage_plan)
            st.reactive = 1.0 if self.rng.random() < 0.65 else 0.2      # some teams stick to the plan
            span = hi - lo
            if anchor == "late" and n_stints == 2 and p.track_type == ROAD_COURSE:
                anchor = "roadmap"                   # road course: the final tank is timed to the finish exactly
            if anchor == "roadmap" and lo - stint_start < 0.6 * usable:
                anchor = "split"                     # the spare laps would make a silly short stint: split evenly
            centre = {"roadmap": 0.0, "late": 1.0}.get(anchor, (ideal_pit - lo) / span)
            spread = clamp((p.ss_stint_spread if self.drafting else p.stint_spread) / 100.0)
            # short-pit / stretch: spread x half the window, but never more than ~12% of a tank either side
            # (a short-pit is a few laps early, not half a stint)
            offset = min(spread * span / 2, max(3.0, 0.12 * tank))
            base = lo + clamp(centre) * span
            chosen = round(base + {SHORT_PIT: -offset, STRETCH: offset}.get(st.style, 0.0))
            if self.drafting:
                max_var = 0                          # drafting: each plan's group pits on the same lap
            elif st.style != SPLIT:
                max_var //= 2
            elif anchor == "split":
                max_var //= 4                        # the stage split is a highly predictable roadmap
        else:
            st.style = ""
            chosen = ideal_pit                       # v1 behaviour: even stints
        if max_var > 0:
            chosen += self.rng.randint(-max_var, max_var)
        return max(lo, min(hi, chosen))

    def choose_style(self, me: CarInfo | None, rating: int, wear_need: float, p: StrategyParams,
                     window_frac: float = 0.5, has_stages: bool = True) -> str:
        """Weighted, not random: tire falloff favours going early, a caution-heavy
        race favours stretching, the lead pack runs the even split, cars further
        back try the undercut or gamble on a caution, lapped cars wait for one."""
        w = {SHORT_PIT: 1.0, SPLIT: 2.0 + rating / 100, STRETCH: 1.0}
        if p.track_type in (SHORT_TRACK, ROAD_COURSE):
            # a green stop costs laps (short track) or a long pit lane (road course): stay out, wait for yellows
            w[STRETCH] *= 3.0
            w[SHORT_PIT] *= 0.3
        elif p.track_type == INTERMEDIATE:
            level = self.wear_level(p)
            if level == "high":                              # strict 4-tire stops and aggressive undercuts
                w[SHORT_PIT] *= 1.8
                w[STRETCH] *= 0.6
            elif level == "low":                             # tires don't matter much: run the fuel out
                w[SHORT_PIT] *= 0.5
                w[STRETCH] *= 1.5
        if wear_need > 1.0 and not self.drafting:            # tires go off before the fuel runs out
            w[SHORT_PIT] += min(2.0, wear_need - 1.0) * 1.5
            w[SPLIT] += 0.5
            w[STRETCH] *= 0.5
        w[STRETCH] += self.cautions * 100 / max(20, self.lap_now) / 3   # cautions per 100 laps
        if self.drafting:
            w[STRETCH] += 1.0                                # big wrecks are likely: wait for a caution
        together = self.rules(p).team_pits == "together"
        for style in self._team_styles:                  # teammates already on a plan: split up, or join them
            if style in w:
                w[style] *= 3.0 if together else 0.35
        if me is not None and self._stage_plan and me.down == 0:
            if me.rank <= 10:                                # racing for stage points: hold position at the end
                w[SPLIT] += 1.0
                w[STRETCH] += 0.5
            elif me.rank > 15:                               # playing track position: get the tires early
                w[SHORT_PIT] += 0.8
        if me is not None:
            if me.down > 0:
                w[STRETCH] += 2.0
            elif me.gap <= p.lead_pack_gap:
                w[SPLIT] += 1.0
                w[STRETCH] += 0.5
            else:
                w[SHORT_PIT] += 1.0
                w[STRETCH] += 0.5
        if self.behavior_on and not self.drafting and self._style_name:
            # one tendency for both (user, 2026-10-28): a driver who manages his tires stretches the run, an
            # aggressive one pushes early and short-pits. Weights, not rules: most of the time, not always.
            temper = temperament(self._style_name)
            if manager_score(rating, temper, 60) >= MANAGE_SCORE:
                w[STRETCH] *= 2.5
                w[SHORT_PIT] *= 0.4
            elif temper >= 0.65:
                w[SHORT_PIT] *= 2.2
                w[STRETCH] *= 0.5
        if me is not None and me.down == 0 and me.rank <= 10:
            # the leaders have the track position: a short-pit only gives it away (they answer undercuts instead)
            w[SHORT_PIT] *= 0.1 if me.rank <= 5 else 0.35
        if p.crew_brain and self.cc_profiles and self._style_name:
            from .crewchief import apply_style_bias
            cc = self.cc_profiles.get(getattr(self, "_style_car_number", -1))
            if cc is not None:
                w = apply_style_bias(w, cc, p.track_type, window_frac, has_stages)
        r = self.rng.random() * sum(w.values())
        for style, weight in w.items():
            r -= weight
            if r <= 0:
                return style
        return SPLIT

    def react_to_stops(self, car: CarSnapshot, st: CarState, car_lap: int, me: CarInfo | None, p: StrategyParams):
        """Green flag, once a lap: when cars running near this one start their stops,
        the team may bring its own stop forward - strongly if a car behind just
        pitted (undercut threat), less if one ahead did. Stretch-style teams and
        teams sticking to their plan rarely react."""
        if (not p.stint_strategy or me is None or st.react_lap == car_lap or not isinstance(st.planned_lap, int)
                or st.planned_lap <= car_lap + 1 or st.window is None or car_lap + 1 < st.window[0]):
            return
        if p.track_type in (SHORT_TRACK, ROAD_COURSE) or st.borderline:
            return                                       # a green stop costs too much to answer one
        st.react_lap = car_lap
        span = 2 * max(0.1, p.lead_pack_gap)
        early = st.planned_lap - car_lap                 # laps of the plan given up by reacting now
        gain = self.fresh_tire_gain(car, st, car_lap)    # s/lap fresh tires are worth right now
        behind, ahead, who = 0.0, 0, None
        for idx, number, lap, gap, down in self.green_stops:
            if idx == car.idx or down != me.down or lap < self.lap_now - 2:
                continue
            d = gap - st.green_gap
            if 0 < d <= span:
                # an undercut only works if fresh tires make up the gap before this car's own stop
                threat = 0.6 if gain is None else clamp(min(early, 3) * gain / max(0.2, d))
                if threat > 0.05:
                    behind += threat
                    who = who or number
            elif -span <= d < 0:
                ahead += 1
                who = who or number
        if not (behind or ahead):
            return
        field = max(10, len(self.cars))
        place = 0.3 + 0.7 * clamp((me.rank - 3) / (0.4 * field))     # good track position: hold it
        stretch = 0.35 if st.style == STRETCH else 1.0
        cost = clamp(1 - (early - 2) / 8, 0.2, 1.0)                  # many laps early = a short stint later
        chance = (0.25 * behind + 0.12 * ahead) * st.reactive * stretch * place * cost
        info = self.pace_info.get(car.idx, {})
        if info.get("keeping_lead"):
            chance *= 0.4                                # matching the leaders' pace: no need to react
        elif info.get("fading_leader"):
            chance *= 1.5                                # leader losing time: cover the undercut
        if self.rng.random() < min(0.9, chance) and not self.extra_stop(car_lap, st.planned_lap, car_lap + 1, p):
            st.planned_lap = car_lap + 1
            st.plan_note = f"reacts to #{who} pitting" + (" (undercut)" if behind else "")

    def fresh_tire_gain(self, car: CarSnapshot, st: CarState, car_lap: int) -> float | None:
        """Seconds per lap fresh tires are worth against this car's current set: from its own falloff
        this run, else the learned tire model x tire age. None = no data yet."""
        fall = self.pace_info.get(car.idx, {}).get("falloff")
        lt = self.lap_time or DEFAULT_LAP_TIME
        if fall is not None:
            return max(0.0, fall * lt)
        model = self.tire_model
        if model is not None and model.ready:
            return max(0.0, model.slope * max(0, car_lap - min(st.left_since, st.right_since)))
        return None

    @staticmethod
    def fuel_from(st: CarState) -> float:
        """Car lap its tank was full (or would have been, from the real fuel level)."""
        return st.fuel_start if st.fuel_start is not None else st.last_stop_lap

    @staticmethod
    def usable_stint(tank: float, margin: float) -> float:
        """Laps a full tank really gives between two stops: the tank, less the fuel cushion (at least 1.5
        laps), less the lap it takes to reach pit entry on the lap it pits."""
        return max(1.0, tank - max(1.5, margin) - 1.0)

    @classmethod
    def max_pit(cls, st: CarState, tank: float, margin: float = 1.5) -> int:
        """Last lap this car can pit on with the fuel it has."""
        return math.floor(cls.fuel_from(st) + cls.usable_stint(tank, margin))

    def learn_fuel(self, car: CarSnapshot, st: CarState, green: bool):
        """Fuel used per green-flag lap, per car: fuel burnt over the distance actually covered (laps +
        position round the lap) between two line crossings, so it stays exact at any time acceleration
        (the sample can be taken a little after the line). Median of the car's last 8 clean laps."""
        if car.fuel is None:
            st.fuel_mark = None
            return
        if not green or car.pitting or st.event is not None or car.on_pit_road or car.idx in self.behavior_cars:
            st.fuel_dirty = True
        tl = self.track_len if self.track_len > 100 else 0.0
        prog = car.laps_done + (min(car.lap_dist / tl, 0.999) if tl else 0.0)
        mark = st.fuel_mark
        if mark is None or car.laps_done < mark[0] or car.fuel > mark[1] + 0.05:
            st.fuel_mark, st.fuel_dirty = (car.laps_done, car.fuel, prog), True     # first look / refuelled
            return
        if car.laps_done == mark[0]:
            return
        span = prog - mark[2] if tl else float(car.laps_done - mark[0])
        used = (mark[1] - car.fuel) / span if span > 0 else 0.0
        ok = 0.7 <= span <= 2.5 and not st.fuel_dirty and used > 0
        if ok and self.field_rate:
            ok = 0.6 * self.field_rate <= used <= 1.6 * self.field_rate
        if ok:
            st.fuel_hist = (st.fuel_hist + [used])[-8:]
            st.fuel_rate = statistics.median(st.fuel_hist)
            st.fuel_samples += 1
        st.fuel_mark, st.fuel_dirty = (car.laps_done, car.fuel, prog), False

    def plan_still_good(self, st, car_lap, tank, fuel_left, to_go, p) -> str:
        """Crew chief checks the plan each lap against fuel and distance left. Returns why it must be redone."""
        if not isinstance(st.planned_lap, int) or st.in_box:
            return ""
        if st.plan_note.startswith(("reacts", "pits with", "flips", "undercut", "overcut")) or "tires" in st.plan_note:
            safety = 0.5                             # a deliberate move: only fuel running out undoes it
        else:
            safety = None
        if fuel_left - (1.0 if st.fuel_start is not None else p.fuel_margin) >= to_go:
            return "makes it on fuel now"
        out_lap = self.max_pit(st, tank, p.fuel_margin)
        if st.planned_lap > out_lap:
            return "fuel runs out sooner"
        if st.borderline and self.drafting and self.fuel_save_gain > 0 and st.planned_lap < out_lap - 1:
            return "fuel saved, the window moved"
        if safety is None and st.plan_tank and abs(tank - st.plan_tank) / st.plan_tank > 0.04:
            return f"fuel mileage ({tank:.0f} laps a tank)"
        return ""

    def fuel_model(self, car: CarSnapshot, st: CarState, car_lap: int, p: StrategyParams) -> tuple[float, float]:
        """(laps a full tank lasts for this car, laps of fuel left). From the real fuel level and the car's
        own green-flag mileage when the game shows them; else laps since the last stop on the preset tank."""
        if car.fuel is None or not car.fuel_cap or p.tank_laps <= 0:
            st.fuel_start = None
            return p.tank_laps, max(0.0, p.tank_laps - (car_lap - st.last_stop_lap))
        rate = (st.fuel_rate if st.fuel_samples >= 2 else None) or self.field_rate or car.fuel_cap / p.tank_laps
        tank = car.fuel_cap / rate
        if tank < MIN_TANK_LAPS and p.tank_laps >= MIN_TANK_LAPS:
            rate = car.fuel_cap / p.tank_laps                    # a bad mileage reading: not a 2-lap tank
            tank = p.tank_laps
        left = car.fuel / rate
        st.fuel_start = car_lap - (tank - left)
        return tank, left

    def drafting_partners(self, car: CarSnapshot, st: CarState, car_lap: int, infos, snap, p: StrategyParams):
        """Green flag: same-make cars running together line up their stops.
        Checked once a lap per car; the closer the partner, the likelier."""
        if isinstance(st.planned_lap, int) and st.planned_lap <= car_lap:
            return                                      # the stop is due now: it goes ahead as planned
        r = self.rules(p, snap.drafting)
        if snap.drafting and car.make and isinstance(st.planned_lap, int) and st.oem_lap != car_lap:
            self.block_stop(car, st, car_lap, p)
        team_on = p.team_strategy and car.team >= 0 and r.priority != "make_only"
        make_on = bool(p.oem_strategy and car.make) and r.priority != "team_only"
        if not ((make_on or team_on) and isinstance(st.planned_lap, int)) or st.oem_lap == car_lap:
            return
        st.oem_lap = car_lap
        me = infos.get(car.idx)
        window = max(0, r.window)
        if me is None or st.planned_lap - car_lap > window + (2 if team_on else 0):
            return
        team_first = r.priority in ("team", "team_only")
        player_stops = {g[0] for g in self.green_stops if g[2] >= self.lap_now - 1}   # the player pitted under green
        best = None
        for other in snap.cars:
            if other.idx == car.idx or not other.valid or other.reason_out:
                continue
            mate = team_on and other.team == car.team
            if not mate and not (make_on and other.make == car.make):
                continue
            oi = infos.get(other.idx)
            ost = self.cars.get(other.idx)
            if oi is None or ost is None or oi.down != me.down:
                continue
            o_lap = ost.planned_lap
            if not other.controlled:                       # the player: his green stop is the plan to join
                o_lap = None
                if mate and p.follow_player and other.idx in player_stops:
                    o_lap = car_lap
            if not isinstance(o_lap, int):
                continue
            d = abs(oi.gap - me.gap)
            gap_limit = (r.team_gap if mate else r.make_gap)
            any_gap = mate and gap_limit <= 0                  # teammates line up wherever they run
            win = window + (2 if mate else 0)
            if (not any_gap and d > gap_limit) or abs(o_lap - st.planned_lap) > win:
                continue
            if mate:
                weight = 0.95 if r.team_pits == "together" else 0.0
            else:
                weight = clamp(r.make_pits / 100.0)
            if self.pace_info.get(car.idx, {}).get("keeping_lead") and not mate:
                weight *= 0.4                                  # running lead-pack pace: keep running
            close = 1.0 if any_gap else (1 - d / max(0.01, gap_limit))
            if self.rng.random() < weight * close:
                rank_key = (mate == team_first, -o_lap)
                if best is None or rank_key > best[4]:
                    best = (o_lap, other.number, d, mate, rank_key)
        if best is None:
            return
        if best[0] > st.planned_lap and (best[0] > st.planned_lap + 1 or st.partner_later == st.last_stop_lap):
            return                                      # never chase a partner later (the lower-fuel car leads);
                                                        # one lap later at most, once a run
        if best[0] > st.planned_lap:
            st.partner_later = st.last_stop_lap
        lap = max(car_lap + 1, min(self.max_pit(st, p.tank_laps, p.fuel_margin), best[0]))
        if lap != st.planned_lap and not self.extra_stop(car_lap, st.planned_lap, lap, p):
            st.planned_lap = lap
            st.plan_note = (f"pits with teammate #{best[1]}" if best[3]
                            else f"pits with #{best[1]} ({car.make}, {best[2]:.1f}s)")

    # ------------------------------------------------------- caution tactics
    def late_caution_call(self, car, st, me: CarInfo, infos, to_go, p, prob_l, prob_r):
        """Car can make it on fuel: weigh fresh tires against track position.
        Returns (pit?, tires, note).

        When crew_brain is on and this car has a profile, the decision is
        delegated to crewchief.caution_call() which scores STAY_OUT /
        FOUR_TIRE / TWO_TIRE with cc_rating noise and teammate offset.
        The existing L/R service logic (prob_l/prob_r) still picks the side
        when the brain chooses TWO_TIRE.
        """
        if p.crew_brain and self.cc_profiles:
            profile = self.cc_profiles.get(car.number)
            if profile is not None:
                return self._brain_caution_call(car, st, me, infos, to_go, p, prob_l, prob_r, profile)

        laps_factor = to_go / (to_go + 15.0)            # how long fresh tires get to pay off
        info = self.pace_info.get(car.idx, {})
        tire_gain = me.age * laps_factor * info.get("k", 1.0)
        losses = self.tire_losses(car, st, self.lap_now) if p.value_tires else None
        if losses is not None:
            tire_gain = clamp(to_go * sum(losses) / 15.0, 0.0, 1.5)   # seconds a 4-tire stop gains, scaled
        threats, lost = [], 0.0
        for idx, oi in infos.items():
            if idx == car.idx or oi.down > 0:
                continue
            ost = self.cars.get(idx)
            if ost is None:
                continue
            behind = ost.green_gap - st.green_gap
            if behind <= 0:
                continue
            lost += 1 - oi.age                           # positions lost if I pit and they stay out
            if me.rank < oi.rank <= me.rank + p.threat_positions:
                pit_chance = oi.age
                threats.append(pit_chance * me.age + (1 - pit_chance) * (me.age - oi.age))
        threat = sum(threats) / len(threats) if threats else 0.0
        pos_cost = (lost / (lost + 3.0)) * (1.3 - laps_factor)
        bias = (p.track_position_bias - 50) / 50.0
        score = 2.0 * tire_gain + 1.2 * threat - 1.6 * pos_cost * (1 + bias)
        if info.get("keeping_lead"):
            score -= 0.3                                 # running lead-pack pace from further back: keep it
        pit = self.rng.random() < 1 / (1 + math.exp(-6 * (score - 0.1)))
        if not pit:
            return False, None, f"stays out: track position (tires {me.age:.0%} worn, {len(threats)} close behind)"
        need_l = prob_l / 100 * laps_factor * 1.4
        need_r = prob_r / 100 * laps_factor * 1.4
        thr = 0.4 + 0.3 * pos_cost
        if need_l >= thr and need_r >= thr:
            tires = FOUR_TIRES
        elif need_l >= thr or need_r >= thr:
            tires = LEFT_SIDE if need_l > need_r else RIGHT_SIDE
        else:
            tires = RIGHT_SIDE if need_r >= need_l else LEFT_SIDE
        vs = self.value_service(car, st, self.lap_now, to_go, True, p) if p.value_tires else None
        if vs is not None and vs[0] != FUEL_ONLY:
            tires = vs[0]                                 # 2 vs 4 by what they are worth over the laps left
        tires = self.limit_service(tires, st, p)
        n = {FOUR_TIRES: "4 tires", LEFT_SIDE: "2 tires", RIGHT_SIDE: "2 tires"}.get(tires, "no tires left")
        return True, tires, f"pits for {n} (tires {me.age:.0%} worn, {len(threats)} close behind)"

    def _brain_caution_call(self, car, st, me: CarInfo, infos, to_go, p,
                            prob_l, prob_r, profile):
        """Delegate to crewchief.caution_call(); handle service-side selection."""
        from .crewchief import caution_call as brain_call, FOUR_TIRE, TWO_TIRE

        # Find best-placed teammate's decision for offset signal.
        teammate_decision, teammate_rank = None, None
        if p.team_strategy and car.team >= 0:
            mates = [m for m in self.teams.get(car.team, [])
                     if m.idx != car.idx and m.idx in infos
                     and self.cars.get(m.idx) is not None
                     and self.cars[m.idx].decision is not None]
            if mates:
                lead = min(mates, key=lambda m: infos[m.idx].rank)
                teammate_decision = self.cars[lead.idx].decision
                teammate_rank = infos[lead.idx].rank

        # Blend in the game's strategy field (0-2 → 0-1).
        eff = profile.with_strategy((car.strategy or 0.0) / 2.0)

        pit, token, note = brain_call(
            track_type=p.track_type,
            tire_age=me.age,
            to_go=to_go,
            fuel_ok=True,       # caller ensures FUEL_MAKES_IT before reaching here
            me_rank=me.rank,
            field_size=max(1, len(infos)),
            profile=eff,
            rng=self.rng,
            teammate_decision=teammate_decision,
            teammate_rank=teammate_rank,
            laps_down=me.down,
            has_wave_around=(p.stages > 0),
        )
        if not pit:
            return False, None, note

        # Brain chose to pit: pick the service side with existing prob_l/prob_r logic.
        laps_factor = to_go / (to_go + 15.0)
        if token == FOUR_TIRE:
            tires = FOUR_TIRES
        else:
            # TWO_TIRE: use wear probabilities to pick the better side.
            need_l = prob_l / 100 * laps_factor * 1.4
            need_r = prob_r / 100 * laps_factor * 1.4
            tires = LEFT_SIDE if need_l >= need_r else RIGHT_SIDE

        vs = self.value_service(car, st, self.lap_now, to_go, True, p) if p.value_tires else None
        if vs is not None and vs[0] != FUEL_ONLY:
            tires = vs[0]
        tires = self.limit_service(tires, st, p)
        return True, tires, note

    def follow_call(self, car, st, me: CarInfo, infos, snap, p) -> tuple[bool, str] | None:
        """Lead pack follows the leader; same-make drafting partners follow each other."""
        if me.down == 0 and me.rank > 1 and self.leader_call is not None:
            span = max(0.1, p.lead_pack_gap)
            if st.green_gap <= span and self.rng.random() < 0.9 * (1 - st.green_gap / (span * 1.5)):
                return self.leader_call, "follows the leader"
        r = self.rules(p, snap.drafting)
        order = ("make", "team") if r.priority in ("make", "make_only") else ("team", "make")
        for kind in order:
            if kind == "team" and p.team_strategy and car.team >= 0 and r.priority != "make_only":
                mates = [m for m in self.teams.get(car.team, []) if m.idx != car.idx and m.idx in infos
                         and infos[m.idx].rank < me.rank and self.cars.get(m.idx) is not None
                         and self.cars[m.idx].decision is not None]
                if mates:
                    lead = min(mates, key=lambda m: infos[m.idx].rank)
                    if self.rng.random() < clamp(r.team_follow / 100.0):
                        return self.cars[lead.idx].decision, f"follows teammate #{lead.number}"
            if kind == "make" and p.oem_strategy and car.make and r.priority != "team_only":
                weight = clamp(r.make_follow / 100.0)
                if self.pace_info.get(car.idx, {}).get("keeping_lead"):
                    weight *= 0.4
                gap = max(0.01, r.make_gap)
                for other in snap.cars:
                    if other.idx == car.idx or other.make != car.make:
                        continue
                    ost, oi = self.cars.get(other.idx), infos.get(other.idx)
                    if ost is None or oi is None or ost.decision is None or oi.down != me.down:
                        continue
                    d = st.green_gap - ost.green_gap
                    if 0 < d <= gap and self.rng.random() < weight * (1 - d / gap):
                        return ost.decision, f"follows #{other.number} ({car.make})"
        return None

    def wear_level(self, p: StrategyParams) -> str:
        """'high' / 'normal' / 'low': how much lap time a full fuel run on one set of tires costs here,
        from the learned tire model (seconds per lap per lap of tire age) - the biggest strategy
        difference between intermediate tracks."""
        model = self.tire_model
        lt = self.lap_time or DEFAULT_LAP_TIME
        if model is None or not model.ready or p.tank_laps <= 0:
            return "normal"
        frac = max(0.0, model.slope) * p.tank_laps / lt        # loss at the end of a run, share of a lap
        return "high" if frac > 0.05 else "low" if frac < 0.015 else "normal"

    def block_stop(self, car, st, car_lap, p):
        """Superspeedway green cycles are scripted: whole manufacturer blocks dive onto pit road together.
        The first car of a make to plan sets the block's lap; the others join it when it is in their window."""
        blk = self.block_plans.get(car.make)
        if blk is None or blk <= car_lap:
            self.block_plans[car.make] = st.planned_lap
            return
        if blk == st.planned_lap or st.plan_note.startswith("pits with the"):
            return
        lo = st.window[0] if st.window else car_lap + 1
        if lo <= blk <= self.max_pit(st, p.tank_laps, p.fuel_margin) and abs(blk - st.planned_lap) <= 6 \
                and not self.extra_stop(car_lap, st.planned_lap, max(car_lap + 1, blk), p) \
                and self.rng.random() < 0.85:
            st.planned_lap = max(car_lap + 1, blk)
            st.plan_note = f"pits with the {car.make} block"

    def extra_stop(self, car_lap: int, old, new: int, p: StrategyParams) -> bool:
        """Moving this car's stop earlier, from `old` to `new` (car laps), would cost it another green stop
        before its next sure stop (the next stage break, else the finish). Joining a teammate, a make's block
        or answering an undercut must never do that (Daytona report: blocks and teammates pulled stops 5-7 laps
        early, again and again, and cars made three stops in a stage that needs two)."""
        if not isinstance(old, int) or new >= old or p.tank_laps <= 0:
            return False
        end = self.next_stage_end()
        target = (end if end is not None else self.lap_total) - (self.lap_now - car_lap)
        usable = self.usable_stint(p.tank_laps, p.fuel_margin)

        def stops(start):                                 # green stops still needed after a full tank at `start`
            return max(0, math.ceil((target - start) / usable) - 1) if target > start else 0
        return stops(new) > stops(old)

    def rules(self, p: StrategyParams, drafting: bool | None = None) -> TeamRules:
        return p.rules_draft if (self.drafting if drafting is None else drafting) else p.rules_other

    def caution_fuel_call(self, st, fuel_left, to_go, p, me: CarInfo | None = None
                          ) -> tuple[bool | None, str, bool]:
        """Fuel side of a caution call: (pit? or None = no opinion, note, must)."""
        tank = p.tank_laps
        if tank <= 0 or st.fuel_state == FUEL_MAKES_IT:
            return None, "", False
        if self.drafting:
            # drafting tracks: fuel is everything, but a stop has to change the fuel math. A car whose fuel reaches
            # the next sure chance (the stage break is a free stop) stays out; else it pits when a fill now gets it
            # there, or saves a stop before it. (It used to pit under nearly every caution when under ~80 % full:
            # a 30-lap tank in a 57-lap stage with cautions every few laps gave 3-4 stops a stage, live 2026-10-26.)
            need = self.laps_to_next_chance(to_go)
            where = "stage break" if self.next_stage_end() is not None else "finish"
            if fuel_left - p.fuel_margin >= need:
                return None, "", False
            if tank - p.fuel_margin >= need:
                return True, f"fuel: {fuel_left:.0f} laps left, a fill makes the {where}", True
            usable = self.usable_stint(tank, p.fuel_margin)

            def stops_before(f):                          # stops needed before the next sure chance on f laps
                return math.ceil(max(0.0, need - f + p.fuel_margin) / usable)
            if stops_before(fuel_left) > stops_before(tank):
                return True, f"fuel: filling now saves a stop before the {where}", True
            full = fuel_left / tank                       # nearly empty: it must stop soon whatever happens
            if full < 1.0 - p.ss_topoff_pct / 100.0:
                return True, f"fuel ({full:.0%} of a tank left)", True
            return None, "", False
        need = self.laps_to_next_chance(to_go)
        # a car that can't reach the finish only has to take the caution stop for fuel when the fuel won't last to the
        # next likely caution (1.5 x the race's average laps between yellows so far) - else it would stop under
        # green. Never above the old caps (a green stop costs most at short tracks and road courses). Filling up for
        # the finish or to save a green stop are their own checks below. (A flat 0.9 of a tank at short tracks pulled
        # the whole field in under every yellow of a 500-lap race - #40, Bristol 2026-11-02, leading with 90 % fuel
        # and good tires, six caution stops in 170 laps, cautions every ~30 laps.)
        full_enough = {SHORT_TRACK: 0.9, ROAD_COURSE: 0.75}.get(p.track_type, 0.5)
        gap = self.lap_now / max(1, self.cautions) if self.lap_now > 0 else tank
        soon = clamp(1.5 * gap, 0.25 * tank, full_enough * tank)
        if fuel_left - p.fuel_margin < need and fuel_left <= soon:
            # it would have to stop under green soon after the restart: take the stop now
            where = "stage break" if self.next_stage_end() is not None else "finish"
            return True, f"fuel: {fuel_left:.0f} laps left, can't reach the {where}", True
        usable = self.usable_stint(tank, p.fuel_margin)

        def green_stops(f):                            # green stops to the next sure chance on f laps of fuel
            return 0 if f - p.fuel_margin >= need else math.ceil((need - f + p.fuel_margin) / usable)
        if green_stops(fuel_left) > green_stops(tank) and self.rng.random() < 0.9:
            # filling up now saves a green flag stop before the next sure chance (a green stop costs about a
            # lap): the crew chief takes it, leaders included - the few who stay out gamble on another yellow
            where = "stage break" if self.next_stage_end() is not None else "finish"
            return True, f"fuel: filling now saves a green flag stop (reaches the {where})", True
        short = to_go - (tank - p.fuel_margin)        # laps it would still be short after filling now
        if short <= p.fuel_reach:
            return True, ("fills up to make the finish" if short <= 0
                          else f"fuel: {short:.0f} laps short of the finish if it fills now"), True
        used = 1 - fuel_left / tank                    # a caution stop is cheap once enough fuel is gone
        chance = clamp((used - 0.30) / 0.30)
        if me is not None and me.down == 0:
            # lead lap cars give up track position for it: later in the run, and less the further up they run
            field = max(10, len(self.cars))
            bias = clamp(1 - (p.track_position_bias - 50) / 100, 0.5, 1.5)
            chance = clamp((used - 0.45) / 0.35) * (0.35 + 0.65 * clamp((me.rank - 1) / (0.6 * field))) * bias
        if self.rng.random() < chance:
            return True, f"free stop ({used:.0%} of the tank used)", False
        return None, "", False

    def caution_decision(self, car, st, me, infos, snap, p, race, to_go, prob_l, prob_r, fuel_left=0.0):
        st.note = ""
        st.ai_must = ""
        st.tires_good = False
        if car.waved and not car.free_pass and race and not st.forced_damage:
            # already waved around (Race Control may credit the lap mid-caution, so it can read as a lead lap
            # car now): a pit stop after the wave gives the lap back up
            st.decision, st.note = False, "waved around: stays out to keep the lap"
            st.wave_hold = True
            return
        since = (car.laps_done + 1) - st.last_stop_lap if car.laps_done > 0 else 99
        if (race and st.stops > 0 and since <= max(3, p.min_stop_gap) and not st.forced_damage
                and fuel_left >= min(to_go, 0.6 * p.tank_laps)):
            # it has just made its stop (often a green stop the caution then caught a lap down): fresh tires
            # and fuel - no second stop on top of it, whatever the teammates, the stage break or the free
            # stop logic would say
            st.decision, st.note = False, f"pitted {since} lap(s) ago: stays out"
            if me is not None and me.down >= 1 and self.wave_candidate(me, infos, snap, car.idx):
                st.wave_hold = True
            return
        if car.free_pass and me is not None and me.down >= 1:
            # Race Control's free pass: it gets its lap back at the green - call it like a lead lap car
            me = dataclasses.replace(me, down=0)
        must = race and (st.forced_damage or st.fuel_state == FUEL_WINDOW)
        tactics = race and p.position_strategy and me is not None
        fuel_pit, fuel_note, fuel_must = (self.caution_fuel_call(st, fuel_left, to_go, p, me) if race
                                          else (None, "", False))
        if (tactics and p.lapped_wave and me.down >= 1
                and self.wave_candidate(me, infos, snap, car.idx)):
            # in line for the wave around: a lap back beats anything but damage, worn out tires or an empty tank
            why = self.wave_service_need(car, st, fuel_left, to_go, p, prob_l, prob_r)
            if why:
                st.decision, st.note = True, f"gives up the wave around: {why}"
            else:
                st.decision, st.note = False, "stays out for the wave around"
                st.wave_hold = True
            return
        if race and self.stage_break is not None:
            if self.stage_break_call(car, st, me, infos, p, to_go, prob_l, prob_r, fuel_left):
                if me is not None and me.rank == 1:
                    self.leader_call = st.decision
                return
        must = must or fuel_must
        if must:
            st.decision = True
            st.note = fuel_note
            st.ai_must = "damage" if st.forced_damage else (fuel_note or "fuel")
        elif self.drafting and st.fuel_state == FUEL_MAKES_IT:
            st.decision, st.note = False, "makes it on fuel: track position"
        elif tactics and me.down == 0 and st.fuel_state == FUEL_MAKES_IT:
            pit, tires, st.note = self.late_caution_call(car, st, me, infos, to_go, p, prob_l, prob_r)
            st.decision = pit
            if pit:
                st.service = tires
        else:
            wants = self.rng.randint(30, 100) <= st.urgency * self.pace_info.get(car.idx, {}).get("k", 1.0)
            if wants:
                if st.service == FUEL_ONLY and not st.forced_damage:
                    if st.fuel_state == FUEL_MAKES_IT:
                        st.decision = False
                    else:
                        st.decision = self.rng.randint(1, 100) <= 20
                else:
                    st.decision = True
            else:
                st.decision = False
            if fuel_pit and not st.decision:
                st.decision, st.note = True, fuel_note
            if car.free_pass:
                st.note = (st.note + "; " if st.note else "") + "free pass: gets its lap back"
            elif tactics and me.down >= 1 and self.lucky_dog(me, infos):
                st.note = (st.note + "; " if st.note else "") + "lucky dog: gets the lap back"
        if tactics and not must:
            follow = self.follow_call(car, st, me, infos, snap, p)
            if follow is not None and follow[0] != st.decision:
                st.decision, st.note = follow
        if st.decision and not must and tactics and me.down == 0:
            self.good_tires_call(car, st, me, p, to_go)
        if me is not None and me.rank == 1:
            self.leader_call = st.decision

    def good_tires_call(self, car, st, me: CarInfo, p, to_go: float):
        """Driver behavior on: a lead lap car about to pit whose tires are still good - it managed them, or it is
        on a stretch run - weighs track position: it stays out, or takes 2 tires. Michigan 2026-10-28: #9 managed
        his tires better than anyone, then followed the field in for 4 at an untimely caution."""
        if not self.behavior_on or self.drafting or st.forced_damage or to_go <= 15:
            return
        worn = self.tire_ratio(car, p)
        m = self.tire_manager_ratio(car)
        managed = m is not None and m <= 0.85
        if worn > 0.6 or not (managed or st.style == STRETCH):
            return
        temper = temperament(car.name)
        stay = 0.2 + 0.25 * (1.0 - temper) + (0.15 if me.rank <= 10 else 0.0) + (0.1 if worn <= 0.35 else 0.0)
        how = f"managed them ({m:.0%} of the field's falloff)" if managed else "stretch run"
        if self.rng.random() < stay:
            st.decision = False
            st.note = f"stays out: tires still good ({worn:.0%} of the wear limit, {how}), keeps track position"
            return
        st.tires_good = True
        st.note = (st.note + "; " if st.note else "") + f"tires still good ({worn:.0%} of the wear limit, {how})"

    # ------------------------------------------------------------- stages
    def update_stages(self, p: StrategyParams, total: int, lap_now: int, override: tuple | None = None):
        """Race laps where stages end; stages that ended without a caution are marked done. `override`:
        the stage ends Race Control is running (it throws the stage cautions), used over the preset's."""
        ends = []
        self.stage_source = "preset"
        if override is not None and total > 0:
            ends = [e for e in override if 0 < e < total]
            self.stage_source = "RaceControl"
        elif p.stages >= 2 and total >= 10:           # (no stages before the race length is known)
            for v in list(p.stage_ends)[:p.stages - 1]:
                try:
                    lap = round(total * float(v) / 100.0) if p.stage_by == "percent" else int(float(v))
                except (TypeError, ValueError):
                    continue
                if 0 < lap < total:
                    ends.append(lap)
        self.stage_list = sorted(set(ends))
        for e in self.stage_list:
            if lap_now > e + 2 and e not in self.stage_done and self.stage_break != e:
                self.stage_done.add(e)                    # ran past it under green (no stage caution)

    def next_stage_end(self) -> int | None:
        for e in self.stage_list:
            if e not in self.stage_done and self.stage_break != e:
                return e
        return None

    def stage_no(self, end: int) -> int:
        return self.stage_list.index(end) + 1 if end in self.stage_list else 0

    def laps_to_next_chance(self, to_go: float) -> float:
        """Laps (race laps) until the next sure chance to pit: the coming stage break, else the finish."""
        e = self.next_stage_end()
        if e is not None and self.lap_now <= e + 1:
            return e + 2 - self.lap_now
        return to_go

    DEFAULT_CAUTION_COVER = 3.0              # laps a caution is assumed to last until this race has shown some

    def caution_cover(self) -> float:
        """Laps a caution lasts here: the median of this race's cautions so far (last 6), else a default."""
        if self.caution_lens:
            return float(statistics.median(self.caution_lens[-6:]))
        return self.DEFAULT_CAUTION_COVER

    def break_window(self, p: StrategyParams) -> int:
        """How many laps before the stage end a caution still counts as that stage's break. stage_early > 0 is a
        fixed number of laps. 0 = automatic: the laps the caution is expected to last, less the laps the crew
        chief needs to get the stop called and the cars onto pit road, so the leader still reaches the stage end
        under this yellow and the service is at the break, not a stop before it."""
        if p.stage_early > 0:
            return int(p.stage_early)
        if getattr(self, "stage_source", "preset") == "RaceControl":
            return 0        # Race Control throws its own stage caution on the last lap: an earlier one is a normal
                            # caution (only one still out at the stage end is the break: the late catch in tick)
        return max(0, int(self.caution_cover() - max(0.0, p.stage_call_lead) + 1e-9))

    def stage_break_start(self, p: StrategyParams, lap_now: int, window: int | None = None):
        """A caution just came out: is it the break after a stage (at or just before the stage end)?"""
        self.stage_break = None
        e = self.next_stage_end()
        if window is None:
            window = self.break_window(p)
        if e is not None and e - max(0, window) <= lap_now <= e + 2:
            self.stage_break = e
            self.stage_done.add(e)

    def plan_stage(self, st: CarState, p: StrategyParams, rating: int, car_lap: int, lap_offset: int,
                   end: int, me: CarInfo | None, wear_need: float, tank: float, to_go: float):
        """Green flag with a stage break coming: the break is a sure chance to pit. A car that can reach it
        waits for it (or flips the stage); one that can't splits or stretches the stage with the normal
        stint styles, aiming its stop so it reaches the break."""
        n = self.stage_no(end)
        end_car = end - lap_offset                       # this car's lap when the stage ends
        reach = self.fuel_from(st) + tank - p.fuel_margin  # last lap its fuel covers
        if self.drafting and self.fuel_save_gain > 0:     # the pack saves fuel the rest of the way (see plan_stop)
            reach += self.fuel_save_gain * max(0.0, min(end_car, reach) - car_lap - 3) * 0.7
        if reach >= end_car + 2:
            if st.stage_serviced == end:                  # already took the break service at the caution: no flip
                st.stage_checked, st.flip_stage = end, None
            if st.stage_checked != end:
                st.stage_checked = end
                flip = self.flip_lap(st, p, rating, car_lap, lap_offset, end, me, tank)
                st.flip_stage = end if flip is not None else None
                if flip is not None:
                    st.plan_note = f"flips stage {n}: pits before the break for track position"
                    st.style = ""
                    return flip
            if st.flip_stage == end and st.flipped != end:
                return self.flip_lap(st, p, 100, car_lap, lap_offset, end, me, tank, force=True)
            st.plan_note = f"pits at the stage {n} break (lap {end})"
            st.style, st.window = "", None
            return STAGE_BREAK
        target = end_car + 2
        self._stage_plan = True
        try:
            planned = self.plan_stop(st, p, rating, car_lap, target - car_lap + 1, target, tank, me, wear_need,
                                     anchor="late" if p.track_type == SHORT_TRACK else "split")
        finally:
            self._stage_plan = False
        if planned == GAME_DECIDE and p.game_plan_off:
            planned = max(car_lap + 1, self.max_pit(st, tank, p.fuel_margin) - self.rng.randint(0, 3))
            st.plan_note = f"fuel window (stage {n})"
        elif isinstance(planned, int) and st.borderline:
            st.plan_note = f"fuel window (stage {n}): ~{st.borderline} laps short of one stop fewer, saving fuel"
        elif isinstance(planned, int):
            st.plan_note = f"{st.style + ' ' if st.style else ''}stage {n}"
        return planned

    def flip_lap(self, st, p, rating, car_lap, lap_offset, end, me, tank, force=False) -> int | None:
        """Lap to pit if the car flips the stage (pits a few laps before the break so it can stay out at
        the break and restart up front), or None. Cars outside the stage points with nothing to lose
        do it; the fuel has to reach well into the next stage."""
        end_car = end - lap_offset
        if me is None or me.down > 0 or end_car - car_lap < 3:
            return None
        cost = self.tire_model.green_loss if self.tire_model is not None else 25.0
        if self.lap_time and me.gap + cost >= 0.95 * self.lap_time:
            return None                                   # the leader would lap it while it pits: no flip
        if not force:
            # stage groups by running position: the top 10 race for stage points, 11-15 rarely gamble,
            # deeper in the field the track position at the restart is worth more than the points
            place = 0.0 if me.rank <= 10 else 0.3 if me.rank <= 15 else 1.0
            chance = clamp(p.flip_pct / 100.0) * place * (1.2 - rating / 200)
            if place <= 0 or self.rng.random() >= chance:
                return None
        later = [e for e in self.stage_list if e > end]
        nxt_car = (later[0] + 2 if later else self.lap_total) - lap_offset
        k = self.rng.randint(2, 6)
        lap = max(car_lap + 1, end_car - k)
        usable = self.usable_stint(tank, p.fuel_margin)

        def stops(start):                                 # green stops needed from a full tank at `start`
            return max(0, math.ceil((nxt_car - start) / usable) - 1)
        if stops(lap) > stops(end_car + 2):
            return None                                   # flipping would cost an extra stop next stage
        return lap

    def stage_break_call(self, car, st, me, infos, p, to_go, prob_l, prob_r, fuel_left) -> bool:
        """Caution at a stage break: nearly everyone pits. A car that flipped the stage stays out; one that
        pitted recently and can reach the next stage end (or the finish) may stay out for track position.
        Returns True when it decided."""
        n = self.stage_no(self.stage_break)
        if st.forced_damage:
            st.decision, st.note = True, f"stage {n} break: damage"
            return True
        if st.flipped == self.stage_break:
            st.decision, st.note = False, f"flipped stage {n}: stays out for track position"
            return True
        tank = p.tank_laps
        if tank <= 0:
            return False
        later = [e for e in self.stage_list if e > self.stage_break]
        nxt = (later[0] + 2 - self.lap_now) if later else to_go
        used = 1 - fuel_left / tank
        usable = self.usable_stint(tank, p.fuel_margin)

        def green_stops(f):
            return 0 if f - p.fuel_margin >= nxt else math.ceil((nxt - f + p.fuel_margin) / usable)

        recent = (st.pre_break == self.stage_break or st.stage_serviced == self.stage_break) and st.stops > 0
        worn = max(car.wear_rf, car.wear_rr, car.wear_lf, car.wear_lr) >= 0.35
        if (me is not None and me.down == 0 and (recent or used < 0.35) and not worn
                and green_stops(fuel_left) <= green_stops(tank)):
            # it pitted just before the break: stopping again only gives up the spots the others lose here
            stay = 1.0 - max(0, min(100, p.stage_double_back_pct)) / 100.0 if recent else 0.6
            if self.rng.random() < stay:
                st.decision, st.note = False, f"stage {n} break: pitted recently, stays out for track position"
                return True
        if fuel_left - p.fuel_margin >= nxt and me is not None and me.down == 0:
            if not later:                                 # final stage: the usual tires vs track position call
                pit, tires, st.note = self.late_caution_call(car, st, me, infos, to_go, p, prob_l, prob_r)
                st.decision = pit
                if pit:
                    st.service = tires
                st.note = f"stage {n} break: " + st.note
                return True
        st.decision, st.note = True, f"stage {n} break"
        return True

    def caution_service(self, car, st, me, to_go, p, base, tank) -> tuple[str, str]:
        """Tires on a caution stop, by track type, wear level and laps to go. Early and mid race the
        car takes what the track demands; late in the race (about the last third of a tank) cars running
        from 3rd to 15th gamble on 2 tires - or none - to beat the 4-tire cars off pit road."""
        late = to_go <= max(25.0, 0.35 * tank)
        gambler = me is not None and me.down == 0 and 3 <= me.rank <= 15
        wl, wr = (car.wear_lf + car.wear_lr) / 2, (car.wear_rf + car.wear_rr) / 2
        two = RIGHT_SIDE if wr >= wl else LEFT_SIDE
        r = self.rng.random()
        level = self.wear_level(p)
        kind = p.track_type
        if kind == SUPERSPEEDWAY:
            # grip is not the bottleneck: fuel, and two tires only when they are really worn
            worn = self.tire_ratio(car, p) >= 1.0
            if late:
                pick, why = (FUEL_ONLY, "fills up, no tires: track position") if r < 0.55 or not worn else \
                    (two, "fills up and 2 tires")
            else:
                pick, why = ((FOUR_TIRES, "") if self.tire_ratio(car, p) >= 1.2 else
                             (two, "") if worn else (FUEL_ONLY, "fuel only: tires are fine here"))
        elif kind == SHORT_TRACK:
            pick, why = FOUR_TIRES, ""
            if late and gambler:
                if r < 0.15:
                    pick, why = FUEL_ONLY, "no-tire gamble for track position"
                elif r < 0.5:
                    pick, why = two, "2-tire gamble for track position"
        elif kind == ROAD_COURSE:
            pick, why = FOUR_TIRES, ""
            if late and gambler and r < 0.3:
                pick, why = two, "2-tire gamble for track position"
        else:                                                    # intermediate
            if level == "high":
                pick, why = FOUR_TIRES, ""
                if late and gambler and r < 0.12:
                    pick, why = two, "2-tire gamble for track position"
            elif level == "low":
                pick, why = (base if base in (FOUR_TIRES, LEFT_SIDE, RIGHT_SIDE) else two), ""
                if late and gambler:
                    if r < 0.2:
                        pick, why = FUEL_ONLY, "fuel only: track position"
                    elif r < 0.65:
                        pick, why = two, "2-tire gamble for track position"
            else:
                pick, why = FOUR_TIRES, ""
                if late and gambler and r < 0.35:
                    pick, why = two, "2-tire gamble for track position"
        if st.tires_good and pick == FOUR_TIRES and kind != SUPERSPEEDWAY:
            # its tires are still good (good_tires_call): 2 usually do, for track position
            if self.rng.random() < (0.35 if level == "high" else 0.65):
                pick, why = two, "2 tires: his tires are still good"
        limited = self.limit_service(pick, st, p)
        if limited != pick:
            why = (why + "; " if why else "") + "no tires left for that"
        return limited, why

    def tire_ratio(self, car: CarSnapshot, p: StrategyParams) -> float:
        """Worst side's wear against its threshold (1.0 = at the threshold), not capped."""
        wl, wr = (car.wear_lf + car.wear_lr) / 2, (car.wear_rf + car.wear_rr) / 2
        return max(wl / p.wear_l if p.wear_l > 0 else 0.0, wr / p.wear_r if p.wear_r > 0 else 0.0)

    def wave_service_need(self, car, st, fuel_left, to_go, p, prob_l, prob_r) -> str:
        """Why a wave-around car must pit anyway ('' = it stays out): damage, tires worn out
        (at drafting tracks only near a blowout) or nearly out of fuel."""
        if st.forced_damage:
            return "damage"
        has_tires = self.limit_service(FOUR_TIRES, st, p) != FUEL_ONLY
        if self.drafting:
            if self.tire_ratio(car, p) >= BLOWOUT_RATIO and has_tires:
                return "tires near a blowout"
        elif has_tires:
            fall = self.pace_info.get(car.idx, {}).get("falloff")
            if fall is not None:
                if fall >= 0.03:                     # 3%+ slower than its early run: really worn out
                    return f"tires worn out ({fall:.1%} slower)"
            elif self.tire_ratio(car, p) >= 1.3:     # no lap time data: well past the wear threshold
                return "tires worn out"
        if p.tank_laps > 0 and fuel_left < to_go and fuel_left <= max(3.0, 0.15 * p.tank_laps):
            return f"nearly out of fuel ({fuel_left:.0f} laps left)"
        return ""

    def green_tire_call(self, car, st, car_lap, fuel_left, to_go, p) -> tuple[int | None, str]:
        """Green flag: tires going away. Crews almost never add a stop for tires alone - they bring the
        planned stop forward. Returns (new planned lap, note) or (None, "").
        - move the planned stop up (no extra stop) when the tires are at the threshold and the time they
          lose until then is worth a few seconds, as long as the stop is still in its fuel window;
        - an extra stop only when the time lost before the next stop is clearly (2x) more than the stop costs;
        - drafting tracks: only near a blowout."""
        if st.stops > 0 and car_lap - st.last_stop_lap < max(5, p.min_stop_gap):
            return None, ""
        if self.limit_service(FOUR_TIRES, st, p) == FUEL_ONLY:
            return None, ""                          # no tire sets left: a stop can't fix the tires
        if p.track_type in (SHORT_TRACK, ROAD_COURSE) or (p.track_type == INTERMEDIATE
                                                           and self.wear_level(p) == "low"):
            return None, ""                          # only fuel-forced green stops here
        raw = self.tire_ratio(car, p)
        ratio = raw * self.pace_info.get(car.idx, {}).get("k", 1.0)
        if self.drafting:
            return (car_lap + 1, "tires near a blowout") if raw >= BLOWOUT_RATIO else (None, "")
        # laps until the stop where it gets tires anyway
        if isinstance(st.planned_lap, int):
            until = st.planned_lap - car_lap
        elif st.planned_lap == STAGE_BREAK and self.next_stage_end() is not None:
            until = self.next_stage_end() + 1 - self.lap_now
        else:
            until = min(to_go, fuel_left)
        if until < 3:
            return None, ""
        cost = self.tire_model.green_loss if self.tire_model is not None else 25.0
        losses = self.tire_losses(car, st, car_lap) if p.value_tires else None
        gain = sum(losses) if losses is not None else self.fresh_tire_gain(car, st, car_lap)
        if gain is None:
            # no lap time data: the wear thresholds alone are not trusted with an extra stop unless extreme
            return ((car_lap + 1, f"tires gone ({ratio:.0%})") if ratio >= 2.0 else (None, ""))
        lt = self.lap_time or DEFAULT_LAP_TIME
        gain = min(gain, 0.06 * lt)                          # worn tires: a few % of a lap, never more
        lost = until * gain
        if ratio < 1.0:
            return None, ""
        # Crews keep to the fewest stops; wear moves the stop a few laps (undercut / overcut, see
        # tire_management). An extra stop only for a car whose tires are going away far worse than the
        # field's (a bad set, a handling problem), when it would lose well over twice what the stop costs.
        m = self.tire_manager_ratio(car)
        if m is not None and m >= 2.0 and lost > 2 * cost:
            return car_lap + 1, (f"tires going away ({m:.1f}x the field's falloff): {lost:.0f}s to lose "
                                 f"before the next stop (stop costs {cost:.0f}s)")
        return None, ""

    def field_slope(self) -> float | None:
        """Typical falloff (s/lap per lap) right now: the learned tire model, else the median of the
        current runs (refreshed every 10 game seconds)."""
        if self.now - self._field_slope_t > 10 or self._field_slope is None:
            self._field_slope_t = self.now
            vals = [v for v in (self.pace.run_slope(i, skip=4) for i in list(self.pace.stints)) if v is not None]
            self._field_slope = statistics.median(vals) if len(vals) >= 5 else None
        if self._field_slope:
            return self._field_slope                  # the field's current runs, measured the same way
        model = self.tire_model
        if model is not None and model.ready and model.slope and model.slope > 0:
            return model.slope
        return None

    def tire_manager_ratio(self, car: CarSnapshot) -> float | None:
        """This run's falloff against the field's: under 1 = saving its tires (a long-run car, or a good
        set), over 1 = going away faster. None until the run has 8+ clean laps."""
        st = self.pace.stints.get(car.idx)
        if st is None or len(st.laps) < 8:
            return None
        own = self.pace.run_slope(car.idx, skip=4)
        typical = self.field_slope()
        if own is None or not typical or typical <= 0.002:
            return None
        return max(0.0, own) / typical

    def tire_management(self, car, st, car_lap, p) -> None:
        """Once a run, near the planned stop: a car falling off faster than the field brings the stop
        forward 1-3 laps (undercut, where wear matters); one saving its tires stays out 2-4 laps longer
        (overcut) so it has the freshest tires for the next run. Always inside the fuel window."""
        if not isinstance(st.planned_lap, int) or st.window is None or st.tire_adjusted == st.last_stop_lap:
            return
        if self.drafting or st.planned_lap - car_lap > 8 or st.planned_lap - car_lap < 1:
            return
        m = self.tire_manager_ratio(car)
        if m is None:
            return
        st.tire_adjusted = st.last_stop_lap
        lo, hi = st.window
        if m >= 1.25 and p.track_type == INTERMEDIATE and self.wear_level(p) != "low" and not st.borderline:
            new = max(lo, car_lap + 1, st.planned_lap - self.rng.randint(1, 3))
            if new < st.planned_lap:
                st.plan_note = (f"undercut: tires going away ({m:.0%} of the field's falloff), "
                                f"in {st.planned_lap - new} laps early")
                st.planned_lap = new
        elif m <= 0.8:
            new = min(hi, st.planned_lap + self.rng.randint(2, 4))
            if new > st.planned_lap:
                st.plan_note = (f"overcut: managing tires ({m:.0%} of the field's falloff), "
                                f"stays out {new - st.planned_lap} more laps")
                st.planned_lap = new

    def wave_candidate(self, me: CarInfo | None, infos, snap, idx: int | None = None) -> bool:
        """Lapped car running ahead of the leader on track (between the leader and the pace car): if it stays
        out it is waved around and gets a lap back. A lead lap car also in front of the leader (caught there,
        nearly a lap down when the caution came out) is sent around too and does not stop the wave around."""
        if me is None or me.down < 1 or me.rank == 1 or not infos or self.track_len <= 0:
            return False
        if not snap.wave_around:
            return False                                  # no wave around in this race: nothing to wait for
        leader = min(infos.values(), key=lambda i: i.rank)
        lead_frac = leader.progress % 1.0
        ahead = (me.progress % 1.0 - lead_frac) % 1.0          # how far round the lap it is ahead of the leader
        pace = 0.5
        if snap.pace_dist is not None:
            pace_ahead = (snap.pace_dist / self.track_len - lead_frac) % 1.0
            if pace_ahead > 0.02:
                pace = pace_ahead
        return 0 < ahead < pace

    @staticmethod
    def lucky_dog(me: CarInfo, infos) -> bool:
        """First car one lap down: gets its lap back under the free pass anyway."""
        one_down = [i for i in infos.values() if i.down == 1]
        return bool(one_down) and me.down == 1 and me.rank == min(i.rank for i in one_down)

    def second_topoff(self, st, me, car_lap, rating, room: float = 1.0, short: bool = True):
        """Drafting tracks: under a long caution, aggressive or hopeless cars come back
        for another splash so their final stop is as short as possible - only with real room in the tank (room =
        share of the tank empty) and while the fuel still doesn't reach the next sure chance (short)."""
        if st.human_caution == self.cautions and st.human_key is not None:
            return                                        # the crew chief on the phone made this caution's call
        if room < 0.10 or not short:
            return                                        # (live: second stops that added 0-1 % fuel)
        if not self.drafting or me is None or st.topoffs or st.topoff_lap == car_lap or car_lap <= st.last_stop_lap:
            if st.topoff_lap == -1:
                st.topoff_lap = car_lap
            return
        st.topoff_lap = car_lap
        if me.down > 0 or me.rank > 25:
            chance = 0.40                                 # nothing to lose
        elif me.rank > 15:
            chance = 0.15
        else:
            chance = 0.03
        chance *= 1.3 - rating / 100                      # cautious teams less likely
        if self.rng.random() < chance:
            st.topoffs += 1
            st.post_stop_lock = False
            st.decision = True
            st.service = FUEL_ONLY
            st.note = "back in to top off"

    def wear_need_value(self, tank: float, p: StrategyParams) -> float | None:
        """Learned version of wear_need: lap time lost by the end of a fuel run vs the 'worn' level."""
        model = self.tire_model
        if not (p.value_tires and model is not None and model.ready and self.lap_time):
            return None
        return model.slope * tank / (self.lap_time * p.worn_pct / 100)

    @staticmethod
    def wear_need(car: CarSnapshot, st: CarState, car_lap: int, tank: float, p: StrategyParams) -> float:
        """Projected wear vs threshold (1.0 = at threshold) if the car ran to the
        end of its fuel on the tires it has now."""
        need = 0.0
        for wear, since, thr in (((car.wear_lf + car.wear_lr) / 2, st.left_since, p.wear_l),
                                 ((car.wear_rf + car.wear_rr) / 2, st.right_since, p.wear_r)):
            laps_on = car_lap - since
            if thr > 0 and laps_on >= 3:
                need = max(need, (wear + wear / laps_on * max(0, StrategyEngine.fuel_from(st) + tank - car_lap))
                           / thr)
        return need

    # ------------------------------------------------------------ value-based tires
    def tire_losses(self, car: CarSnapshot, st: CarState, car_lap: int) -> tuple[float, float] | None:
        """(left, right) seconds per lap this car is losing to fresh tires right now.
        From the track's learned falloff (s/lap per lap of tire age) and each side's age, shared
        between the sides by their wear, then scaled to this car's own measured falloff."""
        model = self.tire_model
        if model is None or not model.ready:
            return None
        slope = max(0.0, model.slope)
        wl, wr = (car.wear_lf + car.wear_lr) / 2, (car.wear_rf + car.wear_rr) / 2
        share_r = wr / (wl + wr) if wl + wr > 0 else 0.5
        age_l, age_r = max(0, car_lap - st.left_since), max(0, car_lap - st.right_since)
        loss_l = slope * age_l * (1 - share_r)
        loss_r = slope * age_r * share_r
        own = self.pace.run_slope(car.idx)
        if own is not None and slope > 0:               # this car falls off faster/slower than typical
            k = clamp(own / slope, 0.75, 1.5) if own > 0 else 0.75
            loss_l, loss_r = loss_l * k, loss_r * k
        return loss_l, loss_r

    def service_value(self, svc: str, losses, laps: float, caution: bool, p: StrategyParams) -> float:
        """Seconds gained over `laps` by this service, minus the extra time in the box compared
        with fuel only (under caution that time costs places, so it weighs more)."""
        model = self.tire_model
        loss_l, loss_r = losses
        gain = laps * ((loss_l if svc in (FOUR_TIRES, LEFT_SIDE) else 0) +
                       (loss_r if svc in (FOUR_TIRES, RIGHT_SIDE) else 0))
        cls = {FOUR_TIRES: "4", LEFT_SIDE: "2", RIGHT_SIDE: "2"}.get(svc, "F")
        extra = max(0.0, model.stop_time(cls) - model.stop_time("F"))
        weight = 1.0 + (1.0 + 2.0 * p.track_position_bias / 100) if caution else 1.0
        return gain - extra * weight

    def value_service(self, car, st, car_lap, laps, caution, p, rating=50) -> tuple[str, str] | None:
        """Best service by value (fuel only / right / left / four), within the tire-set limit.
        A little team-personality noise keeps it from being robotic. None = model not ready."""
        if not p.value_tires:
            return None
        losses = self.tire_losses(car, st, car_lap)
        if losses is None:
            return None
        best, best_v, values = FUEL_ONLY, 0.0, {}
        noise = (100 - rating) / 100 * 1.5
        for svc in (FOUR_TIRES, RIGHT_SIDE, LEFT_SIDE):
            if self.limit_service(svc, st, p) != svc:
                continue
            v = self.service_value(svc, losses, laps, caution, p) + self.rng.uniform(-noise, noise)
            values[svc] = v
            if v > best_v:
                best, best_v = svc, v
        gain4 = laps * (losses[0] + losses[1])
        return best, f"tires worth {gain4:.1f}s over {laps:.0f} laps"

    def game_stop_service(self, st: CarState, p: StrategyParams, prob_l: int, prob_r: int) -> str:
        """Service for a stop the game asked for under green. The game usually
        wants tires; giving it fuel only sends the car straight back in a few
        laps later, so the more worn side is changed when it is worn at all."""
        if prob_l >= 70 and prob_r >= 70:
            svc = FOUR_TIRES
        elif prob_r >= 40 or prob_l >= 40:
            svc = RIGHT_SIDE if prob_r >= prob_l else LEFT_SIDE
        else:
            svc = FUEL_ONLY
        out = self.limit_service(svc, st, p)
        if out == FUEL_ONLY and svc != FUEL_ONLY:
            # preferred side used up: try the other side
            other = LEFT_SIDE if svc == RIGHT_SIDE else RIGHT_SIDE
            out = self.limit_service(other, st, p) if svc != FOUR_TIRES else out
        return out

    # ------------------------------------------------------------------- tick
    def tick(self, snap: RaceSnapshot, p: StrategyParams) -> TickResult:
        res = TickResult()
        wall = self.clock()
        self.now = self.advance_clock(snap, wall)
        self._in_tick = True
        dt = 0.0 if self.last_now is None else max(0.0, self.now - self.last_now)
        self.last_now = self.now
        green = snap.flag == mm.FLAG_GREEN
        race = snap.session == mm.SESSION_RACE
        tank = p.tank_laps
        lap_now = math.floor(snap.laps_done)
        total = snap.laps_total
        if race and total > 0:
            # overtime (green-white-checkered): a caution at the end extends the race. Plan fuel for the caution
            # laps plus the green and white laps, and don't call the race over at the scheduled distance.
            end = max(self.ot_total or 0, total)
            if (not green and lap_now >= end - OT_CAUTION_LAPS and self.race_started
                    and snap.laps_done <= end + 0.5):    # (not after the checkered flag)
                self.ot_pending = True                   # a caution this late may well end in overtime
            if self.ot_pending:
                total = max(total, lap_now + (0 if green else OT_CAUTION_LAPS) + OT_GREEN_LAPS)
                if green:                                # restart: green, white, checkered if past the distance
                    if lap_now + OT_GREEN_LAPS - 1 > snap.laps_total:
                        self.ot_total = lap_now + OT_GREEN_LAPS - 1
                    self.ot_pending = False
                    total = max(snap.laps_total, self.ot_total or 0)
            if self.ot_total:
                total = max(total, self.ot_total)
        if total > 0 and lap_now > total:
            total = lap_now
        to_go = 0.0
        if snap.laps_done > 0 and total > 0:
            to_go = total - (snap.laps_done - 1)
        res.laps_to_go = to_go
        res.laps_total = total
        self.lap_now = lap_now
        self.lap_total = total
        if race and (green or lap_now >= 2):
            self.race_started = True                     # the green has flown (or AIMOD joined a race under way)
        if race:
            self.update_stages(p, snap.laps_total, lap_now, snap.stage_ends)
        self._race = race
        if self.tire_model is not None:                   # finished runs (practice too) teach the tire model
            for slope, laps in self.pace.take_finished():
                self.tire_model.add_run(slope, laps, practice=not race)

        if (self.last_flag == mm.FLAG_GREEN) != green:
            for st in self.cars.values():
                st.post_stop_lock = False
                st.planned_lap = None
                st.plan_note = ""
                st.cancel_at_green = (green and st.event is not None and not st.event.green
                                      and st.event.state in ("requested", "in"))
                if st.in_box:
                    st.decision = True           # mid-stop when the flag changed: let it finish
                else:
                    st.decision = None
                    st.service = None
                    st.done_service = None
                st.wave_hold = False
                st.game_accepted = False
            self.leader_call = None
            for st in self.cars.values():
                st.topoffs, st.topoff_lap = 0, -1
            if not green and race:
                self.cautions += 1
                self.caution_start = self.now
                self.caution_wall = wall
                self.pits_open_at = None
                self._caution_lap0 = lap_now
                if self.race_started:
                    self.stage_break_start(p, lap_now)
            if green:
                if race and self._caution_lap0 is not None and lap_now >= self._caution_lap0:
                    self.caution_lens.append(lap_now - self._caution_lap0)
                self._caution_lap0 = None
                self.stage_break = None
            if green:
                self.green_lap = lap_now
                for car in snap.cars:
                    st = self.state(car.idx)
                    st.damage_ref = car.damage
                    st.forced_damage = False
            # (no reseed here: the live engine's Random() is already unseeded, and a seeded one passed in by
            # tests / racesim must stay reproducible)
        if race and not green and self.race_started and self.stage_break is None:
            # The caution was out before the stage end was close enough to call it the break, and is still out when
            # the leader reaches the stage's last lap: the stage ends under it (Race Control), so it IS the break.
            # (Talladega 2026-11-06: yellow on lap 55, stage end 56.) Cars that already stopped keep their stop.
            self.stage_break_start(p, lap_now, window=0)
            if self.stage_break is not None:
                log.info("Stage %d ends under this caution (lap %d): it is the stage break",
                         self.stage_no(self.stage_break), lap_now)
                for st in self.cars.values():
                    if st.event is None and not st.in_box and not st.post_stop_lock:
                        st.decision = None
                        st.service = None
                        st.done_service = None
        self.last_flag = snap.flag

        self.drafting = snap.drafting
        if not green and race and not snap.pits_closed and snap.flag >= 1 and self.pits_open_at is None:
            self.pits_open_at = self.now                 # pit road open to the field this caution
        infos = self.field_info(snap)
        self._in_tick = False
        self.pace.worn_pct = p.worn_pct
        self.pace_info = self.compute_pace(snap, infos, p)
        teams: dict[int, list[CarSnapshot]] = {}
        for c in snap.cars:
            if c.valid and c.team >= 0 and not c.reason_out:
                teams.setdefault(c.team, []).append(c)
        self.teams = teams
        if self.drafting:
            starts = [self.cars[i].last_stop_lap for i, inf in infos.items()
                      if i in self.cars and inf.down == 0]
            self.common_start = max(set(starts), key=starts.count) if starts else None
        if green:
            for idx, info in infos.items():
                self.state(idx).green_gap = info.gap
        cars = sorted((c for c in snap.cars if c.valid), key=lambda c: infos[c.idx].rank if c.idx in infos else 99)
        rates = sorted(self.cars[c.idx].fuel_rate for c in cars
                       if c.idx in self.cars and self.cars[c.idx].fuel_rate and self.cars[c.idx].fuel_samples >= 2)
        if len(rates) >= 3:
            self.field_rate = rates[len(rates) // 2]
            caps = sorted(c.fuel_cap for c in cars if c.fuel_cap)
            self.field_tank = caps[len(caps) // 2] / self.field_rate if caps else None
        p0 = p
        grip_ref = [((c.wear_lf + c.wear_lr) / 2, (c.wear_rf + c.wear_rr) / 2, c.grip) for c in cars
                    if c.valid and c.controlled and c.grip_real and not c.reason_out]
        for car in cars:
            p = p0
            i = car.idx
            st = self.state(i)
            me = infos.get(i)
            rank, down = (me.rank, me.down) if me else (0, 0)
            if green and i in self.pending_pos and rank:
                for ev in self.pending_pos.pop(i):
                    ev.pos_out, ev.pos_pending = rank, False
                    res.resolved.append(ev)
            car_lap = car.laps_done + 1 if car.laps_done > 0 else lap_now
            lap_offset = lap_now - car_lap                       # car laps -> race laps
            rating = min(100, max(0, math.floor(car.strategy * 50 + 0.5)))
            name = car.name
            common = dict(pos=rank, lap=car_lap, laps_down=down, make=car.make, black_flag=car.black_flag, waved=car.waved, free_pass=car.free_pass, team=car.team,
                          pace_delta=self.pace_info.get(i, {}).get("delta"),
                          falloff=self.pace_info.get(i, {}).get("falloff"),
                          gap=(me.gap if green else st.green_gap) if me and not down else None)
            if car.reason_out > 0:
                if st.event is not None:                  # retired during a stop: close it off
                    st.event.note = (st.event.note + "; " if st.event.note else "") + "retired"
                    self.finish_stop(st, car, rank, res, state="retired")
                st.in_box = False
                res.rows.append(CarRow(i, car.number, name, rating, out=True, status="OUT", kind="out",
                                       stops=st.stops, last_pit=self.last_pit_text(st), **common))
                continue
            wear_l = (car.wear_lf + car.wear_lr) / 2
            wear_r = (car.wear_rf + car.wear_rr) / 2
            if (race and snap.laps_total > 0 and not self.ot_pending
                    and snap.laps_done > max(self.ot_total or 0, snap.laps_total) + 0.5 and st.event is None
                    and car.controlled):
                # the leader has taken the checkered flag: no more pit calls, nothing after it is a stop
                res.rows.append(CarRow(i, car.number, name, rating, out=False, grip=car.grip, wear_l=wear_l,
                                       wear_r=wear_r, status="RACE OVER", kind="finished", stops=st.stops,
                                       last_pit=self.last_pit_text(st), **common))
                continue
            if not car.controlled:
                st.cancel_at_green = False
                # the player's own stops are seen by the AI: teammates and the same make can follow them
                # under caution, and nearby cars can react to them under green
                on_road = bool(car.on_pit_road) or car.pitting == 1
                if race and not green:
                    # the call sticks for the caution: pitted = True; drove past pit road entry with pit road
                    # open, or one to go = False (teammates behind wait for it, see wait_for_player)
                    if on_road:
                        st.decision = True
                    elif st.decision is None and (snap.flag >= FLAG_ONE_TO_GO or (
                            self.pits_open_at is not None and not snap.pits_closed
                            and self.passed_entry(st.player_dist, car.lap_dist))):
                        st.decision = False
                else:
                    st.decision = None
                st.player_dist = car.lap_dist
                if race and green and car.on_pit_road and not st.road_prev and lap_now >= 1:
                    self.green_stops.append((i, car.number, lap_now, st.green_gap, down))
                    self.green_stops = self.green_stops[-80:]
                st.road_prev = bool(car.on_pit_road)
                res.rows.append(self.player_row(car, st, snap, p0, race, green, rank, car_lap, lap_now, lap_offset,
                                                total, to_go, wall, dt, res, grip_ref, common))
                continue

            still_in_box = (car.on_pit_road is True and car.speed is not None and car.speed < STALL_SPEED
                            and st.event is not None)

            def pit_flag(v):
                if v and car.on_pit_road is True and (st.exit_wait or still_in_box or (
                        st.event is not None and st.event.t_box_in is not None)):
                    # once it has stopped in its box the stop is the game's: it clears the request when the service
                    # ends. Writing 1 then (from a snapshot read just before that) put it straight back, AIMOD never saw
                    # the stop end and the car left its box still called in - and pitted again (#98, Bristol 11-02:
                    # repaired lap 42, back in on lap 44 for nothing, out after the green). (Before the box AIMOD may
                    # still write it: taking over a stop the game sent down pit road without a request.)
                    return
                res.writes.append(Write(i, mm.OFF_PITTING, v))

            pitting = car.pitting
            if car.on_pit_road is False and (car.speed or 0) > MOVING_SPEED:
                st.seen_moving = True
            if st.cancel_at_green:
                st.cancel_at_green = False
                if st.event is not None and st.event.state in ("requested", "in") and not car.on_pit_road:
                    # called in under caution but the green came first: no stop on the first green lap
                    pit_flag(0)
                    self.cancel_stop(st, res)
                    st.in_box, st.decision, st.service = False, None, None
                    pitting = 0
            if i in self._crossed:                           # finished a lap: log it for pace / falloff
                self.pace.lap(i, st.last_stop_lap, car.laps_done, self._crossed.pop(i), not st.lap_dirty,
                              (car.wear_lf + car.wear_lr) / 2, (car.wear_rf + car.wear_rr) / 2, self.lap_time,
                              learn=st.human_learn)
                st.lap_dirty = False
            if not green or pitting or st.event is not None or i in self.behavior_cars:
                st.lap_dirty = True
            if car.laps_done > st.seen_laps >= 0 and green and not st.in_box:
                st.green_laps += car.laps_done - st.seen_laps
            st.seen_laps = car.laps_done
            if st.damage_ref is None:
                st.damage_ref = 0
            damage = max(0.0, car.damage - st.damage_ref)
            if st.urgency is None:
                st.urgency = 0
            if st.prev_grip is None:
                st.prev_grip = car.grip

            self.learn_fuel(car, st, green)
            tank, fuel_left = self.fuel_model(car, st, car_lap, p0)
            if abs(tank - p0.tank_laps) > 0.05:
                p = dataclasses.replace(p0, tank_laps=tank)          # this car's own tank, in laps
            # Apply tank extension if configured
            extended_tank = tank * (1.0 + p0.tank_extend_pct / 100.0) if p0.tank_extend_pct > 0 else tank
            if not race:
                st.fuel_state = FUEL_PRACTICE
                fuel_left = tank
            else:
                if to_go <= fuel_left - min(p.fuel_margin, 2.0):     # makes it, with the cushion to spare
                    st.fuel_state = FUEL_MAKES_IT
                elif to_go <= extended_tank - p.fuel_margin:         # use extended tank for window calculation
                    st.fuel_state = FUEL_WINDOW
                else:
                    st.fuel_state = FUEL_SHORT

            hp = self.human_plan_for(car, st, green) if race else None
            # Green-flag planned stop
            if green and race and tank > 0 and snap.laps_done > 0:
                if hp is not None:
                    # a person plans this car: AIMOD keeps planning in the background (shown as "AIMOD would
                    # pit on lap N"), and the person's lap is put in place just before the stop is called
                    st.planned_lap, st.plan_note = st.ai_planned, st.ai_note
                why = (self.plan_still_good(st, car_lap, tank, fuel_left, to_go, p)
                       if pitting == 0 and st.decision is not True else "")
                if why:
                    st.planned_lap = None
                    st.revised = why
                if st.planned_lap is None:
                    st.plan_note = ""
                    car_total = car_lap - 1 + to_go
                    self._style_name = car.name
                    self._style_car_number = car.number
                    self._team_styles = ([self.cars[m.idx].style for m in self.teams.get(car.team, [])
                                          if m.idx != i and m.idx in self.cars
                                          and isinstance(self.cars[m.idx].planned_lap, int)]
                                         if p.team_strategy and car.team >= 0
                                         and self.rules(p).priority != "make_only" else [])
                    need = self.wear_need_value(tank, p)
                    if need is None:
                        need = self.wear_need(car, st, car_lap, tank, p)
                    stage_end = self.next_stage_end()
                    if stage_end is not None and lap_now <= stage_end and p.track_type != ROAD_COURSE:
                        st.planned_lap = self.plan_stage(st, p, rating, car_lap, lap_offset, stage_end, me, need,
                                                         tank, to_go)
                    else:
                        # no stage ahead (or a road course, where the final tank decides everything): the
                        # classic roadmap without stages, the final stint split once stages have run
                        if p.track_type in (SHORT_TRACK, ROAD_COURSE):
                            anchor = "late"          # run every tank as long as it goes; cautions decide
                        else:
                            anchor = "roadmap" if p.stages < 2 else "split"
                        st.planned_lap = self.plan_stop(st, p, rating, car_lap, to_go, car_total, tank, me, need,
                                                        anchor=anchor)
                        if isinstance(st.planned_lap, int) and st.borderline:
                            st.plan_note = f"fuel window: ~{st.borderline} laps short of one stop fewer, saving fuel"
                    if st.revised and isinstance(st.planned_lap, int):
                        st.plan_note = (st.plan_note + "; " if st.plan_note else "") + "revised: " + st.revised
                    st.revised = ""
                    if st.planned_lap == GAME_DECIDE and p.game_plan_off and fuel_left < to_go:
                        # the game no longer pits on its own plan: stop late in the fuel window
                        deadline = self.max_pit(st, tank, p.fuel_margin)
                        st.planned_lap = max(car_lap + 1, deadline - self.rng.randint(0, 3))
                        st.plan_note = "fuel window"
                if pitting == 0 and not st.in_box and st.decision is not True:
                    # the plan can still move; once the stop is called it stays as called
                    self.react_to_stops(car, st, car_lap, me, p)
                    self.tire_management(car, st, car_lap, p)
                    self.drafting_partners(car, st, car_lap, infos, snap, p)
                if p.game_plan_off and st.tire_lap != car_lap and not st.in_box and pitting == 0:
                    st.tire_lap = car_lap
                    soon = ((isinstance(st.planned_lap, int) and st.planned_lap - car_lap <= 5)
                            or (st.planned_lap == STAGE_BREAK and self.next_stage_end() is not None
                                and self.next_stage_end() - lap_now <= 8))
                    lap, why = (None, "") if soon else self.green_tire_call(car, st, car_lap, fuel_left, to_go, p)
                    if lap is not None:
                        st.planned_lap, st.plan_note = lap, why
                if hp is not None:
                    st.ai_planned, st.ai_note = st.planned_lap, st.plan_note
                    st.ai_plan_lap = st.planned_lap + lap_offset if isinstance(st.planned_lap, int) else None
                    self.apply_human_plan(car, st, hp, car_lap, lap_offset, tank, p)
                gauge = car.fuel / car.fuel_cap if car.fuel is not None and car.fuel_cap else None
                if (race and fuel_left <= 2.0 and fuel_left + 0.5 < to_go and to_go >= 1.5 and pitting == 0
                        and not st.in_box and (gauge is None or gauge < 0.25)
                        and not (isinstance(st.planned_lap, int) and st.planned_lap <= car_lap)):
                    st.planned_lap, st.plan_note = car_lap, "fuel emergency: pits now"     # safety net
                if (isinstance(st.planned_lap, int) and car_lap >= st.planned_lap and pitting == 0
                        and not st.post_stop_lock and not st.in_box):
                    st.decision = True
                    pit_flag(1)

            # Urgency from damage and grip loss
            if pitting == 0 and not st.post_stop_lock:
                if damage > p.damage_threshold or car.game_repair >= 1:
                    # (game_repair 1 or 2: the game rates the car as needing repairs. Stock NR2003 brings it in
                    # at once under any flag - green, at the yellow, under caution - and its stall repair cuts the
                    # damage back below the line (control race, Michigan 2026-10-29). AIMOD calls that stop too, so
                    # it is a damage stop whether or not the game's own repair stops are patched out.)
                    st.urgency = 100
                    if not st.forced_damage:
                        st.forced_damage = True
                        st.decision = True
                    if green and race and car.game_repair >= 1 and not st.in_box and not car.on_pit_road:
                        pit_flag(1)
                if car.grip < st.prev_grip:
                    st.urgency += (st.prev_grip - car.grip) * p.urgency_factor
            st.prev_grip = car.grip

            pk = self.pace_info.get(i, {}).get("k", 1.0)   # worn but keeping pace -> less need for tires
            chance_l = min(wear_l / p.wear_l * pk, 1.0) if p.wear_l > 0 else 0
            chance_r = min(wear_r / p.wear_r * pk, 1.0) if p.wear_r > 0 else 0
            prob_l = math.floor(chance_l * 100)
            prob_r = math.floor(chance_r * 100)

            # Stop requested by the game itself (AIMOD didn't call it)
            pre_start = race and not self.race_started   # pace laps: cars may roll off from their stalls
            came_from_track = st.track_m >= PIT_EXIT_CONFIRM_M        # a fresh entry, not the stop's own exit
            if car.on_pit_road:
                st.track_m, st.track_prev = 0.0, None
            elif car.on_pit_road is False:
                if st.track_prev is not None and self.track_len:
                    st.track_m += (car.lap_dist - st.track_prev) % self.track_len
                st.track_prev = car.lap_dist
            if (race and not pre_start and pitting == 0 and st.event is None and not st.in_box
                    and car.on_pit_road and came_from_track and lap_now >= 1):
                # NR2003 sent the car down pit road with no request (its AI commits to a stop before the request
                # can be called off, e.g. a phantom penalty RaceControl forgave): take the stop over, so AIMOD
                # still decides the service and the stop is logged with its real cause
                why = st.withdrawn_hint or st.game_hint or ("AIMOD had called it off: " + st.blocked
                                                             if st.blocked else "")
                keep_out = (not green and st.decision is False and not st.forced_damage and "penalty" not in why
                            and "repair" not in why
                            and not (car.game_penalty or car.rule_pending or self.condition_call(car)))
                if keep_out:
                    # under caution, a car AIMOD keeps out (just pitted, track position...) that the game drove
                    # onto pit road anyway: no request, no service - it drives through (or makes a stop with
                    # nothing to do) instead of taking four tires it doesn't need
                    if st.blocked != "kept out under caution":
                        log.info("#%s %s: the game sent it down pit road under caution; AIMOD keeps it out (%s): "
                                 "no service", car.number, car.name, st.note or "stays out")
                    st.blocked = "kept out under caution"
                    res.writes.extend(self.service_writes(i, car.side_flag, FUEL_ONLY, 0))
                else:
                    st.adopted = ("the game sent it in unasked" + (f" ({why})" if why else "")
                                  + "; AIMOD took the stop over")
                    log.info("#%s %s: %s", car.number, car.name, st.adopted)
                    st.decision = True
                    st.withdrawn_hint = ""
                    pit_flag(1)
                    pitting = 1
            game_request = pitting == 1 and st.decision is not True and not st.in_box and not pre_start
            blocked = False
            insists = False
            if game_request:
                hints = []
                if car.game_penalty:
                    # the game's "stop to serve" flag is set by its repair (limp) and tow modes as well as by
                    # penalty rules: a penalty only when a rule shows the car the black flag
                    hints.append("penalty to serve" if car.black_flag or car.rule_pending else
                                 "repair stop" if car.game_repair >= 1 else "the game's repair / tow stop")
                elif car.rule_pending:
                    hints.append("penalty to serve")
                if self.condition_call(car):
                    hints.append(f"car condition {car.game_condition:.2f}")
                if damage > 0.05:
                    hints.append(f"damage {damage:.2f}")
                recent = [lap for lap in st.calloffs if car_lap - lap <= 1]
                if len(recent) >= 3:
                    hints.append("keeps asking")
                if not hints and car.on_pit_road and green and race and p.guard_game_stops:
                    # asked as the car turned onto pit road: too late to call off - the stop goes ahead with
                    # AIMOD's service, and the log says why it wasn't blocked
                    hints.append("asked as it reached pit road, too late to call off")
                if hints and not st.game_hint:
                    log.info("Game wants #%s in: %s", car.number, ", ".join(hints))
                st.game_hint = ", ".join(hints)
                # the game re-asks every frame for these (it brings the car in anyway): don't fight it
                # ("keeps asking" only without the pit gate: with it, a car AIMOD calls off stays out however often
                # the game re-asks - its strategy re-plan asks again and again after a stop)
                insists = bool(car.game_penalty or car.rule_pending or self.condition_call(car)
                               or (len(recent) >= 3 and not getattr(self, "gate_on", False)))
            if (game_request and green and race and p.guard_game_stops and not st.forced_damage and not insists
                    and not car.on_pit_road and not self.late_caution_stop(st, car_lap)):
                svc = self.game_stop_service(st, p, prob_l, prob_r)
                since = car_lap - st.last_stop_lap
                reason = ""
                # Calculate fuel threshold for game stop guard
                # On aggressive green-flag stretches with good tires, use configurable threshold (5-10%)
                fuel_threshold_pct = fuel_pit_fraction(p, st.fuel_state, max(prob_l, prob_r))
                needs_fuel = fuel_left < to_go and (st.fuel_state == FUEL_WINDOW or fuel_left <= tank * fuel_threshold_pct)
                worn = (not self.drafting) and svc != FUEL_ONLY and max(prob_l, prob_r) >= 90
                losses = self.tire_losses(car, st, car_lap) if p.value_tires else None
                if losses is not None:
                    # an extra green stop for tires only pays if the time gained before the car's
                    # fuel stop (when it would get tires anyway) beats what the stop costs
                    laps_left = min(to_go, fuel_left)
                    worn = (not self.drafting) and laps_left * sum(losses) > self.tire_model.green_loss
                if st.stops > 0 and p.min_stop_gap > 0 and since < p.min_stop_gap:
                    reason = f"pitted {since} lap(s) ago"
                elif not (needs_fuel or worn):
                    reason = (f"fuel is fine ({fuel_left:.0f} laps left)" + ("" if self.drafting else
                              f", tires {max(prob_l, prob_r)}%"))
                if reason and st.req_ticks < CANCEL_TICKS:
                    st.req_ticks += 1
                    if st.req_ticks == 1:
                        st.blocked_count += 1
                        st.calloffs = (st.calloffs + [car_lap])[-6:]
                    st.blocked = reason
                    blocked = True
                    pit_flag(0)
                else:
                    st.service = svc
                    why = (f"pitted {since} lap(s) ago" if reason else
                           "needs fuel" if needs_fuel else "tires worn enough to pay for the stop")
                    ok = ("AIMOD let it through: " + why) if not reason else "the game kept asking: AIMOD let it go"
                    if ok not in st.game_hint:
                        if not st.game_hint:
                            log.info("Game wants #%s in: %s", car.number, ok)
                        st.game_hint = (st.game_hint + "; " if st.game_hint else "") + ok
            if (game_request and not green and race and st.decision is False and not insists
                    and not st.forced_damage and not car.on_pit_road):
                # under caution the game asks for cars on its own (every car, at the first chance). AIMOD makes the
                # caution call itself: a car it keeps out stays out, one it hasn't called yet waits for its call -
                # before, any request made the car pit ("shows stays out, pits anyway")
                if st.blocked != "kept out under caution" and st.decision is False:
                    log.info("#%s %s: the game wants it in under caution; AIMOD keeps it out (%s)",
                             car.number, car.name, st.note or "stays out")
                st.blocked = "kept out under caution"
                blocked = True
                pit_flag(0)
            if pitting == 0:
                st.req_ticks = 0
            if game_request and not blocked and not (not green and race and self.race_started and st.decision is None
                                                     and not insists and not st.forced_damage):
                st.decision = True
                st.game_accepted = True

            if green and pitting == 1 and not st.in_box and not blocked and not game_request:
                st.service = None
            if st.service is None and race and p.value_tires:
                laps_next = min(to_go, tank) if tank > 0 else to_go
                vs = self.value_service(car, st, car_lap, laps_next, not green, p, rating)
                if vs is not None:
                    st.service = vs[0]
            if st.service is None:
                c_l = self.rng.random() <= chance_l
                c_r = prob_r >= self.rng.randint(20, 100)
                if c_l and c_r:
                    svc = FOUR_TIRES
                elif c_r:
                    svc = FOUR_TIRES if prob_l >= 70 else RIGHT_SIDE
                elif c_l:
                    svc = LEFT_SIDE if prob_l >= 70 else FUEL_ONLY
                else:
                    svc = FUEL_ONLY
                st.service = self.limit_service(svc, st, p)
            if (green and race and pitting == 1 and not st.in_box and not blocked and st.service != FOUR_TIRES
                    and to_go > 10 and p.track_type == INTERMEDIATE and self.wear_level(p) != "low"):
                # a green-flag stop at a wearing intermediate: always four tires (the stop costs about the same)
                st.service = self.limit_service(FOUR_TIRES, st, p)
            if (green and st.human_plan is not None and st.plan_service is not None and pitting == 1
                    and not st.in_box and not blocked):
                st.service = st.plan_service
            human_svc = (not green and st.human_caution == self.cautions and st.human_service is not None
                         and st.human_key is not None)
            if human_svc and pitting == 1 and not st.in_box:
                st.service = st.human_service
            elif not green and race and pitting == 1 and not st.in_box and not blocked and not st.forced_damage:
                svc, why = self.caution_service(car, st, me, to_go, p, st.service, tank)
                st.service = svc
                if why:
                    st.note = (st.note + "; " if st.note else "") + why

            if st.exit_wait and car.on_pit_road is not True:
                st.exit_wait = False                       # left pit road after its stop
            if (p.repair_on_stops and car.controlled and car.on_pit_road is True and not st.exit_wait
                    and (st.event is None or st.event.t_box_in is None)
                    and (car.damage >= REPAIR_MIN or car.game_condition >= REPAIR_MIN)):
                # on pit road, not in its box yet: tell the crew to fix the damage. Under caution the game clears
                # this flag at pit entry, so caution stops never repaired and the game kept calling the car back in
                res.writes.append(Write(i, mm.OFF_PLAN_REPAIR, 1))      # (the planner copies the flag from here)
                res.writes.append(Write(i, mm.OFF_REPAIR_FLAG, 1))
                if st.event is not None and not st.event.damage:
                    st.event.damage = True
                    log.info("#%s %s: crew fixes the damage this stop (damage %.2f, condition %.2f)",
                             car.number, car.name, car.damage, car.game_condition)
            # serviced and still on pit road (held at the exit until the field is lined up): a pit request now is
            # not a new stop - it can't go back to its stall; if the request is still out once it's on track, it is
            held_at_exit = car.on_pit_road is True and not st.in_box and (
                st.exit_wait or (st.event is not None and st.event.t_box_out is not None))
            if pitting == 1 and not blocked and not pre_start and not held_at_exit:
                if not st.in_box:
                    if st.event is not None:               # previous stop still on pit road: close it
                        self.finish_stop(st, car, rank, res)
                    st.undo = (st.last_stop_lap, st.left_pairs_used, st.right_pairs_used, st.left_since,
                               st.right_since, st.green_laps)
                    st.last_stop_lap = car_lap
                    svc = self.limit_service(st.service, st, p)
                    st.service = svc
                    st.fuel = self.needs_fuel(fuel_left, to_go, svc, p) if race else True
                    st.fuel_level, st.fuel_note = (self.fuel_level(car, to_go, p, st, svc, not green)
                                                   if race and st.fuel else (5, ""))
                    if green and st.human_plan is not None and st.plan_fuel is not None:
                        st.fuel, st.fuel_level = True, max(1, min(5, int(st.plan_fuel)))
                        st.fuel_note = "fuel: the crew chief's plan"
                    if (not green and st.human_caution == self.cautions and st.human_fuel is not None
                            and st.human_key is not None):
                        st.fuel, st.fuel_level = True, max(1, min(5, int(st.human_fuel)))
                        st.fuel_note = "fuel: the crew chief's call"
                    if svc in (FOUR_TIRES, LEFT_SIDE):
                        st.left_pairs_used += 1
                        st.left_since = car_lap
                    if svc in (FOUR_TIRES, RIGHT_SIDE):
                        st.right_pairs_used += 1
                        st.right_since = car_lap
                    st.stops += 1
                    st.blocked = ""
                    if car.black_flag:
                        reason = "BLACK FLAG"
                    elif st.forced_damage:
                        reason = "DAMAGE"
                    elif not green:
                        reason = "CAUTION"
                    elif isinstance(st.planned_lap, int) and car_lap >= st.planned_lap:
                        reason = "PLANNED"
                    else:
                        reason = "GAME"
                    if reason == "PLANNED" and st.flip_stage is not None:
                        st.flipped = st.flip_stage
                    if reason == "PLANNED":
                        note = st.plan_note or (f"{st.style} stint" if st.style else "")
                    elif reason == "GAME" and st.adopted:
                        note = st.adopted
                    elif reason == "GAME":
                        note = ("game asked for the stop" + (f" ({st.game_hint})" if st.game_hint else "")
                                + ("; called under caution, came in after the restart"
                                   if self.late_caution_stop(st, car_lap) else ""))
                    elif reason == "CAUTION" and st.adopted:
                        note = st.adopted
                    elif green and reason == "CAUTION":
                        note = st.caution_call[1]
                    else:
                        note = st.note
                    st.caution_call = None
                    st.adopted = ""
                    st.event = PitEvent(wall, i, car.number, name, lap_now, svc, reason, green, st.stops,
                                        pos_in=(self.run_rank.get(i, rank) if green else rank),
                                        fuel=st.fuel, note=note, t_game=self.now,
                                        damage=reason == "DAMAGE" or st.forced_damage, dmg_in=car.damage,
                                        make=car.make, caution_no=self.cautions)
                    self.board.append(st.event)
                    st.event.fuel_level, st.event.fuel_in = (st.fuel_level if st.fuel else 0), car.fuel
                    hr = self.human_adjust_for(car, st) if race and self.crew_chief is not None else None
                    if hr is not None and reason == "DAMAGE":
                        # (kept for the next real stop: a damage stop is repairs, not chassis work)
                        st.human_adjust_id, st.human_adjust_status = hr.adjust_id, "waiting: damage stop"
                    if race and self.crew_chief is not None and reason != "DAMAGE":
                        falls = [v.get("falloff") for v in self.pace_info.values() if v.get("falloff")]
                        if hr is not None:                    # the phone's changes, instead of AIMOD's
                            st.human_adjust_used.add(hr.adjust_id)
                            adj = self.crew_chief.human(
                                car, snap.cars, hr, to_go, tank, falloff=self.pace_info.get(i, {}).get("falloff"),
                                field_falloff=statistics.median(falls) if len(falls) >= 5 else None,
                                tires=svc != FUEL_ONLY)
                            st.human_adjust_id, st.human_adjust_status = hr.adjust_id, adjust_status(
                                adj, hr, svc != FUEL_ONLY)
                        else:
                            adj = self.crew_chief.call(
                                car, snap.cars, to_go, tank, falloff=self.pace_info.get(i, {}).get("falloff"),
                                field_falloff=statistics.median(falls) if len(falls) >= 5 else None,
                                crew=self.crew_rating.get(car.number), tires=svc != FUEL_ONLY, green=green)
                        if adj is not None:
                            st.event.adjust = adj
                            st.event.note = (st.event.note + "; " if st.event.note else "") + adj.short
                    if st.fuel_note:
                        st.event.note = (st.event.note + "; " if st.event.note else "") + st.fuel_note
                    st.green_laps = 0
                    if green and (reason == "PLANNED" or (reason == "GAME" and not st.game_hint)):
                        # (a penalty, damage or empty-tank stop is not a strategy move: nobody reacts to it)
                        self.green_stops.append((i, car.number, lap_now, st.green_gap, down))
                        self.green_stops = self.green_stops[-80:]
                    st.prev_dist = car.lap_dist
                    st.prev_still = False
                    st.event.timer0 = car.pit_timer
                    st.event.state = self.approach_state(car.lap_dist)
                    res.events.append(st.event)
                elif st.event is not None:
                    self.track_stop(st, car, dt, pitting, rank, res)
                st.in_box = True
                st.urgency = 0
                st.done_service = st.service
                if race:
                    near = self.break_near(p)
                    st.stage_serviced = (self.stage_break if self.stage_break is not None
                                         else near if near is not None and st.pre_break == near else None)
                res.writes.extend(self.service_writes(i, car.side_flag, st.service,
                                                      st.fuel_level if st.fuel else 0))

            ev = st.event
            if (car.controlled and ev is not None and ev.t_box_in is not None and ev.t_box_out is None
                    and car.on_pit_road and car.speed is not None and car.speed < STALL_SPEED
                    and self.now - ev.t_box_in > STUCK_S
                    and (st.released_at is None or self.now - st.released_at > 30.0)):
                # parked in its stall with no service going on (seen: a game-requested stop that never ended,
                # car sitting still for 40 laps, with AIMOD and RaceControl closed): clear the pit request,
                # the service requests and the penalty-stop flag so the game lets it go
                st.released_at = self.now
                res.writes.extend([Write(i, mm.OFF_PITTING, 0), Write(i, mm.OFF_GAME_PENALTY_STOP, 0)]
                                  + self.service_writes(i, car.side_flag, FUEL_ONLY, 0))
                log.warning("#%s %s: parked in the pit stall for %.0f s with no service - released (pit request, "
                            "service and penalty flags cleared)", car.number, car.name, self.now - ev.t_box_in)
                ev.note = (ev.note + "; " if ev.note else "") + "stuck in the stall, released by AIMOD"

            if st.in_box and pitting == 0:
                # request withdrawn before the car reached pit road (Race Control calling off a phantom
                # penalty stop, pits closed, ...): not a stop - no lock-out, plan again
                withdrawn = (st.event is not None and st.event.t_pit_in is None and car.on_pit_road is False)
                if withdrawn and st.game_accepted:
                    log.info("#%s: the game's stop was withdrawn before pit road (%s)", car.number,
                             st.game_hint or "no reason given")
                    st.withdrawn_hint = st.game_hint or "the game's request"
                if withdrawn:
                    st.game_hint = ""
                st.game_accepted = False
                st.in_box = False
                st.decision = None
                st.service = None
                st.forced_damage = False
                st.planned_lap = None
                st.plan_note = ""
                st.post_stop_lock = not withdrawn
                if st.event is not None:
                    self.track_stop(st, car, dt, pitting, rank, res)

            elif st.event is not None and not st.in_box:
                self.track_stop(st, car, dt, pitting, rank, res)
            elif st.event is None and race and pitting == 0 and lap_now >= 1 and to_go > 0:
                self.unrequested_stop(car, st, green, car_lap, lap_now, rank, wall, res)

            if pitting == 1 and st.post_stop_lock and car.on_pit_road is not True:
                st.post_stop_lock = False        # (a request seen while still on pit road after the stop isn't a new call)
            if st.urgency > 100:
                st.urgency = 100

            kind = "green"
            if (not green and st.post_stop_lock and race and pitting == 0 and 0 < snap.flag < FLAG_ONE_TO_GO
                    and not car.waved and not car.free_pass
                    and not (p.lapped_wave and self.wave_candidate(me, infos, snap, car.idx))):
                # (a car that just finished a green flag stop can still be waved around: no extra splash then)
                self.second_topoff(st, me, car_lap, rating, room=1.0 - fuel_left / tank if tank > 0 else 0.0,
                                   short=fuel_left - p.fuel_margin < self.laps_to_next_chance(to_go))
            if not green:
                if st.post_stop_lock:
                    pit_flag(0)
                    status = "FINISHED: " + service_code(st.done_service or FUEL_ONLY, st.fuel)
                    kind = "finished"
                elif not self.race_started and race and not st.in_box:
                    # pace laps before the start run under a caution flag: no pit calls (a car starting from
                    # its pit stall is left alone - the game rolls it off pit road)
                    if not car.on_pit_road:
                        pit_flag(0)
                    st.decision = None
                    status = "PACE LAPS"
                    kind = "stayout"
                elif (snap.pits_closed or (snap.flag == 0 and wall - self.caution_wall < CAUTION_SETTLE)) \
                        and not car.on_pit_road and not st.in_box:
                    # Race Control holds pit road closed for the first caution lap: no call until it opens,
                    # so the decision is made with the field lined up
                    pit_flag(0)
                    st.decision = None
                    status = "PITS CLOSED"
                    kind = "stayout"
                elif race and car.going_around and pitting == 0 and st.event is None and not car.on_pit_road:
                    # Race Control is sending it around the pace car: pitting before it is back across the
                    # line behind it costs it a lap - the call waits
                    pit_flag(0)
                    status = "GOING AROUND: pits once it is back in line"
                    kind = "stayout"
                elif (race and snap.flag >= FLAG_ONE_TO_GO and st.event is None and pitting == 0
                      and not car.on_pit_road and not car.pit_mode and self.pits_open_at is not None
                      and self.now - self.pits_open_at > (self.lap_time or DEFAULT_LAP_TIME)):
                    # (only once pit road has been open a lap: if Race Control opened it late, cars still go)
                    # one to go: too late for a new call, it would only reach pit road under green
                    pit_flag(0)
                    st.decision = False
                    status = "ONE TO GO: pit road closing"
                    kind = "stayout"
                elif (race and st.decision is None and st.event is None and pitting == 0 and not car.on_pit_road
                      and (who := self.wait_for_human(car, st, wall)) is not None):
                    # a crew chief on a phone owns this car: the caution call is theirs, for a few seconds
                    pit_flag(0)
                    status = f"WAITS FOR CREW CHIEF ({who})"
                    kind = "stayout"
                elif (race and st.decision is None and st.event is None and pitting == 0 and not car.on_pit_road
                      and (wait := self.wait_for_player(car, st, me, infos, p)) is not None):
                    # its teammate (the player) is ahead on track: the call waits until he pits or drives by
                    pit_flag(0)
                    status = f"WAITS FOR #{wait}: teammate's call"
                    kind = "stayout"
                else:
                    if st.decision is None:
                        self.caution_decision(car, st, me, infos, snap, p, race, to_go, prob_l, prob_r,
                                              fuel_left)
                    if (race and i in self.human_owner and not car.on_pit_road
                            and (st.event is None or st.event.t_pit_in is None)):
                        # (also while AIMOD's own request is out but the car isn't on pit road yet: a stay-out
                        # from the phone calls it off - the request is withdrawn before pit road)
                        self.apply_human_call(car, st, snap, fuel_left, to_go)
                    if st.decision is False:
                        pit_flag(0)
                        st.caution_call = None
                        status = "STAY OUT"
                        kind = "stayout"
                    else:
                        pit_flag(1)
                        if race and pitting == 0 and st.event is None:
                            st.caution_call = (self.cautions, (st.note + "; " if st.note else "")
                                               + "pitted after the restart")
                        prefix = "D+" if st.forced_damage else ""
                        verb = "PITTING: " if pitting == 1 else "GOING PIT: "
                        if st.service and pitting == 0 and st.event is None:
                            st.fuel = self.needs_fuel(fuel_left, to_go, st.service, p) if race else True
                        status = verb + prefix + (service_code(st.service, st.fuel) if st.service
                                                  else "CALCULATING...")
                        kind = "pitting"
                    if st.note:
                        status += f" ({st.note})"
            elif st.post_stop_lock:
                status = "OUT: " + service_code(st.done_service or FUEL_ONLY, st.fuel)
                kind = "finished"
                if pitting == 0:
                    st.post_stop_lock = False
            elif blocked:
                status = f"GAME STOP CALLED OFF: {st.blocked}"
                kind = "stayout"
            elif pitting == 1:
                status = "PITTING (GREEN): " + service_code(st.service or FUEL_ONLY, st.fuel)
                kind = "pitting"
            else:
                st.in_box = False
                status = "GREEN"
                if st.plan_note:
                    status += f"  ({st.plan_note})"
                elif st.blocked:
                    status += f"  (last game stop called off: {st.blocked})"
            if st.event is not None and st.event.state in ("requested", "in") and pitting == 1:
                status = ("PITTING THIS TIME BY: " if st.event.state == "in" else "PIT REQUESTED: ") +                     st.event.code + (f" ({st.note})" if st.note and not green else "")
            if kind == "green" and st.forced_damage:
                kind = "damage"

            if (race and not green and car.controlled and st.decision is False and st.event is None
                    and not car.on_pit_road and not car.pit_mode and not st.forced_damage and not car.game_penalty
                    and not car.rule_pending and not self.condition_call(car)):
                res.keep_out.add(i)
            next_pit = self.next_pit_text(st, p, race, snap, car_lap, lap_offset, lap_now, total, to_go,
                                          wear_l, wear_r, tank)
            if green:
                st.caution_next = next_pit
            elif st.caution_next and not st.post_stop_lock and st.event is None and pitting == 0:
                next_pit = st.caution_next                # plan from before the caution, until it pits
            else:
                st.caution_next = ""
            res.rows.append(CarRow(
                i, car.number, name, rating, out=False, grip=car.grip, wear_l=wear_l, wear_r=wear_r,
                prob_l=prob_l, prob_r=prob_r, damage=damage, urgency=st.urgency,
                fuel_state=st.fuel_state or "ALERT", fuel_laps=fuel_left if race else None,
                green_laps=st.green_laps, fuel_margin=(fuel_left - to_go) if race and to_go > 0 else None,
                fuel_frac=(car.fuel / car.fuel_cap if car.fuel is not None and car.fuel_cap
                           else fuel_left / tank if race and tank > 0 else None),
                fuel_real=car.fuel is not None and bool(car.fuel_cap), car_tank=tank if tank > 0 else None,
                own_mileage=st.fuel_samples >= 2,
                tires_left=self.tires_left_text(st, p), stops=st.stops, last_pit=self.last_pit_text(st),
                next_pit=next_pit, status=status, kind=kind, **common))
        return res

    def player_row(self, car: CarSnapshot, st: CarState, snap: RaceSnapshot, p: StrategyParams, race: bool,
                   green: bool, rank: int, car_lap: int, lap_now: int, lap_offset: int, total: int, to_go: float,
                   wall: float, dt: float, res: TickResult, grip_ref: list, common: dict) -> CarRow:
        """The player's car gets the same readings as an AI car (fuel and mileage, wear, grip, pace and falloff,
        its stops on the pit board, in the pit log and as last pit), all read-only: AIMOD never writes to it
        and doesn't plan its stops."""
        i = car.idx
        wear_l = (car.wear_lf + car.wear_lr) / 2
        wear_r = (car.wear_rf + car.wear_rr) / 2
        if wear_l > 0.001 or wear_r > 0.001:
            st.wear_seen = True
        wear_known = st.wear_seen or st.green_laps < 3
        if car.on_pit_road is False and (car.speed or 0) > MOVING_SPEED:
            st.seen_moving = True
        if i in self._crossed:
            # (learn=False: the player's laps give its pace and falloff but don't teach AIMOD's tire model)
            self.pace.lap(i, st.last_stop_lap, car.laps_done, self._crossed.pop(i), not st.lap_dirty,
                          wear_l, wear_r, self.lap_time, learn=False)
            st.lap_dirty = False
        if not green or car.on_pit_road or st.event is not None:
            st.lap_dirty = True
        if car.laps_done > st.seen_laps >= 0 and green and st.event is None:
            st.green_laps += car.laps_done - st.seen_laps
        st.seen_laps = car.laps_done
        if st.damage_ref is None:
            st.damage_ref = 0
        damage = max(0.0, car.damage - st.damage_ref)

        self.learn_fuel(car, st, green)
        tank, fuel_left = self.fuel_model(car, st, car_lap, p)
        if not race:
            st.fuel_state = FUEL_PRACTICE
            fuel_left = tank
        elif to_go <= fuel_left - min(p.fuel_margin, 2.0):
            st.fuel_state = FUEL_MAKES_IT
        elif to_go <= tank - p.fuel_margin:
            st.fuel_state = FUEL_WINDOW
        else:
            st.fuel_state = FUEL_SHORT

        # its stops, seen the way AIMOD sees an AI car the game brings in unasked: pit road, the game's pit timer
        # and the car stopping in its stall (a drive-through is not a stop); the service is read from the tires
        decision = st.decision                   # (the caution call teammates follow, kept as the loop set it)
        if st.event is not None:
            self.track_stop(st, car, dt, 0, rank, res)
        elif race and lap_now >= 1 and to_go > 0:
            self.unrequested_stop(car, st, green, car_lap, lap_now, rank, wall, res)
            if st.event is not None:
                st.event.reason = "PLAYER" if green else "CAUTION"
                st.event.note = "your stop" + ("" if wear_known else "; tires not readable for your car")
        st.decision = decision

        pk = self.pace_info.get(i, {}).get("k", 1.0)
        prob_l = math.floor(min(wear_l / p.wear_l * pk, 1.0) * 100) if p.wear_l > 0 else 0
        prob_r = math.floor(min(wear_r / p.wear_r * pk, 1.0) * 100) if p.wear_r > 0 else 0
        grip, grip_est = car.grip, False
        if not car.grip_real:
            grip, grip_est = (self.grip_like(wear_l, wear_r, grip_ref) if wear_known else None), True
            if grip is None:
                grip, grip_est = 0.0, False
        next_pit = "-"
        if race and tank > 0 and snap.laps_done > 0:
            by_lap = math.floor(self.fuel_from(st) + tank - p.fuel_margin) + lap_offset
            next_pit = ("NO STOP NEEDED" if by_lap >= total else "FUEL DUE" if by_lap <= lap_now
                        else f"FUEL TO LAP {by_lap}")
        status = "YOUR CAR (not controlled)"
        if st.event is not None:
            status = "YOUR CAR: " + {"stall": "IN THE BOX", "exit": "LEAVING THE BOX"}.get(st.event.state, "PIT ROAD")
        return CarRow(
            i, car.number, car.name, min(100, max(0, math.floor(car.strategy * 50 + 0.5))), out=False, grip=grip,
            grip_est=grip_est,
            wear_known=wear_known, wear_l=wear_l, wear_r=wear_r, prob_l=prob_l, prob_r=prob_r, damage=damage,
            fuel_state=st.fuel_state, fuel_laps=fuel_left if race else None, green_laps=st.green_laps,
            fuel_margin=(fuel_left - to_go) if race and to_go > 0 else None,
            fuel_frac=(car.fuel / car.fuel_cap if car.fuel is not None and car.fuel_cap
                       else fuel_left / tank if race and tank > 0 else None),
            fuel_real=car.fuel is not None and bool(car.fuel_cap), car_tank=tank if tank > 0 else None,
            own_mileage=st.fuel_samples >= 2, tires_left=self.tires_left_text(st, p), stops=st.stops,
            last_pit=self.last_pit_text(st), next_pit=next_pit, status=status, kind="player", **common)

    @staticmethod
    def grip_like(wear_l: float, wear_r: float, ref: list, k: int = 5) -> float | None:
        """Grip for a car the game keeps no grip figure for (the player's): the median grip of the AI cars
        whose tires are worn most like it."""
        if len(ref) < 3:
            return None
        near = sorted(ref, key=lambda r: (r[0] - wear_l) ** 2 + (r[1] - wear_r) ** 2)[:k]
        return statistics.median(r[2] for r in near)

    # ------------------------------------------------------------- stop phases
    def track_stop(self, st: CarState, car: CarSnapshot, dt: float, pitting: int, rank: int, res: TickResult):
        """Pit entry / box entry / box exit / pit exit from NR2003's own signals: the per-car
        pit road flag (entry and exit), the game's pit timer (counts while the car is serviced),
        the pit request (cleared when the car leaves its box) and the car's speed as a backup."""
        ev = st.event
        on_road = car.on_pit_road
        still = car.speed is not None and car.speed < STALL_SPEED
        if car.fuel is not None:                 # fuel in the box vs fuel when the service ends: fuel flow
            if ev.t_box_in is not None and ev.fuel_box_in is None:
                ev.fuel_box_in, ev.fuel_cap = car.fuel, car.fuel_cap
            if ev.t_box_out is not None and ev.fuel_box_out is None:
                ev.fuel_box_out = car.fuel
        if on_road is None:                       # flag not available: old behaviour
            if still and st.prev_still:
                ev.box_time += dt
                ev.t_box_in = ev.t_box_in or self.now
            st.prev_still = still
            if pitting == 0:
                self.finish_stop(st, car, rank, res)
            elif ev.t_box_in is not None:
                ev.state = "stall" if still else "exit"
            return
        if on_road:
            st.off_road_at = None                 # (back on pit road: a flag gap, not the exit)
            if ev.t_pit_in is None:
                ev.t_pit_in = self.now
                self.learn_pit_entry(car.lap_dist)
                # the stop's lap and time are when it reached pit road, not when it was called (under caution
                # the call comes a lap or more before the car gets to pit road)
                ev.lap = self.car_lap_of(car)
                ev.time = self.clock()
            timer_running = (car.pit_timer is not None and ev.timer0 is not None and ev.t_box_out is None
                             and (ev.t_box_in is not None or car.pit_timer < ev.timer0 - 0.05
                                  or (ev.timer0 == 0 and car.pit_timer > 0.05)))
            if ev.t_box_out is None and (timer_running or (still and st.prev_still)):
                ev.t_box_in = ev.t_box_in or self.now
                if car.pit_timer is not None and timer_running:
                    ev.box_time = car.pit_timer
                elif still and st.prev_still:
                    ev.box_time += dt
            if ev.t_box_out is None and ((pitting == 0 and not ev.unrequested)
                                         or (ev.t_box_in is not None and (car.speed or 0) > 3)):
                ev.t_box_out = self.now               # service over (request cleared / pulled away)
            ev.state = "exit" if ev.t_box_out else "stall" if ev.t_box_in else "road"
            ev.road_time = self.now - ev.t_pit_in
            if (ev.t_box_out is not None and self.now - ev.t_box_out > PIT_EXIT_TIMEOUT
                    and not (still and self.now - ev.t_box_out < PIT_HOLD_MAX)):
                ev.t_pit_out = self.now               # pit exit never seen: don't leave the stop open
                self.finish_stop(st, car, rank, res)
                return
        else:
            if ev.t_pit_in is not None:           # back out on the track: pit exit, once it's clearly gone
                if st.off_road_at is None:
                    st.off_road_at = (self.now, car.lap_dist)
                tl = self.track_len or 0.0
                went = car.lap_dist - st.off_road_at[1]
                if tl > 0:
                    went %= tl
                if went < min(PIT_EXIT_CONFIRM_M, 0.4 * tl if tl > 0 else PIT_EXIT_CONFIRM_M) and \
                        self.now - st.off_road_at[0] < PIT_EXIT_TIMEOUT:
                    st.prev_still = still
                    return                        # maybe just a gap in the pit road flag: wait
                ev.t_pit_out = st.off_road_at[0]
                ev.road_time = ev.t_pit_out - ev.t_pit_in
                st.off_road_at = None
                self.finish_stop(st, car, rank, res)
            elif pitting == 0:                    # request withdrawn before it reached pit road
                self.cancel_stop(st, res)
            else:
                ev.state = self.approach_state(car.lap_dist)
        st.prev_still = still

    def car_lap_of(self, car: CarSnapshot) -> int:
        """The lap the car itself is on (its own lap counter: what the game shows for it)."""
        return car.laps_done + 1 if car.laps_done > 0 else self.lap_now

    def late_caution_stop(self, st: CarState, car_lap: int) -> bool:
        """A green-flag stop within 3 laps of the restart by a car that was called in last caution
        but never got to pit road (pits held closed, or the restart came first)."""
        return (st.caution_call is not None and st.caution_call[0] == self.cautions
                and self.green_lap is not None and self.lap_now - self.green_lap <= 3)

    def unrequested_stop(self, car: CarSnapshot, st: CarState, green: bool, car_lap: int, lap_now: int,
                         rank: int, wall: float, res: TickResult):
        """NR2003 can still bring a car in after its request was called off (by the green-flag guard,
        or Race Control holding the pits closed): the car stops in its stall with no request. Log it
        anyway; the service is read from which tires came back new."""
        timer_on = (car.pit_timer is not None and st.prev_timer is not None and car.pit_timer > 0.05
                    and abs(car.pit_timer - st.prev_timer) > 0.01)
        still = car.speed is not None and car.speed < STALL_SPEED
        st.prev_timer = car.pit_timer
        if st.exit_wait or not car.on_pit_road or not (still or timer_on) or not st.seen_moving:
            st.road_still = 0
            return
        st.road_still += 1
        if st.road_still < 3:
            return
        st.road_still = 0
        if green and self.late_caution_stop(st, car_lap):
            reason, note = "GAME", "called under caution, came in after the restart"
        elif not green:
            reason, note = "CAUTION", "came in without a request"
        else:
            reason, note = "GAME", ("game brought it in" + (f" ({st.game_hint})" if st.game_hint else "")
                                    + (f"; AIMOD had called it off: {st.blocked}" if st.blocked else ""))
        st.caution_call = None
        st.post_stop_lock = False
        st.planned_lap, st.plan_note, st.style, st.window = None, "", "", None   # this was the stop: plan anew
        st.decision = None
        st.undo = (st.last_stop_lap, st.left_pairs_used, st.right_pairs_used, st.left_since,
                   st.right_since, st.green_laps)
        st.last_stop_lap = car_lap
        st.stops += 1
        st.blocked = ""
        st.green_laps = 0
        st.event = PitEvent(wall, car.idx, car.number, car.name, car_lap, FUEL_ONLY, reason, green, st.stops,
                            pos_in=(self.run_rank.get(car.idx, rank) if green else rank), fuel=True, note=note,
                            t_game=self.now, dmg_in=car.damage,
                            make=car.make, caution_no=self.cautions, unrequested=True,
                            wear_in=((car.wear_lf + car.wear_lr) / 2, (car.wear_rf + car.wear_rr) / 2))
        ev = st.event
        ev.t_pit_in = ev.t_box_in = self.now
        ev.timer0 = 0.0
        ev.state = "stall"
        st.prev_still = still
        self.board.append(ev)
        # (a stop the game forced - penalty, damage, empty tank - is not a strategy move: nobody reacts to it)
        res.events.append(ev)

    def finish_stop(self, st: CarState, car: CarSnapshot, rank: int, res: TickResult, state: str = "out"):
        ev = st.event
        st.game_hint, st.calloffs, st.game_accepted = "", [], False
        if state == "out":
            self.learn_fuel_add(ev, car)
        if ev.unrequested and ev.wear_in is not None:
            wl, wr = (car.wear_lf + car.wear_lr) / 2, (car.wear_rf + car.wear_rr) / 2
            new_l, new_r = wl < ev.wear_in[0] * 0.5, wr < ev.wear_in[1] * 0.5
            ev.service = (FOUR_TIRES if new_l and new_r else LEFT_SIDE if new_l else RIGHT_SIDE if new_r
                          else FUEL_ONLY)
            st.done_service, st.post_stop_lock = ev.service, True     # serviced: not sent back in this caution
            if new_l:
                st.left_pairs_used += 1
                st.left_since = st.last_stop_lap
            if new_r:
                st.right_pairs_used += 1
                st.right_since = st.last_stop_lap
        if self.tire_model is not None and state == "out":
            self.tire_model.add_stop(ev.code, ev.box_time, ev.road_time, ev.green, self.lap_time)
        ev.pos_out = None
        if state == "out":
            if ev.green:
                ev.pos_out = self.run_rank.get(car.idx, rank)     # where it rejoins on track
            else:
                # under caution the order at pit exit is half way through the pit cycle: the real result is
                # the lineup, read at the green
                ev.pos_pending = True
                self.pending_pos.setdefault(car.idx, []).append(ev)
        ev.duration = self.now - ev.t_game
        ev.state, ev.t_exit = state, self.now
        st.exit_wait = car.on_pit_road is True
        ev.dmg_out = car.damage
        res.exits.append(ev)
        st.last_event, st.event = ev, None

    def cancel_stop(self, st: CarState, res: TickResult):
        ev = st.event
        st.event = None
        if st.undo is not None:
            (st.last_stop_lap, st.left_pairs_used, st.right_pairs_used, st.left_since, st.right_since,
             st.green_laps) = st.undo
        st.stops = max(0, st.stops - 1)
        if ev in self.board:
            self.board.remove(ev)
        res.cancels.append(ev)

    # ------------------------------------------------------------- pit entry
    def passed_entry(self, prev: float | None, now: float) -> bool:
        """The car went past pit road entry between two ticks (and is not on pit road)."""
        if prev is None or self.pit_entry is None or self.track_len <= 0:
            return False
        moved = (now - prev) % self.track_len
        if moved <= 0 or moved > 0.5 * self.track_len:
            return False
        line = (self.pit_entry + 30.0) % self.track_len           # a little past it: the pit road flag lags
        return 0 < (line - prev) % self.track_len <= moved

    def human_adjust_for(self, car, st: CarState):
        """The phone's chassis changes for this car's stop, or None (AIMOD's crew chief decides). Each request
        is made at one stop only."""
        hr = self.human_adjusts.get(car.idx)
        if (hr is None or car.idx not in self.human_owner or not car.controlled
                or getattr(hr, "adjust_id", None) in st.human_adjust_used):
            return None
        return hr

    def human_plan_for(self, car, st: CarState, green: bool):
        """The person's green-flag plan for this car, or None. A plan is used up by the next stop the car makes
        (planned, under caution, damage, penalty...): its plan_id never applies again."""
        if st.human_plan is not None and st.stops > st.human_plan_stops:
            ev = st.event
            st.human_plan_id = st.human_plan
            st.human_plan_used.add(st.human_plan)
            st.human_plan_status = "done" if (ev is None or ev.green) else "used under caution"
            st.human_plan, st.plan_service, st.plan_fuel = None, None, None
            st.planned_lap, st.plan_note, st.ai_planned, st.ai_note = None, "", None, ""   # plan the next run anew
            st.human_learn = True                         # the next run is AIMOD's again (unless re-planned)
        hp = self.human_plans.get(car.idx)
        if (hp is None or car.idx not in self.human_owner or not car.controlled
                or getattr(hp, "plan_id", None) in st.human_plan_used):
            if st.human_plan is not None:                 # the phone dropped the plan: AIMOD's own plan again
                st.human_plan, st.plan_service, st.plan_fuel = None, None, None
                st.planned_lap, st.plan_note = st.ai_planned, st.ai_note
                st.human_plan_status = ""
            return None
        if st.human_plan != hp.plan_id:
            if st.human_plan is None:
                st.ai_planned, st.ai_note = st.planned_lap, st.plan_note
            st.human_plan, st.human_plan_id, st.human_plan_stops = hp.plan_id, hp.plan_id, st.stops
            st.human_learn = False
        st.plan_service, st.plan_fuel = hp.service, hp.fuel_level
        return hp

    def apply_human_plan(self, car, st: CarState, hp, car_lap: int, lap_offset: int, tank: float,
                         p: StrategyParams):
        """Put the person's lap in place (race lap -> this car's laps). A lap already reached = box now.
        Safety: never later than the fuel lasts (moved to the last safe lap)."""
        want = int(hp.lap) - lap_offset
        lap = max(car_lap, want)
        safe = self.max_pit(st, tank, p.fuel_margin) if tank > 0 else lap
        who = getattr(hp, "who", "") or self.human_owner.get(car.idx, "crew chief")
        if lap > safe:
            lap = max(car_lap, safe)
            st.human_plan_status = f"overridden: moved to lap {lap + lap_offset}, fuel won't last"
        else:
            st.human_plan_status = f"accepted: lap {lap + lap_offset}"
        st.human_plan_id = hp.plan_id
        st.planned_lap, st.plan_note = lap, f"crew chief plan ({who})"

    def _human_reset(self, st: CarState):
        if st.human_caution != self.cautions:
            st.human_caution, st.human_status, st.human_deadline_s = self.cautions, "", None
            st.human_wait_since, st.human_key, st.human_service, st.human_fuel = None, None, None, None

    def human_call(self, idx: int):
        """This caution's call from the crew chief on the phone, or None (no call, a stale one)."""
        c = self.human_calls.get(idx)
        return c if c is not None and getattr(c, "caution_no", None) == self.cautions else None

    def wait_for_human(self, car, st: CarState, wall: float) -> str | None:
        """Caution, pits open, a human-owned car with no call yet: AIMOD waits HUMAN_WAIT_S real seconds (or
        until the car nears pit entry), then makes its own call. Returns the crew chief's name while waiting."""
        who = self.human_owner.get(car.idx)
        if who is None or not car.controlled:
            return None
        self._human_reset(st)
        if self.human_call(car.idx) is not None or st.human_status:
            st.human_deadline_s = None
            return None                                   # a call is in (or the wait already ran out)
        if st.human_wait_since is None:
            st.human_wait_since = wall
        left = HUMAN_WAIT_S - (wall - st.human_wait_since)
        near = (self.pit_entry is not None and self.track_len > 0
                and (self.pit_entry - car.lap_dist) % self.track_len < HUMAN_APPROACH_M)
        if left <= 0 or near:
            st.human_deadline_s = None
            st.human_status = "no answer, AIMOD's call"
            return None
        st.human_deadline_s = round(left, 1)
        return who

    def apply_human_call(self, car, st: CarState, snap, fuel_left: float, to_go: float):
        """The crew chief's caution call, applied over AIMOD's own (already made, so its safety reasons are
        known). Safety beats the human: heavy damage, nearly no fuel, a penalty or the game's condition stop, and no
        pit call for a car on the wave around / free pass. Each new call is applied once; AIMOD's follow and
        top-off logic don't change it afterwards."""
        self._human_reset(st)
        call = self.human_call(car.idx)
        if call is None:
            return
        key = (bool(call.pit), call.service, call.fuel_level)
        if key == st.human_key:
            return
        who = getattr(call, "who", "") or self.human_owner.get(car.idx, "crew chief")
        if snap.flag >= FLAG_ONE_TO_GO:
            st.human_key, st.human_status = key, "too late"
            return
        if not call.pit and car.pit_mode and st.decision is not False:
            st.human_key, st.human_status = key, "too late: already heading to pit road"
            return
        st.human_key = key
        st.human_service = call.service if call.pit else None
        st.human_fuel = call.fuel_level if call.pit else None
        # safety only: AIMOD's strategic "musts" (fill now to save a green stop, can't reach the finish without
        # another stop) are the human's call to make
        short = fuel_left - 1.0 < min(to_go, 4.0)          # can't run a few green laps to a later stop
        must = ("damage" if st.forced_damage else "") or (
            ("penalty" if car.black_flag else "repair") if car.game_penalty else "") or ("car condition" if self.condition_call(car) else "") or (
            "fuel" if short else "")
        if not call.pit and must:
            st.decision, st.human_status = True, f"overridden: {must}"
            st.human_service = st.human_fuel = None
            return
        if call.pit and not must and (car.waved or car.free_pass or car.going_around or st.wave_hold):
            what = "free pass" if car.free_pass else "going around" if car.going_around else "wave around"
            st.decision, st.human_status = False, f"overridden: {what}, stays out"
            st.human_service = st.human_fuel = None
            return
        st.decision = bool(call.pit)
        st.note = f"crew chief call ({who})"
        st.human_status = "accepted"

    def wait_for_player(self, car, st, me, infos, p: StrategyParams) -> int | None:
        """Caution: an AI car whose teammate is the player, running ahead of it on the same lap, holds its call
        until the player has pitted or driven past pit road (then follow_call can follow him like any lead
        teammate). It stops waiting when it gets close to pit entry itself or after a lap and a half.
        Returns the player's car number while it waits."""
        if not (p.team_strategy and p.follow_player) or car.team < 0 or me is None:
            return None
        for o in self.teams.get(car.team, []):
            if o.controlled or o.idx == car.idx:
                continue
            ost, oi = self.cars.get(o.idx), infos.get(o.idx)
            if ost is None or oi is None or ost.decision is not None or oi.down != me.down or oi.rank >= me.rank:
                continue
            if self.pit_entry is not None and self.track_len > 0 \
                    and (self.pit_entry - car.lap_dist) % self.track_len < HOLD_ROOM_M:
                return None                                   # too close to pit road to wait any longer
            if st.hold_since is None or st.hold_since[0] != self.pits_open_at:
                st.hold_since = (self.pits_open_at, self.now)
            if self.now - st.hold_since[1] > 1.5 * (self.lap_time or DEFAULT_LAP_TIME):
                return None
            return o.number
        return None

    def learn_pit_entry(self, dist: float | None):
        if dist is None or dist <= 0:
            return
        self.entry_samples.append(dist)
        self.entry_samples = self.entry_samples[-15:]
        self.pit_entry = statistics.median(self.entry_samples)

    def approach_state(self, dist: float) -> str:
        """'in' when the car is on its final approach to pit road entry this lap, otherwise
        'requested' (pit request in, but it isn't coming in this time by yet). Until pit road
        entry has been seen at this track, cars only show once they are on pit road."""
        if self.pit_entry is None or self.track_len <= 0:
            return "requested"
        ahead = (self.pit_entry - dist) % self.track_len
        return "in" if ahead <= APPROACH_LAP * self.track_len else "requested"

    # ------------------------------------------------------------- pit board
    def pit_board(self, hold: float = 12.0) -> list[PitEvent]:
        """Stops in progress plus stops whose service ended within the last `hold` game seconds
        (timer starts when the car leaves its box). Retired cars drop off after a few seconds."""

        def keep(e):
            if e.state == "retired":
                return self.now - (e.t_exit or self.now) <= min(hold, 6)
            done_at = e.t_box_out if e.t_box_out is not None else e.t_exit
            if done_at is not None:
                return self.now - done_at <= hold      # gone a set time after the service ends
            return self.now - e.t_game <= 600
        self.board = [e for e in self.board if keep(e)]
        return list(self.board)

    # --------------------------------------------------------------- display
    @staticmethod
    def last_pit_text(st: CarState) -> str:
        ev = st.event or st.last_event
        if ev is None:
            return ""
        return f"{ev.lap}: {ev.code}"

    def next_pit_text(self, st, p, race, snap, car_lap, lap_offset, lap_now, total, to_go, wear_l, wear_r, tank):
        if st.planned_lap == STAGE_BREAK and self.next_stage_end() is not None:
            e = self.next_stage_end()
            return f"STAGE {self.stage_no(e)} BREAK (lap {e})"
        if isinstance(st.planned_lap, int):
            # likely service at the planned lap, from each side's wear rate so far
            ahead = max(0, st.planned_lap - car_lap)
            proj = []
            for wear, since, thr in ((wear_l, st.left_since, p.wear_l), (wear_r, st.right_since, p.wear_r)):
                laps_on = max(1, car_lap - since)
                proj.append(100 * (wear + wear / laps_on * ahead) / thr if thr > 0 else 0)
            tires = self.limit_service(self.likely_service(*proj), st, p)
            fuel_left_then = tank - (st.planned_lap - self.fuel_from(st))
            fuel = self.needs_fuel(fuel_left_then, to_go - ahead, tires, p)
            tag = f"  {st.style}" if st.style else ("  flip" if st.flip_stage is not None
                                                     and st.flipped != st.flip_stage else "")
            return f"LAP {st.planned_lap + lap_offset}  ~{service_code(tires, fuel)}{tag}"
        if race and tank > 0 and snap.laps_done > 0:
            # Not planned by AIMOD: the game pits it, at the latest when fuel runs out.
            by_lap = math.floor(self.fuel_from(st) + tank - p.fuel_margin) + lap_offset
            if by_lap >= total:
                return "GAME (no stop)"
            if by_lap <= lap_now:
                return "GAME (due)"
            return f"GAME (by {by_lap})"
        return GAME_DECIDE
