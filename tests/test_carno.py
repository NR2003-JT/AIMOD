"""Car numbers shown as painted (NR2003 stores "01" as 2001)."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aimod.carno import CarNo, label, matches, raw


class CarNoTests(unittest.TestCase):
    def test_labels(self):
        self.assertEqual([label(v) for v in (2001, 2000, 100, 3007, 1, 42, 999)],
                         ["01", "00", "00", "007", "1", "42", "999"])

    def test_raw_from_car_file(self):
        self.assertEqual([raw(s) for s in ("01", "00", "007", "1", "42", "100")], [2001, 2000, 3007, 1, 42, 100])

    def test_carno_prints_as_painted_but_keeps_its_identity(self):
        n = CarNo(2001)
        self.assertEqual((f"#{n}", str(n), "%s" % n, f"{n:>3}"), ("#01", "01", "01", " 01"))
        self.assertNotEqual(n, 1)                          # #01 and #1 are two cars
        self.assertEqual({n: "x"}[2001], "x")
        self.assertEqual(json.dumps({"n": n}), '{"n": 2001}')
        self.assertTrue(matches(n, "01") and matches(CarNo(100), "00") and not matches(n, "1"))


if __name__ == "__main__":
    unittest.main()
