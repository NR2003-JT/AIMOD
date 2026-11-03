"""Simulated-race tests for the strategy engine (no game needed).

Run: python -m unittest discover -s tests
"""
import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aimod import memory_map as mm
from aimod.presets import Preset, PresetStore
from aimod.strategy import (FOUR_TIRES, FUEL_MAKES_IT, FUEL_ONLY, GAME_DECIDE, LEFT_SIDE, RIGHT_SIDE, CarInfo,
                            CarSnapshot, RaceSnapshot, STAGE_BREAK, StrategyEngine, StrategyParams, service_code)

GREEN, CAUTION = mm.FLAG_GREEN, 3


class Sim:
    """Tiny race: cars obey the pit flag the engine writes, finishing the stop
    a few ticks later; grip and wear degrade per lap and reset on new tires."""

    def __init__(self, n=10, total=200, rating_strat=2.0, seed=1):
        self.n, self.total = n, total
        self.lap = 1.0
        self.flag = GREEN
        self.pitting = [0] * n
        self.pit_ticks = [0] * n
        self.wear = [[0.0, 0.0] for _ in range(n)]   # left, right
        self.crew = [[0, 0] for _ in range(n)]
        self.strat = rating_strat
        self.damage = [0.0] * n
        self.engine = StrategyEngine(random.Random(seed))

    def snapshot(self):
        cars = []
        for i in range(self.n):
            l, r = self.wear[i]
            cars.append(CarSnapshot(i, True, grip=max(0.02, 1.0 - (l + r) / 2), strategy=self.strat,
                                    pitting=self.pitting[i], wear_lf=l, wear_lr=l, wear_rf=r, wear_rr=r,
                                    damage=self.damage[i], number=i + 1, name=f"Driver {i + 1}"))
        return RaceSnapshot(self.flag, mm.SESSION_RACE, 0.0, self.lap, self.total, cars)

    def step(self, params, ticks_per_lap=10):
        res = self.engine.tick(self.snapshot(), params)
        for w in res.writes:
            if w.offset == mm.OFF_PITTING:
                if w.value == 1 and self.pitting[w.idx] == 0:
                    self.pitting[w.idx] = 1
                    self.pit_ticks[w.idx] = 3
                elif w.value == 0 and self.pit_ticks[w.idx] <= 0:
                    self.pitting[w.idx] = 0
            elif w.offset == mm.OFF_REQ_LEFT:
                self.crew[w.idx][0] = w.value
            elif w.offset == mm.OFF_REQ_RIGHT:
                self.crew[w.idx][1] = w.value
        for i in range(self.n):
            if self.pitting[i]:
                self.pit_ticks[i] -= 1
                if self.pit_ticks[i] <= 0:
                    if self.crew[i][0]:
                        self.wear[i][0] = 0.0
                    if self.crew[i][1]:
                        self.wear[i][1] = 0.0
                    self.pitting[i] = 0
            self.wear[i][0] += 0.003 / ticks_per_lap
            self.wear[i][1] += 0.004 / ticks_per_lap
        self.lap += 1 / ticks_per_lap
        return res


class StrategyTests(unittest.TestCase):
    def run_race(self, params, sim, caution_laps=()):
        events = []
        while sim.lap < sim.total:
            lap = int(sim.lap)
            sim.flag = CAUTION if any(a <= lap < b for a, b in caution_laps) else GREEN
            events += sim.step(params).events
        return events

    def test_two_stint_race_plans_one_stop_in_window(self):
        p = StrategyParams(tank_laps=110, plan_multi_stop=False, stint_strategy=False)   # v1: even stints
        sim = Sim(total=200)
        events = self.run_race(p, sim)
        for i in range(sim.n):
            stops = [e for e in events if e.idx == i]
            self.assertEqual(len(stops), 1, f"car {i}: {stops}")
            # rating 100 always follows the plan with no spread: ideal lap 100 (fuel amount trims the finish)
            self.assertEqual(stops[0].lap, 100)
            self.assertEqual(stops[0].reason, "PLANNED")

    def test_multi_stop_plans_every_stint(self):
        p = StrategyParams(tank_laps=60, plan_multi_stop=True)
        events = self.run_race(p, Sim(n=5, total=200))
        for i in range(5):
            laps = [e.lap for e in events if e.idx == i]
            self.assertEqual(len(laps), 3, laps)        # 200 laps / 60 -> 4 stints
            stints = [b - a for a, b in zip([0] + laps, laps + [200])]
            self.assertTrue(all(s <= 60 for s in stints), stints)

    def test_rating_spreads_planned_laps(self):
        p = StrategyParams(tank_laps=110, plan_multi_stop=False, stint_strategy=False)
        sim = Sim(n=40, total=200, rating_strat=1.0)   # rating 50: follows ~half the time, +/-10 laps
        sim.step(p)
        plans = [st.planned_lap for st in sim.engine.cars.values()]
        planned = [x for x in plans if isinstance(x, int)]
        self.assertTrue(5 < len(planned) < 35, plans)
        self.assertTrue(all(92 <= x <= 109 for x in planned), planned)
        self.assertGreater(len(set(planned)), 3)

    def test_v1_behaviour_leaves_multi_stop_to_game(self):
        p = StrategyParams(tank_laps=60, plan_multi_stop=False)
        sim = Sim(n=3, total=200)
        sim.step(p)
        self.assertTrue(all(st.planned_lap == GAME_DECIDE for st in sim.engine.cars.values()))

    def test_tire_limit_downgrades_service(self):
        p = StrategyParams(tank_laps=60, max_tire_sets=2, sets_include_start=True, wear_l=0.05, wear_r=0.05,
                            stint_strategy=False)
        sim = Sim(n=6, total=200)
        events = self.run_race(p, sim)
        for i in range(sim.n):
            st = sim.engine.cars[i]
            self.assertLessEqual(st.left_pairs_used, 1)
            self.assertLessEqual(st.right_pairs_used, 1)
            svcs = [e.service for e in events if e.idx == i]
            self.assertGreaterEqual(len(svcs), 3)
            self.assertIn(FUEL_ONLY, svcs)   # once both sides are used up, stops are fuel only

    def test_limit_service_mapping(self):
        eng = StrategyEngine()
        p = StrategyParams(max_tire_sets=3, sets_include_start=False)
        st = eng.state(0)
        self.assertEqual(eng.limit_service(FOUR_TIRES, st, p), FOUR_TIRES)
        st.left_pairs_used = 3
        self.assertEqual(eng.limit_service(FOUR_TIRES, st, p), RIGHT_SIDE)
        self.assertEqual(eng.limit_service(LEFT_SIDE, st, p), FUEL_ONLY)
        st.right_pairs_used = 3
        self.assertEqual(eng.limit_service(FOUR_TIRES, st, p), FUEL_ONLY)
        self.assertEqual(eng.limit_service(FOUR_TIRES, st, StrategyParams()), FOUR_TIRES)

    def test_caution_stops_cars_in_window_once(self):
        p = StrategyParams(tank_laps=110, plan_multi_stop=False)
        sim = Sim(n=8, total=200, rating_strat=0.0)   # rating 0: never follows a plan
        events = self.run_race(p, sim, caution_laps=[(95, 99)])
        for i in range(sim.n):
            stops = [e for e in events if e.idx == i]
            self.assertEqual(len(stops), 1, stops)
            self.assertEqual(stops[0].reason, "CAUTION")
            self.assertTrue(95 <= stops[0].lap < 99)

    def test_no_pit_calls_while_pits_are_closed(self):
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=60)
        st = eng.state(0)
        st.fuel_state, st.urgency, st.decision = "ON WINDOW", 100, True
        snap = race([car(0, on_pit_road=False)], flag=CAUTION)
        snap.pits_closed = True
        res = eng.tick(snap, p)
        self.assertEqual(writes_for(res, 0, mm.OFF_PITTING), [0])
        self.assertIsNone(eng.cars[0].decision)
        snap.pits_closed = False                 # pit road opens: now the car is decided
        eng.tick(snap, p)
        self.assertIsNotNone(eng.cars[0].decision)

    def test_damage_forces_stop(self):
        p = StrategyParams(tank_laps=300)
        sim = Sim(n=2, total=100)
        for _ in range(30):
            sim.step(p)
        sim.damage[1] = 2.0
        events = []
        for _ in range(30):                      # green: flagged, but v1 waits for a caution
            events += sim.step(p).events
        self.assertEqual(events, [])
        self.assertTrue(sim.engine.cars[1].forced_damage)
        sim.flag = CAUTION
        for _ in range(30):
            events += sim.step(p).events
        self.assertEqual([e.idx for e in events], [1])
        self.assertEqual(events[0].reason, "DAMAGE")

    def test_no_plan_until_lap_count_known(self):
        p = StrategyParams(tank_laps=64)
        sim = Sim(n=3, total=167)
        sim.lap = 0.0
        res = sim.step(p)
        self.assertTrue(all(st.planned_lap is None for st in sim.engine.cars.values()))
        self.assertTrue(all(r.next_pit == GAME_DECIDE for r in res.rows))
        sim.lap = 10.0
        sim.step(p)
        self.assertTrue(all(isinstance(st.planned_lap, int) for st in sim.engine.cars.values()))

    def test_game_decide_shows_fuel_deadline(self):
        p = StrategyParams(tank_laps=64)
        sim = Sim(n=4, total=167, rating_strat=0.0)    # rating 0: never follows a plan
        sim.lap = 5.0
        res = sim.step(p)
        self.assertTrue(all(r.next_pit == "GAME (by 62)" for r in res.rows), [r.next_pit for r in res.rows])

    def test_invalid_and_out_cars(self):
        eng = StrategyEngine(random.Random(0))
        cars = [CarSnapshot(0, False), CarSnapshot(1, True, grip=0.9, reason_out=2, name="X")]
        res = eng.tick(RaceSnapshot(GREEN, mm.SESSION_RACE, 0, 10, 100, cars), StrategyParams())
        self.assertEqual(len(res.rows), 1)
        self.assertTrue(res.rows[0].out)
        self.assertEqual(res.writes, [])


def car(idx, **kw):
    base = dict(grip=0.9, strategy=0.0, number=idx, name=f"D{idx}", laps_done=10, lap_dist=100.0)
    base.update(kw)
    return CarSnapshot(idx, True, **base)


def race(cars, flag=GREEN, lap=11, total=100):
    return RaceSnapshot(flag, mm.SESSION_RACE, 0.0, lap, total, cars)


def writes_for(res, idx, offset):
    return [w.value for w in res.writes if w.idx == idx and w.offset == offset]


class PitRequestTests(unittest.TestCase):
    def test_side_bytes_follow_the_side_flag(self):
        for flag, a, b in ((1, 1, 0), (0, 0, 1)):     # LEFT SIDE stop
            ws = {w.offset: w.value for w in StrategyEngine.service_writes(3, flag, LEFT_SIDE)}
            self.assertEqual((ws[mm.OFF_REQ_LEFT], ws[mm.OFF_REQ_RIGHT]), (1, 0))
            self.assertEqual((ws[mm.OFF_CREW_A], ws[mm.OFF_CREW_B]), (a, b))
            self.assertEqual(ws[mm.OFF_FUEL_REQ], mm.FUEL_FULL)

    def test_game_stop_right_after_a_stop_is_called_off(self):
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=60)
        st = eng.state(0)
        st.stops, st.last_stop_lap, st.planned_lap = 1, 9, GAME_DECIDE
        res = eng.tick(race([car(0, pitting=1, laps_done=10)]), p)   # car lap 11: 2 laps after its stop
        self.assertEqual(res.events, [])
        self.assertIn(0, writes_for(res, 0, mm.OFF_PITTING))
        self.assertIn("CALLED OFF", res.rows[0].status)
        p.guard_game_stops = False
        res = eng.tick(race([car(0, pitting=1, laps_done=10)]), p)
        self.assertEqual(len(res.events), 1)

    def test_car_condition_call_is_called_off_once_patched(self):
        # Bristol 2026-11-02: #33's "car condition" sat at 0.99 for 400 laps and the game asked it in every few laps
        # (a stall stop never changes the value). With the no_game_condition_stops patch AIMOD doesn't give in to it.
        for patched, stops in ((False, 1), (True, 0)):
            eng = StrategyEngine(random.Random(1))
            eng.condition_off = patched
            p = StrategyParams(tank_laps=60)
            st = eng.state(0)
            st.stops, st.last_stop_lap, st.planned_lap = 1, 9, GAME_DECIDE
            res = eng.tick(race([car(0, pitting=1, laps_done=10, game_condition=0.99)]), p)
            self.assertEqual(len(res.events), stops, f"patched={patched}")
            if patched:
                self.assertIn(0, writes_for(res, 0, mm.OFF_PITTING))
                self.assertIn("CALLED OFF", res.rows[0].status)

    def test_gate_allow_list(self):
        eng = StrategyEngine(random.Random(1))
        eng.state(1).decision = True                       # AIMOD called it in
        eng.state(2).decision = False                      # AIMOD keeps it out
        cars = [car(1), car(2), car(3, rule_pending=True), car(4, game_penalty=True), car(5, game_repair=1),
                car(6, fuel=1.0, fuel_cap=100.0), car(7, pitting=1)]       # 7: only the game's own request
        self.assertEqual(eng.gate_allow(race(cars)), {1, 3, 4, 5, 6})

    def test_with_the_gate_a_repeated_game_ask_is_still_called_off(self):
        for gate, stops in ((False, 1), (True, 0)):
            eng = StrategyEngine(random.Random(1))
            eng.gate_on = gate
            p = StrategyParams(tank_laps=60)
            st = eng.state(0)
            st.stops, st.last_stop_lap, st.planned_lap = 1, 9, GAME_DECIDE
            st.calloffs = [11, 11, 11]                     # the re-plan asked three times this lap
            res = eng.tick(race([car(0, pitting=1, laps_done=10)]), p)
            self.assertEqual(len(res.events), stops, f"gate={gate}")

    def test_a_pending_penalty_rule_is_not_called_off(self):
        eng = StrategyEngine(random.Random(1))
        eng.gate_on = True
        st = eng.state(0)
        st.stops, st.last_stop_lap, st.planned_lap = 1, 9, GAME_DECIDE
        res = eng.tick(race([car(0, pitting=1, laps_done=10, rule_pending=True)]), StrategyParams(tank_laps=60))
        self.assertEqual(len(res.events), 1)

    def test_car_condition_counts_only_while_the_game_still_asks_for_it(self):
        eng = StrategyEngine(random.Random(1))
        bad, ok = car(0, game_condition=0.99), car(1, game_condition=0.3)
        self.assertTrue(eng.condition_call(bad))           # (an engine made without the app: not patched)
        self.assertFalse(eng.condition_call(ok))
        eng.condition_off = True
        self.assertFalse(eng.condition_call(bad))

    def test_game_stop_with_no_tires_left(self):
        p = StrategyParams(tank_laps=60, max_tire_sets=1, sets_include_start=True, min_stop_gap=0)
        eng = StrategyEngine(random.Random(1))
        st = eng.state(0)
        st.stops, st.last_stop_lap, st.planned_lap = 1, 5, GAME_DECIDE
        worn = dict(wear_lf=0.5, wear_lr=0.5, wear_rf=0.5, wear_rr=0.5)
        res = eng.tick(race([car(0, pitting=1, laps_done=10, **worn)], total=100), p)  # 54 laps of fuel left
        self.assertEqual(res.events, [], "fuel is fine and there are no tires to fit")
        st.last_stop_lap = -40                                      # now only 9 laps of fuel left
        res = eng.tick(race([car(0, pitting=1, laps_done=10, **worn)], total=100), p)
        self.assertEqual([e.service for e in res.events], [FUEL_ONLY])

    def test_game_stop_gets_the_worn_side(self):
        eng = StrategyEngine(random.Random(1))
        eng.state(0).planned_lap = GAME_DECIDE
        res = eng.tick(race([car(0, pitting=1, wear_rf=0.24, wear_rr=0.24, wear_lf=0.05, wear_lr=0.05)]),
                       StrategyParams(tank_laps=60, track_type="short"))      # (intermediates take 4 under green)
        self.assertEqual([e.service for e in res.events], [RIGHT_SIDE])

    def test_flag_change_mid_stop_counts_one_stop(self):
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=60)
        eng.state(0).planned_lap = GAME_DECIDE
        events = []
        for flag in (GREEN, GREEN, CAUTION, CAUTION):
            events += eng.tick(race([car(0, pitting=1)], flag=flag), p).events
        res = eng.tick(race([car(0, pitting=0)], flag=CAUTION), p)
        self.assertEqual(len(events), 1)
        self.assertEqual(len(res.exits), 1)
        self.assertEqual(writes_for(res, 0, mm.OFF_PITTING), [0])   # finished, not sent back in

    def test_player_car_is_never_written(self):
        eng = StrategyEngine(random.Random(1))
        res = eng.tick(race([car(0, pitting=1, controlled=False), car(1)], flag=CAUTION), StrategyParams())
        self.assertFalse([w for w in res.writes if w.idx == 0])
        self.assertEqual(res.rows[0].kind, "player")


class RunningOrderTests(unittest.TestCase):
    def test_order_and_laps_down(self):
        eng = StrategyEngine(random.Random(1))
        cars = [car(0, laps_done=50, lap_dist=500), car(1, laps_done=50, lap_dist=1500),
                car(2, laps_done=49, lap_dist=1900), car(3, laps_done=48, lap_dist=200),
                car(4, laps_done=50, lap_dist=2400)]
        info = eng.field_info(race(cars))
        self.assertEqual(sorted(info, key=lambda i: info[i].rank), [4, 1, 0, 2, 3])
        self.assertEqual(info[3].down, 2)
        self.assertEqual(info[2].down, 1)          # 49.79 is more than a lap behind 50.99
        self.assertEqual(info[0].down, 0)

    def test_position_only_changes_at_the_line(self):
        eng = StrategyEngine(random.Random(1))
        eng.track_len = 2400.0
        t = [100.0]

        def snap(d0, d1, l0=10, l1=10):
            t[0] += 1
            r = race([car(0, laps_done=l0, lap_dist=d0), car(1, laps_done=l1, lap_dist=d1)])
            r.session_time = t[0]
            return r
        info = eng.field_info(snap(1000, 900))
        self.assertEqual((info[0].rank, info[1].rank), (1, 2))
        info = eng.field_info(snap(1100, 1200))         # car 1 passes mid-lap: order held
        self.assertEqual((info[0].rank, info[1].rank), (1, 2))
        info = eng.field_info(snap(2390, 10, 10, 11))   # car 1 crosses the line first
        self.assertEqual((info[0].rank, info[1].rank), (2, 1))
        info = eng.field_info(snap(5, 30, 11, 11))      # car 0 crosses behind it
        self.assertEqual((info[0].rank, info[1].rank), (2, 1))

    def test_lap_time_and_gap_in_game_seconds(self):
        eng = StrategyEngine(random.Random(1))
        eng.track_len = 2400.0
        for n, lap in enumerate((10, 11, 12)):
            r = race([car(0, laps_done=lap, lap_dist=0), car(1, laps_done=lap, lap_dist=0)])
            r.session_time = 1000 + 30 * n
            eng.field_info(r)
        r = race([car(0, laps_done=12, lap_dist=1200), car(1, laps_done=12, lap_dist=1120)])
        r.session_time = 1075
        info = eng.field_info(r)
        self.assertAlmostEqual(eng.lap_time, 30.0)
        self.assertAlmostEqual(info[1].gap, 1.0, places=1)     # 80 m of 2400 at 30 s a lap

    def test_lapped_car_fuel_uses_its_own_laps(self):
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=60)
        eng.track_len = 2400.0
        cars = [car(0, laps_done=100, lap_dist=10), car(1, laps_done=90, lap_dist=5)]
        for c in (0, 1):
            eng.state(c).last_stop_lap = 50
        res = eng.tick(race(cars, lap=101, total=200), p)
        rows = {r.idx: r for r in res.rows}
        self.assertEqual(rows[1].laps_down, 10)
        self.assertEqual(rows[0].next_pit, "GAME (by 108)")
        self.assertEqual(rows[1].next_pit, "GAME (by 118)")   # its lap 108 = race lap 118 (10 down)

    def test_overtime_keeps_counting(self):
        eng = StrategyEngine(random.Random(1))
        res = eng.tick(race([car(0, laps_done=168)], lap=169, total=167), StrategyParams(tank_laps=60))
        self.assertEqual(res.laps_total, 169)
        self.assertEqual(res.laps_to_go, 1)



