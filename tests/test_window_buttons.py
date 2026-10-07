"""The hover buttons at the right end of the top bar (and the small strip),
and where small mode puts the window.

    python -m unittest discover -s tests
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import belt  # noqa: E402

FULL = belt.BAR_BUTTONS_FULL            # small mode, minimize, close


class ButtonHits(unittest.TestCase):
    """The buttons are drawn at the very end of the line; a click is matched
    to them by column, so drawing and hit-testing must agree."""

    def test_strip_is_the_three_buttons_two_spaces_apart(self):
        self.assertEqual(belt.button_strip(FULL), "▾  –  ×")

    def test_last_column_is_close(self):
        self.assertEqual(belt.button_at(99, 100, FULL), "close")

    def test_three_columns_in_is_minimize(self):
        self.assertEqual(belt.button_at(96, 100, FULL), "minimize")

    def test_six_columns_in_is_small_mode(self):
        self.assertEqual(belt.button_at(93, 100, FULL), "small")

    def test_gaps_and_the_rest_of_the_bar_are_not_buttons(self):
        for x in (0, 50, 91, 95, 98):
            self.assertIsNone(belt.button_at(x, 100, FULL), x)

    def test_small_strip_offers_the_way_back(self):
        self.assertEqual(belt.button_strip(belt.BAR_BUTTONS_SMALL), "▴  –  ×")
        self.assertEqual(belt.button_at(93, 100, belt.BAR_BUTTONS_SMALL), "full")


class SmallModeRect(unittest.TestCase):
    def test_sized_for_the_strip_from_the_current_cell_size_plus_window_edges(self):
        # 9x19 px cells, window edges add 16 px across and 8 px down
        self.assertEqual(belt.small_rect(cell=(9, 19), edges=(16, 8), corner=(0, 0)),
                         (0, 0, 112 * 9 + 16, 4 * 19 + 8))

    def test_goes_to_the_work_area_corner_not_under_a_top_taskbar(self):
        self.assertEqual(belt.small_rect(cell=(9, 19), edges=(0, 0), corner=(0, 48))[:2], (0, 48))


if __name__ == "__main__":
    unittest.main()
