"""Team radio: what gets said in a scripted race (no audio: the voice is replaced by a list)."""
import unittest
from types import SimpleNamespace

from aimod import memory_map as mm
from aimod.radio import Radio, tenths
from aimod.strategy import CarRow, CarSnapshot, RaceSnapshot, TickResult

GREEN, CAUTION, ONE_TO_GO = mm.FLAG_GREEN, 0, 5


class FakeVoice:
    def __init__(self):
        self.said = []
        self.numbers = []
        self.speakers = []
        self.calls = []                  # (text, role, sub, then): what the subtitle box / conversations got

    def say(self, text, role, prio, ttl, key, number=None, tweak=(0, 0), speaker=None, sub=None, then=None):
        self.said.append(text)
        self.calls.append((text, role, sub, then or []))
        self.numbers.append(number)
        self.speakers.append(speaker)

    def start(self):
        pass

    def stop(self):
        pass


class Race:
    """Three cars: #24 leads, #48 second, #9 (the player) third. Lap time 20 s, the player 0.3 s a lap
    quicker than #48."""

    def __init__(self, fuel=30.0):
        self.radio = Radio({"on": True})
        self.radio.voice = FakeVoice()
        self.radio.start = lambda: None
        self.engine = SimpleNamespace(now=0.0, race_started=True, lap_now=1, pits_open_at=None, field_rate=1.0,
                                      stage_list=[], next_stage_end=lambda: None, stage_no=lambda e: 1)
        self.p = SimpleNamespace(damage_threshold=0.5)
        self.flag = GREEN
        self.lap = {24: 10, 48: 10, 9: 10}
        self.cross = {24: [0], 48: [1500], 9: [3000]}     # ms they crossed the line, per lap done
        self.fuel = fuel
        self.pit_road = False
        self.speed = 50.0
        self.wear = 0.5
        self.events = []
        self.pits_closed = False

    @property
    def said(self):
        return self.radio.voice.said

    def car(self, slot, number, controlled=True):
        c = self.cross[number]
        return CarSnapshot(slot, True, number=number, name=f"Driver {number}", controlled=controlled,
                           laps_done=self.lap[number], cross_ms=c[-1], prev_cross_ms=c[-2] if len(c) > 1 else None,
                           fuel=self.fuel if not controlled else 20.0, fuel_cap=40.0,
                           on_pit_road=self.pit_road if not controlled else False,
                           speed=self.speed if not controlled else 50.0,
                           wear_lf=self.wear, wear_lr=self.wear, wear_rf=self.wear, wear_rr=self.wear)

    def tick(self, total=100, tv=None):
        cars = [self.car(2, 24), self.car(3, 48), self.car(0, 9, controlled=False)]
        snap = RaceSnapshot(self.flag, mm.SESSION_RACE, 40, self.lap[24] + 1, total, cars)
        snap.pits_closed = self.pits_closed
        order = sorted(cars, key=lambda c: (-c.laps_done, c.cross_ms))
        rows = [CarRow(c.idx, c.number, c.name, 50, False, pos=n + 1, lap=c.laps_done + 1)
                for n, c in enumerate(order)]
        res = TickResult(rows=rows, events=self.events, laps_total=total)
        self.events = []
        self.engine.lap_now = self.lap[24] + 1
        self.radio.update(snap, res, self.engine, self.p, tv)

    def green_lap(self, me_gain=300):
        """everyone completes a lap; the player gains me_gain ms on #48"""
        for n in (24, 48, 9):
            self.lap[n] += 1
            gain = me_gain if n == 9 else 0
            self.cross[n].append(self.cross[n][-1] + 20000 - gain)
        self.fuel -= 1.0
        self.engine.now += 20
        self.tick()


