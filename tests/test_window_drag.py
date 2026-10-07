"""Dragging the borderless window by UtilityBelt's own top bar.

    python -m unittest discover -s tests
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import belt  # noqa: E402


class DragMath(unittest.TestCase):
    def test_window_moves_by_exactly_how_far_the_mouse_moved(self):
        self.assertEqual(belt.drag_target(window=(100, 50), grab=(400, 60), mouse=(450, 40)), (150, 30))

    def test_window_can_go_partly_off_screen_to_the_left_and_top(self):
        self.assertEqual(belt.drag_target(window=(10, 10), grab=(20, 20), mouse=(0, 0)), (-10, -10))


class ClickOrDrag(unittest.TestCase):
    def test_tiny_wobble_while_clicking_is_still_a_click(self):
        self.assertTrue(belt.is_click((400, 60), (402, 61)))

    def test_moving_a_few_pixels_is_a_drag(self):
        self.assertFalse(belt.is_click((400, 60), (406, 60)))


if __name__ == "__main__":
    unittest.main()
