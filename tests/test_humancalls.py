"""Crew chief on a phone (beta), stage 2: the Desk between phones and the engine."""
import json
import time
import unittest
import urllib.request
from types import SimpleNamespace

from aimod import humancalls as hc
from aimod.carno import CarNo
from aimod.humancalls import Desk
from aimod.strategy import FOUR_TIRES, RIGHT_SIDE, CarSnapshot, RaceSnapshot
from aimod.webserver import BoardServer

GREEN, CAUTION = 7, 0


def snap(flag=GREEN):
    cars = [CarSnapshot(0, True, number=CarNo(9), controlled=False),         # the player
            CarSnapshot(2, True, number=CarNo(24)),
            CarSnapshot(3, True, number=CarNo(2001))]                       # the 01
    return RaceSnapshot(flag, 3, 40, 10, 100, cars)


class DeskTest(unittest.TestCase):
    def setUp(self):
        self.engine = SimpleNamespace(cautions=0)
        self.desk = Desk()
        self.desk.apply(self.engine, snap())

    def test_claim_one_phone_per_car_never_the_player(self):
        a = self.desk.claim("24", "Dave")
        self.assertTrue(a["ok"])
        self.assertFalse(self.desk.claim("24", "Sam")["ok"])
        self.assertFalse(self.desk.claim("9", "Sam")["ok"])                  # your own car: you drive it
        self.assertTrue(self.desk.claim("#01", "Sam")["ok"])
        self.desk.apply(self.engine, snap())
        self.assertEqual(self.engine.human_owner, {2: "Dave", 3: "Sam"})

    def test_calls_only_under_caution_for_the_owner(self):
        tok = self.desk.claim("24", "Dave")["token"]
        self.assertFalse(self.desk.call(tok, 0, 1, "4")["ok"])               # green: nothing to call
        self.engine.cautions = 1
        self.desk.apply(self.engine, snap(CAUTION))
        self.assertFalse(self.desk.call("not-a-token", 1, 1, "4")["ok"])
        self.assertFalse(self.desk.call(tok, 0, 1, "4")["ok"])               # last caution's number
        self.assertTrue(self.desk.call(tok, 1, 1, "R", 3)["ok"])
        self.desk.apply(self.engine, snap(CAUTION))
        c = self.engine.human_calls[2]
        self.assertEqual((c.pit, c.service, c.fuel_level, c.who), (True, RIGHT_SIDE, 3, "Dave"))
        self.engine.cautions = 2                                             # next caution: the call is stale
        self.desk.apply(self.engine, snap(CAUTION))
        self.assertEqual(self.engine.human_calls, {})

    def test_stay_out_has_no_service(self):
        tok = self.desk.claim("24", "Dave")["token"]
        self.engine.cautions = 1
        self.desk.apply(self.engine, snap(CAUTION))
        self.assertTrue(self.desk.call(tok, 1, 0, "4", 5)["ok"])
        self.desk.apply(self.engine, snap(CAUTION))
        c = self.engine.human_calls[2]
        self.assertEqual((c.pit, c.service, c.fuel_level), (False, None, None))

    def test_quiet_phone_is_away_then_back(self):
        tok = self.desk.claim("24", "Dave")["token"]
        self.desk.owners["24"].seen -= hc.PHONE_GONE_S + 1
        self.desk.apply(self.engine, snap())
        self.assertEqual(self.engine.human_owner, {})                       # asleep: AIMOD runs the car
        self.assertTrue(self.desk.view()["24"]["away"])
        self.assertFalse(self.desk.claim("24", "Sam")["ok"])                 # but it's still Dave's
        self.assertTrue(self.desk.ping(tok)["ok"])                           # the phone wakes: his again
        self.desk.apply(self.engine, snap())
        self.assertEqual(self.engine.human_owner, {2: "Dave"})

    def test_gone_for_good_and_kill_switch(self):
        tok = self.desk.claim("24", "Dave")["token"]
        self.desk.owners["24"].seen -= hc.HOLD_S + 1
        self.desk.apply(self.engine, snap())
        self.assertEqual(self.desk.owners, {})
        self.assertFalse(self.desk.ping(tok)["ok"])
        self.desk.claim("24", "Dave")
        self.desk.release_all()
        self.desk.apply(self.engine, snap())
        self.assertEqual(self.engine.human_owner, {})

    def test_same_name_takes_its_car_back_with_its_plan(self):
        self.desk.claim("24", "Dave")
        self.desk.plan(self.desk.owners["24"].token, 60)
        r = self.desk.claim("24", "dave")                                    # a new token (re-paired phone)
        self.assertTrue(r["ok"] and r["back"])
        self.assertIn("24", self.desk.plans)

    def test_takeover_after_a_long_sleep_and_one_car_per_person(self):
        self.desk.claim("24", "Dave")
        self.desk.owners["24"].seen -= hc.TAKEOVER_S + 1
        self.assertTrue(self.desk.claim("24", "Sam")["ok"])
        self.assertTrue(self.desk.claim("#01", "Sam")["ok"])                  # Sam moves to the 01
        self.assertEqual(set(self.desk.owners), {"01"})

    def test_bad_input(self):
        tok = self.desk.claim("24", "Dave")["token"]
        self.engine.cautions = 1
        self.desk.apply(self.engine, snap(CAUTION))
        self.assertFalse(self.desk.call(tok, "x", 1)["ok"])
        self.assertFalse(self.desk.call(tok, 1, 1, "Z")["ok"])
        self.assertFalse(self.desk.call(tok, 1, 1, "4", 9)["ok"])

    def test_web(self):
        srv = BoardServer(port=18768)
        srv.desk = self.desk
        self.assertTrue(srv.start())
        try:
            get = lambda q: json.loads(urllib.request.urlopen("http://127.0.0.1:18768" + q).read())
            tok = get("/crew/claim?car=24&name=Dave")["token"]
            self.assertTrue(get(f"/crew/ping?token={tok}")["ok"])
            self.engine.cautions = 1
            self.desk.apply(self.engine, snap(CAUTION))
            self.assertTrue(get(f"/crew/call?token={tok}&caution=1&pit=1&service=4")["ok"])
            self.desk.apply(self.engine, snap(CAUTION))
            self.assertEqual(self.engine.human_calls[2].service, FOUR_TIRES)
            self.assertTrue(get(f"/crew/release?token={tok}")["ok"])
            self.desk.apply(self.engine, snap(CAUTION))
            self.assertEqual(self.engine.human_owner, {})
        finally:
            srv.stop()