class RadioTest(unittest.TestCase):
    def test_wording(self):
        self.assertEqual(tenths(0.1), "a tenth")
        self.assertEqual(tenths(0.34), "3 tenths")
        self.assertEqual(tenths(1.24), "1.2")

    def test_caution_cycle(self):
        r = Race()
        r.tick()
        r.flag = CAUTION
        r.tick()
        self.assertTrue(any("Caution" in s for s in r.said), r.said)
        r.flag = 1
        r.engine.pits_open_at = 5.0
        r.tick()
        self.assertTrue(any("Pit road is open" in s for s in r.said), r.said)
        # fuel 30 laps, 89 to go: the crew chief calls the car in
        self.assertTrue(any("fuel" in s.lower() and ("bring it in" in s.lower() or "come on in" in s.lower())
                            for s in r.said), r.said)
        r.flag = ONE_TO_GO
        r.tick()
        self.assertIn("One to go. One to go.", r.said)
        r.flag = GREEN
        r.tick()
        self.assertIn("Green, green, green!", r.said)

    def test_own_stop(self):
        r = Race()
        r.tick()
        r.pit_road = True
        r.tick()
        r.speed = 0.0
        r.tick()
        r.engine.now += 13.4
        r.wear, r.fuel = 0.0, 40.0
        r.tick()
        r.speed = 30.0
        r.tick()
        r.pit_road = False
        r.tick()
        stop = [s for s in r.said if "in the box" in s]
        self.assertEqual(len(stop), 1, r.said)
        self.assertIn("13.4 in the box", stop[0])
        self.assertIn("Four tires", stop[0])
        self.assertIn("Fuel's good for about 40 laps", stop[0])

    def test_closing_and_fuel(self):
        r = Race(fuel=14.0)
        r.tick()
        for _ in range(4):
            r.green_lap()
        self.assertTrue(any(s.startswith("You're catching the 48") for s in r.said), r.said)
        self.assertTrue(any("Ten laps of fuel left" in s for s in r.said), r.said)
        for _ in range(5):
            r.green_lap(0)
        self.assertTrue(any("five laps of fuel" in s for s in r.said), r.said)

    def test_ai_car_says_aimods_plan(self):
        """listening to an AI car: under caution only AIMOD's own call, with the service it picked"""
        from aimod.strategy import CarState, PitEvent
        r = Race()
        r.radio.cfg["car"] = "48"
        st = CarState()
        r.engine.cars = {3: st}
        r.tick()
        r.flag = CAUTION
        r.tick()
        r.flag = 1
        r.engine.pits_open_at = 5.0
        st.decision = None                       # not decided yet: nothing said about pitting
        r.tick()
        self.assertFalse(any("Bring it in" in s or "staying out" in s.lower() for s in r.said), r.said)
        ev = PitEvent(0, 3, 48, "Driver 48", 40, "4 TIRES", "CAUTION", False, 1, fuel=True)
        ev.fuel_level = 4
        r.events = [ev]
        r.tick()
        call = [s for s in r.said if "Four tires" in s]
        self.assertEqual(call, [call[0]], r.said)
        self.assertIn("Four tires and a can and a half of fuel.", call[0])
        self.assertFalse(any("We need fuel" in s or "Come on in" in s for s in r.said), r.said)

    def test_ai_car_stays_out(self):
        from aimod.strategy import CarState
        r = Race()
        r.radio.cfg["car"] = "48"
        st = CarState()
        r.engine.cars = {3: st}
        r.tick()
        r.flag = CAUTION
        r.tick()
        r.flag = 1
        r.engine.pits_open_at = 5.0
        st.decision, st.note = False, "makes it on fuel: track position"
        r.tick()
        self.assertFalse(any("stay" in s.lower() for s in r.said), r.said)    # held a few seconds first
        r.flag = ONE_TO_GO
        r.tick()
        self.assertTrue(any("track position" in s for s in r.said), r.said)

    def test_spectating_a_number(self):
        r = Race()
        r.radio.cfg["car"] = "48"
        r.tick()
        self.assertEqual(r.radio.target_idx, 3)

    def test_off_says_nothing(self):
        r = Race()
        r.radio.cfg["on"] = False
        r.tick()
        r.flag = CAUTION
        r.tick()
        self.assertEqual(r.said, [])


