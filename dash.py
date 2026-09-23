#!/usr/bin/env python
"""UtilityBelt - live dashboard for this machine and the Claude work on it.

    python dash.py              # live, repaints continuously
    python dash.py --once       # print one frame and exit
    python dash.py --tick 2     # slower repaint

Read-only. Nothing here starts, stops, or changes anything.

Three clocks, because the panels cost wildly different amounts to produce:
  every tick   clock, and whatever is already cached
  15s          RAM/VRAM/disk (one PowerShell call), process counts
  60s          git status per repo, and the transcript rescan

The transcript scan is the expensive one - 24 files, one of them holding a
billion cache-read tokens - so it is incremental: a file is only re-read when
its mtime or size changes. After the first pass a refresh costs almost nothing.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
CLAUDE = HOME / ".claude"
PROJECTS = CLAUDE / "projects"

REPOS = [
    Path("G:/kitchenmind"),
    Path("F:/python/stock-screener-dashboard"),
    Path("F:/Python"),
    Path("F:/clodcode"),
]

R, DIM, BOLD = "\033[0m", "\033[2m", "\033[1m"
GREEN, YELLOW, RED, CYAN, MAG = (
    "\033[32m", "\033[33m", "\033[31m", "\033[36m", "\033[35m")

PRICES = {
    "claude-fable-5-1": (10, 50, .25, 20), "claude-fable-5": (10, 50, .25, 20),
    "claude-opus-5": (5, 25, .5, 10), "claude-opus-4-8": (5, 25, .5, 10),
    "claude-sonnet-5": (2, 10, .2, 4), "claude-sonnet-4-6": (3, 15, .3, 6),
    "claude-haiku-4-5": (1, 5, .1, 2),
}
LOCAL = ("gpt-oss", "qwen", "llama", "mistral")


# ----------------------------------------------------------------- plumbing

def setup() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if sys.platform == "win32":
        import ctypes
        k = ctypes.windll.kernel32
        k.SetConsoleMode(k.GetStdHandle(-11), 7)


def plain(s: str) -> str:
    """Visible length helper - strips SGR so padding maths stays right."""
    out, esc = [], False
    for ch in s:
        if ch == "\033":
            esc = True
        elif esc:
            esc = ch != "m"
        else:
            out.append(ch)
    return "".join(out)


def bar(frac: float, width: int = 10) -> str:
    frac = max(0.0, min(1.0, frac))
    n = int(frac * width)
    col = GREEN if frac < .75 else (YELLOW if frac < .92 else RED)
    return f"{col}{'#' * n}{R}{DIM}{'-' * (width - n)}{R}"


def gb(n: float) -> str:
    if n >= 1024 ** 4:
        return f"{n / 1024 ** 4:.1f} TB"
    return f"{n / 1024 ** 3:.1f} GB"


def panel(title: str, rows: list, width: int, height: int | None = None,
          accent: str = CYAN) -> list:
    """Fixed-width box. Returns unprefixed lines so two can sit side by side."""
    inner = width - 4
    head = f"- {title} "
    body = [f"{DIM}+{accent}{head}{DIM}{'-' * max(0, inner + 2 - len(head))}+{R}"]
    rows = rows[:height] if height else rows
    for row in rows:
        pad = max(0, inner - len(plain(row)))
        body.append(f"{DIM}|{R} {row}{' ' * pad} {DIM}|{R}")
    if height:
        for _ in range(height - len(rows)):
            body.append(f"{DIM}|{R} {' ' * inner} {DIM}|{R}")
    body.append(f"{DIM}+{'-' * (inner + 2)}+{R}")
    return body


def columns(left: list, right: list, gap: int = 2) -> list:
    n = max(len(left), len(right))
    left = left + [""] * (n - len(left))
    right = right + [""] * (n - len(right))
    lw = max((len(plain(l)) for l in left), default=0)
    out = []
    for l, r in zip(left, right):
        out.append(f" {l}{' ' * (lw - len(plain(l)) + gap)}{r}")
    return out


def paint(lines: list, prev: int) -> int:
    """Repaint without clearing first - clearing is what makes it flicker."""
    buf = ["\033[H"]
    for line in lines:
        buf.append(line + "\033[K\n")
    for _ in range(max(0, prev - len(lines))):
        buf.append("\033[K\n")
    sys.stdout.write("".join(buf))
    sys.stdout.flush()
    return len(lines)


class Cache:
    """Value plus TTL. Keeps an expensive panel from running every tick."""

    def __init__(self) -> None:
        self.at: dict = {}
        self.val: dict = {}

    def get(self, key: str, ttl: float, produce):
        now = time.time()
        if key not in self.val or now - self.at.get(key, 0) >= ttl:
            try:
                self.val[key] = produce()
            except Exception:
                self.val.setdefault(key, None)
            self.at[key] = now
        return self.val[key]


# ----------------------------------------------------------------- claude

_SEEN: dict = {}      # path -> (mtime, size, stats) so rescans stay cheap


def scan_transcripts() -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sessions, today_models = [], defaultdict(lambda: [0, 0, 0, 0])
    today_calls = 0

    for path in PROJECTS.glob("*/*.jsonl") if PROJECTS.exists() else []:
        try:
            st = path.stat()
        except OSError:
            continue
        cached = _SEEN.get(path)
        if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
            stats = cached[2]
        else:
            stats = _read_one(path, today)
            _SEEN[path] = (st.st_mtime, st.st_size, stats)
        if not stats:
            continue
        sessions.append(stats)
        today_calls += stats["today_calls"]
        for m, v in stats["today_models"].items():
            for i in range(4):
                today_models[m][i] += v[i]

    cost_today = sum(_cost(m, v) for m, v in today_models.items())
    sessions.sort(key=lambda s: -s["cost"])
    return {"sessions": sessions, "today_calls": today_calls,
            "today_cost": cost_today}


def _cost(model: str, t) -> float:
    if any(x in model for x in LOCAL):
        return 0.0
    p = PRICES.get(model) or PRICES.get(model.rsplit("-2", 1)[0])
    return (t[0] * p[0] + t[3] * p[1] + t[2] * p[2] + t[1] * p[3]) / 1e6 if p else 0.0


def _read_one(path: Path, today: str) -> dict | None:
    seen: set = set()
    per_model: dict = defaultdict(lambda: [0, 0, 0, 0])
    today_models: dict = defaultdict(lambda: [0, 0, 0, 0])
    calls = today_calls = 0
    title = ""
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
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not title and e.get("type") == "user":
                c = (e.get("message") or {}).get("content")
                if isinstance(c, str):
                    title = c.strip().replace("\n", " ")[:40]
            if e.get("type") != "assistant":
                continue
            m = e.get("message") or {}
            u = m.get("usage")
            if not u:
                continue
            mid = m.get("id")
            if mid and mid in seen:       # same response logged twice
                continue
            if mid:
                seen.add(mid)
            calls += 1
            model = m.get("model") or "?"
            vals = (u.get("input_tokens", 0), u.get("cache_creation_input_tokens", 0),
                    u.get("cache_read_input_tokens", 0), u.get("output_tokens", 0))
            for i in range(4):
                per_model[model][i] += vals[i]
            if (e.get("timestamp") or "")[:10] == today:
                today_calls += 1
                for i in range(4):
                    today_models[model][i] += vals[i]
    if not calls:
        return None
    return {"id": path.stem[:8], "calls": calls, "title": title or "(no text)",
            "cr": sum(v[2] for v in per_model.values()),
            "out": sum(v[3] for v in per_model.values()),
            "cost": sum(_cost(m, v) for m, v in per_model.items()),
            "today_calls": today_calls, "today_models": dict(today_models)}


def claude_rows(u: dict, w: int) -> list:
    s = u["sessions"]
    if not s:
        return [f"{DIM}no transcripts{R}"]
    cr = sum(x["cr"] for x in s)
    out = sum(x["out"] for x in s)
    cost = sum(x["cost"] for x in s)
    calls = sum(x["calls"] for x in s)
    big = s[0]
    col = RED if big["cost"] > 200 else DIM
    return [
        f"{DIM}today    {R}{BOLD}{u['today_calls']:,} calls{R}  {DIM}~${u['today_cost']:,.2f}{R}",
        f"{DIM}all      {R}{len(s)} sessions  {DIM}{calls:,} calls  ~${cost:,.0f}{R}",
        f"{DIM}resend   {R}{cr/(cr+out)*100:.1f}%  {DIM}{cr/1e9:.2f}B cached vs {out/1e6:.1f}M out{R}",
        f"{DIM}largest  {R}{col}{big['id']} ${big['cost']:,.0f}{R} {DIM}{big['calls']:,} calls{R}",
    ]


# ----------------------------------------------------------------- machine

def machine() -> dict:
    if sys.platform != "win32":
        return {}
    ps = r"""
