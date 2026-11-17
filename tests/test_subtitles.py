"""The subtitle box's gate, the radio conversations (scripts, picking, playing in place of a moment, chaining one voice
after another), the tags the radio puts on its calls, the box's image, and clearing AI black flags."""
import os
import random
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_radio import CAUTION, Race                  # noqa: E402

from aimod import memory_map as mm                    # noqa: E402
from aimod import subtitles as S                      # noqa: E402
from aimod.aggression import RivalEvent               # noqa: E402
from aimod.carno import CarNo                         # noqa: E402
from aimod.game import Game                           # noqa: E402
from aimod.radio import CREW, DRIVER, HIGH, SPOTTER, Radio, Voice, _Msg   # noqa: E402


class Gate(unittest.TestCase):
    def gate(self, **k):
        cfg = dict(S.DEFAULTS, on=True, **k)
        return S.SubtitleGate(cfg)

    def test_off_and_untagged_lines_get_no_box(self):
        self.assertFalse(S.SubtitleGate(dict(S.DEFAULTS)).allow({"why": "rival"}, 0))     # "on" is off by default
        g = self.gate()
        self.assertFalse(g.allow(None, 0))
        self.assertFalse(g.allow({"why": "chatter"}, 0))

    def test_moments_need_the_tv_car(self):
        g = self.gate()
        self.assertFalse(g.allow({"why": "moment", "tv": False}, 0))
        self.assertTrue(g.allow({"why": "moment", "tv": True}, 0))

    def test_each_kind_can_be_switched_off(self):
        self.assertFalse(self.gate(rivals=False).allow({"why": "rival"}, 0))
        self.assertFalse(self.gate(moments=False).allow({"why": "moment", "tv": True}, 0))
        self.assertFalse(self.gate(convos=False).allow({"why": "convo"}, 0))

    def test_min_gap_between_boxes(self):
        g = self.gate(min_gap=20)
        self.assertTrue(g.allow({"why": "rival", "group": "g1"}, 100))
        self.assertFalse(g.allow({"why": "rival", "group": "g2"}, 110))
        self.assertTrue(g.allow({"why": "rival", "group": "g3"}, 121))

    def test_lines_of_one_group_ride_on_the_first_ones_box(self):
        g = self.gate(min_gap=20)
        self.assertTrue(g.allow({"why": "convo", "group": "g1"}, 100))
        self.assertFalse(g.allow({"why": "convo", "group": "g2"}, 105))       # another situation waits its turn
        for t in (103, 110, 124):                         # each within the window of the one before
            self.assertTrue(g.allow({"why": "convo", "group": "g1"}, t))
        # the group's window ran out (25 s after its last line), so a late line is a new box: allowed by the gap
        self.assertTrue(g.allow({"why": "convo", "group": "g1"}, 124 + S.GROUP_WINDOW + 1))

    def test_title(self):
        self.assertEqual(S.title_for("driver", " Kyle Larson "), "KYLE LARSON - DRIVER")
        self.assertEqual(S.title_for("driver", ""), "DRIVER")
        self.assertEqual(S.title_for("spotter"), "SPOTTER")
        self.assertEqual(S.title_for("crew", "x"), "CREW CHIEF")


class Scripts(unittest.TestCase):
    def test_parse(self):
        lines, bad = S.parse_script("Driver: Tight in the middle.\n crew chief : Okay, adjust.\n\nS: Clear.\nnonsense\nDriver:")
        self.assertEqual(lines, [("driver", "Tight in the middle."), ("crew", "Okay, adjust."), ("spotter", "Clear.")])
        self.assertEqual(bad, ["nonsense", "Driver:"])

    def test_round_trip(self):
        lines = [("driver", "Help me."), ("crew", "On it."), ("spotter", "Clear.")]
        self.assertEqual(S.parse_script(S.script_text(lines))[0], lines)

    def test_fill_blanks_what_is_missing(self):
        self.assertEqual(S.fill("P{pos} for the {car}, {nope}.", {"pos": 3, "car": 24}), "P3 for the 24, .")
        self.assertEqual(S.fill("{car", {"car": 24}), "{car")           # a broken placeholder is left as written
        self.assertEqual(S.fill("{car}", {"car": None}), "")

    def test_pick_respects_trigger_chance_and_drivers(self):
        conv = {"trigger": "caution", "lines": [("crew", "a"), ("driver", "b")]}
        crew_only = {"trigger": "caution", "lines": [("crew", "a"), ("spotter", "b")]}
        cfg = {"conversations": [conv, {"trigger": "green", "lines": [("crew", "x")]}, {"trigger": "caution", "lines": []}]}
        self.assertIs(S.pick_conversation(cfg, "caution", True), conv)
        self.assertIsNone(S.pick_conversation(cfg, "caution", False))         # your own driver never speaks
        self.assertIsNone(S.pick_conversation(cfg, "white", True))
        self.assertIsNone(S.pick_conversation(cfg, None, True))
        cfg["conversations"].append(crew_only)
        self.assertIs(S.pick_conversation(cfg, "caution", False), crew_only)
        cfg["subtitles"] = {"convo_chance": 0}
        self.assertIsNone(S.pick_conversation(cfg, "caution", True))
        random.seed(1)
        cfg["subtitles"] = {"convo_chance": 100}
        self.assertIsNotNone(S.pick_conversation(cfg, "caution", True))


