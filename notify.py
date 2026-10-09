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

The second rule is "silent while you are looking" (the "mute" setting): by default only the chat you are in
stays quiet while Claude is in front; every other chat's sounds still play (focused_chat() reads the Claude
app's own record of which chat you clicked into last).

Never raises. A hook that throws is worse than a hook that is silent.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
STAMP = HERE / ".turn-start"         # before 2026-10-09: one start time for every chat (still read as a fallback)
STAMPS = HERE / ".turn-starts.json"  # {session_id: start time}: each chat's own turn
SETTINGS = HERE / "sounds.json"      # edited with `python sounds.py`; shared through git
LOCAL = HERE / "sounds.local.json"   # this computer only (volume); not in git

# Turns shorter than this stay silent - you were still watching the screen.
QUIET_UNDER_S = 12.0
STAMP_DAYS = 2                       # a chat's start time is forgotten after this many days

# When a sound stays silent because you are looking:
#   "chat"   only for the chat you are in (the one the Claude app focused last) while Claude is in front;
#            every other chat still plays. Falls back to "app" when the app's chat records cannot be read.
#   "app"    for every chat while Claude is in front (the rule before 2026-10-09)
#   "never"  always play
MUTE_MODES = ("chat", "app", "never")

# What plays when sounds.json is missing or unreadable. Each event names a .wav in
# this folder, or "off".
DEFAULTS = {
    "quiet_under_seconds": QUIET_UNDER_S,
    "mute": "chat",
    "mute_when_claude_focused": True,  # the older on/off switch, kept so older copies of notify.py still read it
    "volume": 1.0,                   # 1 = as made; 2 = twice as loud (each sound stops at its clean maximum)
    "events": {"Stop": "done", "Notification": "attention", "PostToolUseFailure": "error",
               "PermissionDenied": "denied", "SubagentStop": "subagent", "PreCompact": "compact"},
}


def settings() -> dict:
    """DEFAULTS overlaid with sounds.json, then sounds.local.json; a bad or missing file changes nothing.
    A file with the older switch only ("mute_when_claude_focused") and no "mute": true -> "chat", false -> "never"."""
    cfg = json.loads(json.dumps(DEFAULTS))
    mode = None
    for f in (SETTINGS, LOCAL):
        try:
            user = json.loads(f.read_text(encoding="utf-8"))
            cfg["events"].update({k: v for k, v in (user.get("events") or {}).items() if isinstance(v, str)})
            for key in ("quiet_under_seconds", "mute_when_claude_focused", "volume"):
                if key in user:
                    cfg[key] = user[key]
            if user.get("mute") in MUTE_MODES:
                mode = user["mute"]
        except (OSError, ValueError, AttributeError, TypeError):
            pass
    cfg["mute"] = mode or ("chat" if cfg.get("mute_when_claude_focused", True) else "never")
    return cfg


def _read_stamps() -> dict:
    try:
        stamps = json.loads(STAMPS.read_text(encoding="utf-8"))
        return stamps if isinstance(stamps, dict) else {}
    except (OSError, ValueError):
        return {}


