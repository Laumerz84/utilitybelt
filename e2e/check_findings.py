"""End-to-end checks for findings on the strip (the five agreed 2026-10-12).

    python e2e/check_findings.py

Uses the real engine, settings file handling, history file and the strip (Textual,
headless), with a scratch settings folder under .tmp-tests/ so the real
%APPDATA%\\UtilityBelt files are never touched. Prints PASS/FAIL per check.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import belt                                              # noqa: E402
import beltconfig                                        # noqa: E402
import findings                                          # noqa: E402
from findings import Finding                             # noqa: E402

WORK = ROOT / ".tmp-tests" / "e2e-findings"
results: list[tuple[str, bool, str]] = []


def fresh() -> Path:
    """An empty scratch settings folder; the history file goes there too."""
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    beltconfig.HISTORY = WORK / "findings-history.jsonl"
    return WORK / "config.toml"


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))


class Switch:
    """A fake rule whose finding is present while .on is True."""

    def __init__(self, key: str, severity: str) -> None:
        self.on, self.key, self.severity = True, key, severity

    def __call__(self, snap, cfg):
        if not self.on:
            return None
        return Finding(self.key, self.severity, "Test finding", "because a test said so", "turn the test off")


def engine_with(path: Path, *rules, plays: list | None = None, notes: list | None = None) -> findings.Engine:
    eng = findings.Engine(beltconfig.load(path), note=(notes.append if notes is not None else lambda m: None),
                          play=(lambda: plays.append(1)) if plays is not None else None)
    eng.rules = [("agents", r) for r in rules]           # only the test's rules
    return eng


SNAP = {"now": 0.0, "boot": 1_000_000.0}


def passes(eng, n: int) -> list:
    out = []
    for _ in range(n):
        out = eng.evaluate(dict(SNAP))
    return out


def wait_sound() -> None:
    import time
    time.sleep(0.2)                                      # the sound plays on its own thread


# 1. A critical finding sounds once, not once per pass
def sound_once() -> None:
    path = fresh()
    plays: list = []
    rule = Switch("agents.test_critical", "critical")
    eng = engine_with(path, rule, plays=plays)
    passes(eng, 10)
    wait_sound()
    first = len(plays)
    rule.on = False
    passes(eng, 3)
    rule.on = True
    passes(eng, 5)
    wait_sound()
    again = len(plays)
    rule.on = False
    passes(eng, 1)
    eng.mute(rule.key, "1h", "critical")
    rule.on = True
    passes(eng, 5)
    wait_sound()
    check("1. critical sound plays once per appearance",
          first == 1 and again == 2 and len(plays) == 2,
          f"10 passes -> {first} sound(s); cleared and back -> {again}; muted and back -> {len(plays)}")


# 2. A malformed config still starts the strip
def malformed_config() -> None:
    path = fresh()
    path.write_text("actions = true\n[rules\nthis is not toml = = =\n", encoding="utf-8")
    cfg = beltconfig.load(path)
    eng = findings.Engine(cfg, note=lambda m: None)
    found = eng.evaluate(dict(SNAP))
    keys = [f.key for f in found]
    drawn = asyncio.run(strip_text(eng))
    check("2. malformed settings: defaults used, strip starts, file named",
          bool(cfg.error) and cfg.rules == beltconfig.DEFAULTS["rules"] and "config.malformed" in keys
          and str(path) in next(f.because for f in found if f.key == "config.malformed")
          and "could not be read" in drawn,
          f"error={cfg.error[:60]!r}; findings={keys}; strip shows it: {'could not be read' in drawn}")


async def strip_text(eng: findings.Engine) -> str:
    """Start the real app headless with this engine as its findings source and
    return what the strip draws."""
    s = belt.Sampler()
    s.engine = eng
    try:
        s._evaluate()
        app = belt.Belt(s, mini=True)
        async with app.run_test(size=(100, 8)) as pilot:
            await pilot.pause(1.2)
            strip = app.screen.query_one("#mini")
            # the app has drawn it once already (a crash would have ended run_test);
            # this is the same drawing, as text
            return app.render_mini(s.read(), [], 98, bar=strip).plain
    finally:
        s.stop.set()


# 3. Mutes survive a restart (and the rest of the file is left alone)
def mutes_survive() -> None:
    path = fresh()
    beltconfig.load(path)                                # writes the commented default file
    with path.open("a", encoding="utf-8") as fh:
        fh.write("")
    text = path.read_text(encoding="utf-8").replace("disk_floor_gb = 20", "disk_floor_gb = 33  # mine")
    path.write_text(text, encoding="utf-8")
    rule = Switch("agents.test_warn", "warn")
    eng = engine_with(path, rule)
    passes(eng, 1)
    eng.mute(rule.key, "forever")
    eng.mute("agents.other", "reboot")
    restarted = engine_with(path, rule)                  # a new engine reads the file again
    f = passes(restarted, 1)[0]
    after = path.read_text(encoding="utf-8")
    check("3. mutes survive a restart; hand edits kept",
          f.muted and restarted.cfg.mutes.get("agents.other", "").startswith("reboot")
          and "disk_floor_gb = 33  # mine" in after and "# UtilityBelt settings" in after,
          f"muted after restart: {f.muted}; mutes={restarted.cfg.mutes}; hand edit kept: "
          f"{'disk_floor_gb = 33  # mine' in after}")


# 4. A cleared finding goes into the history, which stays within its bound
def history() -> None:
    path = fresh()
    path.write_text(beltconfig.TEMPLATE.replace("history_entries = 50", "history_entries = 5"), encoding="utf-8")
    rule = Switch("agents.test_info", "info")
    eng = engine_with(path, rule)
    for _ in range(40):                                  # appear, clear, appear, clear...
        passes(eng, 1)
        rule.on = not rule.on
    lines = beltconfig.HISTORY.read_text(encoding="utf-8").splitlines()
    restarted = engine_with(path, rule)
    entry = restarted.history[-1] if restarted.history else {}
    check("4. cleared findings logged, kept across restart, bounded",
          len(eng.history) == 5 and len(restarted.history) == 5 and entry.get("key") == rule.key
          and entry.get("cleared_at") is not None and len(lines) <= 2 * 5,
          f"in memory {len(eng.history)}, after restart {len(restarted.history)}, file lines {len(lines)}, "
          f"last entry {json.dumps(entry)[:80]}")


# 5. A rule that raises does not disturb the strip
def broken_rule() -> None:
    path = fresh()
    notes: list = []

    def broken(snap, cfg):
        raise KeyError("nope")

    good = Switch("agents.test_warn", "warn")
    eng = engine_with(path, broken, good, notes=notes)
    found = passes(eng, 5)
    drawn = asyncio.run(strip_text(eng))
    check("5. a raising rule is skipped, noted once, strip still draws",
          [f.key for f in found] == ["agents.test_warn"] and len(notes) == 1 and "Test finding" in drawn,
          f"findings={[f.key for f in found]}; notes={notes}; strip shows the good finding: {'Test finding' in drawn}")


def main() -> int:
    real_history = beltconfig.HISTORY
    try:
        for fn in (sound_once, malformed_config, mutes_survive, history, broken_rule):
            try:
                fn()
            except Exception as exc:                     # a crash is a failure, not the end of the run
                check(fn.__name__, False, f"{type(exc).__name__}: {exc}")
    finally:
        beltconfig.HISTORY = real_history
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}\n      {detail}")
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
