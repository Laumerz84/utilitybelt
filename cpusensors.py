"""CPU temperature (and power) from LibreHardwareMonitor's web server. Standard library only.

Windows gives ordinary programs no CPU temperature: reading it takes a kernel driver and admin rights.
LibreHardwareMonitor has both (with the PawnIO driver) and, with "Run web server" on, serves its sensor
tree as JSON on http://127.0.0.1:8085/data.json. UtilityBelt only reads that page, so it keeps running
without admin. No LibreHardwareMonitor running: read() returns None and nothing is shown.

Set up on the PC it was made on: G:\\Tools\\LibreHardwareMonitor, started at login by the scheduled task
"LibreHardwareMonitor" (setup-autostart.ps1 there).

    python cpusensors.py      # print what it reads now
"""
from __future__ import annotations

import json
import re
import time
import urllib.request

URL = "http://127.0.0.1:8085/data.json"
RETRY_AFTER = 30.0          # seconds to wait before trying again when nothing answers

# The temperature to headline, best first. AMD Ryzen: Tctl/Tdie is what AMD's own tools show.
# Intel: CPU Package.
HEADLINE = ("Core (Tctl/Tdie)", "Tctl/Tdie", "CPU Package", "Core (Tctl)", "Tdie", "Package")

# AMD stops boosting at Tjmax (89 °C on X3D chips, 95 °C on most others). Warn a bit below it.
WARN, CRIT = 85.0, 92.0


def _number(text) -> float | None:
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(text or ""))
    return float(m.group(0).replace(",", ".")) if m else None


def _walk(node: dict, path: tuple = ()):
    yield node, path
    for child in node.get("Children") or []:
        yield from _walk(child, path + (node.get("Text") or "",))


def parse(tree: dict) -> dict | None:
    """The CPU's readings from LibreHardwareMonitor's tree: {"name", "temp", "temps": {label: C},
    "power"} or None if it has no CPU temperature."""
    cpu = None
    for node, _ in _walk(tree):
        image = str(node.get("ImageURL") or "").lower()
        if "cpu" in image and node.get("Children"):
            cpu = node
            break
    if cpu is None:
        return None
    temps, power = {}, None
    for group in cpu.get("Children") or []:
        kind = (group.get("Text") or "").lower()
        for sensor in group.get("Children") or []:
            value = _number(sensor.get("Value"))
            if value is None:
                continue
            if kind.startswith("temperature"):
                temps[sensor.get("Text") or "?"] = value
            elif kind.startswith("power") and power is None and "package" in (sensor.get("Text") or "").lower():
                power = value
    if not temps:
        return None
    head = next((temps[k] for k in HEADLINE if k in temps), max(temps.values()))
    return {"name": cpu.get("Text") or "CPU", "temp": head, "temps": temps, "power": power}


class LhmReader:
    """Reads LibreHardwareMonitor's web server; backs off when it isn't there."""

    def __init__(self, url: str = URL) -> None:
        self.url = url
        self._next_try = 0.0
        self.ok = False                       # True after a successful read

    def read(self) -> dict | None:
        if not self.ok and time.time() < self._next_try:
            return None
        try:
            with urllib.request.urlopen(self.url, timeout=1.0) as resp:
                reading = parse(json.load(resp))
        except (OSError, ValueError):
            reading = None
        self.ok = reading is not None
        if not self.ok:
            self._next_try = time.time() + RETRY_AFTER
        return reading


def heat_level(temp: float | None) -> str:
    if temp is None:
        return "ok"
    return "crit" if temp >= CRIT else "warn" if temp >= WARN else "ok"


if __name__ == "__main__":
    print(LhmReader().read() or "No CPU temperature: is LibreHardwareMonitor running with its web server on?")
