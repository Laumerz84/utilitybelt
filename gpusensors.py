"""AMD GPU temperature, fan, power and clocks from the driver's own ADL library.

Windows' performance counters give GPU load and video memory but no sensors.
AMD's driver ships atiadlxx.dll (ADL); its Overdrive "PM log" query returns
up to 256 sensor slots, read here with ctypes: no admin rights, nothing to
install. On a PC without an AMD driver everything returns None.

Sensor slots (ADL's ADLSensorType) used, as reported by an RX 9070 XT:
  1 core clock MHz      2 memory clock MHz    8 edge temp C     9 memory temp C
  14 fan RPM            15 fan %              19 GPU busy %     21 core voltage mV
  27 hotspot temp C     73 power W
The CPU's built-in Radeon graphics is listed too, but reports no edge or
hotspot temperature, which is how the real card is picked.
"""
from __future__ import annotations

import ctypes
import sys

SLOTS = {"core_clock": 1, "mem_clock": 2, "edge": 8, "memory": 9, "fan_rpm": 14,
         "fan_pct": 15, "busy": 19, "voltage": 21, "hotspot": 27, "power": 73}

# (warn, crit) in C. AMD cards slow themselves down around 110 C hotspot.
LIMITS = {"hotspot": (100, 108), "edge": (85, 95), "memory": (95, 105)}


def pick_adapter(adapters: list) -> dict | None:
    """The discrete card: the adapter reporting a GPU temperature, with the
    most sensors if more than one does."""
    cards = [a for a in adapters if SLOTS["edge"] in a["sensors"] or SLOTS["hotspot"] in a["sensors"]]
    return max(cards, key=lambda a: len(a["sensors"])) if cards else None


def label(sensors: dict) -> dict:
    """Raw slot -> value into named readings; a missing sensor is None, not 0."""
    out = {name: sensors.get(slot) for name, slot in SLOTS.items()}
    if out["voltage"] is not None:
        out["voltage"] = out["voltage"] / 1000                  # mV -> V
    return out


def heat_level(r: dict) -> str:
    """'ok', 'warn' or 'crit' - the worst of the three temperatures."""
    worst = "ok"
    for name, (warn, crit) in LIMITS.items():
        value = r.get(name)
        if value is None:
            continue
        if value >= crit:
            return "crit"
        if value >= warn:
            worst = "warn"
    return worst


class AdlReader:
    """Reads the discrete AMD card's sensors. .read() -> labelled dict or None."""

    def __init__(self) -> None:
        self.ok = False
        if sys.platform != "win32":
            return
        try:
            from ctypes import POINTER, Structure, WINFUNCTYPE, byref, c_char, c_int, c_void_p
            self._adl = ctypes.WinDLL("atiadlxx.dll")
        except OSError:
            return                                              # no AMD driver
        self._buffers = []

        @WINFUNCTYPE(c_void_p, c_int)
        def alloc(size):
            buf = ctypes.create_string_buffer(size)
            self._buffers.append(buf)
            return ctypes.addressof(buf)

        class AdapterInfo(Structure):
            _fields_ = [("iSize", c_int), ("iAdapterIndex", c_int), ("strUDID", c_char * 256),
                        ("iBusNumber", c_int), ("iDeviceNumber", c_int), ("iFunctionNumber", c_int),
                        ("iVendorID", c_int), ("strAdapterName", c_char * 256),
                        ("strDisplayName", c_char * 256), ("iPresent", c_int), ("iExist", c_int),
                        ("strDriverPath", c_char * 256), ("strDriverPathExt", c_char * 256),
                        ("strPNPString", c_char * 256), ("iOSDisplayIndex", c_int)]

        class Sensor(Structure):
            _fields_ = [("supported", c_int), ("value", c_int)]

        class PMLog(Structure):
            _fields_ = [("size", c_int), ("sensors", Sensor * 256)]

        self._alloc, self._PMLog, self._byref = alloc, PMLog, byref
        self._ctx = c_void_p()
        try:
            if self._adl.ADL2_Main_Control_Create(alloc, 1, byref(self._ctx)) != 0:
                return
            n = c_int()
            self._adl.ADL2_Adapter_NumberOfAdapters_Get(self._ctx, byref(n))
            infos = (AdapterInfo * max(1, n.value))()
            self._adl.ADL2_Adapter_AdapterInfo_Get(self._ctx, infos, ctypes.sizeof(infos))
            adapters, seen = [], set()
            for a in infos[:n.value]:
                if not a.iPresent or a.iBusNumber in seen:
                    continue
                seen.add(a.iBusNumber)
                sensors = self._query(a.iAdapterIndex)
                if sensors is not None:
                    adapters.append({"name": a.strAdapterName.decode(errors="replace"),
                                     "index": a.iAdapterIndex, "sensors": sensors})
            card = pick_adapter(adapters)
            if card:
                self.name, self.index, self.ok = card["name"], card["index"], True
        except Exception:
            self.ok = False

    def _query(self, index: int) -> dict | None:
        log = self._PMLog()
        log.size = ctypes.sizeof(self._PMLog)
        if self._adl.ADL2_New_QueryPMLogData_Get(self._ctx, index, self._byref(log)) != 0:
            return None
        return {i: log.sensors[i].value for i in range(256) if log.sensors[i].supported}

    def read(self) -> dict | None:
        if not self.ok:
            return None
        try:
            sensors = self._query(self.index)
        except Exception:
            return None
        return label(sensors) if sensors else None
