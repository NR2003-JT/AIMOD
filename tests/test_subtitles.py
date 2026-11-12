"""The subtitle box: which transmissions get it, conversations, rival custom lines, and how the box is drawn."""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_radio import Race                          # noqa: E402
from test_radio_rivals import ev, race_on_24, say    # noqa: E402

from aimod import subtitles as st                    # noqa: E402
from aimod.radio import TRIGGERS                     # noqa: E402


def gate(**cfg):
    base = dict(st.DEFAULTS, on=True)
    base.update(cfg)
    return st.SubtitleGate(base)


class GateTests(unittest.TestCase):
    def test_off_by_default(self):
        self.assertFalse(st.SubtitleGate(dict(st.DEFAULTS)).allow({"why": "rival"}, 100.0))

    def test_untagged_never_shows(self):
        g = gate()
        self.assertFalse(g.allow(None, 100.0))
        self.assertFalse(g.allow({"why": None}, 100.0))

    def test_rivals_and_their_switch(self):
        self.assertTrue(gate().allow({"why": "rival", "group": "a"}, 100.0))
        self.assertFalse(gate(rivals=False).allow({"why": "rival", "group": "a"}, 100.0))

    def test_moments_only_for_the_tv_car(self):
        self.assertFalse(gate().allow({"why": "moment", "tv": False}, 100.0))
        self.assertTrue(gate().allow({"why": "moment", "tv": True}, 100.0))
        self.assertFalse(gate(moments=False).allow({"why": "moment", "tv": True}, 100.0))

    def test_conversations_switch(self):
        self.assertTrue(gate().allow({"why": "convo", "group": "c"}, 100.0))
        self.assertFalse(gate(convos=False).allow({"why": "convo", "group": "c"}, 100.0))

    def test_spacing_between_situations(self):
        g = gate(min_gap=20)
        self.assertTrue(g.allow({"why": "rival", "group": "a"}, 100.0))
        self.assertFalse(g.allow({"why": "rival", "group": "b"}, 110.0))      # too soon
        self.assertTrue(g.allow({"why": "rival", "group": "b"}, 121.0))

    def test_every_line_of_a_situation_shows(self):
        g = gate(min_gap=20)
        self.assertTrue(g.allow({"why": "rival", "group": "a"}, 100.0))       # the crew's line
        self.assertTrue(g.allow({"why": "rival", "group": "a"}, 104.0))       # the driver's, inside the gap
        self.assertTrue(g.allow({"why": "rival", "group": "a"}, 108.0))

    def test_a_blocked_situation_stays_blocked(self):
        g = gate(min_gap=20)
        g.allow({"why": "rival", "group": "a"}, 100.0)
        self.assertFalse(g.allow({"why": "rival", "group": "b"}, 105.0))
        self.assertFalse(g.allow({"why": "rival", "group": "b"}, 106.0))


class TextTests(unittest.TestCase):
    def test_title(self):
        self.assertEqual(st.title_for("driver", "Kyle Larson"), "KYLE LARSON - DRIVER")
        self.assertEqual(st.title_for("driver", ""), "DRIVER")
        self.assertEqual(st.title_for("spotter", "Kyle Larson"), "SPOTTER")      # no name for the others
        self.assertEqual(st.title_for("crew", "Kyle Larson"), "CREW CHIEF")

    def test_parse_script(self):
        lines, bad = st.parse_script("Driver: Tight in the middle.\ncrew chief: Copy.\n\nSpotter: Clear!\noops")
        self.assertEqual(lines, [("driver", "Tight in the middle."), ("crew", "Copy."), ("spotter", "Clear!")])
        self.assertEqual(bad, ["oops"])
        self.assertEqual(st.parse_script(st.script_text(lines))[0], lines)

    def test_a_colon_inside_a_line_is_kept(self):
        self.assertEqual(st.parse_script("Crew: Stage 1: green in two")[0], [("crew", "Stage 1: green in two")])


class ConversationPickTests(unittest.TestCase):
    cfg = {"conversations": [{"trigger": "driver_tight", "lines": [("driver", "Tight!"), ("crew", "Copy.")]},
                             {"trigger": "caution", "lines": [("spotter", "Yellow."), ("crew", "Pit?")]}]}

    def test_by_moment(self):
        self.assertEqual(st.pick_conversation(self.cfg, "caution", True)["lines"][0][1], "Yellow.")
        self.assertIsNone(st.pick_conversation(self.cfg, "green", True))

    def test_driver_rows_need_an_ai_car(self):
        self.assertIsNone(st.pick_conversation(self.cfg, "driver_tight", False))
        self.assertIsNotNone(st.pick_conversation(self.cfg, "driver_tight", True))
        self.assertIsNotNone(st.pick_conversation(self.cfg, "caution", False))

    def test_chance(self):
        cfg = dict(self.cfg, subtitles={"convo_chance": 0})
        self.assertIsNone(st.pick_conversation(cfg, "caution", True))


