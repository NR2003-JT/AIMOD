"""Driver behavior: per car, when to save fuel, save or cool its tires, or push.

Each AI car runs a PLAN, set at the start of each run (the green at the start of the race, and after every stop),
and changes on the fly only in real situations:

  plan (whole run)
    tire manager    long runs: saves tires through the middle of the run, pushes the last few laps on what it
                    saved; who manages comes from the strategy rating, temperament and the run's length
    runs it out     no saving: normal pace all run
    fuel            a run that makes the finish / the stage break it waits for only by saving: saves fuel all run
    drafting        the pack rides and saves fuel mid-run (shorter stop), pushes at the stage end; the cars of one
                    team (else make) in a pack follow their lead car; never saves tires
  on the fly
    battling for the lead   top 3 within a second of each other: they push
    staying on the lead lap the leader closing on a lead-lap car: it pushes to hold him off
    late in the race        everyone with the fuel pushes (last N %)
    tires going away        a car not managing whose tires fall off early saves the rest of the run (once a run)
    stuck in dirty air      stuck behind a car it is faster than: drops back to cool its tires, then a run at it
                            (once a run)

A mode is a pace change plus a resource change (live tests 2026-10-25: a slower car does not save tires or fuel
by itself in NR2003, so behavior_mem keeps only part of the real wear / fuel use). Nothing is random: personality
is fixed per driver (strategy rating + a temperament from the driver's name), so the same situation gives the same
call. Teammates never battle each other this way.

Pure logic: no memory access, no GUI. gather() builds the inputs from the strategy engine after its tick.
"""
from __future__ import annotations

import statistics
import zlib
from dataclasses import dataclass, field

from .carno import CarNo

NORMAL, SAVE_FUEL, SAVE_TIRES, COOL, PUSH = "", "SAVE FUEL", "SAVE TIRES", "COOL TIRES", "PUSH"
MODES = (NORMAL, SAVE_FUEL, SAVE_TIRES, COOL, PUSH)
SAVING = (SAVE_FUEL, SAVE_TIRES, COOL)

MANAGE, RUN_OUT, RIDE = "tire manager", "runs it out", "riding"     # run plan styles
ATTACK = "attacker"     # pushes the opening laps of a short-pit run, then pits early (2026-10-28)
ATTACK_SHARE = 0.3      # attacker: pushes about this share of the run (3-10 laps)

# Strategy and behavior agree (user, 2026-10-28: "a driver saving tires is likely going to stretch the run, one
# who pushes early may plan to pit sooner - not absolute, but most of the time"). One tendency per driver drives
# both: the strategy engine leans its stop style on it, and the run plan follows the stop style it picked.
STRETCH_STYLE, SHORT_PIT_STYLE = "stretch", "short-pit"     # strategy.STRETCH / SHORT_PIT (no import cycle)
STRETCH_RUNS_OUT = 0.9  # a stretch plan is run by a tire manager, except the most aggressive ~10 %
SHORT_PIT_MANAGES = 0.3  # a short-pit plan is attacked, except by the most patient ~30 %


# Temperament per car number for the loaded session: the driver's .car aggression as a percentile of the field
# (aggression.temperaments; set by the app when the track / mod loads). Cars missing from it use the name hash.
_TEMPERS: dict[int, float] = {}


def set_temperaments(table: dict[int, float] | None):
    _TEMPERS.clear()
    _TEMPERS.update({int(k): float(v) for k, v in (table or {}).items()})


def temperament(name: str, number: int | None = None) -> float:
    """0 = patient ... 1 = aggressive. Fixed per driver: his .car aggression's place in this field (2026-11-05),
    else a value from his name, so the same driver always leans the same way."""
    if number is not None:
        t = _TEMPERS.get(int(number))
        if t is not None:
            return t
    return (zlib.crc32((name or "").encode("utf-8", "replace")) % 100) / 100.0


