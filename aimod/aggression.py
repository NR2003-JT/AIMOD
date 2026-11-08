"""Racing intensity and rivals: how hard each AI driver races the cars around him, as one number per car - his
working aggression (car + 0x16F6, see tools/aggression_probe.py).

Pure logic: no memory access, no GUI. The writer is behavior_mem.AggressionWriter; the per-tick engine that picks
who races whom lives with the driver behavior (behavior.py).

The lever (static disassembly of NR2003.exe 2026-11-03, location and hold live-confirmed): NR2003 reads the working
value only in its car-next-to-car code. With A = aggression x 0.5 the side-by-side room a driver insists on is
3.4035 - 1.25 A, 3.9035 - 1.5 A, 5.4035 - 2.25 A (a car is ~1.9035 wide): more aggression = less room = closer
racing. The game rolls it once at the session start (mean + a draw from the deviation) and never again in the race,
so AIMOD sets it when it should change and puts the rolled value back when it is done.

Everything is relative to the driver's own rolled value b, so each driver keeps his own character:
    target = b + s x (raw - b)                                        s = strength / 100 (0 = the game's value)
    raw    = (b x phase + battle) x damage x team x lapped            (rivals: at least b + RIVAL_ADD x s)
clamped to [min(FLOOR, b), max(SAFE_MAX, b)] - rival pairs up to RIVAL_CAP.

LIVE 2026-11-05 (tools/aggression_probe.py, Darlington, Nextel Cup 2007, field 0.78-1.40, 9 runs, 6 pairs): up to
2.4 a car races much like with its own value (side gap ~3.0 m when alongside); at 2.7-3.0 pairs run clearly closer
(gap 2.2-2.9 m, 19-73 % of the time under 2.4 m against 0 % at the game value) but did NOT wreck: one light touch in
~15 boosted minutes, no cautions. Every write held and was put back. So RIVAL_CAP = 3.0 and the battle bumps are
bigger than the first guesses (small changes below ~2 do little).
NOT KNOWN YET: whether a low value makes a driver lift (the probe found no clean-air reference in pack racing), short
tracks (Bristol) and superspeedway packs, other re-roll triggers. The governor stays on for that.
"""
from __future__ import annotations

import math
import re
import zlib
from dataclasses import dataclass, field

from .carno import CarNo

CAR_WIDTH = 1.9035
SAFE_MAX = 2.40          # the tightest side-by-side threshold is a car width here: nobody else goes past it
RIVAL_CAP = 3.00         # rival pairs only: close side by side, no wrecks at Darlington (live 2026-11-05)
RETALIATE_CAP = 4.00     # retaliation only (wrecked beyond repair by him): "drives through him". UNTESTED above 3.0
FLOOR = 0.50             # lowest value written (a driver rolled lower keeps his own)
HARD_MAX = RETALIATE_CAP  # the writer never writes more, whatever it is asked

# race phase (multiplies b)
EARLY = 0.90             # early in a stage: a little more room (mild: "low = lifts" is not proven)
LATE = 1.20              # stage end window
FINAL = 1.30             # the last laps of the race
FINAL_LAPS = 3
NOSTAGE_LATE = 1.15      # races without stages: the build over the whole race ...
NOSTAGE_FINAL = 1.25     # ... and its last 10 %
NOSTAGE_FINAL_PCT = 0.10

# who to race (added, before strength)
BATTLE_ADD = 0.60        # racing a car on the same lap, any position (live: below ~2 small changes do little)
LEAD_ADD = 0.90          # racing for the lead
BATTLE_ENTER, BATTLE_LEAVE = 0.5, 1.0      # s: gap windows with hysteresis (Michigan 11-05: 0.9 flapped)
RIVAL_ENTER, RIVAL_LEAVE = 0.8, 1.5
RIVAL_ADD = 2.0          # top rival level above b at strength 100 (b 1.1, strength 70 -> 2.5; strength 100 -> cap)
RIVAL_ADDS = (0.0, 1.4, 1.7, 2.0)          # by feud level 1 (grudge / list) .. 3 (run-ins pile up): escalation
RETALIATE_ADD = 3.0      # b 1.1: strength 70 -> 3.2, strength 100 -> the cap
MIN_HOLD_LAPS = 3        # a battle / rival switch holds at least this many laps (2 flapped at Michigan)

# multipliers
# damage: with NR2003 damage on moderate most of it is cosmetic (user 2026-11-05) - only from the game's must-repair
# level on does a driver protect the car
DAMAGE_SOFT, DAMAGE_SOFT_K = 0.50, 0.90    # damage (the game's sum) -> protect the car a little
DAMAGE_HARD, DAMAGE_HARD_K = 0.80, 0.75    # ... and from here no battles, no rivals (retaliation still)
TEAM_HOLD_K = 0.95       # right behind his teammate: doesn't attack him
TEAM_SAVE_K = 0.85       # out of contention, racing his teammate who is in it: gives him room
LAPPED_K = 0.85          # about to be lapped: gives the leaders room
OUTSIDE_TOP, OUTSIDE_K = 10, 0.90          # running outside the top 10: a bit calmer (feuds still apply)

DEADBAND = 0.05          # changes smaller than this are not written