class TacticsTests(unittest.TestCase):
    def setUp(self):
        self.p = StrategyParams(tank_laps=60)

    def info(self, rank, gap, age, down=0):
        return CarInfo(rank, down, 0.0, gap, age)

    def late_call_rate(self, my_age, other_age, to_go=10, n=400):
        """How often a car that makes it on fuel pits under a late caution."""
        eng = StrategyEngine(random.Random(3))
        me = car(5)
        st = eng.state(5)
        st.green_gap = 1.0
        infos = {5: self.info(3, 1.0, my_age)}
        for k in range(6, 12):                       # six lead-lap cars close behind
            eng.state(k).green_gap = 1.0 + 0.2 * (k - 5)
            infos[k] = self.info(k - 2, eng.state(k).green_gap, other_age)
        pits = sum(eng.late_caution_call(me, st, infos[5], infos, to_go, self.p, 80, 80)[0] for _ in range(n))
        return pits / n

    def test_worn_car_with_fresh_cars_behind_pits_more(self):
        self.assertGreater(self.late_call_rate(0.9, 0.2), self.late_call_rate(0.2, 0.9) + 0.3)

    def test_more_laps_to_go_favours_tires(self):
        self.assertGreater(self.late_call_rate(0.6, 0.6, to_go=40), self.late_call_rate(0.6, 0.6, to_go=4))

    def test_track_position_bias(self):
        self.p.track_position_bias = 100
        hold = self.late_call_rate(0.6, 0.6)
        self.p.track_position_bias = 0
        self.assertGreater(self.late_call_rate(0.6, 0.6), hold)

    def test_lead_pack_follows_the_leader_by_gap(self):
        eng = StrategyEngine(random.Random(5))
        eng.leader_call = True
        rates = []
        for gap in (0.2, 1.5, 5.0):
            st = eng.state(9)
            st.green_gap = gap
            me = self.info(4, gap, 0.3)
            hits = sum((eng.follow_call(car(9), st, me, {9: me}, race([]), self.p) or (None,))[0] is True
                       for _ in range(300))
            rates.append(hits / 300)
        self.assertGreater(rates[0], rates[1])
        self.assertEqual(rates[2], 0)

    def test_drafting_partners_pit_together(self):
        eng = StrategyEngine(random.Random(2))
        eng.rng.random = lambda: 0.0                         # partner check always succeeds
        snap = race([car(0, make="Ford"), car(1, make="Ford"), car(2, make="Chevrolet")])
        snap.drafting = True
        infos = {0: self.info(1, 0.0, 0.3), 1: self.info(2, 0.3, 0.3), 2: self.info(3, 0.5, 0.3)}
        for idx, lap in ((0, 15), (1, 13), (2, 12)):
            eng.state(idx).planned_lap = lap
            eng.state(idx).last_stop_lap = 0
        eng.drafting_partners(snap.cars[0], eng.state(0), 11, infos, snap, self.p)
        self.assertEqual(eng.state(0).planned_lap, 13)      # moved up to its Ford partner, not the Chevy
        self.assertIn("#1", eng.state(0).plan_note)


class StintTests(unittest.TestCase):
    def plan(self, style_weights_rng=None, **kw):
        eng = StrategyEngine(random.Random(7))
        st = eng.state(0)
        st.last_stop_lap = 120
        return eng, st, eng.plan_stop(st, StrategyParams(tank_laps=60, **kw), 100, 121, 80, 200, 60)

    def test_window_and_styles(self):
        # 80 to go on 60 laps of fuel: stop between lap 142 (makes it to the end) and 179 (fuel runs out)
        seen = {}
        for seed in range(60):
            eng = StrategyEngine(random.Random(seed))
            st = eng.state(0)
            st.last_stop_lap = 120
            lap = eng.plan_stop(st, StrategyParams(tank_laps=60), 100, 121, 80, 200, 60)
            self.assertEqual(st.window, (143, 177))      # usable stint: 60 - 2 margin - 1 lap to pit entry
            self.assertTrue(143 <= lap <= 177)
            seen.setdefault(st.style, set()).add(lap)
        self.assertEqual(set(seen), {"short-pit", "split", "stretch"})
        self.assertLess(max(seen["short-pit"]), min(seen["stretch"]))
        self.assertIn(160, seen["split"])

    def test_cautions_and_wear_shift_the_choice(self):
        def count(style, **setup):
            n = 0
            for seed in range(300):
                eng = StrategyEngine(random.Random(seed))
                eng.cautions, eng.lap_now = setup.get("cautions", 0), 100
                n += eng.choose_style(None, 50, setup.get("wear", 0.0), StrategyParams()) == style
            return n
        self.assertGreater(count("stretch", cautions=10), count("stretch") + 40)
        self.assertGreater(count("short-pit", wear=2.0), count("short-pit") + 40)

    def test_team_reacts_to_undercut(self):
        eng = StrategyEngine(random.Random(1))
        eng.rng.random = lambda: 0.0
        eng.lap_now = 150
        st = eng.state(0)
        st.planned_lap, st.window, st.style, st.green_gap = 165, (142, 179), "split", 1.0
        eng.green_stops = [(5, 24, 150, 1.6, 0)]                 # #24, 0.6 s behind, just pitted
        eng.react_to_stops(car(0), st, 150, CarInfo(3, 0, 0, 1.0, 0.3), StrategyParams())
        self.assertEqual(st.planned_lap, 151)
        self.assertIn("#24", st.plan_note)

    def test_gap_holds_steady_over_the_line(self):
        eng = StrategyEngine(random.Random(1))
        eng.track_len = 2400.0
        seq = [(10, 2390), (10, 5), (11, 30), (11, 60)]          # distance rolls over a tick before the counter
        progs = [eng.car_progress(car(0, laps_done=l, lap_dist=d), 2400.0) for l, d in seq]
        self.assertTrue(all(b > a for a, b in zip(progs, progs[1:])), progs)
        self.assertLess(progs[-1] - progs[0], 0.05)
        eng2 = StrategyEngine(random.Random(1))
        seq = [(10, 2390), (11, 2398), (11, 8), (11, 40)]        # counter ticks first
        progs = [eng2.car_progress(car(0, laps_done=l, lap_dist=d), 2400.0) for l, d in seq]
        self.assertTrue(all(b > a for a, b in zip(progs, progs[1:])), progs)


class ServiceCodeTests(unittest.TestCase):
    def test_codes(self):
        self.assertEqual(service_code(FOUR_TIRES, True), "F+L/R")
        self.assertEqual(service_code(RIGHT_SIDE, False), "R")
        self.assertEqual(service_code(FUEL_ONLY, True), "F")
        self.assertEqual(service_code(LEFT_SIDE, True), "F+L")

    def test_tire_only_stop_when_fuel_makes_it(self):
        eng = StrategyEngine(random.Random(1))
        st = eng.state(0)
        st.planned_lap, st.last_stop_lap = GAME_DECIDE, 80
        st.service = RIGHT_SIDE
        eng.last_flag = CAUTION
        eng.caution_service = lambda car, st, me, to_go, p, base, tank: (base, "")   # keep the side choice
        res = eng.tick(race([car(0, pitting=1, laps_done=89)], flag=CAUTION, lap=90, total=100),
                       StrategyParams(tank_laps=60, free_fuel=False))
        self.assertEqual(res.events[0].code, "R")
        self.assertEqual(writes_for(res, 0, mm.OFF_FUEL_REQ), [0])
        eng = StrategyEngine(random.Random(1))                 # default: fuel fits in the tire change
        st = eng.state(0)
        st.planned_lap, st.last_stop_lap, st.service = GAME_DECIDE, 80, RIGHT_SIDE
        eng.last_flag = CAUTION
        eng.caution_service = lambda car, st, me, to_go, p, base, tank: (base, "")
        res = eng.tick(race([car(0, pitting=1, laps_done=89)], flag=CAUTION, lap=90, total=100),
                       StrategyParams(tank_laps=60))
        self.assertEqual(res.events[0].code, "F+R")

    def test_service_and_pit_road_times(self):
        # modelled on a real stop (Charlotte probe): pit road flag, game timer 0 -> 14.3, request clears at box exit
        eng = StrategyEngine(random.Random(1))
        eng.state(0).planned_lap = GAME_DECIDE
        p = StrategyParams(tank_laps=60)
        exits = []
        seq = [  # t, pitting, on pit road, game pit timer, speed, dist, damage
            (10, 1, False, 14.1, 70, 1200, 0.8), (20, 1, True, 14.1, 20, 2160, 0.8), (31, 1, True, 14.1, 20, 160, 0.8),
            (32, 1, True, 0.4, 0.0, 165, 0.8), (40, 1, True, 8.0, 0.0, 165, 0.5), (46, 1, True, 14.3, 0.0, 165, 0.2),
            (46.2, 0, True, 14.3, 5, 166, 0.2), (51, 0, True, 14.3, 20, 330, 0.2), (52, 0, False, 14.3, 40, 360, 0.2),
            (60, 0, False, 14.3, 60, 800, 0.2)]           # clearly gone (400 m off pit road): exit at 52
        states = []
        for t, pit, road, timer, spd, dist, dmg in seq:
            r = race([car(0, pitting=pit, on_pit_road=road, pit_timer=timer, speed=spd, lap_dist=dist, damage=dmg)],
                     flag=CAUTION)
            r.session_time = t
            exits += eng.tick(r, p).exits
            ev = eng.cars[0].event or eng.cars[0].last_event
            states.append(ev.state)
        self.assertEqual(states, ["requested", "road", "road", "stall", "stall", "stall", "exit", "exit", "exit",
                                  "out"])
        e = exits[0]
        self.assertAlmostEqual(e.box_time, 14.3)           # the game's own service timer
        self.assertAlmostEqual(e.road_time, 32.0)          # pit road entry (20) to exit (52)
        self.assertEqual((e.t_pit_in, e.t_box_in, e.t_box_out, e.t_pit_out), (20, 32, 46.2, 52))
        self.assertAlmostEqual(e.dmg_in, 0.8)
        self.assertAlmostEqual(e.dmg_out, 0.2)

    def test_withdrawn_request_is_not_a_stop(self):
        eng = StrategyEngine(random.Random(1))
        st = eng.state(0)
        st.planned_lap = GAME_DECIDE
        p = StrategyParams(tank_laps=60)
        res = eng.tick(race([car(0, pitting=1, on_pit_road=False, pit_timer=0.0)], flag=CAUTION), p)
        self.assertEqual(len(res.events), 1)
        res = eng.tick(race([car(0, pitting=0, on_pit_road=False, pit_timer=0.0)], flag=CAUTION), p)
        self.assertEqual(len(res.cancels), 1)
        self.assertEqual(st.stops, 0)
        self.assertEqual(eng.pit_board(), [])

    def test_damage_marker_and_green_laps(self):
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=60)
        st = eng.state(0)
        st.planned_lap, st.forced_damage, st.decision = GAME_DECIDE, True, True
        res = eng.tick(race([car(0, pitting=1, laps_done=10)], flag=CAUTION), p)
        self.assertEqual(res.events[0].code[:2], "D+")
        eng.tick(race([car(0, pitting=0, laps_done=10)], flag=CAUTION), p)
        for lap, flag in ((11, CAUTION), (12, GREEN), (13, GREEN), (14, GREEN)):
            res = eng.tick(race([car(0, laps_done=lap)], flag=flag, lap=lap + 1, total=100), p)
        self.assertEqual(res.rows[0].green_laps, 3)            # laps 12-14 completed once the green was out
        self.assertEqual(res.rows[0].fuel_margin, 60 - 4 - 86)


class PresetTests(unittest.TestCase):
    def test_legacy_roundtrip_and_track_assignment(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "Old.json"), "w") as f:
                f.write('{"UMBRAL_WEAR_R": 0.293, "UMBRAL_WEAR_L": 0.336, "FACTOR_URGENCIA": 1250.0,'
                        ' "UMBRAL_DANIO": 0.5, "MAX_TIRE_SETS": 3, "TANQUE_MANUAL_VALOR": 61.0}')
            store = PresetStore(d)
            p = store.load("Old")
            self.assertEqual((p.wear_r, p.max_tire_sets, p.tank_laps), (0.293, 3, 61.0))
            store.save("New", Preset(tank_laps=64))
            store.assign_track("Old", "charlotte")
            self.assertEqual(store.find_for_track("Charlotte"), "Old")
            store.assign_track("New", "charlotte")
            self.assertEqual(store.find_for_track("charlotte"), "New")
            self.assertEqual(store.load("Old").tracks, [])
            import json
            with open(store.path("New"), encoding="utf-8") as f:
                raw = json.load(f)
            self.assertEqual(raw["TANQUE_MANUAL_VALOR"], 64)


if __name__ == "__main__":
    unittest.main()


class FuelCallTests(unittest.TestCase):
    def call(self, fuel_left, to_go, state, drafting=False, seed=1, **kw):
        eng = StrategyEngine(random.Random(seed))
        eng.drafting = drafting
        st = eng.state(0)
        st.fuel_state = state
        return eng.caution_fuel_call(st, fuel_left, to_go, StrategyParams(tank_laps=60, **kw))

    def test_within_reach_must_pit(self):
        pit, note, must = self.call(20, 70, "SHORT ON FUEL")      # fill now -> 12 laps short of the finish
        self.assertTrue(pit and must, note)

    def test_far_from_the_end_depends_on_fuel_used(self):
        early = sum(bool(self.call(55, 150, "SHORT ON FUEL", seed=k)[0]) for k in range(100))
        late = sum(bool(self.call(20, 150, "SHORT ON FUEL", seed=k)[0]) for k in range(100))
        self.assertEqual(early, 0)                                # 8% of the tank used: stay out
        self.assertEqual(late, 100)                               # 67% used: free stop

    def test_drafting_pits_only_when_it_changes_the_fuel_math(self):
        # 60-lap tank. Reaches the finish: stays out (a stop gains nothing)
        self.assertIsNone(self.call(55, 50, "SHORT ON FUEL", drafting=True)[0])
        # can't reach it, a fill gets it there: must
        pit, note, must = self.call(20, 50, "SHORT ON FUEL", drafting=True)
        self.assertTrue(pit and must, note)
        # 150 to go: 40 laps of fuel needs 2 more stops, a full tank too - no point stopping now (live 2026-10-26:
        # this kind of call gave 3-4 stops in a 57-lap Talladega stage)
        self.assertIsNone(self.call(40, 150, "SHORT ON FUEL", drafting=True)[0])
        # 10 laps of fuel: 3 more stops, a full tank 2 - filling now saves one
        pit, note, must = self.call(10, 150, "SHORT ON FUEL", drafting=True)
        self.assertTrue(pit and must and "saves" in note, note)
        self.assertIsNone(self.call(20, 15, "MAKES IT", drafting=True)[0])        # makes it: track position call

    def test_drafting_groups_pit_on_the_same_lap(self):
        laps = {}
        for seed in range(40):
            eng = StrategyEngine(random.Random(seed))
            eng.drafting, eng.common_start = True, 120
            st = eng.state(0)
            st.last_stop_lap = 121                               # pitted a lap after the pack
            lap = eng.plan_stop(st, StrategyParams(tank_laps=60), 60, 122, 80, 200, 60)
            laps.setdefault(st.style, set()).add(lap)
        self.assertTrue(all(len(v) == 1 for v in laps.values()), laps)   # one lap per plan
        self.assertLess(max(laps["stretch"]) - min(laps["short-pit"]), 15)  # tight spread (30% of the window)


class GameStopGuardTests(unittest.TestCase):
    def request(self, last_stop, drafting=False, wear=0.0):
        eng = StrategyEngine(random.Random(1))
        st = eng.state(0)
        st.stops, st.last_stop_lap, st.planned_lap = 1, last_stop, GAME_DECIDE
        r = race([car(0, pitting=1, laps_done=40, wear_lf=wear, wear_lr=wear, wear_rf=wear, wear_rr=wear)],
                 lap=41, total=200)
        r.drafting = drafting
        return eng.tick(r, StrategyParams(tank_laps=40))

    def test_plenty_of_fuel_is_called_off(self):
        res = self.request(last_stop=28, drafting=True)            # 13 laps into a 40-lap tank
        self.assertEqual(res.events, [])
        self.assertIn("fuel is fine", res.rows[0].status)

    def test_low_fuel_goes_through(self):
        res = self.request(last_stop=10, drafting=True)            # 31 laps in: 9 left of 40
        self.assertEqual(len(res.events), 1)

    def test_worn_tires_only_count_away_from_drafting_tracks(self):
        self.assertEqual(len(self.request(last_stop=28, wear=0.4).events), 1)
        self.assertEqual(self.request(last_stop=28, drafting=True, wear=0.4).events, [])

    def test_drafting_late_caution_makes_it_stays_out(self):
        eng = StrategyEngine(random.Random(1))
        eng.drafting = True
        st = eng.state(0)
        st.fuel_state, st.urgency = FUEL_MAKES_IT, 100
        eng.caution_decision(car(0), st, CarInfo(20, 1, 0, 9.0, 0.9), {}, race([]), StrategyParams(), True, 2, 95, 95,
                             fuel_left=30)
        self.assertFalse(st.decision)


