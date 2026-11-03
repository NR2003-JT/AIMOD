"""Driver behavior engine (plans per run + situational changes) and its memory writer, no game needed.

Run: python -m unittest discover -s tests
"""
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aimod import memory_map as mm
from aimod.behavior import (COOL, EFFECTS, MANAGE, OFF_TICKS, NORMAL, PUSH, MANAGE_SCORE, PUSH_END_LAPS, RIDE, RUN_OUT, SAVE_FUEL, SAVE_TIRES,
                            BehaviorConfig, BehaviorEngine, Car, Ctx, Effect)
from aimod.behavior_mem import BehaviorWriter

LT = 30.0          # lap time used by every test: 1 s = 1/30 of a lap


def ctx(**kw):
    d = dict(race=True, green=True, lap_now=50, total=200, to_go=151, lap_time=LT)
    d.update(kw)
    return Ctx(**d)


def car(idx, gap_to_leader=0.0, name=None, **kw):
    """A car `gap_to_leader` seconds behind the front car; at the start of a 40-lap run with plenty of fuel."""
    d = dict(number=idx, name=name or f"Driver {idx}", make="Ford", lap=50, progress=49.5 - gap_to_leader / LT,
             fuel_laps=60.0, fuel_margin=-91.0, laps_in_run=40.0, stint_laps=0, run_id=50, falloff=0.0,
             pace_delta=0.0, rating=50)
    d.update(kw)
    return Car(idx, **d)


def manager(idx, gap=0.0, **kw):
    """A driver that manages its tires on a long run (high rating, patient temperament - found by name)."""
    c = car(idx, gap, rating=100, **kw)
    k = 0
    while BehaviorEngine._manager_score(c, 40) < MANAGE_SCORE or c.temper > 0.4:
        k += 1
        c.name = f"Manager {idx}-{k}"
    return c


def runner(idx, gap=0.0, **kw):
    """A driver that runs it out (low rating, aggressive temperament)."""
    c = car(idx, gap, rating=0, **kw)
    k = 0
    while BehaviorEngine._manager_score(c, 40) >= MANAGE_SCORE or c.temper < 0.6:
        k += 1
        c.name = f"Runner {idx}-{k}"
    return c


def engine(**kw):
    cfg = BehaviorConfig(enabled=True)
    for k, v in kw.items():
        setattr(cfg, k, v)
    return BehaviorEngine(cfg)


def next_lap(cars, laps=1):
    for c in cars:
        c.lap += laps
        c.progress += laps
        if c.laps_in_run is not None:
            c.laps_in_run = max(0.0, c.laps_in_run - laps)
        c.stint_laps += laps
    return cars


def run_modes(e, cars, laps, idx, **kw):
    out = []
    for _ in range(laps):
        e.tick(ctx(**kw), cars)
        out.append(e.mode(idx))
        next_lap(cars)
    return out


