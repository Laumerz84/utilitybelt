#!/usr/bin/env python
"""UtilityBelt - interactive dashboard for this machine and the Claude work on it.

    python belt.py            # full interactive dashboard
    python belt.py --probe    # print one sample of every metric and exit

Click any panel (or press its number) to open a detail view. Escape goes back.
Everything repaints once a second; nothing here starts, stops, or changes
anything on the machine.

Why the sampling is threaded
----------------------------
A 1 Hz repaint cannot afford to block. psutil counters are cheap and read on a
background thread; the GPU is not. There is no nvidia-smi on this box (the card
is an AMD RX 9070 XT), so GPU load has to come from Windows performance
counters, and Get-Counter costs 200-400 ms per call. Spawning PowerShell every
second would cost more than the frame. Instead one PowerShell process is
started once and left running, printing a JSON line per second that a reader
thread consumes. The UI only ever touches already-sampled values.

Metric selection follows what btop and glances consider the useful set: per-core
CPU rather than just an average, memory split by cached/available, disk I/O
rates alongside capacity, network throughput, and per-process attribution.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

import psutil
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Sparkline, Static

HOME = Path.home()
CLAUDE = HOME / ".claude"
PROJECTS = CLAUDE / "projects"

HIST = 120                      # samples kept per series (2 minutes at 1 Hz)
AGENT_LIVE_SECONDS = 90         # an agent transcript touched more recently than
                                # this is treated as still working

PRICES = {                      # $ per 1M tokens: in, out, cache_read, cache_write
    "claude-fable-5-1": (10, 50, .25, 20), "claude-fable-5": (10, 50, .25, 20),
    "claude-opus-5": (5, 25, .5, 10), "claude-opus-4-8": (5, 25, .5, 10),
    "claude-sonnet-5": (2, 10, .2, 4), "claude-sonnet-4-6": (3, 15, .3, 6),
    "claude-haiku-4-5": (1, 5, .1, 2),
}
LOCAL = ("gpt-oss", "qwen", "llama", "mistral")


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


def heat(frac: float) -> str:
    """Colour name for a 0-1 load, shared by every bar so severity reads the same."""
    if frac < 0.60:
        return "green"
    if frac < 0.85:
        return "yellow"
    return "red"


def bar(frac: float, width: int = 18) -> str:
    frac = max(0.0, min(1.0, float(frac or 0)))
    filled = int(round(frac * width))
    colour = heat(frac)
    return (f"[{colour}]{'█' * filled}[/][dim]{'─' * (width - filled)}[/]")


# ----------------------------------------------------------------- sampling

class Series:
    """A bounded history that also answers 'what is it doing now'."""

    def __init__(self) -> None:
        self.points: deque[float] = deque([0.0] * HIST, maxlen=HIST)

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
            "errors": [],
        }

        self._proc_cache: dict[int, psutil.Process] = {}
        self._last_net = psutil.net_io_counters()
        self._last_disk = psutil.disk_io_counters()
        self._last_cpu_times = psutil.cpu_stats()
        self._seen_files: dict = {}

        threading.Thread(target=self._fast_loop, daemon=True).start()
        threading.Thread(target=self._slow_loop, daemon=True).start()
        threading.Thread(target=self._gpu_loop, daemon=True).start()

    # -- fast: every second, cheap counters only
    def _fast_loop(self) -> None:
        psutil.cpu_percent(percpu=True)          # prime; first call is garbage
        while not self.stop.wait(1.0):
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

    def _sample_processes(self) -> None:
        rows = []
        alive = set()
        for proc in psutil.process_iter(["pid", "name", "memory_info", "num_threads"]):
            try:
                pid = proc.info["pid"]
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
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        for pid in list(self._proc_cache):
            if pid not in alive:
                self._proc_cache.pop(pid, None)
        rows.sort(key=lambda r: -r["cpu"])
        with self.lock:
            self.snap["procs"] = rows

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
        out.sort(key=lambda d: -d["total"])
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

    @staticmethod
    def _read_transcript(path: Path, today: str) -> dict | None:
        seen: set = set()
        models: dict = defaultdict(lambda: [0, 0, 0, 0])
        today_models: dict = defaultdict(lambda: [0, 0, 0, 0])
        calls = today_calls = 0
        title = ""
        last_tool = ""
        last_text = ""
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
                message = entry.get("message") or {}
                if not title and entry.get("type") == "user":
                    content = message.get("content")
                    if isinstance(content, str):
                        title = content.strip().replace("\n", " ")[:60]
                if entry.get("type") != "assistant":
                    continue
                for block in (message.get("content") or []):
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        last_tool = block.get("name") or ""
                    elif block.get("type") == "text" and block.get("text"):
                        last_text = str(block["text"]).strip().replace("\n", " ")[:70]
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
            "title": title or last_text or "(no prompt text)",
            "calls": calls,
            "today_calls": today_calls,
            "models": dict(models),
            "today_models": dict(today_models),
            "cache_read": sum(v[2] for v in models.values()),
            "output": sum(v[3] for v in models.values()),
            "cost": sum(cost_of(m, v) for m, v in models.items()),
            "last_tool": last_tool,
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


def cost_of(model: str, tokens) -> float:
    if any(x in model for x in LOCAL):
        return 0.0
    price = PRICES.get(model) or PRICES.get(model.rsplit("-2", 1)[0])
    if not price:
        return 0.0
    return (tokens[0] * price[0] + tokens[3] * price[1]
            + tokens[2] * price[2] + tokens[1] * price[3]) / 1e6


# -------------------------------------------------------------------- cards

class Card(Static):
    """One clickable panel on the overview grid."""

    def __init__(self, key: str, title: str, index: int) -> None:
        super().__init__(id=f"card-{key}", classes="card")
        self.key = key
        self.title_text = title
        self.index = index

    def on_click(self, event: events.Click) -> None:
        self.app.open_detail(self.key)


class Overview(Screen):
    CARDS = [
        ("cpu", "CPU"), ("memory", "MEMORY"), ("gpu", "GPU"), ("storage", "STORAGE"),
        ("network", "NETWORK"), ("claude", "CLAUDE"), ("agents", "AGENTS"),
        ("processes", "PROCESSES"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Grid(id="grid"):
            for i, (key, title) in enumerate(self.CARDS, start=1):
                yield Card(key, title, i)
        yield Static(id="statusline")
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(1.0, self.redraw)
        self.redraw()

    def redraw(self) -> None:
        snap = self.app.sampler.read()
        for key, _ in self.CARDS:
            try:
                card = self.query_one(f"#card-{key}", Card)
            except Exception:
                continue
            card.update(self.app.render_card(key, snap, card))
        uptime = duration(time.time() - snap.get("boot", time.time()))
        errs = snap.get("errors") or []
        note = f"  [red]{errs[-1]}[/]" if errs else ""
        self.query_one("#statusline", Static).update(
            f"[dim]up {uptime}  ·  {snap['claude']['scanned']} transcripts  ·  "
            f"click a panel or press 1-8 for detail  ·  costs are API list rates, "
            f"not your bill[/]{note}")


class Detail(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Back"), ("q", "app.pop_screen", "Back")]

    def __init__(self, key: str) -> None:
        super().__init__()
        self.key = key

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll(id="detail-body"):
            yield Static(id="detail-content")
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(1.0, self.redraw)
        self.redraw()

    def redraw(self) -> None:
        snap = self.app.sampler.read()
        self.query_one("#detail-content", Static).update(
            self.app.render_detail(self.key, snap))


# ---------------------------------------------------------------------- app

class Belt(App):
    CSS = """
    Screen { background: $surface; }
    #grid {
        layout: grid;
        grid-size: 4 2;
        grid-gutter: 1 2;
        padding: 1 2;
    }
    .card {
        border: round $primary 40%;
        padding: 0 1;
        height: 100%;
    }
    .card:hover { border: round $accent; background: $boost; }
    #statusline { padding: 0 3; height: 1; }
    #detail-body { padding: 1 3; }
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
    ]

    TITLE = "UtilityBelt"

    def __init__(self, sampler: Sampler) -> None:
        super().__init__()
        self.sampler = sampler

    def on_mount(self) -> None:
        self.push_screen(Overview())

    def action_detail(self, key: str) -> None:
        self.open_detail(key)

    def open_detail(self, key: str) -> None:
        if isinstance(self.screen, Detail):
            self.pop_screen()
        self.push_screen(Detail(key))

    # ---- overview card bodies

    def render_card(self, key: str, snap: dict, card: Card) -> str:
        s = self.sampler
        head = f"[bold]{card.index} {card.title_text}[/]"
        try:
            body = getattr(self, f"_card_{key}")(snap, s)
        except Exception as exc:
            body = f"[red]{type(exc).__name__}: {exc}[/]"
        return f"{head}\n{body}"

    def _card_cpu(self, snap, s) -> str:
        pct = s.cpu.now
        cores = len(s.cpu_cores)
        hot = sorted((c.now for c in s.cpu_cores), reverse=True)[:1]
        freq = snap.get("cpu_freq") or 0
        return (f"{bar(pct / 100)} [bold]{pct:4.1f}%[/]\n"
                f"[dim]{cores} threads · peak core {hot[0] if hot else 0:.0f}% · "
                f"{freq / 1000:.1f} GHz[/]\n"
                f"[dim]2m avg {s.cpu.mean:.0f}% · max {s.cpu.peak:.0f}%[/]")

    def _card_memory(self, snap, s) -> str:
        vm = snap.get("mem")
        if not vm:
            return "[dim]sampling…[/]"
        sw = snap.get("swap")
        swap_line = (f"\n[dim]swap {sw.percent:.0f}% of {human_bytes(sw.total)}[/]"
                     if sw else "")
        return (f"{bar(vm.percent / 100)} [bold]{vm.percent:4.1f}%[/]\n"
                f"[dim]{human_bytes(vm.used)} of {human_bytes(vm.total)} · "
                f"{human_bytes(vm.available)} free[/]"
                f"{swap_line}")

    def _card_gpu(self, snap, s) -> str:
        used = snap.get("vram_used") or 0
        total = snap.get("vram_total") or 0
        pct = s.gpu.now
        if total == 0 and pct == 0:
            return "[dim]waiting for counters…[/]"
        vfrac = used / total if total else 0
        return (f"{bar(pct / 100)} [bold]{pct:4.1f}%[/] [dim]load[/]\n"
                f"{bar(vfrac)} [bold]{vfrac * 100:4.1f}%[/] [dim]vram[/]\n"
                f"[dim]{human_bytes(used)} of {human_bytes(total)}[/]")

    def _card_storage(self, snap, s) -> str:
        disks = snap.get("disks") or []
        if not disks:
            return "[dim]sampling…[/]"
        lines = []
        for d in disks[:4]:
            lines.append(f"[dim]{d['device']:<3}[/]{bar(d['percent'] / 100, 12)} "
                         f"[dim]{human_bytes(d['free'])} free[/]")
        lines.append(f"[dim]r {rate(s.disk_read.now)} · w {rate(s.disk_write.now)}[/]")
        return "\n".join(lines)

    def _card_network(self, snap, s) -> str:
        return (f"[green]▼[/] [bold]{rate(s.net_down.now)}[/]\n"
                f"[cyan]▲[/] [bold]{rate(s.net_up.now)}[/]\n"
                f"[dim]peak ▼{rate(s.net_down.peak)} ▲{rate(s.net_up.peak)}[/]")

    def _card_claude(self, snap, s) -> str:
        if not snap.get("claude_ready"):
            return "[dim]scanning transcripts…[/]"
        c = snap["claude"]
        cr = sum(v[2] for v in c["models"].values())
        out = sum(v[3] for v in c["models"].values())
        resend = cr / (cr + out) * 100 if (cr + out) else 0
        return (f"[bold]{c['today_calls']:,}[/] calls today  [dim]~${c['today_cost']:,.2f}[/]\n"
                f"[dim]{len(c['sessions'])} sessions · "
                f"${sum(x['cost'] for x in c['sessions']):,.0f} all time[/]\n"
                f"[dim]resend {resend:.1f}% · {cr / 1e9:.2f}B cached[/]")

    def _card_agents(self, snap, s) -> str:
        if not snap.get("claude_ready"):
            return "[dim]scanning transcripts…[/]"
        agents = snap.get("agents") or []
        live = [a for a in agents if a["live"]]
        if not agents:
            return "[dim]no agent transcripts[/]"
        lines = [f"[bold]{len(live)}[/] live [dim]of {len(agents)} seen[/]"]
        for a in live[:2]:
            tool = a["last_tool"] or "thinking"
            lines.append(f"[green]●[/] [dim]{a['id']}[/] {tool}")
        if not live:
            lines.append(f"[dim]last: {agents[0]['id']} "
                         f"{duration(agents[0]['age'])} ago[/]")
        return "\n".join(lines)

    def _card_processes(self, snap, s) -> str:
        procs = snap.get("procs") or []
        if not procs:
            return "[dim]sampling…[/]"
        lines = []
        for p in procs[:3]:
            lines.append(f"[dim]{p['name'][:14]:<14}[/]{p['cpu']:5.1f}%  "
                         f"[dim]{human_bytes(p['rss'])}[/]")
        return "\n".join(lines)

    # ---- detail bodies

    def render_detail(self, key: str, snap: dict) -> str:
        try:
            return getattr(self, f"_detail_{key}")(snap, self.sampler)
        except Exception as exc:
            return f"[red]{type(exc).__name__}: {exc}[/]"

    def _detail_cpu(self, snap, s) -> str:
        out = [f"[bold]CPU[/]  [dim]{snap['cpu_physical']} cores / "
               f"{snap['cpu_logical']} threads[/]", ""]
        out.append(f"total   {bar(s.cpu.now / 100, 30)} [bold]{s.cpu.now:5.1f}%[/]")
        out.append(f"[dim]2 min average {s.cpu.mean:.1f}%   peak {s.cpu.peak:.1f}%[/]")
        out.append("")
        out.append("[bold]per thread[/]")
        for i, core in enumerate(s.cpu_cores):
            out.append(f"  {i:>2}  {bar(core.now / 100, 26)} {core.now:5.1f}% "
                       f"[dim]avg {core.mean:4.1f}%[/]")
        freq, fmax = snap.get("cpu_freq") or 0, snap.get("cpu_freq_max") or 0
        out += ["", "[bold]clocks and interrupts[/]",
                f"  frequency      {freq / 1000:.2f} GHz"
                + (f" [dim]of {fmax / 1000:.2f} GHz[/]" if fmax else ""),
                f"  context switches {snap.get('ctx_switches', 0):,}/s",
                f"  interrupts       {snap.get('interrupts', 0):,}/s",
                f"  uptime           {duration(time.time() - snap.get('boot', time.time()))}"]
        try:
            load = psutil.getloadavg()
            out.append(f"  load average     {load[0]:.2f}  {load[1]:.2f}  {load[2]:.2f}")
        except Exception:
            pass
        procs = snap.get("procs") or []
        out += ["", "[bold]top by CPU[/]"]
        for p in procs[:12]:
            out.append(f"  {p['cpu']:5.1f}%  [dim]{p['pid']:>7}[/]  {p['name'][:28]:<28}"
                       f"[dim]{human_bytes(p['rss'])}[/]")
        return "\n".join(out)

    def _detail_memory(self, snap, s) -> str:
        vm, sw = snap.get("mem"), snap.get("swap")
        if not vm:
            return "[dim]sampling…[/]"
        out = ["[bold]MEMORY[/]", ""]
        out.append(f"physical  {bar(vm.percent / 100, 30)} [bold]{vm.percent:5.1f}%[/]")
        out += [
            f"  total      {human_bytes(vm.total)}",
            f"  used       {human_bytes(vm.used)}",
            f"  available  {human_bytes(vm.available)}",
        ]
        for field in ("cached", "buffers", "shared"):
            if hasattr(vm, field):
                out.append(f"  {field:<10} {human_bytes(getattr(vm, field))}")
        if sw:
            out += ["", f"swap      {bar(sw.percent / 100, 30)} [bold]{sw.percent:5.1f}%[/]",
                    f"  total      {human_bytes(sw.total)}",
                    f"  used       {human_bytes(sw.used)}"]
            if getattr(sw, "sin", 0) or getattr(sw, "sout", 0):
                out.append(f"  paged      in {human_bytes(sw.sin)} / out {human_bytes(sw.sout)}")
        out += ["", "[dim]2 min: "
                f"avg {s.mem.mean:.1f}%  peak {s.mem.peak:.1f}%[/]", ""]
        out.append("[bold]top by memory[/]")
        for p in sorted(snap.get("procs") or [], key=lambda r: -r["rss"])[:15]:
            frac = p["rss"] / vm.total if vm.total else 0
            out.append(f"  {bar(frac, 12)} {human_bytes(p['rss']):>9}  "
                       f"[dim]{p['pid']:>7}[/]  {p['name'][:30]}")
        return "\n".join(out)

    def _detail_gpu(self, snap, s) -> str:
        used = snap.get("vram_used") or 0
        total = snap.get("vram_total") or 0
        out = ["[bold]GPU[/]", ""]
        out.append(f"load      {bar(s.gpu.now / 100, 30)} [bold]{s.gpu.now:5.1f}%[/]")
        if total:
            out.append(f"vram      {bar(used / total, 30)} [bold]{used / total * 100:5.1f}%[/]")
            out.append(f"  {human_bytes(used)} of {human_bytes(total)}")
        out.append(f"[dim]2 min: avg {s.gpu.mean:.1f}%  peak {s.gpu.peak:.1f}%[/]")
        types = snap.get("gpu_types") or {}
        if types:
            out += ["", "[bold]by engine[/]",
                    "[dim]3D is rendering and most compute; copy is data moving "
                    "to and from the card; videodecode/encode are the media blocks.[/]"]
            for name, value in sorted(types.items(), key=lambda kv: -float(kv[1] or 0)):
                v = min(100.0, float(value or 0))
                out.append(f"  {name[:20]:<20} {bar(v / 100, 22)} {v:5.1f}%")
        else:
            out += ["", "[dim]No engine breakdown yet — the counter stream takes "
                    "a second to start.[/]"]
        out += ["", "[dim]Read from Windows GPU performance counters. This box has "
                "an AMD card, so there is no nvidia-smi and therefore no "
                "temperature, fan or power draw available here.[/]"]
        return "\n".join(out)

    def _detail_storage(self, snap, s) -> str:
        out = ["[bold]STORAGE[/]", ""]
        for d in snap.get("disks") or []:
            out += [f"[bold]{d['device']}[/] [dim]{d['fstype']}[/]",
                    f"  {bar(d['percent'] / 100, 30)} {d['percent']:5.1f}%",
                    f"  {human_bytes(d['used'])} used · {human_bytes(d['free'])} free "
                    f"· {human_bytes(d['total'])} total", ""]
        out += ["[bold]throughput (all volumes)[/]",
                f"  read   {rate(s.disk_read.now):>12}  [dim]peak {rate(s.disk_read.peak)}[/]",
                f"  write  {rate(s.disk_write.now):>12}  [dim]peak {rate(s.disk_write.peak)}[/]",
                ""]
        per = snap.get("per_disk") or {}
        if per:
            out.append("[bold]lifetime per device[/]")
            for name, v in sorted(per.items(), key=lambda kv: -(kv[1]["read"] + kv[1]["write"]))[:8]:
                out.append(f"  {name[:22]:<22} read {human_bytes(v['read']):>9}  "
                           f"write {human_bytes(v['write']):>9}  "
                           f"[dim]{v['rcount'] + v['wcount']:,} ops[/]")
        out += ["", "[dim]disk-sentinel.py answers the other storage question — "
                "what is actually eating the space.[/]"]
        return "\n".join(out)

    def _detail_network(self, snap, s) -> str:
        out = ["[bold]NETWORK[/]", "",
               f"  down   [bold]{rate(s.net_down.now):>12}[/]  "
               f"[dim]peak {rate(s.net_down.peak)}  avg {rate(s.net_down.mean)}[/]",
               f"  up     [bold]{rate(s.net_up.now):>12}[/]  "
               f"[dim]peak {rate(s.net_up.peak)}  avg {rate(s.net_up.mean)}[/]", ""]
        out.append("[bold]interfaces[/]")
        for n in snap.get("nets") or []:
            speed = f"{n['speed']} Mb/s link" if n["speed"] else "speed unknown"
            out += [f"  [bold]{n['name'][:34]}[/] [dim]{speed}[/]",
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

    def _detail_claude(self, snap, s) -> str:
        c = snap["claude"]
        out = ["[bold]CLAUDE[/]", "",
               f"  today        [bold]{c['today_calls']:,}[/] calls  "
               f"[dim]~${c['today_cost']:,.2f}[/]",
               f"  all time     {len(c['sessions'])} sessions  "
               f"[dim]${sum(x['cost'] for x in c['sessions']):,.2f}[/]", ""]
        models = c.get("models") or {}
        if models:
            out.append("[bold]by model[/]  [dim]input · cache write · cache read · output[/]")
            for name, v in sorted(models.items(), key=lambda kv: -cost_of(kv[0], kv[1])):
                out.append(f"  {name[:26]:<26} {v[0] / 1e6:7.1f}M {v[1] / 1e6:7.1f}M "
                           f"{v[2] / 1e6:9.1f}M {v[3] / 1e6:7.1f}M   "
                           f"[bold]${cost_of(name, v):,.2f}[/]")
            cr = sum(v[2] for v in models.values())
            out_tok = sum(v[3] for v in models.values())
            if cr + out_tok:
                out += ["", f"[dim]{cr / (cr + out_tok) * 100:.1f}% of all tokens are "
                        f"cache reads — the conversation being re-read each turn, "
                        f"not new work.[/]"]
        out += ["", "[bold]most expensive sessions[/]"]
        for sess in c["sessions"][:12]:
            out.append(f"  [bold]${sess['cost']:>8,.2f}[/]  [dim]{sess['id']:<10}[/] "
                       f"{sess['calls']:>5} calls  {sess['title'][:44]}")
        out += ["", "[dim]Costs are API list rates applied to the transcript logs. "
                "Your plan is billed differently — /usage is authoritative.[/]"]
        return "\n".join(out)

    def _detail_agents(self, snap, s) -> str:
        agents = snap.get("agents") or []
        out = ["[bold]AGENTS[/]  [dim]subagent transcripts under "
               "~/.claude/projects[/]", ""]
        if not agents:
            return "\n".join(out + ["[dim]No agent transcripts found.[/]"])
        live = [a for a in agents if a["live"]]
        out.append(f"  [bold]{len(live)}[/] working now  [dim]· {len(agents)} seen "
                   f"in total · 'working' means the log was written to within "
                   f"{AGENT_LIVE_SECONDS}s[/]")
        out.append("")
        for a in agents[:20]:
            dot = "[green]●[/]" if a["live"] else "[dim]○[/]"
            tool = a["last_tool"] or "—"
            wf = f" [dim]{a['workflow'][:14]}[/]" if a["workflow"] else ""
            out += [f"{dot} [bold]{a['id']}[/]{wf}  [dim]{duration(a['age'])} since "
                    f"last write[/]",
                    f"    last tool [bold]{tool}[/]  ·  {a['calls']} calls  ·  "
                    f"[dim]${a['cost']:,.2f}[/]",
                    f"    [dim]{a['title'][:76]}[/]"]
        return "\n".join(out)

    def _detail_processes(self, snap, s) -> str:
        procs = snap.get("procs") or []
        vm = snap.get("mem")
        out = [f"[bold]PROCESSES[/]  [dim]{len(procs)} running[/]", ""]
        groups: dict = defaultdict(lambda: [0, 0.0, 0])
        for p in procs:
            g = groups[p["name"]]
            g[0] += 1
            g[1] += p["cpu"]
            g[2] += p["rss"]
        out.append("[bold]by program[/]  [dim]instances · cpu · memory[/]")
        for name, (count, cpu, rss) in sorted(groups.items(), key=lambda kv: -kv[1][2])[:14]:
            out.append(f"  {name[:26]:<26} [dim]{count:>3}×[/]  {cpu:5.1f}%  "
                       f"{human_bytes(rss):>9}")
        out += ["", "[bold]top single processes by CPU[/]"]
        for p in procs[:14]:
            out.append(f"  {p['cpu']:5.1f}%  [dim]{p['pid']:>7}[/]  {p['name'][:30]:<30}"
                       f"{human_bytes(p['rss']):>9}  [dim]{p['threads']} threads[/]")
        if vm:
            out += ["", "[bold]top single processes by memory[/]"]
            for p in sorted(procs, key=lambda r: -r["rss"])[:14]:
                out.append(f"  {human_bytes(p['rss']):>9}  [dim]{p['pid']:>7}[/]  "
                           f"{p['name'][:30]:<30}{p['cpu']:5.1f}%")
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
        if snap.get("procs") and snap.get("disks") and snap["claude"]["scanned"]:
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
    print(f"gpu          {sampler.gpu.now:.1f}%  engines={list((snap.get('gpu_types') or {}).keys())}")
    print(f"vram         {human_bytes(snap.get('vram_used') or 0)} / "
          f"{human_bytes(snap.get('vram_total') or 0)}")
    print(f"disks        {[d['device'] for d in snap.get('disks') or []]}")
    print(f"disk io      r {rate(sampler.disk_read.now)}  w {rate(sampler.disk_write.now)}")
    print(f"net          down {rate(sampler.net_down.now)}  up {rate(sampler.net_up.now)}")
    print(f"nics         {[n['name'] for n in snap.get('nets') or []][:4]}")
    print(f"processes    {len(snap.get('procs') or [])}")
    c = snap["claude"]
    print(f"claude       {c['today_calls']} calls today, ${c['today_cost']:.2f}, "
          f"{len(c['sessions'])} sessions")
    print(f"agents       {len(snap.get('agents') or [])} "
          f"({sum(1 for a in snap.get('agents') or [] if a['live'])} live)")
    if snap.get("errors"):
        print("errors      ", snap["errors"])
    sampler.stop.set()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--probe", action="store_true",
                    help="print one sample of every metric and exit")
    args = ap.parse_args()
    if args.probe:
        return probe()
    sampler = Sampler()
    app = Belt(sampler)
    try:
        app.run()
    finally:
        sampler.stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
