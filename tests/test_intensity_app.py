"""The app's racing intensity tick (App._intensity_tick) end to end on a stub app and fake game memory:
capture -> run-ins -> grudge -> feud -> values written -> caution settles -> own values back; CSV trace; SHADOW
writes nothing; the player's car is never written."""
import csv
import os
import struct
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace as NS

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "tools"))
sys.path.insert(0, HERE)

from fake_nr2003 import FakeNR2003                      # noqa: E402
from aimod import aggression as agg                      # noqa: E402
from aimod import behavior                               # noqa: E402
from aimod import memory_map as mm                       # noqa: E402
from aimod.aggtrace import AggTrace                      # noqa: E402
from aimod.app import App                                # noqa: E402
from aimod.behavior import Car, Ctx                      # noqa: E402
from aimod.behavior_mem import AggressionWriter          # noqa: E402
from aimod.presets import Preset                         # noqa: E402

LT = 30.0
METHODS = ("_intensity_tick", "_agg_reset_race", "_agg_summary", "_rival_pairs", "_player_car",
           "_update_rival_onscreen", "_rival_onscreen_info")


class Fixture(unittest.TestCase):
    mode = "on"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        pm = FakeNR2003(n_ai=4, clock=lambda: 0.0)
        pm.process_id = 1
        addr, want = mm.AGG_CODE_CHECK
        pm._put(addr, want)
        pm._put(AggressionWriter.addr(0), struct.pack("<f", 1.11))       # the player's value: must never change
        for sl in range(mm.NUM_CARS):
            pm._put(AggressionWriter._side_addr(sl), struct.pack("<f", 1.0))   # side-padding base
        self.pm = pm
        self.nums = {c.slot: c.number for c in pm.cars}                 # slots 2..5
        behavior.set_temperaments({n: 0.8 for n in self.nums.values()})
        s = types.SimpleNamespace()
        for m in METHODS:
            setattr(s, m, types.MethodType(getattr(App, m), s))
        s.params = Preset(agg_mode=self.mode, agg_strength=100)
        s.game = NS(pm=pm, write_enabled=True, session_names=lambda: ("fakeoval", "fakemod"))
        s.agg_writer = AggressionWriter(os.path.join(self.tmp.name, "restore.json"))
        s.intensity = agg.IntensityEngine()
        s.engine = NS(lap_now=50, rival_events=[], prog_state={})
        s.contacts, s.rivals = agg.ContactWatcher(), agg.RivalBook()
        s.agg_trace = AggTrace(self.tmp.name)
        s.track = "fakeoval"
        s.settings = {}
        s._series_key = lambda: "fakemod"
        s.rival_ind_cfg = {"on": True}
        s._rival_onscreen = None
        self.tv = None
        s.tv_slot_override = None
        s._agg_mode = agg.OFF
        s._agg_reset_race()
        self.app = s
        self.t = 100.0
        self.dmg = {slot: 0.0 for slot in self.nums}
        self.lap = 50
        self.flag = mm.FLAG_GREEN

    def tearDown(self):
        self.app.agg_trace.close()
        behavior.set_temperaments({})
        self.tmp.cleanup()

    def mem(self, slot):
        return struct.unpack("<f", self.pm._get(AggressionWriter.addr(slot), 4))[0]

    def side(self, slot):
        return struct.unpack("<f", self.pm._get(AggressionWriter._side_addr(slot), 4))[0]

    def tick(self, gaps, near=None, dt=1.0, new_lap=False):
        """gaps: slot -> seconds behind the leader; near: (slot, slot) put 3 m apart (for contact)."""
        self.t += dt
        if new_lap:
            self.lap += 1
        x = {slot: -g * 50.0 for slot, g in gaps.items()}
        if near:
            x[near[1]] = x[near[0]] + 3.0
        snap_cars = [NS(idx=slot, number=self.nums[slot], name=f"Driver {self.nums[slot]}", valid=True,
                        pos=(x[slot], 0.0), vel=(50.0, 0.0), damage=self.dmg[slot], reason_out=0, on_pit_road=False,
                        laps_done=self.lap - 1) for slot in gaps]
        snap = NS(flag=self.flag, laps_done=float(self.lap - 1), session_time=self.t, cars=snap_cars)
        cars = [Car(slot, self.nums[slot], f"Driver {self.nums[slot]}", lap=self.lap, progress=self.lap - g / LT,
                    damage=self.dmg[slot]) for slot, g in gaps.items()]
        ctx = Ctx(race=True, green=self.flag == mm.FLAG_GREEN, lap_now=self.lap, total=100, to_go=100 - self.lap,
                  lap_time=LT, stage_ends=())
        self.app._intensity_tick(snap, ctx, cars)

    def rows(self):
        path = self.app.agg_trace.path
        with open(path, encoding="utf-8") as f:
            return list(csv.DictReader(f))