class TestBasics(unittest.TestCase):
    def test_off_by_default_and_under_caution(self):
        cars = [car(2), car(3, 2.0)]
        self.assertEqual(BehaviorEngine().tick(ctx(), cars).effects, {})
        e = engine()
        self.assertEqual(e.tick(ctx(green=False), cars).effects, {})
        self.assertEqual(e.tick(ctx(race=False), cars).effects, {})

    def test_default_strength_is_70(self):
        self.assertEqual(BehaviorConfig().strength, 70)

    def test_a_blip_off_track_keeps_the_mode(self):
        e = engine()
        cars = [manager(2)]
        run_modes(e, cars, 6, 2)
        self.assertEqual(e.mode(2), SAVE_TIRES)
        cars[0].on_track = False
        for _ in range(OFF_TICKS - 1):
            e.tick(ctx(), cars)
        cars[0].on_track = True
        res = e.tick(ctx(), cars)
        self.assertEqual(e.mode(2), SAVE_TIRES)
        self.assertEqual(res.switches, [])

    def test_switches_carry_their_reason(self):
        e = engine()
        res = e.tick(ctx(), [car(2, fuel_laps=145.0, fuel_margin=-6.0, laps_in_run=151.0)])
        sw = res.switches[0]
        self.assertEqual((sw.new, sw.code), (SAVE_FUEL, "fuel_finish"))
        self.assertAlmostEqual(sw.value, 6.5)

    def test_strength_scales_effects(self):
        e = engine(strength=50)
        base = EFFECTS["other"][PUSH]
        half = e.effect(PUSH, False)
        self.assertAlmostEqual(half.traction - 1, (base.traction - 1) / 2)
        self.assertEqual(engine(strength=0).effect(PUSH, False), Effect())

    def test_same_inputs_same_calls(self):
        def run():
            e = engine()
            cars = [manager(2), runner(3, 5.0), car(4, 9.0, fuel_laps=150.0, fuel_margin=-1.0, laps_in_run=151.0)]
            return [sorted(e.tick(ctx(), next_lap(cars)).modes.items()) for _ in range(40)]
        self.assertEqual(run(), run())

    def test_pit_road_resets_to_normal_and_a_new_run_gets_a_new_plan(self):
        e = engine()
        cars = [manager(2)]
        run_modes(e, cars, 6, 2)
        self.assertEqual(e.mode(2), SAVE_TIRES)
        cars[0].on_track = False
        for _ in range(OFF_TICKS):                       # off the track for ~1.5 s: back to normal
            res = e.tick(ctx(), cars)
        self.assertEqual(e.mode(2), NORMAL)
        self.assertNotIn(2, res.effects)
        cars[0].on_track, cars[0].run_id, cars[0].stint_laps, cars[0].laps_in_run = True, 99, 0, 10.0
        e.tick(ctx(), cars)
        self.assertEqual(e.plan(2).style, RUN_OUT)            # a 10-lap run is just run out


class TestPlans(unittest.TestCase):
    def test_tire_manager_saves_mid_run_then_pushes_at_the_end(self):
        e = engine()
        modes = run_modes(e, [manager(2)], 40, 2)
        self.assertEqual(e.plan(2).style, MANAGE)
        self.assertEqual(modes[:4], [NORMAL] * 4)
        self.assertTrue(all(m == SAVE_TIRES for m in modes[4:25]), modes)
        self.assertEqual(modes[-PUSH_END_LAPS:], [PUSH] * PUSH_END_LAPS)
        switches = sum(1 for a, b in zip(modes, modes[1:]) if a != b)
        self.assertLessEqual(switches, 3, modes)            # a plan, not constant changes

    def test_runner_runs_it_out(self):
        e = engine()
        modes = run_modes(e, [runner(2)], 40, 2)
        self.assertEqual(e.plan(2).style, RUN_OUT)
        self.assertEqual(set(modes), {NORMAL})

    def test_short_runs_are_run_out(self):
        e = engine()
        run_modes(e, [manager(2, laps_in_run=12.0)], 1, 2)
        self.assertEqual(e.plan(2).style, RUN_OUT)

    def test_plan_follows_a_replanned_stop(self):
        e = engine()
        cars = [manager(2)]
        run_modes(e, cars, 6, 2)
        cars[0].laps_in_run = 2.0                            # AIMOD moved the stop up: the end-of-run push now
        e.tick(ctx(), cars)
        self.assertEqual(e.mode(2), PUSH)

    def test_neighbours_dont_change_a_plan(self):
        e = engine()
        front = [runner(9), runner(10, 4.0), runner(11, 8.0)]  # the top 3, spread out
        a, b = manager(2, 12.0), runner(3, 12.6)              # mid pack, close together
        modes = run_modes(e, front + [a, b], 30, 3)
        self.assertEqual(set(modes), {NORMAL})


class TestFuel(unittest.TestCase):
    def test_saves_when_saving_makes_the_finish(self):
        e = engine()
        e.tick(ctx(), [car(2, fuel_laps=145.0, fuel_margin=-6.0, laps_in_run=151.0)])
        self.assertEqual(e.mode(2), SAVE_FUEL)
        self.assertIn("short of the finish", e.state[2].reason)

    def test_no_save_when_a_stop_is_needed_anyway(self):
        e = engine()
        e.tick(ctx(), [runner(2, fuel_laps=40.0, fuel_margin=-111.0)])
        self.assertEqual(e.mode(2), NORMAL)

    def test_saves_for_the_stage_break_it_waits_for(self):
        e = engine()
        e.tick(ctx(stage_end=70), [car(2, fuel_laps=19.0, fuel_margin=-100.0, waits_for_stage=True)])
        self.assertEqual(e.mode(2), SAVE_FUEL)
        self.assertIn("stage break", e.state[2].reason)