class WaveAroundTests(unittest.TestCase):
    def setup(self, car_frac, pace_frac=0.6, down=1):
        eng = StrategyEngine(random.Random(1))
        eng.rng.random = lambda: 0.0
        eng.track_len = 1000.0
        infos = {1: CarInfo(1, 0, 100.10, 0.0, 0.2), 9: CarInfo(25, down, 100.10 - down + car_frac, 40.0, 0.9)}
        snap = race([])
        snap.pace_dist = 1000.0 * (0.10 + pace_frac) % 1000.0
        st = eng.state(9)
        st.fuel_state, st.urgency, st.service = "SHORT ON FUEL", 100, FOUR_TIRES
        return eng, st, infos, snap

    def test_lapped_car_ahead_of_leader_stays_out_for_the_wave(self):
        eng, st, infos, snap = self.setup(car_frac=0.3)          # 30% of a lap ahead of the leader, before the pace car
        eng.caution_decision(car(9), st, infos[9], infos, snap, StrategyParams(tank_laps=60), True, 80, 95, 95,
                             fuel_left=40)
        self.assertFalse(st.decision)
        self.assertIn("wave around", st.note)

    def test_no_wave_around_in_this_race_means_no_waiting_for_it(self):
        # bug report: RaceControl's wave around switched off, AIMOD still kept lapped cars out "for the wave around"
        eng, st, infos, snap = self.setup(car_frac=0.3)
        snap.wave_around = False
        eng.caution_decision(car(9), st, infos[9], infos, snap, StrategyParams(tank_laps=60), True, 80, 95, 95,
                             fuel_left=40)
        self.assertTrue(st.decision)
        self.assertNotIn("wave around", st.note)

    def test_race_control_wave_around_line(self):
        import tempfile
        from unittest import mock
        from aimod import game as g
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.txt")
            with mock.patch.object(g, "RACE_CONTROL_STATE", path):
                self.assertIsNone(g.race_control_wave_around())                 # RaceControl not running
                cases = ((["pits_closed=0", "wave_around=0"], False), (["wave_around=1"], True),
                         (["pits_closed=0", "stage_ends=84,204"], True))       # older build: always waved
                for lines, want in cases:
                    text = "".join(line + chr(10) for line in lines)
                    open(path, "w").write(text)
                    self.assertIs(g.race_control_wave_around(), want, text)

    def test_lapped_car_behind_the_leader_pits(self):
        eng, st, infos, snap = self.setup(car_frac=0.8)          # beyond the pace car: not in line for the wave
        eng.caution_decision(car(9), st, infos[9], infos, snap, StrategyParams(tank_laps=60), True, 80, 95, 95,
                             fuel_left=40)
        self.assertTrue(st.decision)

    def blocker_setup(self, blocker_decision, blocker_on_road=False):
        eng, st, infos, snap = self.setup(car_frac=0.3)
        infos[5] = CarInfo(10, 0, 100.10 - 0.5, 20.0, 0.5)     # lead lap car 0.5 behind: in front of #9 on track
        eng.state(5).decision = blocker_decision
        snap.cars = [car(5, on_pit_road=blocker_on_road), car(9, on_pit_road=False)]
        return eng, st, infos, snap

    def test_lead_lap_car_caught_in_front_of_the_leader_does_not_stop_the_wave(self):
        for decision, on_road in ((False, False), (True, False), (None, True)):
            eng, st, infos, snap = self.blocker_setup(decision, on_road)
            eng.caution_decision(car(9), st, infos[9], infos, snap, StrategyParams(tank_laps=60), True, 80, 95, 95,
                                 fuel_left=40)
            self.assertFalse(st.decision)
            self.assertIn("wave around", st.note)

    def test_no_service_needed_is_not_called_a_wave_stay_out(self):
        eng, st, infos, snap = self.setup(car_frac=0.3)
        st.urgency, st.fuel_state = 0, FUEL_MAKES_IT
        eng.caution_decision(car(9), st, infos[9], infos, snap, StrategyParams(tank_laps=60), True, 80, 95, 95,
                             fuel_left=40)
        self.assertFalse(st.decision)
        self.assertIn("wave around", st.note)

    def test_only_damage_worn_tires_or_an_empty_tank_give_up_the_wave(self):
        p = StrategyParams(tank_laps=60)
        eng, st, infos, snap = self.setup(car_frac=0.3)
        st.fuel_state = "ON WINDOW"                              # fuel window alone: still takes the wave
        eng.caution_decision(car(9), st, infos[9], infos, snap, p, True, 50, 95, 95, fuel_left=40)
        self.assertFalse(st.decision)
        for kw, why in ((dict(fuel_left=5), "fuel"), (dict(prob=100), "tires"), (dict(damage=True), "damage")):
            eng, st, infos, snap = self.setup(car_frac=0.3)
            st.forced_damage = kw.get("damage", False)
            prob = kw.get("prob", 60)
            if "prob" in kw:
                eng.pace_info = {9: {"falloff": 0.04, "k": 1.0}}      # 4% slower than its early run
            eng.caution_decision(car(9), st, infos[9], infos, snap, p, True, 50, prob, prob,
                                 fuel_left=kw.get("fuel_left", 40))
            self.assertTrue(st.decision, why)
            self.assertIn(why, st.note)

    def test_drafting_wave_only_given_up_near_a_blowout(self):
        p = StrategyParams(tank_laps=60, wear_l=0.3, wear_r=0.3)
        for wear, pits in ((0.40, False), (0.46, True)):
            eng, st, infos, snap = self.setup(car_frac=0.3)
            eng.drafting = True
            eng.caution_decision(car(9, wear_rf=wear, wear_rr=wear), st, infos[9], infos, snap, p, True, 50,
                                 100, 100, fuel_left=40)
            self.assertEqual(bool(st.decision), pits)

    def test_lapped_car_not_in_line_has_no_nothing_to_lose_stop(self):
        for seed in range(30):
            eng, st, infos, snap = self.setup(car_frac=0.8)
            eng.rng = random.Random(seed)
            st.urgency, st.fuel_state = 0, FUEL_MAKES_IT
            eng.caution_decision(car(9), st, infos[9], infos, snap, StrategyParams(tank_laps=60), True, 20, 50,
                                 50, fuel_left=40)
            self.assertNotIn("nothing to lose", st.note)


class PitBoardTests(unittest.TestCase):
    def run_stops(self, flag):
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=60)
        for i in (0, 1):
            eng.state(i).planned_lap = GAME_DECIDE
            eng.state(i).decision = True
        eng.last_flag = flag
        eng.caution_start = 0.0 if flag != GREEN else None
        seq = [  # (time, [(pitting, dist, speed)] for car 0 and car 1)
            (10, [(1, 100, 30), (1, 50, 30)]), (11, [(1, 900, 20), (1, 120, 30)]), (12, [(1, 900, 0), (1, 880, 20)]),
            (20, [(1, 900, 0), (1, 880, 0)]), (21, [(1, 900, 20), (1, 880, 0)]), (24, [(0, 1000, 60), (1, 880, 0)])]
        for t, cs in seq:
            r = race([car(i, pitting=a, lap_dist=d, speed=v) for i, (a, d, v) in enumerate(cs)], flag=flag)
            r.session_time = t
            eng.tick(r, p)
        return eng, p

    def test_race_off_pit_road(self):
        from aimod.pitboard import payload
        eng, p = self.run_stops(CAUTION)
        board = payload(eng.pit_board(12), eng.now, False, 50, 100, {})
        self.assertEqual(board["mode"], "race_off")
        self.assertEqual([(d["idx"], d["state"]) for d in board["stops"]], [(0, "out"), (1, "stall")])
        self.assertEqual(board["stops"][0]["label"], "4 TIRES")        # caution stop at an intermediate
        self.assertAlmostEqual(board["stops"][0]["service"], 8.0)
        eng.now = 400                                       # left pit road long ago: dropped even under caution
        self.assertEqual(sorted(e.idx for e in eng.pit_board(12)), [1])

    def test_green_pit_road_drops_finished_stops(self):
        from aimod.pitboard import payload
        eng, p = self.run_stops(GREEN)
        board = payload(eng.pit_board(12), eng.now, True, 50, 100, {})
        self.assertEqual([(d["idx"], d["state"]) for d in board["stops"]], [(0, "out"), (1, "stall")])  # order in
        eng.now = 40
        self.assertEqual([e.idx for e in eng.pit_board(12)], [1])

    def test_retired_car_leaves_the_board(self):
        eng, p = self.run_stops(CAUTION)
        r = race([car(0, lap_dist=1000, speed=60), car(1, pitting=1, lap_dist=880, speed=0, reason_out=3)],
                 flag=CAUTION)
        r.session_time = 26
        res = eng.tick(r, p)
        self.assertEqual([e.state for e in res.exits], ["retired"])
        self.assertIsNone(eng.cars[1].event)
        eng.now = 33                                        # retired car gone after 6 s, finished one kept 12 s
        self.assertEqual([e.idx for e in eng.pit_board(12)], [0])

    def test_short_track_names(self):
        from aimod.pitboard import short_track
        self.assertEqual(short_track("Charlotte_2012_SDT_Night_CE"), "CHARLOTTE")
        self.assertEqual(short_track("Texas Motor_Speedway_2012"), "TEXAS")

    def test_rows_locked_in_arrival_order_and_coming_in_strip(self):
        from aimod.pitboard import payload
        from aimod.strategy import PitEvent

        def ev(idx, num, name, state, t_pit_in=None, t_box_in=None, t_box_out=None, pos_in=5):
            e = PitEvent(0, idx, num, name, 50, "4 TIRES", "CAUTION", False, 1, pos_in=pos_in, t_game=idx)
            e.state, e.t_pit_in, e.t_box_in, e.t_box_out = state, t_pit_in, t_box_in, t_box_out
            return e
        evs = [ev(1, 11, "Denny Hamlin", "exit", 10, 20, 33), ev(2, 24, "William Byron", "exit", 11, 21, 32),
               ev(3, 5, "Kyle Larson", "stall", 12, 22), ev(4, 9, "Chase Elliott", "road", 13),
               ev(5, 12, "Ryan Blaney", "in", pos_in=7), ev(6, 22, "Joey Logano", "requested", pos_in=3)]
        board = payload(evs, 40, False, 50, 100, {})
        self.assertEqual([d["number"] for d in board["stops"]], ["11", "24", "5", "9"])     # order they reached pit road
        self.assertEqual([(c["number"], c["this_time_by"]) for c in board["coming_in"]], [("12", True), ("22", False)])

    def test_requested_vs_this_time_by(self):
        from aimod.pitboard import payload
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=60)
        eng.track_len = 2400.0
        eng.learn_pit_entry(2300.0)                       # pit road starts 2300 m into the lap
        st = eng.state(0)
        st.planned_lap, st.decision = GAME_DECIDE, True
        states = []
        for t, dist, spd in ((10, 500, 80), (11, 1200, 80), (12, 1700, 80), (13, 2100, 60), (14, 2300, 20), (14.15, 2300, 20), (14.3, 2300, 0),
                             (15, 2300, 0)):
            r = race([car(0, pitting=1, lap_dist=dist, speed=spd, on_pit_road=t >= 14.15, pit_timer=0.0)],
                     flag=CAUTION)
            r.session_time = t
            eng.tick(r, p)
            states.append(eng.cars[0].event.state)
            if t == 11:
                self.assertEqual(payload(eng.pit_board(), eng.now, False, 50, 100, {})["stops"], [])
        self.assertEqual(states, ["requested", "requested", "in", "in", "in", "road", "road", "stall"])
        self.assertAlmostEqual(eng.pit_entry, 2300.0)

    def test_pit_entry_unknown_shows_only_on_pit_road(self):
        eng = StrategyEngine(random.Random(1))
        eng.track_len = 2400.0
        self.assertEqual(eng.approach_state(2290.0), "requested")


class WaveLogTests(unittest.TestCase):
    def test_reads_new_wave_lines_only(self):
        from aimod.wavelog import WaveLog
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "bc_wave.log")
            with open(path, "w") as f:
                f.write("16:30:50 lap 73 flag 5: wave around (laps behind the leader): #2(0.99) -> tail of the field\n")
            w = WaveLog(path)
            self.assertEqual(w.poll(), [])                       # old lines ignored
            with open(path, "a") as f:
                f.write("17:09:09 lap 52 flag 5: one to go: leader #16, 28 lead lap, 11 lapped, 3 waved\n"
                        "17:09:09 lap 52 flag 5: wave around (laps behind the leader): #83(1.93) #22(1.94) #5(0.97)"
                        " -> tail of the field\n")
            self.assertEqual(w.poll(), [(52, [83, 22, 5])])
            self.assertTrue(w.is_waved(22, 53))
            self.assertFalse(w.is_waved(2, 53))
            self.assertFalse(w.is_waved(22, 60))                 # tag expires after a few laps


class ClockTests(unittest.TestCase):
    def test_frozen_game_clock_falls_back_to_real_time_while_cars_move(self):
        wall = [100.0]
        eng = StrategyEngine(random.Random(1), clock=lambda: wall[0])
        p = StrategyParams(tank_laps=60)
        for k in range(5):                                   # session clock stuck at 1517.47 (seen live)
            wall[0] += 0.15
            r = race([car(0, lap_dist=100 + 12 * k)])
            r.session_time = 1517.47
            eng.tick(r, p)
            if k == 0:
                start = eng.now
        self.assertAlmostEqual(eng.now - start, 0.6, places=3)   # 4 ticks of real time
        for k in range(3):                                   # paused: nothing moves, clock holds
            wall[0] += 0.15
            r = race([car(0, lap_dist=148)])
            r.session_time = 1517.47
            eng.tick(r, p)
        self.assertAlmostEqual(eng.now - start, 0.6, places=3)     # held while paused
        wall[0] += 0.15
        r = race([car(0, lap_dist=170)])
        r.session_time = 1519.47                             # clock running again (2 s = time acceleration)
        eng.tick(r, p)
        self.assertAlmostEqual(eng.now - start, 2.6, places=3)


class BoardDropTests(unittest.TestCase):
    def stop(self, steps):
        eng = StrategyEngine(random.Random(1))
        eng.state(0).planned_lap = GAME_DECIDE
        p = StrategyParams(tank_laps=60)
        res_all = []
        for t, pit, road, timer, spd in steps:
            r = race([car(0, pitting=pit, on_pit_road=road, pit_timer=timer, speed=spd)], flag=CAUTION)
            r.session_time = t
            res_all.append(eng.tick(r, p))
        return eng, res_all

    def test_row_drops_hold_seconds_after_box_exit(self):
        eng, _ = self.stop([(10, 1, True, 5.0, 20), (12, 1, True, 0.3, 0), (25, 1, True, 13.0, 0),
                            (25.2, 0, True, 13.0, 5), (30, 0, True, 13.0, 20)])
        self.assertEqual(len(eng.pit_board(12)), 1)          # 4.8 s after box exit
        eng.now = 38.0
        self.assertEqual(eng.pit_board(12), [])              # 12.8 s after box exit, still on pit road

    def test_pit_exit_fallback(self):
        eng, res = self.stop([(10, 1, True, 5.0, 20), (12, 1, True, 0.3, 0), (25, 1, True, 13.0, 0),
                              (25.2, 0, True, 13.0, 5), (51, 0, True, 13.0, 60)])
        self.assertEqual([e.state for e in res[-1].exits], ["out"])   # flag never cleared: closed after 25 s
        self.assertIsNone(eng.cars[0].event)


class ArtTests(unittest.TestCase):
    def make_art(self, d):
        for series, nums in (("2012NCS", ["24", "48", "99", "17"]), ("2026NCS", ["24", "5", "11", "23", "45"])):
            os.makedirs(os.path.join(d, "series", series, "numbers"))
            for n in nums:
                open(os.path.join(d, "series", series, "numbers", n + ".png"), "wb").close()
        with open(os.path.join(d, "series", "2026NCS", "colors.json"), "w") as f:
            f.write('{"5": "#123456"}')
        os.makedirs(os.path.join(d, "images"))
        open(os.path.join(d, "images", "defaultheadshot.png"), "wb").close()

    def test_auto_matches_series_to_the_field(self):
        from aimod.companion_art import ArtData
        with tempfile.TemporaryDirectory() as d:
            self.make_art(d)
            art = ArtData([d], companion=False)
            art.set_field([5, 11, 23, 24])
            art.refresh(force=True)
            self.assertEqual(art.series, "2026NCS")
            self.assertEqual(art.rel_number(5), "series/2026NCS/numbers/5.png")
            self.assertIsNone(art.rel_number(48))
            self.assertEqual(art.rel_headshot(5), "images/defaultheadshot.png")
            self.assertEqual(art.color(5), "#123456")
            art.set_field([48, 99, 17])
            art.refresh(force=True)
            self.assertEqual(art.series, "2012NCS")
            art.choice = "2026NCS"
            art.refresh(force=True)
            self.assertEqual(art.series, "2026NCS")             # a chosen series wins
            self.assertIsNone(art.abs_path("../secret.png"))


class PaceTests(unittest.TestCase):
    def test_falloff_and_learned_threshold(self):
        from aimod.pace import PaceTracker
        pt = PaceTracker(worn_pct=1.0)
        for car_idx in range(6):
            for lap in range(1, 30):                       # 30.0 s early, losing 0.02 s a lap
                wear = 0.01 * lap
                pt.lap(car_idx, 10, 10 + lap, 30.0 + max(0, lap - 4) * 0.02, True, wear, wear * 1.3, 30.0)
        self.assertAlmostEqual(pt.falloff(0), 24 * 0.02 / 30.0, places=4)   # median of the last 3 laps
        self.assertIsNone(pt.learned())                    # no run has ended yet
        self.assertTrue(0.15 < pt.worn_points[0][0] < 0.25)   # 1% lost around stint lap 19-20
        for car_idx in range(3):                           # three runs cut short by a caution
            for lap in range(1, 12):
                pt.lap(car_idx + 6, 10, 10 + lap, 30.0, True, 0.01 * lap, 0.013 * lap, 30.0)
        pt.flush()
        left, right, runs = pt.learned()
        self.assertEqual(runs, 6)                          # the short runs are left out
        self.assertAlmostEqual(left, 0.29)                 # wear at the end of the full run (lap 29)
        self.assertAlmostEqual(right, 0.377)

    def test_one_slow_lap_in_traffic_is_not_worn(self):
        from aimod.pace import PaceTracker
        pt = PaceTracker(worn_pct=1.0)
        for lap in range(2, 16):                       # flat 30.0 s, one 30.6 s lap stuck behind a lapped car
            pt.lap(0, 0, lap, 30.6 if lap == 9 else 30.0, True, 0.01 * lap, 0.012 * lap, 30.0)
        self.assertEqual(pt.samples, [])

    def test_dirty_and_outliers_ignored(self):
        from aimod.pace import PaceTracker
        pt = PaceTracker()
        pt.lap(0, 10, 11, 31.0, True, 0, 0, 30)            # out-lap
        pt.lap(0, 10, 12, 30.0, False, 0, 0, 30)           # caution in the lap
        pt.lap(0, 10, 13, 60.0, True, 0, 0, 30)            # slowed for a wreck
        self.assertIsNone(pt.pace(0))

    def test_tire_factor(self):
        from aimod.pace import tire_factor
        self.assertEqual(tire_factor(None, None, 30), 1.0)
        self.assertAlmostEqual(tire_factor(-0.1, 0.002, 30), 0.6)   # keeping pace: less need
        self.assertGreater(tire_factor(0.6, 0.03, 30), 1.3)         # 2% slower and falling off

    def test_keeping_lead_pack_pace_from_the_back(self):
        eng = StrategyEngine(random.Random(1))
        eng.lap_time = 30.0
        infos = {i: CarInfo(i + 1, 0, 50 - i * 0.01, i * 0.5, 0.5) for i in range(12)}
        for i in range(12):
            for lap in range(2, 6):
                eng.pace.lap(i, 0, lap, 30.0 + (0.5 if 5 <= i < 11 else 0.0), True, 0.1, 0.1, 30.0)
        info = eng.compute_pace(race([car(i) for i in range(12)]), infos, StrategyParams(lead_pack_gap=2.0))
        self.assertTrue(info[11]["keeping_lead"])                # P12, 5.5 s back, lead-pack pace
        self.assertFalse(info[7]["keeping_lead"])                # P8 is 0.5 s a lap slower


class TeamTests(unittest.TestCase):
    def test_teammate_follows_lead_car_under_caution(self):
        eng = StrategyEngine(random.Random(1))
        eng.rng.random = lambda: 0.0
        lead, mate = car(1, team=3, number=48), car(2, team=3, number=5)
        eng.teams = {3: [lead, mate]}
        eng.state(1).decision = True
        infos = {1: CarInfo(2, 0, 0, 1.0, 0.3), 2: CarInfo(9, 0, 0, 6.0, 0.3)}
        st = eng.state(2)
        st.green_gap = 6.0
        res = eng.follow_call(mate, st, infos[2], infos, race([lead, mate]), StrategyParams(lead_pack_gap=2.0))
        self.assertEqual(res, (True, "follows teammate #48"))

    def test_teammates_pick_different_plans(self):
        counts = {"short-pit": 0, "split": 0, "stretch": 0}
        for seed in range(300):
            eng = StrategyEngine(random.Random(seed))
            eng._team_styles = ["split", "split"]
            counts[eng.choose_style(None, 50, 0.0, StrategyParams())] += 1
        self.assertLess(counts["split"], 60)

    def test_teammates_pit_together_at_drafting_tracks(self):
        eng = StrategyEngine(random.Random(1))
        eng.rng.random = lambda: 0.0
        snap = race([car(0, team=2, make="Ford"), car(1, team=2, make="Chevrolet")])
        snap.drafting = True
        infos = {0: CarInfo(1, 0, 0, 0.0, 0.3), 1: CarInfo(5, 0, 0, 1.5, 0.3)}
        eng.state(0).planned_lap, eng.state(1).planned_lap = 20, 17
        eng.drafting_partners(snap.cars[0], eng.state(0), 16, infos, snap, StrategyParams(oem_gap=1.0))
        self.assertEqual(eng.state(0).planned_lap, 17)
        self.assertIn("teammate #1", eng.state(0).plan_note)


