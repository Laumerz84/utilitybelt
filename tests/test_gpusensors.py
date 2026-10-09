"""gpusensors: AMD GPU temperature, fan and power from the driver's ADL library.

The sample readings are real ones from this PC (RX 9070 XT under full load,
plus the Ryzen's built-in graphics, which ADL also lists).

    python -m unittest discover -s tests
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gpusensors  # noqa: E402

IGPU = {"name": "AMD Radeon(TM) Graphics", "index": 0,
        "sensors": {1: 600, 2: 2400, 3: 1200, 7: 100, 16: 1015, 17: 10, 18: 10, 19: 0, 21: 1069,
                    23: 50, 28: 46, 29: 50, 30: 25, 31: 23, 35: 0, 40: 3, 41: 16}}
RX9070 = {"name": "AMD Radeon RX 9070 XT", "index": 5,
          "sensors": {1: 2943, 2: 1968, 8: 63, 9: 89, 14: 1489, 15: 29, 19: 100, 20: 1, 21: 1001,
                      27: 87, 38: 0, 39: 0, 40: 4, 41: 16, 58: 5, 73: 268}}


class PickCard(unittest.TestCase):
    def test_the_graphics_card_wins_over_the_cpus_built_in_graphics(self):
        self.assertEqual(gpusensors.pick_adapter([IGPU, RX9070])["name"], "AMD Radeon RX 9070 XT")

    def test_order_does_not_matter(self):
        self.assertEqual(gpusensors.pick_adapter([RX9070, IGPU])["index"], 5)

    def test_nothing_with_temperatures_means_no_card(self):
        self.assertIsNone(gpusensors.pick_adapter([IGPU]))
        self.assertIsNone(gpusensors.pick_adapter([]))


class Labels(unittest.TestCase):
    def test_raw_readings_become_named_values(self):
        r = gpusensors.label(RX9070["sensors"])
        self.assertEqual((r["edge"], r["hotspot"], r["memory"]), (63, 87, 89))
        self.assertEqual((r["fan_rpm"], r["fan_pct"]), (1489, 29))
        self.assertEqual((r["power"], r["busy"]), (268, 100))
        self.assertEqual((r["core_clock"], r["mem_clock"]), (2943, 1968))
        self.assertAlmostEqual(r["voltage"], 1.001)

    def test_missing_sensors_are_none_not_zero(self):
        r = gpusensors.label({8: 50})
        self.assertIsNone(r["fan_rpm"])
        self.assertIsNone(r["power"])

    def test_a_stopped_fan_is_zero_not_missing(self):
        self.assertEqual(gpusensors.label({8: 40, 14: 0, 15: 0})["fan_rpm"], 0)


class Levels(unittest.TestCase):
    def test_full_load_on_this_card_is_normal(self):
        self.assertEqual(gpusensors.heat_level(gpusensors.label(RX9070["sensors"])), "ok")

    def test_hot_hotspot_warns_then_goes_red(self):
        self.assertEqual(gpusensors.heat_level({"edge": 70, "hotspot": 101, "memory": 80}), "warn")
        self.assertEqual(gpusensors.heat_level({"edge": 70, "hotspot": 109, "memory": 80}), "crit")

    def test_hot_memory_warns(self):
        self.assertEqual(gpusensors.heat_level({"edge": 60, "hotspot": 80, "memory": 96}), "warn")

    def test_unknown_readings_are_not_a_warning(self):
        self.assertEqual(gpusensors.heat_level({"edge": None, "hotspot": None, "memory": None}), "ok")


if __name__ == "__main__":
    unittest.main()