class TestSituations(unittest.TestCase):
    def test_battle_for_the_lead_pushes_both(self):
        e = engine()
        cars = [manager(2, stint_laps=10), manager(3, 0.6, stint_laps=10)]
        e.tick(ctx(), cars)
        self.assertEqual((e.mode(2), e.mode(3)), (PUSH, PUSH))
        self.assertIn("defending the lead from #3", e.state[2].reason)
        self.assertIn("for the lead", e.state[3].reason)

    def test_no_battle_with_a_teammate(self):
        e = engine()
        e.tick(ctx(), [runner(2, team=1), runner(3, 0.6, team=1)])
        self.assertEqual((e.mode(2), e.mode(3)), (NORMAL, NORMAL))

    def test_mid_pack_battles_dont_push(self):
        e = engine()
        cars = [runner(i, gap) for i, gap in ((2, 0.0), (3, 3.0), (4, 6.0), (5, 6.5))]
        e.tick(ctx(), cars)
        self.assertEqual((e.mode(4), e.mode(5)), (NORMAL, NORMAL))

    def test_holds_off_the_leader_to_stay_on_the_lead_lap(self):
        e = engine()
        lead, back = runner(2), runner(7)
        back.progress = lead.progress - 1.0 + 1.5 / LT          # the leader 1.5 s behind it on track
        e.tick(ctx(), [lead, back])
        self.assertEqual(e.mode(7), PUSH)
        self.assertIn("stay on the lead lap", e.state[7].reason)

    def test_already_lapped_cars_dont_fight_the_leader(self):
        e = engine()
        lead, back = runner(2), runner(7, laps_down=1)
        back.progress = lead.progress - 1.0 + 1.5 / LT
        e.tick(ctx(), [lead, back])
        self.assertEqual(e.mode(7), NORMAL)

    def test_tires_going_away_saves_the_rest_of_the_run(self):
        e = engine()
        cars = [runner(2)]
        modes = run_modes(e, cars, 6, 2)
        cars[0].falloff = 0.05
        modes += run_modes(e, cars, 30, 2)
        self.assertIn(SAVE_TIRES, modes)
        first = modes.index(SAVE_TIRES)
        self.assertTrue(all(m == SAVE_TIRES for m in modes[first:len(modes) - 5]), modes)

    def test_stuck_cools_then_makes_a_run_once_a_run(self):
        e = engine()
        front = [runner(9), runner(10, 5.0), runner(11, 10.0)]  # the top 3, spread out
        lead, stuck = runner(2, 15.0), runner(3, 15.3, pace_delta=-0.2)
        modes = run_modes(e, front + [lead, stuck], 20, 3)
        self.assertIn(COOL, modes, modes)
        i = modes.index(COOL)
        self.assertEqual(modes[i:i + 5], [COOL, COOL, PUSH, PUSH, PUSH])
        self.assertEqual(modes.count(COOL), 2)                 # once this run

    def test_on_the_fly_can_be_off(self):
        e = engine(reactions=False)
        cars = [runner(2), runner(3, 0.6)]
        e.tick(ctx(), cars)
        self.assertEqual(e.mode(3), NORMAL)


