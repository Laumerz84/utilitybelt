"""The agent views must survive whatever an agent writes: code, markup-looking
brackets, JSON. Rendering goes through Textual's markup parser, so a text that
is not escaped for it crashes the view.

    python -m unittest discover -s tests
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from textual.markup import to_content  # noqa: E402

import belt  # noqa: E402

# Taken from a real agent's tool input that crashed the view.
NASTY = ('"lines": [[T(\\"2026-09-23\\")], [T(\\"newest\\", c=\\"dim\\")]], '
         'keys = \\[list(k) for k in RETURN_KEYS], [bold]not bold[/], $var, a\\\\[b]')


def ev(kind, text="", tool="", full=None, error=False):
    return {"ts": 1_780_000_000.0, "kind": kind, "text": text, "full": text if full is None else full,
            "tool": tool, "id": "t1", "error": error}


class AgentRender(unittest.TestCase):
    def assert_parses(self, markup: str):
        to_content(markup)                      # raises MarkupError if anything leaked through

    def test_feed_survives_markup_like_text_in_every_kind(self):
        events = [ev("task", NASTY), ev("thinking", NASTY), ev("output", NASTY),
                  ev("tool", "x.py", "Write", full=NASTY), ev("result", NASTY),
                  ev("result", NASTY, error=True)]
        self.assert_parses(belt.Belt.render_agent_feed(events))

    def test_one_line_summary_survives_markup_like_text(self):
        self.assert_parses(belt.Belt._event_line(ev("output", NASTY), 200))

    def test_escaped_text_shows_exactly_as_written(self):
        self.assertEqual(to_content(belt.escape(NASTY)).plain, NASTY)


if __name__ == "__main__":
    unittest.main()
