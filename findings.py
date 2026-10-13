"""Findings: what is wrong right now, why, and what to do about it.

Rules (rules/*.py) are pure functions of (snap, cfg) that return a Finding or None.
The Engine runs them all on every sampler pass and returns the whole set, so a
finding disappears on its own once its condition stops holding: nothing is ever
dismissed and nothing goes stale. The engine only carries over what must survive
a pass: when each finding first appeared (`since`), which criticals have already
made a sound, mutes, and the log of findings that have cleared.
"""
from __future__ import annotations

import dataclasses
import importlib
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import beltconfig

SEVERITY_ORDER = {"critical": 0, "warn": 1, "info": 2}
ACTION_ERROR_SECONDS = 600              # how long a failed action stays on the strip


@dataclass(frozen=True)
class Action:
    label: str                          # button text, imperative: "Stop model"
    run: Callable[[], None]             # executed on a worker thread, never the UI thread
    confirm: bool = True                # second click required before run()
    confirm_label: str = ""             # what the button says while waiting for that click


@dataclass(frozen=True)
class Finding:
    key: str                            # stable, dotted: "hygiene.disk_floor"
    severity: str                       # "info" | "warn" | "critical"
    title: str                          # plain words, no numbers
    because: str                        # the joined facts, with the numbers that prove them
    fix: str                            # what to do, in words
    action: Action | None = None
    since: float = 0.0                  # set by the engine, not the rule
    cites: tuple = ()                   # quiet-row items this finding is about ("disk", "vram")
    muted: bool = False                 # set by the engine


def ordered(found: list[Finding]) -> list[Finding]:
    """Worst first, then the most recent."""
    return sorted(found, key=lambda f: (SEVERITY_ORDER.get(f.severity, 3), -f.since))


# ------------------------------------------------------------------ rules

MODULES = ("agents", "hygiene", "models", "remote")


def discover() -> list[tuple[str, Callable]]:
    """Every rule in every module, enabled or not: (module, function)."""
    out = []
    for name in MODULES:
        mod = importlib.import_module(f"rules.{name}")
        out += [(name, fn) for fn in getattr(mod, "RULES", [])]
    return out


def rule_key(fn: Callable) -> str:
    return getattr(fn, "key", f"{fn.__module__.removeprefix('rules.')}.{fn.__name__}")


def describe(fn: Callable) -> str:
    return ((fn.__doc__ or "").strip().splitlines() or [""])[0]


# ----------------------------------------------------------------- engine