class SessionResetTests(unittest.TestCase):
    def make(self):
        from types import SimpleNamespace
        from aimod.app import App
        ns = SimpleNamespace(_last_leader=0, _last_clock=0.0, _drop_ticks=0, _last_session=None, _last_lap_src=None,
                             _last_laps=0.0)
        ns.reason = lambda snap, src: App._new_session_reason(ns, snap, src)
        return ns

    def snap(self, laps, clock=100.0, n=20):
        r = race([car(i, laps_done=laps) for i in range(n)], lap=laps + 1, total=200)
        r.session_time = clock
        return r

    def step(self, ns, snap, src="game"):
        why = ns.reason(snap, src)
        ns._last_session, ns._last_laps, ns._last_lap_src = snap.session, snap.laps_done, src
        return why

    def test_new_race_without_restarting_nr2003(self):
        ns = self.make()
        for lap in range(150, 167):
            self.assertEqual(self.step(ns, self.snap(lap, clock=3000 + lap)), "")
        whys = [self.step(ns, self.snap(0, clock=3200), src="player car") for _ in range(10)]
        self.assertTrue(any(whys), whys)                     # leader's laps 166 -> 0, held for a second

    def test_one_bad_read_does_not_clear(self):
        ns = self.make()
        for lap in range(40, 45):
            self.step(ns, self.snap(lap, clock=1000 + lap))
        self.assertEqual(self.step(ns, self.snap(0, clock=1046)), "")   # one tick of zeros
        self.assertEqual(self.step(ns, self.snap(45, clock=1047)), "")
        self.assertEqual(self.step(ns, self.snap(0, clock=1048, n=2)), "")   # cars unreadable

    def test_race_clock_restart(self):
        ns = self.make()
        self.step(ns, self.snap(1, clock=500))
        self.assertEqual(self.step(ns, self.snap(1, clock=12)), "race clock restarted")


class TireModelTests(unittest.TestCase):
    def model(self, d):
        from aimod.tiremodel import TireModel
        return TireModel(os.path.join(d, "tire_models.json"))

    def test_per_mod_and_track_storage_and_blend(self):
        with tempfile.TemporaryDirectory() as d:
            m = self.model(d)
            m.use("FCRD NCS22 v1", "Charlotte")
            self.assertFalse(m.ready)
            for _ in range(4):
                m.add_run(0.02, 20, practice=True)
            for t in (13.8, 14.1, 14.0):
                m.add_stop("F+L/R", t, 40, False, 29)
            self.assertAlmostEqual(m.slope, 0.02)
            m.save()
            m2 = self.model(d)
            m2.use("FCRD NCS22 v1", "Charlotte")
            self.assertAlmostEqual(m2.slope, 0.02)               # prior alone (4 runs) is enough
            self.assertAlmostEqual(m2.stop_time("4"), 14.0)
            m2.use("Cup12", "Charlotte")                        # other mod: nothing carried over
            self.assertFalse(m2.ready)

    def test_session_that_disagrees_overrides_prior(self):
        with tempfile.TemporaryDirectory() as d:
            m = self.model(d)
            m.use("mod", "t")
            for _ in range(8):
                m.add_run(0.02, 20, practice=False)
            m.save()
            m = self.model(d)
            m.use("mod", "t")
            for _ in range(3):
                m.add_run(0.08, 20, practice=True)               # 4x faster falloff: other physics/tires
            self.assertGreater(m.slope, 0.06)

    def test_service_classes(self):
        from aimod.tiremodel import service_class
        self.assertEqual([service_class(c) for c in ("F", "F+R", "L/R", "F+L/R", "D+F+L/R")],
                         ["F", "2", "4", "4", None])


class ValueTireTests(unittest.TestCase):
    def engine(self, slope=0.03):
        from aimod.tiremodel import TireModel
        m = TireModel(os.path.join(tempfile.gettempdir(), "aimod_test_tm.json"))
        m.use("mod", "track")
        for _ in range(4):
            m.add_run(slope, 20, practice=True)
        for code, t in (("F", 7.0), ("F+R", 9.5), ("F+L/R", 14.0)):
            for _ in range(3):
                m.add_stop(code, t, 40, False, 30)
        eng = StrategyEngine(random.Random(1), tire_model=m)
        eng.lap_time = 30.0
        return eng

    def test_many_laps_left_takes_four_few_laps_takes_none(self):
        eng = self.engine()
        st = eng.state(0)
        st.left_since = st.right_since = 0
        worn = car(0, wear_lf=0.3, wear_lr=0.3, wear_rf=0.4, wear_rr=0.4)
        p = StrategyParams(track_position_bias=50)
        self.assertEqual(eng.value_service(worn, st, 40, 50, False, p, rating=100)[0], FOUR_TIRES)
        self.assertEqual(eng.value_service(worn, st, 40, 3, True, p, rating=100)[0], FUEL_ONLY)

    def test_light_falloff_takes_two_right_side(self):
        eng = self.engine(slope=0.006)
        st = eng.state(0)
        st.left_since = st.right_since = 0
        worn = car(0, wear_lf=0.1, wear_lr=0.1, wear_rf=0.4, wear_rr=0.4)    # rights carry the wear
        svc = eng.value_service(worn, st, 40, 40, False, StrategyParams(), rating=100)[0]
        self.assertEqual(svc, RIGHT_SIDE)

    def test_model_not_ready_falls_back(self):
        eng = StrategyEngine(random.Random(1))
        self.assertIsNone(eng.value_service(car(0), eng.state(0), 40, 40, False, StrategyParams()))

    def test_runs_from_practice_feed_the_model(self):
        eng = self.engine()
        before = len(eng.tire_model.runs)
        for lap in range(2, 14):
            eng.pace.lap(0, 0, lap, 30 + lap * 0.03, True, 0.1, 0.1, 30)
        eng.pace.lap(0, 14, 15, 30, True, 0, 0, 30)               # new stint: previous run reported
        r = race([car(0)])
        r.session = 3                                            # practice
        eng.tick(r, StrategyParams())
        self.assertEqual(len(eng.tire_model.runs), before + 1)
        self.assertTrue(eng.tire_model.runs[-1]["practice"])


class PitRoadFlagGapTests(unittest.TestCase):
    def test_gap_in_the_pit_road_flag_is_not_a_new_stop(self):
        # Bristol night: the pit road flag drops between the two pit lane sections; the stop was closed there
        # and the rest logged as "came in without a request" plus a second stop
        eng = StrategyEngine(random.Random(1))
        eng.state(0).planned_lap = GAME_DECIDE
        p = StrategyParams(tank_laps=160)
        events, exits = [], []
        seq = [  # t, pitting, on pit road, timer, speed, dist
            (10, 1, False, 0.0, 30, 100), (12, 1, True, 0.0, 20, 150), (20, 1, True, 5.0, 0.0, 200),
            (30, 0, True, 10.1, 10, 210), (34, 0, False, 10.1, 20, 300), (38, 0, False, 10.1, 20, 420),
            (41, 0, True, 10.1, 20, 480), (46, 0, True, 10.1, 20, 560), (48, 0, False, 10.1, 40, 600),
            (60, 0, False, 10.1, 60, 1100)]
        for t, pit, road, timer, spd, dist in seq:
            r = race([car(0, pitting=pit, on_pit_road=road, pit_timer=timer, speed=spd, lap_dist=dist)], flag=CAUTION)
            r.session_time = t
            res = eng.tick(r, p)
            events += res.events
            exits += res.exits
        self.assertEqual(len(events), 1)
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0].t_pit_out, 48)

    def run_seq(self, seq, laps_done=31):
        eng = StrategyEngine(random.Random(1))
        eng.state(0).planned_lap = GAME_DECIDE
        p = StrategyParams(tank_laps=160)
        events, exits = [], []
        for t, pit, road, timer, spd, dist in seq:
            r = race([car(0, pitting=pit, on_pit_road=road, pit_timer=timer, speed=spd, lap_dist=dist,
                          laps_done=laps_done if t >= 12 else laps_done - 2)], flag=CAUTION)
            r.session_time = t
            res = eng.tick(r, p)
            events += res.events
            exits += res.exits
        return events, exits

    def test_held_at_pit_exit_is_not_a_second_stop(self):
        # Bristol 11-02 #33: served in the stall, then held at the pit exit until the field lined up; the stop was
        # closed 25 s after service and the hold logged as "came in without a request" (0 % fuel, a few seconds)
        seq = [(10, 1, False, 0.0, 30, 100), (12, 1, True, 0.0, 20, 150), (20, 1, True, 5.0, 0.0, 200),
               (30, 0, True, 10.1, 10, 210), (33, 0, True, 10.1, 15, 400)]
        seq += [(t, 0, True, 10.1, 0.0, 450) for t in range(36, 140, 2)]          # held at the exit for 100 s
        seq += [(t, 1 if t == 146 else 0, True, 10.1, 0.0, 450) for t in (142, 144, 146, 148)]  # a request too
        seq += [(150, 0, True, 10.1, 15, 470), (154, 0, False, 10.1, 30, 600), (170, 0, False, 10.1, 60, 1300)]
        events, exits = self.run_seq(seq)
        self.assertEqual(len(events), 1)
        self.assertEqual(len(exits), 1)
        self.assertGreaterEqual(exits[0].t_pit_out, 150)                     # released, not closed at t=55
        self.assertNotIn("without a request", exits[0].note)

    def test_sitting_in_the_box_after_service_is_not_a_second_stop(self):
        # Bristol 11-02 #33 as the user saw it: four tires, then sat in his pit box another ~40 s
        seq = [(10, 1, False, 0.0, 30, 100), (12, 1, True, 0.0, 20, 150), (20, 1, True, 5.0, 0.0, 200),
               (34, 0, True, 14.4, 0.0, 200)]                                   # service over, request cleared
        seq += [(t, 0, True, 14.4, 0.0, 200) for t in range(36, 76, 2)]        # still in the box 40 s
        seq += [(78, 0, True, 14.4, 15, 300), (82, 0, False, 14.4, 30, 600), (96, 0, False, 14.4, 60, 1300)]
        events, exits = self.run_seq(seq)
        self.assertEqual(len(events), 1)
        self.assertEqual(len(exits), 1)
        self.assertAlmostEqual(exits[0].box_time, 14.4, delta=0.5)            # the wait is not service time

    def test_stop_lap_is_the_lap_it_reached_pit_road(self):
        # called under caution on lap 30 (pits closed), on pit road on lap 32: the log says 32
        seq = [(10, 1, False, 0.0, 30, 100), (12, 1, True, 0.0, 20, 150), (20, 1, True, 5.0, 0.0, 200),
               (30, 0, True, 10.1, 10, 210), (34, 0, False, 10.1, 30, 400), (50, 0, False, 10.1, 60, 1200)]
        events, exits = self.run_seq(seq, laps_done=31)
        self.assertEqual(events[0].lap, 32)

    def repair_writes(self, damage, steps, controlled=True, cond=0.0):
        eng = StrategyEngine(random.Random(1))
        eng.state(0).planned_lap = GAME_DECIDE
        p = StrategyParams(tank_laps=160)
        out = []
        for t, pit, road, timer, spd in steps:
            r = race([car(0, pitting=pit, on_pit_road=road, pit_timer=timer, speed=spd, lap_dist=100 + t,
                          laps_done=40, damage=damage, game_condition=cond, controlled=controlled)], flag=CAUTION)
            r.session_time = t
            res = eng.tick(r, p)
            flag = any(w.offset == mm.OFF_REPAIR_FLAG and w.value == 1 for w in res.writes)
            plan = any(w.offset == mm.OFF_PLAN_REPAIR and w.value == 1 for w in res.writes)
            self.assertEqual(flag, plan)              # the planner copies the flag from the plan byte: set both
            out.append(flag)
        return out, eng

    def test_damaged_car_gets_the_repair_flag_on_pit_road_before_its_box(self):
        # NR2003 clears the crew's repair flag as a car enters pit road under caution: caution stops never repaired
        steps = [(10, 1, False, 0.0, 30), (12, 1, True, 0.0, 20), (14, 1, True, 0.0, 15),
                 (20, 1, True, 5.0, 0.0), (22, 1, True, 7.0, 0.0)]
        out, eng = self.repair_writes(0.865, steps)
        self.assertEqual(out, [False, True, True, True, False])          # on pit road until it's seen in its box
        self.assertTrue(eng.board[-1].damage)                             # logged as a D+ stop

    def test_no_repair_flag_for_light_damage_or_the_player(self):
        steps = [(10, 1, False, 0.0, 30), (12, 1, True, 0.0, 20)]
        self.assertEqual(self.repair_writes(0.1, steps)[0], [False, False])
        self.assertEqual(self.repair_writes(0.9, steps, controlled=False)[0], [False, False])
        self.assertEqual(self.repair_writes(0.0, steps, cond=0.6)[0], [False, True])   # car condition counts

    def test_no_pit_request_rewritten_on_pit_road(self):
        # #98 Bristol 11-02: AIMOD rewrote +8118=1 every caution tick; the write from a snapshot taken just before the
        # game cleared it at the end of service put it back - the car left its box still called in and pitted again
        eng = StrategyEngine(random.Random(1))
        st = eng.state(0)
        st.planned_lap = GAME_DECIDE
        p = StrategyParams(tank_laps=160)
        eng.race_started = True
        eng.caution_wall = -1e9
        seq = [(5, 0, False, 0.0, 30, 50), (10, 1, False, 0.0, 30, 100), (12, 1, True, 0.0, 20, 150),
               (20, 1, True, 5.0, 0.0, 200), (30, 1, True, 13.6, 0.0, 200), (31, 0, True, 13.6, 0.0, 200),
               (33, 0, True, 13.6, 15, 300), (40, 0, False, 13.6, 30, 600), (60, 0, False, 13.6, 30, 1200)]
        events, on_road_ones = [], []
        for t, pit, road, timer, spd, dist in seq:
            r = race([car(0, pitting=pit, on_pit_road=road, pit_timer=timer, speed=spd, lap_dist=dist,
                          laps_done=40, damage=1.028)], flag=CAUTION)          # a damage call, like #98's
            r.session_time = t
            res = eng.tick(r, p)
            events += res.events
            if road and t >= 20:                                        # from the box on
                on_road_ones += [w for w in res.writes if w.offset == mm.OFF_PITTING and w.value == 1]
        self.assertEqual(on_road_ones, [])
        self.assertEqual(len(events), 1)

    def test_unrequested_stop_after_leaving_pit_road_still_counts(self):
        eng = StrategyEngine(random.Random(1))
        st = eng.state(0)
        st.exit_wait = True
        p = StrategyParams(tank_laps=160)
        eng.tick(race([car(0, on_pit_road=False, speed=60, lap_dist=100, laps_done=40)], flag=CAUTION), p)
        self.assertFalse(st.exit_wait)


class TakeoverTests(unittest.TestCase):
    def test_car_sent_in_after_a_forgiven_phantom_penalty_becomes_an_aimod_stop(self):
        # Bristol #71: phantom penalty request, RaceControl forgave it, the car came in 14 s later anyway
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=160)
        st = eng.state(0)
        st.planned_lap, st.last_stop_lap = GAME_DECIDE, 5
        eng.tick(race([car(0, pitting=1, laps_done=30, game_penalty=True, on_pit_road=False, lap_dist=100)],
                      flag=CAUTION), p)
        res = eng.tick(race([car(0, pitting=0, laps_done=30, on_pit_road=False, lap_dist=300)], flag=CAUTION), p)
        self.assertEqual(len(res.cancels), 1)                              # withdrawn: not a stop
        eng.tick(race([car(0, pitting=0, laps_done=30, on_pit_road=False, lap_dist=900)], flag=CAUTION), p)
        res = eng.tick(race([car(0, pitting=0, laps_done=31, on_pit_road=True, speed=20, lap_dist=950)],
                            flag=CAUTION), p)
        self.assertIn(1, writes_for(res, 0, mm.OFF_PITTING))                # AIMOD requests it
        self.assertEqual(len(res.events), 1)
        self.assertIn("repair / tow stop", res.events[0].note)          # no rule showed it the black flag
        self.assertIn("AIMOD took the stop over", res.events[0].note)


class CautionGameRequestTests(unittest.TestCase):
    def _just_pitted(self):
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=60)
        eng.race_started = True
        st = eng.state(0)
        st.last_stop_lap, st.stops = 77, 2                    # pitted under green 3 laps ago, full tank
        cars = lambda **kw: [car(0, laps_done=80, fuel=95.0, fuel_cap=100.0, lap_dist=300, **kw)] +             [car(i, laps_done=80, lap_dist=300 + 20 * i) for i in range(1, 6)]
        eng.tick(race(cars(on_pit_road=False), flag=CAUTION, lap=81, total=200), p)
        self.assertIs(st.decision, False)                       # "pitted 3 lap(s) ago: stays out"
        return eng, p, st, cars

    def test_game_request_under_caution_does_not_override_stay_out(self):
        # Crabby / Lowe's lap 81: cars that had just pitted showed "stays out" and pitted anyway
        eng, p, st, cars = self._just_pitted()
        res = eng.tick(race(cars(pitting=1, on_pit_road=False), flag=CAUTION, lap=81, total=200), p)
        self.assertIn(0, writes_for(res, 0, mm.OFF_PITTING))
        self.assertNotIn(1, writes_for(res, 0, mm.OFF_PITTING))
        self.assertIs(st.decision, False)
        self.assertEqual([e for e in res.events if e.idx == 0], [])

    def test_kept_out_car_is_handed_to_the_pit_guard(self):
        eng, p, st, cars = self._just_pitted()
        res = eng.tick(race(cars(on_pit_road=False), flag=CAUTION, lap=81, total=200), p)
        self.assertIn(0, res.keep_out)
        res = eng.tick(race(cars(on_pit_road=False), lap=82, total=200), p)              # green: guard off
        self.assertEqual(res.keep_out, set())

    def test_pit_guard_clears_only_a_set_request(self):
        from aimod.game import Game
        mem = {}

        class PM:
            def read_bytes(self, a, n): return bytes([mem.get(a, 0)])
            def write_bytes(self, a, b, n): mem[a] = b[0]
        g = Game()
        g.pm, g.write_enabled = PM(), True
        a3 = mm.CAR_BLOCK_0 + 3 * mm.CAR_STRIDE + mm.OFF_PITTING
        mem[a3] = 1
        self.assertEqual(g.hold_off({3, 4}), 1)
        self.assertEqual(mem[a3], 0)
        self.assertEqual(g.hold_off({3, 4}), 0)

    def test_car_the_game_drives_onto_pit_road_gets_no_service(self):
        eng, p, st, cars = self._just_pitted()
        st.track_m = 5000.0                                     # came from the track
        res = eng.tick(race(cars(pitting=0, on_pit_road=True, speed=20), flag=CAUTION, lap=81, total=200), p)
        self.assertNotIn(1, writes_for(res, 0, mm.OFF_PITTING))
        self.assertIs(st.decision, False)
        self.assertEqual(writes_for(res, 0, mm.OFF_FUEL_REQ), [0])


class ExtraStopTests(unittest.TestCase):
    def test_joining_a_block_that_costs_a_stop_is_refused(self):
        # Daytona report: pitted lap 10, joined the Chevrolet block on lap 17 with 60% fuel, so a third stop
        # came on lap 24-25 in a 32-lap stage on a 17-lap tank
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=17, fuel_margin=1.5, stages=3, stage_ends=(32, 64))
        eng.update_stages(p, 100, 17)
        eng.lap_now, eng.lap_total = 17, 100
        self.assertTrue(eng.extra_stop(17, 24, 17, p))          # 15 laps to the stage end > 14.5 usable
        self.assertFalse(eng.extra_stop(17, 24, 23, p))         # one lap early still reaches the break
        self.assertFalse(eng.extra_stop(17, 24, 25, p))         # later is never an extra stop


