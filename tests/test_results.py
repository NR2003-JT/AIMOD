"""Race results (aimod/results.py): order, laps led, stages, fastest lap and points worked out from line crossings,
and when the file is written."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aimod import memory_map as mm
from aimod import results as rs
from aimod.strategy import CarSnapshot, PitEvent, RaceSnapshot

# number -> lap times in seconds (a lap that isn't listed takes the default). 12 laps, stages end on laps 4 and 8.
LAP = {
    "A": (20.0, {1: 19.0}),            # leads the first six laps; lap 1 is the quickest but doesn't count
    "B": (20.1, {7: 17.0}),            # fastest lap that counts; takes the lead on lap 7
    "C": (20.2, {}), "D": (20.3, {}), "E": (20.4, {}),
    "F": (20.5, {}),                   # out after lap 6
    "G": (25.0, {}),                   # lapped twice
}
NAMES = {n: (i + 2, 10 + i, f"Driver {n}") for i, n in enumerate(LAP)}     # slot, car number, name
TOTAL = 12


def crossings(until_s=400.0):
    """{name: [ms of lap 1, 2 ...]} (lap 1 starts at 0)."""
    out = {}
    for n, (base, special) in LAP.items():
        t, k, row = 0.0, 0, []
        while True:
            k += 1
            t += special.get(k, base)
            if t > until_s or (n == "F" and k > 6):
                break
            row.append(int(round(t * 1000)))
        out[n] = row
    return out


def snap_at(cross, t_ms, out_codes=None, flag=mm.FLAG_GREEN, dist=None):
    cars = []
    for n, (slot, number, name) in NAMES.items():
        done = [x for x in cross[n] if x <= t_ms]
        k = len(done)
        cars.append(CarSnapshot(
            slot, True, grip=1.0, number=number, name=name, make="Chevrolet", laps_done=k,
            lap_dist=(dist[n] if dist else 100.0), cross_ms=done[-1] if done else None,
            prev_cross_ms=done[-2] if k >= 2 else None,
            reason_out=(out_codes or {}).get(n, 0)))
    return RaceSnapshot(flag, mm.SESSION_RACE, 30.0, 1.0, TOTAL, cars)


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


class ResultsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.rec = rs.ResultsRecorder(self.tmp.name, clock=self.clock)
        self.cross = crossings()

    def tearDown(self):
        self.tmp.cleanup()

    def run_race(self, upto_ms=None):
        grid = {n: 800.0 - 10 * i for i, n in enumerate(LAP)}          # A on the pole ... G last
        self.rec.update(snap_at(self.cross, 0, dist=grid), track="Bristol", mod="Cup", total=TOTAL)
        events = sorted({x for row in self.cross.values() for x in row})
        for t in events:
            if upto_ms is not None and t > upto_ms:
                break
            self.clock.t += 1.0
            self.rec.update(snap_at(self.cross, t, out_codes={"F": 5} if t >= self.cross["F"][5] else None),
                            track="Bristol", mod="Cup", total=TOTAL, stage_ends=(4, 8),
                            cautions=2, caution_laps=7)

    def saved(self):
        base = self.rec._base
        with open(base + ".json", encoding="utf-8") as f:
            return json.load(f)

    def test_points_scale(self):
        self.assertEqual([rs.race_points(p) for p in (1, 2, 3, 35, 36, 40)], [55, 35, 34, 2, 1, 1])
        self.assertEqual([rs.stage_points(p) for p in (1, 10, 11)], [10, 1, 0])

    def test_final_order_laps_led_and_points(self):
        self.run_race()
        d = self.rec.build()
        self.assertTrue(d["complete"])
        self.assertEqual([r["name"][-1] for r in d["results"]], list("BACDEGF"))
        by = {r["name"][-1]: r for r in d["results"]}
        self.assertEqual(by["B"]["interval"], "LEADER")
        self.assertEqual(by["A"]["interval"], "-0.900")
        self.assertEqual(by["G"]["laps"], 10)                     # scored on its first crossing after the winner
        self.assertEqual(by["G"]["interval"], "-2L")
        self.assertEqual(by["F"]["status"], "Out")
        self.assertEqual(by["F"]["out_lap"], 6)
        self.assertEqual(by["G"]["status"], "Running")
        self.assertEqual((by["A"]["laps_led"], by["B"]["laps_led"]), (6, 6))
        self.assertEqual(d["lead_changes"], 1)
        self.assertEqual(d["leaders"], 2)
        self.assertEqual(by["B"]["points"], 55 + 9 + 10 + 1)      # the win, both stages, the fastest lap
        self.assertEqual(by["A"]["points"], 35 + 10 + 9)
        self.assertEqual(by["G"]["points"], 31 + 4 + 4)
        self.assertEqual(d["most_laps_led"]["laps"], 6)

    def test_cooldown_laps_do_not_count(self):
        self.run_race()
        by = {r["name"][-1]: r for r in self.rec.build()["results"]}
        self.assertEqual(by["B"]["laps"], TOTAL)                  # B kept going: more crossings are in the table
        self.assertGreater(len(self.cross["B"]), TOTAL)
        self.assertEqual(self.rec.build()["laps"], {"scheduled": TOTAL, "run": TOTAL})

    def test_stage_results(self):
        self.run_race()
        d = self.rec.build()
        s1, s2 = d["stages"]
        self.assertEqual((s1["end_lap"], s2["end_lap"]), (4, 8))
        self.assertEqual("".join(r["name"][-1] for r in s1["results"]), "ABCDEFG")
        self.assertEqual("".join(r["name"][-1] for r in s2["results"]), "BACDEFG")
        self.assertEqual([r["points"] for r in s1["results"]], [10, 9, 8, 7, 6, 5, 4])
        self.assertEqual(s2["winner"], "Driver B")

    def test_fastest_lap_leaves_out_lap_one(self):
        self.run_race()
        d = self.rec.build()
        self.assertEqual(d["fastest_lap"], {"number": 11, "name": "Driver B", "lap": 7, "time": 17.0})
        by = {r["name"][-1]: r for r in d["results"]}
        self.assertEqual(by["A"]["fastest_lap"], {"lap": 2, "time": 20.0})

    def test_fastest_lap_point_skips_a_car_that_was_out(self):
        self.run_race()
        self.rec.cars[NAMES["B"][0]].out, self.rec.cars[NAMES["B"][0]].out_lap = 5, TOTAL   # off to the garage, back
        d = self.rec.build()
        by = {r["name"][-1]: r for r in d["results"]}
        self.assertEqual((by["B"]["bonus_points"], by["A"]["bonus_points"]), (0, 1))
        self.assertEqual(d["fastest_lap_bonus"]["name"], "Driver A")
        self.assertEqual(sum(r["bonus_points"] for r in d["results"]), 1)
        self.assertEqual(d["fastest_lap"]["name"], "Driver B")                  # the quickest lap is still B's
        self.assertEqual(d["points_system"], "NASCAR 2026 Chase")

    def test_start_positions_and_places_gained(self):
        self.run_race()
        d = self.rec.build()
        by = {r["name"][-1]: r for r in d["results"]}
        self.assertEqual((by["A"]["start"], by["B"]["start"], by["G"]["start"]), (1, 2, 7))
        self.assertEqual((by["B"]["change"], by["A"]["change"], by["G"]["change"]), (1, -1, 1))
        self.assertEqual(d["biggest_mover"]["places"], 1)

    def test_average_running_position(self):
        self.run_race()
        by = {r["name"][-1]: r for r in self.rec.build()["results"]}
        self.assertEqual(by["C"]["avg_running_pos"], 3.0)         # third at every lap's line crossing
        self.assertLess(by["B"]["avg_running_pos"], by["A"]["avg_running_pos"] + 1)

    def test_pit_stops_counted_per_car(self):
        self.rec.stops = []
        self.run_race()
        self.rec.stops = [PitEvent(0, 3, 12, "Driver C", 5, "4 TIRES", "x", True, 1),
                          PitEvent(0, 3, 12, "Driver C", 9, "4 TIRES", "x", False, 2)]
        by = {r["name"][-1]: r for r in self.rec.build()["results"]}
        self.assertEqual((by["C"]["pit_stops"], by["C"]["green_pit_stops"], by["A"]["pit_stops"]), (2, 1, 0))

    def test_written_once_the_field_is_home_and_only_when_changed(self):
        self.run_race()
        self.assertIsNone(self.rec._last_text)                    # not yet: the field may still be finishing
        self.clock.t += rs.FINAL_HOLD_S + 1
        self.rec.update(snap_at(self.cross, 400000), track="Bristol", mod="Cup", total=TOTAL, stage_ends=(4, 8),
                        cautions=2, caution_laps=7)
        d = self.saved()
        self.assertEqual(d["results"][0]["name"], "Driver B")
        self.assertTrue(os.path.exists(self.rec._base + ".html"))
        with open(self.rec._base + ".html", encoding="utf-8") as f:
            html = f.read()
        self.assertIn("Stage 2 (lap 8)", html)
        self.assertIn("LEADER", html)
        mtime = os.path.getmtime(self.rec._base + ".json")
        base = self.rec._base
        self.rec.stops = []
        self.rec.finish("session type changed")                    # same content: no rewrite
        self.assertEqual(os.path.getmtime(base + ".json"), mtime)
        self.assertFalse(self.rec.active)                          # and the recorder starts over
        self.assertEqual([f for f in os.listdir(self.tmp.name) if f.endswith(".tmp")], [])

    def test_unfinished_race_is_saved_when_the_session_ends(self):
        self.run_race(upto_ms=int(7.5 * 20000))
        self.rec.finish("game closed")
        files = [f for f in os.listdir(self.tmp.name) if f.endswith(".json")]
        self.assertEqual(len(files), 1)
        with open(os.path.join(self.tmp.name, files[0]), encoding="utf-8") as f:
            d = json.load(f)
        self.assertFalse(d["complete"])
        self.assertEqual(d["laps"]["scheduled"], TOTAL)
        self.assertLess(d["laps"]["run"], TOTAL)

    def test_a_race_of_two_laps_is_not_saved(self):
        self.run_race(upto_ms=2 * 20000)
        self.rec.finish("session type changed")
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_no_file_outside_a_race(self):
        s = snap_at(self.cross, 100000)
        s.session = 5
        self.rec.update(s, track="Bristol", total=TOTAL)
        self.assertFalse(self.rec.active)

    def test_joining_a_running_race_fills_the_laps_from_the_game_table(self):
        row = {NAMES[n][0]: [0] + self.cross[n] for n in LAP}
        rec = rs.ResultsRecorder(self.tmp.name, history=lambda idx, laps: row.get(idx, [])[:laps + 1],
                                 clock=self.clock)
        rec.update(snap_at(self.cross, 150000), track="Bristol", mod="Cup", total=TOTAL, stage_ends=(4, 8))
        self.assertIsNone(rec.grid)                                # no grid when the race is already running
        self.assertEqual(sorted(rec.cars[NAMES["B"][0]].ms), list(range(0, 8)))
        d = rec.build()
        led = {r["name"][-1]: r["laps_led"] for r in d["results"]}
        self.assertEqual((led["A"], led["B"]), (6, 1))             # 7 laps run by then: A led 1-6, B lap 7
        self.assertEqual(d["stages"][0]["winner"], "Driver A")
        self.assertIsNone(d["results"][0]["start"])

    def test_a_stale_table_read_is_not_taken_for_a_new_lap(self):
        self.rec.update(snap_at(self.cross, 0), track="x", total=TOTAL)
        s = snap_at(self.cross, 45000)
        for c in s.cars:                                           # laps_done moved on, the table entry didn't
            c.cross_ms = c.prev_cross_ms
        self.rec.update(s, track="x", total=TOTAL)
        self.assertTrue(all(max(c.ms, default=0) <= 1 for c in self.rec.cars.values()))


class CrossingHistoryTests(unittest.TestCase):
    """Game.crossing_history reads one car's row of NR2003's scoring table (read-only)."""

    def test_reads_the_row_of_a_slot(self):
        import struct
        from aimod.game import Game

        class PM:
            mem = {}

            def read_bytes(self, addr, n):
                return bytes(self.mem.get(addr + k, 0) for k in range(n))

        pm, table, shift = PM(), 0x20000000, 7
        hdr = struct.pack("<IIII", 5, 300, shift, table)
        for k, b in enumerate(hdr):
            pm.mem[mm.SCORING_HDR + k] = b
        row = 3 << (shift + 2)
        for n, ms in enumerate((5000, 24000, 43500, 63000)):
            for k, b in enumerate(struct.pack("<I", ms | 0xF0000000)):      # (the top bits aren't time)
                pm.mem[table + row + 4 * n + k] = b
        g = Game()
        g.pm = pm
        self.assertEqual(g.crossing_history(3, 3), [5000, 24000, 43500, 63000])
        self.assertEqual(g.crossing_history(3, 1), [5000, 24000])
        self.assertEqual(g.crossing_history(99, 3), [])                     # not a car slot
        g.pm = None
        self.assertEqual(g.crossing_history(3, 3), [])


if __name__ == "__main__":
    unittest.main()
