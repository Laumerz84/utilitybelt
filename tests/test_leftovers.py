"""Which parentless script processes count as left behind.

A launcher that starts a server and exits leaves the server without a parent
on purpose (the Startup-folder "Server dashboard" and "Screener web" do this),
so a missing parent alone is not enough.

    python -m unittest discover -s tests
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import belt  # noqa: E402


class Leftovers(unittest.TestCase):
    def test_console_script_whose_parent_closed_is_left_behind(self):
        self.assertEqual(belt.orphan_kind("python.exe", serving=False), "leftover")

    def test_parentless_process_serving_a_port_is_a_background_server(self):
        self.assertEqual(belt.orphan_kind("python.exe", serving=True), "server")

    def test_windowless_pythonw_is_meant_to_run_in_the_background(self):
        self.assertEqual(belt.orphan_kind("pythonw.exe", serving=False), "server")


class Describe(unittest.TestCase):
    def test_script_is_named_by_its_file(self):
        self.assertEqual(belt.describe_cmd(r"pythonw.exe C:\x\server-dashboard\server.py"), "server.py")

    def test_module_run_is_named_by_the_module(self):
        self.assertEqual(belt.describe_cmd(
            r"C:\S\.venv\Scripts\pythonw.exe -m dashweb run --port 8010"), "python -m dashweb")


if __name__ == "__main__":
    unittest.main()