# run-ins (rival source: contact in the race). Any touch counts as a run-in (separate ones escalate); only the
# size of a wreck matters for retaliation
GRUDGE_DAMAGE = 0.001    # damage rise (the game's sum) that counts as contact (a light rub ~0.002, probe 11-05)
RUNIN_MERGE_S = 8.0      # game s: contacts of the same two cars this close together are one run-in
GRUDGE_TEMPER = 0.30     # drivers below this temperament percentile never hold a grudge / retaliate
GRUDGE_BASE_P, GRUDGE_STRENGTH_P = 0.45, 0.35    # chance a one-run-in grudge goes at it on an encounter
GRUDGE_TEMPER_P = 0.25   # ... + temperament x this (2+ run-ins: always)
MAX_LEVEL = 3
WRECK_DAMAGE = 0.50      # damage taken in one run-in that counts as being wrecked (the game's must-repair level)
BEYOND_REPAIR = 0.30     # damage still on the car after his repair stop (or 2 laps on, without one) -> retaliation
RETALIATE_WAIT_LAPS = 2
AGGRESSOR_ALONG_M = 1.5  # the car behind by more than this hit the one ahead; closer = side contact (less damage
                         # taken = the aggressor)

# safety governor: cautions that follow a run-in of a pair AIMOD set on each other
GOVERNOR_WINDOW_S = 10.0     # game seconds from the run-in to the caution: the pair caused it -> their feud settles
GOVERNOR_CAUTIONS = 3        # this many in a race -> rivals log only (SHADOW) for the rest of it


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * clamp(t, 0.0, 1.0)


# ------------------------------------------------------------------ race phase
@dataclass(frozen=True)
class Phase:
    factor: float
    code: str            # early / mid / late / final
    stage: int           # 1-based stage the lap is in (1 without stages)
    progress: float      # 0..1 through the stage (the race without stages)


def phase(lap: float, total: int, stage_ends=()) -> Phase:
    """Race phase for race lap `lap` of `total`. stage_ends = race laps where stages end (all but the last).
    Stages: patient early in each stage, ramping to LATE through its end (a new stage starts patient again: the
    drop after the break). No stages: patient through half the race, building to the end, NOSTAGE_FINAL for the
    last 10 %. The last FINAL_LAPS laps of the race are FINAL either way."""
    if total <= 0:
        return Phase(1.0, "mid", 1, 0.5)
    to_go = total - lap
    ends = sorted(e for e in stage_ends if 0 < e < total)
    if ends:
        start, stage = 0, 1
        end = total
        for k, e in enumerate(ends):
            if lap <= e:
                end = e
                break
            start, stage = e, k + 2
        p = clamp((lap - start) / max(1.0, end - start), 0.0, 1.0)
        if end == total and to_go <= FINAL_LAPS:
            return Phase(FINAL, "final", stage, p)
        if p < 0.3:
            return Phase(EARLY, "early", stage, p)
        if p < 0.6:
            return Phase(lerp(EARLY, 1.0, (p - 0.3) / 0.3), "mid", stage, p)
        if p < 0.9:
            return Phase(lerp(1.0, LATE, (p - 0.6) / 0.3), "late" if p >= 0.75 else "mid", stage, p)
        return Phase(LATE, "late", stage, p)
    p = clamp(lap / total, 0.0, 1.0)
    if to_go <= FINAL_LAPS:
        return Phase(FINAL, "final", 1, p)
    if p >= 1.0 - NOSTAGE_FINAL_PCT:
        return Phase(NOSTAGE_FINAL, "final", 1, p)
    if p < 0.5:
        return Phase(EARLY, "early", 1, p)
    if p < 0.7:
        return Phase(lerp(EARLY, 1.0, (p - 0.5) / 0.2), "mid", 1, p)
    return Phase(lerp(1.0, NOSTAGE_LATE, (p - 0.7) / 0.2), "late" if p >= 0.8 else "mid", 1, p)


# ------------------------------------------------------------------ windows
def window(was: bool, gap: float | None, enter: float, leave: float) -> bool:
    """Gap window with hysteresis: in once the gap is at most `enter`, out once it is over `leave`."""
    if gap is None or not math.isfinite(gap) or gap < 0:
        return False
    return gap <= (leave if was else enter)


def battle_window(was: bool, gap: float | None) -> bool:
    return window(was, gap, BATTLE_ENTER, BATTLE_LEAVE)


def rival_window(was: bool, gap: float | None) -> bool:
    return window(was, gap, RIVAL_ENTER, RIVAL_LEAVE)


# ------------------------------------------------------------------ terms
def damage_term(damage: float | None) -> tuple[float, str, bool]:
    """(multiplier, reason code, no battles / rivals) for the car's damage."""
    if damage is None or not math.isfinite(damage) or damage < DAMAGE_SOFT:
        return 1.0, "", False
    if damage < DAMAGE_HARD:
        return DAMAGE_SOFT_K, "damage", False
    return DAMAGE_HARD_K, "damage", True


def battle_term(racing: bool, for_lead: bool = False) -> float:
    return (LEAD_ADD if for_lead else BATTLE_ADD) if racing else 0.0


def team_term(behind_teammate: bool, saving_for_teammate: bool) -> tuple[float, str]:
    """behind_teammate: right behind his teammate (in the battle window); saving_for_teammate: he is out of
    contention and racing his teammate who is in it."""
    if saving_for_teammate:
        return TEAM_SAVE_K, "team_save"
    if behind_teammate:
        return TEAM_HOLD_K, "team_hold"
    return 1.0, ""