class TakeoverExitTests(unittest.TestCase):
    def test_car_still_leaving_a_long_pit_lane_is_not_taken_over(self):
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=160)
        eng.state(0).planned_lap = GAME_DECIDE
        for t, dist in ((10, 100), (12, 900)):                             # on track
            r = race([car(0, on_pit_road=False, lap_dist=dist, speed=40)])
            r.session_time = t
            eng.tick(r, p)
        st = eng.state(0)
        st.track_m = 0.0                                                    # just left pit road after a stop
        r = race([car(0, on_pit_road=True, lap_dist=950, speed=20)])
        r.session_time = 14
        res = eng.tick(r, p)
        self.assertNotIn(1, writes_for(res, 0, mm.OFF_PITTING))


class V36Tests(unittest.TestCase):
    def test_stop_without_a_request_is_logged_with_the_tires_it_got(self):
        eng = StrategyEngine(random.Random(1))
        eng.state(0).planned_lap = GAME_DECIDE
        p = StrategyParams(tank_laps=60)
        worn = dict(wear_lf=0.3, wear_lr=0.3, wear_rf=0.3, wear_rr=0.3)
        events, exits = [], []
        seq = [(10, False, None, 60, worn), (20, True, 14.0, 20, worn), (25, True, 14.0, 0.0, worn),
               (26, True, 0.5, 0.0, worn), (27, True, 1.5, 0.0, worn), (38, True, 12.0, 0.0, worn),
               (40, True, 12.0, 8.0, dict(worn, wear_rf=0.0, wear_rr=0.0)),
               (45, False, 12.0, 60, dict(worn, wear_rf=0.0, wear_rr=0.0)),
               (75, False, 12.0, 60, dict(worn, wear_rf=0.0, wear_rr=0.0))]   # off pit road long enough
        for t, road, timer, spd, w in seq:
            r = race([car(0, pitting=0, on_pit_road=road, pit_timer=timer, speed=spd, **w)])
            r.session_time = t
            res = eng.tick(r, p)
            events += res.events
            exits += res.exits
        # no track history before pit road (AIMOD just attached): logged as the game's stop, tires read back
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].reason, "GAME")
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0].service, RIGHT_SIDE)
        self.assertEqual(eng.cars[0].stops, 1)

    def test_guard_leaves_a_car_on_pit_road_alone(self):
        eng = StrategyEngine(random.Random(1))
        st = eng.state(0)
        st.stops, st.last_stop_lap, st.planned_lap = 1, 9, GAME_DECIDE
        res = eng.tick(race([car(0, pitting=1, laps_done=10, on_pit_road=True)]), StrategyParams(tank_laps=60))
        self.assertEqual(len(res.events), 1)

    def test_caution_call_made_after_the_restart_is_labelled_green(self):
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=60, min_stop_gap=0)
        st = eng.state(0)
        st.planned_lap, st.last_stop_lap = GAME_DECIDE, 5
        eng.tick(race([car(0, laps_done=40)], lap=41), p)
        st.decision, st.note = True, "free stop"
        eng.tick(race([car(0, laps_done=40)], flag=CAUTION, lap=41), p)   # called in, never reaches pit road
        self.assertIsNotNone(st.caution_call)
        eng.tick(race([car(0, laps_done=42)], lap=43), p)                 # green
        res = eng.tick(race([car(0, pitting=1, laps_done=43)], lap=44), p)  # the game brings it in a lap later
        self.assertEqual([e.reason for e in res.events], ["GAME"])     # a stop under green is a green stop
        self.assertIn("after the restart", res.events[0].note)

    def test_lead_lap_cars_take_fewer_free_stops(self):
        def count(me):
            n = 0
            for seed in range(200):
                eng = StrategyEngine(random.Random(seed))
                for i in range(30):
                    eng.state(i)
                st = eng.state(0)
                st.fuel_state = "SHORT ON FUEL"
                n += bool(eng.caution_fuel_call(st, 31, 150, StrategyParams(tank_laps=60), me)[0])
            return n
        lapped = count(CarInfo(25, 1, 0, 0, 0.5))
        leader = count(CarInfo(2, 0, 0, 0, 0.5))
        mid = count(CarInfo(15, 0, 0, 0, 0.5))
        self.assertGreater(lapped, mid)
        self.assertGreater(mid, leader)

    def test_front_runners_rarely_answer_an_undercut(self):
        def reacts(rank):
            n = 0
            for seed in range(300):
                eng = StrategyEngine(random.Random(seed))
                for i in range(30):
                    eng.state(i)
                eng.lap_now = 150
                st = eng.state(0)
                st.planned_lap, st.window, st.style, st.green_gap = 153, (142, 179), "split", 1.0
                eng.green_stops = [(5, 24, 150, 1.6, 0)]
                eng.react_to_stops(car(0), st, 150, CarInfo(rank, 0, 0, 1.0, 0.3), StrategyParams())
                n += st.planned_lap == 151
            return n
        self.assertGreater(reacts(18), 2 * reacts(3))

    def test_race_control_wave_lines(self):
        from aimod.wavelog import parse_line
        self.assertEqual(parse_line("02:30:16 lap 51 flag 2: wave around: #37 stayed out ahead of #5 (first lead "
                                    "lap car on track), 1.99 laps behind the leader -> around the pace car"),
                         (51, [37]))
        self.assertEqual(parse_line("12:00:00 lap 112 flag 2: wave around: #88 stayed out in front of every lead lap "
                                    "car on track (first: #47), 1.95 laps behind the leader -> around the pace car "
                                    "to the end of the lead lap cars"), (112, [88]))
        self.assertIsNone(parse_line("12:00:00 lap 112 flag 2: #9 stayed out ahead of the first lead lap car but is "
                                     "under penalty, not waved"))
        self.assertEqual(parse_line("12:54:18 lap 66 flag 2: first time by summary - waved: #9 #33; lead lap, sent "
                                    "around to their place: #33; under penalty: none; pitted: #17 #16"),
                         (66, [9, 33]))
        self.assertEqual(parse_line("13:23:59 lap 4 flag 5: first time by summary - waved: none; pitted: #33"),
                         (4, []))


class GamePlanPatchTests(unittest.TestCase):
    def test_aimod_plans_the_fuel_stop_when_game_plans_are_off(self):
        for off in (False, True):
            eng = StrategyEngine(random.Random(3))
            p = StrategyParams(tank_laps=60, game_plan_off=off)
            eng.plan_stop = lambda *a, **k: GAME_DECIDE              # car the game used to pit itself
            st = eng.state(0)
            st.last_stop_lap = 5
            eng.tick(race([car(0, laps_done=20)], lap=21, total=200), p)
            if off:
                self.assertIsInstance(st.planned_lap, int)
                self.assertTrue(5 + 58 - 3 <= st.planned_lap <= 5 + 58)
            else:
                self.assertEqual(st.planned_lap, GAME_DECIDE)

    def test_patch_bytes_and_optional_skip(self):
        from aimod.game import Game

        class PM:
            def __init__(self, mem):
                self.mem = mem

            def read_bytes(self, a, n):
                return self.mem.get(a, b"\0\0")

            def write_bytes(self, a, b, n):
                self.mem[a] = b
        g = Game.__new__(Game)
        g.applied_patches, g.patch_status, g.write_enabled = set(), "", False
        g.pm = PM({0x40B63F: b"\x75\x2b"})
        self.assertTrue(g.apply_patches(["no_game_plan_stops"]))
        self.assertEqual(g.pm.mem[0x40B63F], b"\xeb\x2b")
        self.assertIn("no_game_plan_stops", g.applied_patches)
        g.restore_patches()
        self.assertEqual(g.pm.mem[0x40B63F], b"\x75\x2b")
        g.pm = PM({0x40B63F: b"\x12\x34"})   # unknown exe build: optional patch skipped
        self.assertTrue(g.apply_patches(["no_game_plan_stops"]))
        self.assertTrue(g.write_enabled)
        self.assertNotIn("no_game_plan_stops", g.applied_patches)
        # caution repairs: test al,al -> xor al,al before the je that skips the pace-car repair halt
        g.pm = PM({0x404AE5: b"\x84\xc0"})
        self.assertTrue(g.apply_patches(["caution_repairs"]))
        self.assertEqual(g.pm.mem[0x404AE5], b"\x32\xc0")
        g.restore_patches()
        self.assertEqual(g.pm.mem[0x404AE5], b"\x84\xc0")

    def test_caution_repairs_patch_matches_both_known_exes(self):
        p = mm.PATCHES["caution_repairs"]
        for exe in (r"C:\Papyrus\NASCAR Racing 2003 Season\NR2003.exe",
                    r"C:\Papyrus\NASCAR Racing 2003 Season\NR2003.exe.Backup"):
            if not os.path.exists(exe):
                self.skipTest("NR2003 not installed here")
            with open(exe, "rb") as f:
                f.seek(p["addr"] - 0x400000 - 6)          # .text: file offset = VA - image base
                code = f.read(16)
            self.assertEqual(code[:6], bytes.fromhex("8a863d070000"))       # mov al, [esi+0x73D] (repair flag)
            self.assertEqual(code[6:8], p["original"])
            self.assertEqual(code[8:10], b"\x0f\x84")                        # je past the halt



class V38Tests(unittest.TestCase):
    def test_next_pit_kept_through_the_caution_until_it_pits(self):
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=60)
        st = eng.state(0)
        st.planned_lap, st.last_stop_lap = 50, 5
        res = eng.tick(race([car(0, laps_done=40)], lap=41), p)
        shown = res.rows[0].next_pit
        self.assertIn("LAP 50", shown)
        res = eng.tick(race([car(0, laps_done=40)], flag=CAUTION, lap=41), p)
        self.assertEqual(res.rows[0].next_pit, shown)

    def test_green_tire_stop_only_near_a_blowout_at_drafting_tracks(self):
        p = StrategyParams(tank_laps=60, wear_l=0.3, wear_r=0.3, value_tires=False)
        for drafting, wear, expect in ((True, 0.40, False), (True, 0.46, True), (False, 0.36, False),
                                       (False, 0.62, True), (False, 0.30, False)):
            eng = StrategyEngine(random.Random(1))
            eng.drafting = drafting
            st = eng.state(0)
            lap, why = eng.green_tire_call(car(0, wear_rf=wear, wear_rr=wear), st, 40, 30, 100, p)
            self.assertEqual(lap is not None, expect, (drafting, wear, why))


class BoardSessionTests(unittest.TestCase):
    def test_race_off_pit_road_only_in_a_race(self):
        from aimod.pitboard import payload
        self.assertEqual(payload([], 0.0, False, 5, 100, {})["mode"], "race_off")
        self.assertEqual(payload([], 0.0, False, 5, 100, {}, race=False)["mode"], "pit_road")
        self.assertEqual(payload([], 0.0, True, 5, 100, {})["mode"], "pit_road")


class TeamRulesTests(unittest.TestCase):
    def partners(self, rules, drafting=False):
        from aimod.strategy import TeamRules
        eng = StrategyEngine(random.Random(1))
        eng.rng.random = lambda: 0.0
        # car 0 (team 2, Ford): teammate #1 (Chevrolet) plans lap 17, same-make #2 (Ford) plans lap 18
        snap = race([car(0, team=2, make="Ford"), car(1, team=2, make="Chevrolet"), car(2, team=5, make="Ford")])
        snap.drafting = drafting
        infos = {0: CarInfo(3, 0, 0, 1.0, 0.3), 1: CarInfo(4, 0, 0, 1.5, 0.3), 2: CarInfo(2, 0, 0, 0.8, 0.3)}
        eng.state(0).planned_lap, eng.state(1).planned_lap, eng.state(2).planned_lap = 19, 17, 18
        p = StrategyParams(rules_draft=TeamRules(**rules), rules_other=TeamRules(**rules))
        eng.drafting_partners(snap.cars[0], eng.state(0), 16, infos, snap, p)
        return eng.state(0)

    def test_priority_and_team_pits(self):
        st = self.partners(dict(team_pits="together", make_pits=100))
        self.assertIn("teammate #1", st.plan_note)              # teammates first
        st = self.partners(dict(team_pits="together", make_pits=100, priority="make"))
        self.assertIn("#2 (Ford", st.plan_note)                 # same make first
        st = self.partners(dict(team_pits="split", make_pits=0))
        self.assertEqual(st.planned_lap, 19)                     # split: teammates do not line up
        st = self.partners(dict(team_pits="together", make_pits=100, priority="make_only"))
        self.assertNotIn("teammate", st.plan_note)

    def test_rules_follow_track_type(self):
        from aimod.strategy import TeamRules
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(rules_draft=TeamRules(team_pits="together"), rules_other=TeamRules(team_pits="split"))
        self.assertEqual(eng.rules(p, True).team_pits, "together")
        self.assertEqual(eng.rules(p, False).team_pits, "split")
        r = TeamRules.from_dict({"team_gap": "0", "priority": "bogus", "make_pits": 50.0})
        self.assertEqual((r.team_gap, r.priority, r.make_pits), (0.0, "team", 50))


class CautionFuelTests(unittest.TestCase):
    def test_low_fuel_that_cannot_reach_the_next_chance_pits_under_caution(self):
        eng = StrategyEngine(random.Random(1))
        st = eng.state(0)
        st.fuel_state = "SHORT ON FUEL"
        me = CarInfo(1, 0, 0, 0, 0.3)                            # the leader: free stops are rare for it
        pit, note, must = eng.caution_fuel_call(st, 22, 150, StrategyParams(tank_laps=60), me)
        self.assertTrue(pit and must, note)
        eng.stage_list, eng.lap_now = [60], 45                   # stage break 17 laps away: it reaches it
        pit, note, must = eng.caution_fuel_call(st, 22, 150, StrategyParams(tank_laps=60), me)
        self.assertFalse(must)


class StageTests(unittest.TestCase):
    def params(self, **kw):
        base = dict(tank_laps=60, stages=3, stage_by="laps", stage_ends=(80, 160), game_plan_off=True)
        base.update(kw)
        return StrategyParams(**base)

    def test_stage_ends_by_laps_or_percent(self):
        eng = StrategyEngine(random.Random(1))
        eng.update_stages(self.params(), 240, 10)
        self.assertEqual(eng.stage_list, [80, 160])
        eng.update_stages(self.params(stage_by="percent", stage_ends=(25, 50)), 200, 10)
        self.assertEqual(eng.stage_list, [50, 100])
        eng.update_stages(self.params(stages=0), 200, 10)
        self.assertEqual(eng.stage_list, [])
        eng.update_stages(self.params(), 240, 85)               # ran past stage 1 under green
        self.assertEqual(eng.next_stage_end(), 160)

    def plan(self, car_lap, last_stop, rank=3, seed=1, **kw):
        eng = StrategyEngine(random.Random(seed))
        p = self.params(**kw)
        eng.update_stages(p, 240, car_lap)
        eng.lap_now, eng.lap_total = car_lap, 240
        st = eng.state(0)
        st.last_stop_lap = last_stop
        e = eng.next_stage_end()
        lap = eng.plan_stage(st, p, 80, car_lap, 0, e, CarInfo(rank, 0, 0, 1.0, 0.3), 0.0, 60, 240 - car_lap)
        return eng, st, lap

    def test_short_stage_waits_for_the_break(self):
        from aimod.strategy import STAGE_BREAK
        eng, st, lap = self.plan(30, 25)                         # fuel to lap 83 >= 80 + 2
        self.assertEqual(lap, STAGE_BREAK)
        self.assertIn("stage 1 break", st.plan_note)

    def test_long_stage_stop_reaches_the_break(self):
        for seed in range(20):
            eng, st, lap = self.plan(10, 0, seed=seed, stage_ends=(100, 160))   # 100-lap stage, 60-lap tank
            self.assertIsInstance(lap, int)
            self.assertGreaterEqual(lap + 60 - 2, 102)          # after the stop it reaches the break
            self.assertLessEqual(lap, 59)                       # before the fuel runs out
            self.assertIn("stage 1", st.plan_note)

    def test_flip_only_outside_the_top_and_with_fuel_for_the_next_stage(self):
        flips = sum(isinstance(self.plan(70, 40, rank=20, seed=k, flip_pct=100)[2], int) for k in range(40))
        self.assertGreater(flips, 20)
        self.assertEqual(sum(isinstance(self.plan(70, 40, rank=3, seed=k, flip_pct=100)[2], int)
                             for k in range(40)), 0)            # top cars race for stage points
        self.assertEqual(sum(isinstance(self.plan(70, 40, rank=20, seed=k, flip_pct=0)[2], int)
                             for k in range(40)), 0)
        for k in range(40):
            eng, st, lap = self.plan(70, 40, rank=20, flip_pct=100, seed=k)
            if isinstance(lap, int):
                self.assertTrue(74 <= lap <= 78, lap)
        # stage 2 ends lap 136: from the break a tank reaches it, from a flip it would not -> no flip
        self.assertEqual(sum(isinstance(self.plan(70, 40, rank=20, seed=k, flip_pct=100,
                                                  stage_ends=(80, 136))[2], int) for k in range(40)), 0)

    def test_stage_break_caution(self):
        eng = StrategyEngine(random.Random(1))
        p = self.params()
        eng.update_stages(p, 240, 81)
        eng.lap_now, eng.lap_total = 81, 240
        eng.stage_break_start(p, 81)
        self.assertEqual(eng.stage_break, 80)
        self.assertEqual(eng.next_stage_end(), 160)
        flipped, normal = eng.state(0), eng.state(1)
        flipped.flipped = 80
        snap = race([], flag=CAUTION)
        infos = {0: CarInfo(5, 0, 0, 1.0, 0.3), 1: CarInfo(6, 0, 0, 1.0, 0.3)}
        eng.caution_decision(car(0), flipped, infos[0], infos, snap, p, True, 160, 50, 50, fuel_left=50)
        self.assertFalse(flipped.decision)
        self.assertIn("flipped stage 1", flipped.note)
        eng.caution_decision(car(1), normal, infos[1], infos, snap, p, True, 160, 50, 50, fuel_left=20)
        self.assertTrue(normal.decision)
        self.assertIn("stage 1 break", normal.note)

    def test_early_caution_ends_the_stage(self):
        eng = StrategyEngine(random.Random(1))
        p = self.params(stage_early=3)
        eng.update_stages(p, 240, 78)
        eng.stage_break_start(p, 78)
        self.assertEqual(eng.stage_break, 80)
        eng = StrategyEngine(random.Random(1))
        p = self.params(stage_early=0)
        eng.update_stages(p, 240, 78)
        eng.stage_break_start(p, 78)
        self.assertIsNone(eng.stage_break)

    def test_flip_stop_then_stays_out_in_a_full_tick_run(self):
        eng = StrategyEngine(random.Random(1))
        p = self.params(flip_pct=100)
        st = eng.state(0)
        st.last_stop_lap = 40
        eng.flip_lap = lambda *a, **k: 76                      # this car flips stage 1 on lap 76

        def cars(lap, pit=0, flag=GREEN):
            return race([car(0, laps_done=lap, pitting=pit)], flag=flag, lap=lap + 1, total=240)
        res = eng.tick(cars(70), p)
        self.assertEqual(st.planned_lap, 76)
        self.assertIn("flip", res.rows[0].next_pit)
        res = eng.tick(cars(75), p)                             # car lap 76: pits
        self.assertIn(1, writes_for(res, 0, mm.OFF_PITTING))
        eng.tick(cars(75, pit=1), p)
        self.assertEqual(st.flipped, 80)
        eng.tick(cars(75, pit=0), p)
        eng.tick(cars(80, flag=CAUTION), p)                     # stage 1 break
        self.assertEqual(eng.stage_break, 80)
        self.assertFalse(st.decision)


