"""What a finding's button does. The fixed set of actions: rules pick from these,
config can only switch them all off (actions = false), never add one.

Each returns a function for Action.run. It runs later on a worker thread, never
while a rule is being evaluated, and raises with a readable message on failure.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import urllib.request
from pathlib import Path

import psutil


def end_processes(targets: list[tuple[int, float, str]]):
    """End each (pid, created, name). The creation time is checked first: a pid
    reused by some other program since the finding was made is left alone."""
    def run() -> None:
        failed = []
        for pid, created, name in targets:
            try:
                proc = psutil.Process(pid)
                if abs(proc.create_time() - created) > 1:
                    continue                                 # pid reused: not ours to end
                proc.terminate()
                try:
                    proc.wait(3)
                except psutil.TimeoutExpired:
                    proc.kill()
            except psutil.NoSuchProcess:
                continue                                     # already gone: that is the goal
            except (psutil.Error, OSError) as exc:
                failed.append(f"{name} ({pid}): {exc}")
        if failed:
            raise RuntimeError("could not end " + "; ".join(failed))
    return run


def delete_folder(path: str):
    """Delete a folder and everything in it."""
    def run() -> None:
        p = Path(path)
        if not p.is_dir():
            return
        errors = []
        shutil.rmtree(p, onexc=lambda f, name, exc: errors.append(name))
        if errors:
            raise RuntimeError(f"{len(errors)} files in {path} could not be deleted "
                               f"(in use?), e.g. {errors[0]}")
    return run


def stop_ollama_model(name: str):
    """Unload a model from Ollama now (keep_alive 0), freeing its memory."""
    def run() -> None:
        body = json.dumps({"model": name, "keep_alive": 0}).encode()
        req = urllib.request.Request("http://127.0.0.1:11434/api/generate", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    return run


def open_terminal(args: list[str], cwd: str):
    """Open a new Windows Terminal window running a command (a report, say)."""
    def run() -> None:
        subprocess.Popen(["wt", "-w", "new", "-d", cwd, *args],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return run