def lapped_term(being_lapped: bool) -> tuple[float, str]:
    return (LAPPED_K, "lapped") if being_lapped else (1.0, "")


def strength(pct: float) -> float:
    """slider % (0-150) -> s"""
    return clamp(pct, 0.0, 150.0) / 100.0


def target(b: float, phase_k: float = 1.0, battle_add: float = 0.0, rival: bool = False, damage_k: float = 1.0,
           no_bumps: bool = False, team_k: float = 1.0, lapped_k: float = 1.0, s: float = 0.7,
           rival_add: float | None = None, retaliate: bool = False, outside_k: float = 1.0) -> float:
    """The value for a driver whose rolled value is b. s = 0 gives b back exactly (the game's own racing).
    rival / rival_add: racing a rival (rival_add = his feud level's add, RIVAL_ADD by default): at least
    b + rival_add x s, up to RIVAL_CAP. retaliate: going after the car that wrecked him: at least
    b + RETALIATE_ADD x s, up to RETALIATE_CAP, whatever his own damage."""
    if not math.isfinite(b) or b <= 0:
        return b
    raw = b * phase_k + (0.0 if no_bumps else battle_add)
    raw *= damage_k * team_k * lapped_k * outside_k
    v = b + s * (raw - b)
    hi = max(SAFE_MAX, b)
    if retaliate:
        v = max(v, b + RETALIATE_ADD * s)
        hi = max(RETALIATE_CAP, b)
    elif (rival or rival_add) and not no_bumps:
        v = max(v, b + (RIVAL_ADD if rival_add is None else rival_add) * s)
        hi = max(RIVAL_CAP, b)
    return clamp(v, min(FLOOR, b), hi)


def worth_writing(new: float, last: float | None) -> bool:
    """A change big enough to write (the first write and any move back to the rolled value always are: last None)."""
    return last is None or abs(new - last) >= DEADBAND


# ------------------------------------------------------------------ dice
def unit(*key) -> float:
    """A fixed number in [0, 1) for this key: the same race seed, cars and encounter always give the same dice."""
    return zlib.crc32("|".join(str(k) for k in key).encode("utf-8")) / 4294967296.0


def pair_key(a: int, b: int) -> tuple[int, int]:
    a, b = CarNo(int(a)), CarNo(int(b))              # (prints as painted: 2001 -> "01")
    return (a, b) if a <= b else (b, a)


def encounter_dice(seed: int, a: int, b: int, n: int) -> float:
    """dice for the n-th encounter of cars a and b (car numbers) in the race with this seed"""
    lo, hi = pair_key(int(a), int(b))
    return unit(seed, lo, hi, n)


def grudge_holds(temper: float) -> bool:
    """Whether a driver hit by another holds it against him (no dice: his temperament decides)."""
    return temper >= GRUDGE_TEMPER


def grudge_goes(seed: int, holder: int, other: int, n: int, temper: float, s: float) -> bool:
    """A grudge holder meeting the car that hit him: does he go at him this time? (seeded per race, pair, encounter)"""
    p = clamp(GRUDGE_BASE_P + GRUDGE_STRENGTH_P * min(1.0, s) + GRUDGE_TEMPER_P * temper, 0.0, 0.95)
    return encounter_dice(seed, holder, other, n) < p


# ------------------------------------------------------------------ rivals list
_PAIR = re.compile(r"^\s*#?(\d{1,3})\s*(?:[,;/\-\s]|vs\.?)\s*#?(\d{1,3})\s*$", re.IGNORECASE)


def parse_rivals(text: str) -> tuple[set[tuple[str, str]], list[str]]:
    """Rivals list file: one pair of car numbers per line ('24 48', '#3 vs #24', '01,99'); '#'-comments after a
    space or at the start of a line ('# old rivalry'), blank lines skipped. Numbers stay text ('01' != '1', as
    NR2003 numbers them). Returns (pairs as sorted tuples, lines it couldn't read)."""
    pairs, bad = set(), []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s.startswith("# ") or s == "#" or s.startswith("//"):
            continue
        s = re.split(r"\s+#\s|\s//", s)[0].strip()
        m = _PAIR.match(s)
        if not m or m.group(1) == m.group(2):
            bad.append(line)
            continue
        a, b = m.group(1), m.group(2)
        pairs.add((a, b) if a <= b else (b, a))
    return pairs, bad


# ------------------------------------------------------------------ temperament
def temper_from_name(name: str) -> float:
    """The old temperament: fixed per driver from his name (0..0.99)."""
    return (zlib.crc32((name or "").encode("utf-8", "replace")) % 100) / 100.0


def temperaments(values: dict, names: dict) -> dict:
    """0 = patient ... 1 = aggressive for each car key: the percentile of its .car aggression (mean) in this field,
    the name hash for cars without one. Percentiles keep the spread the behavior thresholds expect (about half the
    field on each side of 0.5) on any series' scale. Ties are split by the name hash."""
    known = {k: v for k, v in values.items() if v is not None and math.isfinite(v)}
    out = {k: temper_from_name(names.get(k, "")) for k in values if k not in known}
    if len(known) == 1:
        out.update({k: 0.5 for k in known})
    elif known:
        order = sorted(known, key=lambda k: (known[k], temper_from_name(names.get(k, "")), str(k)))
        n = len(order)
        for r, k in enumerate(order):
            out[k] = round(r / (n - 1) * 0.99, 4)
    return out


