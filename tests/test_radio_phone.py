"""The scanner on a phone: calls kept as wav files and served to this PC only."""
import array
import io
import json
import unittest
import urllib.request
import wave

from aimod.radio import CREW, Voice
from aimod.webserver import BoardServer


class PhoneScanner(unittest.TestCase):
    def voice(self, **cfg):
        v = Voice(dict({"volume": 80, "radio_fx": False}, **cfg))
        v.phone_at = -1e9
        return v

    def test_no_phone_keeps_nothing(self):
        v = self.voice(phone_only=True)
        import unittest.mock as um
        with um.patch("winsound.PlaySound") as play:
            v._play(array.array("h", [0] * 2205))
        self.assertTrue(play.called)                            # nobody on the phone: the PC plays
        self.assertEqual(v.clips_info(False)["clips"], [])

    def test_phone_only_keeps_the_pc_quiet(self):
        v = self.voice(phone_only=True)
        v.clips_info(True)                                      # a phone asks: it's listening
        import unittest.mock as um
        with um.patch("winsound.PlaySound") as play:
            v._play(array.array("h", [1000] * 2205))
        self.assertFalse(play.called)
        clips = v.clips_info(True)["clips"]
        self.assertEqual(len(clips), 1)
        self.assertAlmostEqual(clips[0]["secs"], 0.1, places=2)
        with wave.open(io.BytesIO(v.clip_wav(clips[0]["id"]))) as w:
            self.assertEqual(w.getnframes(), 2205)

    def test_both_play_when_not_phone_only(self):
        v = self.voice(phone_only=False)
        v.clips_info(True)
        import unittest.mock as um
        with um.patch("winsound.PlaySound") as play:
            v._play(array.array("h", [0] * 2205))
        self.assertTrue(play.called)
        self.assertEqual(len(v.clips_info(False)["clips"]), 1)

    def test_web_server(self):
        v = self.voice(phone_only=False, on=True)
        radio = type("R", (), {"voice": v, "cfg": v.cfg})()
        srv = BoardServer(port=18767)
        srv.radio = radio
        self.assertTrue(srv.start())
        try:
            base = "http://127.0.0.1:18767"
            info = json.loads(urllib.request.urlopen(base + "/radio.json?listen=1").read())
            self.assertTrue(info["on"])
            self.assertTrue(v.phone_listening())
            import unittest.mock as um
            with um.patch("winsound.PlaySound"):
                v._play(array.array("h", [0] * 2205))
            info = json.loads(urllib.request.urlopen(base + "/radio.json").read())
            no = info["clips"][0]["id"]
            body = urllib.request.urlopen(f"{base}/radio/{no}.wav").read()
            self.assertEqual(body[:4], b"RIFF")
            urllib.request.urlopen(base + "/radio/phone_only?on=1").read()
            self.assertTrue(v.cfg["phone_only"])
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(base + "/radio/999.wav")
        finally:
            srv.stop()


if __name__ == "__main__":
    unittest.main()