class OnMode(Fixture):
    def test_runins_feud_write_and_settle(self):
        A, B = 2, 3                                       # A runs into B twice
        far = {2: 0.0, 3: 3.0, 4: 8.0, 5: 12.0}
        for _ in range(4):
            self.tick(far)
        self.app.engine.lap_now = self.lap
        self.tick(far, dt=3.0)                            # the capture line
        own = dict(self.app.agg_writer.orig)
        self.assertEqual(set(own), {2, 3, 4, 5})
        # run-in 1: B's damage rises with A right behind him
        self.dmg[B] += 0.05
        self.tick({2: 0.0, 3: 0.1, 4: 8.0, 5: 12.0}, near=(A, B))
        self.assertIsNotNone(self.app.rivals.relation(self.nums[B], self.nums[A]))      # B's grudge
        # run-in 2, 20 game s later
        self.tick(far, dt=20.0)
        self.dmg[B] += 0.05
        self.tick({2: 0.0, 3: 0.1, 4: 8.0, 5: 12.0}, near=(A, B))
        self.assertEqual(self.app.rivals.relation(self.nums[A], self.nums[B])[0], agg.FEUD)
        # next lap they are 0.4 s apart: both race each other at the feud level, written to the game
        self.tick({2: 0.0, 3: 0.4, 4: 8.0, 5: 12.0}, new_lap=True)
        for s in (A, B):
            self.assertGreater(self.mem(s), own[s] + agg.RIVAL_ADDS[2] - 0.01)
        self.assertEqual(self.mem(0), struct.unpack("<f", struct.pack("<f", 1.11))[0])   # never the player
        for s in (A, B):                                  # the pushed cars leave less room beside each other
            self.assertAlmostEqual(self.side(s), 0.7, places=5)
        for s in (4, 5):
            self.assertEqual(self.side(s), 1.0)
        # a caution right after their run-in: settled, everyone back to his own value at once
        self.dmg[B] += 0.05
        self.tick({2: 0.0, 3: 0.1, 4: 8.0, 5: 12.0}, near=(A, B))
        self.flag = 0
        self.tick({2: 0.0, 3: 0.1, 4: 8.0, 5: 12.0})
        self.assertIsNone(self.app.rivals.relation(self.nums[A], self.nums[B]))
        for s in own:
            self.assertEqual(self.mem(s), own[s])
            self.assertEqual(self.side(s), 1.0)
        kinds = [r["event"] for r in self.rows()]
        for k in ("contact", "grudge", "feud", "change", "settled"):
            self.assertIn(k, kinds)

    def test_rivals_off_no_feuds(self):
        self.app.params.rivals_on = False
        far = {2: 0.0, 3: 3.0, 4: 8.0, 5: 12.0}
        for _ in range(4):
            self.tick(far)
        self.tick(far, dt=3.0)
        self.dmg[3] += 0.05
        self.tick({2: 0.0, 3: 0.1, 4: 8.0, 5: 12.0}, near=(2, 3))
        self.assertEqual(self.app.rivals.feuds, {})
        self.assertIn("contact", [r["event"] for r in self.rows()])        # contacts are still counted

    def test_rivals_from_the_rivals_window(self):
        self.app.settings["rivals_by_mod"] = {"fakemod": [[self.nums[4], self.nums[5]]], "othermod": [[1, 2]]}
        self.tick({2: 0.0, 3: 3.0, 4: 8.0, 5: 8.5})
        self.assertEqual(self.app.rivals.relation(self.nums[4], self.nums[5])[0], agg.LISTED)
        self.assertIsNone(self.app.rivals.relation(1, 2))                  # another mod's list

    def test_on_screen_indicator(self):
        from unittest import mock
        self.app.settings["rivals_by_mod"] = {"fakemod": [[self.nums[4], self.nums[5]]]}
        far = {2: 0.0, 3: 3.0, 4: 8.0, 5: 8.4}
        with mock.patch("aimod.app.tv_slot", return_value=4):
            for _ in range(4):
                self.tick(far)
            self.tick(far, dt=3.0)
            self.tick(far, new_lap=True)                                    # decided at the line: rivals close
            self.assertEqual(self.app._rival_onscreen_info()[:3], (self.nums[4], self.nums[5], agg.LISTED))
        with mock.patch("aimod.app.tv_slot", return_value=2):              # the camera on someone else
            self.tick(far)
            self.assertIsNone(self.app._rival_onscreen_info())
        with mock.patch("aimod.app.tv_slot", return_value=5):              # on the other car of the pair
            self.tick(far)
            self.assertEqual(self.app._rival_onscreen_info()[:2], (self.nums[5], self.nums[4]))
        between = {2: 0.0, 3: 8.1, 4: 8.0, 5: 8.4}                          # a car right between the two rivals
        with mock.patch("aimod.app.tv_slot", return_value=4):
            self.tick(between)
            self.assertIsNone(self.app._rival_onscreen_info())
        self.flag = 0
        with mock.patch("aimod.app.tv_slot", return_value=4):              # caution: gone
            self.tick(far)
            self.assertIsNone(self.app._rival_onscreen_info())


