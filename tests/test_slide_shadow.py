"""A slid-away strip must not leave its drop shadow on screen.

    python -m unittest discover -s tests
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import belt  # noqa: E402


class FakeWindow:
    """Stands in for WindowMover: remembers what it was told to do."""

    def __init__(self):
        self.hwnd, self.y, self.calls = 1, 0, []
        self.shadow_state = True

    def rect(self):
        return 500, self.y, 800, 100

    def move_to(self, x, y):
        self.y = y

    def mouse(self):
        return (5, 900)                                    # far away: stays hidden

    def button_down(self):
        return False

    def shadow(self, on):
        self.calls.append(("shadow", on))
        self.shadow_state = on

    def shadow_is_on(self):
        return self.shadow_state

    def topmost(self, on):
        self.calls.append(("topmost", on))


class SlideShadow(unittest.TestCase):
    def run_app(self, steps):
        async def go():
            sampler = belt.Sampler()
            app = belt.Belt(sampler)
            try:
                async with app.run_test(size=(100, 30)) as pilot:
                    app.mover = FakeWindow()
                    app._dock_top, app._docked = 0, True
                    await steps(app, pilot)
            finally:
                sampler.stop.set()
        asyncio.run(go())

    def test_shadow_goes_off_once_hidden_and_back_on_when_it_slides_down(self):
        async def steps(app, pilot):
            app._slide(-96, shown=False)
            await pilot.pause(0.4)
            self.assertEqual(app.mover.y, -96)
            self.assertEqual(app.mover.calls[-1], ("shadow", False))
            app._slide(0, shown=True)
            await pilot.pause(0.05)
            self.assertIn(("shadow", True), app.mover.calls)
        self.run_app(steps)

    def test_shadow_switched_back_on_by_windows_is_switched_off_again_while_hidden(self):
        # Windows Terminal redraws its frame after a move and turns the shadow back on.
        async def steps(app, pilot):
            app._start_autohide(0)
            app._slide(-96, shown=False)
            await pilot.pause(0.4)
            app.mover.shadow_state = True                  # what Windows Terminal does
            await pilot.pause(0.3)
            self.assertFalse(app.mover.shadow_state)
        self.run_app(steps)


if __name__ == "__main__":
    unittest.main()