class LateCautionTests(unittest.TestCase):
    def test_request_still_pending_at_the_green_is_called_off(self):
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=60)
        st = eng.state(0)
        st.planned_lap, st.decision = GAME_DECIDE, True
        res = eng.tick(race([car(0, pitting=1, on_pit_road=False, laps_done=40)], flag=CAUTION, lap=41), p)
        self.assertEqual(len(res.events), 1)
        res = eng.tick(race([car(0, pitting=1, on_pit_road=False, laps_done=40)], lap=41), p)   # green
        self.assertEqual(len(res.cancels), 1)
        self.assertIn(0, writes_for(res, 0, mm.OFF_PITTING))
        self.assertEqual(st.stops, 0)

    def test_on_pit_road_at_the_green_keeps_its_stop(self):
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=60)
        eng.state(0).planned_lap, eng.state(0).decision = GAME_DECIDE, True
        eng.tick(race([car(0, pitting=1, on_pit_road=True, laps_done=40)], flag=CAUTION, lap=41), p)
        res = eng.tick(race([car(0, pitting=1, on_pit_road=True, laps_done=40)], lap=41), p)
        self.assertEqual(res.cancels, [])

    def test_no_new_calls_from_one_to_go(self):
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=60)
        st = eng.state(0)
        eng.tick(race([car(0, laps_done=40)], lap=41, total=200), p)
        r = race([car(0, laps_done=40)], flag=1, lap=41, total=200)
        r.session_time = 10.0
        eng.tick(r, p)                                       # pits open (caution lap 1), car fine on fuel
        st.last_stop_lap = 1                                 # ...then it would pit, but it is one to go now
        st.decision = None
        r = race([car(0, laps_done=55)], flag=5, lap=56, total=200)
        r.session_time = 100.0                               # pits open for more than a lap
        res = eng.tick(r, p)
        self.assertIn("ONE TO GO", res.rows[0].status)
        self.assertNotIn(1, writes_for(res, 0, mm.OFF_PITTING))

    def test_no_calls_in_the_first_seconds_of_a_caution(self):
        now = [1000.0]
        eng = StrategyEngine(random.Random(1), clock=lambda: now[0])
        p = StrategyParams(tank_laps=60)
        eng.state(0).last_stop_lap = 1
        eng.tick(race([car(0, laps_done=55)], lap=56, total=200), p)
        res = eng.tick(race([car(0, laps_done=55)], flag=0, lap=56, total=200), p)
        self.assertEqual(res.rows[0].status, "PITS CLOSED")
        now[0] += 5
        res = eng.tick(race([car(0, laps_done=55)], flag=0, lap=56, total=200), p)
        self.assertIn(1, writes_for(res, 0, mm.OFF_PITTING))

    def test_car_parked_on_pit_road_at_the_start_is_not_a_stop(self):
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=60)
        events = []
        for t in range(6):
            r = race([car(0, laps_done=1, on_pit_road=True, speed=0.0, pit_timer=0.0)], flag=CAUTION, lap=1)
            r.session_time = t
            events += eng.tick(r, p).events
        self.assertEqual(events, [])


class FuelTrackingTests(unittest.TestCase):
    def test_learns_each_cars_mileage_and_tank(self):
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=60)                     # preset says 60; this car really gets 38.3 / 1.0
        st = eng.state(0)
        fuel = 38.3
        for lap in range(5, 12):
            for k in range(3):                               # a few ticks per lap, fuel burns through the lap
                c = car(0, laps_done=lap, fuel=fuel - k * 0.3, fuel_cap=38.3)
                eng.learn_fuel(c, st, True)
            fuel -= 1.0
            eng.learn_fuel(car(0, laps_done=lap + 1, fuel=fuel, fuel_cap=38.3), st, True)
        self.assertAlmostEqual(st.fuel_rate, 1.0, places=2)
        tank, left = eng.fuel_model(car(0, fuel=20.0, fuel_cap=38.3), st, 30, p)
        self.assertAlmostEqual(tank, 38.3, places=1)
        self.assertAlmostEqual(left, 20.0, places=1)
        self.assertAlmostEqual(eng.fuel_from(st), 30 - 18.3, places=1)

    def test_caution_laps_and_refuelling_are_not_samples(self):
        eng = StrategyEngine(random.Random(1))
        st = eng.state(0)
        eng.learn_fuel(car(0, laps_done=10, fuel=30.0, fuel_cap=38.3), st, False)
        eng.learn_fuel(car(0, laps_done=11, fuel=29.6, fuel_cap=38.3), st, False)     # caution lap
        eng.learn_fuel(car(0, laps_done=11, fuel=38.3, fuel_cap=38.3), st, True)      # refuelled
        eng.learn_fuel(car(0, laps_done=12, fuel=37.3, fuel_cap=38.3), st, True)      # lap after the stop
        self.assertEqual(st.fuel_samples, 0)

    def test_no_fuel_reading_falls_back_to_laps(self):
        eng = StrategyEngine(random.Random(1))
        st = eng.state(0)
        st.last_stop_lap = 10
        tank, left = eng.fuel_model(car(0), st, 30, StrategyParams(tank_laps=60))
        self.assertEqual((tank, left), (60, 40))
        self.assertEqual(eng.fuel_from(st), 10)


class SuperspeedwayFuelSaveTests(unittest.TestCase):
    """Talladega 2026-10-27: a 30-lap tank and 56-lap stages. Without fuel saving each stage is three stints (two
    green stops); with the pack saving (driver behavior on) it is one stop, moved later as fuel is saved."""
    GAIN = 1 / (1 - 0.15 * 0.75) - 1                      # SAVE FUEL at strength 75

    def plan(self, gain, fuel_start=None, car_lap=2):
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=30, stages=3, stage_ends=(56, 118), game_plan_off=True)
        eng.update_stages(p, 189, 2)
        eng.lap_now, eng.lap_total = car_lap, 189
        eng.drafting, eng.fuel_save_gain = True, gain
        st = eng.state(0)
        st.fuel_start = fuel_start
        lap = eng.plan_stage(st, p, 90, car_lap, 0, 56, CarInfo(10, 0, 0, 3.0, 0.3), 0.0, 30, 189 - car_lap)
        return eng, st, lap, p

    def test_without_saving_the_stage_takes_two_stops(self):
        _, st, lap, _ = self.plan(0.0)
        self.assertEqual(st.borderline, 0)
        self.assertLessEqual(lap, 22)                         # a third of the stage: another stop follows

    def test_saving_plans_one_stop(self):
        _, st, lap, _ = self.plan(self.GAIN)
        self.assertGreater(st.borderline, 0)
        self.assertGreaterEqual(lap, 26)                      # late in the fuel window, saving fuel
        self.assertIn("saving fuel", st.plan_note)

    def test_saved_fuel_moves_the_stop_later(self):
        eng, st, lap, p = self.plan(self.GAIN)
        st.planned_lap, st.plan_tank = lap, 30
        st.fuel_start = 3.0                                   # 3 laps of fuel saved by lap 26
        self.assertIn("fuel saved", eng.plan_still_good(st, 26, 30, 7.0, 163, p))
        st.planned_lap = None
        _, st2, lap2, _ = self.plan(self.GAIN, fuel_start=3.0, car_lap=26)
        self.assertGreater(lap2, lap)

    def test_after_the_one_stop_it_waits_for_the_break(self):
        _, st, lap, _ = self.plan(self.GAIN, fuel_start=30.0, car_lap=31)
        self.assertEqual(lap, STAGE_BREAK)


class RaceLongPlanTests(unittest.TestCase):
    def test_long_stage_is_split_not_short_pitted(self):
        # stage 1 ends lap 84 on a 67-lap tank: split ~ halfway, short-pit earlier, stretch late in the window
        seen = {}
        for seed in range(60):
            eng = StrategyEngine(random.Random(seed))
            p = StrategyParams(tank_laps=67, stages=3, stage_ends=(84, 168), game_plan_off=True)
            eng.update_stages(p, 334, 2)
            eng.lap_now, eng.lap_total = 2, 334
            st = eng.state(0)
            lap = eng.plan_stage(st, p, 90, 2, 0, 84, CarInfo(10, 0, 0, 3.0, 0.3), 0.0, 67, 333)
            seen.setdefault(st.style, []).append(lap)
        self.assertTrue(all(36 <= x <= 50 for x in seen["split"]), seen["split"])
        self.assertGreaterEqual(min(seen["stretch"]), 48)        # a handful of laps past the split
        self.assertTrue(all(lap >= 86 - 67 + 2 for v in seen.values() for lap in v))   # still reaches the break

    def test_last_stop_takes_only_the_fuel_it_needs(self):
        eng = StrategyEngine(random.Random(1))
        eng.lap_now = 180
        p = StrategyParams(tank_laps=38.3, finish_fuel=3)          # 1 unit of fuel a lap
        lv, note = eng.fuel_level(car(0, fuel=5.0, fuel_cap=38.3), 21, p)   # 21 to go: needs ~19 more
        self.assertEqual(lv, 4)                                    # 1 can (~16) is short of 19 -> 1.5 cans
        self.assertIn("finish", note)
        self.assertEqual(eng.fuel_level(car(0, fuel=5.0, fuel_cap=38.3), 8, p)[0], 2)    # 8 to go: half a can
        self.assertEqual(eng.fuel_level(car(0, fuel=5.0, fuel_cap=38.3), 60, p)[0], 5)   # can't make it: full
        self.assertEqual(eng.fuel_level(car(0), 8, p)[0], 5)                              # no real fuel: full

    def test_fuel_added_per_level_is_learned(self):
        from aimod.strategy import PitEvent
        eng = StrategyEngine(random.Random(1))
        ev = PitEvent(0, 0, 1, "D1", 10, "FUEL ONLY", "PLANNED", True, 1)
        ev.fuel_level, ev.fuel_in = 3, 5.0
        eng.learn_fuel_add(ev, car(0, fuel=22.0, fuel_cap=38.3))
        self.assertAlmostEqual(eng.fuel_adds[3], 17.0 / 38.3)
    def test_every_car_gets_a_plan_when_the_game_does_not_pit(self):
        for seed in range(30):
            eng = StrategyEngine(random.Random(seed))
            st = eng.state(0)
            lap = eng.plan_stop(st, StrategyParams(tank_laps=60, game_plan_off=True), 0, 10, 191, 200, 60)
            self.assertIsInstance(lap, int)                  # rating 0 never "follows", still planned

    def test_plan_revised_on_mileage_and_dropped_when_it_makes_it(self):
        eng = StrategyEngine(random.Random(1))
        st = eng.state(0)
        p = StrategyParams(tank_laps=60)
        st.planned_lap, st.plan_tank, st.plan_note = 150, 60.0, "split stint"
        st.fuel_start = 100.0
        self.assertIn("mileage", eng.plan_still_good(st, 120, 55.0, 35.0, 80, p))
        self.assertEqual(eng.plan_still_good(st, 120, 60.0, 40.0, 80, p), "")
        self.assertIn("makes it", eng.plan_still_good(st, 120, 60.0, 40.0, 30, p))
        st.planned_lap = 170                                 # past the lap its fuel runs out
        self.assertIn("runs out", eng.plan_still_good(st, 120, 60.0, 40.0, 80, p))


class GreenTireValueTests(unittest.TestCase):
    def engine(self, own_slope, field=0.06):
        from aimod.pace import Stint
        eng = StrategyEngine(random.Random(1))
        eng.lap_time = 30.0
        eng._field_slope, eng._field_slope_t = field, 1e9          # field falloff 0.06 s/lap per lap
        stint = Stint(20)
        stint.laps = [(n, 30.0 + own_slope * n, 0.1, 0.1) for n in range(2, 14)]
        eng.pace.stints[0] = stint
        eng.pace_info = {0: {"falloff": own_slope * 12 / 30.0, "k": 1.0}}
        return eng

    def test_extra_green_stop_only_for_a_car_far_worse_than_the_field(self):
        p = StrategyParams(tank_laps=60, wear_l=0.3, wear_r=0.3, value_tires=False)
        c = car(0, wear_rf=0.36, wear_rr=0.36)
        eng = self.engine(0.08)                                   # 1.3x the field: no extra stop
        self.assertIsNone(eng.green_tire_call(c, eng.state(0), 40, 50, 100, p)[0])
        eng = self.engine(0.15)                                   # 2.5x the field, big loss: extra stop
        lap, why = eng.green_tire_call(c, eng.state(0), 40, 50, 100, p)
        self.assertEqual(lap, 41)
        self.assertIn("field's falloff", why)

    def test_undercut_and_overcut_move_the_stop_a_few_laps(self):
        from aimod.strategy import INTERMEDIATE
        p = StrategyParams(tank_laps=60, track_type=INTERMEDIATE)
        for own, early in ((0.09, True), (0.03, False)):
            for seed in range(10):
                eng = self.engine(own)
                eng.rng = random.Random(seed)
                st = eng.state(0)
                st.planned_lap, st.window, st.last_stop_lap = 50, (40, 58), 20
                eng.tire_management(car(0), st, 45, p)
                if early:
                    self.assertTrue(47 <= st.planned_lap <= 49, st.planned_lap)
                    self.assertIn("undercut", st.plan_note)
                else:
                    self.assertTrue(52 <= st.planned_lap <= 54, st.planned_lap)
                    self.assertIn("overcut", st.plan_note)
                before = st.planned_lap
                eng.tire_management(car(0), st, 46, p)            # once per run
                self.assertEqual(st.planned_lap, before)



class TireModelPriorTests(unittest.TestCase):
    def test_saved_model_without_a_slope_does_not_crash(self):
        from aimod.tiremodel import TireModel
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "tm.json")
            with open(path, "w") as f:
                f.write('{"mod|trk": {"slope": null, "runs": 3, "stops": {}, "green_loss": 25.0}}')
            m = TireModel(path)
            m.use("mod", "trk")
            for _ in range(3):
                m.add_run(0.02, 10, practice=True)
            self.assertAlmostEqual(m.slope, 0.02)
            self.assertTrue(m.ready)


class FlipFuelTests(unittest.TestCase):
    def test_flipping_car_fuels_for_the_next_stage_not_this_break(self):
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=38.3, stages=3, stage_ends=(80, 110), finish_fuel=3)   # 1 unit a lap
        eng.update_stages(p, 200, 76)
        eng.lap_now = 76
        st = eng.state(0)
        c = car(0, fuel=10.0, fuel_cap=38.3)
        lv, note = eng.fuel_level(c, 124, p, st)          # normal car: break at 80 is 6 laps away
        self.assertEqual(lv, 1)                            # a splash is plenty (10 units on board)
        st.flip_stage = 80                                 # flipping: stays out at 80, next chance lap 112
        lv, note = eng.fuel_level(c, 124, p, st)
        self.assertEqual(lv, 5)                            # needs ~39 laps: full
        st.flipped = 80                                    # already flipped (pitting again later): normal
        self.assertEqual(eng.fuel_level(c, 124, p, st)[0], 1)


class PreBreakCautionTests(unittest.TestCase):
    """Caution a few laps before a stage end (Charlotte: caution lap 76, stage end 84): a car that pits
    then fuels past the break and stays out at it - it does not stop twice 9 laps apart."""

    def engine(self):
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=68, stages=3, stage_ends=(84, 168), fuel_margin=2)
        eng.update_stages(p, 334, 78)
        eng.lap_now = 78
        return eng, p

    def test_caution_stop_before_the_break_fuels_past_it(self):
        eng, p = self.engine()
        st = eng.state(0)
        c = car(0, fuel=25.0, fuel_cap=62.5)
        self.assertLess(eng.fuel_level(c, 256, p, st, FUEL_ONLY)[0], 5)      # green: the break is reachable
        lv, note = eng.fuel_level(c, 256, p, st, FUEL_ONLY, caution=True)
        self.assertEqual(lv, 5)                                              # caution: full, past the break
        self.assertEqual(st.pre_break, 84)

    def test_far_from_the_break_fuels_as_before(self):
        eng, p = self.engine()
        eng.lap_now = 50
        st = eng.state(0)
        c = car(0, fuel=25.0, fuel_cap=62.5)
        eng.fuel_level(c, 284, p, st, FUEL_ONLY, caution=True)
        self.assertIsNone(st.pre_break)

    def stays(self, fuel_left, pre_break=84, wear=0.05):
        out = 0
        for k in range(50):
            eng, p = self.engine()
            eng.rng = random.Random(k)
            eng.lap_now = 86
            eng.stage_break = 84
            st = eng.state(0)
            st.stops, st.pre_break = 2, pre_break
            c = car(0, wear_lf=wear, wear_lr=wear, wear_rf=wear, wear_rr=wear)
            eng.stage_break_call(c, st, CarInfo(12, 0, 0, 5.0, 0.3), {}, p, 248, 0.0, 0.0, fuel_left)
            out += st.decision is False
        return out

    def test_car_that_pitted_just_before_stays_out_at_the_break(self):
        self.assertGreaterEqual(self.stays(60), 40)          # full 8 laps ago: same green stops either way
        self.assertEqual(self.stays(16, pre_break=None), 0)  # did not pit: 16 laps left, it pits
        self.assertEqual(self.stays(60, wear=0.5), 0)        # worn tires: pits

    def test_wave_log_tracks_cars_going_around(self):
        from aimod.wavelog import WaveLog
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "race_control.log")
            open(path, "w").close()
            w = WaveLog(path)
            w.poll()
            with open(path, "a") as f:
                f.write("15:05:03 lap 86 flag 2: waved #20 goes to the end of the line until it has passed the pace car\n"
                        "15:05:03 lap 86 flag 2: waved #8 goes to the end of the line until it has passed the pace car\n"
                        "15:05:44 lap 87 flag 2: waved #8 crossed the line after passing the pace car (lap gained on track); x\n")
            w.poll()
            self.assertTrue(w.is_going_around(20, 87))
            self.assertFalse(w.is_going_around(8, 87))
            self.assertFalse(w.is_going_around(20, 90))       # stale: forgotten
            with open(path, "a") as f:
                f.write("15:06:00 lap 87 flag 2: waved #3 goes to the end of the line until it has passed the pace car\n"
                        "15:06:00 lap 87 flag 2: waved #60 goes to the end of the line until it has passed the pace car\n"
                        "15:06:00 lap 87 flag 2: waved #77 goes to the end of the line until it has passed the pace car\n"
                        "15:06:00 lap 87 flag 2: waved #21 goes to the end of the line until it has passed the pace car\n"
                        "15:06:10 lap 87 flag 2: #3 dropped back behind the leader instead of going around the pace car\n"
                        "15:06:10 lap 87 flag 2: free pass: #77 did not get around the pace car in 45 s\n"
                        "15:06:10 lap 87 flag 2: waved #60 is pitting\n")
            w.poll()
            self.assertFalse(w.is_going_around(3, 87))
            self.assertFalse(w.is_going_around(77, 87))
            self.assertFalse(w.is_going_around(60, 87))
            self.assertTrue(w.is_going_around(21, 87))
            with open(path, "a") as f:
                f.write("15:07:00 lap 88 flag 7: green: #21 sent around (lead lap) ...\n")
            w.poll()
            self.assertFalse(w.is_going_around(21, 88))       # green clears every hold


