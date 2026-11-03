"""Crew chief strategy brain.

Scores STAY_OUT / FOUR_TIRE / TWO_TIRE options for a caution call, and
shifts green-flag style weights, using per-car quality and aggression profiles
derived from the .car file data (pitcrew speed, pitcrew consistency).

The brain sits between the strategy engine's telemetry and its behavior
module: telemetry feeds it (tire age, fuel, position, laps to go); it
outputs a call and a note; the behavior module executes the consequences.

Teammate influence is an *offset* signal, not a follow signal.  The
engine's existing team_follow % already copies a teammate's call directly.
The offset here applies to the other cases: when the car does NOT follow,
it leans to a different strategy from its teammate so the two are on
different cycles.  One of them will get lucky.
"""
from __future__ import annotations

import dataclasses
import random
from dataclasses import dataclass


# Caution option tokens
STAY_OUT = "stay_out"
FOUR_TIRE = "four_tire"
TWO_TIRE = "two_tire"

# Track type position values and costs — parallel to strategy.py constants.
_POS_VAL = {
    "superspeedway": 5,
    "intermediate": 10,
    "short": 20,
    "road": 8,
}
_POS_COST = {
    "superspeedway": 3,
    "intermediate": 8,
    "short": 15,
    "road": 6,
}


@dataclass
class CrewChiefProfile:
    """Per-car crew chief characteristics derived from car file data.

    cc_rating   0-1: decision quality — accurate reads, low noise.
    aggression  0-1: boldness — stay out, short-pit, caution gambles.
    finishing   0-1: end-race intensity — stretch fuel/tires to the flag.
    reliability 0-1: mechanical fragility — low = protect the car, pit sooner.
    short_track / speedway / superspeedway / road_course 0-1: track specialty
        ratings; a specialist gets tighter noise on their home surface.
    """
    cc_rating:    float = 0.5
    aggression:   float = 0.5
    finishing:    float = 0.5
    reliability:  float = 0.5
    short_track:  float = 0.5
    speedway:     float = 0.5
    superspeedway: float = 0.5
    road_course:  float = 0.5

    @classmethod
    def from_car_info(cls, info: dict, driver_name: str = "") -> "CrewChiefProfile":
        """Build from the dict returned by carfiles.car_info().

        All params are on the NR2003 ~0-2 scale, normalised to 0-1.

        cc_rating formula (decision quality):
            pitcrew_strategy   0.35  Papyrus's own crew strategy quality rating
            pitcrew_consistency 0.20  crew execution reliability
            pitcrew_speed      0.20  team resources / speed proxy
            driver_consistency 0.15  consistent driver → better crew feedback
            qual_speed         0.10  team preparation quality proxy

        aggression: aiParamDriverAggression from the car file (replaces
        the CRC name hash used previously).  Falls back to the hash when
        no car data is available.
        """
        pc_speed = info.get("pitcrew_speed")
        if pc_speed is None:
            aggression = _aggression_from_name(driver_name)
            return cls(aggression=aggression)

        pc_strat  = info.get("pitcrew_strategy") or pc_speed
        pc_con    = info.get("pitcrew_consistency") or pc_speed
        drv_con   = info.get("driver_consistency") or pc_con
        qual      = info.get("qual_speed") or pc_speed

        rating = (_norm(pc_strat)  * 0.35
                + _norm(pc_con)    * 0.20
                + _norm(pc_speed)  * 0.20
                + _norm(drv_con)   * 0.15
                + _norm(qual)      * 0.10)

        drv_agg = info.get("driver_aggression")
        aggression = _norm(drv_agg) if drv_agg is not None else _aggression_from_name(driver_name)

        return cls(
            cc_rating=_clamp(rating),
            aggression=_clamp(aggression),
            finishing=_norm(info.get("driver_finishing") or pc_speed),
            reliability=_norm(info.get("vehicle_reliability") or pc_speed),
            short_track=_norm(info.get("driver_shorttrack") or pc_speed),
            speedway=_norm(info.get("driver_speedway") or pc_speed),
            superspeedway=_norm(info.get("driver_superspeedway") or pc_speed),
            road_course=_norm(info.get("driver_roadcourse") or pc_speed),
        )

    def with_strategy(self, strategy_frac: float) -> "CrewChiefProfile":
        """Return a copy blending in the game's per-car strategy field (0-1).

        The game's strategy value reflects how sophisticated the AI's own
        decisions are.  Blending it in (30 % weight) means a car that the
        game considers a strong strategist gets a quality boost even without
        pitcrew data.
        """
        r = self.cc_rating * 0.70 + _clamp(strategy_frac) * 0.30
        return dataclasses.replace(self, cc_rating=_clamp(r))


