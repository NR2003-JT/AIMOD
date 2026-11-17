"""results.ResultsRecorder: a whole fake race through update() -> the .json / .html, points, laps led, stages, grid,
late joins, saving rules."""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from aimod import memory_map as mm   # noqa: E402
from aimod import results as R        # noqa: E402


class Ev:
    def __init__(self, number, green):
        self.number, self.green = number, green


class C:
    def __init__(self, **k):
        self.__dict__.update(k)


class Snap:
    def __init__(self, cars, flag=mm.FLAG_GREEN, total=20):
        self.session, self.cars, self.flag, self.laps_total = mm.SESSION_RACE, cars, flag, total


N, LAPS = 8, 20


def car(i, laps, cross, prev, dist=0.1, out=0):
    return C(valid=True, idx=i + 2, number=10 + i, name=f"Driver {i}", make="Ford", laps_done=laps, cross_ms=cross,
             prev_cross_ms=prev, lap_dist=dist, reason_out=out)


class Race:
    """Car 0 is the fastest and wins every lap; car 7 drops out on lap 8. The clock is ours."""

    def __init__(self, directory, **kw):
        self.t = 0.0
        self.rec = R.ResultsRecorder(directory, clock=lambda: self.t, **kw)
        self.cross = {i: [0] for i in range(N)}
        self.kw = dict(track="Test Track", mod="Series", total=LAPS, stage_ends=(10,), cautions=2, caution_laps=5,
                       stops=[Ev(10, True), Ev(11, False)])

    def grid(self):
        self.rec.update(Snap([car(i, 0, None, None, float(N - i)) for i in range(N)]), **self.kw)

    def lap(self, n):
        cars = []
        for i in range(N):
            if i == 7 and n > 8:
                cars.append(car(i, 8, self.cross[i][-1], self.cross[i][-2], 0.5, out=3))
                continue
            self.cross[i].append(self.cross[i][-1] + 28000 + i * 300)
            cars.append(car(i, n, self.cross[i][-1], self.cross[i][-2]))
        self.t += 30
        self.rec.update(Snap(cars), **self.kw)

    def run(self, laps=LAPS):
        self.grid()
        for n in range(1, laps + 1):
            self.lap(n)


class Points(unittest.TestCase):
    def test_race_and_stage_points(self):
        self.assertEqual([R.race_points(p) for p in (1, 2, 3, 35, 36, 40)], [55, 35, 34, 2, 1, 1])
        self.assertEqual([R.stage_points(p) for p in (1, 2, 10, 11)], [10, 9, 1, 0])