$ErrorActionPreference='SilentlyContinue'
$os = Get-CimInstance Win32_OperatingSystem
$vram = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\*' |
         Where-Object { $_.'HardwareInformation.qwMemorySize' } |
         Measure-Object -Property 'HardwareInformation.qwMemorySize' -Maximum).Maximum
$used = (Get-Counter '\GPU Adapter Memory(*)\Dedicated Usage' -EA SilentlyContinue).CounterSamples |
        Measure-Object -Property CookedValue -Sum
$cpu = (Get-Counter '\Processor(_Total)\% Processor Time' -EA SilentlyContinue).CounterSamples[0].CookedValue
$gpu = (Get-CimInstance Win32_VideoController | Where-Object {$_.Name -notmatch 'Basic|Remote'} | Select-Object -First 1).Name
$disks = Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3 AND Size>50000000000" |
         ForEach-Object { [pscustomobject]@{ id=$_.DeviceID; free=$_.FreeSpace; size=$_.Size } }
$procs = Get-Process claude,ollama,python,msedge,chrome -EA SilentlyContinue |
         Group-Object ProcessName |
         ForEach-Object { [pscustomobject]@{ n=$_.Name; c=$_.Count; g=[math]::Round((($_.Group|Measure-Object WorkingSet64 -Sum).Sum/1GB),2) } }
