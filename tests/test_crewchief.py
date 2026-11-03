"""Unit tests for the crew chief brain (crewchief.py)."""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aimod.crewchief import (
    CrewChiefProfile, caution_call, apply_style_bias, fuel_laps_estimate,
    plan_green_flag_stops,
    STAY_OUT, FOUR_TIRE, TWO_TIRE,
)
from aimod.strategy import SHORT_PIT, SPLIT, STRETCH


def _call(track_type="intermediate", tire_age=0.5, to_go=60.0, fuel_ok=True,
          me_rank=10, field_size=30, cc_rating=0.8, aggression=0.5,
          teammate_decision=None, teammate_rank=None, laps_down=0,
          has_wave_around=True, seed=42):
    rng = random.Random(seed)
    profile = CrewChiefProfile(cc_rating=cc_rating, aggression=aggression)
    return caution_call(track_type, tire_age, to_go, fuel_ok, me_rank, field_size,
                        profile, rng, teammate_decision, teammate_rank,
                        laps_down, has_wave_around)


class TestCrewChiefProfile(unittest.TestCase):
    def test_from_car_info_normalises(self):
        info = {"pitcrew_speed": 1.8, "pitcrew_consistency": 1.6, "name": ""}
        p = CrewChiefProfile.from_car_info(info)
        self.assertGreater(p.cc_rating, 0.0)
        self.assertLessEqual(p.cc_rating, 1.0)

    def test_from_car_info_missing_speed_gives_default(self):
        p = CrewChiefProfile.from_car_info({})
        self.assertEqual(p.cc_rating, 0.5)

    def test_with_strategy_blends(self):
        p = CrewChiefProfile(cc_rating=0.4)
        high = p.with_strategy(1.0)
        self.assertGreater(high.cc_rating, p.cc_rating)

    def test_cc_rating_clamped(self):
        info = {"pitcrew_speed": 2.0, "pitcrew_consistency": 2.0}
        p = CrewChiefProfile.from_car_info(info)
        self.assertLessEqual(p.cc_rating, 1.0)
        self.assertGreaterEqual(p.cc_rating, 0.0)


