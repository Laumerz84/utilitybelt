"""Is there a newer UtilityBelt on GitHub? Standard library only.

UtilityBelt is installed with `git clone`, so a copy is out of date when the branch it
tracks on GitHub has commits it doesn't. check() fetches quietly (never asks for a password),
counts those commits and returns what to show; belt.py runs it in the background at start
and every few hours. Anything unusual - no git, not a clone, offline, a detached HEAD - gives
None, so the dashboard simply shows nothing.

    python update_check.py      # print the result for this copy
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
FETCH_TIMEOUT = 30  # seconds; a slow network just means "no answer this time"
NEW_TITLES = 5  # how many of the newest commit titles to show


@dataclass
class UpdateInfo:
    behind: int  # commits on GitHub that this copy doesn't have (0 = up to date)
    titles: list[str] = field(default_factory=list)  # newest first
    deps_changed: bool = False  # requirements.txt changed -> pip install needed too
    folder: str = ""

    @property
    def available(self) -> bool:
        return self.behind > 0

    @property
    def pull_command(self) -> str:
        return f'git -C "{self.folder}" pull'

    @property
    def pip_command(self) -> str:
        return f'python -m pip install -r "{Path(self.folder) / "requirements.txt"}"'

    def commands(self) -> list[str]:
        return [self.pull_command] + ([self.pip_command] if self.deps_changed else [])


def _git(folder: Path, *args: str, timeout: float = 15) -> str | None:
    """stdout of `git -C folder args`, or None on any failure. Never prompts for credentials."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    try:
        done = subprocess.run(
            ["git", "-C", str(folder), *args], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, env=env, stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def check(folder: str | Path = HERE, fetch: bool = True) -> UpdateInfo | None:
    """Compare this copy with the branch it tracks on GitHub. None = can't tell."""
    folder = Path(folder)
    if shutil.which("git") is None or not (folder / ".git").exists():
        return None
    upstream = (_git(folder, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}") or "").strip()
    if not upstream or "/" not in upstream:
        return None  # detached HEAD or a branch that tracks nothing
    remote, branch = upstream.split("/", 1)
    if fetch and _git(folder, "fetch", "--quiet", remote, branch, timeout=FETCH_TIMEOUT) is None:
        return None  # offline, repo gone private, ...
    count = (_git(folder, "rev-list", "--count", f"HEAD..{upstream}") or "").strip()
    if not count.isdigit():
        return None
    info = UpdateInfo(behind=int(count), folder=str(folder))
    if info.behind:
        log = _git(folder, "log", "--format=%s", f"-n{NEW_TITLES}", f"HEAD..{upstream}") or ""
        info.titles = [line.strip() for line in log.splitlines() if line.strip()]
        changed = _git(folder, "diff", "--name-only", f"HEAD...{upstream}") or ""
        info.deps_changed = "requirements.txt" in {line.strip() for line in changed.splitlines()}
    return info


def main() -> int:
    info = check()
    if info is None:
        print("Can't check for updates here (no git, not a git clone, or offline).")
        return 1
    if not info.available:
        print("UtilityBelt is up to date.")
        return 0
    print(f"Update available: {info.behind} new change{'s' if info.behind != 1 else ''} on GitHub.")
    for title in info.titles:
        print(f"  - {title}")
    print("\nTo update, close UtilityBelt and run:")
    for cmd in info.commands():
        print(f"  {cmd}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