class Build(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.race = Race(self.tmp.name)
        self.race.run()
        self.d = self.race.rec.build()

    def tearDown(self):
        self.tmp.cleanup()

    def test_order_winner_points(self):
        r = self.d["results"]
        self.assertTrue(self.d["complete"])
        self.assertEqual(self.d["winner"], {"number": 10, "name": "Driver 0"})
        self.assertEqual([x["number"] for x in r[:3]], [10, 11, 12])
        self.assertEqual(r[0]["interval"], "LEADER")
        self.assertEqual(r[0]["laps"], LAPS)
        self.assertEqual((r[-1]["number"], r[-1]["status"]), (17, "Out"))     # the car that dropped out is last
        # race 55 + stage 10 + the fastest-lap point
        self.assertEqual((r[0]["race_points"], r[0]["stage_points"], r[0]["bonus_points"], r[0]["points"]),
                         (55, 10, 1, 66))
        self.assertEqual(r[1]["points"], 35 + 9)

    def test_laps_led_and_lead_changes(self):
        self.assertEqual(self.d["most_laps_led"], {"number": 10, "name": "Driver 0", "laps": LAPS})
        self.assertEqual(self.d["lead_changes"], 0)
        self.assertEqual(self.d["leaders"], 1)

    def test_fastest_lap_goes_to_the_fastest_car(self):
        self.assertEqual(self.d["fastest_lap"]["number"], 10)
        self.assertEqual(self.d["fastest_lap"]["time"], 28.0)
        self.assertEqual(self.d["fastest_lap_bonus"]["points"], 1)
        self.assertEqual(sum(x["bonus_points"] for x in self.d["results"]), 1)

    def test_stage_results(self):
        (s,) = self.d["stages"]
        self.assertEqual((s["stage"], s["end_lap"], s["winner"]), (1, 10, "Driver 0"))
        self.assertEqual(s["results"][0]["points"], 10)

    def test_pit_stops_cautions_and_grid(self):
        self.assertEqual(self.d["cautions"], {"count": 2, "laps": 5})
        r = {x["number"]: x for x in self.d["results"]}
        self.assertEqual((r[10]["pit_stops"], r[10]["green_pit_stops"]), (1, 1))
        self.assertEqual((r[11]["pit_stops"], r[11]["green_pit_stops"]), (1, 0))
        self.assertEqual(r[10]["start"], 1)
        self.assertEqual(r[17]["start"], N)
        self.assertEqual(self.d["start_source"], "running order at the first green flag")

    def test_html_has_the_tables(self):
        h = R.render_html(self.d)
        self.assertIn("<title>Race Results</title>", h)
        self.assertIn("Driver 0", h)
        self.assertIn("Stage 1 (lap 10)", h)
        self.assertIn("Fastest lap point", h)
        self.assertNotIn("race not finished", h)


class Saving(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def files(self):
        return sorted(os.listdir(self.tmp.name))

    def test_written_once_the_field_has_finished(self):
        race = Race(self.tmp.name)
        race.run()
        self.assertEqual(self.files(), [])                     # the winner is home but the field gets a moment
        race.t += R.FINAL_HOLD_S + 1
        race.rec.update(Snap([car(i, LAPS, race.cross[i][-1], race.cross[i][-2]) for i in range(7)]), **race.kw)
        names = self.files()
        self.assertEqual(len(names), 2)
        self.assertTrue(names[0].startswith("results_") and names[0].endswith("_Test_Track.html"))
        with open(os.path.join(self.tmp.name, names[1]), encoding="utf-8") as f:
            d = json.load(f)
        self.assertTrue(d["complete"])
        self.assertEqual((d["generated_by"], d["format"]), ("AIMOD", 1))

    def test_unfinished_race_is_saved_when_the_session_ends(self):
        race = Race(self.tmp.name)
        race.run(laps=12)
        race.rec.finish("new session")
        names = [n for n in self.files() if n.endswith(".json")]
        self.assertEqual(len(names), 1)
        with open(os.path.join(self.tmp.name, names[0]), encoding="utf-8") as f:
            d = json.load(f)
        self.assertFalse(d["complete"])
        self.assertIn("race not finished", R.render_html(d))
        self.assertFalse(race.rec.active)                      # and it starts fresh

    def test_a_session_nobody_raced_is_not_saved(self):
        race = Race(self.tmp.name)
        race.grid()
        race.lap(1)
        race.rec.finish("game closed")
        self.assertEqual(self.files(), [])

    def test_only_races_are_recorded(self):
        race = Race(self.tmp.name)
        snap = Snap([car(i, 3, 1000 * i, 500) for i in range(N)])
        snap.session = mm.SESSION_RACE + 1
        race.rec.update(snap, **race.kw)
        self.assertFalse(race.rec.active)


class LateJoin(unittest.TestCase):
    def test_laps_from_before_aimod_joined_come_from_the_game(self):
        times = {i: [0] + [28000 * k + i * 100 for k in range(1, 6)] for i in range(N)}
        with tempfile.TemporaryDirectory() as tmp:
            rec = R.ResultsRecorder(tmp, history=lambda idx, n: times[idx - 2][:n + 1], clock=lambda: 0.0)
            rec.update(Snap([car(i, 5, times[i][5], times[i][4]) for i in range(N)]), track="T", mod="M", total=LAPS)
            self.assertEqual(sorted(rec.cars[2].ms), [0, 1, 2, 3, 4, 5])
            d = rec.build()
            self.assertIsNone(d["start_source"])                # joined after the start: no starting grid
            self.assertEqual(d["results"][0]["laps"], 5)


if __name__ == "__main__":
    unittest.main()
