"""GPU temperature and fan where the user looks: the strip, the GPU card, alerts.

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

SENSORS = {"core_clock": 2854, "mem_clock": 2505, "edge": 63, "memory": 92, "fan_rpm": 1498,
           "fan_pct": 29, "busy": 97, "voltage": 0.972, "hotspot": 84, "power": 304}


def plain(markup) -> str:
    return markup.plain if hasattr(markup, "plain") else to_content(markup).plain


class GpuDisplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sampler = belt.Sampler()
        cls.app = belt.Belt(cls.sampler)

    @classmethod
    def tearDownClass(cls):
        cls.sampler.stop.set()

    def snap(self, sensors):
        snap = self.sampler.read()
        snap.update(vram_used=14 * 2 ** 30, vram_total=16 * 2 ** 30, gpu_sensors=sensors)
        return snap

    def test_strip_shows_gpu_temperature(self):
        text = plain(self.app.render_mini(self.snap(SENSORS), [], 110))
        self.assertIn("63°C", text)

    def test_gpu_card_shows_temperature_and_fan(self):
        lines, _ = self.app._card_gpu(self.snap(SENSORS), self.sampler, 40, 8)
        self.assertIn("63°C", plain(lines[0]))
        self.assertIn("fan 29%", plain(lines[0]))

    def test_no_sensors_means_no_temperature_shown(self):
        lines, _ = self.app._card_gpu(self.snap(None), self.sampler, 40, 8)
        self.assertNotIn("°C", plain(lines[0]))

    def test_hot_card_reaches_the_top_bar(self):
        hot = dict(SENSORS, hotspot=104)
        shorts = [a["short"] for a in belt.compute_alerts(self.snap(hot), self.sampler)]
        self.assertTrue(any("GPU hotspot 104°C" in s for s in shorts), shorts)


if __name__ == "__main__":
    unittest.main()