class TestCautionCall(unittest.TestCase):
    def test_returns_three_fields(self):
        pit, token, note = _call()
        self.assertIsInstance(pit, bool)
        self.assertIsInstance(note, str)

    def test_fresh_tires_favour_stay_out(self):
        # Very fresh tires (age 0.05) should lean heavily stay out.
        results = [_call(tire_age=0.05, seed=i)[0] for i in range(30)]
        self.assertLess(sum(results) / len(results), 0.5)

    def test_old_tires_favour_pit(self):
        # Very worn tires (age 0.95) should lean heavily pit.
        results = [_call(tire_age=0.95, seed=i)[0] for i in range(30)]
        self.assertGreater(sum(results) / len(results), 0.5)

    def test_short_track_favours_two_tire_when_worn(self):
        # Short track, moderately worn tires (65%): two-tire is a realistic call.
        # The brain should produce at least a few two-tire calls across 60 seeds.
        tokens = [_call(track_type="short", tire_age=0.65, to_go=35.0, seed=i)[1] for i in range(60)]
        two_count = sum(1 for t in tokens if t == TWO_TIRE)
        self.assertGreater(two_count, 3)

    def test_no_fuel_forces_pit(self):
        # fuel_ok=False: stay-out score penalised heavily.
        results = [_call(fuel_ok=False, seed=i)[0] for i in range(20)]
        self.assertEqual(sum(results), 20)   # always pits

    def test_aggressive_cc_leans_stay_or_two(self):
        # High aggression should reduce four-tire calls.
        tokens_agg = [_call(tire_age=0.5, aggression=0.95, seed=i)[1] for i in range(50)]
        tokens_con = [_call(tire_age=0.5, aggression=0.05, seed=i)[1] for i in range(50)]
        four_agg = sum(1 for t in tokens_agg if t == FOUR_TIRE)
        four_con = sum(1 for t in tokens_con if t == FOUR_TIRE)
        self.assertLessEqual(four_agg, four_con)

    def test_poor_cc_adds_variance(self):
        # A poor cc_rating should produce different outcomes across seeds more often.
        high = [_call(cc_rating=0.95, seed=i)[0] for i in range(40)]
        low  = [_call(cc_rating=0.05, seed=i)[0] for i in range(40)]
        # Both produce True/False but low should vary more — check it isn't all one value.
        self.assertIn(True, low)
        self.assertIn(False, low)

    def test_teammate_stayed_out_nudges_toward_pit(self):
        # Teammate stayed out and is 15 positions ahead → strong offset toward pitting.
        pit_with = [_call(tire_age=0.5, teammate_decision=False, teammate_rank=1,
                          me_rank=16, seed=i)[0] for i in range(30)]
        pit_without = [_call(tire_age=0.5, seed=i)[0] for i in range(30)]
        self.assertGreaterEqual(sum(pit_with), sum(pit_without))

    def test_teammate_pitted_nudges_toward_stay(self):
        # Teammate pitted and is 15 positions ahead → offset toward staying out.
        pit_with = [_call(tire_age=0.5, teammate_decision=True, teammate_rank=1,
                          me_rank=16, seed=i)[0] for i in range(30)]
        pit_without = [_call(tire_age=0.5, seed=i)[0] for i in range(30)]
        self.assertLessEqual(sum(pit_with), sum(pit_without))

    def test_teammate_ahead_of_me_means_no_offset(self):
        # Teammate is behind me (teammate_rank=20, me_rank=5): influence = 0.
        # Call must be identical to the no-teammate case at the same rank.
        pit_behind = [_call(tire_age=0.5, teammate_decision=True,
                            teammate_rank=20, me_rank=5, seed=i)[0] for i in range(30)]
        pit_base   = [_call(tire_age=0.5, me_rank=5, seed=i)[0] for i in range(30)]
        self.assertEqual(pit_behind, pit_base)

    def test_superspeedway_teammate_follow_not_oppose(self):
        # On a superspeedway, if teammate pitted the car should be MORE likely to pit too.
        pit_ss   = [_call(track_type="superspeedway", tire_age=0.5, teammate_decision=True,
                          teammate_rank=1, me_rank=5, seed=i)[0] for i in range(40)]
        pit_base = [_call(track_type="superspeedway", tire_age=0.5,
                          me_rank=5, seed=i)[0] for i in range(40)]
        self.assertGreaterEqual(sum(pit_ss), sum(pit_base))

    def test_superspeedway_short_pit_penalised(self):
        from aimod.strategy import SHORT_PIT, SPLIT, STRETCH
        from aimod.crewchief import apply_style_bias
        base = {SHORT_PIT: 1.0, SPLIT: 2.0, STRETCH: 1.0}
        ss  = apply_style_bias(base, CrewChiefProfile(aggression=0.8), track_type="superspeedway")
        reg = apply_style_bias(base, CrewChiefProfile(aggression=0.8), track_type="intermediate")
        # SHORT_PIT must be a smaller share on superspeedway
        self.assertLess(ss[SHORT_PIT] / sum(ss.values()),
                        reg[SHORT_PIT] / sum(reg.values()))
        # STRETCH must be a larger share on superspeedway
        self.assertGreater(ss[STRETCH] / sum(ss.values()),
                           reg[STRETCH] / sum(reg.values()))

    def test_note_contains_score(self):
        _, _, note = _call()
        self.assertIn("score", note)

    def test_lapped_car_leans_stay_out(self):
        # Modern era (has_wave_around=True): lapped car should stay out more
        # often than a lead-lap car — wave-around makes pitting costly.
        pit_lapped = [_call(tire_age=0.5, me_rank=30, field_size=40,
                            laps_down=1, has_wave_around=True, seed=i)[0]
                      for i in range(40)]
        pit_lead   = [_call(tire_age=0.5, me_rank=30, field_size=40,
                            laps_down=0, has_wave_around=True, seed=i)[0]
                      for i in range(40)]
        self.assertLessEqual(sum(pit_lapped), sum(pit_lead))

    def test_lapped_car_pre_stage_same_as_lead_lap(self):
        # Pre-stage era (has_wave_around=False): being lapped gives NO stay-out
        # bonus — lapped car should score identically to a lead-lap car.
        pit_lapped = [_call(tire_age=0.5, me_rank=30, field_size=40,
                            laps_down=1, has_wave_around=False, seed=i)[0]
                      for i in range(40)]
        pit_lead   = [_call(tire_age=0.5, me_rank=30, field_size=40,
                            laps_down=0, has_wave_around=False, seed=i)[0]
                      for i in range(40)]
        self.assertEqual(pit_lapped, pit_lead)