class Engine:
    def __init__(self, cfg: beltconfig.Config, note: Callable[[str], None] = print,
                 play: Callable[[], None] | None = None) -> None:
        self.cfg = cfg
        self.note = note
        self.play = play                            # the critical sound; None = silent
        self.lock = threading.Lock()
        self._pass = threading.Lock()               # one evaluation at a time (sampler or a button)
        self.rules = discover()
        self._since: dict[str, float] = {}
        self._previous: dict[str, Finding] = {}
        self._sounded: set[str] = set()
        self._broken: set[str] = set()              # rule keys already reported as raising
        self._action_errors: dict[str, tuple[float, str, str]] = {}
        self._mtime = self._stat()
        self.boot = 0.0
        self.history: list[dict] = self._read_history()
        for n in cfg.notes:
            note(n)

    # -- config: hand edits are picked up without a restart
    def _stat(self) -> float:
        try:
            return self.cfg.path.stat().st_mtime
        except OSError:
            return 0.0

    def _reload_if_changed(self) -> None:
        mtime = self._stat()
        if mtime != self._mtime:
            self._mtime = mtime
            self.cfg = beltconfig.load(self.cfg.path)

    # -- the pass
    def evaluate(self, snap: dict) -> list[Finding]:
        with self._pass:
            return self._evaluate(snap)

    def _evaluate(self, snap: dict) -> list[Finding]:
        self._reload_if_changed()
        cfg, now = self.cfg, snap.get("now") or time.time()
        self.boot = snap.get("boot") or self.boot
        found: list[Finding] = []
        for module, rule in self.rules:
            if not cfg.rules.get(module):
                continue
            try:
                result = rule(snap, cfg)
            except Exception as exc:               # one broken rule must never take down the strip
                key = rule_key(rule)
                if key not in self._broken:
                    self._broken.add(key)
                    self.note(f"rule {key}: {type(exc).__name__}: {exc}")
                continue
            for f in (result if isinstance(result, list) else [result]):
                if isinstance(f, Finding):
                    found.append(f)

        if cfg.error:
            found.append(Finding(
                "config.malformed", "info", "The settings file could not be read",
                f"{cfg.path}: {cfg.error}",
                "Fix the file (or delete it to get the defaults back); until then the defaults are used."))
        with self.lock:
            for key, (when, label, text) in list(self._action_errors.items()):
                if now - when > ACTION_ERROR_SECONDS:
                    del self._action_errors[key]
                    continue
                found.append(Finding(f"action.failed.{key}", "info", f"“{label}” did not work",
                                     text, "Try it by hand; the finding it was for is still shown if it still holds."))

        out = []
        seen = set()
        for f in found:
            if f.key in seen:                      # two findings with one key: keep the first
                continue
            seen.add(f.key)
            if not cfg.actions and f.action is not None:
                f = dataclasses.replace(f, action=None)
            out.append(dataclasses.replace(f, since=self._since.get(f.key) or now,
                                           muted=self._muted(f)))

        current = {f.key: f for f in out}
        for key, f in self._previous.items():      # cleared since the last pass: log it
            if key not in current:
                self._log(f, now)
                self._sounded.discard(key)         # coming back later is a new event
        for f in out:
            if (f.severity == "critical" and f.key not in self._previous and not f.muted
                    and f.key not in self._sounded and cfg.sound_on_critical and self.play):
                self._sounded.add(f.key)
                threading.Thread(target=self._play, daemon=True).start()
        self._since = {f.key: f.since for f in out}
        self._previous = current
        return ordered(out)

    def _play(self) -> None:
        try:
            self.play()
        except Exception as exc:
            self.note(f"sound: {exc}")

    # -- mutes
    def _muted(self, f: Finding) -> bool:
        value = self.cfg.mutes.get(f.key)
        if value is None:
            return False
        until = beltconfig.mute_until(value, self.boot)
        if until is None:
            return False
        if f.severity == "critical":               # never hidden for long: at most 1 hour
            return until - time.time() <= 3600
        return True

    def mute(self, key: str, how: str, severity: str = "warn") -> None:
        """Mute a finding for '1h', until 'reboot', or 'forever' (critical: 1h at most)."""
        if severity == "critical":
            how = "1h"
        with self.lock:
            self.cfg.mutes[key] = beltconfig.mute_value(how, self.boot)
            try:
                beltconfig.write_mutes(self.cfg.mutes, self.boot, self.cfg.path)
                self._mtime = self._stat()
            except OSError as exc:
                self.note(f"mute not saved: {exc}")

    def unmute(self, key: str) -> None:
        with self.lock:
            self.cfg.mutes.pop(key, None)
            try:
                beltconfig.write_mutes(self.cfg.mutes, self.boot, self.cfg.path)
                self._mtime = self._stat()
            except OSError as exc:
                self.note(f"unmute not saved: {exc}")

    # -- actions
    def run_action(self, f: Finding, done: Callable[[], None] | None = None) -> None:
        """Run a finding's action on a worker thread. A failure becomes an info
        finding with the error; success needs nothing, as the finding clears by
        itself on the next pass once its cause is gone."""
        action = f.action
        if action is None or not self.cfg.actions:
            return

        def work() -> None:
            try:
                action.run()
            except Exception as exc:
                with self.lock:
                    self._action_errors[f.key] = (time.time(), action.label, f"{type(exc).__name__}: {exc}")
            if done:
                done()
        threading.Thread(target=work, name=f"action {f.key}", daemon=True).start()

    # -- history: findings that have cleared, kept on disk so it survives a restart
    def _read_history(self) -> list[dict]:
        limit = self.cfg.history_entries
        if limit <= 0:
            return []
        try:
            lines = beltconfig.HISTORY.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        self._file_lines = len(lines)
        out = []
        for ln in lines[-limit:]:
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
        return out

    def _log(self, f: Finding, now: float) -> None:
        limit = self.cfg.history_entries
        if limit <= 0:
            return
        entry = {"key": f.key, "severity": f.severity, "title": f.title, "because": f.because,
                 "since": f.since, "cleared_at": now, "muted": f.muted}
        self.history.append(entry)
        del self.history[:-limit]
        try:
            path = beltconfig.HISTORY
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
            self._file_lines = getattr(self, "_file_lines", 0) + 1
            if self._file_lines > 2 * limit:          # the file never holds more than twice the list
                path.write_text("".join(json.dumps(e) + "\n" for e in self.history), encoding="utf-8")
                self._file_lines = len(self.history)
        except OSError as exc:
            self.note(f"history not saved: {exc}")


def list_rules(cfg: beltconfig.Config | None = None) -> str:
    """`belt rules --list`: every rule, whether it runs, and whether it is muted."""
    cfg = cfg or beltconfig.load()
    import psutil
    boot = psutil.boot_time()
    rows = [("KEY", "MODULE", "STATE", "MUTED", "WHAT IT CATCHES")]
    for module, fn in discover():
        key = rule_key(fn)
        mute = cfg.mutes.get(key)
        muted = mute if mute and beltconfig.mute_until(mute, boot) is not None else "-"
        rows.append((key, module, "enabled" if cfg.rules.get(module) else "disabled", muted, describe(fn)))
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    lines = ["  ".join(r[i].ljust(widths[i]) for i in range(4)) + "  " + r[4] for r in rows]
    return "\n".join(lines) + f"\n\nsettings: {cfg.path}" + (f"\n(not used: {cfg.error})" if cfg.error else "")