# ---------------------------------------------------------------------------
# Caution call

def caution_call(
    track_type: str,
    tire_age: float,        # 0 = new, 1 = worst in field
    to_go: float,           # laps remaining in race
    fuel_ok: bool,          # car makes it to the finish without pitting
    me_rank: int,           # current running position (1-based)
    field_size: int,        # cars in the lead lap
    profile: CrewChiefProfile,
    rng: random.Random,
    teammate_decision: bool | None = None,   # True = teammate pitted; False = stayed out
    teammate_rank: int | None = None,
    laps_down: int = 0,          # laps behind the leader (>0 = not on lead lap)
    has_wave_around: bool = True, # era has a lucky-dog / wave-around rule
) -> tuple[bool, str | None, str]:
    """Score the three options and return (pit, choice_token, note).

    choice_token is FOUR_TIRE, TWO_TIRE, or None (stay out).
    The caller converts FOUR_TIRE -> FOUR_TIRES and TWO_TIRE -> the
    appropriate side constant (RIGHT_SIDE / LEFT_SIDE) using prob_l/prob_r.

    laps_down + has_wave_around interact:
      - has_wave_around=True  (modern / stage era): staying out under caution
        may return the lap before the restart — STAY_OUT is worth more.
      - has_wave_around=False (pre-stage era): lapped cars gain nothing from
        staying out. They restart lapped regardless, on older tires with less
        fuel. Treat them like any lead-lap car — no wave-around credit.
    """
    # Teammate influence differs by track type:
    # - superspeedway: pit TOGETHER (drafting pact) — both directions count
    # - everywhere else: pit on DIFFERENT cycles (offset) — only when you're behind
    influence = 0.0
    if teammate_decision is not None and teammate_rank is not None:
        rank_diff = me_rank - teammate_rank
        if track_type == "superspeedway":
            influence = _clamp(abs(rank_diff) / 20.0)
        elif rank_diff > 0:
            influence = _clamp(rank_diff / 15.0)

    scores = _raw_scores(track_type, tire_age, to_go, fuel_ok,
                         me_rank, field_size, profile, rng)
    scores = _offset_from_teammate(scores, teammate_decision, influence, track_type)

    # Wave-around (modern era only): a lapped car that stays out under caution
    # may get the free lap back before the restart — pitting surrenders that
    # opportunity.  In pre-stage / pre-wave-around era the rule doesn't exist:
    # a lapped car gains nothing from staying out and is at a disadvantage on
    # fuel and tires vs. cars that pit, so no bonus is applied.
    if laps_down > 0 and has_wave_around:
        wave_pull = min(12.0, laps_down * 6.0)
        if track_type == "superspeedway":
            wave_pull *= 1.4     # drafting makes the free lap back especially valuable
        scores[STAY_OUT]  += wave_pull
        scores[FOUR_TIRE] -= laps_down * 3.0
        # two-tire is a gentler compromise: keeps most of wave-around chance
        # while freshening the worst side of the car slightly
        scores[TWO_TIRE]  += 1.0

    choice = _pick(scores, rng)

    if choice == STAY_OUT:
        suffix = " (wave-around)" if (laps_down > 0 and has_wave_around) else ""
        note = (f"stays out: track position{suffix} "
                f"({tire_age:.0%} worn, score {scores[STAY_OUT]:.0f})")
        return False, None, note

    token = FOUR_TIRE if choice == FOUR_TIRE else TWO_TIRE
    sname = "4 tires" if token == FOUR_TIRE else "2 tires"
    note = (f"pits for {sname} "
            f"(score {scores[choice]:.0f} vs stay {scores[STAY_OUT]:.0f})")
    return True, token, note


# ---------------------------------------------------------------------------
# Green-flag style weights

