"""The Rivals window (pairs of car numbers, drag-and-drop like Teams) and the rivals on-screen card."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aimod import rivals_ui                          # noqa: E402
from aimod.rivals_ui import POOL, RivalsDialog       # noqa: E402


class Normalize(unittest.TestCase):
    def test_pairs(self):
        self.assertEqual(rivals_ui.normalize([[24, 48], [48, 24], [3, 3], [2001, 24], ["x", 1], [7]]),
                         [[24, 48], [2001, 24]])
        self.assertEqual(rivals_ui.normalize(None), [])


class Dialog(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        try:
            self.root = tk.Tk()
        except tk.TclError:
            self.skipTest("no display")
        self.root.withdraw()
        self.saved = None
        self.d = RivalsDialog(self.root, [24, 48, 3, 2001], [[24, 48]], {}, self._save)
        self.d.win.withdraw()

    def tearDown(self):
        self.root.destroy()

    def _save(self, pairs):
        self.saved = pairs

    def test_pool_copies_boxes_hold_two(self):
        d = self.d
        d.add_box()
        d.move(24, POOL, 1)                    # #24 into a second rivalry too (a driver can have several)
        d.move(3, POOL, 1)
        self.assertEqual(d.boxes, [[24, 48], [24, 3]])
        d.move(2001, POOL, 1)                  # full: refused
        self.assertEqual(d.boxes[1], [24, 3])
        self.assertIn("two cars", d.lbl.cget("text"))
        self.assertIn(24, d.numbers)           # the pool never runs out

    def test_no_duplicate_rivalry(self):
        d = self.d
        d.add_box()
        d.move(48, POOL, 1)
        d.move(24, POOL, 1)
        self.assertEqual(d.boxes[1], [48])
        self.assertIn("already rivals", d.lbl.cget("text"))

    def test_out_to_the_pool_and_between_boxes(self):
        d = self.d
        d.add_box()
        d.move(48, 0, 1)                       # moves from box 1 to box 2
        self.assertEqual(d.boxes, [[24], [48]])
        d.move(24, 0, POOL)
        self.assertEqual(d.boxes[0], [])

    def test_save_keeps_complete_pairs_only(self):
        d = self.d
        d.add_box()
        d.move(3, POOL, 1)
        d.save()
        self.assertEqual(self.saved, [[24, 48]])


class Card(unittest.TestCase):
    def test_build_card(self):
        from aimod import radio_overlay
        if radio_overlay.Image is None:
            self.skipTest("no PIL")
        for kind in ("grudge", "feud", "rival", "retaliate"):
            img = radio_overlay.build_rival_card(24, 2001, kind, 2, 1.0)
            self.assertEqual(img.mode, "RGBA")
            self.assertGreater(img.width, img.height * 2)


if __name__ == "__main__":
    unittest.main()
