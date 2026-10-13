"""Signals the findings rules need that the rest of the dashboard does not:
caches stranded on C:, the screen resolution, Sunshine's log times. Called from
the Sampler's threads (these do I/O; rules never do)."""
from __future__ import annotations

import os
import re
import time
from datetime import datetime
from pathlib import Path


def _env(name: str) -> str:
    """A user environment variable as Windows has it now (the registry), falling
    back to this process's copy, which is stale if it was set after we started."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return os.path.expandvars(str(winreg.QueryValueEx(key, name)[0]))
    except (OSError, ImportError):
        return os.environ.get(name, "")


def _folder_size(path: Path, limit: float = 120.0) -> int:
    """Bytes under a folder (gives up after `limit` seconds: big is big enough)."""
    total, stack, start = 0, [str(path)], time.time()
    while stack and time.time() - start < limit:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def stranded_caches(min_size: int = 500 * 1024 ** 2) -> list[dict]:
    """Caches whose env var now points somewhere else, but whose old default
    folder is still there and still big. Seen 2026-10-12: UV_CACHE_DIR moved to
    F:, 8.7 GB left on C: that `uv cache clean` could no longer reach. The Claude
    app's sandbox keeps its own copy of the default too, so look there as well."""
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    sandboxes = list((local / "Packages").glob("Claude_*/LocalCache/Local"))
    known = {
        "UV_CACHE_DIR": [Path("uv") / "cache"],
        "PIP_CACHE_DIR": [Path("pip") / "Cache"],
    }
    out = []
    for var, subs in known.items():
        moved = _env(var)
        if not moved:
            continue
        for base in [local, *sandboxes]:
            for sub in subs:
                old = base / sub
                if old.is_dir() and old.resolve() != Path(moved).resolve():
                    size = _folder_size(old)
                    if size >= min_size:
                        out.append({"var": var, "path": str(old), "size": size, "now_at": moved})
    moved = _env("OLLAMA_MODELS")
    old = Path.home() / ".ollama" / "models"
    if moved and old.is_dir() and old.resolve() != Path(moved).resolve():
        size = _folder_size(old)
        if size >= min_size:
            out.append({"var": "OLLAMA_MODELS", "path": str(old), "size": size, "now_at": moved})
    return out


def screen_size() -> str:
    """The main display's resolution, e.g. '5120x1440' ('' off Windows)."""
    try:
        import ctypes
        u32 = ctypes.windll.user32
        return f"{u32.GetSystemMetrics(0)}x{u32.GetSystemMetrics(1)}"
    except (AttributeError, OSError):
        return ""


def log_time(line: str) -> float:
    """The time a log line starts with ('[2026-10-12 13:15:02.123]: ...'), or 0."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})", line[:40])
    if not m:
        return 0.0
    try:
        return datetime.fromisoformat(f"{m.group(1)} {m.group(2)}").timestamp()
    except ValueError:
        return 0.0