class TestLateRace(unittest.TestCase):
    def test_everyone_who_can_pushes_late(self):
        e = engine()
        cars = [runner(2, fuel_laps=30.0, fuel_margin=10.0, laps_in_run=12.0),
                runner(3, 3.0, fuel_laps=30.0, fuel_margin=10.0, laps_in_run=12.0)]
        e.tick(ctx(lap_now=189, to_go=12), cars)
        self.assertEqual((e.mode(2), e.mode(3)), (PUSH, PUSH))

    def test_short_on_fuel_keeps_saving_late(self):
        e = engine()
        e.tick(ctx(lap_now=189, to_go=12), [car(2, fuel_laps=11.0, fuel_margin=-1.0, laps_in_run=12.0)])
        self.assertEqual(e.mode(2), SAVE_FUEL)

    def test_late_push_can_be_off(self):
        e = engine(late_push_pct=0)
        e.tick(ctx(lap_now=189, to_go=12), [runner(2, fuel_laps=30.0, fuel_margin=10.0, laps_in_run=12.0)])
        self.assertEqual(e.mode(2), NORMAL)


class TestDrafting(unittest.TestCase):
    def test_pack_rides_and_saves_with_its_make(self):
        e = engine()
        cars = [car(2, make="Toyota", stint_laps=5), car(3, 1.5, make="Ford", stint_laps=5),
                car(4, 1.8, name="Steady Ford", make="Ford", stint_laps=5), car(5, 2.1, make="Chevrolet", stint_laps=5),
                car(6, 2.4, make="Ford", stint_laps=5)]
        e.tick(ctx(drafting=True, stage_end=80), cars)
        self.assertEqual(e.plan(3).style, RIDE)
        self.assertEqual(e.mode(3), SAVE_FUEL)
        for i in (4, 6):
            self.assertEqual(e.mode(i), SAVE_FUEL)
            self.assertIn("with #3 (make)", e.state[i].reason)

    def test_most_of_the_field_saves(self):
        # superspeedway riding: about 85 % save fuel, the most aggressive ~15 % run their own pace
        e = engine()
        cars = [car(i, i * 0.3, make=("Ford", "Toyota", "Chevrolet")[i % 3], stint_laps=8) for i in range(2, 42)]
        e.tick(ctx(drafting=True, stage_end=80), cars)
        saving = sum(e.mode(c.idx) == SAVE_FUEL for c in cars)
        self.assertGreaterEqual(saving, 0.75 * len(cars))
        self.assertLess(saving, len(cars))

    def test_aggressive_driver_saves_when_its_plan_needs_it(self):
        e = engine()                                            # "Driver 4": one of the aggressive ~15 %
        e.tick(ctx(drafting=True, stage_end=80), [car(4, stint_laps=8)])
        self.assertEqual(e.mode(4), NORMAL)
        e = engine()
        e.tick(ctx(drafting=True, stage_end=80), [car(4, stint_laps=8, stretch=True)])
        self.assertEqual(e.mode(4), SAVE_FUEL)
        self.assertIn("stop fewer", e.state[4].reason)

    def test_team_comes_before_make(self):
        e = engine()
        front = [car(20, 0.0, make="Toyota", stint_laps=5), car(21, 3.0, make="Toyota", stint_laps=5),
                 car(22, 6.0, make="Toyota", stint_laps=5)]                # the top 3, spread out
        cars = front + [car(3, 9.5, make="Ford", stint_laps=5), car(7, 9.8, name="Calm Chevy", make="Chevrolet", team=4, stint_laps=5),
                        car(8, 10.1, make="Ford", team=4, stint_laps=5)]
        e.tick(ctx(drafting=True, stage_end=80), cars)
        self.assertIn("with #7 (team)", e.state[8].reason)

    def test_followers_dont_copy_a_lead_battle(self):
        e = engine()
        # #3 (Ford) leads, #7 (Chevrolet) right behind it battles for the lead; #9 (Chevrolet) and #12 (Ford) are in
        # the same pack: neither copies the battle, they run their own plan (riding)
        cars = [car(3, 1.0, make="Ford", stint_laps=5), car(7, 1.3, make="Chevrolet", stint_laps=5),
                car(9, 1.6, make="Chevrolet", stint_laps=5), car(12, 1.9, make="Ford", stint_laps=5)]
        res = e.tick(ctx(drafting=True, stage_end=80), cars)
        self.assertEqual((e.mode(3), e.mode(7)), (PUSH, PUSH))
        self.assertEqual((e.mode(9), e.mode(12)), (SAVE_FUEL, SAVE_FUEL))
        self.assertNotIn("with", e.state[9].reason)
        codes = {sw.idx: sw.code for sw in res.switches}
        self.assertEqual((codes[3], codes[7], codes[9]), ("lead_defend", "lead_attack", "ride"))

    def test_followers_copy_the_plan(self):
        e = engine()
        front = [car(20, 0.0, make="Toyota", stint_laps=5), car(21, 3.0, make="Toyota", stint_laps=5),
                 car(22, 6.0, make="Toyota", stint_laps=5)]
        cars = front + [car(3, 9.5, make="Ford", stint_laps=5), car(12, 9.8, make="Ford", stint_laps=5)]
        res = e.tick(ctx(drafting=True, stage_end=80), cars)
        sw = next(s for s in res.switches if s.idx == 12)
        self.assertEqual((sw.code, sw.other, sw.detail), ("follow", 3, "make"))

    def test_pushes_for_the_stage_end(self):
        e = engine()
        cars = [car(2, make="Toyota", stint_laps=5), car(3, 1.5, make="Ford", stint_laps=5),
                car(4, 1.8, make="Ford", stint_laps=5)]
        e.tick(ctx(drafting=True, stage_end=53), cars)
        self.assertEqual((e.mode(3), e.mode(4)), (PUSH, PUSH))

    def test_never_saves_or_cools_tires_at_drafting_tracks(self):
        e = engine()
        cars = [car(2, falloff=0.05), car(3, 1.5, pace_delta=-0.5, falloff=0.05)]
        modes = run_modes(e, cars, 30, 3, drafting=True, stage_end=150)
        self.assertFalse(set(modes) & {SAVE_TIRES, COOL}, modes)
        self.assertEqual(e.effect(SAVE_TIRES, True), Effect())


