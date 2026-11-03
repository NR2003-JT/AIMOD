"""Whole-race scenarios by track type: python tools/sim_scenarios.py [all|ch|chns|mart|road|ss|chvar|rand]"""
import os, sys, collections, statistics, re
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_root, "tests")); sys.path.insert(0, _root)
from racesim import RaceSim, Track
from aimod.strategy import StrategyParams, SHORT_TRACK, ROAD_COURSE, SUPERSPEEDWAY, INTERMEDIATE

def report(name, sim, marks=()):
    gs = collections.Counter(c.green_stops for c in sim.cars)
    ff = sim.finish_fuel_laps()
    print(f"== {name}: green stops/car {dict(sorted(gs.items()))}  caution stops {dict(collections.Counter(c.caution_stops for c in sim.cars))}"
          f"  out of fuel {sum(c.out_of_fuel for c in sim.cars)}  finish fuel med {statistics.median(ff):.1f} [{min(ff):.1f}..{max(ff):.1f}]")
    for lap, d in sim.marks.items():
        print(f"   laps down at lap {lap}: {dict(sorted(collections.Counter(d.values()).items()))}")
    print("   final laps down:", dict(sorted(collections.Counter(sim.laps_down().values()).items())))
    notes = collections.Counter(re.sub(r"[0-9.]+", "#", (e.note).split(";")[0])[:60] for e in sim.events if e.green)
    print("   green stop reasons:", notes.most_common(6))
    gl = sorted(l for c in sim.cars for l, g, code in c.stops if g)
    print("   green stop laps:", gl[::max(1, len(gl)//40)])

def run(name, track, laps, marks=(), **kw):
    sim = RaceSim(track, laps, **kw)
    sim.marks = {}
    def on_lap(s, lap):
        if lap in marks:
            s.marks[lap] = s.laps_down()
    sim.run(on_lap=on_lap)
    report(name, sim)
    return sim

which = sys.argv[1] if len(sys.argv) > 1 else "all"
uni = dict(skill_spread=0.0, managers=(1.0,))
ch = Track("charlotte", 2414, 33.7, pit_road_s=40.0)
if which in ("all", "ch"):
    run("Charlotte stages, identical cars", ch, 334, marks=(84, 168), n=36, tank_laps=71,
        params=StrategyParams(tank_laps=71, game_plan_off=True, fuel_margin=2, stages=3, stage_ends=(84, 168)),
        cautions=(85, 169), **uni)
if which in ("all", "chns"):
    run("Charlotte no stages, identical cars, 2 cautions", ch, 334, marks=(), n=36, tank_laps=71,
        params=StrategyParams(tank_laps=71, game_plan_off=True, fuel_margin=2), cautions=(120, 250), **uni)
if which in ("all", "mart"):
    mart = Track("martinsville", 846, 20.0, pit_road_s=26.0, pit_frac=0.25, caution_lap_s=70.0)
    run("Martinsville 500, identical cars, cautions", mart, 500, n=36, tank_laps=130, slope=0.02,
        params=StrategyParams(tank_laps=130, game_plan_off=True, fuel_margin=2, track_type=SHORT_TRACK),
        cautions=(60, 140, 230, 330, 420), **uni)
if which in ("all", "road"):
    wg = Track("watkins", 3900, 72.0, pit_road_s=55.0, pit_frac=0.08)
    run("Watkins Glen 90, stages 20/40", wg, 90, n=36, tank_laps=32, slope=0.05,
        params=StrategyParams(tank_laps=32, game_plan_off=True, fuel_margin=1, track_type=ROAD_COURSE, stages=3,
                              stage_ends=(20, 40)), cautions=(21, 41), **uni)
if which in ("all", "ss"):
    tal = Track("talladega", 4280, 50.0, pit_road_s=45.0, pit_frac=0.06)
    run("Talladega 188, stages 60/120", tal, 188, n=36, tank_laps=40, slope=0.005,
        params=StrategyParams(tank_laps=40, game_plan_off=True, fuel_margin=1, track_type=SUPERSPEEDWAY, stages=3,
                              stage_ends=(60, 120)), cautions=(61, 121), drafting=True, wear_per_lap=0.002, **uni)
if which in ("all", "chvar"):
    run("Charlotte stages, realistic pace/tire spread", ch, 334, marks=(84, 168), n=36, tank_laps=71,
        params=StrategyParams(tank_laps=71, game_plan_off=True, fuel_margin=2, stages=3, stage_ends=(84, 168)),
        cautions=(85, 169))
if which in ("all", "rand"):
    import random as _r
    rr = _r.Random()
    cs = sorted(rr.sample(range(20, 320), 6))
    print("random cautions at", cs)
    run("Charlotte stages + random cautions", ch, 334, marks=(84, 168), n=36, tank_laps=71,
        params=StrategyParams(tank_laps=71, game_plan_off=True, fuel_margin=2, stages=3, stage_ends=(84, 168)),
        cautions=tuple(sorted(set(cs + [85, 169]))))
if which in ("all", "chpre"):
    sim = run("Charlotte: caution 8 laps before the stage 1 end, then the stage break", ch, 334, marks=(84, 168),
              n=36, tank_laps=71, params=StrategyParams(tank_laps=71, game_plan_off=True, fuel_margin=2, stages=3,
                                                       stage_ends=(84, 168)), cautions=(76, 85, 169), **uni)
    both = [c for c in sim.cars if {l for l, g, code in c.stops if not g and 76 <= l <= 80}
            and {l for l, g, code in c.stops if not g and 85 <= l <= 89}]
    print("   cars that stopped at the lap-76 caution AND the stage break:", len(both))
if which in ("all", "chlong"):
    # live Charlotte night: stage 2 is 117 laps (87 -> 204), real tank ~64, mileage estimate a bit low (60)
    sim = run("Charlotte 25/61% stages, tank 64, estimate 60", ch, 334, marks=(84, 204), n=36, tank_laps=64,
              params=StrategyParams(tank_laps=60, game_plan_off=True, fuel_margin=2, stages=3, stage_ends=(84, 204)),
              cautions=(85, 205), **uni)
    s2 = collections.Counter(sum(1 for l, g, code in c.stops if g and 88 <= l <= 203) for c in sim.cars)
    print("   green stops in stage 2 per car:", dict(sorted(s2.items())))
if which in ("all", "ch4"):
    # live Charlotte night, Race Control 2.7.0 defaults: 4 stages 84/167/251, caution at 108 (59 laps before
    # the stage 2 end, a tank is ~64): pitting there skips the green stop in stage 2
    sim = run("Charlotte night 4 stages, caution lap 108", ch, 334, marks=(167, 251), n=36, tank_laps=64,
              params=StrategyParams(tank_laps=64, game_plan_off=True, fuel_margin=2, stages=4,
                                    stage_ends=(84, 167, 251)), cautions=(37, 85, 108, 168, 230, 252), **uni)
    s2 = collections.Counter(sum(1 for l, g, code in c.stops if g and 112 <= l <= 166) for c in sim.cars)
    print("   green stops between the lap-108 caution and the stage 2 end:", dict(sorted(s2.items())))