class RaceControlStageTests(unittest.TestCase):
    def test_race_control_stage_ends_override_the_preset(self):
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(stages=3, stage_by="percent", stage_ends=(25, 50))
        eng.update_stages(p, 334, 10)
        self.assertEqual(eng.stage_list, [84, 167])
        eng.update_stages(p, 334, 10, (84, 204))
        self.assertEqual(eng.stage_list, [84, 204])
        self.assertEqual(eng.stage_source, "RaceControl")

    def test_reads_race_control_state_file(self):
        import aimod.game as g
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.txt")
            old, g.RACE_CONTROL_STATE = g.RACE_CONTROL_STATE, path
            try:
                with open(path, "wb") as f:
                    f.write(b"pits_closed=1\r\nstage_ends=84,204\r\n")       # Race Control 2.7.0 format
                self.assertEqual(g.race_control_stage_ends(), (84, 204))
                self.assertTrue(g.race_control_pits_closed())
                with open(path, "wb") as f:
                    f.write(b"pits_closed=0\r\n")                            # stage racing off
                self.assertIsNone(g.race_control_stage_ends())
                with open(path, "wb") as f:
                    f.write(b"pits_closed=1\r\nstage_ends=84,204\r\n")
                os.utime(path, (1, 1))                     # Race Control closed long ago: a stale file
                self.assertFalse(g.race_control_pits_closed())
                self.assertIsNone(g.race_control_stage_ends())
                os.remove(path)                            # never installed: no file
                self.assertFalse(g.race_control_pits_closed())
                self.assertIsNone(g.race_control_stage_ends())
            finally:
                g.RACE_CONTROL_STATE = old

    def test_four_stage_race_from_race_control(self):
        # Charlotte night: 4 stages at 25/50/75 % of 334 laps
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(stages=3, stage_ends=(84, 204))       # the preset is overridden
        eng.update_stages(p, 334, 10, (84, 167, 251))
        self.assertEqual(eng.stage_list, [84, 167, 251])
        self.assertEqual([eng.stage_no(e) for e in eng.stage_list], [1, 2, 3])
        eng.update_stages(p, 334, 170, (84, 167, 251))           # ran past 84 and 167 under green
        self.assertEqual(eng.next_stage_end(), 251)

    def test_stage_caution_thrown_on_the_last_lap_is_the_break(self):
        # Race Control throws it with the leader 75% round the stage's last lap: 83 done, on lap 84
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(stages=3)
        eng.update_stages(p, 334, 84, (84, 204))
        eng.stage_break_start(p, 84)
        self.assertEqual(eng.stage_break, 84)
        eng = StrategyEngine(random.Random(1))
        eng.update_stages(p, 334, 83, (84, 204))
        eng.stage_break_start(p, 83)                       # a lap earlier: a normal caution
        self.assertIsNone(eng.stage_break)


class BorderlineStageTests(unittest.TestCase):
    """Live Charlotte night: stage 2 ran 117 laps on a ~64-lap tank. With the mileage estimate a little low
    the planners chose two stops and short-pitted; the cars that ran to the fuel window made it on one."""

    def plan(self, tank, rank=8, seed=1):
        eng = StrategyEngine(random.Random(seed))
        eng.lap_now = 88
        p = StrategyParams(tank_laps=tank, game_plan_off=True, fuel_margin=2, stint_strategy=True)
        st = eng.state(0)
        st.last_stop_lap = 87
        lap = eng.plan_stop(st, p, 80, 88, 206 - 88, 206, tank, CarInfo(rank, 0, 0, 3.0, 0.3))
        return lap, st

    def test_a_few_laps_short_runs_to_the_fuel_window(self):
        lap, st = self.plan(60)                    # 2 x 57 usable = 114 < 119: 5 laps short of one stop
        self.assertEqual(st.borderline, 5)
        self.assertEqual(lap, st.window[1])        # the last lap the fuel allows, not a short-pit

    def test_clearly_two_stops_plans_normally(self):
        lap, st = self.plan(45)                    # far short: a normal two-stop plan
        self.assertEqual(st.borderline, 0)

    def test_leaders_rarely_short_pit(self):
        from aimod.strategy import SHORT_PIT
        def short_pits(rank):
            n = 0
            for k in range(300):
                eng = StrategyEngine(random.Random(k))
                eng._stage_plan = True
                n += eng.choose_style(CarInfo(rank, 0, 0, 1.0 if rank <= 5 else 9.0, 0.3), 80, 0.0,
                                      StrategyParams()) == SHORT_PIT
            return n
        self.assertLess(short_pits(1), 25)
        self.assertGreater(short_pits(20), 3 * short_pits(1))


class SaveGreenStopTests(unittest.TestCase):
    """Live Charlotte night: caution at lap 108, stage 2 ends at 167, tank ~64. Only 6 cars pitted; the leader
    was one of them and reached the break with no green stop, lapping the 31 cars that had to stop under green."""

    def calls(self, fuel_left, rank=1, n=100):
        out = 0
        for k in range(n):
            eng = StrategyEngine(random.Random(k))
            p = StrategyParams(tank_laps=64, fuel_margin=2, stages=4, stage_ends=(84, 167, 251))
            eng.update_stages(p, 334, 108)
            eng.lap_now = 108
            pit, note, must = eng.caution_fuel_call(eng.state(0), fuel_left, 227, p, CarInfo(rank, 0, 0, 0.0, 0.3))
            out += bool(pit)
        return out

    def test_takes_the_stop_that_saves_a_green_stop(self):
        self.assertGreaterEqual(self.calls(42), 80)           # 42 laps left, 61 to the break: fill now
        self.assertGreaterEqual(self.calls(42, rank=20), 80)

    def test_no_saving_no_forced_stop(self):
        self.assertLess(self.calls(63), 20)                    # already reaches the break: no reason to stop


class FreeFuelTests(unittest.TestCase):
    def test_tire_stops_take_the_fuel_that_fits_in_the_change(self):
        from aimod.strategy import FOUR_TIRES, RIGHT_SIDE
        eng = StrategyEngine(random.Random(1))
        eng.lap_now = 180
        p = StrategyParams(tank_laps=38.3, finish_fuel=3)
        c = car(0, fuel=20.0, fuel_cap=38.3)                   # 8 to go: needs nothing more
        self.assertEqual(eng.fuel_level(c, 8, p, None, FUEL_ONLY)[0], 1)
        lv, note = eng.fuel_level(c, 8, p, None, FOUR_TIRES)
        self.assertEqual(lv, 4)                                # 1.5 cans with 4 tires
        self.assertIn("fits in the 4 tires change", note)
        self.assertEqual(eng.fuel_level(c, 8, p, None, RIGHT_SIDE)[0], 3)
        self.assertEqual(eng.fuel_level(c, 8, StrategyParams(tank_laps=38.3, free_fuel=False), None,
                                        FOUR_TIRES)[0], 1)

    def test_tire_stop_always_takes_fuel_unless_nearly_full(self):
        p = StrategyParams(tank_laps=60)
        self.assertTrue(StrategyEngine.needs_fuel(40, 10, "4 TIRES", p))      # makes it, still fuels
        self.assertFalse(StrategyEngine.needs_fuel(59.5, 10, "4 TIRES", p))   # tank already full
        p.free_fuel = False
        self.assertFalse(StrategyEngine.needs_fuel(40, 10, "4 TIRES", p))     # old behaviour


class FlipLapDownTests(unittest.TestCase):
    def test_no_flip_where_a_green_stop_costs_a_lap(self):
        def flips(lap_time):
            n = 0
            for k in range(40):
                eng = StrategyEngine(random.Random(k))
                eng.lap_time = lap_time                        # green stop costs 25 s (default)
                p = StrategyParams(tank_laps=60, stages=3, stage_ends=(80, 160), flip_pct=100)
                eng.update_stages(p, 240, 70)
                eng.lap_now, eng.lap_total = 70, 240
                st = eng.state(0)
                st.last_stop_lap = 40
                n += isinstance(eng.plan_stage(st, p, 80, 70, 0, 80, CarInfo(20, 0, 0, 1.0, 0.3), 0.0, 60, 170), int)
            return n
        self.assertEqual(flips(22.0), 0)                       # 25 s stop > 22 s lap: would go a lap down
        self.assertGreater(flips(40.0), 10)


class ScoringTableTests(unittest.TestCase):
    def test_lap_time_and_order_from_the_games_crossing_times(self):
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=60)
        # both cars seen crossing lap 11 in the same tick (time acceleration): #2 really crossed first
        eng.tick(race([car(0, laps_done=10, cross_ms=300000, prev_cross_ms=268000),
                       car(1, laps_done=10, cross_ms=300500, prev_cross_ms=268400)]), p)
        res = eng.tick(race([car(0, laps_done=11, cross_ms=332100, prev_cross_ms=300000),
                             car(1, laps_done=11, cross_ms=331900, prev_cross_ms=300500)], lap=12), p)
        self.assertAlmostEqual(eng.lap_times[0], 32.1)
        self.assertAlmostEqual(eng.lap_times[1], 31.4)
        order = {r.idx: r.pos for r in res.rows}
        self.assertEqual((order[1], order[0]), (1, 2))


class FuelDistanceTests(unittest.TestCase):
    def test_late_samples_under_time_acceleration_still_give_the_true_rate(self):
        eng = StrategyEngine(random.Random(1))
        eng.track_len = 2400.0
        st = eng.state(0)
        rate = 0.86
        prog = 10.0
        offsets = [0.05, 0.40, 0.10, 0.35, 0.02, 0.30, 0.15, 0.25, 0.08, 0.38]   # seen 2-40% past the line
        for k, off in enumerate(offsets):
            p_now = 10 + k + off
            c = car(0, laps_done=10 + k, lap_dist=off * 2400, fuel=60.0 - (p_now - prog) * rate, fuel_cap=62.47)
            eng.learn_fuel(c, st, True)
        self.assertAlmostEqual(st.fuel_rate, rate, places=3)


class WornByFalloffTests(unittest.TestCase):
    def test_wave_kept_when_tires_read_100_but_lap_times_hold_up(self):
        eng, st, infos, snap = WaveAroundTests().setup(car_frac=0.3)
        eng.pace_info = {9: {"falloff": 0.008, "k": 1.0}}               # under 1% slower: fine
        eng.caution_decision(car(9), st, infos[9], infos, snap, StrategyParams(tank_laps=60), True, 50, 100, 100,
                             fuel_left=40)
        self.assertFalse(st.decision)

    def test_no_green_tire_call_without_tire_sets(self):
        eng = StrategyEngine(random.Random(1))
        eng.lap_time = 30.0
        eng.pace_info = {0: {"falloff": 0.06, "k": 1.0}}
        st = eng.state(0)
        st.left_pairs_used = st.right_pairs_used = 5
        p = StrategyParams(tank_laps=60, wear_l=0.3, wear_r=0.3, value_tires=False, max_tire_sets=5)
        self.assertIsNone(eng.green_tire_call(car(0, wear_rf=0.5, wear_rr=0.5), st, 40, 50, 100, p)[0])


class Phase1RoadmapTests(unittest.TestCase):
    def test_no_stage_roadmap_counts_back_from_the_finish(self):
        # 334 laps, 71-lap tank, 3-lap cushion: usable 68 -> 5 stints; split-style crews pit when the
        # remaining stints are all full tanks (lap 334 - 4 x 68 = 62), later stops every 68 laps
        laps = []
        for seed in range(30):
            eng = StrategyEngine(random.Random(seed))
            st = eng.state(0)
            p = StrategyParams(tank_laps=71, fuel_margin=3, game_plan_off=True)
            lap = eng.plan_stop(st, p, 90, 1, 334, 334, 71, anchor="roadmap")
            if st.style == "split":
                laps.append(lap)
        self.assertTrue(laps and all(66 <= x <= 68 for x in laps), laps)     # 334 - 4 x 67 usable laps

    def test_every_stop_of_a_long_race_stays_planned_to_the_finish(self):
        eng = StrategyEngine(random.Random(3))
        st = eng.state(0)
        p = StrategyParams(tank_laps=60, fuel_margin=2, game_plan_off=True)
        lap, stops = 1, []
        while True:
            planned = eng.plan_stop(st, p, 90, lap, 200 - lap + 1, 200, 60, anchor="roadmap")
            if not isinstance(planned, int):
                break
            stops.append(planned)
            st.last_stop_lap, lap = planned, planned
        stints = [b - a for a, b in zip([0] + stops, stops + [200])]
        self.assertEqual(len(stops), 3, stops)                   # 200 laps / 58 usable -> 4 stints
        self.assertTrue(all(s <= 58 for s in stints), stints)

    def test_stage_split_exactly_in_half(self):
        laps = []
        for seed in range(40):
            eng = StrategyEngine(random.Random(seed))
            p = StrategyParams(tank_laps=67, stages=3, stage_ends=(84, 168), game_plan_off=True)
            eng.update_stages(p, 334, 2)
            eng.lap_now, eng.lap_total = 2, 334
            st = eng.state(0)
            lap = eng.plan_stage(st, p, 95, 2, 0, 84, CarInfo(10, 0, 0, 3.0, 0.3), 0.0, 67, 333)
            if st.style == "split":
                laps.append(lap)
        self.assertTrue(laps and all(41 <= x <= 45 for x in laps), laps)     # (86 / 2) +- a lap


class Phase1TrackTypeTests(unittest.TestCase):
    def test_short_track_no_reactions_and_no_tire_stops(self):
        from aimod.strategy import SHORT_TRACK
        eng = StrategyEngine(random.Random(1))
        eng.rng.random = lambda: 0.0
        eng.lap_now = 150
        st = eng.state(0)
        st.planned_lap, st.window, st.style, st.green_gap = 165, (142, 179), "split", 1.0
        eng.green_stops = [(5, 24, 150, 1.6, 0)]
        eng.react_to_stops(car(0), st, 150, CarInfo(12, 0, 0, 1.0, 0.3), StrategyParams(track_type=SHORT_TRACK))
        self.assertEqual(st.planned_lap, 165)
        eng.lap_time = 20.0
        eng.pace_info = {0: {"falloff": 0.06, "k": 1.0}}
        p = StrategyParams(tank_laps=60, wear_l=0.3, wear_r=0.3, value_tires=False, track_type=SHORT_TRACK)
        self.assertIsNone(eng.green_tire_call(car(0, wear_rf=0.5, wear_rr=0.5), st, 150, 50, 100, p)[0])

    def test_road_course_plans_to_the_finish_not_the_stage(self):
        from aimod.strategy import ROAD_COURSE
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=30, stages=3, stage_ends=(20, 40), game_plan_off=True, track_type=ROAD_COURSE)
        st = eng.state(0)
        res = eng.tick(race([car(0, laps_done=5)], lap=6, total=90), p)
        res = eng.tick(race([car(0, laps_done=6)], lap=7, total=90), p)
        self.assertIsInstance(st.planned_lap, int)
        self.assertNotIn("stage", st.plan_note)


class Phase3CautionTireTests(unittest.TestCase):
    def picks(self, track_type, to_go, rank, tank=60, n=200, wear=0.2, slope=None):
        from collections import Counter
        out = Counter()
        for seed in range(n):
            eng = StrategyEngine(random.Random(seed))
            st = eng.state(0)
            p = StrategyParams(tank_laps=tank, track_type=track_type)
            c = car(0, wear_lf=wear * 0.8, wear_lr=wear * 0.8, wear_rf=wear, wear_rr=wear)
            svc, why = eng.caution_service(c, st, CarInfo(rank, 0, 0, 1.0, 0.3), to_go, p, FOUR_TIRES, tank)
            out[svc] += 1
        return out

    def test_short_track_full_car_isnt_pulled_in_for_fuel(self):
        # #40 Bristol 2026-11-02: leading with 152 of 169 laps of fuel, 294 to go, "can't reach the finish" -> pitted
        # under every caution. Only a fill that reaches the finish / saves a green stop, or a low tank, is a must.
        from aimod.strategy import SHORT_TRACK
        eng = StrategyEngine(random.Random(1))
        eng.lap_now, eng.cautions = 206, 7                                    # a yellow every ~30 laps
        st = eng.state(0)
        p = StrategyParams(tank_laps=169, track_type=SHORT_TRACK)
        me = CarInfo(1, 0, 0, 1.0, 0.3)
        pit, note, must = eng.caution_fuel_call(st, 152, 294, p, me)
        self.assertFalse(must)
        self.assertNotIn("can't reach", note)
        pit, note, must = eng.caution_fuel_call(st, 40, 294, p, me)          # won't last to the next likely yellow
        self.assertTrue(pit and must)
        pit, note, must = eng.caution_fuel_call(st, 152, 160, p, me)         # a fill now makes the finish
        self.assertTrue(pit and must)

    def test_short_track_four_tires_early_gambles_late(self):
        from aimod.strategy import SHORT_TRACK
        early = self.picks(SHORT_TRACK, 300, 8)
        self.assertEqual(early[FOUR_TIRES], 200)
        late = self.picks(SHORT_TRACK, 15, 8)
        self.assertGreater(late[RIGHT_SIDE] + late[FUEL_ONLY], 60)          # 2-tire / no-tire gambles
        self.assertEqual(self.picks(SHORT_TRACK, 15, 1)[FOUR_TIRES], 200)   # the leader keeps 4

    def test_superspeedway_mostly_fuel_only(self):
        from aimod.strategy import SUPERSPEEDWAY
        early = self.picks(SUPERSPEEDWAY, 120, 8, wear=0.1)
        self.assertEqual(early[FUEL_ONLY], 200)                             # tires fine: fuel only
        worn = self.picks(SUPERSPEEDWAY, 120, 8, wear=0.30)
        self.assertGreater(worn[RIGHT_SIDE], 150)                           # worn rights: 2 tires

    def test_intermediate_late_two_tire_gamble_mid_pack_only(self):
        late_mid = self.picks("intermediate", 15, 9)
        self.assertGreater(late_mid[RIGHT_SIDE], 40)
        self.assertEqual(self.picks("intermediate", 15, 25)[FOUR_TIRES], 200)   # deep in the field: 4
        self.assertEqual(self.picks("intermediate", 150, 9)[FOUR_TIRES], 200)   # early: 4


class Phase3FlipTests(unittest.TestCase):
    def flips(self, rank, gap, lap_time=40.0):
        n = 0
        for k in range(40):
            eng = StrategyEngine(random.Random(k))
            eng.lap_time = lap_time
            p = StrategyParams(tank_laps=60, stages=3, stage_ends=(80, 160), flip_pct=100)
            eng.update_stages(p, 240, 70)
            eng.lap_now, eng.lap_total = 70, 240
            st = eng.state(0)
            st.last_stop_lap = 40
            n += isinstance(eng.plan_stage(st, p, 80, 70, 0, 80, CarInfo(rank, 0, 0, gap, 0.3), 0.0, 60, 170), int)
        return n

    def test_only_deep_in_the_field_and_not_lapped_doing_it(self):
        self.assertEqual(self.flips(8, 5.0), 0)          # racing for stage points
        self.assertGreater(self.flips(25, 5.0), 10)      # deep in the field, leader far back on track
        self.assertEqual(self.flips(25, 20.0), 0)        # 20 s back + 25 s stop > a 40 s lap: would be lapped
        self.assertLess(self.flips(13, 5.0), self.flips(25, 5.0))


class Phase3BlockTests(unittest.TestCase):
    def test_superspeedway_manufacturer_block(self):
        eng = StrategyEngine(random.Random(1))
        eng.rng.random = lambda: 0.0
        p = StrategyParams(tank_laps=40)
        a, b = eng.state(0), eng.state(1)
        a.planned_lap, a.window = 30, (25, 38)
        b.planned_lap, b.window = 34, (25, 38)
        eng.block_stop(car(0, make="Ford"), a, 20, p)        # first Ford sets the block lap
        eng.block_stop(car(1, make="Ford"), b, 20, p)
        self.assertEqual(b.planned_lap, 30)
        self.assertIn("Ford block", b.plan_note)


class LateRaceFuelTests(unittest.TestCase):
    def test_one_lap_spare_is_not_makes_it_so_it_tops_off(self):
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=68, fuel_margin=2)
        st = eng.state(0)
        st.last_stop_lap = 266                               # 68-lap tank: 62 laps left at car lap 272
        eng.tick(race([car(0, laps_done=271)], lap=273, total=334), p)
        res = eng.tick(race([car(0, laps_done=271)], flag=CAUTION, lap=273, total=334), p)
        self.assertNotEqual(st.fuel_state, FUEL_MAKES_IT)    # 62 laps for 62 to go is not "makes it"
        eng.caution_wall = -100                              # past the settle time
        res = eng.tick(race([car(0, laps_done=271)], flag=2, lap=273, total=334), p)
        self.assertTrue(st.decision, st.note)

    def test_nothing_after_the_checkered(self):
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=60)
        st = eng.state(0)
        st.last_stop_lap = 280
        res = eng.tick(race([car(0, laps_done=333, pitting=1)], lap=336, total=334), p)
        self.assertEqual(res.events, [])
        self.assertEqual(res.rows[0].status, "RACE OVER")