def manager_score(rating: int, temper: float, length: float) -> float:
    """0..1: how likely this driver manages its tires over a run this long. Smart (strategy rating) and patient
    drivers manage; longer runs make everyone more likely to."""
    return 0.4 * (rating / 100.0) + 0.4 * (1.0 - temper) + 0.2 * min(1.0, length / 80.0)

DEFAULT_LAP_TIME = 30.0
BATTLE_GAP = 1.0        # s: top-3 cars this close are battling for the lead
LAPPING_GAP = 2.0       # s: the leader this close behind a lead-lap car is about to lap it
STUCK_GAP = 0.45        # s: this close behind the car ahead ...
STUCK_LAPS = 5          # ... for this many laps (and faster than the cars around) = stuck in its air
COOL_LAPS = 2           # laps dropping back to cool the tires, then a run of RUN_LAPS
RUN_LAPS = 3
MANAGE_MIN_RUN = 20     # laps: shorter runs are just run out
MANAGE_SCORE = 0.62     # manager score from which a driver manages its tires (about half a typical field)
PUSH_END_LAPS = 3       # a tire manager pushes the last laps of its run
STAGE_PUSH_LAPS = 5     # drafting: the pack pushes the last laps of a stage (and the race)
PACK_GAP = 0.8          # s: drafting tracks, cars this close in a line run in the same pack
OFF_TICKS = 10          # ticks (~1.5 s) a car must be off the track (pit road / damage) before its mode resets
SITUATIONAL = frozenset(("lead_attack", "lead_defend", "top3_attack", "top3_defend", "lapping"))
FUEL_SAVE_GAIN = 1 / 0.85 - 1   # laps gained per lap of saving at the default fuel keep (x0.85)
RIDE_SKIP_TEMPER = 0.88  # drafting: the most aggressive ~12 % don't ride and save unless their fuel plan needs it


@dataclass(frozen=True)
class Effect:
    """traction / power: scale on the driver's own grip and engine values. wear / fuel: share of the car's real
    tire wear / fuel use that is kept (0.8 = 20 % less, 1.2 = 20 % more)."""
    traction: float = 1.0
    power: float = 1.0
    wear: float = 1.0
    fuel: float = 1.0

    def scaled(self, k: float) -> "Effect":
        f = lambda v: 1.0 + (v - 1.0) * k
        return Effect(f(self.traction), f(self.power), f(self.wear), f(self.fuel))

    @property
    def neutral(self) -> bool:
        return self == NEUTRAL


NEUTRAL = Effect()
# Sizes at 100 % strength, from the live tests (traction x0.97 = 0.2-0.5 s a lap on a 28 s track; wear x0.7 = 28 %
# less wear and faster laps later in the run; fuel x0.8 = 22 % less fuel). Drafting tracks: grip doesn't limit the
# pace there, power and the draft do.
EFFECTS = {
    "other": {SAVE_FUEL: Effect(0.995, 0.98, 1.0, 0.85),
              SAVE_TIRES: Effect(0.985, 1.0, 0.75, 0.98),
              COOL: Effect(0.975, 0.995, 0.6, 0.95),
              PUSH: Effect(1.015, 1.01, 1.25, 1.08)},
    "draft": {SAVE_FUEL: Effect(1.0, 0.985, 1.0, 0.85),
              SAVE_TIRES: NEUTRAL, COOL: NEUTRAL,
              PUSH: Effect(1.0, 1.015, 1.1, 1.06)},
}


@dataclass
class BehaviorConfig:
    enabled: bool = False
    strength: int = 70          # % of the effect sizes above
    late_push_pct: int = 10     # final % of the race in which everyone who can pushes (0 = off)
    reactions: bool = True      # on-the-fly changes (lead battles, staying on the lead lap, tires, dirty air)
    teams: bool = True          # teammates don't battle; drafting: team / make packs save and push together