if __name__ == "__main__":
    unittest.main()


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.engine = SimpleNamespace(cautions=0, lap_now=40, cars={})
        self.desk = Desk()
        self.desk.apply(self.engine, snap())
        self.desk.sync(self.engine, snap())
        self.tok = self.desk.claim("24", "Dave")["token"]

    def test_plan_and_clear(self):
        r = self.desk.plan(self.tok, 52, "4", 5)
        self.assertTrue(r["ok"])
        self.desk.apply(self.engine, snap())
        pl = self.engine.human_plans[2]
        self.assertEqual((pl.lap, pl.service, pl.fuel_level, pl.who), (52, FOUR_TIRES, 5, "Dave"))
        self.assertTrue(self.desk.plan(self.tok, clear=1)["ok"])
        self.desk.apply(self.engine, snap())
        self.assertEqual(self.engine.human_plans, {})

    def test_box_this_lap_and_limits(self):
        self.assertEqual(self.desk.plan(self.tok, 10)["lap"], 40)          # in the past: box now
        self.assertFalse(self.desk.plan(self.tok, 101)["ok"])              # the race is 100 laps
        self.assertFalse(self.desk.plan(self.tok, "x")["ok"])
        self.assertFalse(self.desk.plan("nope", 50)["ok"])

    def test_used_plan_dropped(self):
        pid = self.desk.plan(self.tok, 52)["plan_id"]
        self.desk.apply(self.engine, snap())
        self.engine.cars = {2: SimpleNamespace(human_plan_id=pid, human_plan_status="done")}
        self.desk.sync(self.engine, snap())
        self.desk.apply(self.engine, snap())
        self.assertEqual(self.engine.human_plans, {})

    def test_new_plan_survives_old_done(self):
        old = self.desk.plan(self.tok, 52)["plan_id"]
        self.desk.plan(self.tok, 60)
        self.engine.cars = {2: SimpleNamespace(human_plan_id=old, human_plan_status="done")}
        self.desk.sync(self.engine, snap())
        self.desk.apply(self.engine, snap())
        self.assertEqual(self.engine.human_plans[2].lap, 60)