class RadioConversations(unittest.TestCase):
    def race(self, convs):
        r = Race()
        r.radio.cfg["conversations"] = convs
        r.radio.cfg["car"] = "24"
        r.tick()
        r.radio.voice.said.clear()
        r.radio.voice.subs.clear()
        r.radio.voice.thens.clear()
        return r

    def test_a_conversation_plays_in_place_of_the_moment(self):
        r = self.race([{"trigger": "caution", "lines": [("spotter", "Caution out, {car}."), ("crew", "Copy that."),
                                                        ("driver", "Tight.")]}])
        r.flag = CAUTION
        r.tick()
        v = r.radio.voice
        self.assertEqual(v.said, ["Caution out, 24."])                  # the first line; the rest follow on air
        sub, then = v.subs[0], v.thens[0]
        self.assertEqual(sub["why"], "convo")
        self.assertTrue(sub["group"].startswith("g"))
        self.assertEqual([(m.role, m.text) for m in then], [(CREW, "Copy that."), (DRIVER, "Tight.")])
        self.assertTrue(all(m.sub is sub for m in then))                 # one decision for the whole conversation
        self.assertTrue(all(m.prio <= HIGH for m in then))
        self.assertTrue(any("Copy that." in line for line in r.radio.lines))   # every line is in the log

    def test_a_driver_conversation_is_skipped_for_your_own_car(self):
        r = Race()
        r.radio.cfg["conversations"] = [{"trigger": "caution", "lines": [("driver", "Hi.")]}]
        r.radio.cfg["car"] = "player"
        r.tick()
        r.radio.voice.said.clear()
        r.flag = CAUTION
        r.tick()
        self.assertFalse(any(s == "Hi." for s in r.said))
        self.assertTrue(any("Caution" in s for s in r.said))

    def test_no_conversation_means_the_normal_line_tagged_as_a_moment(self):
        r = self.race([])
        r.flag = CAUTION
        r.tick()
        i = next(n for n, s in enumerate(r.said) if "Caution" in s)
        self.assertEqual(r.radio.voice.subs[i]["why"], "moment")
        self.assertEqual(r.radio.voice.thens[i], [])

    def test_chance_zero_never_plays_it(self):
        r = self.race([{"trigger": "caution", "lines": [("crew", "Scripted.")]}])
        r.radio.cfg["subtitles"]["convo_chance"] = 0
        r.flag = CAUTION
        r.tick()
        self.assertNotIn("Scripted.", r.said)

    def test_play_script_hears_it(self):
        r = Race()
        r.radio.play_script([("crew", "One."), ("driver", "Two."), ("spotter", "Three.")])
        v = r.radio.voice
        self.assertEqual(v.said, ["One."])
        self.assertEqual([m.text for m in v.thens[0]], ["Two.", "Three."])
        self.assertTrue(v.subs[0]["tv"])
        self.assertEqual(len(r.radio.lines), 3)

    def test_rival_lines_share_a_group_and_are_tagged(self):
        r = Race()
        r.radio.cfg["car"] = "24"
        r.engine.rival_events = []
        r.tick()
        r.radio.voice.said.clear()
        r.radio.voice.subs.clear()
        r.engine.rival_events = [(2, RivalEvent("grudge", CarNo(24), CarNo(48), 1, 0.0, 11))]
        r.tick()
        v = r.radio.voice
        tagged = [s for s in v.subs if s]
        self.assertGreaterEqual(len(tagged), 2)                          # the crew chief's line and the driver's
        self.assertEqual({s["why"] for s in tagged}, {"rival"})
        self.assertEqual(len({s["group"] for s in tagged}), 1)

    def test_a_rival_conversation_replaces_the_crew_line_and_the_drivers(self):
        r = self.race([{"trigger": "rival_grudge", "lines": [("crew", "Remember the {other}.")]}])
        r.engine.rival_events = [(2, RivalEvent("grudge", CarNo(24), CarNo(48), 1, 0.0, 11))]
        r.tick()
        self.assertEqual(r.said, ["Remember the 48."])