class TestApplyStyleBias(unittest.TestCase):
    def test_aggressive_boosts_short_pit(self):
        base = {SHORT_PIT: 1.0, SPLIT: 2.0, STRETCH: 1.0}
        profile = CrewChiefProfile(aggression=1.0)
        w = apply_style_bias(base, profile)
        self.assertGreater(w[SHORT_PIT], base[SHORT_PIT])

    def test_conservative_reduces_boldness(self):
        base = {SHORT_PIT: 1.0, SPLIT: 2.0, STRETCH: 1.0}
        agg = apply_style_bias(base, CrewChiefProfile(aggression=0.9))
        con = apply_style_bias(base, CrewChiefProfile(aggression=0.1))
        self.assertGreater(agg[SHORT_PIT], con[SHORT_PIT])

    def test_weights_stay_positive(self):
        base = {SHORT_PIT: 1.0, SPLIT: 2.0, STRETCH: 1.0}
        for agg in (0.0, 0.5, 1.0):
            w = apply_style_bias(base, CrewChiefProfile(aggression=agg))
            for v in w.values():
                self.assertGreater(v, 0.0)

    def test_fresh_tires_boost_stretch(self):
        # Just pitted (window_frac=0.05): STRETCH should be boosted vs mid-stint.
        base = {SHORT_PIT: 1.0, SPLIT: 2.0, STRETCH: 1.0}
        p = CrewChiefProfile()
        w_fresh = apply_style_bias(base, p, window_frac=0.05)
        w_mid   = apply_style_bias(base, p, window_frac=0.50)
        # Fresh-tire STRETCH share must be higher than mid-stint share
        fresh_sh = w_fresh[STRETCH] / sum(w_fresh.values())
        mid_sh   = w_mid[STRETCH]   / sum(w_mid.values())
        self.assertGreater(fresh_sh, mid_sh)

    def test_deep_window_boosts_short_pit(self):
        # Near fuel deadline (window_frac=0.90): SHORT_PIT urgency rises.
        base = {SHORT_PIT: 1.0, SPLIT: 2.0, STRETCH: 1.0}
        p = CrewChiefProfile()
        w_deep = apply_style_bias(base, p, window_frac=0.90)
        w_mid  = apply_style_bias(base, p, window_frac=0.50)
        deep_sh = w_deep[SHORT_PIT] / sum(w_deep.values())
        mid_sh  = w_mid[SHORT_PIT]  / sum(w_mid.values())
        self.assertGreater(deep_sh, mid_sh)

    def test_pre_stage_era_boosts_stretch(self):
        # No stages: STRETCH fraction must be higher than with stages.
        base = {SHORT_PIT: 1.0, SPLIT: 2.0, STRETCH: 1.0}
        p = CrewChiefProfile(aggression=0.5)
        w_pre   = apply_style_bias(base, p, has_stages=False)
        w_stage = apply_style_bias(base, p, has_stages=True)
        pre_sh   = w_pre[STRETCH]   / sum(w_pre.values())
        stage_sh = w_stage[STRETCH] / sum(w_stage.values())
        self.assertGreater(pre_sh, stage_sh)

    def test_pre_stage_era_penalises_short_pit(self):
        base = {SHORT_PIT: 1.0, SPLIT: 2.0, STRETCH: 1.0}
        p = CrewChiefProfile(aggression=0.5)
        w_pre   = apply_style_bias(base, p, has_stages=False)
        w_stage = apply_style_bias(base, p, has_stages=True)
        pre_sp   = w_pre[SHORT_PIT]   / sum(w_pre.values())
        stage_sp = w_stage[SHORT_PIT] / sum(w_stage.values())
        self.assertLess(pre_sp, stage_sp)


class TestPlanGreenFlagStops(unittest.TestCase):
    def test_basic_count(self):
        # 267-lap race, 66-lap tank → roughly 4 stops
        rng = random.Random(0)
        p = CrewChiefProfile(cc_rating=0.9, aggression=0.5)
        n, gamble = plan_green_flag_stops(267, 66, p, rng)
        self.assertIn(n, range(3, 6))

    def test_gamble_positive(self):
        rng = random.Random(1)
        p = CrewChiefProfile()
        _, gamble = plan_green_flag_stops(267, 66, p, rng)
        self.assertGreaterEqual(gamble, 0.0)

    def test_aggressive_plans_fewer_stops(self):
        # Over many seeds, aggressive crews tend to target fewer stops
        agg_stops = [plan_green_flag_stops(267, 66, CrewChiefProfile(aggression=0.95),
                                           random.Random(i))[0] for i in range(50)]
        con_stops = [plan_green_flag_stops(267, 66, CrewChiefProfile(aggression=0.05),
                                           random.Random(i))[0] for i in range(50)]
        self.assertLessEqual(sum(agg_stops), sum(con_stops))

    def test_zero_tank_returns_zero(self):
        rng = random.Random(0)
        n, g = plan_green_flag_stops(267, 0, CrewChiefProfile(), rng)
        self.assertEqual(n, 0)
        self.assertEqual(g, 0.0)


class TestFuelLapsEstimate(unittest.TestCase):
    def test_good_cc_no_error(self):
        rng = random.Random(0)
        est = fuel_laps_estimate(20.0, 0.99, rng)
        self.assertAlmostEqual(est, 20.0, places=0)

    def test_poor_cc_adds_spread(self):
        rng = random.Random(1)
        errors = [abs(fuel_laps_estimate(20.0, 0.2, rng) - 20.0) for _ in range(100)]
        self.assertGreater(max(errors), 0.5)

    def test_zero_fuel_stays_zero(self):
        rng = random.Random(0)
        self.assertEqual(fuel_laps_estimate(0.0, 0.5, rng), 0.0)


if __name__ == "__main__":
    unittest.main()