# ------------------------------------------------------------------ governor
class Governor:
    """Counts cautions that come out right after a contact involving a car AIMOD made more aggressive. After
    GOVERNOR_CAUTIONS of them in a race, rivals only log (SHADOW) for the rest of it."""

    def __init__(self, limit: int = GOVERNOR_CAUTIONS, window_s: float = GOVERNOR_WINDOW_S):
        self.limit, self.window_s = limit, window_s
        self.contacts: list[tuple[float, tuple]] = []      # (game s, cars) of boosted-car contacts
        self.cautions: list[tuple[float, tuple]] = []      # (game s, the contact's cars) counted
        self.tripped = False

    def contact(self, t: float, cars: tuple, boosted: bool):
        if boosted:
            self.contacts.append((t, tuple(cars)))
            self.contacts = [c for c in self.contacts if t - c[0] <= self.window_s]

    def caution(self, t: float) -> tuple | None:
        """A caution came out at game second t. Returns the cars of the contact it is blamed on (or None)."""
        recent = [c for c in self.contacts if 0 <= t - c[0] <= self.window_s]
        self.contacts = []
        if not recent:
            return None
        cars = recent[-1][1]
        self.cautions.append((t, cars))
        if len(self.cautions) >= self.limit:
            self.tripped = True
        return cars

    def reset(self):
        self.contacts, self.cautions, self.tripped = [], [], False


# ====================================================================== contacts
CONTACT_NEAR_M = 6.0     # a car this close when the damage rises is the one he touched


@dataclass(frozen=True)
class Contact:
    t: float                 # game s
    a: int                   # slot of the car whose damage rose most
    b: int                   # slot of the car he touched
    na: int                  # car numbers
    nb: int
    da: float                # damage taken
    db: float
    aggressor: int | None    # slot of the car that did it (None = side by side, can't tell)
    dist: float