class ScannerTest(unittest.TestCase):
    def scanner(self):
        from aimod.strategy import CarState
        r = Race()
        r.radio.cfg["mode"] = "scanner"
        r.engine.cars = {2: CarState(), 3: CarState()}
        return r

    def test_one_spotter_for_the_flags(self):
        r = self.scanner()
        r.tick()
        r.flag = CAUTION
        r.tick()
        self.assertEqual(sum("Caution" in s for s in r.said), 1, r.said)
        self.assertEqual(r.radio.focus_slot, 0)            # your car calls the flags

    def test_tv_car_calls_the_flags(self):
        r = self.scanner()
        r.tick(tv=3)
        self.assertEqual(r.radio.focus_slot, 3)
        r.flag = CAUTION
        r.tick(tv=3)
        i = next(n for n, s in enumerate(r.said) if "Caution" in s)
        self.assertEqual(r.radio.voice.numbers[i], 48)

    def test_each_car_has_its_own_crew_chief(self):
        from aimod.strategy import PitEvent
        r = self.scanner()
        r.tick()
        r.flag = CAUTION
        r.tick()
        r.flag = 1
        r.engine.pits_open_at = 5.0
        ev24 = PitEvent(0, 2, 24, "Driver 24", 40, "4 TIRES", "CAUTION", False, 1, fuel=True)
        ev48 = PitEvent(0, 3, 48, "Driver 48", 40, "RIGHT SIDE", "CAUTION", False, 1, fuel=False)
        r.events = [ev24, ev48]
        r.tick()
        calls = {n: s for s, n in zip(r.said, r.radio.voice.numbers) if "tire" in s.lower() or "right sides" in s}
        self.assertIn("Four tires", calls.get(24, ""), r.said)
        self.assertIn("Two right sides, no fuel", calls.get(48, ""), r.said)
        self.assertTrue(any("#48" in line for line in r.radio.lines))

    def test_tuned_to_the_tv_car(self):
        """following the TV car: it is scanned even when not in the list, everything it says is aired, other
        cars only get through with important calls (favourites also with normal ones)"""
        from aimod.radio import HIGH, LOW, NORMAL
        r = self.scanner()
        r.radio.cfg["scan"], r.radio.cfg["scan_numbers"] = "numbers", "24"
        r.radio.cfg["favourites"] = "24"
        r.tick(tv=3)                                         # #48 on TV, not in the scan list
        self.assertEqual(r.radio.focus_slot, 3)
        self.assertTrue(r.radio.tv_locked)
        self.assertIn(3, r.radio.cars)
        cars = r.radio.cars
        n = len(r.said)
        cars[3].say("tires", "crew", "TV car, routine.", LOW)
        cars[2].say("tires", "crew", "Favourite, routine.", LOW)
        cars[2].say("tires", "crew", "Favourite, normal.", NORMAL)
        r.radio.cars[0] = r.radio.cars.get(0) or __import__("aimod.radio", fromlist=["CarRadio"]).CarRadio(
            r.radio, 0, 9)
        r.radio.cars[0].say("tires", "crew", "Other car, normal.", NORMAL)
        r.radio.cars[0].say("pits", "crew", "Other car, pit call.", HIGH)
        self.assertEqual(r.said[n:], ["TV car, routine.", "Favourite, normal.", "Other car, pit call."])

    def test_scan_numbers_only(self):
        r = self.scanner()
        r.radio.cfg["scan"], r.radio.cfg["scan_numbers"] = "numbers", "48"
        r.tick()
        self.assertEqual(set(r.radio.cars), {3})

    def test_one_team(self):
        r = self.scanner()
        r.radio.teams = lambda: [{"name": "Hendrick", "cars": [24, 48]}, {"name": "", "cars": [9]}]
        r.radio.cfg["scan"], r.radio.cfg["scan_team"] = "team", "Hendrick"
        r.tick()
        self.assertEqual(set(r.radio.cars), {2, 3})
        r.radio.cfg["scan_team"] = "Team 2"                 # an unnamed team goes by its place in the list
        r.tick()
        self.assertEqual(set(r.radio.cars), {0})

    def test_piper_speakers(self):
        """spotter 615, the focus car's crew chief 600, other cars from the pool (the same voice every time)"""
        from aimod.strategy import PitEvent
        r = self.scanner()
        r.radio.cfg["piper_pool"] = "523, 31"
        r.tick()
        r.flag = CAUTION
        r.tick()
        v = r.radio.voice
        self.assertEqual(v.speakers[v.said.index(next(s for s in v.said if "Caution" in s))], 615)
        r.flag, r.engine.pits_open_at = 1, 5.0
        r.events = [PitEvent(0, 3, 48, "Driver 48", 40, "4 TIRES", "CAUTION", False, 1, fuel=True)]
        r.tick()
        k = next(i for i, s in enumerate(v.said) if "Four tires" in s)
        self.assertIn(v.speakers[k], (523, 31))
        self.assertEqual(r.radio.speaker("crew", r.radio.cars[3]), v.speakers[k])
        self.assertEqual(r.radio.speaker("crew", r.radio.cars[0]), 600)      # your car = the focus car

    def test_voice_tweak_is_stable(self):
        from aimod.radio import voice_tweak
        self.assertEqual(voice_tweak(24), voice_tweak(24))
        for n in range(100):
            p, q = voice_tweak(n)
            self.assertTrue(-3 <= p <= 3 and -1 <= q <= 1)

    def test_indicator_card(self):
        from aimod.radio_overlay import build_card
        img = build_card(24, 1.0)
        self.assertEqual(img.mode, "RGBA")
        self.assertEqual(img.height, 56)
        self.assertGreater(img.width, 56)