class VoiceChain(unittest.TestCase):
    def test_next_line_is_queued_when_one_finishes(self):
        v = Voice({"on": True})
        a = _Msg("one", CREW, HIGH, 30.0, None)
        b = _Msg("two", DRIVER, HIGH, 30.0, None)
        c = _Msg("three", SPOTTER, HIGH, 30.0, None)
        a.then = [b, c]
        v._chain(a)
        self.assertEqual(v.q, [b])
        self.assertEqual(b.then, [c])
        v.q.clear()
        v._chain(b)
        self.assertEqual(v.q, [c])
        self.assertEqual(c.then, [])

    def test_the_gate_is_built_from_the_subtitle_settings(self):
        cfg = {}
        v = Voice(cfg)
        self.assertIs(v.gate.cfg, cfg["subtitles"])

    def test_radio_fills_in_the_subtitle_defaults(self):
        radio = Radio({"on": True})
        for k, v in S.DEFAULTS.items():
            self.assertIn(k, radio.cfg["subtitles"])
        self.assertEqual(radio.cfg["conversations"], [])


class BoxImage(unittest.TestCase):
    def test_wrap_and_fit(self):
        from aimod import subtitle_overlay as O

        width = lambda s: len(s) * 10                                      # noqa: E731
        self.assertEqual(O.wrap("aaa bbb ccc", width, 70), ["aaa bbb", "ccc"])
        self.assertEqual(O.wrap("supercalifragilistic", width, 50), ["supercalifragilistic"])   # a long word stays whole
        self.assertEqual(O.wrap("", width, 50), [])

    def test_the_box_is_drawn(self):
        from aimod import subtitle_overlay as O

        if O.Image is None:
            self.skipTest("Pillow is not installed")
        img = O.build_bug(24, "KYLE LARSON - DRIVER", "Tight in the middle, I need some help on the next stop.", 1.0)
        self.assertEqual(img.mode, "RGBA")
        self.assertEqual(img.height, O.BASE_H)
        big = O.build_bug(24, "SPOTTER", "word " * 80, 1.0)                  # a long line is cut to two
        self.assertEqual(big.height, O.BASE_H)
        self.assertGreater(O.build_bug(24, "X", "y", 2.0).height, img.height)


class FakeMem:
    """Penalty-rule memory: manager at PENALTY_MGR -> array of rules, each a dword per car at rule+4+car*4."""

    def __init__(self, rules):
        self.mem = {}
        self.writes = []
        mgr, arr = 0x1000, 0x2000
        self.put(mm.PENALTY_MGR, mgr)
        self.put(mgr + 4, arr)
        self.put(mgr + 8, len(rules))
        self.rule_addr = []
        for k, cars in enumerate(rules):
            rule = 0x3000 + k * 0x400
            self.rule_addr.append(rule)
            self.put(arr + 4 * k, rule)
            for slot, v in cars.items():
                self.put(rule + 4 + 4 * slot, v)

    def put(self, addr, v):
        self.mem[addr] = v

    def read_uint(self, addr):
        return self.mem.get(addr, 0) & 0xFFFFFFFF

    def read_int(self, addr):
        return self.mem.get(addr, 0)

    def read_bytes(self, addr, n):
        return b"".join(struct.pack("<I", self.mem.get(addr + i, 0)) for i in range(0, n, 4))

    def write_bytes(self, addr, data, n=None):
        self.mem[addr] = struct.unpack("<I", data)[0]
        self.writes.append(addr)


class ClearBlackFlags(unittest.TestCase):
    BLACK = mm.PENALTY_BLACK_BIT

    def game(self, rules, enabled=True):
        g = Game()
        g.pm = FakeMem(rules)
        g.write_enabled = enabled
        return g

    def test_black_flag_and_pit_state_are_wiped_for_the_given_cars_only(self):
        g = self.game([{2: self.BLACK | 3, 3: self.BLACK | 1, 4: 0}, {2: self.BLACK, 5: 0x20}])
        self.assertEqual(g.black_flags(), {2, 3})
        n = g.clear_penalties({2})
        self.assertEqual(n, 2)                                              # car 2 in both rules
        self.assertEqual(g.black_flags(), {3})
        rule0 = g.pm.rule_addr[0]
        self.assertEqual(g.pm.read_uint(rule0 + 4 + 4 * 2), 0)
        self.assertEqual(g.pm.read_uint(rule0 + 4 + 4 * 3), self.BLACK | 1)  # not asked for: untouched

    def test_other_bits_are_kept(self):
        g = self.game([{2: self.BLACK | 0x20 | 5}])
        g.clear_penalties({2})
        self.assertEqual(g.pm.read_uint(g.pm.rule_addr[0] + 4 + 4 * 2), 0x20)

    def test_nothing_written_when_there_is_nothing_to_clear(self):
        g = self.game([{2: 0x20}])
        self.assertEqual(g.clear_penalties({2, 3}), 0)
        self.assertEqual(g.pm.writes, [])

    def test_read_only_without_write_access(self):
        g = self.game([{2: self.BLACK}], enabled=False)
        self.assertEqual(g.clear_penalties({2}), 0)
        self.assertEqual(g.pm.writes, [])

    def test_a_bad_read_is_survived(self):
        g = Game()
        g.pm = None
        g.write_enabled = True
        self.assertEqual(g.clear_penalties({2}), 0)


if __name__ == "__main__":
    unittest.main()
