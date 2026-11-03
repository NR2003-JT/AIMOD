"""Team names from NR2003 .car files (no game needed)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aimod import carfiles


def car_file(folder, fname, number, team, make=0):
    text = ("[AIParamMean]\r\naiParamPitcrewSpeed=1.570000\r\naiParamPitcrewConsistency=1.610000\r\n\r\n"
            f"[Driver]\r\ncar_make={make}\r\ncar_number=  {number}\r\nfirst_name=Ross\r\nlast_name=Chastain\r\n"
            f"sponsor=Wendy's\r\nteam_name={team}\r\n")
    path = os.path.join(folder, fname)
    with open(path, "wb") as f:
        f.write(b"FRAC\x00\x00\xff\xfeZ PYTC\x00\x00INIC\x00\x00\r\n" + text.encode("latin-1") + b"\x00XETC\xff\xfe\x00")
    return path


class CarFileTests(unittest.TestCase):
    def test_reads_driver_section_past_binary_parts(self):
        with tempfile.TemporaryDirectory() as d:
            info = carfiles.car_info(car_file(d, "1 Wendys.cup.car", 1, "Trackhouse Racing"))
        self.assertEqual(info["number"], 1)
        self.assertEqual(info["team"], "Trackhouse Racing")
        self.assertEqual(info["make"], 0)
        self.assertAlmostEqual(info["pitcrew_speed"], 1.57)

    def test_teams_merge_spelling_variants(self):
        with tempfile.TemporaryDirectory() as d:
            paths = [car_file(d, "a.car", 23, "23XI Racing"), car_file(d, "b.car", 45, "23xi"),
                     car_file(d, "c.car", 41, "HAAS Factory Team"), car_file(d, "d.car", 41, "HAASFactoryTeam"),
                     car_file(d, "e.car", 99, "")]
            teams = carfiles.teams_from([carfiles.car_info(p) for p in paths])
        self.assertEqual([t["cars"] for t in teams], [[23, 45], [41]])
        self.assertEqual(teams[0]["name"], "23XI Racing")


if __name__ == "__main__":
    unittest.main()