[pscustomobject]@{
  ram_total=($os.TotalVisibleMemorySize*1kb); ram_free=($os.FreePhysicalMemory*1kb)
  vram_total=$vram; vram_used=$used.Sum; cpu=$cpu; gpu=$gpu; disks=$disks; procs=$procs
} | ConvertTo-Json -Compress -Depth 4
"""
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                          "-Command", ps], capture_output=True, text=True,
                         timeout=30).stdout.strip()
    return json.loads(out) if out.startswith("{") else {}


def as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def machine_rows(m: dict, inner: int = 60) -> list:
    if not m:
        return [f"{DIM}unavailable{R}"]
    rows = []
    rt, rf = m.get("ram_total") or 0, m.get("ram_free") or 0
    if rt:
        f = (rt - rf) / rt
        rows.append(f"{DIM}ram {R}{bar(f)} {gb(rt-rf)} / {gb(rt)} {DIM}{f*100:.0f}%{R}")
    vt, vu = m.get("vram_total") or 0, m.get("vram_used") or 0
    if vt:
        f = vu / vt
        rows.append(f"{DIM}vram{R} {bar(f)} {gb(vu)} / {gb(vt)} {DIM}{f*100:.0f}%{R}")
    cpu = min(m.get("cpu") or 0, 100)
    rows.append(f"{DIM}cpu {R}{bar(cpu/100)} {cpu:.0f}%")

    # Disk bars are scaled to CAPACITY as well as fill: the widest bar is the
    # biggest drive, so a 1.8 TB disk visibly outruns a 231 GB one instead of
    # both rendering as the same 10 cells. Fill within each bar is still its
    # own used fraction. Linear, not sqrt - a bar that misreports relative
    # size is exactly the thing this is fixing.
    disks = [d for d in as_list(m.get("disks")) if (d.get("size") or 0) > 0]
    if disks:
        biggest = max(d["size"] for d in disks)
        span = max(8, min(26, inner - 26))      # leave room for the text
        for d in sorted(disks, key=lambda x: -x["size"]):
            size, free = d["size"], d.get("free") or 0
            w = max(3, round(span * size / biggest))
            used = max(0.0, min(1.0, (size - free) / size))
            n = int(used * w)
            col = GREEN if used < .8 else (YELLOW if used < .93 else RED)
            cells = f"{col}{'#' * n}{R}{DIM}{'-' * (w - n)}{R}"
            pad = " " * (span - w)
            rows.append(f"{DIM}{d.get('id','?'):<4}{R}{cells}{pad} "
                        f"{DIM}{gb(free)} free of {gb(size)}{R}")
    return rows


def proc_rows(m: dict) -> list:
    procs = as_list(m.get("procs"))
    if not procs:
        return [f"{DIM}nothing running{R}"]
    procs.sort(key=lambda p: -(p.get("g") or 0))
    return [f"{p.get('n',''):<9}{DIM}{p.get('c',0):>3} proc{R}  {p.get('g',0):>5.2f} GB"
            for p in procs[:6]]


# ----------------------------------------------------------------- repos

def repo_rows() -> list:
    rows = []
    for repo in REPOS:
        if not (repo / ".git").exists():
            continue

        def git(*a):
            try:
                return subprocess.run(["git", "-C", str(repo), *a],
                                      capture_output=True, text=True,
                                      timeout=15).stdout.strip()
            except Exception:
                return ""
        dirty = len([l for l in git("status", "--porcelain").splitlines() if l])
        tracked = git("rev-parse", "--abbrev-ref", "@{u}")
        ahead = len(git("log", "--oneline", "@{u}..HEAD").splitlines()) if tracked else -1
        bits = []
        if dirty:
            bits.append(f"{YELLOW}{dirty} uncommitted{R}")
        if ahead > 0:
            bits.append(f"{RED}{ahead} unpushed{R}")
        elif ahead < 0:
            bits.append(f"{DIM}no remote{R}")
        if not bits:
            bits.append(f"{GREEN}clean{R}")
        rows.append(f"{repo.name[:18]:<19}{' '.join(bits)}")
    return rows or [f"{DIM}no repos found{R}"]


# ----------------------------------------------------------------- tools

def describe(path: Path) -> str:
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            lines = [next(fh, "") for _ in range(14)]
    except OSError:
        return ""
    if path.suffix == ".py":
        for i, line in enumerate(lines):
            s = line.strip()
            if s.startswith(('"""', "'''")):
                first = s[3:].strip()
                return first.rstrip('"\'') or (lines[i + 1].strip() if i + 1 < len(lines) else "")
        return ""
    for line in lines:
        s = line.strip()
        if s.startswith("#!") or not s:
            continue
        if s.startswith("#"):
            return s.lstrip("# ").strip()
        if s.upper().startswith("REM "):
            return s[4:].strip()
    return ""