class ShowdownLock(Fixture):
    mode = "on"

    def test_a_probe_lock_gives_the_cars_back_and_blocks_writes(self):
        from aimod import showdown_lock
        old = os.environ.get("AIMOD_SHOWDOWN_LOCK")
        os.environ["AIMOD_SHOWDOWN_LOCK"] = os.path.join(self.tmp.name, "showdown.lock")
        try:
            self.app.settings["rivals_by_mod"] = {"fakemod": [[self.nums[4], self.nums[5]]]}
            near = {2: 0.0, 3: 3.0, 4: 8.0, 5: 8.4}
            for _ in range(4):
                self.tick(near)
            self.tick(near, dt=3.0)
            self.tick(near, new_lap=True)                                   # rivals racing: values boosted
            own4 = self.app.agg_writer.orig[4]
            self.assertGreater(self.mem(4), own4 + 0.5)
            showdown_lock.write([self.nums[4]])                             # the probe takes #4
            self.tick(near)
            self.assertAlmostEqual(self.mem(4), own4, 4)                    # his own value is back
            self.assertIn(4, self.app.agg_writer.held)
            self.pm.writes.clear()
            for _ in range(3):
                self.tick(near, new_lap=True)
            self.assertFalse(any(addr == AggressionWriter.addr(4) for addr, _ in self.pm.writes))
            showdown_lock.remove()                                          # the probe is done
            self.tick(near)
            self.assertEqual(self.app.agg_writer.held, set())
        finally:
            if old is None:
                os.environ.pop("AIMOD_SHOWDOWN_LOCK", None)
            else:
                os.environ["AIMOD_SHOWDOWN_LOCK"] = old


class ShadowMode(Fixture):
    mode = "shadow"

    def test_shadow_never_writes(self):
        far = {2: 0.0, 3: 3.0, 4: 8.0, 5: 12.0}
        for _ in range(4):
            self.tick(far)
        self.tick(far, dt=3.0)
        before = len(self.pm.writes)
        self.tick({2: 0.0, 3: 0.3, 4: 8.0, 5: 12.0}, new_lap=True)
        self.assertEqual(len(self.pm.writes), before)
        self.assertIn("change", [r["event"] for r in self.rows()])
        self.assertTrue(all(r["mode"] in ("shadow", "") for r in self.rows()))


if __name__ == "__main__":
    unittest.main()