def apply_style_bias(weights: dict, profile: CrewChiefProfile,
                     track_type: str = "intermediate",
                     window_frac: float = 0.5,
                     has_stages: bool = True) -> dict:
    """Shift SHORT_PIT / SPLIT / STRETCH weights by crew chief personality,
    stint position, and era.

    Called from StrategyEngine.choose_style after its own weights are built.
    The shift is multiplicative so it preserves all existing adjustments;
    it never zeroes out any option.

    window_frac: 0 = just pitted (fresh), 1 = fuel deadline, >1 = past it.
      Early stints lean STRETCH; late stints lean SHORT_PIT.

    has_stages: False when no stage break is coming (pre-stage era or the
      final stint of a stage-era race). Without a free break ahead, giving
      up track position via a short-pit is riskier and stretching for a
      natural caution is more rewarding.

    Superspeedway note: SHORT_PIT is penalised because track position is
    cheap to recover by drafting — an early stop just splits the car from
    the pack. Aggressive crews gamble on cautions (STRETCH) instead.
    """
    a   = profile.aggression
    fin = profile.finishing
    rel = profile.reliability
    ss  = track_type == "superspeedway"
    w   = dict(weights)

    # ---- Window position: where are we in the fuel stint? ------------------
    # Early (< 0.40): patience — STRETCH, hold position
    # Late  (> 0.65): urgency  — SHORT_PIT, time to think about going
    if window_frac < 0.40:
        fresh = (0.40 - window_frac) / 0.40     # 1.0 right after a stop, 0 at 40%
        w.setdefault("stretch", w.get("stretch", 1.0))
        for k in list(w):
            if k == "stretch":
                w[k] *= (1.0 + fresh * 0.55)
            elif "short" in k.lower() or k == "short-pit":
                w[k] *= (1.0 - fresh * 0.30)
    elif window_frac > 0.65:
        deep = (window_frac - 0.65) / 0.35      # 0 at 65%, 1.0 at fuel deadline
        deep = min(deep, 1.0)
        for k in list(w):
            if "short" in k.lower() or k == "short-pit":
                w[k] *= (1.0 + deep * 0.70)
            elif k == "stretch":
                w[k] *= max(0.25, 1.0 - deep * 0.65)

    # ---- Era / stage context -----------------------------------------------
    # Pre-stage era or final stint: no free break coming.
    # Track position surrendered via a short-pit may never come back.
    # Running to the caution / fuel window is the right default.
    if not has_stages:
        for k in list(w):
            if k == "stretch":
                w[k] *= 1.40
            elif "short" in k.lower() or k == "short-pit":
                w[k] *= 0.72
        if ss:
            for k in list(w):
                if k == "stretch":
                    w[k] *= 1.20    # superspeedway pre-stage: even more caution-hunting

    # ---- Personality biases ------------------------------------------------
    for key in list(w):
        if "short" in key.lower() or key == "short-pit":
            if ss:
                w[key] *= 0.3
            else:
                w[key] *= (1.0 + a * 0.8)
                w[key] *= (1.0 - fin * 0.25)
                w[key] *= (0.5 + rel * 0.5)
        elif key == "stretch":
            if ss:
                w[key] *= (1.0 + a * 0.9)
            else:
                w[key] *= (1.0 + a * 0.5)
                w[key] *= (1.0 + fin * 0.35)
                w[key] *= (0.6 + rel * 0.4)
        elif key == "split":
            w[key] *= (1.0 - a * 0.25)
            w[key] *= (1.0 + (1.0 - rel) * 0.3)
    return w


# ---------------------------------------------------------------------------
# Fuel estimation noise

