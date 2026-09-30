#!/usr/bin/env python
"""Choose which sound each Claude Code event plays, or none. Saves sounds.json beside notify.py.

    python sounds.py             the menu
    python sounds.py --install   point this computer's Claude Code hooks at this folder's notify.py,
                                 and its status line at statusline.ps1. Creates ~/.claude/settings.json
                                 and sounds.json if they are missing; an existing settings.json is backed
                                 up first and its other hooks and settings are kept.

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
        print(f" 9. volume on this computer        {cfg['volume']:g}  (1 = as made, up to 4)")
        c = input("\n number to change, p<number> to hear one, s to save, q to quit: ").strip().lower()
        if c == "q":
            return
        if c == "s":
            shared = {k: v for k, v in cfg.items() if k != "volume"}
            notify.SETTINGS.write_text(json.dumps(shared, indent=2) + "\n", encoding="utf-8")
            notify.LOCAL.write_text(json.dumps({"volume": cfg["volume"]}, indent=2) + "\n", encoding="utf-8")
            print(f" saved: {notify.SETTINGS.name} (commit and push it to share) and this computer's volume")
        elif c.startswith("p") and c[1:].isdigit() and 1 <= int(c[1:]) <= len(rows):
            snd = cfg["events"].get(rows[int(c[1:]) - 1])
            print(" (off)") if snd in (None, "", "off") else notify.play(snd, float(cfg["volume"]))
        elif c == "9":
            got = input("   volume (0.2 to 4): ").strip()
            try:
                cfg["volume"] = min(4.0, max(0.2, float(got)))
            except ValueError:
                pass
        elif c.isdigit() and 1 <= int(c) <= len(rows):
            ev = rows[int(c) - 1]
            cfg["events"][ev] = pick(names, cfg["events"].get(ev) or "off")
        elif c == "7":
            got = input("   seconds: ").strip()
            cfg["quiet_under_seconds"] = float(got) if got.replace(".", "", 1).isdigit() else cfg["quiet_under_seconds"]
        elif c == "8":
            cfg["mute_when_claude_focused"] = not cfg["mute_when_claude_focused"]


def install(path: Path = Path.home() / ".claude" / "settings.json") -> None:
    try:
        s = json.loads(path.read_text(encoding="utf-8") or "{}") if path.exists() else {}
        if not isinstance(s, dict):
            raise ValueError("top level is not an object")
    except ValueError as exc:                            # never overwrite a file we cannot read
        sys.exit(f"{path} is not valid JSON ({exc}); fix or move it, then run this again. Nothing was changed.")
    if path.exists():
        shutil.copy2(path, path.with_name(f"settings.json.bak-{time.strftime('%Y%m%d-%H%M%S')}"))
    else:
        print(f"no {path} yet - creating it")
    if not notify.SETTINGS.exists():                     # the sound choices; notify.py falls back without it
        shared = {k: v for k, v in notify.DEFAULTS.items() if k != "volume"}
        notify.SETTINGS.write_text(json.dumps(shared, indent=2) + "\n", encoding="utf-8")
        print(f"created {notify.SETTINGS.name} with the default sounds (change them with: python sounds.py)")
    hooks, script = s.setdefault("hooks", {}), (notify.HERE / "notify.py").as_posix()
    for ev in ["UserPromptSubmit", *EVENTS]:
        keep = [g for g in hooks.get(ev, []) if not any("notify.py" in (h.get("command") or "")
                                                        for h in g.get("hooks", []))]
        # the Python running this install, by full path: "python" alone is often not on PATH on a managed laptop
        exe = Path(sys.executable).as_posix()
        hook = {"type": "command", "command": f'"{exe}" "{script}" {ev}', "timeout": 5 if ev == "UserPromptSubmit" else 10}
        if ev != "UserPromptSubmit":
            hook["async"] = True
        hooks[ev] = keep + [{"hooks": [hook]}]
    # The status line: ours is replaced (so a moved folder is fixed), anyone else's is left alone.
    status = (notify.HERE / "statusline.ps1").as_posix()
    current = (s.get("statusLine") or {}).get("command") or ""
    if not current or "statusline.ps1" in current:
        s["statusLine"] = {"type": "command", "command": f"powershell -NoProfile -File {status}", "padding": 1}
        print(f"status line now runs {status}")
    else:
        print(f"kept your existing status line ({current[:60]}); to use this one instead, point it at {status}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(s, indent=2) + "\n", encoding="utf-8")
    print(f"hooks in {path} now call {script}; restart Claude Code to pick them up")


if __name__ == "__main__":
    install() if "--install" in sys.argv else menu()
