"""Driver radio from inside the helmet (no game needed)."""
import array
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aimod import helmet


class HelmetTests(unittest.TestCase):
    def test_engine_under_the_voice_even_without_the_game_samples(self):
        voice = array.array("h", (int(9000 * math.sin(i / 10.0)) for i in range(helmet.RATE)))
        out = helmet.helmet_mix(voice, 60.0, nr2003_dir="")          # synthetic engine
        self.assertEqual(len(out), len(voice) + 2 * int(0.25 * helmet.RATE))
        lead_in = out[: int(0.2 * helmet.RATE)]
        self.assertGreater(max(abs(s) for s in lead_in), 1000)        # engine heard before he speaks
        self.assertLess(max(abs(s) for s in out), 32768)

    def test_uses_the_installed_cockpit_samples(self):
        d = r"C:\\Papyrus\\NASCAR Racing 2003 Season"
        if not os.path.isfile(os.path.join(d, "sound", "winston_high.wav")):
            self.skipTest("NR2003 sounds not installed here")
        self.assertGreaterEqual(len(helmet.engine_samples(d)), 1)


if __name__ == "__main__":
    unittest.main()