def plan_green_flag_stops(
    race_laps: float,
    tank_laps: float,
    profile: CrewChiefProfile,
    rng: random.Random,
) -> tuple[int, float]:
    """Compute ideal stop count and fuel-gamble buffer for a green-flag race.

    Returns (n_stops, gamble_laps):
      n_stops     — how many pit stops the crew is planning for this race.
                    A poor cc_rating introduces estimation noise; the crew
                    may plan wrong and have to adjust mid-race.
      gamble_laps — extra laps past the fuel deadline this CC will wait,
                    hoping a caution arrives, before pitting regardless.
                    Aggression drives this up; reliability drives it down
                    (fragile cars don't gamble with the engine).

    This is informational / display-level: the planning window that
    plan_stop() computes is still the authoritative timing. Use this to
    annotate strategy notes or tune the window_frac presented to the brain.
    """
    if tank_laps <= 0 or race_laps <= 0:
        return 0, 0.0

    # Crew's read of the fuel window — poor crews get it wrong
    noise = (1.0 - profile.cc_rating) * 0.14
    tank_est = tank_laps * max(0.72, 1.0 + rng.gauss(0, noise))

    n_stops = max(0, int(race_laps / tank_est))

    # Personality shift: aggressive crews aim for one fewer stop (fuel gamble);
    # conservative crews add an insurance stop
    if profile.aggression > 0.65 and rng.random() < (profile.aggression - 0.65) / 0.35:
        n_stops = max(0, n_stops - 1)
    elif profile.aggression < 0.35 and rng.random() < (0.35 - profile.aggression) / 0.35:
        n_stops += 1

    # Gamble buffer: laps past the deadline before panic-pitting
    # Quality CCs know exactly where the window ends; poor ones guess and stretch
    gamble = profile.aggression * 8.0 * (0.5 + profile.cc_rating * 0.5)
    gamble *= (0.5 + profile.reliability * 0.5)      # fragile = don't risk it
    gamble = max(0.0, gamble + rng.gauss(0, (1.0 - profile.cc_rating) * 2.5))

    return n_stops, gamble


def fuel_laps_estimate(actual: float, cc_rating: float, rng: random.Random) -> float:
    """Apply crew chief estimation error to a fuel reading.

    A poor cc_rating adds proportional Gaussian error to the crew's read of
    how many laps of fuel remain.  A 0.3-rated chief may think they have
    15 laps when they have 12; a 0.9-rated chief is essentially exact.
    Used by the engine to bias pit-window timing calls.
    """
    if cc_rating >= 0.95 or actual <= 0:
        return actual
    scale = (1.0 - cc_rating) * 0.12        # max ~12 % error at rating 0
    error = rng.gauss(0, scale)
    return max(0.0, actual * (1.0 + error))


# ---------------------------------------------------------------------------
# Internal helpers

def _norm(v: float) -> float:
    """Normalise a ~0-2 NR2003 param to 0-1."""
    return _clamp(v / 2.0)


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if v < lo else hi if v > hi else v


def _aggression_from_name(name: str) -> float:
    """Derive aggression 0-1 from the driver name via behavior.temperament()."""
    if not name:
        return 0.5
    try:
        from .behavior import temperament
        return _clamp(temperament(name))
    except Exception:
        return 0.5


def _track_specialty(profile: CrewChiefProfile, track_type: str) -> float:
    """Return the driver's specialty rating (0-1) for this track type."""
    return {
        "short":        profile.short_track,
        "superspeedway": profile.superspeedway,
        "road":         profile.road_course,
    }.get(track_type, profile.speedway)


