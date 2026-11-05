"""Pit log CSV (aimod/pitlog.py): a locked file (open in Excel) must never raise into the tick, and its rows
must be written once the file is free again."""
import csv
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aimod import pitlog as pl
from aimod.strategy import PitEvent


def ev(n):
    return PitEvent(0, 3, n, f"Driver {n}", 40, "4 TIRES", "CAUTION", False, 1, fuel=True)


class PitLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = [0.0]
        self.log = pl.PitLog(self.tmp.name, clock=lambda: self.now[0])

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self):
        files = [f for f in os.listdir(self.tmp.name) if f.endswith(".csv")]
        self.assertEqual(len(files), 1)
        with open(os.path.join(self.tmp.name, files[0]), newline="", encoding="utf-8") as f:
            return list(csv.reader(f))

    def test_rows_are_written_with_one_header(self):
        a, b = ev(48), ev(24)
        self.log.start(a, "Bristol")
        self.log.start(b, "Bristol")
        self.log.finish(a)
        self.log.finish(b)
        rows = self.rows()
        self.assertEqual(rows[0], pl.COLUMNS)
        self.assertEqual([r[2] for r in rows[1:]], ["48", "24"])

    def test_locked_file_keeps_the_rows_and_never_raises(self):
        a, b, c = ev(48), ev(24), ev(9)
        for e in (a, b, c):
            self.log.start(e, "Bristol")
        with mock.patch.object(pl, "open", side_effect=PermissionError(13, "locked"), create=True):
            self.assertIsNotNone(self.log.finish(a))         # no exception, the row still goes to the UI
            self.log.new_session()                           # session change while locked: flush must not raise
        self.assertEqual(self.log.unwritten, 3)
        self.assertEqual(self.log.pending, {})
        self.log.retry()                                     # too soon after the last try
        self.assertEqual(self.log.unwritten, 3)
        self.now[0] += pl.PitLog.RETRY_S
        self.log.retry()
        self.assertEqual(self.log.unwritten, 0)
        rows = self.rows()
        self.assertEqual(rows[0], pl.COLUMNS)
        self.assertEqual([r[2] for r in rows[1:]], ["48", "24", "9"], "order kept, one header")


if __name__ == "__main__":
    unittest.main()
