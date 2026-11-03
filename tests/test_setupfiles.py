"""Setup-file gearing (copies of installed track setups; skipped without NR2003)."""
import os
import shutil
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aimod import setupfiles as sf

NR = r"C:\Papyrus\NASCAR Racing 2003 Season"


@unittest.skipUnless(os.path.isfile(os.path.join(NR, "NR2003.exe")), "NR2003 not installed here")
class SetupGearingTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        shutil.copy(os.path.join(NR, "NR2003.exe"), self.dir)
        for t in ("Charlotte_2012_SDT_Night_CE", "bristol_night_RR"):
            os.makedirs(os.path.join(self.dir, "tracks", t))
            src = os.path.join(NR, "tracks", t, "fast.cup.sim")
            if os.path.isfile(src + sf.BACKUP):                  # the install is already re-geared: use the original
                src += sf.BACKUP
            shutil.copy(src, os.path.join(self.dir, "tracks", t, "fast.cup.sim"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def gears(self, t):
        b = open(os.path.join(self.dir, "tracks", t, "fast.cup.sim"), "rb").read()
        return len(b), [struct.unpack_from("<i", b, o)[0] for o in (0x2F8, 0x2FC, 0x300, 0x304, 0x338)]

    def test_spread_keeps_first_and_top_and_restores(self):
        self.assertEqual(sf.convert_all(self.dir, 5), (2, 0))
        self.assertEqual(self.gears("Charlotte_2012_SDT_Night_CE"), (2892, [5, 12, 21, 33, 60]))
        size, g = self.gears("bristol_night_RR")             # v13 converted to v14
        self.assertEqual((size, g[0], g[4]), (2892, 17, 65))
        b = open(os.path.join(self.dir, "tracks", "bristol_night_RR", "fast.cup.sim"), "rb").read()
        self.assertEqual(struct.unpack_from("<i", b, 0x224)[0], 280)   # DGTS block length grows too (else NR2003
        self.assertEqual(b[0x340:0x344], b"TGTS")                     # says "cannot load track")
        sf.convert_all(self.dir, 5)                           # again: from the backup, same result
        self.assertEqual(self.gears("Charlotte_2012_SDT_Night_CE")[1], [5, 12, 21, 33, 60])
        self.assertEqual(sf.restore_all(self.dir), 2)
        self.assertEqual(self.gears("bristol_night_RR")[0], 2884)
        self.assertEqual(self.gears("Charlotte_2012_SDT_Night_CE")[1][:4], [5, 15, 33, 60])


if __name__ == "__main__":
    unittest.main()
