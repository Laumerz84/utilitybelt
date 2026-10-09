#!/usr/bin/env python
"""UtilityBelt - interactive dashboard for this machine and the Claude work on it.

    python belt.py            # full interactive dashboard
    python belt.py --mini     # start as the small always-visible strip
    python belt.py --probe    # print one sample of every metric and exit

Three levels of detail:
  mini       a three-line strip for a corner of the screen. Chosen automatically
             when the window is small, or with --mini / the m key.
  standard   eight cards, each with one headline number and a 5-minute graph.
  detail     click a card or press its number (1-8); h opens the health view.
             Escape goes back.

Everything repaints once a second; nothing here starts, stops, or changes
anything on the machine. The one exception is the update check: at start and
every 6 hours a background `git fetch` asks GitHub whether a newer UtilityBelt
exists (update_check.py). If so, the hint line says so and u shows what's new
and the command to run - nothing is ever updated automatically.

Display rules
-------------
Problems first: the top bar names whatever needs attention, or says all good.
Colour carries meaning only - grey is normal, yellow is worth a look, red needs
action. Each card leads with one number; everything else lives in its detail
view. Lines never wrap; they end in an ellipsis instead.

Why the sampling is threaded
----------------------------
A 1 Hz repaint cannot afford to block. psutil counters are cheap and read on a
background thread; the GPU is not. There is no nvidia-smi on this box (the card
is an AMD RX 9070 XT), so GPU load has to come from Windows performance
counters, and Get-Counter costs 200-400 ms per call. Spawning PowerShell every
second would cost more than the frame. Instead one PowerShell process is
started once and left running, printing a JSON line per second that a reader
thread consumes. The UI only ever touches already-sampled values.

Slower facts (Ollama's loaded models, the Windows event log, RAM and BIOS
details) come from a third thread on their own clocks: Ollama every 5 s, the
event log every 5 min, hardware inventory once at start.

Metric selection follows what btop and glances consider the useful set: per-core
CPU rather than just an average, memory split by cached/available, disk I/O
rates alongside capacity, network throughput, and per-process attribution.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psutil
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

import agentlog
import gpusensors
import update_check


def escape(text) -> str:
    """Make any text safe to show inside Textual markup. Every "[" is escaped:
    rich's and Textual's own escape functions both let some code through
    (e.g. `[[T("x", c="dim")]]` from an agent's tool input) and the whole view
    then fails to render."""
    return str(text).replace("[", "\\[")


_JOB = None                    # Windows job that owns our child processes
UPDATE_EVERY = 6 * 3600         # seconds between checks for a newer version on GitHub


def _die_with_this_process(proc) -> None:
    """Put `proc` in a job object that is closed when this process ends, however
    it ends (window closed, killed, crash). Without it the GPU PowerShell loop
    outlived every closed belt and piled up: seven orphans polling the GPU
    counters found on 2026-09-23, costing a CPU core and stream stutter."""
    global _JOB
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.OpenProcess.restype = wintypes.HANDLE
        if _JOB is None:
            job = k32.CreateJobObjectW(None, None)
            if not job:
                return

            class _Limits(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                            ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD),
                            ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t),
                            ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t),
                            ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class _IoCounters(ctypes.Structure):
                _fields_ = [(n, ctypes.c_uint64) for n in (
                    "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                    "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

            class _ExtLimits(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", _Limits),
                            ("IoInfo", _IoCounters),
                            ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t),
                            ("PeakProcessMemoryUsed", ctypes.c_size_t),
                            ("PeakJobMemoryUsed", ctypes.c_size_t)]

            info = _ExtLimits()
            info.BasicLimitInformation.LimitFlags = 0x2000   # KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
                return                                         # 9 = ExtendedLimitInformation
            _JOB = job                                         # keep open for our lifetime
        handle = k32.OpenProcess(0x0001 | 0x0100, False, proc.pid)  # TERMINATE | SET_QUOTA
        if handle:
            k32.AssignProcessToJobObject(_JOB, handle)
            k32.CloseHandle(handle)
    except Exception:
        pass                                                   # best effort; never break the belt


HOME = Path.home()
CLAUDE = HOME / ".claude"
PROJECTS = CLAUDE / "projects"

HIST = 300                      # samples kept per series (5 minutes at 1 Hz)
AGENT_LIVE_SECONDS = 90         # an agent transcript touched more recently than
                                # this is treated as still working
SESSION_RECENT_HOURS = 12       # chats older than this are not "waiting on you"
EVENT_DAYS = 7                  # how far back the health view reads the event log
ALERT_EVENT_HOURS = 72          # how recent an event must be to reach the top bar

PRICES = {                      # $ per 1M tokens: in, out, cache_read, cache_write
    "claude-fable-5-1": (10, 50, .25, 20), "claude-fable-5": (10, 50, .25, 20),
    "claude-opus-5-5": (5, 25, .5, 10),
    "claude-opus-5": (5, 25, .5, 10), "claude-opus-4-8": (5, 25, .5, 10),
    "claude-sonnet-5": (2, 10, .2, 4), "claude-sonnet-4-6": (3, 15, .3, 6),
    "claude-haiku-4-5": (1, 5, .1, 2),
}
LOCAL = ("gpt-oss", "qwen", "llama", "mistral")

OLLAMA_PS = "http://127.0.0.1:11434/api/ps"

# Script interpreters that are only ever meant to run under a parent. One of
# these whose parent has gone is almost always something left behind.
LEFTOVER_NAMES = {"powershell.exe", "pwsh.exe", "python.exe", "pythonw.exe", "node.exe"}

# DDR5 speed grades, for reading a kit's rated speed out of its part number
# (CMK32GX5M2B6400Z36 -> 6400). Windows only reports the JEDEC speed.
DDR_GRADES = ("4800", "5200", "5600", "6000", "6200", "6400", "6600", "6800",
              "7000", "7200", "7600", "8000", "8200", "8400")


# --------------------------------------------------------------- formatting

def human_bytes(n: float) -> str:
    n = float(n or 0)
    for unit, size in (("TB", 1024 ** 4), ("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= size:
            return f"{n / size:.1f} {unit}"
    return f"{n:.0f} B"


def rate(n: float) -> str:
    n = float(n or 0)
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB/s"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} MB/s"
    if n >= 1024:
        return f"{n / 1024:.0f} KB/s"
    return f"{n:.0f} B/s"


def duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m {s}s"


# Severity is the only thing colour is used for. Every bar, graph, number and
# border goes through these so "yellow" means the same thing everywhere.
OK, WARN, CRIT = "ok", "warn", "crit"
FILL = {OK: "grey62", WARN: "yellow", CRIT: "red"}
NUM = {OK: "bold", WARN: "bold yellow", CRIT: "bold red"}


def level(value: float, warn: float, crit: float) -> str:
    if value >= crit:
        return CRIT
    if value >= warn:
        return WARN
    return OK


def worst(*levels: str) -> str:
    return CRIT if CRIT in levels else WARN if WARN in levels else OK


def bar(frac: float, width: int = 18, lvl: str = OK) -> str:
    frac = max(0.0, min(1.0, float(frac or 0)))
    filled = int(round(frac * width))
    return f"[{FILL[lvl]}]{'█' * filled}[/][grey30]{'─' * (width - filled)}[/]"


BLOCKS = " ▁▂▃▄▅▆▇█"


def squeeze(points: list[float], width: int) -> list[float]:
    """Fit a history into `width` columns, keeping each column's peak so a
    short spike is never averaged away."""
    if width <= 0:
        return []
    if len(points) <= width:
        return points
    step = len(points) / width
    return [max(points[int(i * step):int((i + 1) * step)] or [0.0]) for i in range(width)]


def chart(points, width: int, height: int = 1, top: float = 100.0, lvl: str = OK) -> list[str]:
    """A block-character graph `height` rows tall, top row first. History
    grows in from the right; zero reads as a faint baseline, not as nothing."""
    vals = squeeze(list(points), width)
    pad = width - len(vals)
    top = top or 1.0
    # Graphs stay grey even when the reading is bad: a 90% memory graph is a
    # solid slab, and a solid yellow slab shouts louder than the problem. The
    # number and the card border carry the severity instead.
    colour = "grey58"
    rows = []
    for r in range(height - 1, -1, -1):
        cells = []
        for v in vals:
            eighths = max(0.0, min(1.0, v / top)) * height * 8 - r * 8
            idx = 0 if eighths <= 0 else 8 if eighths >= 8 else int(round(eighths))
            if r == 0 and v > 0 and idx == 0:
                idx = 1                                # never draw a live value as nothing
            cells.append(BLOCKS[idx])
        body = "".join(cells)
        if r == 0:
            base = body.replace(" ", "\0")             # mark empty bottom cells
            body = "".join(f"[grey27]▁[/]" if ch == "\0" else f"[{colour}]{ch}[/]" for ch in base)
            rows.append(" " * pad + body)
        else:
            rows.append(" " * pad + f"[{colour}]{body}[/]")
    return rows


def disk_level(d: dict) -> str:
    free, total = d["free"], d["total"] or 1
    if free < 15 * 1024 ** 3 or free / total < 0.05:
        return CRIT
    if free < 40 * 1024 ** 3 and free / total < 0.15 or free / total < 0.10:
        return WARN
    return OK


def rated_speed(part: str) -> int:
    part = (part or "").upper()
    m = re.search(r"KF5(\d{2})C", part)                 # Kingston Fury: KF560C36 -> 6000
    if m:
        return int(m.group(1)) * 100
    for grade in reversed(DDR_GRADES):
        if re.search(rf"(?<!\d){grade}(?!\d)", part):
            return int(grade)
    return 0


def ps_json(script: str, timeout: float = 30):
    """Run one PowerShell script that prints JSON; None on any failure."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return json.loads(out.stdout) if out.stdout.strip() else None
    except Exception:
        return None


# ----------------------------------------------------------------- sampling

class Series:
    """A bounded history that also answers 'what is it doing now'."""

    def __init__(self) -> None:
        self.points: deque[float] = deque(maxlen=HIST)   # empty until sampled

    def push(self, value: float) -> None:
        self.points.append(float(value or 0))

    @property
    def now(self) -> float:
        return self.points[-1] if self.points else 0.0

    @property
    def peak(self) -> float:
        return max(self.points) if self.points else 0.0

    @property
    def mean(self) -> float:
        return sum(self.points) / len(self.points) if self.points else 0.0

    def recent(self, n: int) -> float:
        """Average of the last n samples - steadier than `now` for alerts."""
        pts = list(self.points)[-n:]
        return sum(pts) / len(pts) if pts else 0.0

    def tail(self, n: int = 40) -> list[float]:
        pts = list(self.points)[-n:]
        return pts or [0.0]


GPU_STREAMER = r"""
$ErrorActionPreference = 'SilentlyContinue'
$vramTotal = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\*' |
    Where-Object { $_.'HardwareInformation.qwMemorySize' } |
    Measure-Object -Property 'HardwareInformation.qwMemorySize' -Maximum).Maximum
while ($true) {
    $eng = (Get-Counter '\GPU Engine(*)\Utilization Percentage').CounterSamples |
           Where-Object { $_.CookedValue -gt 0 }
    $byType = @{}
    foreach ($s in $eng) {
        if ($s.InstanceName -match 'engtype_(\w+)') {
            $t = $Matches[1]
            $byType[$t] = [double]$byType[$t] + $s.CookedValue
        }
    }
    $mem = (Get-Counter '\GPU Adapter Memory(*)\Dedicated Usage').CounterSamples |
           Measure-Object -Property CookedValue -Sum
    [pscustomobject]@{
        total = [double]($eng | Measure-Object CookedValue -Sum).Sum
        types = $byType
        vram_used = [double]$mem.Sum
        vram_total = [double]$vramTotal
    } | ConvertTo-Json -Compress -Depth 3
    Start-Sleep -Milliseconds 850
}
"""

SYSINFO_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
[pscustomobject]@{
    ram = @(Get-CimInstance Win32_PhysicalMemory | ForEach-Object { [pscustomobject]@{
        maker = "$($_.Manufacturer)".Trim(); part = "$($_.PartNumber)".Trim()
        slot = "$($_.DeviceLocator)"; size = [double]$_.Capacity
        speed = [int]$_.Speed; configured = [int]$_.ConfiguredClockSpeed } })
    board = (Get-CimInstance Win32_BaseBoard | ForEach-Object { "$($_.Manufacturer) $($_.Product)".Trim() })
    bios = (Get-CimInstance Win32_BIOS | ForEach-Object { [pscustomobject]@{
        version = "$($_.SMBIOSBIOSVersion)"; date = $_.ReleaseDate.ToString('yyyy-MM-dd') } })
    cpu = "$((Get-CimInstance Win32_Processor | Select-Object -First 1).Name)".Trim()
    gpus = @(Get-CimInstance Win32_VideoController | ForEach-Object { [pscustomobject]@{
        name = "$($_.Name)"; driver = "$($_.DriverVersion)"
        date = $(if ($_.DriverDate) { $_.DriverDate.ToString('yyyy-MM-dd') } else { '' }) } })
} | ConvertTo-Json -Compress -Depth 4
"""

# Everything in the event log that says the hardware or a driver misbehaved.
# Readable without admin. Takes ~0.3 s, so it runs every few minutes.
EVENTS_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$since = (Get-Date).AddDays(-__DAYS__)
$filters = @(
    @{LogName='System'; ProviderName='Microsoft-Windows-WHEA-Logger'; StartTime=$since},
    @{LogName='System'; ProviderName='Display'; Id=4101; StartTime=$since},
    @{LogName='System'; ProviderName='Microsoft-Windows-Kernel-Power'; Id=41; StartTime=$since},
    @{LogName='System'; ProviderName='Microsoft-Windows-WER-SystemErrorReporting'; Id=1001; StartTime=$since},
    @{LogName='Application'; ProviderName='Application Error'; Id=1000; StartTime=$since})
$out = foreach ($f in $filters) {
    Get-WinEvent -FilterHashtable $f -MaxEvents 60 | ForEach-Object { [pscustomobject]@{
        t = $_.TimeCreated.ToString('s'); id = $_.Id; p = $_.ProviderName
        m = "$(($_.Message -split "`n")[0])".Trim() } }
}
ConvertTo-Json -InputObject @($out) -Compress
"""

EVENT_KINDS = {                 # (provider, id) -> (kind, severity)
    ("Microsoft-Windows-WHEA-Logger", None): ("hardware error", CRIT),
    ("Display", 4101): ("GPU driver crashed and recovered", WARN),
    ("Microsoft-Windows-Kernel-Power", 41): ("unexpected shutdown or restart", WARN),
    ("Microsoft-Windows-WER-SystemErrorReporting", 1001): ("blue screen", CRIT),
    ("Application Error", 1000): ("app crash", OK),
}


class Sampler:
    """Owns every reading. The UI never calls psutil or PowerShell directly."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.stop = threading.Event()

        self.cpu = Series()
        self.cpu_cores: list[Series] = []
        self.mem = Series()
        self.gpu = Series()
        self.vram = Series()
        self.gpu_temp = Series()                  # edge temperature, C (AMD cards via ADL)
        self.gpu_power = Series()                 # W
        self.net_up = Series()
        self.net_down = Series()
        self.disk_read = Series()
        self.disk_write = Series()

        self.snap: dict = {
            "cpu_freq": 0.0, "cpu_freq_max": 0.0,
            "cpu_physical": psutil.cpu_count(logical=False) or 0,
            "cpu_logical": psutil.cpu_count(logical=True) or 0,
            "ctx_switches": 0, "interrupts": 0,
            "mem": None, "swap": None,
            "disks": [], "nets": [], "procs": [],
            "gpu_types": {}, "gpu_adapters": [],
            "boot": psutil.boot_time(),
            "claude": {"sessions": [], "today_calls": 0, "today_cost": 0.0,
                       "models": {}, "scanned": 0},
            "agents": [],
            # The first transcript pass reads every .jsonl under projects/ and
            # takes a few seconds. Until it lands, those panels must say so
            # rather than confidently reporting zero.
            "claude_ready": False,
            "ollama": None,             # None until first poll; {"up": bool, "models": [...]}
            "sysinfo": None,            # RAM modules, board, BIOS, CPU, GPU driver
            "events": None,             # event-log entries from the last EVENT_DAYS
            "helpers": {"sessions": 0, "count": 0, "rss": 0},
            "leftovers": [],
            "tailscale": None,          # None = not installed, else up/down
            "errors": [],
        }

        self._proc_cache: dict[int, psutil.Process] = {}
        self._cmd_cache: dict[tuple, str] = {}
        self._last_net = psutil.net_io_counters()
        self._last_disk = psutil.disk_io_counters()
        self._last_cpu_times = psutil.cpu_stats()
        self._seen_files: dict = {}
        self._own = {os.getpid()} | {p.pid for p in psutil.Process().parents()}

        threading.Thread(target=self._fast_loop, daemon=True).start()
        threading.Thread(target=self._slow_loop, daemon=True).start()
        threading.Thread(target=self._gpu_loop, daemon=True).start()
        threading.Thread(target=self._info_loop, daemon=True).start()

        # Live agent feed: an AgentLog per followed transcript, read incrementally.
        self._agent_logs: dict[Path, agentlog.AgentLog] = {}
        self._watched: set[Path] = set()          # open in the agent view: follow even when idle
        self._titles: dict[str, str] = {}         # session id -> chat title, from _sample_claude
        threading.Thread(target=self._agent_loop, daemon=True).start()

    # -- fast: every second, cheap counters only
    def _fast_loop(self) -> None:
        psutil.cpu_percent(percpu=True)          # prime; first call is garbage
        adl = gpusensors.AdlReader()             # AMD temperature/fan/power; .ok False elsewhere
        tick = 0
        while not self.stop.wait(1.0):
            tick += 1
            if adl.ok and tick % 2 == 0:         # every 2 s; a read takes ~0.3 ms
                reading = adl.read()
                with self.lock:
                    self.snap["gpu_sensors"] = reading
                    if reading:
                        self.gpu_temp.push(reading["edge"] or 0)
                        self.gpu_power.push(reading["power"] or 0)
            try:
                cores = psutil.cpu_percent(percpu=True)
                vm = psutil.virtual_memory()
                sw = psutil.swap_memory()
                net = psutil.net_io_counters()
                disk = psutil.disk_io_counters()
                stats = psutil.cpu_stats()
                freq = psutil.cpu_freq()

                with self.lock:
                    if len(self.cpu_cores) != len(cores):
                        self.cpu_cores = [Series() for _ in cores]
                    for s, v in zip(self.cpu_cores, cores):
                        s.push(v)
                    self.cpu.push(sum(cores) / len(cores) if cores else 0)
                    self.mem.push(vm.percent)
                    self.net_up.push(max(0, net.bytes_sent - self._last_net.bytes_sent))
                    self.net_down.push(max(0, net.bytes_recv - self._last_net.bytes_recv))
                    if disk and self._last_disk:
                        self.disk_read.push(max(0, disk.read_bytes - self._last_disk.read_bytes))
                        self.disk_write.push(max(0, disk.write_bytes - self._last_disk.write_bytes))
                    self.snap["mem"] = vm
                    self.snap["swap"] = sw
                    self.snap["cpu_freq"] = getattr(freq, "current", 0) or 0
                    self.snap["cpu_freq_max"] = getattr(freq, "max", 0) or 0
                    self.snap["ctx_switches"] = max(0, stats.ctx_switches - self._last_cpu_times.ctx_switches)
                    self.snap["interrupts"] = max(0, stats.interrupts - self._last_cpu_times.interrupts)
                self._last_net, self._last_disk, self._last_cpu_times = net, disk, stats
            except Exception as exc:                      # a dead counter must not kill the loop
                self._note(f"fast: {exc}")

    # -- slow: every few seconds, the expensive walks
    def _slow_loop(self) -> None:
        tick = 0
        while not self.stop.wait(3.0):
            tick += 1
            try:
                self._sample_processes()
                self._sample_disks()
                self._sample_nets()
            except Exception as exc:
                self._note(f"slow: {exc}")
            if tick % 7 == 1:                              # ~every 21s
                try:
                    self._sample_claude()
                except Exception as exc:
                    self._note(f"claude: {exc}")

    def _cmdline(self, pid: int, created: float) -> str:
        key = (pid, created)                               # pid alone gets reused
        if key not in self._cmd_cache:
            try:
                self._cmd_cache[key] = " ".join(psutil.Process(pid).cmdline())
            except (psutil.Error, OSError):
                self._cmd_cache[key] = ""
        return self._cmd_cache[key]

    def _sample_processes(self) -> None:
        rows = []
        alive = set()
        for proc in psutil.process_iter(["pid", "name", "memory_info", "num_threads",
                                         "ppid", "create_time"]):
            try:
                pid = proc.info["pid"]
                if pid == 0:                               # System Idle Process: idle time, not a program
                    continue
                alive.add(pid)
                cached = self._proc_cache.get(pid)
                if cached is None:
                    cached = proc
                    self._proc_cache[pid] = cached
                    cached.cpu_percent(None)               # prime this pid
                    cpu = 0.0
                else:
                    cpu = cached.cpu_percent(None)
                mem = proc.info["memory_info"]
                rows.append({
                    "pid": pid,
                    "name": proc.info["name"] or "?",
                    "cpu": cpu / max(1, psutil.cpu_count(logical=True)),
                    "rss": getattr(mem, "rss", 0) or 0,
                    "threads": proc.info.get("num_threads") or 0,
                    "ppid": proc.info.get("ppid") or 0,
                    "created": proc.info.get("create_time") or 0,
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        for pid in list(self._proc_cache):
            if pid not in alive:
                self._proc_cache.pop(pid, None)
        for key in list(self._cmd_cache):
            if key[0] not in alive:
                self._cmd_cache.pop(key, None)
        rows.sort(key=lambda r: -r["cpu"])
        helpers, leftovers = self._helpers(rows)
        with self.lock:
            self.snap["procs"] = rows
            self.snap["helpers"] = helpers
            self.snap["leftovers"] = leftovers
            self.snap["servers"] = self._servers

    def _helpers(self, rows: list[dict]) -> tuple[dict, list[dict]]:
        """Split background processes into the ones Claude Code chats own
        (expected: each open chat runs its own MCP servers) and ones whose
        parent has gone (leftovers)."""
        by_pid = {r["pid"]: r for r in rows}
        children: dict = defaultdict(list)
        for r in rows:
            children[r["ppid"]].append(r)

        backends = [r for r in rows if r["name"].lower() == "claude.exe"
                    and "stream-json" in self._cmdline(r["pid"], r["created"])]
        count = rss = 0
        for b in backends:
            stack = list(children[b["pid"]])
            while stack:
                c = stack.pop()
                count += 1
                rss += c["rss"]
                stack.extend(children[c["pid"]])

        listening: dict = defaultdict(set)                # pid -> ports it listens on
        try:
            for c in psutil.net_connections(kind="inet"):
                if c.status == psutil.CONN_LISTEN and c.pid:
                    listening[c.pid].add(c.laddr.port)
        except (psutil.Error, OSError):
            pass

        def ports(pid: int) -> set:
            found, stack = set(listening.get(pid, ())), list(children[pid])
            while stack:
                c = stack.pop()
                found |= listening.get(c["pid"], set())
                stack.extend(children[c["pid"]])
            return found

        leftovers, servers = [], []
        for r in rows:
            if r["name"].lower() not in LEFTOVER_NAMES or r["pid"] in self._own:
                continue
            parent = by_pid.get(r["ppid"])
            if parent and parent["created"] <= r["created"]:
                continue                                   # parent alive (and not a reused pid)
            cmd = self._cmdline(r["pid"], r["created"])
            if "GPU Engine" in cmd:
                what = "GPU counter loop from a closed UtilityBelt"
            else:
                what = describe_cmd(cmd) or r["name"]
            served = ports(r["pid"])
            record = dict(r, what=what, age=time.time() - r["created"], ports=sorted(served))
            (servers if orphan_kind(r["name"], bool(served)) == "server" else leftovers).append(record)
        self._servers = servers
        return {"sessions": len(backends), "count": count, "rss": rss}, leftovers

    def _sample_disks(self) -> None:
        out = []
        per_disk = psutil.disk_io_counters(perdisk=True) or {}
        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except (PermissionError, OSError):
                continue
            if usage.total < 10 * 1024 ** 3:               # skip tiny/removable
                continue
            out.append({
                "device": part.device.rstrip("\\"),
                "fstype": part.fstype,
                "total": usage.total, "used": usage.used,
                "free": usage.free, "percent": usage.percent,
            })
        out.sort(key=lambda d: d["device"])
        with self.lock:
            self.snap["disks"] = out
            self.snap["per_disk"] = {
                k: {"read": v.read_bytes, "write": v.write_bytes,
                    "rcount": v.read_count, "wcount": v.write_count}
                for k, v in per_disk.items()
            }

    def _sample_nets(self) -> None:
        per_nic = psutil.net_io_counters(pernic=True) or {}
        stats = psutil.net_if_stats() or {}
        out = []
        tailscale = None
        for name, st in stats.items():
            if "tailscale" in name.lower():
                tailscale = bool(tailscale) or st.isup
        for name, io in per_nic.items():
            st = stats.get(name)
            if st is None or not st.isup:
                continue
            if io.bytes_sent == 0 and io.bytes_recv == 0:
                continue
            out.append({
                "name": name, "sent": io.bytes_sent, "recv": io.bytes_recv,
                "pkt_sent": io.packets_sent, "pkt_recv": io.packets_recv,
                "errin": io.errin, "errout": io.errout,
                "dropin": io.dropin, "dropout": io.dropout,
                "speed": getattr(st, "speed", 0) or 0,
            })
        out.sort(key=lambda n: -(n["sent"] + n["recv"]))
        with self.lock:
            self.snap["nets"] = out
            self.snap["tailscale"] = tailscale

    # -- GPU: one long-lived PowerShell, read line by line
    def _gpu_loop(self) -> None:
        if sys.platform != "win32":
            return
        try:
            proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", GPU_STREAMER],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            self._note(f"gpu spawn: {exc}")
            return
        _die_with_this_process(proc)
        buf = ""
        for line in proc.stdout:                            # blocks; own thread
            if self.stop.is_set():
                break
            buf += line.strip()
            if not buf.endswith("}"):
                continue
            try:
                data = json.loads(buf)
            except json.JSONDecodeError:
                buf = ""
                continue
            buf = ""
            with self.lock:
                self.gpu.push(min(100.0, data.get("total") or 0))
                total = data.get("vram_total") or 0
                used = data.get("vram_used") or 0
                self.vram.push((used / total * 100) if total else 0)
                self.snap["gpu_types"] = data.get("types") or {}
                self.snap["vram_used"] = used
                self.snap["vram_total"] = total
        try:
            proc.terminate()
        except Exception:
            pass

    # -- info: Ollama every 5 s, event log every 5 min, inventory once
    def _info_loop(self) -> None:
        tick = 0
        while not self.stop.is_set():
            try:
                self._sample_ollama()
            except Exception as exc:
                self._note(f"ollama: {exc}")
            if sys.platform == "win32":
                if tick == 0:
                    info = ps_json(SYSINFO_PS)
                    with self.lock:
                        self.snap["sysinfo"] = info or {}
                if tick % 60 == 0:
                    self._sample_events()
            tick += 1
            if self.stop.wait(5.0):
                break

    def _sample_ollama(self) -> None:
        try:
            with urllib.request.urlopen(OLLAMA_PS, timeout=1.0) as resp:
                data = json.load(resp)
        except Exception:
            with self.lock:
                self.snap["ollama"] = {"up": False, "models": []}
            return
        models = []
        for m in data.get("models") or []:
            expires = m.get("expires_at") or ""
            try:                                           # Ollama writes 7 fraction digits
                until = datetime.fromisoformat(re.sub(r"\.\d+", "", expires)).timestamp()
            except ValueError:
                until = 0
            models.append({"name": (m.get("name") or "?").removesuffix(":latest"),
                           "vram": m.get("size_vram") or 0, "size": m.get("size") or 0,
                           "params": (m.get("details") or {}).get("parameter_size") or "",
                           "context": m.get("context_length") or 0, "until": until})
        with self.lock:
            self.snap["ollama"] = {"up": True, "models": models}

    def _sample_events(self) -> None:
        raw = ps_json(EVENTS_PS.replace("__DAYS__", str(EVENT_DAYS)))
        if raw is None:
            return
        out = []
        for e in raw if isinstance(raw, list) else [raw]:
            key = (e.get("p"), e.get("id"))
            kind, sev = EVENT_KINDS.get(key) or EVENT_KINDS.get((e.get("p"), None)) or ("event", OK)
            detail = e.get("m") or ""
            m = re.search(r"Faulting application name: ([^,]+)", detail)
            if m:
                detail = m.group(1)
            try:
                when = datetime.fromisoformat(e.get("t") or "").timestamp()
            except ValueError:
                continue
            out.append({"when": when, "kind": kind, "sev": sev, "detail": detail})
        out.sort(key=lambda e: -e["when"])
        with self.lock:
            self.snap["events"] = out

    # -- Claude transcripts and live agents
    def _sample_claude(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        sessions: list[dict] = []
        agents: list[dict] = []
        today_models: dict = defaultdict(lambda: [0, 0, 0, 0])
        all_models: dict = defaultdict(lambda: [0, 0, 0, 0])
        today_calls = 0
        now = time.time()

        if not PROJECTS.exists():
            return
        for path in PROJECTS.rglob("*.jsonl"):
            try:
                st = path.stat()
            except OSError:
                continue
            cached = self._seen_files.get(path)
            if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
                info = cached[2]
            else:
                info = self._read_transcript(path, today)
                self._seen_files[path] = (st.st_mtime, st.st_size, info)
            if not info:
                continue

            age = now - st.st_mtime
            record = dict(info, age=age, path=path)
            if path.name.startswith("agent-"):
                record["live"] = age < AGENT_LIVE_SECONDS
                agents.append(record)
            else:
                record["state"] = session_state(info["last_kind"], age)
                sessions.append(record)
                today_calls += info["today_calls"]
                for model, vals in info["today_models"].items():
                    for i in range(4):
                        today_models[model][i] += vals[i]
            for model, vals in info["models"].items():
                for i in range(4):
                    all_models[model][i] += vals[i]

        sessions.sort(key=lambda s: -s["cost"])
        agents.sort(key=lambda a: (not a["live"], a["age"]))
        with self.lock:
            self.snap["claude"] = {
                "sessions": sessions,
                "today_calls": today_calls,
                "today_cost": sum(cost_of(m, v) for m, v in today_models.items()),
                "today_models": dict(today_models),
                "models": dict(all_models),
                "scanned": len(sessions) + len(agents),
            }
            self.snap["agents"] = agents
            self.snap["claude_ready"] = True
            self._titles = {s["path"].stem: s["title"] for s in sessions}

    # -- live agents: every second, only the lines appended to working agents' logs
    def _agent_loop(self) -> None:
        """Follow working subagents. Finding them costs a stat per agent log
        (every 3 s); reading them costs only what was appended since last time."""
        tick = 0
        candidates: list[Path] = []
        while not self.stop.wait(1.0):
            try:
                now = time.time()
                if tick % 3 == 0 and PROJECTS.exists():
                    candidates = []
                    for path in PROJECTS.glob("*/*/subagents/**/agent-*.jsonl"):
                        try:
                            if now - path.stat().st_mtime < AGENT_LIVE_SECONDS:
                                candidates.append(path)
                        except OSError:
                            continue
                tick += 1
                with self.lock:
                    follow = set(candidates) | self._watched
                for path in follow:
                    log = self._agent_logs.get(path)
                    if log is None:
                        log = self._agent_logs[path] = agentlog.AgentLog(path)
                    with self.lock:
                        log.poll()
                live = []
                for path in candidates:
                    log = self._agent_logs[path]
                    live.append(self._agent_summary(log, now))
                live.sort(key=lambda a: a["started"])
                with self.lock:
                    self.snap["live_agents"] = live
                    for path in list(self._agent_logs):     # forget agents nobody is following
                        if path not in follow and len(self._agent_logs) > 40:
                            del self._agent_logs[path]
            except Exception as exc:
                self._note(f"agents: {exc}")

    def _agent_summary(self, log: agentlog.AgentLog, now: float) -> dict:
        evs = log.events
        meta = log.meta
        session_dir = next((p for p in log.path.parents if p.name == "subagents"), log.path.parent).parent
        workflow = next((p.name for p in log.path.parents if p.name.startswith("wf_")), "")
        return {
            "path": log.path,
            "id": log.path.stem.replace("agent-", "")[:10],
            "desc": meta.get("description") or next((e["text"][:60] for e in evs if e["kind"] == "task"), ""),
            "type": meta.get("agentType") or "", "model": meta.get("model") or "",
            "chat": self._titles.get(session_dir.name, ""), "workflow": workflow,
            "started": evs[0]["ts"] if evs else now,
            "tools": sum(1 for e in evs if e["kind"] == "tool"),
            "errors": sum(1 for e in evs if e["kind"] == "result" and e["error"]),
            "now": agentlog.now_step(evs),
            "recent": [e for e in evs if e["kind"] not in ("task", "system")][-8:],
        }

    def watch_agent(self, path: Path, on: bool = True) -> None:
        """Keep following an agent while its view is open, even after it goes quiet."""
        with self.lock:
            (self._watched.add if on else self._watched.discard)(path)

    def agent_view(self, path: Path) -> tuple[list[dict], dict]:
        """A copy of one agent's events (read so far) and its summary."""
        log = self._agent_logs.get(path)
        if log is None:
            log = self._agent_logs[path] = agentlog.AgentLog(path)
        with self.lock:
            if not log.events:
                log.poll()
            return list(log.events), self._agent_summary(log, time.time())

    @staticmethod
    def _read_transcript(path: Path, today: str) -> dict | None:
        seen: set = set()
        models: dict = defaultdict(lambda: [0, 0, 0, 0])
        today_models: dict = defaultdict(lambda: [0, 0, 0, 0])
        calls = today_calls = 0
        title = custom_title = ""
        last_tool = ""
        last_text = ""
        last_kind = ""
        try:
            fh = path.open(encoding="utf-8", errors="replace")
        except OSError:
            return None
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") == "custom-title" and entry.get("customTitle"):
                    custom_title = str(entry["customTitle"])
                message = entry.get("message") or {}
                if entry.get("type") == "user" and not entry.get("isSidechain"):
                    last_kind = "user"
                    if not title:
                        content = message.get("content")
                        if isinstance(content, str):
                            title = content.strip().replace("\n", " ")[:60]
                if entry.get("type") != "assistant":
                    continue
                has_tool = False
                for block in (message.get("content") or []):
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        last_tool = block.get("name") or ""
                        has_tool = True
                    elif block.get("type") == "text" and block.get("text"):
                        last_text = str(block["text"]).strip().replace("\n", " ")[:70]
                if not entry.get("isSidechain"):
                    last_kind = ("end_turn" if message.get("stop_reason") == "end_turn"
                                 else "tool_use" if has_tool else "assistant")
                usage = message.get("usage")
                if not usage:
                    continue
                mid = message.get("id")
                if mid and mid in seen:
                    continue
                if mid:
                    seen.add(mid)
                calls += 1
                model = message.get("model") or "?"
                vals = (usage.get("input_tokens", 0),
                        usage.get("cache_creation_input_tokens", 0),
                        usage.get("cache_read_input_tokens", 0),
                        usage.get("output_tokens", 0))
                for i in range(4):
                    models[model][i] += vals[i]
                if (entry.get("timestamp") or "")[:10] == today:
                    today_calls += 1
                    for i in range(4):
                        today_models[model][i] += vals[i]
        if not calls:
            return None
        return {
            "id": path.stem.replace("agent-", "")[:10],
            "title": custom_title or title or last_text or "(no prompt text)",
            "calls": calls,
            "today_calls": today_calls,
            "models": dict(models),
            "today_models": dict(today_models),
            "cache_read": sum(v[2] for v in models.values()),
            "output": sum(v[3] for v in models.values()),
            "cost": sum(cost_of(m, v) for m, v in models.items()),
            "last_tool": last_tool,
            "last_kind": last_kind,
            "workflow": path.parent.name if path.parent.name.startswith("wf_") else "",
        }

    def _note(self, message: str) -> None:
        with self.lock:
            errs = self.snap.setdefault("errors", [])
            stamp = f"{datetime.now():%H:%M:%S} {message}"
            if stamp not in errs:
                errs.append(stamp)
                del errs[:-8]

    def read(self) -> dict:
        with self.lock:
            return dict(self.snap)


def describe_cmd(cmd: str) -> str:
    """A short name for a script process: its script file, or python -m <module>."""
    tokens = [t.strip('"') for t in cmd.split()]
    script = next((Path(t).name for t in tokens
                   if t.lower().endswith((".py", ".js", ".mjs", ".ps1"))), "")
    if script:
        return script
    if "-m" in tokens[:-1]:
        return f"python -m {tokens[tokens.index('-m') + 1]}"
    return cmd[:60]


def orphan_kind(name: str, serving: bool) -> str:
    """A script process whose parent has gone: left behind, or a background
    server started that way on purpose? Launchers (e.g. Startup-folder
    shortcuts) start a server and exit, so the server serving a port, or run
    windowless with pythonw, is deliberate."""
    if serving or name.lower() == "pythonw.exe":
        return "server"
    return "leftover"


def session_state(last_kind: str, age: float) -> str:
    """What a chat is doing, read from how its transcript ends.

    Claude finished its reply        -> waiting on you
    Written to in the last 90 s      -> working
    Ends on a tool call, then silent -> probably waiting for a permission
                                        prompt (or a very long command)
    """
    if age > SESSION_RECENT_HOURS * 3600:
        return "old"
    if last_kind == "end_turn":
        return "waiting"
    if age < AGENT_LIVE_SECONDS:
        return "working"
    if last_kind == "tool_use":
        return "approval"
    return "idle"


STATE_LABEL = {"working": "working", "waiting": "waiting on you",
               "approval": "may need approval", "idle": "idle", "old": "old"}


def cost_of(model: str, tokens) -> float:
    if any(x in model for x in LOCAL):
        return 0.0
    price = PRICES.get(model) or PRICES.get(model.rsplit("-2", 1)[0])
    if not price:
        return 0.0
    return (tokens[0] * price[0] + tokens[3] * price[1]
            + tokens[2] * price[2] + tokens[1] * price[3]) / 1e6


# ------------------------------------------------------------------- alerts

def ram_speed(snap: dict) -> tuple[int, int]:
    """(running MT/s, rated MT/s); 0 where unknown."""
    mods = (snap.get("sysinfo") or {}).get("ram") or []
    if isinstance(mods, dict):
        mods = [mods]
    running = min((m.get("configured") or m.get("speed") or 0 for m in mods), default=0)
    rated = min((rated_speed(m.get("part")) for m in mods), default=0)
    return running, rated


def compute_alerts(snap: dict, s: Sampler) -> list[dict]:
    """Everything that deserves the top bar, worst first. Each has a short
    label for the bar and a sentence for the health view."""
    out = []

    def add(lvl, short, long):
        out.append({"lvl": lvl, "short": short, "long": long})

    total = snap.get("vram_total") or 0
    if total:
        vpct = (snap.get("vram_used") or 0) / total * 100
        lvl = level(vpct, 90, 97)
        if lvl != OK:
            models = (snap.get("ollama") or {}).get("models") or []
            why = (f" Ollama is holding {models[0]['name']} "
                   f"({human_bytes(models[0]['vram'])}) until "
                   f"{datetime.fromtimestamp(models[0]['until']):%H:%M}."
                   if models and models[0]["until"] else "")
            add(lvl, f"VRAM {vpct:.0f}%",
                f"Video memory is {vpct:.0f}% full, so games and models may slow "
                f"down or fail to load.{why}")

    vm = snap.get("mem")
    if vm:
        lvl = level(s.mem.recent(30), 85, 93)
        if lvl != OK:
            add(lvl, f"memory {vm.percent:.0f}%",
                f"System memory is {vm.percent:.0f}% used; Windows will start "
                f"swapping to disk.")

    lvl = level(s.cpu.recent(60), 85, 95)
    if lvl != OK:
        add(lvl, f"CPU {s.cpu.recent(60):.0f}% for 1 min",
            "The processor has been nearly flat out for the last minute.")

    running, rated = ram_speed(snap)
    if running and rated and running < rated - 100:
        add(WARN, f"RAM speed {running} of {rated}",
            f"Your RAM is rated for {rated} MT/s but running at {running}. "
            f"EXPO is probably off in the BIOS (Ai Tweaker > Ai Overclock Tuner > EXPO I).")

    for d in snap.get("disks") or []:
        lvl = disk_level(d)
        if lvl != OK:
            add(lvl, f"{d['device']} {human_bytes(d['free'])} free",
                f"Drive {d['device']} has {human_bytes(d['free'])} free "
                f"({100 - d['percent']:.0f}%). disk-sentinel.py shows what is using it.")

    now = time.time()
    recent = [e for e in snap.get("events") or [] if now - e["when"] < ALERT_EVENT_HOURS * 3600]
    for kind in ("hardware error", "blue screen", "GPU driver crashed and recovered",
                 "unexpected shutdown or restart"):
        hits = [e for e in recent if e["kind"] == kind]
        if hits:
            sev = hits[0]["sev"]
            label = {"hardware error": "hardware error", "blue screen": "blue screen",
                     "GPU driver crashed and recovered": "GPU driver reset",
                     "unexpected shutdown or restart": "unexpected restart"}[kind]
            add(sev, f"{len(hits)} {label}{'s' if len(hits) > 1 else ''}",
                f"{len(hits)} × {kind} in the last {ALERT_EVENT_HOURS // 24} days, "
                f"most recently {datetime.fromtimestamp(hits[0]['when']):%a %H:%M}.")

    left = snap.get("leftovers") or []
    if left:
        add(WARN, f"{len(left)} leftover process{'es' if len(left) > 1 else ''}",
            f"{len(left)} script process(es) are still running although whatever "
            f"started them has closed. Details under h.")

    sensors = snap.get("gpu_sensors")
    if sensors:
        lvl = gpusensors.heat_level(sensors)
        if lvl != OK:
            name, value = max(((n, sensors.get(n)) for n in gpusensors.LIMITS if sensors.get(n) is not None),
                              key=lambda nv: nv[1] - gpusensors.LIMITS[nv[0]][0])
            what = {"hotspot": "GPU hotspot", "edge": "GPU", "memory": "GPU memory"}[name]
            add(lvl, f"{what} {value}°C",
                f"The graphics card's {what.replace('GPU ', '').replace('GPU', 'core')} temperature is "
                f"{value} °C (hotspot {sensors.get('hotspot')} °C, memory {sensors.get('memory')} °C, "
                f"fan {sensors.get('fan_pct')}%). AMD cards slow themselves down around 110 °C hotspot; "
                f"check the card's airflow and dust if this stays high.")

    out.sort(key=lambda a: a["lvl"] != CRIT)
    return out


# -------------------------------------------------------------------- views

class Card(Static):
    """One clickable panel on the standard grid."""

    def __init__(self, key: str, title: str, index: int) -> None:
        super().__init__(id=f"card-{key}", classes="card")
        self.key = key
        self.border_title = f"{index} {title}"

    def on_click(self, event: events.Click) -> None:
        self.app.open_detail(self.key)


def drag_target(window: tuple, grab: tuple, mouse: tuple) -> tuple:
    """Where the window goes: it moves exactly as far as the mouse has since the grab."""
    return window[0] + mouse[0] - grab[0], window[1] + mouse[1] - grab[1]


def is_click(press: tuple, release: tuple, slop: int = 4) -> bool:
    """A press and release this close together is a click, not a drag."""
    return abs(release[0] - press[0]) <= slop and abs(release[1] - press[1]) <= slop


BAR_BUTTONS_FULL = (("▾", "small"), ("–", "minimize"), ("×", "close"))
BAR_BUTTONS_SMALL = (("▴", "full"), ("–", "minimize"), ("×", "close"))
SMALL_COLS, SMALL_ROWS = 88, 4          # the small strip's window, in terminal cells
PEEK = 4                                # px of a hidden strip left showing at the top edge
HIDE_AFTER = 0.5                        # s the mouse must be away before it slides up


def button_strip(buttons) -> str:
    """The buttons as drawn at the very end of a bar line, two spaces apart."""
    return "  ".join(mark for mark, _ in buttons)


def button_at(x: int, width: int, buttons):
    """Which button (its action) column x of a `width`-wide line lands on, if any.
    Uses the same layout as button_strip, so drawing and clicking agree."""
    strip = button_strip(buttons)
    i = x - (width - len(strip))
    if 0 <= i < len(strip) and strip[i] != " ":
        return buttons[i // 3][1]
    return None


def small_rect(cell: tuple, edges: tuple, area: tuple,
               cols: int = SMALL_COLS, rows: int = SMALL_ROWS) -> tuple:
    """(x, y, w, h) for the small strip: `cols` x `rows` cells of the current
    size plus the window's own edges, at the top centre of the work area
    (left, top, right, bottom), like Zoom's meeting controls."""
    w = round(cols * cell[0]) + edges[0]
    h = round(rows * cell[1]) + edges[1]
    left, top, right, _ = area
    return left + (right - left - w) // 2, top, w, h


def hidden_y(top: int, height: int, peek: int = PEEK) -> int:
    """Window y when slid up out of sight, leaving `peek` px at the top edge."""
    return top - height + peek


def slide_steps(start: int, end: int, n: int = 6) -> list:
    """The y positions of a short ease-out slide from start to end."""
    return [round(start + (end - start) * (1 - (1 - i / n) ** 2)) for i in range(1, n + 1)]


def docks(y: int, top: int, snap: int = 24) -> bool:
    """A strip dropped this close to the top edge docks there (and hides)."""
    return y - top <= snap


def wants_open(mouse: tuple, left: int, width: int, top: int,
               peek: int = PEEK, slack: int = 2) -> bool:
    """Is the mouse on the sliver a hidden strip leaves at the top edge?"""
    return left <= mouse[0] < left + width and top - slack <= mouse[1] <= top + peek + slack


class WindowMover:
    """Finds the Windows Terminal window this program runs in and moves it.

    In focus mode the terminal has no title bar, so the top bar has to do its
    job. The console window Windows hands this process is owned by the
    terminal's real window (class CASCADIA_HOSTING_WINDOW_CLASS), which is what
    gets moved. Everything is a no-op off Windows or outside Windows Terminal.
    """

    def __init__(self) -> None:
        self.hwnd = None
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes
            self._wt = wintypes
            self._u32 = ctypes.WinDLL("user32", use_last_error=True)
            self._ct = ctypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.GetConsoleWindow.restype = wintypes.HWND
            self._u32.GetAncestor.restype = wintypes.HWND
            self._u32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
            console = k32.GetConsoleWindow()
            root = self._u32.GetAncestor(console, 3) if console else None   # GA_ROOTOWNER
            name = ctypes.create_unicode_buffer(64)
            if root and self._u32.GetClassNameW(root, name, 64) and \
                    name.value == "CASCADIA_HOSTING_WINDOW_CLASS":
                self.hwnd = root
        except Exception:
            self.hwnd = None

    def mouse(self) -> tuple:
        pt = self._wt.POINT()
        self._u32.GetCursorPos(self._ct.byref(pt))
        return pt.x, pt.y

    def button_down(self) -> bool:
        return bool(self._u32.GetAsyncKeyState(0x01) & 0x8000)     # VK_LBUTTON

    def position(self) -> tuple:
        r = self._wt.RECT()
        self._u32.GetWindowRect(self.hwnd, self._ct.byref(r))
        return r.left, r.top

    def move_to(self, x: int, y: int) -> None:
        # SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
        self._u32.SetWindowPos(self.hwnd, None, int(x), int(y), 0, 0, 0x0001 | 0x0004 | 0x0010)

    def rect(self) -> tuple:
        r = self._wt.RECT()
        self._u32.GetWindowRect(self.hwnd, self._ct.byref(r))
        return r.left, r.top, r.right - r.left, r.bottom - r.top

    def client_size(self) -> tuple:
        r = self._wt.RECT()
        self._u32.GetClientRect(self.hwnd, self._ct.byref(r))
        return r.right - r.left, r.bottom - r.top

    def set_rect(self, x: int, y: int, w: int, h: int) -> None:
        # SWP_NOZORDER | SWP_NOACTIVATE
        self._u32.SetWindowPos(self.hwnd, None, int(x), int(y), int(w), int(h), 0x0004 | 0x0010)

    def work_area(self) -> tuple:
        """(left, top, right, bottom) of the usable area (taskbar excluded) of this window's monitor."""
        ct, wt = self._ct, self._wt

        class MonitorInfo(ct.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                        ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]

        self._u32.MonitorFromWindow.restype = wt.HANDLE
        mon = self._u32.MonitorFromWindow(self.hwnd, 2)          # MONITOR_DEFAULTTONEAREST
        info = MonitorInfo()
        info.cbSize = ct.sizeof(MonitorInfo)
        if mon and self._u32.GetMonitorInfoW(wt.HANDLE(mon), ct.byref(info)):
            r = info.rcWork
            return r.left, r.top, r.right, r.bottom
        return 0, 0, 1920, 1040

    def topmost(self, on: bool) -> None:
        # HWND_TOPMOST / HWND_NOTOPMOST; SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
        self._u32.SetWindowPos(self.hwnd, self._wt.HWND(-1 if on else -2), 0, 0, 0, 0,
                               0x0002 | 0x0001 | 0x0010)

    def minimize(self) -> None:
        self._u32.ShowWindow(self.hwnd, 6)                       # SW_MINIMIZE

    def shadow(self, on: bool) -> None:
        """Windows draws a drop shadow below the window even when the window
        itself is slid up off screen. Switching off its non-client rendering
        (DWMWA_NCRENDERING_POLICY = disabled) removes the shadow; "use window
        style" puts it back."""
        value = self._ct.c_int(0 if on else 1)
        try:
            self._ct.windll.dwmapi.DwmSetWindowAttribute(
                self._wt.HWND(self.hwnd), 2, self._ct.byref(value), 4)
        except Exception:
            pass


class DragBar(Static):
    """A bar that stands in for the missing title bar: drag it to move the
    window, and while the mouse is over it, buttons appear at the right end of
    its last line.

    While the mouse button is held the real mouse position is polled (~60 Hz)
    rather than waiting for terminal mouse events: the window moves under the
    cursor, so the cursor barely moves relative to it and the terminal stops
    reporting."""

    BUTTONS = BAR_BUTTONS_FULL
    BUTTON_ROW = 0                                         # the text line the buttons are drawn on

    def on_mount(self) -> None:
        self._grab = None
        self.hover = False
        self.hot = None                                    # the button under the mouse

    def _button(self, event):
        """The button action under the mouse, if the event is on a button."""
        off = event.get_content_offset(self)
        if off is None or off.y != self.BUTTON_ROW:
            return None
        return button_at(off.x, self.content_size.width, self.BUTTONS)

    def _refresh(self) -> None:
        screen = self.screen
        if isinstance(screen, Overview):
            screen.redraw()

    def on_enter(self, event: events.Enter) -> None:
        self.hover = True
        self._refresh()

    def on_leave(self, event: events.Leave) -> None:
        self.hover, self.hot = False, None
        self._refresh()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        hot = self._button(event)
        if hot != self.hot:
            self.hot = hot
            self._refresh()

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 1:
            return
        if self._button(event):                            # buttons act on the click (release),
            return                                          # not here: see on_click
        mover = self.app.mover
        if mover.hwnd is None:
            return
        self._grab = (mover.position(), mover.mouse())
        self._dragging = False
        self._timer = self.set_interval(1 / 60, self._follow)

    def _follow(self) -> None:
        mover = self.app.mover
        window, grab = self._grab
        mouse = mover.mouse()
        if mover.button_down():
            self._dragging = self._dragging or not is_click(grab, mouse)
            if self._dragging:
                mover.move_to(*drag_target(window, grab, mouse))
            return
        self._timer.stop()                                  # released
        self._grab = None
        if self._dragging:
            self.on_drag_end()
        else:
            self.on_plain_click()

    def on_click(self, event: events.Click) -> None:
        # Buttons act on release: acting on press changes the layout under the
        # mouse, and the release then lands on (and clicks) whatever moved there.
        action = self._button(event)
        if action:
            self.app.bar_action(action)
        elif self.app.mover.hwnd is None:
            self.on_plain_click()                           # no dragging here: plain click

    def on_plain_click(self) -> None:
        pass

    def on_drag_end(self) -> None:
        pass


class TopBar(DragBar):
    """The standard view's top line: drag to move, click for Health."""

    BUTTONS = BAR_BUTTONS_FULL

    def on_plain_click(self) -> None:
        self.app.open_detail("health")


class MiniStrip(DragBar):
    """The small strip: drag anywhere to move; buttons on its last line."""

    BUTTONS = BAR_BUTTONS_SMALL
    BUTTON_ROW = 2                                         # third line, after the clock

    def on_drag_end(self) -> None:
        self.app.strip_dropped()


class Overview(Screen):
    CARDS = [
        ("cpu", "CPU"), ("memory", "MEMORY"), ("gpu", "GPU"), ("storage", "STORAGE"),
        ("network", "NETWORK"), ("claude", "CLAUDE"), ("agents", "AGENTS"),
        ("processes", "PROCESSES"),
    ]

    def compose(self) -> ComposeResult:
        yield TopBar(id="topbar")
        with Grid(id="grid"):
            for i, (key, title) in enumerate(self.CARDS, start=1):
                yield Card(key, title, i)
        yield Static(id="hint")
        yield MiniStrip(id="mini")

    def on_mount(self) -> None:
        self.set_interval(1.0, self.redraw)
        self.apply_mode()
        self.redraw()

    def on_resize(self, event: events.Resize) -> None:
        self.apply_mode()
        self.redraw()

    def is_mini(self) -> bool:
        forced = self.app.forced_mode
        if forced:
            return forced == "mini"
        return self.size.height < 16 or self.size.width < 90

    def apply_mode(self) -> None:
        self.set_class(self.is_mini(), "-mini")
        grid = self.query_one("#grid", Grid)
        wide = self.size.width >= 128
        grid.styles.grid_size_columns = 4 if wide else 2
        grid.styles.grid_size_rows = 2 if wide else 4

    def redraw(self) -> None:
        app = self.app
        snap = app.sampler.read()
        alerts = compute_alerts(snap, app.sampler)
        if self.is_mini():
            strip = self.query_one("#mini", MiniStrip)
            strip.update(app.render_mini(snap, alerts, self.size.width - 2, bar=strip))
            return
        bar = self.query_one("#topbar", TopBar)
        bar.update(app.render_topbar(alerts, self.size.width - 4, bar=bar))
        for key, _ in self.CARDS:
            try:
                card = self.query_one(f"#card-{key}", Card)
            except Exception:
                continue
            w = max(10, card.size.width - 4)
            h = max(3, card.size.height - 2)
            try:
                lines, lvl = getattr(app, f"_card_{key}")(snap, app.sampler, w, h)
            except Exception as exc:
                lines, lvl = [f"[red]{escape(type(exc).__name__)}: {escape(str(exc))}[/]"], WARN
            card.set_class(lvl == WARN, "warn")
            card.set_class(lvl == CRIT, "crit")
            card.update("\n".join(lines[:h]))
        errs = snap.get("errors") or []
        note = f"   [red]{escape(errs[-1])}[/]" if errs else ""
        info = app.update_info
        if info is not None and info.available:
            n = info.behind
            note = f"   [bold yellow]⬆ Update available ({n} new change{'s' if n != 1 else ''}) · u to update[/]" + note
        self.query_one("#hint", Static).update(
            "[dim]1-8 open a panel · h health · m mini view · q quit[/]" + note)


class Detail(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Back"), ("q", "app.pop_screen", "Back")]

    TITLES = {"cpu": "CPU", "memory": "Memory", "gpu": "GPU", "storage": "Storage",
              "network": "Network", "claude": "Claude", "agents": "Agents",
              "processes": "Processes", "health": "Health", "update": "Update"}

    def __init__(self, key: str) -> None:
        super().__init__()
        self.key = key

    def compose(self) -> ComposeResult:
        yield Static(id="dtitle")
        with VerticalScroll(id="detail-body"):
            yield Static(id="detail-content")
        yield Static(id="dhint")

    def on_mount(self) -> None:
        self.set_interval(1.0, self.redraw)
        self.redraw()

    def redraw(self) -> None:
        snap = self.app.sampler.read()
        self.query_one("#dtitle", Static).update(
            f"[bold]UtilityBelt[/] [dim]›[/] [bold]{self.TITLES.get(self.key, self.key)}[/]"
            f"   [dim]{datetime.now():%H:%M:%S}[/]")
        self.query_one("#detail-content", Static).update(
            self.app.render_detail(self.key, snap, max(40, self.size.width - 8)))
        self.query_one("#dhint", Static).update(
            "[dim]esc back · 1-8 other panels · h health · u update · q back[/]")


class AgentBlock(Static):
    """One agent in the Agents list; click (or Enter) opens its live view."""

    def __init__(self, path: Path) -> None:
        super().__init__(classes="agent-block")
        self.path = path

    def on_click(self, event: events.Click) -> None:
        self.app.open_agent(self.path)


class AgentsScreen(Detail):
    """Working agents as live blocks, then the recently finished ones."""

    BINDINGS = Detail.BINDINGS + [
        Binding("up", "move(-1)", "Up", priority=True),
        Binding("down", "move(1)", "Down", priority=True),
        Binding("enter", "open", "Open", priority=True),
    ]

    def __init__(self) -> None:
        super().__init__("agents")
        self.selected = 0
        self.paths: list[Path] = []

    def compose(self) -> ComposeResult:
        yield Static(id="dtitle")
        with VerticalScroll(id="detail-body"):
            yield Static(id="agents-head")
            yield Vertical(id="agent-blocks")
            yield Static(id="agents-foot")
        yield Static(id="dhint")

    def redraw(self) -> None:
        app = self.app
        snap = app.sampler.read()
        live = snap.get("live_agents") or []
        live_paths = {a["path"] for a in live}
        done = [a for a in snap.get("agents") or [] if a["path"] not in live_paths][:10]
        rows = [("live", a) for a in live] + [("done", a) for a in done]
        paths = [a["path"] for _, a in rows]
        box = self.query_one("#agent-blocks", Vertical)
        if paths != self.paths:                           # agent set changed: rebuild the blocks
            keep = self.paths[self.selected] if self.paths and self.selected < len(self.paths) else None
            box.remove_children()
            box.mount_all([AgentBlock(p) for p in paths])
            self.paths = paths
            self.selected = paths.index(keep) if keep in paths else 0
        width = max(40, self.size.width - 10)
        for i, (block, (kind, a)) in enumerate(zip(box.query(AgentBlock), rows)):
            block.set_class(i == self.selected, "-selected")
            block.set_class(kind == "done", "-done")
            block.update(app.render_agent_block(a, width) if kind == "live"
                         else app.render_agent_done(a))
        self.query_one("#dtitle", Static).update(
            f"[bold]UtilityBelt[/] [dim]›[/] [bold]Agents[/]   [dim]{datetime.now():%H:%M:%S}[/]")
        seen = (f"{len(snap.get('agents') or [])} seen in total" if snap.get("claude_ready")
                else "scanning older agents…")
        self.query_one("#agents-head", Static).update(
            f"[bold]{len(live)}[/] working now  [dim]· {seen} · click one, or ↑/↓ and Enter, "
            f"to watch it live[/]"
            + ("" if live else "\n\n[dim]No agent is working right now.[/]"))
        self.query_one("#agents-foot", Static).update(
            f"\n[dim]'Working' means its log was written to in the last {AGENT_LIVE_SECONDS}s. "
            f"Steps appear as each one finishes.[/]")
        self.query_one("#dhint", Static).update(
            "[dim]↑/↓ choose · enter or click to watch · esc back · 1-8 other panels · q back[/]")

    def action_move(self, step: int) -> None:
        if not self.paths:
            return
        self.selected = (self.selected + step) % len(self.paths)
        blocks = list(self.query(AgentBlock))
        for i, block in enumerate(blocks):
            block.set_class(i == self.selected, "-selected")
        if self.selected < len(blocks):
            blocks[self.selected].scroll_visible()

    def action_open(self) -> None:
        if self.paths:
            self.app.open_agent(self.paths[self.selected])


class AgentView(Detail):
    """Everything one agent has done, from its task on, following new steps
    as they land (like tail -f) while the view is scrolled to the bottom."""

    BINDINGS = Detail.BINDINGS + [("f", "follow", "Follow"), ("end", "follow", "Follow")]

    def __init__(self, path: Path) -> None:
        super().__init__("agent")
        self.path = path
        self.following = True
        self.rendered = -1

    def compose(self) -> ComposeResult:
        yield Static(id="dtitle")
        yield Static(id="agent-head")
        with VerticalScroll(id="detail-body"):
            yield Static(id="detail-content")
        yield Static(id="dhint")

    def on_mount(self) -> None:
        self.app.sampler.watch_agent(self.path)
        super().on_mount()

    def on_unmount(self) -> None:
        self.app.sampler.watch_agent(self.path, on=False)

    def redraw(self) -> None:
        app = self.app
        evs, a = app.sampler.agent_view(self.path)
        body = self.query_one("#detail-body", VerticalScroll)
        if self.rendered >= 0:                            # at the bottom = following
            self.following = body.scroll_y >= body.max_scroll_y - 1
        self.query_one("#dtitle", Static).update(
            f"[bold]UtilityBelt[/] [dim]› Agents ›[/] [bold]{escape(a['desc'][:70] or a['id'])}[/]"
            f"   [dim]{datetime.now():%H:%M:%S}[/]")
        self.query_one("#agent-head", Static).update(app.render_agent_head(a, evs))
        if len(evs) != self.rendered:
            self.query_one("#detail-content", Static).update(app.render_agent_feed(evs))
            self.rendered = len(evs)
            if self.following:
                self.call_after_refresh(body.scroll_end, animate=False)
        state = "[green]following new steps[/]" if self.following else "[yellow]paused[/] [dim]· f to follow[/]"
        self.query_one("#dhint", Static).update(
            f"{state}   [dim]scroll up to pause · f / end follow · esc back[/]")

    def action_follow(self) -> None:
        self.following = True
        self.query_one("#detail-body", VerticalScroll).scroll_end(animate=False)


# ---------------------------------------------------------------------- app

class Belt(App):
    CSS = """
    Screen { background: $surface; }
    #topbar { height: 1; padding: 0 2; background: $panel; }
    #grid {
        layout: grid;
        grid-size: 4 2;
        grid-gutter: 0 1;
        padding: 1 1 0 1;
    }
    .card {
        border: round $panel-lighten-3;
        border-title-color: $text-muted;
        border-title-style: bold;
        padding: 0 1;
        height: 100%;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    .card.warn { border: round $warning 70%; border-title-color: $warning; }
    .card.crit { border: round $error; border-title-color: $error; }
    .card:hover { border: round $accent; }
    #hint { height: 1; padding: 0 2; }
    #mini { display: none; padding: 0 1; text-wrap: nowrap; text-overflow: ellipsis; }
    Overview.-mini #topbar, Overview.-mini #grid, Overview.-mini #hint { display: none; }
    Overview.-mini #mini { display: block; height: 100%; }
    #dtitle { height: 1; padding: 0 2; background: $panel; }
    #detail-body { padding: 1 3; }
    #dhint { height: 1; padding: 0 2; }
    #agent-head { height: auto; padding: 0 3; background: $panel; }
    #agent-blocks { height: auto; }
    .agent-block { height: auto; padding: 0 1; margin: 1 0 0 0; border-left: blank; }
    .agent-block.-done { margin: 0; }
    .agent-block:hover { background: $boost; }
    .agent-block.-selected { border-left: thick $accent; background: $boost; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("1", "detail('cpu')", "CPU"),
        ("2", "detail('memory')", "Memory"),
        ("3", "detail('gpu')", "GPU"),
        ("4", "detail('storage')", "Storage"),
        ("5", "detail('network')", "Network"),
        ("6", "detail('claude')", "Claude"),
        ("7", "detail('agents')", "Agents"),
        ("8", "detail('processes')", "Processes"),
        ("h", "detail('health')", "Health"),
        ("m", "toggle_mini", "Mini"),
        ("u", "detail('update')", "Update"),
        ("c", "copy_update", "Copy update command"),
    ]

    TITLE = "UtilityBelt"

    def __init__(self, sampler: Sampler, mini: bool = False) -> None:
        super().__init__()
        self.sampler = sampler
        self.mover = WindowMover()          # lets the top bar drag a title-bar-less window
        self.forced_mode = "mini" if mini else None
        self.update_info: update_check.UpdateInfo | None = None
        self.update_checked_at: datetime | None = None
        self._update_thread: threading.Thread | None = None

    def on_mount(self) -> None:
        self.push_screen(Overview())
        self.start_update_check()
        self.set_interval(UPDATE_EVERY, self.start_update_check)

    # ---- is there a newer UtilityBelt on GitHub? (background; never blocks a frame)

    def start_update_check(self) -> None:
        if self._update_thread is not None and self._update_thread.is_alive():
            return

        def run() -> None:
            info = update_check.check()
            if info is not None:  # offline etc. keeps the last answer
                self.update_info = info
            self.update_checked_at = datetime.now()

        self._update_thread = threading.Thread(target=run, name="update-check", daemon=True)
        self._update_thread.start()

    def action_copy_update(self) -> None:
        info = self.update_info
        if info is None or not info.available:
            return
        text = "\n".join(info.commands())
        try:
            subprocess.run(["clip"], input=text, text=True, check=True, timeout=5,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError):
            self.copy_to_clipboard(text)  # terminals that support OSC 52
        self.notify("Update command copied - quit UtilityBelt (q), then paste it into a terminal.")

    def action_detail(self, key: str) -> None:
        self.open_detail(key)

    def action_toggle_mini(self) -> None:
        while isinstance(self.screen, Detail):
            self.pop_screen()
        overview = self.screen
        if isinstance(overview, Overview):
            self.set_small(not overview.is_mini())

    def set_small(self, small: bool) -> None:
        """Small mode: the strip view in a small window in the screen's top-left
        corner. Leaving it puts the window back where and as big as it was."""
        mover = self.mover
        if mover.hwnd is not None:
            x, y, w, h = mover.rect()
            cw, ch = mover.client_size()
            cell = (cw / max(1, self.size.width), ch / max(1, self.size.height))
            if small:
                self._full_rect = (x, y, w, h)
                area = mover.work_area()
                mover.set_rect(*small_rect(cell, (w - cw, h - ch), area))
                mover.topmost(True)
                self._start_autohide(area[1])
            else:
                self._stop_autohide()
                mover.topmost(False)
                mover.shadow(True)
                if getattr(self, "_full_rect", None):
                    mover.set_rect(*self._full_rect)
                else:                                       # started small: grow to a standard size
                    mover.set_rect(x, y, round(150 * cell[0]) + w - cw, round(42 * cell[1]) + h - ch)
        self.forced_mode = "mini" if small else "full"
        if isinstance(self.screen, Overview):
            self.screen.apply_mode()
            self.screen.redraw()

    # ---- slide-away small mode: docked at the top edge, the strip slides up out
    # of sight leaving a thin sliver, and slides down when the mouse reaches it.

    def _start_autohide(self, top: int) -> None:
        self._dock_top, self._docked, self._shown = top, True, True
        self._away_since, self._sliding = None, False
        self._stop_autohide()
        self._autohide_timer = self.set_interval(0.05, self._autohide)

    def _stop_autohide(self) -> None:
        timer = getattr(self, "_autohide_timer", None)
        if timer is not None:
            timer.stop()
            self._autohide_timer = None

    def _autohide(self) -> None:
        mover = self.mover
        if not self._docked or self._sliding or mover.button_down():
            return                                          # not docked, mid-slide, or being dragged
        x, y, w, h = mover.rect()
        mouse = mover.mouse()
        if not self._shown:
            if wants_open(mouse, x, w, self._dock_top):
                self._slide(self._dock_top, shown=True)
            return
        inside = x - 8 <= mouse[0] <= x + w + 8 and y - 8 <= mouse[1] <= y + h + 8
        if inside:
            self._away_since = None
        elif self._away_since is None:
            self._away_since = time.time()
        elif time.time() - self._away_since >= HIDE_AFTER:
            self._slide(hidden_y(self._dock_top, h), shown=False)

    def _slide(self, target_y: int, shown: bool) -> None:
        x, y, _, _ = self.mover.rect()
        steps = slide_steps(y, target_y)
        self._sliding, self._shown, self._away_since = True, shown, None
        if shown:
            self.mover.shadow(True)                         # back before it comes into view

        def step() -> None:
            self.mover.move_to(x, steps.pop(0))
            if not steps:
                timer.stop()
                self._sliding = False
                if not shown:
                    self.mover.shadow(False)                # hidden: no shadow left on screen

        timer = self.set_interval(0.025, step)

    def strip_dropped(self) -> None:
        """After dragging the strip: near the top edge it docks (and hides when
        the mouse leaves); anywhere else it stays put and visible."""
        if self.mover.hwnd is None or getattr(self, "_autohide_timer", None) is None:
            return
        x, y, _, _ = self.mover.rect()
        self._docked = docks(y, self._dock_top)
        self.mover.shadow(True)
        if self._docked:
            self.mover.move_to(x, self._dock_top)
            self._shown, self._away_since = True, None

    def bar_action(self, action: str) -> None:
        if action in ("small", "full"):
            self.set_small(action == "small")
        elif action == "minimize" and self.mover.hwnd is not None:
            self.mover.minimize()
        elif action == "close":
            self.exit()

    def open_detail(self, key: str) -> None:
        while isinstance(self.screen, Detail):
            self.pop_screen()
        self.push_screen(AgentsScreen() if key == "agents" else Detail(key))

    def open_agent(self, path: Path) -> None:
        self.push_screen(AgentView(path))

    # ---- agents

    @staticmethod
    def _event_line(e: dict, width: int) -> str:
        """One step as a single line: time, kind mark, the gist."""
        t = f"[dim]{datetime.fromtimestamp(e['ts']):%H:%M:%S}[/]" if e["ts"] else "[dim]--:--:--[/]"
        first = escape(agentlog._first_line(e["text"])[:width])
        kind = e["kind"]
        if kind == "tool":
            return f"{t}  [bold]▸ {escape(e['tool'])}[/] {first}"
        if kind == "output":
            return f"{t}  ✎ {first}"
        if kind == "thinking":
            return f"{t}  [dim italic]· thinking{': ' + first if first else ''}[/]"
        if kind == "result" and e["error"]:
            return f"{t}  [red]✗ {first or 'failed'}[/]"
        if kind == "result":
            return f"{t}  [dim]← {first or '(no output)'}[/]"
        return f"{t}  [dim]task: {first}[/]"

    @staticmethod
    def _since(ts: float) -> str:
        return duration(time.time() - ts) if ts else "—"

    def render_agent_block(self, a: dict, width: int) -> str:
        step = a["now"]
        waited = time.time() - step["since"] if step["since"] else 0
        slow = "yellow" if step["label"] not in ("thinking", "writing") and waited > 120 else "bold"
        where = " · ".join(x for x in (a["chat"] and f"from {escape(a['chat'][:40])}",
                                       a["workflow"] and escape(a["workflow"][:16])) if x)
        errs = f" · [red]{a['errors']} failed[/]" if a["errors"] else ""
        lines = [f"[green]●[/] [bold]{escape(a['desc'][:width - 30] or a['id'])}[/]  "
                 f"[dim]{escape(a['type'])} · {escape(a['model'])} · running {self._since(a['started'])} · "
                 f"{a['tools']} tool calls[/]{errs}",
                 f"  [dim]{where}[/]" if where else "",
                 f"  now  [{slow}]{escape(step['label'][:width - 20])}[/]  [dim]for {duration(waited)}[/]"]
        lines += [f"  {self._event_line(e, width - 14)}" for e in a["recent"]]
        return "\n".join(x for x in lines if x)

    def render_agent_done(self, a: dict) -> str:
        """A finished agent from the full transcript scan: one compact line."""
        desc = self._agent_desc(a["path"]) or a["title"]
        return (f"[dim]○[/] {escape(desc[:60])}  [dim]{a['calls']} calls · last tool "
                f"{escape(a['last_tool'] or '—')} · finished {duration(a['age'])} ago[/]")

    def _agent_desc(self, path: Path) -> str:
        cache = self.__dict__.setdefault("_desc_cache", {})
        if path not in cache:
            try:
                cache[path] = json.loads(path.with_name(path.stem + ".meta.json")
                                         .read_text(encoding="utf-8")).get("description") or ""
            except (OSError, ValueError):
                cache[path] = ""
        return cache[path]

    def render_agent_head(self, a: dict, evs: list[dict]) -> str:
        step = a["now"]
        live = evs and time.time() - max(e["ts"] for e in evs[-3:]) < AGENT_LIVE_SECONDS
        where = " · ".join(x for x in (a["chat"] and f"from {escape(a['chat'][:50])}",
                                       a["workflow"] and escape(a["workflow"])) if x)
        status = (f"now [bold]{escape(step['label'][:90])}[/] [dim]for {self._since(step['since'])}[/]"
                  if live else f"[dim]finished · last step {self._since(step['since'])} ago[/]")
        return (f"[dim]{escape(a['type'])} · {escape(a['model'])} · {where} · started "
                f"{self._since(a['started'])} ago · {a['tools']} tool calls"
                + (f" · [red]{a['errors']} failed[/]" if a["errors"] else "") + f"[/]\n{status}")

    @staticmethod
    def render_agent_feed(evs: list[dict], limit: int = 500) -> str:
        """The whole log, each step labelled by what it is. Output is Claude's
        actual words, full brightness; thinking is dim italic; tools show their
        full input; results are cut to their first lines."""
        out = []
        if len(evs) > limit:
            out.append(f"[dim]… {len(evs) - limit} earlier steps not shown[/]\n")
            evs = evs[-limit:]

        def clip(text: str, n: int) -> tuple[str, str]:
            lines = text.rstrip().splitlines()
            more = f"\n[dim]  (+{len(lines) - n} more lines)[/]" if len(lines) > n else ""
            return escape("\n".join(lines[:n])), more

        hidden_thinking = 0
        for e in evs:
            if e["kind"] == "thinking" and not e["text"].strip():
                hidden_thinking += 1
                continue
            if hidden_thinking:
                out.append(f"[dim italic]thinking{f' ×{hidden_thinking}' if hidden_thinking > 1 else ''}"
                           f" — not recorded[/]\n")
                hidden_thinking = 0
            t = f"[dim]{datetime.fromtimestamp(e['ts']):%H:%M:%S}[/]  " if e["ts"] else ""
            kind = e["kind"]
            if kind == "task":
                text, more = clip(e["text"], 8)
                out.append(f"{t}[bold]TASK[/]\n[dim]{text}[/]{more}\n")
            elif kind == "system":
                inner = e["text"].replace("<system-reminder>", "").replace("</system-reminder>", "").strip()
                text, more = clip(inner, 2)
                out.append(f"{t}[dim]NOTE FROM CLAUDE CODE\n{text}[/]{more}\n")
            elif kind == "thinking":
                out.append(f"{t}[dim italic]THINKING\n{escape(e['text'].rstrip())}[/]\n")
            elif kind == "output":
                out.append(f"{t}[bold]OUTPUT[/]\n{escape(e['text'].rstrip())}\n")
            elif kind == "tool":
                if e["tool"] in ("Bash", "PowerShell"):
                    detail = escape(json.loads(e["full"]).get("command", "")) if e["full"] else ""
                    out.append(f"{t}[bold]▸ {escape(e['tool'])}[/]\n{detail}\n")
                else:
                    text, more = clip(e["full"], 15)
                    out.append(f"{t}[bold]▸ {escape(e['tool'])}[/]  {escape(e['text'])}\n[dim]{text}[/]{more}\n")
            elif kind == "result" and e["error"]:
                text, more = clip(e["text"], 12)
                out.append(f"{t}[red]✗ FAILED\n{text}[/]{more}\n")
            else:
                text, more = clip(e["text"], 6)
                out.append(f"{t}[dim]← RESULT\n{text or '(no output)'}[/]{more}\n")
        if hidden_thinking:
            out.append(f"[dim italic]thinking{f' ×{hidden_thinking}' if hidden_thinking > 1 else ''}"
                       f" — not recorded[/]")
        return "\n".join(out) or "[dim]Nothing in this agent's log yet.[/]"

    # ---- top bar and mini strip

    @staticmethod
    def _alert_text(alerts: list[dict], limit: int = 4) -> str:
        if not alerts:
            return "[green]✓[/] [dim]all good[/]"
        shown = " [dim]·[/] ".join(
            f"[{FILL[a['lvl']]}]{escape(a['short'])}[/]" for a in alerts[:limit])
        more = f" [dim]+{len(alerts) - limit} more[/]" if len(alerts) > limit else ""
        mark = "[red]●[/]" if alerts[0]["lvl"] == CRIT else "[yellow]⚠[/]"
        return f"{mark} {shown}{more}"

    @staticmethod
    def _line(left: str, right: str, width: int) -> Text:
        """left-aligned markup and a right-aligned tail on one line, the left
        side cut short (with an ellipsis) rather than wrapping."""
        l, r = Text.from_markup(left), Text.from_markup(right)
        room = width - r.cell_len - 2
        if l.cell_len > room:
            l.truncate(max(0, room), overflow="ellipsis")
        l.append(" " * max(1, width - l.cell_len - r.cell_len))
        l.append_text(r)
        return l

    @staticmethod
    def _buttons(bar) -> str:
        """The hover buttons as markup, or nothing when the mouse is elsewhere."""
        if bar is None or not getattr(bar, "hover", False):
            return ""
        marks = [f"[bold]{m}[/]" if a == bar.hot else f"[dim]{m}[/]" for m, a in bar.BUTTONS]
        return "   " + "  ".join(marks)

    def render_topbar(self, alerts: list[dict], width: int, bar=None) -> Text:
        return self._line(f"[bold]UtilityBelt[/]   {self._alert_text(alerts)}",
                          f"[dim]{datetime.now():%H:%M:%S}[/]{self._buttons(bar)}", width)

    def render_mini(self, snap: dict, alerts: list[dict], width: int, bar=None) -> Text:
        s = self.sampler
        vtotal = snap.get("vram_total") or 0
        vpct = (snap.get("vram_used") or 0) / vtotal * 100 if vtotal else 0
        vm = snap.get("mem")
        cpu_l = level(s.cpu.recent(10), 85, 95)
        v_l = level(vpct, 90, 97)
        m_l = level(vm.percent if vm else 0, 85, 93)
        spark_w = 8 if width >= 100 else 5

        parts = [
            f"CPU [{NUM[cpu_l]}]{s.cpu.now:3.0f}%[/] {chart(s.cpu.points, spark_w, lvl=cpu_l)[0]}",
            f"GPU [bold]{s.gpu.now:3.0f}%[/]{self._gpu_temp(snap)} {chart(s.gpu.points, spark_w)[0]}",
            f"VRAM [{NUM[v_l]}]{vpct:.0f}%[/]",
            f"MEM [{NUM[m_l]}]{vm.percent if vm else 0:.0f}%[/]",
        ]
        tight = sorted(snap.get("disks") or [], key=lambda d: d["free"])
        if tight:
            d = tight[0]
            parts.append(f"{d['device']} [{NUM[disk_level(d)]}]{human_bytes(d['free'])}[/] free")
        line1 = "   ".join(parts)

        net = f"↓ {rate(s.net_down.now)}  ↑ {rate(s.net_up.now)}"
        ts = snap.get("tailscale")
        if ts is not None:
            net += "   Tailscale " + ("[green]✓[/]" if ts else "[yellow]off[/]")
        line2 = f"{net}   {self._claude_summary(snap)}"

        info = self.update_info
        update = "[yellow]⬆ update (u)[/]  " if info is not None and info.available else ""
        return Text("\n").join([
            self._line(line1, "", width),
            self._line(line2, "", width),
            self._line(self._alert_text(alerts, limit=3),
                       f"{update}[dim]{datetime.now():%H:%M}[/]{self._buttons(bar)}", width),
        ])

    @staticmethod
    def _claude_summary(snap: dict) -> str:
        if not snap.get("claude_ready"):
            return "[dim]Claude: scanning…[/]"
        c = snap["claude"]
        states = Counter(x["state"] for x in c["sessions"])
        live = sum(1 for a in snap.get("agents") or [] if a["live"])
        bits = [f"Claude [bold]${c['today_cost']:,.0f}[/] today"]
        if states["waiting"]:
            bits.append(f"[bold]{states['waiting']}[/] waiting on you")
        if states["approval"]:
            bits.append(f"[yellow]{states['approval']} may need approval[/]")
        if states["working"] or live:
            bits.append(f"{states['working'] + live} working")
        return " · ".join(bits)

    # ---- standard cards: (lines, severity)

    @staticmethod
    def _graph_rows(h: int, fixed: int) -> int:
        """Graphs take whatever height the card has left over."""
        return max(1, min(12, h - fixed))

    def _card_cpu(self, snap, s, w, h):
        lvl = level(s.cpu.recent(10), 85, 95)
        freq = snap.get("cpu_freq") or 0
        busiest = max((c.now for c in s.cpu_cores), default=0)
        lines = [f"[{NUM[lvl]}]{s.cpu.now:.0f}%[/]  [dim]{freq / 1000:.1f} GHz[/]"]
        lines += chart(s.cpu.points, w, self._graph_rows(h, 2), lvl=lvl)
        lines.append(f"[dim]busiest core {busiest:.0f}% · 5 min avg {s.cpu.mean:.0f}%[/]")
        return lines, lvl

    def _card_memory(self, snap, s, w, h):
        vm = snap.get("mem")
        if not vm:
            return ["[dim]sampling…[/]"], OK
        lvl = level(s.mem.recent(30), 85, 93)
        lines = [f"[{NUM[lvl]}]{vm.percent:.0f}%[/]  [dim]{human_bytes(vm.used)} of "
                 f"{human_bytes(vm.total)}[/]"]
        lines += chart(s.mem.points, w, self._graph_rows(h, 2), lvl=lvl)
        running, rated = ram_speed(snap)
        if running and rated and running < rated - 100:
            lines.append(f"[yellow]RAM {running} MT/s · rated {rated}[/]")
            lvl = worst(lvl, WARN)
        elif running:
            lines.append(f"[dim]RAM {running} MT/s · {human_bytes(vm.available)} free[/]")
        else:
            lines.append(f"[dim]{human_bytes(vm.available)} free[/]")
        return lines, lvl

    def _card_gpu(self, snap, s, w, h):
        used = snap.get("vram_used") or 0
        total = snap.get("vram_total") or 0
        if total == 0 and s.gpu.now == 0:
            return ["[dim]waiting for counters…[/]"], OK
        vpct = used / total * 100 if total else 0
        v_l = level(vpct, 90, 97)
        sensors = snap.get("gpu_sensors")
        heat = ""
        if sensors:
            fan = f"  [dim]fan {sensors['fan_pct']}%[/]" if sensors.get("fan_pct") is not None else ""
            heat = f" {self._gpu_temp(snap)}{fan}"
            v_l = worst(v_l, gpusensors.heat_level(sensors))
        lines = [f"[bold]{s.gpu.now:.0f}%[/] [dim]load[/]   VRAM [{NUM[level(vpct, 90, 97)]}]{vpct:.0f}%[/]{heat}"]
        lines += chart(s.gpu.points, w, self._graph_rows(h, 2))
        ol = snap.get("ollama")
        if ol is None:
            lines.append("[dim]checking Ollama…[/]")
        elif not ol["up"]:
            lines.append("[dim]Ollama not running[/]")
        elif ol["models"]:
            m = ol["models"][0]
            more = f" +{len(ol['models']) - 1}" if len(ol["models"]) > 1 else ""
            lines.append(f"[dim]ollama[/] {escape(m['name'])} [dim]{human_bytes(m['vram'])}{more}[/]")
        else:
            lines.append("[dim]Ollama idle · no model loaded[/]")
        return lines, v_l

    @staticmethod
    def _gpu_temp(snap) -> str:
        """' 63°C' coloured by the card's worst temperature, or '' without sensors."""
        sensors = snap.get("gpu_sensors")
        if not sensors or sensors.get("edge") is None:
            return ""
        return f" [{NUM[gpusensors.heat_level(sensors)]}]{sensors['edge']}°C[/]"

    def _card_storage(self, snap, s, w, h):
        disks = snap.get("disks") or []
        if not disks:
            return ["[dim]sampling…[/]"], OK
        lines, lvl = [], OK
        bw = max(4, w - 17)
        for d in disks[:max(1, h - 1)]:
            dl = disk_level(d)
            lvl = worst(lvl, dl)
            lines.append(f"{d['device']:<3}{bar(d['percent'] / 100, bw, dl)} "
                         f"[{NUM[dl] if dl != OK else 'default'}]{human_bytes(d['free']):>8}[/] [dim]free[/]")
        lines.append(f"[dim]read {rate(s.disk_read.now)} · write {rate(s.disk_write.now)}[/]")
        return lines, lvl

    def _card_network(self, snap, s, w, h):
        lines = [f"↓ [bold]{rate(s.net_down.now)}[/]   ↑ [bold]{rate(s.net_up.now)}[/]"]
        top = max(s.net_down.peak, s.net_up.peak, 64 * 1024)
        lines += chart(s.net_down.points, w, self._graph_rows(h, 2), top=top)
        ts = snap.get("tailscale")
        tail = [f"[dim]peak ↓ {rate(s.net_down.peak)}[/]"]
        if ts is not None:
            tail.insert(0, "Tailscale " + ("[green]connected[/]" if ts else "[yellow]off[/]"))
        lines.append(" [dim]·[/] ".join(tail))
        return lines, OK

    def _card_claude(self, snap, s, w, h):
        if not snap.get("claude_ready"):
            return ["[dim]scanning transcripts…[/]"], OK
        c = snap["claude"]
        lines = [f"[bold]${c['today_cost']:,.2f}[/] [dim]today · {c['today_calls']:,} calls[/]"]
        order = {"approval": 0, "waiting": 1, "working": 2}
        active = sorted((x for x in c["sessions"] if x["state"] in order),
                        key=lambda x: (order[x["state"]], x["age"]))
        if not active:
            lines.append("[dim]no chats active in the last 12h[/]")
        for x in active[:h - 1]:
            mark = {"approval": "[yellow]?[/]", "waiting": "[bold]◆[/]",
                    "working": "[green]●[/]"}[x["state"]]
            lines.append(f"{mark} {escape(x['title'])}")
        lvl = WARN if any(x["state"] == "approval" for x in active) else OK
        return lines, lvl

    def _card_agents(self, snap, s, w, h):
        if not snap.get("claude_ready"):
            return ["[dim]scanning transcripts…[/]"], OK
        agents = snap.get("agents") or []
        if not agents:
            return ["[dim]no agent transcripts[/]"], OK
        live = snap.get("live_agents") or []
        lines = [f"[bold]{len(live)}[/] [dim]working now · 7 to watch[/]"]
        for a in live[:max(1, (h - 1) // 2)]:           # two lines each: what it is, what it's doing
            step = a["now"]
            lines.append(f"[green]●[/] {escape(a['desc'] or a['id'])}")
            lines.append(f"  [dim]{escape(step['label'])} · {self._since(step['since'])}[/]")
        if not live:
            lines.append(f"[dim]last one finished {duration(agents[0]['age'])} ago[/]")
        return lines, OK

    def _card_processes(self, snap, s, w, h):
        procs = snap.get("procs") or []
        if not procs:
            return ["[dim]sampling…[/]"], OK
        left = snap.get("leftovers") or []
        hp = snap.get("helpers") or {}
        tail = []
        if hp.get("sessions"):
            tail.append(f"[dim]{hp['sessions']} Claude chats loaded · "
                        f"{hp['count']} helpers · {human_bytes(hp['rss'])}[/]")
        if left:
            tail.append(f"[yellow]{len(left)} leftover process{'es' if len(left) > 1 else ''}[/]")
        top = [f"{escape(p['name'][:16]):<16} [bold]{p['cpu']:4.1f}%[/] [dim]{human_bytes(p['rss'])}[/]"
               for p in procs[:max(1, min(5, h - len(tail) - 1))]]
        gap = [""] * max(0, h - len(top) - len(tail))
        return top + gap + tail, WARN if left else OK

    # ---- detail bodies

    def render_detail(self, key: str, snap: dict, width: int) -> str:
        try:
            return getattr(self, f"_detail_{key}")(snap, self.sampler, width)
        except Exception as exc:
            return f"[red]{escape(type(exc).__name__)}: {escape(str(exc))}[/]"

    def _detail_update(self, snap, s, width) -> str:
        info, at = self.update_info, self.update_checked_at
        when = f"[dim]Last checked {at:%H:%M}; checks again every {UPDATE_EVERY // 3600} hours.[/]" if at else ""
        if info is None:
            if at is None:
                return "[dim]Checking GitHub for a newer version…[/]"
            return ("Can't check for updates from here: git isn't installed, this copy wasn't made "
                    "with git clone, or GitHub couldn't be reached.\n\n" + when)
        if not info.available:
            return f"[green]✓[/] UtilityBelt is up to date.\n\n{when}"
        n = info.behind
        lines = [f"[bold yellow]⬆ {n} new change{'s' if n != 1 else ''} on GitHub[/]", "", "[bold]What's new[/]"]
        lines += [f"  • {escape(t)}" for t in info.titles]
        if n > len(info.titles):
            lines.append(f"  [dim]…and {n - len(info.titles)} more[/]")
        lines += ["", "[bold]To update[/]", "  1. Quit UtilityBelt (q).",
                  "  2. Open a terminal and run:"]
        lines += [f"       [bold]{escape(cmd)}[/]" for cmd in info.commands()]
        if info.deps_changed:
            lines.append("     [dim](the second line installs new packages this version needs)[/]")
        lines += ["  3. Start UtilityBelt again.", "",
                  "[dim]c copies the command" + ("s" if len(info.commands()) > 1 else "") + ".[/]", when]
        return "\n".join(lines)

    @staticmethod
    def _graph(points, width, lvl=OK, top=100.0, label="") -> list[str]:
        rows = chart(points, min(width, 120), 5, top=top, lvl=lvl)
        return rows + [f"[dim]{'5 minutes ago':<{min(width, 120) - 3}}now[/]"
                       + (f"  [dim]{label}[/]" if label else "")]

    def _detail_cpu(self, snap, s, width) -> str:
        lvl = level(s.cpu.recent(10), 85, 95)
        out = [f"[{NUM[lvl]}]{s.cpu.now:.1f}%[/]  [dim]{snap['cpu_physical']} cores / "
               f"{snap['cpu_logical']} threads · 5 min avg {s.cpu.mean:.1f}% · "
               f"peak {s.cpu.peak:.1f}%[/]", ""]
        out += self._graph(s.cpu.points, width, lvl)
        out += ["", "[bold]per thread[/]"]
        for i, core in enumerate(s.cpu_cores):
            cl = level(core.now, 85, 95)
            out.append(f"  {i:>2}  {bar(core.now / 100, 26, cl)} {core.now:5.1f}% "
                       f"[dim]avg {core.mean:4.1f}%[/]")
        freq, fmax = snap.get("cpu_freq") or 0, snap.get("cpu_freq_max") or 0
        info = snap.get("sysinfo") or {}
        out += ["", "[bold]clocks and interrupts[/]"]
        if info.get("cpu"):
            out.append(f"  processor        {escape(info['cpu'])}")
        out += [f"  frequency        {freq / 1000:.2f} GHz"
                + (f" [dim]of {fmax / 1000:.2f} GHz base[/]" if fmax else ""),
                f"  context switches {snap.get('ctx_switches', 0):,}/s",
                f"  interrupts       {snap.get('interrupts', 0):,}/s",
                f"  uptime           {duration(time.time() - snap.get('boot', time.time()))}"]
        procs = snap.get("procs") or []
        out += ["", "[bold]top by CPU[/]"]
        for p in procs[:12]:
            out.append(f"  {p['cpu']:5.1f}%  [dim]{p['pid']:>7}[/]  {escape(p['name'][:28]):<28}"
                       f"[dim]{human_bytes(p['rss'])}[/]")
        out += ["", "[dim]Temperature, power and voltage need LibreHardwareMonitor — "
                "coming in the sensors step.[/]"]
        return "\n".join(out)

    def _detail_memory(self, snap, s, width) -> str:
        vm, sw = snap.get("mem"), snap.get("swap")
        if not vm:
            return "[dim]sampling…[/]"
        lvl = level(s.mem.recent(30), 85, 93)
        out = [f"[{NUM[lvl]}]{vm.percent:.1f}%[/]  [dim]{human_bytes(vm.used)} of "
               f"{human_bytes(vm.total)} used · {human_bytes(vm.available)} available[/]", ""]
        out += self._graph(s.mem.points, width, lvl)
        out += ["", "[bold]breakdown[/]",
                f"  total      {human_bytes(vm.total)}",
                f"  used       {human_bytes(vm.used)}",
                f"  available  {human_bytes(vm.available)}"]
        if sw:
            out.append(f"  swap       {human_bytes(sw.used)} of {human_bytes(sw.total)} "
                       f"[dim]({sw.percent:.0f}%)[/]")
        mods = (snap.get("sysinfo") or {}).get("ram") or []
        if isinstance(mods, dict):
            mods = [mods]
        if mods:
            running, rated = ram_speed(snap)
            out += ["", "[bold]modules[/]"]
            for m in mods:
                out.append(f"  {escape(m.get('slot') or '?'):<10} {human_bytes(m.get('size') or 0):>8}  "
                           f"{escape(m.get('maker') or '')} {escape(m.get('part') or '')}  "
                           f"[dim]{m.get('configured') or m.get('speed')} MT/s[/]")
            if running and rated and running < rated - 100:
                out += ["", f"[yellow]Running at {running} MT/s but rated for {rated}.[/] "
                        "EXPO is probably off. In the BIOS: F7 for Advanced Mode, then "
                        "Ai Tweaker > Ai Overclock Tuner > EXPO I, then F10 to save.",
                        "[dim]The first boot after that trains the memory and can sit on a "
                        "black screen for a few minutes. Let it finish.[/]"]
            elif rated:
                out.append(f"  [green]running at its rated {rated} MT/s[/]")
        out += ["", "[bold]top by memory[/]"]
        for p in sorted(snap.get("procs") or [], key=lambda r: -r["rss"])[:15]:
            frac = p["rss"] / vm.total if vm.total else 0
            out.append(f"  {bar(frac, 12)} {human_bytes(p['rss']):>9}  "
                       f"[dim]{p['pid']:>7}[/]  {escape(p['name'][:30])}")
        return "\n".join(out)

    def _detail_gpu(self, snap, s, width) -> str:
        used = snap.get("vram_used") or 0
        total = snap.get("vram_total") or 0
        vpct = used / total * 100 if total else 0
        v_l = level(vpct, 90, 97)
        out = [f"[bold]{s.gpu.now:.0f}%[/] load   VRAM [{NUM[v_l]}]{vpct:.0f}%[/] "
               f"[dim]{human_bytes(used)} of {human_bytes(total)} · 5 min avg load "
               f"{s.gpu.mean:.0f}%[/]", ""]
        out += self._graph(s.gpu.points, width, label="load")
        out += ["", "[bold]video memory[/]"] + self._graph(s.vram.points, width, v_l, label="VRAM")

        ol = snap.get("ollama")
        out += ["", "[bold]Ollama (local models)[/]"]
        if ol is None:
            out.append("  [dim]checking…[/]")
        elif not ol["up"]:
            out.append("  [dim]not running[/]")
        elif not ol["models"]:
            out.append("  [dim]running, no model loaded — nothing held in video memory[/]")
        else:
            for m in ol["models"]:
                until = (f"unloads at {datetime.fromtimestamp(m['until']):%H:%M}"
                         if m["until"] else "stays loaded")
                share = f" · {m['vram'] / total * 100:.0f}% of VRAM" if total else ""
                out.append(f"  [bold]{escape(m['name'])}[/]  {human_bytes(m['vram'])}{share}  "
                           f"[dim]{m['params']} · {m['context']:,} context · {until}[/]")
            out.append("  [dim]Ollama frees the memory itself when the model unloads. "
                       "`ollama stop <model>` frees it now.[/]")

        types = snap.get("gpu_types") or {}
        if types:
            out += ["", "[bold]by engine[/]  [dim]3D is rendering and most compute; copy "
                    "moves data to and from the card; video is the media blocks[/]"]
            for name, value in sorted(types.items(), key=lambda kv: -float(kv[1] or 0)):
                v = min(100.0, float(value or 0))
                out.append(f"  {escape(name[:20]):<20} {bar(v / 100, 22)} {v:5.1f}%")
        gpus = (snap.get("sysinfo") or {}).get("gpus") or []
        if isinstance(gpus, dict):
            gpus = [gpus]
        for g in gpus:
            out += ["", f"[dim]{escape(g.get('name') or '')} · driver {g.get('driver')} "
                    f"({g.get('date')})[/]"]
        sensors = snap.get("gpu_sensors")
        if sensors:
            lvl = gpusensors.heat_level(sensors)

            def val(key, unit, fmt="{}"):
                v = sensors.get(key)
                return "—" if v is None else fmt.format(v) + unit

            out += ["", f"[bold]sensors[/]  [dim]from AMD's driver (ADL), every 2 s[/]",
                    f"  temperature  [{NUM[lvl]}]{val('edge', ' °C')}[/]  "
                    f"[dim]hotspot {val('hotspot', ' °C')} · memory {val('memory', ' °C')} · "
                    f"slows itself down around 110 °C hotspot[/]",
                    f"  fan          {val('fan_pct', '%')}  [dim]{val('fan_rpm', ' RPM')}[/]",
                    f"  power        {val('power', ' W')}",
                    f"  clocks       {val('core_clock', ' MHz')} core  [dim]· {val('mem_clock', ' MHz')} memory · "
                    f"{val('voltage', ' V', '{:.3f}')}[/]",
                    "", "[bold]temperature[/]"]
            out += self._graph(s.gpu_temp.points, width, top=max(100.0, s.gpu_temp.peak), label="°C, 0-100")
            out += ["", "[bold]power[/]"]
            out += self._graph(s.gpu_power.points, width, top=max(50.0, s.gpu_power.peak),
                               label=f"W, peak {s.gpu_power.peak:.0f}")
        else:
            out += ["[dim]No temperature or fan readings: these come from AMD's driver, and this "
                    "card isn't an AMD one (see NVIDIA-SETUP.md for NVIDIA cards).[/]"]
        return "\n".join(out)

    def _detail_storage(self, snap, s, width) -> str:
        out = [f"read [bold]{rate(s.disk_read.now)}[/]   write [bold]{rate(s.disk_write.now)}[/]"
               f"  [dim]peaks {rate(s.disk_read.peak)} / {rate(s.disk_write.peak)}[/]", ""]
        top = max(s.disk_read.peak + s.disk_write.peak, 1024 ** 2)
        out += self._graph([r + w for r, w in zip(s.disk_read.points, s.disk_write.points)],
                           width, top=top, label="read + write")
        out.append("")
        for d in snap.get("disks") or []:
            dl = disk_level(d)
            out += [f"[bold]{d['device']}[/] [dim]{d['fstype']}[/]",
                    f"  {bar(d['percent'] / 100, 30, dl)} [{NUM[dl]}]{human_bytes(d['free'])} free[/]"
                    f"  [dim]{human_bytes(d['used'])} used of {human_bytes(d['total'])}[/]", ""]
        per = snap.get("per_disk") or {}
        if per:
            out.append("[bold]lifetime per device[/]")
            for name, v in sorted(per.items(), key=lambda kv: -(kv[1]["read"] + kv[1]["write"]))[:8]:
                out.append(f"  {escape(name[:22]):<22} read {human_bytes(v['read']):>9}  "
                           f"write {human_bytes(v['write']):>9}  "
                           f"[dim]{v['rcount'] + v['wcount']:,} ops[/]")
        out += ["", "[dim]disk-sentinel.py answers the other storage question — "
                "what is actually eating the space.[/]"]
        return "\n".join(out)

    def _detail_network(self, snap, s, width) -> str:
        top = max(s.net_down.peak, s.net_up.peak, 64 * 1024)
        out = [f"↓ [bold]{rate(s.net_down.now)}[/]   ↑ [bold]{rate(s.net_up.now)}[/]  "
               f"[dim]peaks ↓ {rate(s.net_down.peak)} ↑ {rate(s.net_up.peak)}[/]", ""]
        out += self._graph(s.net_down.points, width, top=top, label="download")
        out += [""] + self._graph(s.net_up.points, width, top=top, label="upload")
        ts = snap.get("tailscale")
        if ts is not None:
            out += ["", "Tailscale " + ("[green]connected[/]" if ts else "[yellow]not connected[/]")]
        out += ["", "[bold]interfaces[/]"]
        for n in snap.get("nets") or []:
            speed = f"{n['speed']} Mb/s link" if n["speed"] else "speed unknown"
            out += [f"  [bold]{escape(n['name'][:34])}[/] [dim]{speed}[/]",
                    f"    received {human_bytes(n['recv']):>10}  "
                    f"[dim]{n['pkt_recv']:,} packets[/]",
                    f"    sent     {human_bytes(n['sent']):>10}  "
                    f"[dim]{n['pkt_sent']:,} packets[/]"]
            if n["errin"] or n["errout"] or n["dropin"] or n["dropout"]:
                out.append(f"    [yellow]errors in {n['errin']} out {n['errout']} · "
                           f"dropped in {n['dropin']} out {n['dropout']}[/]")
        try:
            conns = psutil.net_connections(kind="inet")
            established = sum(1 for c in conns if c.status == "ESTABLISHED")
            listening = sum(1 for c in conns if c.status == "LISTEN")
            out += ["", f"[bold]sockets[/]  {established} established · "
                    f"{listening} listening · {len(conns)} total"]
        except (psutil.AccessDenied, PermissionError):
            out += ["", "[dim]socket counts need elevation[/]"]
        return "\n".join(out)

    def _detail_claude(self, snap, s, width) -> str:
        c = snap["claude"]
        out = [f"[bold]${c['today_cost']:,.2f}[/] today  [dim]{c['today_calls']:,} calls · "
               f"{len(c['sessions'])} chats · ${sum(x['cost'] for x in c['sessions']):,.2f} "
               f"all time[/]", ""]
        order = {"approval": 0, "waiting": 1, "working": 2, "idle": 3}
        active = sorted((x for x in c["sessions"] if x["state"] in order),
                        key=lambda x: (order[x["state"]], x["age"]))
        out.append("[bold]chats in the last 12 hours[/]")
        if not active:
            out.append("  [dim]none[/]")
        for x in active:
            colour = {"approval": "yellow", "waiting": "bold", "working": "green", "idle": "dim"}[x["state"]]
            out.append(f"  [{colour}]{STATE_LABEL[x['state']]:<18}[/] {escape(x['title'][:50]):<50} "
                       f"[dim]{duration(x['age'])} ago[/]")
        out += ["[dim]  'may need approval' means the chat stopped on a tool call and has been "
                "quiet since — usually a permission prompt, sometimes a long command.[/]", ""]
        models = c.get("models") or {}
        if models:
            out.append("[bold]by model[/]  [dim]input · cache write · cache read · output[/]")
            for name, v in sorted(models.items(), key=lambda kv: -cost_of(kv[0], kv[1])):
                out.append(f"  {escape(name[:26]):<26} {v[0] / 1e6:7.1f}M {v[1] / 1e6:7.1f}M "
                           f"{v[2] / 1e6:9.1f}M {v[3] / 1e6:7.1f}M   "
                           f"[bold]${cost_of(name, v):,.2f}[/]")
            cr = sum(v[2] for v in models.values())
            out_tok = sum(v[3] for v in models.values())
            if cr + out_tok:
                out += ["", f"[dim]{cr / (cr + out_tok) * 100:.1f}% of all tokens are "
                        f"cache reads — the conversation being re-read each turn, "
                        f"not new work.[/]"]
        out += ["", "[bold]most expensive chats[/]"]
        for sess in c["sessions"][:12]:
            out.append(f"  [bold]${sess['cost']:>8,.2f}[/]  {sess['calls']:>5} calls  "
                       f"{escape(sess['title'][:50])}")
        out += ["", "[dim]Costs are API list rates applied to the transcript logs. "
                "Your plan is billed differently — /usage is authoritative.[/]"]
        return "\n".join(out)

    def _detail_processes(self, snap, s, width) -> str:
        procs = snap.get("procs") or []
        vm = snap.get("mem")
        out = [f"[bold]{len(procs)}[/] processes running", ""]
        hp = snap.get("helpers") or {}
        left = snap.get("leftovers") or []
        out.append("[bold]Claude Code helpers[/]")
        if hp.get("sessions"):
            out.append(f"  {hp['sessions']} chats are loaded in the Claude app. Each runs its "
                       f"own MCP servers: {hp['count']} processes using "
                       f"{human_bytes(hp['rss'])} in total.")
            out.append("  [dim]Expected, not a leak. Archiving chats you no longer need "
                       "closes theirs.[/]")
        else:
            out.append("  [dim]no Claude Code chats loaded[/]")
        servers = snap.get("servers") or []
        if servers:
            out += ["", "[bold]background servers[/]  [dim]started by a launcher that has since "
                    "exited — on purpose, e.g. from the Startup folder[/]"]
            for p in servers:
                port = ", ".join(f":{n}" for n in p["ports"]) or "windowless"
                out.append(f"  {p['pid']:>7}  {escape(p['name']):<16} {human_bytes(p['rss']):>9}  "
                           f"[dim]{duration(p['age'])} old[/]  {escape(port):<10} {escape(p['what'])}")
        out += ["", "[bold]leftover processes[/]  [dim]script processes whose parent has closed[/]"]
        if not left:
            out.append("  [green]none[/]")
        for p in left:
            out.append(f"  [yellow]{p['pid']:>7}[/]  {escape(p['name']):<16} "
                       f"{human_bytes(p['rss']):>9}  [dim]{duration(p['age'])} old[/]  "
                       f"{escape(p['what'])}")
        if left:
            out.append("  [dim]End them in Task Manager (Details tab, by PID) if you do not "
                       "recognise them. UtilityBelt never kills anything itself.[/]")
        groups: dict = defaultdict(lambda: [0, 0.0, 0])
        for p in procs:
            g = groups[p["name"]]
            g[0] += 1
            g[1] += p["cpu"]
            g[2] += p["rss"]
        out += ["", "[bold]by program[/]  [dim]instances · cpu · memory[/]"]
        for name, (count, cpu, rss) in sorted(groups.items(), key=lambda kv: -kv[1][2])[:14]:
            out.append(f"  {escape(name[:26]):<26} [dim]{count:>3}×[/]  {cpu:5.1f}%  "
                       f"{human_bytes(rss):>9}")
        out += ["", "[bold]top single processes by CPU[/]"]
        for p in procs[:14]:
            out.append(f"  {p['cpu']:5.1f}%  [dim]{p['pid']:>7}[/]  {escape(p['name'][:30]):<30}"
                       f"{human_bytes(p['rss']):>9}  [dim]{p['threads']} threads[/]")
        if vm:
            out += ["", "[bold]top single processes by memory[/]"]
            for p in sorted(procs, key=lambda r: -r["rss"])[:14]:
                out.append(f"  {human_bytes(p['rss']):>9}  [dim]{p['pid']:>7}[/]  "
                           f"{escape(p['name'][:30]):<30}{p['cpu']:5.1f}%")
        return "\n".join(out)

    def _detail_health(self, snap, s, width) -> str:
        alerts = compute_alerts(snap, s)
        out = ["[bold]needs attention[/]"]
        if not alerts:
            out.append("  [green]✓ nothing — all readings are normal[/]")
        for a in alerts:
            mark = "[red]●[/]" if a["lvl"] == CRIT else "[yellow]⚠[/]"
            out.append(f"  {mark} {escape(a['long'])}")

        evs = snap.get("events")
        out += ["", f"[bold]event log, last {EVENT_DAYS} days[/]  [dim]hardware errors, "
                "blue screens, driver resets, unexpected restarts and app crashes[/]"]
        if evs is None:
            out.append("  [dim]reading…[/]")
        else:
            counts = Counter(e["kind"] for e in evs)
            for kind in ("hardware error", "blue screen", "GPU driver crashed and recovered",
                         "unexpected shutdown or restart", "app crash"):
                n = counts.get(kind, 0)
                sev = next((e["sev"] for e in evs if e["kind"] == kind), OK)
                colour = "green" if not n else FILL[sev] if sev != OK else "default"
                out.append(f"  [{colour}]{n:>3}[/]  {kind}")
            serious = [e for e in evs if e["kind"] != "app crash"]
            if serious:
                out += ["", "  [bold]most recent[/]"]
                for e in serious[:8]:
                    out.append(f"  [dim]{datetime.fromtimestamp(e['when']):%a %d %b %H:%M}[/]  "
                               f"{e['kind']}")
            crashes = Counter(e["detail"] for e in evs if e["kind"] == "app crash")
            if crashes:
                out += ["", "  [bold]apps that crashed[/]"]
                for app, n in crashes.most_common(6):
                    out.append(f"  {n:>3}×  {escape(app)}")
            if not counts.get("hardware error"):
                out += ["", "  [dim]No hardware (WHEA) errors. After changing RAM or BIOS "
                        "settings this is the first place instability shows up.[/]"]

        info = snap.get("sysinfo") or {}
        out += ["", "[bold]this machine[/]"]
        if info:
            bios = info.get("bios") or {}
            running, rated = ram_speed(snap)
            gpus = info.get("gpus") or []
            if isinstance(gpus, dict):
                gpus = [gpus]
            rows = [("board", info.get("board")),
                    ("BIOS", f"{bios.get('version', '?')} ({bios.get('date', '?')})"),
                    ("processor", info.get("cpu")),
                    ("RAM", f"{running} MT/s" + (f" of {rated} rated" if rated else ""))]
            rows += [("graphics", f"{g.get('name')} · driver {g.get('driver')} ({g.get('date')})")
                     for g in gpus]
            for label, value in rows:
                out.append(f"  {label:<10} {escape(str(value or '?'))}")
        else:
            out.append("  [dim]reading…[/]")
        return "\n".join(out)


# --------------------------------------------------------------------- main

def probe() -> int:
    """One sample of everything, as plain text. Used to check the plumbing."""
    sampler = Sampler()
    # The slow loop walks every process and every transcript; on a cold start
    # that takes a good few seconds. Wait for it rather than reporting zeros.
    deadline = time.time() + 60
    while time.time() < deadline:
        time.sleep(1.0)
        snap = sampler.read()
        if (snap.get("procs") and snap.get("disks") and snap["claude"]["scanned"]
                and snap.get("sysinfo") is not None and snap.get("events") is not None):
            break
        print(f"\r[waiting] procs={len(snap.get('procs') or [])} "
              f"disks={len(snap.get('disks') or [])} "
              f"transcripts={snap['claude']['scanned']}", end="", flush=True)
    print("\r" + " " * 70 + "\r", end="")
    snap = sampler.read()
    print(f"cpu          {sampler.cpu.now:.1f}%  ({len(sampler.cpu_cores)} threads)")
    vm = snap.get("mem")
    if vm:
        print(f"memory       {vm.percent:.1f}%  {human_bytes(vm.used)} / {human_bytes(vm.total)}")
    running, rated = ram_speed(snap)
    print(f"ram speed    {running} MT/s, rated {rated or '?'}")
    print(f"gpu          {sampler.gpu.now:.1f}%  engines={list((snap.get('gpu_types') or {}).keys())}")
    print(f"gpu sensors  {snap.get('gpu_sensors')}")
    print(f"vram         {human_bytes(snap.get('vram_used') or 0)} / "
          f"{human_bytes(snap.get('vram_total') or 0)}")
    ol = snap.get("ollama") or {}
    print(f"ollama       up={ol.get('up')}  "
          f"{[(m['name'], human_bytes(m['vram'])) for m in ol.get('models') or []]}")
    print(f"disks        {[(d['device'], human_bytes(d['free'])) for d in snap.get('disks') or []]}")
    print(f"disk io      r {rate(sampler.disk_read.now)}  w {rate(sampler.disk_write.now)}")
    print(f"net          down {rate(sampler.net_down.now)}  up {rate(sampler.net_up.now)}  "
          f"tailscale={snap.get('tailscale')}")
    print(f"processes    {len(snap.get('procs') or [])}")
    hp = snap.get("helpers") or {}
    print(f"helpers      {hp.get('sessions')} chats, {hp.get('count')} procs, "
          f"{human_bytes(hp.get('rss') or 0)}")
    print(f"leftovers    {[(p['pid'], p['what']) for p in snap.get('leftovers') or []]}")
    evs = snap.get("events") or []
    print(f"events       {dict(Counter(e['kind'] for e in evs))}")
    c = snap["claude"]
    print(f"claude       {c['today_calls']} calls today, ${c['today_cost']:.2f}, "
          f"{len(c['sessions'])} sessions, "
          f"states={dict(Counter(x['state'] for x in c['sessions']))}")
    print(f"agents       {len(snap.get('agents') or [])} "
          f"({sum(1 for a in snap.get('agents') or [] if a['live'])} live)")
    print("alerts       " + " | ".join(a["short"] for a in compute_alerts(snap, sampler)))
    if snap.get("errors"):
        print("errors      ", snap["errors"])
    sampler.stop.set()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--probe", action="store_true",
                    help="print one sample of every metric and exit")
    ap.add_argument("--mini", action="store_true",
                    help="start as the small strip (m switches back)")
    args = ap.parse_args()
    if args.probe:
        return probe()
    sampler = Sampler()
    app = Belt(sampler, mini=args.mini)
    try:
        app.run()
    finally:
        sampler.stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
