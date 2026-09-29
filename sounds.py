#!/usr/bin/env python
"""Choose which sound each Claude Code event plays, or none. Saves sounds.json beside notify.py.

    python sounds.py             the menu
    python sounds.py --install   point this computer's Claude Code hooks at this folder's notify.py
                                 (~/.claude/settings.json is backed up first; other hooks are kept)

Share the settings between computers through git: commit and push sounds.json here, pull it there.
"""
import json
import shutil
import sys
import time
from pathlib import Path

import notify

EVENTS = {"Stop": "Claude finished a turn", "Notification": "Claude needs you",
          "PostToolUseFailure": "a tool failed", "PermissionDenied": "permission refused",
          "SubagentStop": "a subagent finished", "PreCompact": "context compacted"}


def wavs() -> list:
    return sorted(p.stem for p in notify.HERE.glob("*.wav"))


def pick(names: list, current: str) -> str:
    for i, n in enumerate(["off"] + names):
        print(f"   {i}. {n}{'  (now)' if n == current else ''}")
    got = input("   sound number (Enter keeps it): ").strip()
    return (["off"] + names)[int(got)] if got.isdigit() and int(got) <= len(names) else current


def menu() -> None:
    cfg, names = notify.settings(), wavs()
    rows = list(EVENTS)
    while True:
        print()
        for i, ev in enumerate(rows, 1):
            print(f" {i}. {EVENTS[ev]:<24} {cfg['events'].get(ev) or 'off'}")
        print(f" 7. quiet for turns shorter than   {cfg['quiet_under_seconds']:g} s")
        print(f" 8. silent while Claude is in front {'yes' if cfg['mute_when_claude_focused'] else 'no'}")
        c = input("\n number to change, p<number> to hear one, s to save, q to quit: ").strip().lower()
        if c == "q":
            return
        if c == "s":
            notify.SETTINGS.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
            print(f" saved {notify.SETTINGS.name}: commit and push it to share")
        elif c.startswith("p") and c[1:].isdigit() and 1 <= int(c[1:]) <= len(rows):
            snd = cfg["events"].get(rows[int(c[1:]) - 1])
            print(" (off)") if snd in (None, "", "off") else notify.play(snd)
        elif c.isdigit() and 1 <= int(c) <= len(rows):
            ev = rows[int(c) - 1]
            cfg["events"][ev] = pick(names, cfg["events"].get(ev) or "off")
        elif c == "7":
            got = input("   seconds: ").strip()
            cfg["quiet_under_seconds"] = float(got) if got.replace(".", "", 1).isdigit() else cfg["quiet_under_seconds"]
        elif c == "8":
            cfg["mute_when_claude_focused"] = not cfg["mute_when_claude_focused"]


def install(path: Path = Path.home() / ".claude" / "settings.json") -> None:
    s = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if path.exists():
        shutil.copy2(path, path.with_name(f"settings.json.bak-{time.strftime('%Y%m%d-%H%M%S')}"))
    hooks, script = s.setdefault("hooks", {}), (notify.HERE / "notify.py").as_posix()
    for ev in ["UserPromptSubmit", *EVENTS]:
        keep = [g for g in hooks.get(ev, []) if not any("notify.py" in (h.get("command") or "")
                                                        for h in g.get("hooks", []))]
        hook = {"type": "command", "command": f'python "{script}" {ev}', "timeout": 5 if ev == "UserPromptSubmit" else 10}
        if ev != "UserPromptSubmit":
            hook["async"] = True
        hooks[ev] = keep + [{"hooks": [hook]}]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(s, indent=2) + "\n", encoding="utf-8")
    print(f"hooks in {path} now call {script}; restart Claude Code to pick them up")


if __name__ == "__main__":
    install() if "--install" in sys.argv else menu()