class RadioIntegrationTests(unittest.TestCase):
    def setUp(self):
        random.seed(3)

    def test_rival_lines_share_one_group_and_can_be_customised(self):
        r = race_on_24()
        r.radio.cfg["custom_lines"] = {"rival_grudge": ["Him again, {other}."],
                                       "driver_rival_grudge": ["Remember {other}."]}
        r.radio.cfg["custom_only"] = True
        say(r, ev("grudge", 24, 48))
        calls = r.radio.voice.calls
        self.assertEqual([c[0] for c in calls], ["Him again, 48.", "Remember 48."])
        subs = [c[2] for c in calls]
        self.assertTrue(all(s["why"] == "rival" for s in subs))
        self.assertEqual(subs[0]["group"], subs[1]["group"])

    def test_rival_moments_are_in_the_custom_lines_list(self):
        keys = {t[0] for t in TRIGGERS}
        for k in ("rival_grudge", "rival_feud", "rival_wrecked", "rival_retaliate", "rival_retaliate_target",
                  "rival_settled", "driver_rival_grudge", "driver_rival_feud", "driver_rival_retaliate"):
            self.assertIn(k, keys)

    def test_a_wreck_has_no_driver_line(self):
        r = race_on_24()
        say(r, ev("wrecked", 24, 48))
        self.assertEqual(len(r.radio.voice.calls), 1)                         # the crew chief only

    def test_conversation_replaces_the_moment_and_chains_its_lines(self):
        r = race_on_24()
        r.radio.cfg["conversations"] = [{"trigger": "rival_grudge", "lines": [
            ("crew", "The {other} got into you."), ("driver", "I saw it."), ("crew", "Stay cool.")]}]
        say(r, ev("grudge", 24, 48))
        first = r.radio.voice.calls[0]
        self.assertEqual(first[0], "The 48 got into you.")
        self.assertEqual([m.text for m in first[3]], ["I saw it.", "Stay cool."])
        self.assertEqual([m.role for m in first[3]], ["driver", "crew"])
        self.assertEqual({first[2]["why"], first[3][0].sub["why"]}, {"convo"})
        self.assertEqual(first[2]["group"], first[3][1].sub["group"])
        self.assertEqual(len(r.radio.voice.calls), 1)       # the grudge's own lines are not said as well

    def test_conversation_with_a_driver_skips_your_own_car(self):
        r = Race()
        r.radio.cfg["car"] = "player"
        r.radio.cfg["conversations"] = [{"trigger": "caution", "lines": [("spotter", "Yellow."), ("driver", "Copy.")]}]
        r.tick()
        r.said.clear()
        r.radio.cars[0].say("flags", "spotter", "Caution, caution.", 0, 8, "flag", trig="caution")
        self.assertEqual(r.said, ["Caution, caution."])

    def test_big_moment_is_tagged(self):
        r = Race()
        r.radio.cfg["car"] = "player"
        r.tick()
        r.said.clear()
        r.radio.cars[0].say("flags", "spotter", "Caution, caution.", 0, 8, "flag", trig="caution", why="moment")
        sub = r.radio.voice.calls[-1][2]
        self.assertEqual(sub["why"], "moment")
        self.assertTrue(sub["tv"])                         # one-car mode: the car you listen to counts as the TV car

    def test_scanner_moment_is_tv_only_for_the_tv_car(self):
        r = Race()
        r.radio.cfg.update(mode="scanner", scan="all", follow_tv=True)
        r.tick(tv=2)
        r.said.clear()
        r.radio.cars[2].say("flags", "spotter", "Caution, caution.", 0, 8, "flag", trig="caution", why="moment")
        r.radio.cars[3].say("status", "crew", "Black flag.", 0, 8, "bf", trig="black_flag", why="moment")
        tv = {c[0]: c[2]["tv"] for c in r.radio.voice.calls if c[2]}
        self.assertTrue(tv.get("Caution, caution."))
        self.assertFalse(tv.get("Black flag.", False))


class VoiceChainTests(unittest.TestCase):
    def test_next_line_follows_the_one_that_aired(self):
        from aimod.radio import Voice, _Msg
        v = Voice({})
        b = _Msg("Copy.", "crew", 1, 30.0, None)
        c = _Msg("Thanks.", "driver", 1, 30.0, None)
        a = _Msg("Tight.", "driver", 1, 12.0, None, then=[b, c])
        v._chain(a)
        self.assertEqual(v.q, [b])
        self.assertEqual(b.then, [c])                                   # and after that one, the last
        v.q.clear()
        v._chain(b)
        self.assertEqual(v.q, [c])


class DrawTests(unittest.TestCase):
    def test_wrap(self):
        from aimod.subtitle_overlay import wrap
        self.assertEqual(wrap("a bb ccc dddd", len, 6), ["a bb", "ccc", "dddd"])
        self.assertEqual(wrap("", len, 6), [])

    def test_box_draws(self):
        from aimod.subtitle_overlay import BASE_H, build_bug
        for text in ("Clear.", "Tight in the middle, I need some help on the next stop. " * 6):
            img = build_bug(24, "KYLE LARSON - DRIVER", text, 1.0)
            self.assertEqual(img.height, BASE_H)                    # always the same height (it doesn't jump)
            self.assertLessEqual(img.width, 1000)
        self.assertGreater(build_bug(24, "SPOTTER", "x", 2.0).height, BASE_H)


if __name__ == "__main__":
    unittest.main()