class FakePM:
    """Byte-addressed fake process memory."""

    def __init__(self):
        self.mem = {}
        self.writes = []
        addr, want = mm.DRIVER_MULT_CHECK
        self.put(addr, want)

    def put(self, addr, data):
        for k, b in enumerate(data):
            self.mem[addr + k] = b

    def read_bytes(self, addr, n):
        return bytes(self.mem.get(addr + k, 0) for k in range(n))

    def write_bytes(self, addr, data, n):
        self.writes.append(addr)
        self.put(addr, data[:n])

    def f(self, addr):
        return struct.unpack("<f", self.read_bytes(addr, 4))[0]

    def setf(self, addr, v):
        self.put(addr, struct.pack("<f", v))


def drv(i, off):
    return mm.CAR_BLOCK_0 + i * mm.CAR_STRIDE + mm.OFF_DRIVER + off


def setup_car(pm, i, traction=1.0, power=1.0, fuel=50.0):
    base = mm.CAR_BLOCK_0 + i * mm.CAR_STRIDE
    pm.setf(drv(i, mm.DRV_TRACTION), traction)
    pm.setf(drv(i, mm.DRV_POWER), power)
    for k in range(4):
        pm.setf(base + mm.OFF_WEAR_LF + 4 * k, 0.1)
    pa, pb = 0x100000 + i * 16, 0x200000 + i * 16
    pm.put(base + mm.OFF_FUEL_A, struct.pack("<I", pa))
    pm.put(base + mm.OFF_FUEL_B, struct.pack("<I", pb))
    key = bytes(range(1, 9))
    pm.put(pb, key)
    pm.put(pa, bytes(x ^ y for x, y in zip(struct.pack("<d", fuel), key)))
    return base, pa, pb


def fuel_of(pm, pa, pb):
    return struct.unpack("<d", bytes(x ^ y for x, y in zip(pm.read_bytes(pa, 8), pm.read_bytes(pb, 8))))[0]