class FixesTest(unittest.TestCase):
    def test_laps_down_only_changes_at_the_line(self):
        """a car running almost exactly 5 laps back: AIMOD's running gap flips 4 / 5, the radio must not"""
        r = Race()
        r.radio.cfg["car"] = "48"
        r.lap = {24: 20, 48: 15, 9: 20}
        r.cross = {24: [0], 48: [100], 9: [50]}
        r.tick()
        for _ in range(6):                                   # nobody crosses the line: nothing changes
            r.tick()
            self.assertEqual(r.radio.cars[3].down, 5)
        self.assertFalse(any("down" in s or "Got one back" in s for s in r.said), r.said)
        r.lap[24] += 1                                       # the leader crosses: still 5 down for #48 ...
        r.cross[24].append(20000)
        r.tick()
        r.lap[48] += 1                                       # ... until #48 crosses: 21 - 16 = 5 down
        r.cross[48].append(20100)
        r.tick()
        self.assertEqual(sum("down" in s for s in r.said), 0, r.said)    # it was 5 down from the start

    def test_chassis_adjustment_is_voiced(self):
        """AIMOD's crew chief adjustment: the driver's complaint when the stop is called (first person, in the
        driver's voice), the crew's line once the car is stopped in its box"""
        from aimod.strategy import CarState, PitEvent
        r = Race()
        r.radio.cfg["car"] = "48"
        r.engine.cars = {3: CarState()}
        r.tick()
        ev = PitEvent(0, 3, 48, "Driver 48", 40, "4 TIRES", "PLANNED", True, 1, fuel=True)
        ev.adjust = SimpleNamespace(driver="He's tight in the center, can't get it to turn.",
                                    crew="We're gonna take out a round of wedge, should free him up.")
        r.events = [ev]
        r.tick()
        v = r.radio.voice
        self.assertEqual(v.said[-2], "I'm tight in the center, can't get it to turn.")
        self.assertTrue(v.said[-1].startswith("Box this lap"), v.said)
        self.assertEqual(r.radio.speaker("driver", None), 21)
        self.assertFalse(any("wedge" in s for s in v.said))            # not until the box
        orig_car = r.car

        def on_road(slot, number, controlled=True):
            c = orig_car(slot, number, controlled)
            if number == 48:
                c.on_pit_road, c.speed = True, r.speed
            return c
        r.car = on_road
        r.tick()                                                         # rolling down pit road
        r.speed = 0.0
        r.tick()                                                         # stopped in the box
        self.assertEqual(v.said[-1], "We're gonna take out a round of wedge, should free him up.")

    def test_driver_lines_and_swearing(self):
        from aimod.radio import DRIVER_LINES
        r = Race()
        r.radio.cfg["car"] = "48"
        r.tick()
        cr = r.radio.cars[3]
        adj = SimpleNamespace(diagnosis="loose", driver="He's loose off, can't put the power down.")
        self.assertIn(cr._driver_adjust_line(adj), DRIVER_LINES["loose"][0])
        r.radio.cfg["driver_swears"] = True
        self.assertIn(cr._driver_adjust_line(adj), DRIVER_LINES["loose"][1])
        # an unknown diagnosis falls back to the crew chief's account, in the driver's words
        self.assertEqual(cr._driver_adjust_line(SimpleNamespace(diagnosis="short run to the finish",
                                                                driver="He's happy with it.")), "I'm happy with it.")

    def test_driver_voices(self):
        """Larson always 670 (by name, any number); other drivers 21 / 92; 670 never given to anyone else"""
        from aimod.radio import CarRadio, Radio
        r = Radio({"on": True, "mode": "scanner"})
        r.focus_slot = 99
        larson = CarRadio(r, 4, 5)
        larson.name = "Kyle Larson"
        self.assertEqual(r.speaker("driver", larson), 670)
        r.focus_slot = 4                                      # on TV too
        self.assertEqual(r.speaker("driver", larson), 670)
        r.focus_slot = 99
        voices = set()
        for n in range(1, 60):
            c = CarRadio(r, n + 10, n)
            c.name = f"Driver {n}"
            voices.add(r.speaker("driver", c))
        self.assertEqual(voices, {21, 92})
        r2 = Radio({"on": True, "driver_voice": "Piper 523"})   # the old default moves to the new one
        self.assertEqual(r2.cfg["driver_voice"], "Piper 21")

    def test_driver_reacts_to_damage(self):
        r = Race()
        r.radio.cfg["car"] = "48"
        r.tick()
        cr = r.radio.cars[3]
        cr.dmg_ref = 0.0
        orig = r.car

        def hit(slot, number, controlled=True):
            c = orig(slot, number, controlled)
            if number == 48:
                c.damage = 0.3
            return c
        r.car = hit
        r.tick()
        from aimod.radio import DRIVER_LINES
        k = next(i for i, s in enumerate(r.said) if s in DRIVER_LINES["damage"][0])
        self.assertIn("damage", r.said[k + 1])

    def test_one_wave_around_call(self):
        from aimod.strategy import CarState
        r = Race()
        r.radio.cfg["car"] = "48"
        st = CarState()
        r.engine.cars = {3: st}
        r.tick()
        r.flag = CAUTION
        r.tick()
        r.flag, r.engine.pits_open_at = 1, 5.0
        st.decision, st.note = False, "stays out for the wave around"
        r.flag = ONE_TO_GO
        r.tick()
        cr = r.radio.cars[3]
        cr.status["waved"] = False                           # now the wave around is confirmed
        orig_rows = r.tick

        def waved_tick():
            import aimod.radio as rm
            real = rm.CarRadio._status

            def status(self, car, me, p):
                me.waved = True
                return real(self, car, me, p)
            rm.CarRadio._status = status
            try:
                orig_rows()
            finally:
                rm.CarRadio._status = real
        waved_tick()
        self.assertEqual(sum("wave around" in s for s in r.said), 1, r.said)

    def test_alongside_on_an_oval(self):
        """a car pulls alongside on the inside on a left-turning circle, then drops back: inside, then clear"""
        import math
        from aimod.radio import CarRadio
        r = Race()
        cr = CarRadio(r.radio, 3, 48)
        R, v = 200.0, 50.0
        said = r.radio.voice.said

        def snap_at(t, other_lag, other_r):
            a = v * t / R                                    # counter-clockwise (left turns)
            me = CarSnapshot(3, True, number=48, speed=v, on_pit_road=False,
                             pos=(R * math.cos(a), R * math.sin(a)), vel=(-v * math.sin(a), v * math.cos(a)))
            b = a - other_lag / R
            other = CarSnapshot(2, True, number=24, speed=v, on_pit_road=False,
                                pos=(other_r * math.cos(b), other_r * math.sin(b)), vel=(0.0, 0.0))
            return SimpleNamespace(flag=GREEN, cars=[me, other]), me
        t = 0.0
        for _ in range(40):                                  # learn the turn, other car far back
            t += 0.15
            s, me = snap_at(t, 60, R)
            cr.alongside(s, me)
        self.assertEqual(said, [])
        for _ in range(4):                                   # alongside, 2.5 m to the inside
            t += 0.15
            s, me = snap_at(t, 1.0, R - 2.5)
            cr.alongside(s, me)
        self.assertEqual(said[-1], "Car inside.", said)
        for _ in range(5):                                   # drops back
            t += 0.15
            s, me = snap_at(t, 30, R - 2.5)
            cr.alongside(s, me)
        self.assertIn(said[-1], ("Clear.", "Clear, clear.", "You're clear."), said)
        for _ in range(4):                                   # one on the outside
            t += 0.15
            s, me = snap_at(t, -1.0, R + 2.5)
            cr.alongside(s, me)
        self.assertEqual(said[-1], "Car outside.", said)


if __name__ == "__main__":
    unittest.main()
