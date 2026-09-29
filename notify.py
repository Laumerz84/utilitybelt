#!/usr/bin/env python
"""Sound dispatcher for Claude Code hooks - decides what to play, and whether.

Every hook points here instead of at a fixed .wav:

    python notify.py <event>        # hook JSON arrives on stdin

The point is that a hook is not limited to one sound per event. The JSON on
stdin carries tool_name, tool_input, tool_response and more, so one script can
branch on what actually happened. The rules live in decide() below - edit
there, nothing else needs changing.

The rule that matters most is the silence rule. A chime on every three-second
turn is maddening and you quickly stop hearing it. A sound only helps when you
have actually context-switched away, so Stop stays silent unless the turn ran
longer than QUIET_UNDER_S. That needs a turn start time, which is why
UserPromptSubmit is wired up too - it records a timestamp and plays nothing.

Never raises. A hook that throws is worse than a hook that is silent.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
STAMP = HERE / ".turn-start"
SETTINGS = HERE / "sounds.json"      # edited with `python sounds.py`; shared through git

# Turns shorter than this stay silent - you were still watching the screen.
QUIET_UNDER_S = 12.0

# What plays when sounds.json is missing or unreadable. Each event names a .wav in
# this folder, or "off".
DEFAULTS = {
    "quiet_under_seconds": QUIET_UNDER_S,
    "mute_when_claude_focused": True,
    "events": {"Stop": "done", "Notification": "attention", "PostToolUseFailure": "error",
               "PermissionDenied": "denied", "SubagentStop": "subagent", "PreCompact": "compact"},
}


def settings() -> dict:
    """DEFAULTS overlaid with sounds.json; a bad or missing file changes nothing."""
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        user = json.loads(SETTINGS.read_text(encoding="utf-8"))
        cfg["events"].update({k: v for k, v in (user.get("events") or {}).items() if isinstance(v, str)})
        for key in ("quiet_under_seconds", "mute_when_claude_focused"):
            if key in user:
                cfg[key] = user[key]
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return cfg

# Tools whose failure is routine and not worth a sound.
BORING_FAILURES = {"Read", "Glob", "Grep", "TodoWrite"}

# Stay silent while one of these is the foreground app - you can see the
# sidebar brighten, so a sound adds nothing. Set to an empty set to always
# play. Note this is app-level, not per-chat: the Claude window title is just
# "Claude", with no session identity in it, so "mute only the chat I am
# looking at" is not detectable without reading the UI tree on every turn.
MUTE_WHEN_FOCUSED = {"claude.exe"}

# Sounds that play even when the app is focused. Empty by default.
ALWAYS_PLAY: set = set()


def foreground_exe() -> str:
    """Basename of the foreground window's process, lowercased. '' if unknown."""
    if sys.platform != "win32":
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
        hwnd = u32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        # 0x1000 = PROCESS_QUERY_LIMITED_INFORMATION, works without elevation
        h = k32.OpenProcess(0x1000, False, pid.value)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(512)
            size = wintypes.DWORD(512)
            if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return ""
            return buf.value.rsplit("\\", 1)[-1].lower()
        finally:
            k32.CloseHandle(h)
    except Exception:
        return ""


def decide(event: str, data: dict, cfg: dict | None = None) -> str | None:
    """-> sound name, or None for silence. cfg is settings() (DEFAULTS when None)."""
    cfg = cfg or DEFAULTS
    quiet = float(cfg.get("quiet_under_seconds", QUIET_UNDER_S))
    sound = cfg["events"].get("PreCompact" if event == "PostCompact" else event)
    sound = None if not sound or sound == "off" else sound

    if event == "UserPromptSubmit":
        try:
            STAMP.write_text(str(time.time()), encoding="utf-8")
        except OSError:
            pass
        return None

    if event == "Stop":
        try:
            elapsed = time.time() - float(STAMP.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            elapsed = quiet + 1              # unknown: assume worth hearing
        return None if elapsed < quiet else sound

    if event == "PostToolUseFailure":
        # A failed Read while exploring is not news; a failed Bash or Edit is.
        return None if data.get("tool_name") in BORING_FAILURES else sound

    return sound


def play(name: str) -> None:
    path = HERE / f"{name}.wav"
    if not path.exists():
        return
    if sys.platform == "win32":
        import winsound
        # Blocking on purpose: the process exiting would cut an async sound
        # short. The hook itself is marked async, so Claude is not waiting.
        winsound.PlaySound(str(path), winsound.SND_FILENAME)
    else:
        import subprocess
        for player in (["afplay", str(path)], ["aplay", "-q", str(path)]):
            try:
                subprocess.run(player, timeout=10, check=True,
                               capture_output=True)
                return
            except Exception:
                continue


def main() -> int:
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    data: dict = {}
    try:
        if not sys.stdin.isatty():
            raw = sys.stdin.read()
            if raw.strip():
                data = json.loads(raw)
    except Exception:
        data = {}

    try:
        cfg = settings()
        sound = decide(event, data if isinstance(data, dict) else {}, cfg)
        if not sound:
            return 0
        # Suppression is separate from the decision so decide() stays pure
        # and testable, and so the timestamp side effects still happen.
        muted = MUTE_WHEN_FOCUSED if cfg.get("mute_when_claude_focused", True) else set()
        if sound not in ALWAYS_PLAY and foreground_exe() in muted:
            return 0
        play(sound)
    except Exception:
        pass          # a crashing hook is worse than a silent one
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