class TestWriter(unittest.TestCase):
    def test_exe_check(self):
        w = BehaviorWriter()
        self.assertTrue(w.available(FakePM()))
        bad = FakePM()
        bad.put(mm.DRIVER_MULT_CHECK[0], b"\x90" * 6)
        self.assertFalse(BehaviorWriter().available(bad))

    def test_pace_ramps_in_and_follows_the_lap_rebuild(self):
        pm, w = FakePM(), BehaviorWriter()
        setup_car(pm, 5, traction=1.0)
        eff = {5: Effect(traction=0.97)}
        for _ in range(20):
            w.apply(pm, eff, {5: True})
        self.assertAlmostEqual(pm.f(drv(5, mm.DRV_TRACTION)), 0.97, places=5)
        pm.setf(drv(5, mm.DRV_TRACTION), 1.02)          # new lap: the game rebuilt it from the ratings
        w.apply(pm, eff, {5: True})
        self.assertAlmostEqual(pm.f(drv(5, mm.DRV_TRACTION)), 1.02 * 0.97, places=5)
        for _ in range(20):                              # mode ends: ramps back to the game's value
            w.apply(pm, {}, {5: True})
        self.assertAlmostEqual(pm.f(drv(5, mm.DRV_TRACTION)), 1.02, places=5)
        self.assertEqual(w.scale, {})

    def test_restore_puts_the_game_values_back(self):
        pm, w = FakePM(), BehaviorWriter()
        setup_car(pm, 5, traction=0.99, power=1.01)
        for _ in range(20):
            w.apply(pm, {5: Effect(traction=1.015, power=1.01)}, {5: True})
        self.assertEqual(w.restore(pm), 2)
        self.assertAlmostEqual(pm.f(drv(5, mm.DRV_TRACTION)), 0.99, places=5)
        self.assertAlmostEqual(pm.f(drv(5, mm.DRV_POWER)), 1.01, places=5)

    def test_wear_keeps_a_share_of_each_rise(self):
        pm, w = FakePM(), BehaviorWriter()
        base, _, _ = setup_car(pm, 5)
        eff = {5: Effect(wear=0.75)}
        w.apply(pm, eff, {5: True})
        for _ in range(10):
            for k in range(4):
                a = base + mm.OFF_WEAR_LF + 4 * k
                pm.setf(a, pm.f(a) + 0.01)
            w.apply(pm, eff, {5: True})
        self.assertAlmostEqual(pm.f(base + mm.OFF_WEAR_LF), 0.1 + 0.1 * 0.75, places=5)
        pm.setf(base + mm.OFF_WEAR_LF, 0.0)              # new tires: starts over, nothing written back up
        w.apply(pm, eff, {5: True})
        self.assertAlmostEqual(pm.f(base + mm.OFF_WEAR_LF), 0.0, places=6)

    def test_fuel_keeps_a_share_of_the_burn(self):
        pm, w = FakePM(), BehaviorWriter()
        _, pa, pb = setup_car(pm, 5, fuel=50.0)
        eff = {5: Effect(fuel=0.8)}
        w.apply(pm, eff, {5: True})
        for _ in range(10):
            f = fuel_of(pm, pa, pb) - 1.0
            pm.put(pa, bytes(x ^ y for x, y in zip(struct.pack("<d", f), pm.read_bytes(pb, 8))))
            w.apply(pm, eff, {5: True})
        self.assertAlmostEqual(fuel_of(pm, pa, pb), 50.0 - 10 * 0.8, places=6)

    def test_nothing_on_pit_road_and_never_slot_0_or_1(self):
        pm, w = FakePM(), BehaviorWriter()
        base, pa, pb = setup_car(pm, 5, fuel=50.0)
        for i in (0, 1):
            setup_car(pm, i)
        pm.writes.clear()
        w.apply(pm, {0: Effect(traction=0.9), 1: Effect(traction=0.9), 5: Effect(wear=0.5, fuel=0.5)},
                {0: True, 1: True, 5: False})
        f = fuel_of(pm, pa, pb) - 1.0
        pm.put(pa, bytes(x ^ y for x, y in zip(struct.pack("<d", f), pm.read_bytes(pb, 8))))
        w.apply(pm, {5: Effect(wear=0.5, fuel=0.5)}, {5: False})
        self.assertEqual(pm.writes, [])
        self.assertAlmostEqual(fuel_of(pm, pa, pb), 49.0)


if __name__ == "__main__":
    unittest.main()