class PaceLapTests(unittest.TestCase):
    def test_no_pit_calls_on_the_pace_laps(self):
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=60)
        st = eng.state(0)
        st.urgency = 100                                      # would want tires under a caution
        res = eng.tick(race([car(0, laps_done=0, grip=0.8)], flag=CAUTION, lap=0, total=334), p)
        self.assertEqual(res.rows[0].status, "PACE LAPS")
        self.assertNotIn(1, writes_for(res, 0, mm.OFF_PITTING))
        self.assertEqual(res.events, [])
        eng.tick(race([car(0, laps_done=1)], lap=2, total=334), p)            # green
        self.assertTrue(eng.race_started)


class PitRoadStartTests(unittest.TestCase):
    def test_cars_starting_from_their_stalls_are_not_stops(self):
        # Bristol night: the field rolls off pit road during the pace laps (caution flag, lap 0). Those cars
        # were logged as "stage 1 break" stops and called in; then they pitted right after the green.
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=160, stages=3, stage_by="percent", stage_ends=(21, 51))
        cars = [car(i, laps_done=0, pitting=1, on_pit_road=True) for i in range(3)]
        for _ in range(5):
            res = eng.tick(race(cars, flag=CAUTION, lap=0, total=500), p)
            self.assertEqual(res.events, [])
            for i in range(3):
                self.assertNotIn(1, writes_for(res, i, mm.OFF_PITTING))
        self.assertIsNone(eng.stage_break)
        self.assertEqual(res.rows[0].status, "PACE LAPS")


class PlayerCarTests(unittest.TestCase):
    def test_player_stops_are_seen_by_the_ai(self):
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=60)
        you = car(0, laps_done=20, controlled=False, on_pit_road=False, team=3)
        mate = car(2, laps_done=20, team=3, on_pit_road=False)
        eng.tick(race([you, mate], lap=21), p)
        self.assertIn(0, [m.idx for m in eng.teams.get(3, [])])          # the player's car is on its team
        res = eng.tick(race([car(0, laps_done=20, controlled=False, on_pit_road=True, team=3), mate], lap=21), p)
        self.assertIn(0, [g[0] for g in eng.green_stops])                 # green stop: nearby cars can react
        self.assertEqual(res.rows[0].kind, "player")
        eng.tick(race([car(0, laps_done=21, controlled=False, on_pit_road=True, team=3), mate],
                      flag=CAUTION, lap=22), p)
        self.assertIs(eng.state(0).decision, True)                        # caution: teammates can follow it

    def test_player_stop_is_logged_and_on_the_pit_board_like_an_ai_stop(self):
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=60)
        worn = dict(wear_lf=0.3, wear_lr=0.3, wear_rf=0.3, wear_rr=0.3)
        events, exits, board = [], [], []
        seq = [(10, False, None, 60, worn), (20, True, 14.0, 20, worn), (25, True, 14.0, 0.0, worn),
               (26, True, 0.5, 0.0, worn), (27, True, 1.5, 0.0, worn), (38, True, 12.0, 0.0, worn),
               (40, True, 12.0, 8.0, dict(worn, wear_rf=0.0, wear_rr=0.0)),
               (45, False, 12.0, 60, dict(worn, wear_rf=0.0, wear_rr=0.0)),
               (75, False, 12.0, 60, dict(worn, wear_rf=0.0, wear_rr=0.0))]
        for t, road, timer, spd, w in seq:
            r = race([car(0, controlled=False, grip=1.0, grip_real=False, on_pit_road=road, pit_timer=timer,
                          speed=spd, **w), car(2)])
            r.session_time = t
            res = eng.tick(r, p)
            events += res.events
            exits += res.exits
            board += [e.idx for e in eng.pit_board()]
            self.assertFalse([w for w in res.writes if w.idx == 0])          # still never written
        self.assertEqual([e.reason for e in events], ["PLAYER"])
        self.assertEqual(exits[0].service, RIGHT_SIDE)                       # read back from the tires
        self.assertIn(0, board)
        row = res.rows[0]
        self.assertEqual((row.kind, row.stops), ("player", 1))
        self.assertTrue(row.last_pit.endswith(exits[0].code), row.last_pit)

    def test_player_gets_fuel_and_mileage_readings(self):
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=60)
        res = None
        for lap in range(10, 16):
            res = eng.tick(race([car(0, controlled=False, laps_done=lap, fuel=100.0 - 2.0 * (lap - 10),
                                     fuel_cap=120.0, on_pit_road=False, speed=60),
                                 car(2, laps_done=lap, fuel=100.0 - 2.0 * (lap - 10), fuel_cap=120.0)],
                                lap=lap + 1), p)
        row = res.rows[0]
        self.assertTrue(row.fuel_real)
        self.assertAlmostEqual(row.fuel_laps, 90.0 / 2.0, delta=1.0)
        self.assertAlmostEqual(row.car_tank, 60.0, delta=1.0)
        self.assertTrue(row.fuel_state)
        self.assertTrue(row.next_pit.startswith(("FUEL TO LAP", "NO STOP", "FUEL DUE")), row.next_pit)

    def test_player_grip_is_estimated_from_ai_cars_worn_like_it(self):
        eng = StrategyEngine(random.Random(1))
        w = lambda v: dict(wear_lf=v, wear_lr=v, wear_rf=v, wear_rr=v)
        cars = [car(0, controlled=False, grip=1.0, grip_real=False, **w(0.3))]
        cars += [car(i, grip=0.8, **w(0.3)) for i in (2, 3, 4)] + [car(i, grip=1.0, **w(0.0)) for i in (5, 6, 7)]
        row = eng.tick(race(cars), StrategyParams(tank_laps=60)).rows[0]
        self.assertTrue(row.grip_est)
        self.assertAlmostEqual(row.grip, 0.8, delta=0.11)

    def test_player_wear_that_never_moves_is_shown_as_unknown(self):
        eng = StrategyEngine(random.Random(1))
        res = None
        for lap in range(10, 16):
            res = eng.tick(race([car(0, controlled=False, grip=1.0, grip_real=False, laps_done=lap),
                                 car(2, laps_done=lap, wear_lf=0.2, wear_lr=0.2)], lap=lap + 1),
                           StrategyParams(tank_laps=60))
        self.assertFalse(res.rows[0].wear_known)
        self.assertFalse(res.rows[0].grip_est)


    def _caution_pair(self, player_dist, mate_dist):
        now = [1000.0]
        eng = StrategyEngine(random.Random(1), clock=lambda: now[0])
        p = StrategyParams(tank_laps=60)
        eng.race_started = True
        eng.pit_entry, eng.track_len = 800.0, 1000.0
        mk = lambda pd, md, **kw: race([car(0, laps_done=40, controlled=False, on_pit_road=False, team=3,
                                            lap_dist=pd, **kw.get("you", {})),
                                        car(2, laps_done=40, team=3, on_pit_road=False, lap_dist=md)],
                                       flag=CAUTION, lap=41, total=200)
        eng.tick(race([car(0, laps_done=40, controlled=False, team=3, lap_dist=player_dist),
                       car(2, laps_done=40, team=3, lap_dist=mate_dist)], lap=41, total=200), p)
        now[0] += 10
        return eng, p, now, mk

    def test_teammate_behind_waits_for_the_players_caution_call(self):
        eng, p, now, mk = self._caution_pair(500, 300)
        res = eng.tick(mk(500, 300), p)
        self.assertTrue(res.rows[1].status.startswith("WAITS FOR #0"), res.rows[1].status)
        self.assertNotIn(1, writes_for(res, 2, mm.OFF_PITTING))
        now[0] += 1
        eng.tick(mk(790, 590), p)
        now[0] += 1
        res = eng.tick(mk(850, 650), p)                     # the player drove past pit road entry
        self.assertIs(eng.state(0).decision, False)
        self.assertFalse(res.rows[1].status.startswith("WAITS"))
        self.assertIsNotNone(eng.state(2).decision)         # the teammate now makes its own (or the same) call

    def test_follow_player_toggle_off(self):
        eng, p, now, mk = self._caution_pair(500, 300)
        p.follow_player = False
        res = eng.tick(mk(500, 300), p)
        self.assertFalse(res.rows[1].status.startswith("WAITS"), res.rows[1].status)

    def test_teammate_ahead_does_not_wait(self):
        eng, p, now, mk = self._caution_pair(300, 500)
        res = eng.tick(mk(300, 500), p)
        self.assertFalse(res.rows[1].status.startswith("WAITS"), res.rows[1].status)

    def test_teammate_close_to_pit_entry_stops_waiting(self):
        eng, p, now, mk = self._caution_pair(760, 700)
        res = eng.tick(mk(760, 700), p)                      # only 100 m to pit road: decide now
        self.assertFalse(any(r.status.startswith("WAITS") for r in res.rows))

    def test_passed_entry(self):
        eng = StrategyEngine(random.Random(1))
        eng.pit_entry, eng.track_len = 980.0, 1000.0
        self.assertTrue(eng.passed_entry(990, 20))            # across the line wrap
        self.assertFalse(eng.passed_entry(20, 60))
        self.assertFalse(eng.passed_entry(None, 60))


class TinyTankTests(unittest.TestCase):
    def test_bad_tank_value_does_not_loop_full_cars_onto_pit_road(self):
        # reports 2026-10-18: every car "fuel emergency: pits now" on laps 1, 2, 3... with a full tank, at a
        # track with no learned tank (the game's tank value was tiny)
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=2.0)
        eng.race_started = True
        for lap in range(1, 6):
            cars = [car(i, laps_done=lap, on_pit_road=False, fuel=97.0, fuel_cap=100.0) for i in range(6)]
            res = eng.tick(race(cars, lap=lap + 1, total=200), p)
            for i in range(6):
                self.assertNotIn(1, writes_for(res, i, mm.OFF_PITTING), (lap, res.rows[i].status))


class StuckInStallTests(unittest.TestCase):
    def test_car_parked_in_its_stall_is_released(self):
        # Rockingham 2026-10-19: #8 came in on a game-requested stop and sat still in his stall for 40 laps
        now = [1000.0]
        eng = StrategyEngine(random.Random(1), clock=lambda: now[0])
        p = StrategyParams(tank_laps=60)
        eng.race_started = True
        def mk(road, spd):
            r = race([car(0, laps_done=30, pitting=1, game_penalty=True, on_pit_road=road, speed=spd),
                      car(1, laps_done=30, lap_dist=(now[0] * 50) % 1500, speed=50.0)], lap=31, total=200)
            r.session_time = now[0]                            # the field keeps racing: the clock runs
            return r
        eng.tick(race([car(0, laps_done=30, on_pit_road=False, speed=40.0), car(1, laps_done=30)], lap=31,
                      total=200), p)
        eng.tick(mk(False, 30.0), p)
        released = []
        for k in range(60):
            now[0] += 5.0
            res = eng.tick(mk(True, 0.0), p)
            if 0 in writes_for(res, 0, mm.OFF_PITTING) and 0 in writes_for(res, 0, mm.OFF_GAME_PENALTY_STOP):
                released.append(k * 5.0)
        self.assertTrue(released, "never released")
        self.assertGreater(released[0], 140.0)                 # not during a real stop


class OvertimeTests(unittest.TestCase):
    def test_overtime_caution_keeps_pit_calls_and_plans_fuel_for_it(self):
        # bug report: AI pitting / running out of fuel in green-white-checkered overtime. AIMOD showed RACE OVER
        # past the scheduled distance and planned for 1 lap to go
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=60)
        eng.race_started = True
        res = eng.tick(race([car(0, laps_done=498, on_pit_road=False)], flag=CAUTION, lap=499, total=500), p)
        self.assertTrue(eng.ot_pending)
        self.assertGreaterEqual(res.laps_to_go, 5)                       # caution laps + green + white
        res = eng.tick(race([car(0, laps_done=501, on_pit_road=False)], flag=CAUTION, lap=502, total=500), p)
        self.assertNotEqual(res.rows[0].status, "RACE OVER")              # still calling under the OT caution
        self.assertGreaterEqual(res.laps_to_go, 5)
        res = eng.tick(race([car(0, laps_done=502, on_pit_road=False)], lap=503, total=500), p)   # OT restart
        self.assertEqual(eng.ot_total, 504)
        self.assertEqual(res.laps_to_go, 2)
        res = eng.tick(race([car(0, laps_done=504, on_pit_road=False)], lap=505, total=500), p)   # checkered
        self.assertEqual(res.rows[0].status, "RACE OVER")

    def test_no_overtime_when_the_race_ends_green(self):
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=60)
        eng.race_started = True
        res = eng.tick(race([car(0, laps_done=500, on_pit_road=False)], lap=501, total=500), p)
        self.assertEqual(res.rows[0].status, "RACE OVER")
        eng.tick(race([car(0, laps_done=500, on_pit_road=False)], flag=CAUTION, lap=501, total=500), p)
        self.assertFalse(eng.ot_pending)                                  # cool-down caution after the finish


class JustPittedCautionTests(unittest.TestCase):
    def test_green_stop_just_before_the_caution_is_not_repeated(self):
        # user report: cars pitted the lap before a caution, then came in again under it (losing the wave)
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=60)
        st = eng.state(0)
        st.stops, st.last_stop_lap, st.urgency = 2, 41, 100
        c = car(0, laps_done=41, on_pit_road=False)
        eng.caution_decision(c, st, CarInfo(20, 1, 0, 30.0, 0.1), {}, race([c], flag=CAUTION), p, True, 150,
                             95, 95, fuel_left=59.0)
        self.assertIs(st.decision, False)
        self.assertIn("pitted 1 lap(s) ago", st.note)


class GameInsistsTests(unittest.TestCase):
    def test_penalty_stop_is_not_fought(self):
        eng = StrategyEngine(random.Random(1))
        st = eng.state(0)
        st.planned_lap, st.last_stop_lap = GAME_DECIDE, 5
        res = eng.tick(race([car(0, pitting=1, laps_done=10, game_penalty=True, black_flag=True)]),
                       StrategyParams(tank_laps=60))
        self.assertEqual(len(res.events), 1)
        self.assertEqual(res.events[0].reason, "BLACK FLAG")             # a rule showed it the black flag

    def test_game_stop_flag_without_a_penalty_rule_is_logged_as_repair_or_tow(self):
        # the game sets its "stop to serve" flag for its own repair and tow stops too (control race 2026-10-29)
        eng = StrategyEngine(random.Random(1))
        st = eng.state(0)
        st.planned_lap, st.last_stop_lap = GAME_DECIDE, 5
        res = eng.tick(race([car(0, pitting=1, laps_done=10, game_penalty=True)]), StrategyParams(tank_laps=60))
        self.assertEqual(len(res.events), 1)
        self.assertNotIn("penalty", res.events[0].note)
        self.assertIn("repair / tow", res.events[0].note)

    def test_phantom_penalty_called_off_drops_the_stop(self):
        # Race Control 2.7.5 clears +8119/+8118 when no penalty shows: AIMOD drops the stop and does not
        # re-assert it (under caution it would otherwise keep writing the pit request)
        for flag in (GREEN, CAUTION):
            eng = StrategyEngine(random.Random(1))
            p = StrategyParams(tank_laps=60)
            st = eng.state(0)
            st.planned_lap, st.last_stop_lap = GAME_DECIDE, 5
            res = eng.tick(race([car(0, pitting=1, laps_done=10, game_penalty=True, on_pit_road=False)], flag=flag), p)
            self.assertEqual(len(res.events), 1)
            res = eng.tick(race([car(0, pitting=0, laps_done=10, on_pit_road=False)], flag=flag), p)
            self.assertEqual(len(res.cancels), 1)
            self.assertFalse(eng.state(0).game_accepted)
            self.assertIsNot(eng.state(0).decision, True)

    def test_waved_car_credited_mid_caution_stays_out(self):
        # Race Control 2.7.6 credits the waved car's lap during the caution: it then reads as a lead lap car,
        # but pitting now would give the lap back up
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=60)
        st = eng.state(0)
        st.urgency = 100
        c = car(0, laps_done=40, waved=True, on_pit_road=False)
        eng.caution_decision(c, st, CarInfo(20, 0, 0, 20.0, 0.9), {}, race([c], flag=CAUTION), p, True, 200,
                             95, 95, fuel_left=20.0)
        self.assertIs(st.decision, False)
        self.assertIn("waved", st.note)

    def test_repeated_requests_are_let_through(self):
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=60)
        st = eng.state(0)
        st.planned_lap, st.last_stop_lap = GAME_DECIDE, 5
        events = []
        for k in range(4):                                   # the game asks, AIMOD calls it off, it asks again
            events += eng.tick(race([car(0, pitting=1, laps_done=10)]), p).events
            eng.tick(race([car(0, pitting=0, laps_done=10)]), p)
        self.assertEqual(len(events), 1)
        self.assertIn("keeps asking", events[0].note)


class GameStopReplanTests(unittest.TestCase):
    def test_a_stop_the_game_made_replaces_the_plan(self):
        eng = StrategyEngine(random.Random(1))
        p = StrategyParams(tank_laps=60)
        st = eng.state(0)
        st.planned_lap, st.window, st.last_stop_lap = 40, (35, 45), 1
        for t in range(4):                                   # stopped on pit road with no request
            r = race([car(0, laps_done=36, pitting=0, on_pit_road=True, speed=0.0, pit_timer=float(t))], lap=37)
            r.session_time = t
            st.seen_moving = True
            eng.tick(r, p)
        self.assertNotEqual(st.planned_lap, 40)
        self.assertEqual(st.last_stop_lap, 37)


class LatePitOpeningTests(unittest.TestCase):
    def test_calls_still_made_when_race_control_opens_the_pits_at_one_to_go(self):
        now = [1000.0]
        eng = StrategyEngine(random.Random(1), clock=lambda: now[0])
        p = StrategyParams(tank_laps=60)
        eng.state(0).last_stop_lap = 1                    # nearly empty: must pit
        def snap(flag, closed, t):
            r = race([car(0, laps_done=55)], flag=flag, lap=56, total=200)
            r.pits_closed, r.session_time = closed, t
            return r
        eng.tick(race([car(0, laps_done=55)], lap=56, total=200), p)
        eng.tick(snap(1, True, 10), p)
        eng.tick(snap(2, True, 60), p)
        now[0] += 10
        res = eng.tick(snap(5, False, 120), p)            # pits only just opened, one to go
        self.assertIn(1, writes_for(res, 0, mm.OFF_PITTING))


class FreePassTests(unittest.TestCase):
    def test_new_summary_format_and_free_pass_lines(self):
        from aimod.wavelog import WaveLog, parse_line
        self.assertEqual(parse_line("14:30:00 lap 88 flag 3: first time by summary - free pass: #47; waved: #8 #77; "
                                    "lead lap, sent around to their place: none; pitted: #11"), (88, [8, 77]))
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "race_control.log")
            open(path, "w").close()
            w = WaveLog(path)
            w.poll()
            with open(path, "a") as f:
                f.write("14:29:40 lap 87 flag 2: free pass: #47 (1.21 laps behind the leader, the first car one lap "
                        "down) goes around the pace car to get its lap back, pit road waits for it so it can pit; "
                        "restarts at the tail\n")
            w.poll()
            self.assertTrue(w.is_free_pass(47, 88))
            self.assertFalse(w.is_waved(47, 88))
            self.assertEqual(w.new_free_pass, [(87, 47)])

    def test_free_pass_car_called_like_a_lead_lap_car(self):
        eng, st, infos, snap = WaveAroundTests().setup(car_frac=0.3)      # lapped, in line for a wave
        st.urgency, st.fuel_state, st.service = 0, "SHORT ON FUEL", FOUR_TIRES
        c = car(9, free_pass=True)
        eng.caution_decision(c, st, infos[9], infos, snap, StrategyParams(tank_laps=60), True, 80, 95, 95,
                             fuel_left=20)
        self.assertNotIn("wave around", st.note)                           # not treated as a wave candidate
        self.assertTrue(st.decision)
