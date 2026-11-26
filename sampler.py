"""UtilityBelt's sampler: every reading the dashboard shows, gathered on background threads.

Moved out of belt.py (2026-11-25) so other programs on this PC can use the same readings
without starting the Textual app: Home Base (the home-base project) imports this module.
belt.py imports everything it needs from here, so UtilityBelt itself behaves as before.

    from sampler import Sampler
    s = Sampler()            # starts its threads
    snap = s.read()          # a copy of the latest readings
    s.stop.set()             # ends the loops

Why the sampling is threaded, and what each loop reads, is described at the top of belt.py.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

import psutil

import agentlog
import beltconfig
import signals
import winprocs
import cpusensors
import gpusensors



_JOB = None                    # Windows job that owns our child processes


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
HIDDEN_FAST_EVERY = 5.0         # seconds between cheap samples while the strip is
                                # slid out of sight: coarse history, not a gap
HIDDEN_SLOW_EVERY = 4           # ...and only every 4th process/disk walk (~12 s)
STRANDED_EVERY = 6 * 3600       # how often to look for caches left behind on C:
AGENT_RECENT = 8                # steps kept per working agent (the findings rules read these)
AGENT_RECENT_DETAILED = 14      # ...while the Agents screen is open
CHAT_TAIL_BYTES = 1024 * 1024   # how much of a working chat's transcript the feed reads
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
LAB_FRESH = 15 * 60       # s: a lab status.json older than this = the service is gone

# Script interpreters that are only ever meant to run under a parent. One of
# these whose parent has gone is almost always something left behind.
LEFTOVER_NAMES = {"powershell.exe", "pwsh.exe", "python.exe", "pythonw.exe", "node.exe"}

# DDR5 speed grades, for reading a kit's rated speed out of its part number
# (CMK32GX5M2B6400Z36 -> 6400). Windows only reports the JEDEC speed.
DDR_GRADES = ("4800", "5200", "5600", "6000", "6200", "6400", "6600", "6800",
              "7000", "7200", "7600", "8000", "8200", "8400")


# --------------------------------------------------------------- formatting

def tokens(n: float) -> str:
    """Token counts the short way: 950, 12.3k, 1.2M."""
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.1f}k" if n >= 1e3 else f"{n:.0f}"


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


# Severity levels. The colours that go with them are belt.py's business.
OK, WARN, CRIT = "ok", "warn", "crit"


def level(value: float, warn: float, crit: float) -> str:
    if value >= crit:
        return CRIT
    if value >= warn:
        return WARN
    return OK


def worst(*levels: str) -> str:
    return CRIT if CRIT in levels else WARN if WARN in levels else OK


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


class TranscriptTally:
    """A transcript's running totals, read incrementally: each feed() reads only the lines added
    since the last one. A busy chat's transcript can be 150 MB, and re-reading it whole every scan
    froze the window for 0.5-2 s each time (2026-11-12)."""

    RECENT_IDS = 4000            # a reply's id repeats on consecutive lines; older ones never come back

    def __init__(self, today: str) -> None:
        self.today = today
        self.offset = 0
        self.partial = b""
        self.seen: set = set()
        self.seen_order: deque = deque()
        self.models: dict = defaultdict(lambda: [0, 0, 0, 0])
        self.today_models: dict = defaultdict(lambda: [0, 0, 0, 0])
        self.calls = self.today_calls = 0
        self.title = self.custom_title = ""
        self.last_tool = self.last_text = self.last_kind = ""

    def feed(self, path: Path) -> bool:
        try:
            with path.open("rb") as fh:
                fh.seek(self.offset)
                chunk = fh.read()
        except OSError:
            return False
        self.offset += len(chunk)
        lines = (self.partial + chunk).split(b"\n")
        self.partial = lines.pop()                       # an unfinished last line waits for the rest
        for raw in lines:
            raw = raw.strip()
            if raw:
                try:
                    self._entry(json.loads(raw.decode("utf-8", errors="replace")))
                except (ValueError, AttributeError):
                    continue
        return True

    def _entry(self, entry: dict) -> None:
        if entry.get("type") == "custom-title" and entry.get("customTitle"):
            self.custom_title = str(entry["customTitle"])
        message = entry.get("message") or {}
        if entry.get("type") == "user" and not entry.get("isSidechain"):
            self.last_kind = "user"
            if not self.title:
                content = message.get("content")
                if isinstance(content, str):
                    self.title = content.strip().replace("\n", " ")[:60]
        if entry.get("type") != "assistant":
            return
        has_tool = False
        for block in (message.get("content") or []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                self.last_tool = block.get("name") or ""
                has_tool = True
            elif block.get("type") == "text" and block.get("text"):
                self.last_text = str(block["text"]).strip().replace("\n", " ")[:70]
        if not entry.get("isSidechain"):
            self.last_kind = ("end_turn" if message.get("stop_reason") == "end_turn"
                              else "tool_use" if has_tool else "assistant")
        usage = message.get("usage")
        if not usage:
            return
        mid = message.get("id")
        if mid:
            if mid in self.seen:
                return
            self.seen.add(mid)
            self.seen_order.append(mid)
            if len(self.seen_order) > self.RECENT_IDS:
                self.seen.discard(self.seen_order.popleft())
        self.calls += 1
        model = message.get("model") or "?"
        vals = (usage.get("input_tokens", 0), usage.get("cache_creation_input_tokens", 0),
                usage.get("cache_read_input_tokens", 0), usage.get("output_tokens", 0))
        for i in range(4):
            self.models[model][i] += vals[i]
        if (entry.get("timestamp") or "")[:10] == self.today:
            self.today_calls += 1
            for i in range(4):
                self.today_models[model][i] += vals[i]

    def result(self, path: Path) -> dict | None:
        if not self.calls:
            return None
        models = self.models
        return {
            "id": path.stem.replace("agent-", "")[:10],
            "title": self.custom_title or self.title or self.last_text or "(no prompt text)",
            "calls": self.calls,
            "today_calls": self.today_calls,
            "models": dict(models),
            "today_models": dict(self.today_models),
            "cache_read": sum(v[2] for v in models.values()),
            "output": sum(v[3] for v in models.values()),
            "cost": sum(cost_of(m, v) for m, v in models.items()),
            "last_tool": self.last_tool,
            "last_kind": self.last_kind,
            "workflow": path.parent.name if path.parent.name.startswith("wf_") else "",
        }


class Sampler:
    """Owns every reading. The UI never calls psutil or PowerShell directly."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.stop = threading.Event()
        # Set while the strip is docked and slid out of sight. Nothing on screen can
        # be read then, so the loops below back off: the expensive walks are skipped
        # entirely and the cheap counters drop to a coarse interval, so the graphs
        # still have (low-resolution) history when the strip slides back down.
        self.hidden = threading.Event()

        self.cpu = Series()
        self.cpu_cores: list[Series] = []
        self.mem = Series()
        self.gpu = Series()
        self.vram = Series()
        self.gpu_temp = Series()                  # edge temperature, C (AMD cards via ADL)
        self.gpu_power = Series()                 # W
        self.cpu_temp = Series()                  # C, from LibreHardwareMonitor when it runs
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
            # For findings (findings.py, rules/): what is wrong and what to do.
            "findings": [],             # the current set, worked out every slow pass
            "disk_history": {},         # {device: ((t, free), ...)} for disk trends
            "stranded": [],             # caches left on C: after their env var moved them
            "ollama_loads": (),         # ((t, model), ...) each time a model was loaded
            "sunshine": None,           # remote rules only: stream state from Sunshine's log
            "lab": None,                # a Strategy Lab search running: its service's status.json
        }
        self.engine = None              # a findings.Engine, set by main(); None = no findings
        self._disk_hist: dict = defaultdict(lambda: deque(maxlen=400))
        self._ollama_names: set = set()
        self._ollama_loads: deque = deque(maxlen=50)
        self._sun = {"pos": None, "live": False, "since": 0.0, "screen": "", "screen_before": ""}

        self._proc_cache: dict[int, psutil.Process] = {}
        self._cmd_cache: dict[tuple, str] = {}
        self._last_net = psutil.net_io_counters()
        self._last_disk = psutil.disk_io_counters()
        self._last_cpu_times = psutil.cpu_stats()
        self._seen_files: dict = {}
        self._tallies: dict = {}                 # path -> TranscriptTally, for live transcripts
        self._own = {os.getpid()} | {p.pid for p in psutil.Process().parents()}

        threading.Thread(target=self._fast_loop, daemon=True).start()
        threading.Thread(target=self._slow_loop, daemon=True).start()
        threading.Thread(target=self._gpu_loop, daemon=True).start()
        threading.Thread(target=self._info_loop, daemon=True).start()
        threading.Thread(target=self._stranded_loop, daemon=True).start()

        # Live agent feed: an AgentLog per followed transcript, read incrementally.
        self._agent_logs: dict[Path, agentlog.AgentLog] = {}
        self._watched: set[Path] = set()          # open in the agent view: follow even when idle
        self._titles: dict[str, str] = {}         # session id -> chat title, from _sample_claude
        # How many Agents screens / agent views are open. Only then are working
        # chats followed too, with longer step lists: the detailed feed costs
        # nothing while nobody is looking at it.
        self.agents_open = 0
        threading.Thread(target=self._agent_loop, daemon=True).start()

    # -- fast: every second, cheap counters only
    def _fast_loop(self) -> None:
        psutil.cpu_percent(percpu=True)          # prime; first call is garbage
        adl = gpusensors.AdlReader()             # AMD temperature/fan/power; .ok False elsewhere
        lhm = cpusensors.LhmReader()             # CPU temperature; None unless LibreHardwareMonitor runs
        tick = 0
        while not self.stop.wait(HIDDEN_FAST_EVERY if self.hidden.is_set() else 1.0):
            tick += 1
            if tick % 2 == 1:                    # every 2 s; a local HTTP read, retried every 30 s when absent
                cpu_reading = lhm.read()
                with self.lock:
                    self.snap["cpu_sensors"] = cpu_reading
                    if cpu_reading:
                        self.cpu_temp.push(cpu_reading["temp"])
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
        tick, last_claude = 0, 0.0
        while not self.stop.wait(3.0):
            tick += 1
            # Out of sight: walk every 4th tick (~12 s) instead of every one. Not
            # never: the findings are worked out here, and the hidden strip's sliver
            # still has to raise them.
            if self.hidden.is_set() and tick % HIDDEN_SLOW_EVERY:
                continue
            try:
                self._sample_processes()
                self._sample_disks()
                self._sample_nets()
            except Exception as exc:
                self._note(f"slow: {exc}")
            if time.time() - last_claude >= 21:            # ~every 21 s
                last_claude = time.time()
                try:
                    self._sample_claude()
                except Exception as exc:
                    self._note(f"claude: {exc}")
            self._evaluate()

    def _evaluate(self) -> None:
        """Work out the findings (findings.py) from this pass's readings."""
        if self.engine is None:
            return
        snap = self.read()
        snap["now"] = time.time()
        try:
            found = self.engine.evaluate(snap)
        except Exception as exc:                           # the engine itself: never fatal
            self._note(f"findings: {type(exc).__name__}: {exc}")
            return
        with self.lock:
            self.snap["findings"] = found

    def _cmdline(self, pid: int, created: float) -> str:
        key = (pid, created)                               # pid alone gets reused
        if key not in self._cmd_cache:
            try:
                self._cmd_cache[key] = " ".join(psutil.Process(pid).cmdline())
            except (psutil.Error, OSError):
                self._cmd_cache[key] = ""
        return self._cmd_cache[key]

    def _process_rows_fast(self) -> tuple[list, set] | None:
        """All processes from one Windows call (winprocs.py): a few ms instead of 1.5-5 s with
        psutil's one-process-at-a-time reads. CPU % comes from each process's CPU time since the
        last scan. None off Windows (then psutil is used)."""
        procs = winprocs.snapshot()
        if procs is None:
            return None
        now = time.time()
        prev, then = getattr(self, "_fast_prev", ({}, now))
        wall = max(1e-3, now - then) * max(1, psutil.cpu_count(logical=True))
        seen, rows, alive = {}, [], set()
        for p in procs:
            pid = p["pid"]
            if pid == 0:                                   # System Idle Process: idle time, not a program
                continue
            alive.add(pid)
            key = (pid, round(p["created"]))
            seen[key] = p["cpu_time"]
            before = prev.get(key)
            row = dict(p, cpu=max(0.0, (p["cpu_time"] - before) / wall * 100) if before is not None else 0.0)
            if row["name"].lower() in LEFTOVER_NAMES:     # which script, not just "python.exe"
                row["what"] = describe_cmd(self._cmdline(pid, row["created"])) or row["name"]
            rows.append(row)
        self._fast_prev = (seen, now)
        return rows, alive

    def _sample_processes(self) -> None:
        fast = self._process_rows_fast()
        if fast is not None:
            rows, alive = fast
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
            return
        rows = []
        alive = set()
        for proc in psutil.process_iter(["pid", "name", "memory_info", "num_threads",
                                         "ppid", "create_time", "cpu_times"]):
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
                times = proc.info.get("cpu_times")
                row = {
                    "pid": pid,
                    "name": proc.info["name"] or "?",
                    "cpu": cpu / max(1, psutil.cpu_count(logical=True)),
                    # seconds of CPU since it started: an instantaneous % of the
                    # whole CPU hides a process that has pinned one core for a day
                    "cpu_time": (times.user + times.system) if times else 0.0,
                    "rss": getattr(mem, "rss", 0) or 0,
                    "threads": proc.info.get("num_threads") or 0,
                    "ppid": proc.info.get("ppid") or 0,
                    "created": proc.info.get("create_time") or 0,
                }
                if row["name"].lower() in LEFTOVER_NAMES:     # which script, not just "python.exe"
                    row["what"] = describe_cmd(self._cmdline(pid, row["created"])) or row["name"]
                rows.append(row)
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
        now = time.time()
        for d in out:
            self._disk_hist[d["device"]].append((now, d["free"]))
        with self.lock:
            self.snap["disks"] = out
            self.snap["disk_history"] = {k: tuple(v) for k, v in self._disk_hist.items()}
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
            engine = self.engine
            if engine is not None and engine.cfg.rules.get("remote"):
                try:
                    self._sample_sunshine(engine.cfg.remote.get("sunshine_log") or "")
                except Exception as exc:
                    self._note(f"sunshine: {exc}")
            try:
                self._sample_lab()
            except Exception as exc:
                self._note(f"lab: {exc}")
            tick += 1
            if self.stop.wait(5.0):
                break

    # -- caches left behind on C: when an env var moved them (once, then every 6 h)
    def _stranded_loop(self) -> None:
        if sys.platform != "win32":
            return
        wait = 20.0                                         # let the first passes settle first
        while not self.stop.wait(wait):
            wait = STRANDED_EVERY
            try:
                found = signals.stranded_caches()
            except Exception as exc:
                self._note(f"stranded: {exc}")
                continue
            with self.lock:
                self.snap["stranded"] = found

    # -- Sunshine: is anyone streaming this PC? Only its log knows (the stream is
    # UDP, so netstat shows nothing but the listener).
    def _sample_sunshine(self, log: str) -> None:
        sun = self._sun
        screen = signals.screen_size()
        path = Path(log)
        if not log or not path.is_file():
            with self.lock:
                self.snap["sunshine"] = None
            return
        size = path.stat().st_size
        if sun["pos"] is None or size < sun["pos"]:          # first look, or the log was rotated
            sun["pos"] = max(0, size - 256 * 1024)
        if size > sun["pos"]:
            with path.open("rb") as fh:
                fh.seek(sun["pos"])
                chunk = fh.read(size - sun["pos"]).decode("utf-8", "replace")
            sun["pos"] = size
            for line in chunk.splitlines():
                if "CLIENT CONNECTED" in line:
                    sun.update(live=True, since=signals.log_time(line) or time.time(),
                               screen_before=sun["screen"] or screen)
                elif "CLIENT DISCONNECTED" in line:
                    sun["live"] = False
        if not sun["live"]:
            sun["screen"] = screen                          # what it is outside a stream
            if screen == sun["screen_before"]:
                sun["screen_before"] = ""                   # put back: nothing to report
        with self.lock:
            self.snap["sunshine"] = {"live": sun["live"], "since": sun["since"],
                                     "screen": screen, "screen_before": sun["screen_before"]}

    # -- Strategy Lab: a search is running while its service's status.json says so
    # and was written lately (the service rewrites it every generation)
    def _sample_lab(self) -> None:
        cfg = self.engine.cfg.lab if self.engine is not None else beltconfig.DEFAULTS["lab"]
        path = Path(cfg.get("status") or "")
        lab = None
        if cfg.get("status") and path.is_file() and time.time() - path.stat().st_mtime < LAB_FRESH:
            st = json.loads(path.read_text(encoding="utf-8"))
            if st.get("state") in ("running", "paused"):
                lab = {"state": st["state"], "generation": st.get("generation"), "run_id": st.get("run_id"),
                       "replicate": st.get("replicate"), "replicates": st.get("replicates")}
        with self.lock:
            self.snap["lab"] = lab

    def _sample_ollama(self) -> None:
        try:
            with urllib.request.urlopen(OLLAMA_PS, timeout=1.0) as resp:
                data = json.load(resp)
        except Exception:
            self._ollama_names = set()
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
        names = {m["name"] for m in models}
        for name in names - self._ollama_names:            # newly loaded since the last poll
            self._ollama_loads.append((time.time(), name))
        self._ollama_names = names
        with self.lock:
            self.snap["ollama"] = {"up": True, "models": models}
            self.snap["ollama_loads"] = tuple(self._ollama_loads)

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
                # Read only what was added, for transcripts that keep changing (live chats).
                tally = self._tallies.get(path)
                if tally is None or tally.today != today or st.st_size < tally.offset:
                    tally = TranscriptTally(today)
                tally.feed(path)
                info = tally.result(path)
                self._seen_files[path] = (st.st_mtime, st.st_size, info)
                if now - st.st_mtime < 3600:                 # live: keep its place for next time
                    self._tallies[path] = tally
                else:
                    self._tallies.pop(path, None)
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
        chats: list[Path] = []
        while not self.stop.wait(1.0):
            try:
                now = time.time()
                detailed = self.agents_open > 0
                if tick % 3 == 0 and PROJECTS.exists():
                    candidates = []
                    for path in PROJECTS.glob("*/*/subagents/**/agent-*.jsonl"):
                        try:
                            if now - path.stat().st_mtime < AGENT_LIVE_SECONDS:
                                candidates.append(path)
                        except OSError:
                            continue
                    chats = []
                    if detailed:                            # working chats: only while someone looks
                        for path in PROJECTS.glob("*/*.jsonl"):
                            try:
                                if now - path.stat().st_mtime < AGENT_LIVE_SECONDS:
                                    chats.append(path)
                            except OSError:
                                continue
                tick += 1
                with self.lock:
                    follow = set(candidates) | set(chats) | self._watched
                for path in follow:
                    log = self._agent_logs.get(path)
                    if log is None:
                        log = self._agent_logs[path] = new_log(path)
                    with self.lock:
                        log.poll()
                keep = AGENT_RECENT_DETAILED if detailed else AGENT_RECENT
                live = sorted((self._agent_summary(self._agent_logs[p], now, keep) for p in candidates),
                              key=lambda a: a["started"])
                working = sorted((self._agent_summary(self._agent_logs[p], now, keep) for p in chats),
                                 key=lambda a: -a["now"]["since"])
                with self.lock:
                    self.snap["live_agents"] = live
                    self.snap["live_chats"] = working
                    if not detailed:                        # screen closed: let the chat logs go
                        for path in [p for p in self._agent_logs if not p.name.startswith("agent-")
                                     and p not in self._watched]:
                            del self._agent_logs[path]
                    for path in list(self._agent_logs):     # forget agents nobody is following
                        if path not in follow and len(self._agent_logs) > 40:
                            del self._agent_logs[path]
            except Exception as exc:
                self._note(f"agents: {exc}")

    def _agent_summary(self, log: agentlog.AgentLog, now: float, keep: int = AGENT_RECENT) -> dict:
        evs = log.events
        meta = log.meta
        is_chat = not log.path.name.startswith("agent-")
        session_dir = next((p for p in log.path.parents if p.name == "subagents"), log.path.parent).parent
        workflow = next((p.name for p in log.path.parents if p.name.startswith("wf_")), "")
        if is_chat:
            desc = self._titles.get(log.path.stem) or next(
                (e["text"][:60] for e in reversed(evs) if e["kind"] == "task"), "") or log.path.stem[:8]
        else:
            desc = meta.get("description") or next((e["text"][:60] for e in evs if e["kind"] == "task"), "")
        return {
            "path": log.path,
            "kind": "chat" if is_chat else "agent",
            "id": log.path.stem.replace("agent-", "")[:10],
            "desc": desc,
            "type": "chat" if is_chat else meta.get("agentType") or "",
            "model": meta.get("model") or log.model.removeprefix("claude-"),
            "chat": "" if is_chat else self._titles.get(session_dir.name, ""), "workflow": workflow,
            "started": evs[0]["ts"] if evs else now,
            "tools": sum(1 for e in evs if e["kind"] == "tool"),
            "errors": sum(1 for e in evs if e["kind"] == "result" and e["error"]),
            "now": agentlog.now_step(evs),
            "recent": [e for e in evs if e["kind"] not in ("task", "system")][-keep:],
            "tokens_out": log.tokens_out, "context": log.context,
        }

    def watch_agent(self, path: Path, on: bool = True) -> None:
        """Keep following an agent while its view is open, even after it goes quiet."""
        with self.lock:
            (self._watched.add if on else self._watched.discard)(path)

    def agent_view(self, path: Path) -> tuple[list[dict], dict]:
        """A copy of one agent's events (read so far) and its summary."""
        log = self._agent_logs.get(path)
        if log is None:
            log = self._agent_logs[path] = new_log(path)
        with self.lock:
            if not log.events:
                log.poll()
            return list(log.events), self._agent_summary(log, time.time())

    @staticmethod
    def _read_transcript(path: Path, today: str) -> dict | None:
        """One transcript's totals, read from the start (see TranscriptTally)."""
        tally = TranscriptTally(today)
        tally.feed(path)
        return tally.result(path)

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


def new_log(path: Path) -> agentlog.AgentLog:
    """A reader for a transcript. A chat's can be tens of MB: read only its end."""
    return agentlog.AgentLog(path, tail=0 if path.name.startswith("agent-") else CHAT_TAIL_BYTES)


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

    cpu = snap.get("cpu_sensors")
    if cpu:
        lvl = cpusensors.heat_level(s.cpu_temp.recent(10))
        if lvl != OK:
            add(lvl, f"CPU {cpu['temp']:.0f}°C",
                f"The processor is at {cpu['temp']:.0f} °C (about 20 s average "
                f"{s.cpu_temp.recent(10):.0f} °C). AMD X3D chips slow themselves down at 89 °C; "
                f"check the cooler and case airflow if this stays high.")

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