def _raw_scores(track_type: str, tire_age: float, to_go: float, fuel_ok: bool,
                me_rank: int, field_size: int, profile: CrewChiefProfile,
                rng: random.Random) -> dict:
    """Score the three options.

    The key insight is that all three options share the same base (50) so
    scores are comparable.  TWO_TIRE sits between the other two:

      STAY_OUT: full position + tire quality decays as tires wear
      FOUR_TIRE: full fresh tires + pays position cost
      TWO_TIRE: full position credit + partial tire refresh + small position cost

    TWO_TIRE wins when BOTH tire quality AND track-position value are high —
    i.e. tires are significantly worn AND it's a short track.  At fresh tires
    it loses to STAY_OUT; at superspeedways it loses to FOUR_TIRE.
    """
    pos_val = _POS_VAL.get(track_type, 10)
    pos_cost = _POS_COST.get(track_type, 8)
    laps_factor = to_go / (to_go + 30.0) if to_go > 0 else 0.5
    rank_frac = _clamp(me_rank / max(10, field_size))

    # STAY OUT
    # The tire term uses (1 - 1.7*tire_age)*30 so it goes negative at ~59% wear,
    # creating a natural crossover where staying on worn tires starts to hurt.
    stay = 50.0
    stay += (1.0 - 1.7 * tire_age) * 30.0  # positive when fresh, negative when worn
    stay += pos_val                           # track position value by type
    stay -= rank_frac * 8.0                   # deep in field: position worth less
    if not fuel_ok:
        stay -= 45.0                          # can't make it: effectively forced pit

    # FOUR TIRE
    four = 50.0
    four += tire_age * 30.0                   # old tires = big benefit from fresh set
    four += laps_factor * 20.0               # more laps left = more value in fresh rubber
    four -= pos_cost                          # costs track position

    # TWO TIRE — the compromise call
    # Gets the same position credit as STAY_OUT (you hold most of your spot)
    # plus a partial tire benefit that scales with how worn the tires are.
    # Pays a small position cost (slower stop / one side of the car restarts fresh).
    two = 50.0
    two += pos_val                            # full position credit: you hold your spot
    two += tire_age * 20.0                    # more worn = more gain from any refresh
    two -= pos_cost * 0.4                     # partial position cost (less than 4-tire)
    two -= laps_factor * 10.0                # long runs favour the full 4-tire benefit

    # CC rating noise: poor crew chiefs score options less accurately.
    # Track specialists get a tighter read on their home surface.
    specialty = _track_specialty(profile, track_type)
    noise_scale = (1.0 - profile.cc_rating) * 15.0 * (1.0 - specialty * 0.35)
    stay += rng.gauss(0, noise_scale)
    four += rng.gauss(0, noise_scale)
    two  += rng.gauss(0, noise_scale)

    # Aggression bias
    a = profile.aggression
    stay += a * 15.0                          # bold crew: lean stay out / gamble
    two  += a * 18.0                          # and love a 2-tire track-position call
    four -= a * 10.0                          # slightly less likely to pit for 4

    # Reliability: fragile car benefits from a caution pit (inspection, check-over).
    # Low reliability nudges toward pitting; has no effect on a bulletproof car.
    rel = profile.reliability
    stay -= (1.0 - rel) * 8.0
    four += (1.0 - rel) * 5.0

    return {STAY_OUT: stay, FOUR_TIRE: four, TWO_TIRE: two}


def _offset_from_teammate(scores: dict, teammate_decision: bool | None,
                           influence: float, track_type: str = "intermediate") -> dict:
    """Nudge scores based on the teammate's decision.

    Superspeedway: FOLLOW the teammate (drafting pact — pit on the same lap).
    Everywhere else: OPPOSE the teammate (different cycles — one of you gets lucky).
    Magnitude scales with influence (0 = no nudge, 1 = strong nudge).
    """
    if teammate_decision is None or influence <= 0.0:
        return scores
    s = dict(scores)
    if track_type == "superspeedway":
        # Follow: match what the teammate chose
        if teammate_decision:   # teammate pitting → come in with them
            s[FOUR_TIRE] += influence * 20.0
            s[TWO_TIRE]  += influence * 5.0
            s[STAY_OUT]  -= influence * 18.0
        else:                   # teammate staying out → stay with them
            s[STAY_OUT]  += influence * 20.0
            s[FOUR_TIRE] -= influence * 15.0
            s[TWO_TIRE]  -= influence * 10.0
    elif not teammate_decision: # teammate staying out → offset: lean pit
        s[FOUR_TIRE] += influence * 18.0
        s[TWO_TIRE]  += influence * 10.0
        s[STAY_OUT]  -= influence * 12.0
    else:                       # teammate pitting → offset: lean stay or 2 tires
        s[STAY_OUT]  += influence * 15.0
        s[TWO_TIRE]  += influence * 8.0
        s[FOUR_TIRE] -= influence * 12.0
    return s


def _pick(scores: dict, rng: random.Random) -> str:
    """Deterministic when the gap is large; probabilistic for close calls."""
    ranked = sorted(scores, key=scores.get, reverse=True)
    best, second = ranked[0], ranked[1]
    gap = scores[best] - scores[second]
    if gap >= 8.0:
        return best
    # Weighted sample between the top two (the third is unlikely enough to ignore)
    top_two = [(best, max(0.0, scores[best])), (second, max(0.0, scores[second]))]
    total = sum(v for _, v in top_two)
    if total <= 0:
        return best
    r = rng.random() * total
    for name, val in top_two:
        r -= val
        if r <= 0:
            return name
    return best