@dataclass
class Ctx:
    race: bool
    green: bool
    lap_now: int
    total: int
    to_go: float
    lap_time: float = DEFAULT_LAP_TIME
    drafting: bool = False
    stage_end: int | None = None    # race lap the coming stage ends on
    stage_ends: tuple = ()          # race laps where the stages end (all but the last; racing intensity)


@dataclass
class Car:
    idx: int
    number: int
    name: str
    make: str = ""
    team: int = -1
    rating: int = 50
    lap: int = 0                    # car lap it is on
    laps_down: int = 0
    progress: float = 0.0           # laps + fraction round the lap (distance covered: race order)
    on_track: bool = True           # racing under green: not pitting, not on pit road, not damaged/out
    fuel_laps: float | None = None  # laps of fuel left
    fuel_margin: float | None = None  # laps of fuel over (+) / short (-) of the finish
    laps_in_run: float | None = None  # laps until its next stop (or the finish)
    stint_laps: int = 0             # laps since its last stop
    run_id: int = 0                 # changes when it starts a new run (its last stop lap)
    falloff: float | None = None    # fraction slower than early in this run
    pace_delta: float | None = None  # s/lap slower (+) / faster (-) than the cars around it
    waits_for_stage: bool = False   # plans to pit at the coming stage break
    stretch: bool = False           # its stop plan only works by saving fuel (a stop fewer)
    stop_style: str = ""            # the strategy engine's stop style for this run: stretch / split / short-pit
    damage: float = 0.0             # the game's damage sum (racing intensity: hurt cars protect the car)

    @property
    def temper(self) -> float:
        return temperament(self.name, self.number)


@dataclass
class RunPlan:
    style: str = RUN_OUT
    start: int = 0                  # car lap the run started (its stop lap)
    end: int = 0                    # car lap the run is expected to end (stop / finish)
    save_from: int = 0              # tire manager: saves on car laps save_from .. save_to - 1,
    save_to: int = 0                # then pushes from end - PUSH_END_LAPS
    push_to: int = 0                # attacker: pushes on car laps start + 1 .. push_to - 1


@dataclass
class CarMode:
    mode: str = NORMAL
    reason: str = ""
    code: str = ""                  # why, as a key (see Switch.code)
    since: int = 0                  # car lap it started
    decided_lap: int = -1
    run_id: int | None = None
    plan: RunPlan | None = None
    stuck: int = 0                  # laps stuck in the air of the car ahead
    cool_used: bool = False         # cooled / ran once this run already
    cool_until: int = 0             # car lap the cool + run sequence ends
    tires_gone: bool = False        # switched to saving the rest of the run (tires went away early)
    plan_style: str = ""            # the stop style the run plan was made from
    off_ticks: int = 0              # ticks in a row seen off the track (pit road / damage) under green

    @property
    def situational(self) -> bool:
        """A push for the moment (a lead battle, holding off the leader), not part of the car's plan: pack
        followers don't copy it."""
        return self.code in SITUATIONAL


@dataclass
class Switch:
    idx: int
    number: int
    name: str
    lap: int
    old: str
    new: str
    reason: str
    code: str = ""          # why, as a key for the radio: fuel_finish, fuel_stage, late, lead_attack, lead_defend,
                            # top3_attack, top3_defend, lapping, cool, run, tires_gone, manage, end_run, ride,
                            # stage_push, draft_end, follow, normal
    other: int | None = None  # the other car's number (the car battled, followed, stuck behind)
    value: float | None = None  # laps (short of fuel, to go, left in the run)
    detail: str = ""        # follow: "team" / "make"


@dataclass
class BehaviorResult:
    modes: dict = field(default_factory=dict)       # idx -> (mode, reason)
    effects: dict = field(default_factory=dict)     # idx -> Effect (only cars with a non-neutral effect)
    switches: list = field(default_factory=list)