def record_start(session_id: str | None, now: float | None = None) -> None:
    """A turn starts in this chat: its time in STAMPS (old ones dropped) and in STAMP."""
    now = time.time() if now is None else now
    try:
        STAMP.write_text(str(now), encoding="utf-8")
    except OSError:
        pass
    if not session_id:
        return
    stamps = {k: v for k, v in _read_stamps().items()
              if isinstance(v, (int, float)) and now - v < STAMP_DAYS * 86400}
    stamps[session_id] = now
    try:
        tmp = STAMPS.with_name(f"{STAMPS.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(stamps), encoding="utf-8")
        os.replace(tmp, STAMPS)
    except OSError:
        pass


def turn_start(session_id: str | None) -> float | None:
    """When this chat's turn started: its own time, else the shared one; None when unknown."""
    if session_id:
        got = _read_stamps().get(session_id)
        if isinstance(got, (int, float)):
            return float(got)
    try:
        return float(STAMP.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---- which chat you are in: the Claude desktop app's own record of each chat
#
# The app keeps one small JSON per chat (claude-code-sessions/<account>/<org>/local_<id>.json) with
# "cliSessionId" (the session_id a hook gets) and "lastFocusedAt" (ms, set when you click into that chat,
# in the main window or a split-view pane). The chat with the newest lastFocusedAt is the one you are in.
# This is the app's private storage, not an API: when it cannot be read, "chat" falls back to "app".

def _chat_record_dirs() -> list:
    dirs = []
    if sys.platform == "win32":
        local, roaming = os.environ.get("LOCALAPPDATA"), os.environ.get("APPDATA")
        if local:   # the Microsoft Store app keeps its data in its package folder
            dirs += sorted(Path(local, "Packages").glob("Claude_*/LocalCache/Roaming/Claude/claude-code-sessions"))
        if roaming:
            dirs.append(Path(roaming, "Claude", "claude-code-sessions"))
    elif sys.platform == "darwin":
        dirs.append(Path.home() / "Library" / "Application Support" / "Claude" / "claude-code-sessions")
    return [d for d in dirs if d.is_dir()]


def focused_chat(dirs: list | None = None, newest: int = 12) -> str | None:
    """session_id (cliSessionId) of the chat focused last in the Claude app; None when unknown. Reads only the
    `newest` most recently written records: focusing a chat rewrites its record."""
    try:
        files = [f for d in (_chat_record_dirs() if dirs is None else dirs) for f in d.glob("*/*/local_*.json")]
        files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        best, best_at = None, -1.0
        for f in files[:newest]:
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            at = rec.get("lastFocusedAt") if isinstance(rec, dict) else None
            if isinstance(at, (int, float)) and at > best_at and rec.get("cliSessionId"):
                best, best_at = str(rec["cliSessionId"]), float(at)
        return best
    except Exception:
        return None


def louder(path: Path, volume: float) -> bytes | None:
    """The 16-bit .wav scaled by volume, never past full scale, as WAV bytes; None = play the file as is."""
    import array
    import io
    import wave
    try:
        with wave.open(str(path)) as w:
            params, frames = w.getparams(), w.readframes(w.getnframes())
        if params.sampwidth != 2 or sys.byteorder != "little":
            return None
        a = array.array("h", frames)
        gain = min(float(volume), 32767 / max(1, max(abs(x) for x in a)))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as out:
            out.setparams(params)
            out.writeframes(array.array("h", (int(x * gain) for x in a)).tobytes())
        return buf.getvalue()
    except Exception:
        return None

# Tools whose failure is routine and not worth a sound.
BORING_FAILURES = {"Read", "Glob", "Grep", "TodoWrite"}

# "Claude is in front": the foreground app is one of these (Windows: the exe's name; macOS: the app's
# name, lowercased). Which chat you are in comes from focused_chat() (the "mute" setting).
MUTE_WHEN_FOCUSED = {"claude.exe", "claude"}

# Sounds that play even when the app is focused. Empty by default.
ALWAYS_PLAY: set = set()


def foreground_exe() -> str:
    """Basename of the foreground window's process, lowercased (macOS: the front app's name). '' if unknown."""
    if sys.platform == "darwin":
        try:            # lsappinfo needs no Accessibility or Automation permission (System Events would ask)
            import subprocess
            front = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True, timeout=3).stdout.strip()
            info = subprocess.run(["lsappinfo", "info", "-only", "name", front],
                                  capture_output=True, text=True, timeout=3).stdout
            return info.rsplit("=", 1)[-1].strip().strip('"').lower() if "=" in info else ""
        except Exception:
            return ""
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
        record_start(data.get("session_id"))
        return None

    if event == "Stop":
        started = turn_start(data.get("session_id"))
        elapsed = quiet + 1 if started is None else time.time() - started   # unknown: assume worth hearing
        return None if elapsed < quiet else sound

    if event == "PostToolUseFailure":
        # A failed Read while exploring is not news; a failed Bash or Edit is.
        return None if data.get("tool_name") in BORING_FAILURES else sound

    return sound


def silenced(mode: str, sound: str, data: dict) -> bool:
    """True when you are looking, by the "mute" mode: Claude is in front and ("app") it is any chat, or
    ("chat") it is the chat you are in. "chat" with no session_id or no readable chat records acts as "app"."""
    if mode == "never" or sound in ALWAYS_PLAY or foreground_exe() not in MUTE_WHEN_FOCUSED:
        return False
    if mode != "chat":
        return True
    mine, focused = data.get("session_id"), focused_chat()
    return not mine or focused is None or focused == mine


def play(name: str, volume: float = 1.0) -> None:
    path = HERE / f"{name}.wav"
    if not path.exists():
        return
    if sys.platform == "win32":
        import winsound
        data = louder(path, volume) if volume != 1.0 else None
        # Blocking on purpose: the process exiting would cut an async sound
        # short. The hook itself is marked async, so Claude is not waiting.
        if data:
            winsound.PlaySound(data, winsound.SND_MEMORY)
        else:
            winsound.PlaySound(str(path), winsound.SND_FILENAME)
    else:
        import subprocess
        for player in (["afplay", "-v", str(volume), str(path)], ["aplay", "-q", str(path)]):
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
        if not silenced(cfg.get("mute", "chat"), sound, data if isinstance(data, dict) else {}):
            play(sound, float(cfg.get("volume", 1.0)))
    except Exception:
        pass          # a crashing hook is worse than a silent one
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