def tool_rows() -> list:
    rows = []
    for f in sorted(CLAUDE.glob("*.py")) + sorted(CLAUDE.glob("*.ps1")):
        if not f.name.startswith("."):
            rows.append(f"{f.name:<19}{DIM}{describe(f)[:40]}{R}")
    km = HOME / "AppData/Local/Microsoft/WindowsApps/km.cmd"
    if km.exists():
        rows.append(f"{'km':<19}{DIM}{describe(km)[:40]}{R}")
    for f in sorted(CLAUDE.glob("*.md")):
        if f.name != "MEMORY.md":
            rows.append(f"{MAG}{f.name:<19}{R}{DIM}note{R}")
    return rows


# ----------------------------------------------------------------- frame

def build(cache: Cache, width: int, live: bool) -> list:
    usage = cache.get("usage", 60.0, scan_transcripts) or {"sessions": [], "today_calls": 0, "today_cost": 0}
    mach = cache.get("machine", 15.0, machine) or {}
    repos = cache.get("repos", 60.0, repo_rows) or []
    tools = cache.get("tools", 120.0, tool_rows) or []

    dot = f"{GREEN}*{R} {DIM}live{R}" if live else f"{DIM}snapshot{R}"
    head = (f" {BOLD}U T I L I T Y   B E L T{R}"
            f"{' ' * max(1, width - 46)}{DIM}{datetime.now():%a %d %b %H:%M:%S}{R}  {dot}")

    narrow = width < 84
    cw = width - 4 if narrow else (width - 5) // 2

    cl = panel("CLAUDE", claude_rows(usage, cw), cw, 4)
    mc = panel("MACHINE", machine_rows(mach, cw - 4), cw, 8)
    rp = panel("REPOS", repos, cw, max(2, len(repos)))
    pr = panel("RUNNING", proc_rows(mach), cw, max(2, len(repos)))

    out = [head, ""]
    if narrow:
        for block in (cl, mc, rp, pr):
            out += [" " + l for l in block] + [""]
    else:
        out += columns(cl, mc) + [""] + columns(rp, pr) + [""]

    out += [f" {BOLD}TOOLS{R}  {DIM}{CLAUDE}{R}"]
    out += [f"   {r}" for r in tools]
    out += ["", f" {DIM}costs are API list rates, not your bill · /usage is "
                f"authoritative{'  ·  Ctrl+C to exit' if live else ''}{R}"]
    return out


def main() -> int:
    setup()
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="one frame, then exit")
    ap.add_argument("--tick", type=float, default=1.0, help="seconds per repaint")
    args = ap.parse_args()

    cache = Cache()
    if args.once:
        print("\n".join(build(cache, shutil.get_terminal_size((100, 40)).columns, False)))
        return 0

    painted = 0
    sys.stdout.write("\033[2J\033[?25l")
    try:
        while True:
            w = shutil.get_terminal_size((100, 40)).columns
            painted = paint(build(cache, w, True), painted)
            time.sleep(args.tick)
    except KeyboardInterrupt:
        return 0
    finally:
        sys.stdout.write("\033[?25h\n")
        sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