class BehaviorEngine:
    def __init__(self, cfg: BehaviorConfig | None = None):
        self.cfg = cfg or BehaviorConfig()
        self.reset()

    def reset(self):
        self.state: dict[int, CarMode] = {}
        self._field_fall = 0.0          # median tire falloff of the field this tick
        self._tag: tuple = ("normal", None, None, "")

    def mode(self, idx: int) -> str:
        st = self.state.get(idx)
        return st.mode if st else NORMAL

    def plan(self, idx: int) -> RunPlan | None:
        st = self.state.get(idx)
        return st.plan if st else None

    # ------------------------------------------------------------------ tick
    def tick(self, ctx: Ctx, cars: list[Car]) -> BehaviorResult:
        res = BehaviorResult()
        active = self.cfg.enabled and ctx.race and ctx.green
        order = sorted(cars, key=lambda c: -c.progress)            # race order (distance covered), front first
        lt = ctx.lap_time if ctx.lap_time and ctx.lap_time > 1 else DEFAULT_LAP_TIME
        pos = {c.idx: n for n, c in enumerate(order)}
        late = active and self._late(ctx)
        falls = [c.falloff for c in cars if c.on_track and c.falloff is not None and c.stint_laps >= 5]
        self._field_fall = statistics.median(falls) if len(falls) >= 3 else 0.0
        leaders = self._pack_leaders(order, lt) if active and ctx.drafting and self.cfg.teams else {}
        for c in order:
            st = self.state.setdefault(c.idx, CarMode())
            if c.run_id != st.run_id:                              # a new run (pitted / race start): new plan
                st.run_id, st.plan, st.stuck, st.cool_used, st.tires_gone = c.run_id, None, 0, False, False
                st.cool_until = 0
                st.decided_lap = -1
            if active and not c.on_track:
                st.off_ticks += 1
            elif c.on_track:
                st.off_ticks = 0
            if not active or st.off_ticks >= OFF_TICKS:            # caution: at once; off the track: once it lasts
                self._tag = ("normal", None, None, "")
                self._set(st, c, NORMAL, "", res)
                st.decided_lap = -1
                continue
            if st.off_ticks:                                       # a blip (pit road flag / state flicker): hold
                self._report(st, c, res, ctx)
                continue
            n = pos[c.idx]
            ahead = order[n - 1] if n > 0 else None
            behind = order[n + 1] if n + 1 < len(order) else None
            gap_a = (ahead.progress - c.progress) * lt if ahead else None
            gap_b = (c.progress - behind.progress) * lt if behind else None
            if st.plan is None or (st.plan_style != c.stop_style and c.stint_laps <= 3 and not ctx.drafting):
                # (the stop style can come a moment after the run starts: the plan follows it early in the run)
                st.plan, st.plan_style = self._make_plan(ctx, c), c.stop_style
            elif c.laps_in_run is not None:                         # AIMOD may replan the stop: follow it
                self._retime(st.plan, c)
            if st.decided_lap != c.lap:                            # once per lap, at its line crossing
                if st.decided_lap >= 0:
                    rival = ahead is not None and self._racing(c, ahead)
                    st.stuck = st.stuck + 1 if (rival and gap_a is not None and gap_a < STUCK_GAP) else 0
                st.decided_lap = c.lap
                self._tag = ("normal", None, None, "")
                mode, why = self._decide(ctx, c, st, n, order, ahead, behind, gap_a, gap_b, late, leaders, lt)
                self._set(st, c, mode, why, res)
            self._report(st, c, res, ctx)
        for idx in list(self.state):
            if idx not in pos:
                del self.state[idx]
        return res

    def _report(self, st: CarMode, c: Car, res: BehaviorResult, ctx: Ctx):
        if st.mode != NORMAL:
            res.modes[c.idx] = (st.mode, st.reason)
            eff = self.effect(st.mode, ctx.drafting)
            if not eff.neutral:
                res.effects[c.idx] = eff

    def effect(self, mode: str, drafting: bool) -> Effect:
        base = EFFECTS["draft" if drafting else "other"].get(mode, NEUTRAL)
        return base.scaled(max(0, min(200, self.cfg.strength)) / 100.0)

    def _t(self, mode: str, why: str, code: str, other=None, value=None, detail: str = ""):
        """A decision with its reason key (for the radio)."""
        self._tag = (code, other, value, detail)
        return mode, why

    def _set(self, st: CarMode, c: Car, mode: str, why: str, res: BehaviorResult):
        code, other, value, detail = self._tag
        if mode == st.mode:
            if mode != NORMAL and why:
                st.reason, st.code = why, code
            return
        res.switches.append(Switch(c.idx, c.number, c.name, c.lap, st.mode, mode, why, code, other, value, detail))
        st.mode, st.reason, st.code, st.since = mode, why, code, c.lap

    # ------------------------------------------------------------------ plan
    def _make_plan(self, ctx: Ctx, c: Car) -> RunPlan:
        start = c.lap - c.stint_laps
        left = c.laps_in_run if c.laps_in_run is not None else 0.0
        end = int(round(c.lap + left))
        length = end - start
        if ctx.drafting:
            return RunPlan(RIDE, start, end)
        if length < MANAGE_MIN_RUN:
            return RunPlan(RUN_OUT, start, end)
        # the run plan follows the stop plan: a stretch is run by managing the tires, a short-pit by pushing early
        # (each with its exceptions); an even split goes by the driver's own leaning
        if c.stop_style == STRETCH_STYLE:
            manage = c.temper < STRETCH_RUNS_OUT
        elif c.stop_style == SHORT_PIT_STYLE:
            if c.temper >= SHORT_PIT_MANAGES:
                return RunPlan(ATTACK, start, end, push_to=start + 1 + max(3, min(10, int(length * ATTACK_SHARE))))
            manage = True
        else:
            manage = self._manager_score(c, length) >= MANAGE_SCORE
        if not manage:
            return RunPlan(RUN_OUT, start, end)
        save_from = start + 4
        save_to = end - max(PUSH_END_LAPS + 2, int(length * 0.25))
        return RunPlan(MANAGE, start, end, save_from, max(save_from, save_to))

    @staticmethod
    def _retime(p: RunPlan, c: Car):
        """The run's end moved (a new pit plan, a caution): same style, the saving window from the new end."""
        p.end = int(round(c.lap + c.laps_in_run))
        if p.style == MANAGE:
            length = p.end - p.start
            p.save_to = max(p.save_from, p.end - max(PUSH_END_LAPS + 2, int(length * 0.25)))

    @staticmethod
    def _manager_score(c: Car, length: float) -> float:
        return manager_score(c.rating, c.temper, length)

    # -------------------------------------------------------------- helpers
    def _late(self, ctx: Ctx) -> bool:
        if self.cfg.late_push_pct <= 0 or ctx.total <= 0 or ctx.to_go <= 0:
            return False
        return ctx.to_go <= max(3.0, ctx.total * self.cfg.late_push_pct / 100.0)

    def _teammates(self, a: Car, b: Car) -> bool:
        return self.cfg.teams and a.team >= 0 and a.team == b.team

    def _racing(self, a: Car, b: Car) -> bool:
        """a and b race each other: same lap, not teammates."""
        return a.laps_down == b.laps_down and not self._teammates(a, b)

    @staticmethod
    def _fuel_short(ctx: Ctx, c: Car) -> tuple[float, str] | None:
        """(laps short, of what) when the car can only reach its target by saving fuel; None otherwise (it makes
        it, or it is too far short and must stop anyway)."""
        if c.fuel_laps is None:
            return None
        if c.waits_for_stage and ctx.stage_end is not None:
            target, what = ctx.stage_end - ctx.lap_now + 1.0, "the stage break"
        elif c.fuel_margin is not None and ctx.to_go > 0:
            target, what = ctx.to_go, "the finish"
        else:
            return None
        short = target + 0.5 - c.fuel_laps
        reach = FUEL_SAVE_GAIN * min(target, c.fuel_laps)
        return (short, what) if 0 < short <= reach else None

    @staticmethod
    def _push_ok(c: Car) -> bool:
        """Enough fuel to burn more than usual to the end of the run (pushing burns ~8 % more)."""
        if c.fuel_laps is None or c.laps_in_run is None:
            return True
        return c.fuel_laps >= c.laps_in_run * 1.08 + 1.0

    def _pack_leaders(self, order: list[Car], lt: float) -> dict[int, Car]:
        """Drafting tracks: car idx -> the front car of its team (else make) in its pack. Packs are lines of cars
        less than PACK_GAP s apart on the same lap."""
        out: dict[int, Car] = {}
        pack: list[Car] = []
        for k, c in enumerate(order):
            prev = order[k - 1] if k else None
            if prev is None or (prev.progress - c.progress) * lt > PACK_GAP or prev.laps_down != c.laps_down:
                pack = []
            lead = next((p for p in pack if p.team >= 0 and p.team == c.team), None)
            if lead is None and c.make:
                lead = next((p for p in pack if p.make == c.make), None)
            out[c.idx] = lead if lead is not None else c
            pack.append(c)
        return out

    def _falloff_limit(self, c: Car) -> float:
        """Falloff (fraction of a lap) at which a driver not managing its tires decides they are going away (and
        1.5 x the field's: worse than everyone's tires, not just a long run): a better strategy rating and a patient
        temperament notice sooner."""
        return 0.008 + 0.008 * (1.0 - c.rating / 100.0) + 0.004 * c.temper

    # ---------------------------------------------------------------- decide
    def _decide(self, ctx, c: Car, st: CarMode, n, order, ahead, behind, gap_a, gap_b, late, leaders, lt):
        short = self._fuel_short(ctx, c)
        if short is not None:
            return self._t(SAVE_FUEL, f"{short[0]:.1f} laps short of {short[1]}",
                           "fuel_stage" if short[1] == "the stage break" else "fuel_finish", value=short[0])
        push_ok = self._push_ok(c)
        if late and push_ok:
            return self._t(PUSH, "late in the race", "late", value=ctx.to_go)
        if self.cfg.reactions and push_ok:
            r = self._situation(c, n, order, ahead, behind, gap_a, gap_b, lt, ctx.drafting)
            if r is not None:
                return r
        if ctx.drafting:
            return self._draft(ctx, c, st, push_ok, leaders)
        if self.cfg.reactions:
            r = self._on_the_fly(c, st, ahead, push_ok)
            if r is not None:
                return r
        return self._follow_plan(c, st, push_ok)

    def _situation(self, c: Car, n, order, ahead, behind, gap_a, gap_b, lt, drafting=False):
        """Lead battles and holding off the leader: the only reasons a car leaves its plan to push. At drafting
        tracks cars of the same make at the front work together instead of battling."""
        rivals = lambda a, b: self._racing(a, b) and not (drafting and self.cfg.teams and a.make and a.make == b.make)
        if c.laps_down == 0 and n <= 2:
            if (ahead is not None and gap_a is not None and gap_a <= BATTLE_GAP and rivals(c, ahead)
                    and ahead.laps_down == 0):
                if n == 1:
                    return self._t(PUSH, "racing #%s for the lead" % ahead.number, "lead_attack", ahead.number)
                return self._t(PUSH, "racing #%s, top 3" % ahead.number, "top3_attack", ahead.number)
            if (n <= 1 and behind is not None and gap_b is not None and gap_b <= BATTLE_GAP
                    and rivals(c, behind) and behind.laps_down == 0):
                if n == 0:
                    return self._t(PUSH, "defending the lead from #%s" % behind.number, "lead_defend", behind.number)
                return self._t(PUSH, "holding #%s off, top 3" % behind.number, "top3_defend", behind.number)
        lead = order[0] if order else None
        if (lead is not None and lead is not c and c.laps_down == 0 and not self._teammates(c, lead)
                and 0 < (c.progress + 1.0 - lead.progress) * lt <= LAPPING_GAP):
            return self._t(PUSH, "holding off the leader #%s to stay on the lead lap" % lead.number, "lapping",
                           lead.number)
        return None

    def _on_the_fly(self, c: Car, st: CarMode, ahead, push_ok):
        if st.cool_until and c.lap < st.cool_until:              # cool + run sequence under way
            if c.lap < st.cool_until - RUN_LAPS:
                return self._t(COOL, st.reason or "cooling the tires", "cool", ahead.number if ahead else None)
            tgt = f" on #{ahead.number}" if ahead is not None else ""
            if push_ok:
                return self._t(PUSH, f"making a run{tgt} after cooling the tires", "run",
                               ahead.number if ahead else None)
            return self._t(NORMAL, "", "normal")
        left = (st.plan.end - c.lap) if st.plan else 99
        if (not st.cool_used and st.stuck >= STUCK_LAPS and ahead is not None and left >= COOL_LAPS + RUN_LAPS + 3
                and (c.pace_delta or 0) < 0):
            st.cool_used, st.stuck = True, 0
            st.cool_until = c.lap + COOL_LAPS + RUN_LAPS
            return self._t(COOL, f"stuck behind #{ahead.number}, dropping back to cool the tires", "cool", ahead.number)
        if (not st.tires_gone and st.plan is not None and st.plan.style in (RUN_OUT, ATTACK) and c.falloff is not None
                and c.falloff >= max(self._falloff_limit(c), 1.5 * self._field_fall) and left >= 10
                and c.stint_laps >= 5):
            st.tires_gone = True
        return None

    def _follow_plan(self, c: Car, st: CarMode, push_ok):
        p = st.plan
        if p is None:
            return self._t(NORMAL, "", "normal")
        if st.tires_gone and c.lap < p.end - PUSH_END_LAPS:
            return self._t(SAVE_TIRES, "tires going away, saving them for the rest of the run", "tires_gone",
                           value=p.end - c.lap)
        if p.style == ATTACK and p.start < c.lap < p.push_to and push_ok:
            return self._t(PUSH, f"pushing early, short-pitting (stop lap {p.end})", "attack", value=p.end - c.lap)
        if p.style == MANAGE:
            if p.save_from <= c.lap < p.save_to:
                return self._t(SAVE_TIRES, f"managing tires (run to lap {p.end})", "manage", value=p.end - c.lap)
            if c.lap >= p.end - PUSH_END_LAPS and push_ok:
                return self._t(PUSH, "end of the run, using the tires he saved", "end_run", value=p.end - c.lap)
        return self._t(NORMAL, "", "normal")

    def _draft(self, ctx, c: Car, st: CarMode, push_ok, leaders):
        lead = leaders.get(c.idx, c)
        if lead is not c and lead.idx in self.state and not self.state[lead.idx].situational:
            # in the pack with its team / make: follow the lead car's plan (never its lead battle)
            lm = self.state[lead.idx]
            why = "team" if lead.team >= 0 and lead.team == c.team else "make"
            if lm.mode == PUSH and not push_ok:
                return self._t(NORMAL, "", "normal")
            if lm.mode == SAVE_FUEL and c.temper >= RIDE_SKIP_TEMPER and not c.stretch:
                return self._t(NORMAL, "", "normal")
            if lm.mode != NORMAL:
                return self._t(lm.mode, f"with #{lead.number} ({why})", "follow", lead.number, detail=why)
            # its lead car isn't saving or pushing: it runs its own plan
        # the front car of its team / make in this pack (or a follower whose lead car is battling) runs its own plan
        stage_left = (ctx.stage_end - ctx.lap_now + 1) if ctx.stage_end is not None else None
        if stage_left is not None and 0 < stage_left <= STAGE_PUSH_LAPS and push_ok:
            return self._t(PUSH, "pushing for the stage end", "stage_push", value=stage_left)
        chances = [x for x in (c.laps_in_run, stage_left, ctx.to_go) if x is not None and x > 0]
        next_chance = min(chances) if chances else 0.0
        # the run ends in a green flag stop (not the stage break or the finish): the pack keeps saving into the stop
        green_stop = (c.laps_in_run is not None and c.laps_in_run == next_chance and not c.waits_for_stage
                      and c.laps_in_run < ctx.to_go and (stage_left is None or c.laps_in_run < stage_left))
        if c.stint_laps >= 3 and (next_chance > STAGE_PUSH_LAPS or green_stop):
            if c.stretch:
                return self._t(SAVE_FUEL, "saving fuel to make it a stop fewer", "ride")
            if c.temper < RIDE_SKIP_TEMPER:
                return self._t(SAVE_FUEL, "riding, saving fuel for a shorter stop", "ride")
            return self._t(NORMAL, "", "normal")
        if next_chance <= STAGE_PUSH_LAPS and push_ok and c.stint_laps >= 3:
            return self._t(PUSH, "end of the run", "draft_end", value=next_chance)
        return self._t(NORMAL, "", "normal")


