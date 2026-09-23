#!/usr/bin/env python
"""What is eating the disk, and what grew since last time.

    python disk-sentinel.py              # drives + biggest folders
    python disk-sentinel.py --snapshot   # also record sizes for next comparison
    python disk-sentinel.py --root F:/   # scan somewhere else
    python disk-sentinel.py --depth 3    # look deeper (slower)

"Biggest" alone is not actionable - Windows and Program Files are always big
and you are not deleting them. Growth is actionable: a folder that gained 8 GB
this week is doing something you probably did not intend. So this keeps a
snapshot in ~/.claude/.disk-snapshot.json and diffs against it.

Read-only. It never deletes anything.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

SNAP = Path.home() / ".claude" / ".disk-snapshot.json"

# Scanned by default: where user-generated bloat actually accumulates.
DEFAULT_ROOTS = [
    Path.home(),
    Path(os.environ.get("LOCALAPPDATA", "")) if os.environ.get("LOCALAPPDATA") else None,
    Path(os.environ.get("ProgramData", "")) if os.environ.get("ProgramData") else None,
]

# Never descend into these - huge, system-owned, and not yours to clear.
SKIP = {"windows", "$recycle.bin", "system volume information", "winsxs",
        "node_modules/.cache", "onedrivetemp"}

R, DIM, BOLD = "\033[0m", "\033[2m", "\033[1m"
GREEN, YELLOW, RED, CYAN = "\033[32m", "\033[33m", "\033[31m", "\033[36m"


def setup() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if sys.platform == "win32":
        import ctypes
        k = ctypes.windll.kernel32
        k.SetConsoleMode(k.GetStdHandle(-11), 7)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} PB"


def bar(frac: float, width: int = 16) -> str:
    frac = max(0.0, min(1.0, frac))
    n = int(frac * width)
    col = GREEN if frac < .8 else (YELLOW if frac < .93 else RED)
    return f"{col}{'#' * n}{R}{DIM}{'-' * (width - n)}{R}"


def dir_size(path: Path) -> int:
    """Bytes under path. Permission errors are skipped, not raised - a scan
    that dies on one locked folder is useless."""
    total = 0
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name.lower() in SKIP:
                                continue
                            stack.append(Path(entry.path))
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            continue
    return total


def drives() -> list:
    if sys.platform != "win32":
        return []
    import ctypes
    out = []
    bits = ctypes.windll.kernel32.GetLogicalDrives()
    for i in range(26):
        if not bits & (1 << i):
            continue
        letter = f"{chr(65 + i)}:\\"
        free, total = ctypes.c_ulonglong(), ctypes.c_ulonglong()
        ok = ctypes.windll.kernel32.GetDiskFreeSpaceExW(
            ctypes.c_wchar_p(letter), None, ctypes.byref(total), ctypes.byref(free))
        if ok and total.value:
            out.append((letter, free.value, total.value))
    return out


def scan(roots: list, depth: int) -> dict:
    sizes: dict = {}
    for root in roots:
        if not root or not root.exists():
            continue
        try:
            entries = [e for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
        except (OSError, PermissionError):
            continue
        for e in entries:
            if e.name.lower() in SKIP:
                continue
            p = Path(e.path)
            sizes[str(p)] = dir_size(p)
            if depth > 2:
                try:
                    for sub in os.scandir(p):
                        if sub.is_dir(follow_symlinks=False) and sub.name.lower() not in SKIP:
                            sizes[sub.path] = dir_size(Path(sub.path))
                except (OSError, PermissionError):
                    pass
    return sizes


def main() -> int:
    setup()
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", action="append", help="scan this path (repeatable)")
    ap.add_argument("--depth", type=int, default=2)
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--snapshot", action="store_true",
                    help="record sizes so the next run can show growth")
    args = ap.parse_args()

    print(f"\n {BOLD}DRIVES{R}")
    warn = []
    for letter, free, total in drives():
        used = (total - free) / total
        print(f"   {letter:<4} {bar(used)} {human(free)} free of {human(total)} "
              f"{DIM}{used*100:.0f}% used{R}")
        if used > .9 and total > 50 * 1024 ** 3:
            warn.append((letter, free, used))

    roots = [Path(r) for r in args.root] if args.root else DEFAULT_ROOTS
    shown = [str(r) for r in roots if r and r.exists()]
    print(f"\n {BOLD}SCANNING{R}  {DIM}depth {args.depth} · "
          f"{', '.join(shown) or 'nothing'}{R}")
    sizes = scan(roots, args.depth)
    if not sizes:
        print(f"   {DIM}nothing readable{R}")
        return 1

    old = {}
    if SNAP.exists():
        try:
            old = json.loads(SNAP.read_text(encoding="utf-8")).get("sizes", {})
        except (OSError, json.JSONDecodeError):
            old = {}

    top = sorted(sizes.items(), key=lambda kv: -kv[1])[:args.top]
    print(f"\n {BOLD}BIGGEST{R}")
    for path, size in top:
        delta = size - old.get(path, size)
        if delta > 100 * 1024 ** 2:
            tag = f"  {RED}+{human(delta)}{R}"
        elif delta < -100 * 1024 ** 2:
            tag = f"  {GREEN}{human(delta)}{R}"
        else:
            tag = ""
        name = path if len(path) <= 52 else "..." + path[-49:]
        print(f"   {human(size):>11}  {name}{tag}")

    if old:
        grew = sorted(((p, s - old.get(p, s)) for p, s in sizes.items()),
                      key=lambda kv: -kv[1])
        grew = [g for g in grew if g[1] > 50 * 1024 ** 2][:8]
        if grew:
            print(f"\n {BOLD}GREW SINCE LAST SNAPSHOT{R}")
            for path, d in grew:
                name = path if len(path) <= 52 else "..." + path[-49:]
                print(f"   {RED}+{human(d):>10}{R}  {name}")
        else:
            print(f"\n {DIM}nothing grew more than 50 MB since last snapshot{R}")
    else:
        print(f"\n {DIM}no snapshot yet — run with --snapshot to enable "
              f"growth tracking{R}")

    for letter, free, used in warn:
        print(f"\n {RED}{letter} is {used*100:.0f}% full{R} — {human(free)} left")

    if args.snapshot:
        SNAP.write_text(json.dumps(
            {"at": datetime.now().isoformat(timespec="seconds"), "sizes": sizes},
            indent=1), encoding="utf-8")
        print(f"\n {DIM}snapshot written to {SNAP}{R}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