class ContactWatcher:
    """Contact between cars from the snapshot: a car's damage rising (the game's sum) with another car within
    CONTACT_NEAR_M. The car behind (by more than AGGRESSOR_ALONG_M) is the aggressor; side by side, the one that
    took less damage. Damage with no car near (the wall) is not contact. Green flag only. Heavy time acceleration
    only makes "who was next to him" less sure: the damage itself is never missed (it adds up)."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.prev: dict[int, float] = {}

    def update(self, t: float, cars, green: bool) -> list[Contact]:
        live = {c.idx: c for c in cars if getattr(c, "valid", False) and c.pos is not None and c.vel is not None
                and not c.reason_out and not c.on_pit_road}
        dmg = {i: float(c.damage or 0.0) for i, c in live.items()}
        rises = {i: d - self.prev[i] for i, d in dmg.items() if i in self.prev and d - self.prev[i] > GRUDGE_DAMAGE}
        self.prev = dmg
        if not green or not rises:
            return []
        out, seen = [], set()
        for i in sorted(rises, key=lambda k: -rises[k]):
            if i in seen:
                continue
            c = live[i]
            near = None
            for j, o in live.items():
                if j == i:
                    continue
                d = math.hypot(o.pos[0] - c.pos[0], o.pos[1] - c.pos[1])
                if d <= CONTACT_NEAR_M and (near is None or d < near[0]):
                    near = (d, o)
            if near is None:
                continue                                    # the wall (or nobody we can see)
            d, o = near
            seen.update((i, o.idx))
            ro = rises.get(o.idx, 0.0)
            out.append(Contact(t, i, o.idx, CarNo(int(c.number)), CarNo(int(o.number)), rises[i], ro,
                               self._aggressor(c, o, rises[i], ro), d))
        return out

    @staticmethod
    def _aggressor(c, o, rc: float, ro: float) -> int | None:
        sp = math.hypot(*c.vel)
        if sp > 5.0:
            hx, hy = c.vel[0] / sp, c.vel[1] / sp
            along = (o.pos[0] - c.pos[0]) * hx + (o.pos[1] - c.pos[1]) * hy      # + = o is ahead of c
            if along > AGGRESSOR_ALONG_M:
                return c.idx                                # c ran into the back of o
            if along < -AGGRESSOR_ALONG_M:
                return o.idx
        if rc > ro * 1.25:
            return o.idx                                    # side by side: who came out of it better did it
        if ro > rc * 1.25:
            return c.idx
        return None


# ====================================================================== rivals
GRUDGE, FEUD, LISTED, RETALIATE = "grudge", "feud", "rival", "retaliate"


@dataclass
class Feud:
    a: int                       # car numbers, a < b
    b: int
    listed: bool = False         # in the rivals list file: rivals from the start
    runins: int = 0              # separate run-ins this race (since the last settle)
    last_t: float = -1e9
    runin_dmg: dict = field(default_factory=dict)     # number -> damage taken in the current run-in
    victim: dict = field(default_factory=dict)        # number -> run-ins he came out of as the victim
    holder: int | None = None    # one run-in: the victim holds a grudge (his temperament allowing)
    retaliate: int | None = None  # he was wrecked beyond repair by the other: drives through him
    pending: tuple | None = None  # (holder, race lap, damage after the wreck): waiting for his repair stop
    settled: int = 0

    @property
    def level(self) -> int:
        return min(MAX_LEVEL, self.runins + (1 if self.listed else 0))

    def other(self, n: int) -> int:
        return self.b if n == self.a else self.a


@dataclass(frozen=True)
class RivalEvent:
    kind: str            # grudge / feud / wrecked / retaliate / settled / governor / runin
    holder: int          # car number it is about (the grudge holder, the wrecked car, a member of the pair)
    other: int
    level: int
    t: float
    lap: int
    detail: str = ""


class RivalBook:
    """Who has it in for whom this race. Sources (user 2026-11-05): run-ins in the race (any touch; separate run-ins
    escalate the pair: 1 = the victim's grudge, his temperament allowing; 2 = a feud both ways; 3 = the top level),
    the rivals list file (rivals from the start, escalating the same way), teammates allowed. A run-in that wrecks a
    car (WRECK_DAMAGE) beyond what his repair stop fixes (BEYOND_REPAIR) makes him retaliate. A caution that
    follows a run-in of the pair settles it (back to nothing, or the list's level 1). The governor: after
    GOVERNOR_CAUTIONS such cautions in a race rivals only log. Pure: fed contacts, cautions and car states."""

    def __init__(self, seed: int = 0):
        self.pairs: set[tuple[int, int]] = set()
        self.teammates = True
        self.player: int | None = None       # the player's car number when player rivals are on
        self.player_slot = 0
        self.grudges = True
        self.retaliation = True
        self.teams: dict[int, int] = {}      # car number -> team id (-1 none)
        self.reset(seed)

    def reset(self, seed: int = 0):
        self.seed = seed
        self.feuds: dict[tuple[int, int], Feud] = {}
        self.gov = Governor()
        self._gov_said = False
        for a, b in self.pairs:
            self.feuds[pair_key(a, b)] = Feud(*pair_key(a, b), listed=True)

    def set_pairs(self, pairs):
        """The rivals list (car numbers). Pairs already in the book keep their run-ins."""
        self.pairs = {pair_key(CarNo(int(a)), CarNo(int(b))) for a, b in pairs if int(a) != int(b)}
        for k, f in self.feuds.items():
            f.listed = k in self.pairs
        for k in self.pairs:
            self.feuds.setdefault(k, Feud(*k, listed=True))

    @property
    def tripped(self) -> bool:
        return self.gov.tripped

    def _teammates(self, a: int, b: int) -> bool:
        ta, tb = self.teams.get(a, -1), self.teams.get(b, -1)
        return ta >= 0 and ta == tb

    def _allowed(self, c: Contact) -> bool:
        for slot, n in ((c.a, c.na), (c.b, c.nb)):
            if slot == self.player_slot and (self.player is None or n != self.player):
                return False                 # the player's car: only with player rivals on
        if not self.teammates and self._teammates(c.na, c.nb) and pair_key(c.na, c.nb) not in self.pairs:
            return False
        return True

    def runin(self, c: Contact, lap: int, temper: dict, boosted: bool = False) -> list[RivalEvent]:
        """A contact between two cars. boosted: either car was raced harder than his own value by AIMOD then."""
        ev: list[RivalEvent] = []
        if not self._allowed(c):
            return ev
        k = pair_key(c.na, c.nb)
        f = self.feuds.setdefault(k, Feud(*k))
        aggr = c.na if c.aggressor == c.a else c.nb if c.aggressor == c.b else None
        new = c.t - f.last_t > RUNIN_MERGE_S
        f.last_t = c.t
        if new:
            f.runins += 1
            f.runin_dmg = {}
        f.runin_dmg[c.na] = f.runin_dmg.get(c.na, 0.0) + c.da
        f.runin_dmg[c.nb] = f.runin_dmg.get(c.nb, 0.0) + c.db
        if aggr is not None:
            victim = f.other(aggr)
        else:
            victim = c.na if f.runin_dmg[c.na] >= f.runin_dmg[c.nb] else c.nb
        self.gov.contact(c.t, k, boosted or f.level >= 1 or f.retaliate is not None)
        if new:
            f.victim[victim] = f.victim.get(victim, 0) + 1
            ev.append(RivalEvent("runin", victim, f.other(victim), f.level, c.t, lap,
                                 f"run-in {f.runins}, " + (f"#{aggr} did it" if aggr is not None else "side by side")))
            if f.level == 1 and not f.listed:
                if self.grudges and grudge_holds(temper.get(victim, 0.5)):
                    f.holder = victim
                    ev.append(RivalEvent(GRUDGE, victim, f.other(victim), 1, c.t, lap))
            elif f.level >= 2:
                f.holder = None
                ev.append(RivalEvent(FEUD, victim, f.other(victim), f.level, c.t, lap,
                                     f"{f.victim.get(victim, 0)}x the victim"))
        # a wreck: the victim retaliates if his repair stop can't fix it
        if (self.retaliation and f.runin_dmg.get(victim, 0.0) >= WRECK_DAMAGE and f.retaliate != victim
                and (f.pending is None or f.pending[0] != victim) and grudge_holds(temper.get(victim, 0.5))):
            f.pending = (victim, lap, None)
            ev.append(RivalEvent("wrecked", victim, f.other(victim), f.level, c.t, lap,
                                 f"took {f.runin_dmg[victim]:.2f} damage"))
        return ev

    def caution(self, t: float, lap: int) -> list[RivalEvent]:
        """A caution came out: the pair whose run-in (just before) caused it settles."""
        ev: list[RivalEvent] = []
        k = self.gov.caution(t)
        if k is None:
            return ev
        f = self.feuds.get(k)
        if f is not None:
            was = f.level
            f.runins, f.holder, f.retaliate, f.pending = 0, None, None, None
            f.victim, f.runin_dmg = {}, {}
            f.settled += 1
            ev.append(RivalEvent("settled", f.a, f.b, was, t, lap, "their run-in brought out the caution"))
        if self.gov.tripped and not self._gov_said:
            self._gov_said = True
            ev.append(RivalEvent("governor", k[0], k[1], 0, t, lap,
                                 f"{len(self.gov.cautions)} cautions after rival run-ins: rivals log only now"))
        return ev

    def update(self, lap: int, cars: dict) -> list[RivalEvent]:
        """cars: number -> (damage, on track). A wrecked car's pending retaliation is decided after his repair
        stop (the damage drops) or RETALIATE_WAIT_LAPS laps on without one: still BEYOND_REPAIR -> retaliate."""
        ev = []
        for f in self.feuds.values():
            if f.pending is None:
                continue
            holder, wl, seen = f.pending
            st = cars.get(holder)
            if st is None:
                continue
            dmg, on_track = st
            if seen is None or dmg > seen:
                f.pending = (holder, wl, dmg)                # the most damage he got from it
                seen = dmg
            repaired = dmg < seen - 0.05
            if not on_track or not (repaired or lap >= wl + RETALIATE_WAIT_LAPS):
                continue
            f.pending = None
            if dmg >= BEYOND_REPAIR:
                f.retaliate = holder
                ev.append(RivalEvent(RETALIATE, holder, f.other(holder), MAX_LEVEL, 0.0, lap,
                                     f"damage {dmg:.2f} left after " + ("his repair" if repaired else "the wreck")))
        return ev

    def relation(self, me: int, other: int) -> tuple[str, float] | None:
        """(kind, add) of what car `me` has against car `other`, or None."""
        f = self.feuds.get(pair_key(me, other))
        if f is None:
            return None
        if f.retaliate == me:
            return RETALIATE, RETALIATE_ADD
        lvl = f.level
        if lvl >= 2:
            return FEUD, RIVAL_ADDS[lvl]
        if lvl == 1:
            if f.listed:
                return LISTED, RIVAL_ADDS[1]
            if f.holder == me:
                return GRUDGE, RIVAL_ADDS[1]
        return None

    def against(self, me: int) -> list[int]:
        """the car numbers `me` has something against"""
        out = []
        for (a, b) in self.feuds:
            if me in (a, b):
                o = b if me == a else a
                if self.relation(me, o) is not None:
                    out.append(o)
        return out

    def goes(self, me: int, other: int, n: int, temper: float, s: float) -> bool:
        """Does he go at him on their n-th encounter? Only a one-run-in grudge rolls the (seeded) dice."""
        rel = self.relation(me, other)
        if rel is None:
            return False
        if rel[0] == GRUDGE:
            return grudge_goes(self.seed, me, other, n, temper, s)
        return True

    def repeat_victim(self, me: int) -> bool:
        """He came out of 2+ run-ins with the same car as the victim."""
        return any(f.victim.get(me, 0) >= 2 for f in self.feuds.values() if me in (f.a, f.b))


# ====================================================================== the engine
OFF, SHADOW, ON = "off", "shadow", "on"
MODES = (OFF, SHADOW, ON)
CONTENTION_PLACES = 10   # a car this many places behind his teammate (or a lap down on him) is out of contention
LAPPED_GAP = 1.0         # s: a car this close behind on track and a lap up is lapping him
TEAM_NEAR = 1.0          # s: a teammate this close on track


@dataclass
class IntensityConfig:
    mode: str = OFF          # off / shadow (decide and log, write nothing) / on
    strength: int = 70       # % (0 = the game's own values, 150 max)
    teams: bool = True       # teammates don't race each other this way


@dataclass
class CarIntensity:
    target: float | None = None      # value wanted (None = his own)
    reasons: str = ""
    decided_lap: int = -1
    battle: int | None = None        # car number he is racing
    battle_since: int = 0            # car lap the battle started
    near: dict = field(default_factory=dict)        # rival number -> in his rival window (hysteresis)
    encounters: dict = field(default_factory=dict)  # rival number -> times they came together
    going: dict = field(default_factory=dict)       # rival number -> goes at him this encounter
    rival: tuple | None = None       # (kind, number) he is going at now


@dataclass(frozen=True)
class IntensityChange:
    idx: int
    number: int
    name: str
    lap: int
    base: float          # his own (rolled) value
    old: float           # value before (his own when None)
    new: float
    reasons: str
    gap: float | None = None


@dataclass
class IntensityResult:
    targets: dict = field(default_factory=dict)      # idx -> value (cars at their own value left out)
    reasons: dict = field(default_factory=dict)      # idx -> reason codes
    changes: list = field(default_factory=list)      # IntensityChange


class IntensityEngine:
    """Per car, once a lap at his line crossing: how hard he races the cars around him (see target()).
    Deterministic: the same race gives the same values. Off the track (pit road, damage stop) and under caution a
    car is back at his own value at once. Inputs are behavior.Ctx / behavior.Car (duck typed: no import cycle) and
    `own`: idx -> the car's rolled value (from the writer)."""

    def __init__(self, cfg: IntensityConfig | None = None):
        self.cfg = cfg or IntensityConfig()
        self.reset()

    def reset(self):
        self.state: dict[int, CarIntensity] = {}

    @property
    def active_mode(self) -> bool:
        return self.cfg.mode in (SHADOW, ON)

    def tick(self, ctx, cars, own: dict, rivals: "RivalBook | None" = None, extra=(),
             temper: dict | None = None) -> IntensityResult:
        """rivals: the race's RivalBook (None = no rivals); extra: cars that count for rivals but never get a value
        (the player's car with player rivals on); temper: car number -> temperament (grudge dice)."""
        res = IntensityResult()
        live = self.active_mode and ctx.race and ctx.green
        present = {c.idx for c in cars}
        if not live:
            why = "caution" if ctx.race and self.active_mode else OFF
            for c in cars:
                st = self.state.get(c.idx)
                if st is not None:
                    self._set(st, c, None, why, own.get(c.idx), res, None)
            for idx in list(self.state):
                if idx not in present:
                    del self.state[idx]
            return res
        s = strength(self.cfg.strength)
        order = sorted(cars, key=lambda c: -c.progress)
        pos = {c.idx: n for n, c in enumerate(order)}
        lt = ctx.lap_time if ctx.lap_time and ctx.lap_time > 1 else 30.0
        ph = phase(ctx.lap_now, ctx.total, getattr(ctx, "stage_ends", ()) or ())
        late = ph.code in ("late", "final")
        for c in order:
            b = own.get(c.idx)
            st = self.state.setdefault(c.idx, CarIntensity())
            if b is None or not math.isfinite(b) or b <= 0:
                continue
            if not c.on_track:
                self._set(st, c, None, "pit", b, res, None)
                st.battle, st.decided_lap = None, -1
                continue
            if st.decided_lap == c.lap:
                self._report(st, c, res)
                continue
            st.decided_lap = c.lap
            n = pos[c.idx]
            ahead = order[n - 1] if n > 0 else None
            behind = order[n + 1] if n + 1 < len(order) else None
            gap_a = (ahead.progress - c.progress) * lt if ahead else None
            gap_b = (c.progress - behind.progress) * lt if behind else None
            reasons = [ph.code]
            dk, dcode, no_bumps = damage_term(getattr(c, "damage", 0.0))

            # who he races: the closer of the cars right ahead / behind on his lap (not a teammate)
            cand = [(g, o) for g, o in ((gap_a, ahead), (gap_b, behind))
                    if o is not None and g is not None and self._racing(c, o)]
            partner = min(cand, key=lambda x: x[0]) if cand else None
            was = st.battle is not None
            racing = partner is not None and battle_window(was, partner[0])
            if (not racing and was and c.lap - st.battle_since < MIN_HOLD_LAPS
                    and any(o.number == st.battle for _, o in cand)):
                racing = True                                   # a battle holds at least MIN_HOLD_LAPS laps
                partner = next(x for x in cand if x[1].number == st.battle)
            if racing and ctx.drafting and not late:
                racing = False                                  # packs: only at the stage end / the finish
            if no_bumps:
                racing = False                                  # a badly damaged car protects the car
            if racing:
                if st.battle != partner[1].number:
                    st.battle, st.battle_since = partner[1].number, c.lap
                lead = c.laps_down == 0 and partner[1].laps_down == 0 and n <= 1 and pos[partner[1].idx] <= 1
                reasons.append(f"{'lead_battle' if lead else 'battle'}:#{partner[1].number}")
                add = battle_term(True, lead)
            else:
                st.battle = None
                add = 0.0

            if dcode:
                reasons.append(dcode)
            tk, tcode = team_term(*self._team(c, ahead, gap_a, order, pos, lt))
            if tcode:
                reasons.append(tcode)
            lk, lcode = lapped_term(self._being_lapped(c, order, lt))
            if lcode:
                reasons.append(lcode)
            rel = self._rival(c, st, rivals, list(order) + list(extra), lt, late, ctx.drafting, s, temper or {})
            rival_add, retaliate = None, False
            if rel is not None:
                kind, other, radd, gap = rel
                tag = f"{kind}:#{other}" + (f" L{rivals.feuds[pair_key(c.number, other)].level}"
                                            if kind in (FEUD, LISTED) else "")
                if rivals.tripped:
                    reasons.append(tag + "(governor)")          # the governor: rivals only log now
                elif kind == RETALIATE:
                    retaliate = True
                    reasons.append(tag)
                elif not no_bumps:
                    rival_add = radd
                    reasons.append(tag)
            calm = n >= OUTSIDE_TOP and not (rivals is not None and rivals.repeat_victim(c.number))
            if calm:
                reasons.append("outside_top10")
            t = target(b, ph.factor, add, False, dk, no_bumps, tk, lk, s, rival_add=rival_add, retaliate=retaliate,
                       outside_k=OUTSIDE_K if calm else 1.0)
            prev = st.target
            if prev is not None and abs(t - prev) < DEADBAND:
                t = prev
            elif prev is None and abs(t - b) < DEADBAND:
                t = None
            self._set(st, c, t, " ".join(reasons), b, res, partner[0] if racing else None)
        for idx in list(self.state):
            if idx not in present:
                del self.state[idx]
        return res

    # -------------------------------------------------------------- helpers
    _RANK = {RETALIATE: 3, FEUD: 2, LISTED: 1, GRUDGE: 0}

    def _rival(self, c, st: CarIntensity, rivals, field_cars, lt, late, drafting, s, temper):
        """(kind, other number, add, gap) of the strongest rival he is going at now, or None. A rival counts within
        RIVAL_ENTER s on track (any lap; out at RIVAL_LEAVE); a one-run-in grudge rolls the seeded dice once per
        encounter. Drafting tracks: only at the stage end / the finish."""
        st.rival = None
        if rivals is None:
            return None
        foes = rivals.against(c.number)
        if not foes:
            st.near.clear()
            return None
        by_num = {o.number: o for o in field_cars}
        best = None
        for other in foes:
            o = by_num.get(other)
            if o is None or not getattr(o, "on_track", True):
                st.near[other] = False
                continue
            g = _track_gap(c, o, lt)
            was = st.near.get(other, False)
            now = rival_window(was, g)
            st.near[other] = now
            if now and not was:
                n = st.encounters.get(other, 0) + 1
                st.encounters[other] = n
                st.going[other] = rivals.goes(c.number, other, n, temper.get(c.number, 0.5), s)
            if not now or not st.going.get(other, False):
                continue
            rel = rivals.relation(c.number, other)
            if rel is None:
                continue
            if drafting and not late:
                continue                                       # packs: even retaliation waits for the end
            cand = (self._RANK[rel[0]], rel[1], -g, rel[0], other, g)
            if best is None or cand > best:
                best = cand
        if best is None:
            return None
        st.rival = (best[3], best[4])
        return best[3], best[4], best[1], best[5]

    def _teammates(self, a, b) -> bool:
        return self.cfg.teams and a.team >= 0 and a.team == b.team

    def _racing(self, a, b) -> bool:
        return a.laps_down == b.laps_down and not self._teammates(a, b)

    def _team(self, c, ahead, gap_a, order, pos, lt) -> tuple[bool, bool]:
        """(right behind his teammate, out of contention and racing a teammate who is in it)"""
        if not self.cfg.teams or c.team < 0:
            return False, False
        behind_tm = (ahead is not None and gap_a is not None and gap_a <= BATTLE_ENTER and self._teammates(c, ahead))
        saving = False
        for o in order:
            if o is c or not self._teammates(c, o):
                continue
            near = _track_gap(c, o, lt) <= TEAM_NEAR
            out = c.laps_down > o.laps_down or pos[c.idx] - pos[o.idx] >= CONTENTION_PLACES
            if near and out:
                saving = True
                break
        return behind_tm, saving

    @staticmethod
    def _being_lapped(c, order, lt) -> bool:
        """A car a lap (or more) up is right behind him on track."""
        for o in order:
            if o is c or o.progress - c.progress < 0.5:
                continue
            behind_on_track = ((c.progress - o.progress) % 1.0) * lt
            if behind_on_track <= LAPPED_GAP:
                return True
        return False

    def _report(self, st: CarIntensity, c, res: IntensityResult):
        if st.target is not None:
            res.targets[c.idx] = st.target
            res.reasons[c.idx] = st.reasons

    def _set(self, st: CarIntensity, c, t, reasons: str, b, res: IntensityResult, gap):
        old = st.target
        st.target, st.reasons = t, reasons
        self._report(st, c, res)
        if (old is None) != (t is None) or (old is not None and t is not None and abs(old - t) > 1e-9):
            base = b if b is not None else (old if old is not None else 0.0)
            res.changes.append(IntensityChange(c.idx, c.number, c.name, c.lap, base,
                                               old if old is not None else base,
                                               t if t is not None else base, reasons, gap))


ONSCREEN_M = 30.0        # the rival card shows only for a rival this close (about 6 car lengths) to the TV car ...


def close_enough_for_screen(me, other, cars) -> bool:
    """... with no other car between them (Michigan 11-05: the card showed rivals with up to 2 cars in between).
    me / other / cars: snapshot cars with .pos (x, y); a car is between when it is nearer to both than they are to
    each other."""
    if me.pos is None or other.pos is None:
        return False
    d = math.hypot(me.pos[0] - other.pos[0], me.pos[1] - other.pos[1])
    if d > ONSCREEN_M:
        return False
    for o in cars:
        if o is me or o is other or not getattr(o, "valid", False) or o.pos is None:
            continue
        if (math.hypot(o.pos[0] - me.pos[0], o.pos[1] - me.pos[1]) < d
                and math.hypot(o.pos[0] - other.pos[0], o.pos[1] - other.pos[1]) < d):
            return False
    return True


def _track_gap(a, b, lt: float) -> float:
    """seconds between two cars on track (either way round, whatever lap they are on)"""
    d = (a.progress - b.progress) % 1.0
    return min(d, 1.0 - d) * lt
