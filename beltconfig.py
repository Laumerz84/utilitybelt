"""UtilityBelt's settings file: %APPDATA%\\UtilityBelt\\config.toml.

Created with the defaults (and comments) on first run. Missing keys fall back to the
defaults, unknown keys are ignored with a note, and a file that cannot be read falls
back to the defaults entirely: the strip must always start.

Only the [mutes] table is ever written back, and only that table's lines are touched,
so hand edits and comments elsewhere survive.
"""
from __future__ import annotations

import copy
import os
import re
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

DIR = Path(os.environ.get("APPDATA") or Path.home()) / "UtilityBelt"
PATH = DIR / "config.toml"
HISTORY = DIR / "findings-history.jsonl"

DEFAULTS = {
    "actions": True,
    "sound_on_critical": True,
    "history_entries": 50,
    "rules": {"agents": True, "hygiene": True, "models": False, "remote": False},
    "thresholds": {
        "disk_floor_gb": 20,
        "disk_trend_minutes": 60,
        "core_hog_hours": 4,
        "spend_today_usd": 100,
        "unattended_minutes": 30,
        "approval_minutes": 5,
    },
    "quiet_row": {"items": ["agents", "vram", "spend", "disk", "clock"]},
    "remote": {"sunshine_log": r"C:\Program Files\Sunshine\config\sunshine.log"},
    "mutes": {"hygiene.stranded_cache": "forever"},
}

TEMPLATE = """\
# UtilityBelt settings. Delete this file to get the defaults back.
# Top-level keys must come before the first [table].

actions = true               # false: the strip only reports, its buttons never act
sound_on_critical = true     # a sound once when something critical appears
history_entries = 50         # cleared findings kept for "what happened while I was out"; 0 = none

[rules]                      # which groups of checks run
agents = true                # Claude chats waiting on you, stuck, or spending a lot
hygiene = true               # disk space, runaway and leftover processes, stranded caches
models = false               # Ollama models crowding the graphics card
remote = false               # Sunshine streams

[thresholds]
disk_floor_gb = 20           # warn when a drive has less free than this
disk_trend_minutes = 60      # warn when a drive will reach that floor within this
core_hog_hours = 4           # a process using a whole core for this long
spend_today_usd = 100        # Claude spend today
unattended_minutes = 30      # a chat waiting on you for this long
approval_minutes = 5         # a chat silent on a tool call (likely a permission prompt) this long

[quiet_row]                  # what the strip shows when nothing is wrong, in order
items = ["agents", "vram", "spend", "disk", "clock"]
# also available: "cpu", "gpu", "mem", "net", "gputemp"

[remote]
sunshine_log = 'C:\\Program Files\\Sunshine\\config\\sunshine.log'

[mutes]
# "finding.key" = "forever" | "until 2026-10-12 19:30" | "reboot 2026-10-12 08:01"
# (written by the strip's mute buttons; a reboot mute ends when the PC restarts)
"hygiene.stranded_cache" = "forever"
"""


@dataclass
class Config:
    actions: bool = True
    sound_on_critical: bool = True
    history_entries: int = 50
    rules: dict = field(default_factory=dict)
    thresholds: dict = field(default_factory=dict)
    quiet_row: list = field(default_factory=list)
    remote: dict = field(default_factory=dict)
    mutes: dict = field(default_factory=dict)
    path: Path = PATH
    error: str = ""              # why the file was not used, if it was not
    notes: list = field(default_factory=list)   # unknown keys etc.

    def t(self, name: str) -> float:
        """A threshold, falling back to its default."""
        return self.thresholds.get(name, DEFAULTS["thresholds"][name])


def _merge(raw: dict, notes: list) -> dict:
    out = copy.deepcopy(DEFAULTS)
    for key, value in raw.items():
        if key not in DEFAULTS:
            notes.append(f"config: unknown key {key!r} ignored")
            continue
        default = DEFAULTS[key]
        if isinstance(default, dict):
            if not isinstance(value, dict):
                notes.append(f"config: [{key}] should be a table; default used")
                continue
            if key == "mutes":
                out[key] = {str(k): str(v) for k, v in value.items()}
                continue
            for k, v in value.items():
                if k not in default:
                    notes.append(f"config: unknown key {key}.{k} ignored")
                elif type(v) is not type(default[k]) and not (
                        isinstance(v, (int, float)) and isinstance(default[k], (int, float))):
                    notes.append(f"config: {key}.{k} has the wrong type; default used")
                else:
                    out[key][k] = v
        elif type(value) is not type(default):
            notes.append(f"config: {key} has the wrong type; default used")
        else:
            out[key] = value
    return out


def load(path: Path = PATH) -> Config:
    """Read the settings, creating the file with the defaults if it is missing."""
    notes: list = []
    error = ""
    raw: dict = {}
    try:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(TEMPLATE, encoding="utf-8")
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:               # TOMLDecodeError is a ValueError
        error, raw = f"{type(exc).__name__}: {exc}", {}
    merged = _merge(raw, notes) if not error else copy.deepcopy(DEFAULTS)
    return Config(actions=merged["actions"], sound_on_critical=merged["sound_on_critical"],
                  history_entries=int(merged["history_entries"]), rules=merged["rules"],
                  thresholds=merged["thresholds"], quiet_row=list(merged["quiet_row"]["items"]),
                  remote=merged["remote"], mutes=merged["mutes"], path=path, error=error, notes=notes)


# ------------------------------------------------------------------- mutes

STAMP = "%Y-%m-%d %H:%M"


def mute_value(how: str, boot: float, now: float | None = None) -> str:
    """The [mutes] value for '1h', 'reboot' or 'forever'."""
    now = time.time() if now is None else now
    if how == "1h":
        return "until " + datetime.fromtimestamp(now + 3600).strftime(STAMP)
    if how == "reboot":
        return "reboot " + datetime.fromtimestamp(boot).strftime(STAMP)
    return "forever"


def mute_until(value: str, boot: float) -> float | None:
    """When a mute ends (epoch), inf for forever, or None if it has no effect
    (expired, a reboot mute from an earlier boot, or unreadable)."""
    value = value.strip()
    if value == "forever":
        return float("inf")
    kind, _, stamp = value.partition(" ")
    try:
        when = datetime.strptime(stamp.strip(), STAMP).timestamp()
    except ValueError:
        return None
    if kind == "until":
        return when if when > time.time() else None
    if kind == "reboot":
        same_boot = datetime.fromtimestamp(boot).strftime(STAMP) == stamp.strip()
        return float("inf") if same_boot else None
    return None


def write_mutes(mutes: dict, boot: float, path: Path = PATH) -> None:
    """Rewrite only the [mutes] table: the rest of the file is left as it is.
    Mutes that no longer have any effect are dropped."""
    keep = {k: v for k, v in mutes.items() if mute_until(v, boot) is not None}
    body = "".join(f'"{k}" = "{v}"\n' for k, v in sorted(keep.items()))
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = TEMPLATE
    lines = text.splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if re.match(r"\s*\[mutes\]\s*(#.*)?$", ln)), None)
    if start is None:
        new = text + ("" if text.endswith("\n") else "\n") + "\n[mutes]\n" + body
    else:
        end = next((i for i in range(start + 1, len(lines)) if re.match(r"\s*\[", lines[i])), len(lines))
        comments = [ln for ln in lines[start + 1:end] if ln.lstrip().startswith("#")]
        new = "".join(lines[:start + 1] + comments) + body + "".join(lines[end:])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8")