def gather(engine, res, snap, p, track_type: str | None = None) -> tuple[Ctx, list[Car]]:
    """Inputs from the strategy engine after its tick (TickResult rows + engine state)."""
    from . import memory_map as mm
    from .strategy import STAGE_BREAK, SUPERSPEEDWAY
    ctx = Ctx(race=snap.session == mm.SESSION_RACE, green=snap.flag == mm.FLAG_GREEN,
              lap_now=engine.lap_now, total=engine.lap_total, to_go=res.laps_to_go,
              lap_time=engine.lap_time or DEFAULT_LAP_TIME,
              drafting=bool(snap.drafting or (track_type or p.track_type) == SUPERSPEEDWAY),
              stage_end=engine.next_stage_end(), stage_ends=tuple(getattr(engine, "stage_list", ()) or ()))
    by_idx = {c.idx: c for c in snap.cars if c.valid}
    cars = []
    for r in res.rows:
        sc = by_idx.get(r.idx)
        if r.out or sc is None or not sc.controlled or r.idx in (mm.PLAYER_SLOT, mm.PACE_SLOT):
            continue
        ps = engine.prog_state.get(r.idx)
        if ps is None:
            continue
        progress = ps[0] + ps[2] + ps[1]
        st = engine.cars.get(r.idx)
        planned = st.planned_lap if st is not None else None
        last_stop = st.last_stop_lap if st is not None else 0
        car_lap = r.lap or (sc.laps_done + 1)
        waits = planned == STAGE_BREAK
        stretch = bool(st is not None and st.borderline and isinstance(planned, int) and not isinstance(planned, bool))
        if isinstance(planned, int) and not isinstance(planned, bool):
            left = max(0.0, planned - car_lap)
        elif waits and ctx.stage_end is not None:
            left = max(0.0, ctx.stage_end - ctx.lap_now + 1.0)
        elif r.fuel_margin is not None and r.fuel_margin >= 0:
            left = res.laps_to_go
        else:
            left = r.fuel_laps
        on_track = (r.kind == "green" and not sc.on_pit_road and not sc.pitting and not sc.reason_out)
        cars.append(Car(r.idx, CarNo(r.number), r.name, make=r.make, team=r.team, rating=r.rating, lap=car_lap,
                        laps_down=r.laps_down, progress=progress, on_track=on_track, fuel_laps=r.fuel_laps,
                        fuel_margin=r.fuel_margin, laps_in_run=left, stint_laps=max(0, car_lap - last_stop),
                        run_id=last_stop, falloff=r.falloff, pace_delta=r.pace_delta, waits_for_stage=waits,
                        stretch=stretch, stop_style=(st.style if st is not None else "") or "",
                        damage=float(getattr(sc, "damage", 0.0) or 0.0)))
    return ctx, cars
