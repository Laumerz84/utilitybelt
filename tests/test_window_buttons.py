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
        x, y, w, h = belt.small_rect(cell=(9, 19), edges=(16, 8), area=(0, 0, 1920, 1040))
        self.assertEqual((w, h), (88 * 9 + 16, 4 * 19 + 8))

    def test_sits_top_centre_of_the_work_area(self):
        x, y, w, h = belt.small_rect(cell=(10, 20), edges=(0, 0), area=(0, 0, 1920, 1040))
        self.assertEqual((x, y), ((1920 - 880) // 2, 0))

    def test_stays_below_a_top_taskbar(self):
        self.assertEqual(belt.small_rect(cell=(10, 20), edges=(0, 0), area=(0, 48, 1920, 1080))[1], 48)


class SlideAway(unittest.TestCase):
    def test_hidden_leaves_a_thin_sliver_showing_at_the_top_edge(self):
        self.assertEqual(belt.hidden_y(top=0, height=100), -96)

    def test_slide_ends_exactly_where_it_should(self):
        steps = belt.slide_steps(-96, 0)
        self.assertEqual(steps[-1], 0)
        self.assertTrue(all(a <= b for a, b in zip(steps, steps[1:])))   # never jumps backwards

    def test_slide_is_short(self):
        self.assertLessEqual(len(belt.slide_steps(-96, 0)), 10)

    def test_dropped_near_the_top_docks_and_hides(self):
        self.assertTrue(belt.docks(y=10, top=0))

    def test_dropped_lower_down_stays_put_and_visible(self):
        self.assertFalse(belt.docks(y=200, top=0))

    def test_mouse_in_the_sliver_brings_it_down(self):
        # window 400..1200 across, hidden so only y 0..4 shows
        self.assertTrue(belt.wants_open(mouse=(800, 2), left=400, width=800, top=0))
        self.assertFalse(belt.wants_open(mouse=(800, 40), left=400, width=800, top=0))
        self.assertFalse(belt.wants_open(mouse=(100, 2), left=400, width=800, top=0))


if __name__ == "__main__":
    unittest.main()
