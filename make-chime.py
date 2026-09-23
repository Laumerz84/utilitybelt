#!/usr/bin/env python
"""Generate the notification sounds. Re-run after editing to change them.

    python make-chime.py                 # write both wavs
    python make-chime.py --play          # write both and play them in turn
    python make-chime.py --only attention --play

Two sounds, deliberately different so you can tell them apart from another
tab without looking:

  done       Claude finished a turn.  Rising fifth, 0.68s, unhurried.
  attention  Claude is blocked on you. Two quick taps, 0.30s, same pitch.

Design notes, since "not disturbing" is the whole requirement:
  - Rising reads as "ready"; falling reads as "failed"; repeating a single
    pitch reads as "waiting". That is the whole vocabulary here.
  - Soft curved attack. An instant attack is what makes a notification
    sound like an alarm.
  - Exponential decay with a little 2nd/3rd harmonic, so each note reads as
    a struck bell rather than a test tone.
  - Peaks well below full scale, so they sit under music instead of over it.
"""
from __future__ import annotations

import argparse
import math
import struct
import sys
import wave
from pathlib import Path

RATE = 44_100
HERE = Path(__file__).resolve().parent

# name -> (peak amplitude, attack seconds, decay rate, [(start s, Hz, dur s)])
PROFILES = {
    # finished, you can come back - rising fifth, unhurried
    "done": (0.22, 0.018, 3.4, [
        (0.00, 146.83, 0.42),   # D3
        (0.11, 220.00, 0.52),   # A3, a fifth above
    ]),
    # blocked on you - one pitch repeated reads as "waiting"
    "attention": (0.26, 0.010, 7.0, [
        (0.00, 220.00, 0.16),   # A3
        (0.13, 220.00, 0.17),
    ]),
    # something failed - falling semitone, the only dissonant sound here
    "error": (0.24, 0.008, 6.0, [
        (0.00, 220.00, 0.16),   # A3
        (0.12, 207.65, 0.30),   # G#3
    ]),
    # refused - one short low thud, no pitch movement, nothing to resolve
    "denied": (0.22, 0.006, 9.0, [
        (0.00, 146.83, 0.22),   # D3
    ]),
    # informational, happens on its own - descending third, deliberately quiet
    "compact": (0.13, 0.020, 4.0, [
        (0.00, 220.00, 0.26),   # A3
        (0.14, 174.61, 0.34),   # F3
    ]),
    # a subagent finished - lighter echo of "done", easy to ignore
    "subagent": (0.14, 0.014, 5.0, [
        (0.00, 220.00, 0.24),   # A3
    ]),
}


# What each one means. Kept next to the sound it describes so the two cannot
# drift apart - `--list` and SOUNDS.md are both generated from here.
MEANINGS = {
    "done":      ("Claude finished a turn",
                  "Rising fifth. Rising reads as ready."),
    "attention": ("Blocked on you - permission or a question",
                  "One pitch twice. Repetition reads as waiting. Loudest."),
    "error":     ("A tool failed",
                  "Falling semitone, the only dissonance. Falling reads as failed."),
    "denied":    ("Permission refused",
                  "One low thud, no pitch movement. Nothing is pending."),
    "compact":   ("Context was compacted",
                  "Descending third, quiet. It happened on its own."),
    "subagent":  ("A subagent finished",
                  "Single light tap. The real done still comes later."),
}

NOTE_NAMES = {146.83: "D3", 174.61: "F3", 207.65: "G#3", 220.00: "A3"}


def describe_all() -> str:
    """The sound reference, rendered from PROFILES + MEANINGS."""
    import wave as _wave

    lines = ["KitchenMind / Claude Code notification sounds", ""]
    lines.append(f"{'SOUND':<11}{'NOTES':<10}{'LEN':>6}  MEANING")
    lines.append("-" * 74)
    for name, (peak, _a, _d, notes) in PROFILES.items():
        pitches = " ".join(NOTE_NAMES.get(round(f, 2), f"{f:.0f}Hz")
                           for _, f, _ in notes)
        try:
            with _wave.open(str(HERE / f"{name}.wav")) as w:
                dur = w.getnframes() / w.getframerate()
        except Exception:
            dur = 0.0
        meaning = MEANINGS.get(name, ("", ""))[0]
        lines.append(f"{name:<11}{pitches:<10}{dur:>5.2f}s  {meaning}")
    lines.append("")
    lines.append("Why they sound like that")
    for name, (_m, why) in MEANINGS.items():
        lines.append(f"  {name:<11}{why}")
    lines += [
        "",
        "When they do NOT play",
        "  - Turns under 12s are silent (QUIET_UNDER_S in notify.py) - you were",
        "    still watching the screen.",
        "  - Everything is silent while the Claude app is focused",
        "    (MUTE_WHEN_FOCUSED) - the sidebar already brightens. Add names to",
        "    ALWAYS_PLAY to let a sound through anyway.",
        "  - Failed Read / Glob / Grep / TodoWrite make no sound; failed Bash",
        "    or Edit do (BORING_FAILURES).",
        "",
        "Grammar: rising = ready, falling = failed, repeating = waiting,",
        "single = done and nothing needed. Loud wants you; quiet informs you.",
        "",
        "Hear one:   python make-chime.py --only error --play",
        "Rules live in decide() in notify.py. Sounds live in PROFILES here.",
    ]
    return "\n".join(lines)


def render(peak: float, attack: float, decay: float, notes: list) -> bytes:
    total = max(s + d for s, _, d in notes) + 0.05
    n = int(total * RATE)
    buf = [0.0] * n

    for start, freq, dur in notes:
        i0 = int(start * RATE)
        for i in range(int(dur * RATE)):
            if i0 + i >= n:
                break
            t = i / RATE
            if t < attack:
                env = (t / attack) ** 2
            else:
                env = math.exp(-decay * (t - attack) / max(dur - attack, 1e-6))
            s = math.sin(2 * math.pi * freq * t)
            s += 0.28 * math.sin(2 * math.pi * freq * 2 * t)
            s += 0.08 * math.sin(2 * math.pi * freq * 3 * t)
            buf[i0 + i] += s * env

    top = max(abs(v) for v in buf) or 1.0
    scale = peak / top
    fade = int(0.004 * RATE)          # 4ms tail so it cannot click
    out = bytearray()
    for i, v in enumerate(buf):
        v *= scale
        if i > n - fade:
            v *= (n - i) / fade
        out += struct.pack("<h", int(max(-1.0, min(1.0, v)) * 32767))
    return bytes(out)


def play(path: Path) -> None:
    if sys.platform != "win32":
        return
    import subprocess
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    f"(New-Object Media.SoundPlayer '{path}').PlaySync()"],
                   timeout=15)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--play", action="store_true")
    ap.add_argument("--only", choices=sorted(PROFILES))
    ap.add_argument("--list", action="store_true",
                    help="print the sound reference and exit")
    ap.add_argument("--doc", action="store_true",
                    help="write that reference to SOUNDS.md")
    args = ap.parse_args()

    if args.list or args.doc:
        text = describe_all()
        if args.list:
            print(text)
        if args.doc:
            (HERE / "SOUNDS.md").write_text(text + "\n", encoding="utf-8")
            print(f"\nwrote {HERE / 'SOUNDS.md'}")
        return 0

    for name, (peak, attack, decay, notes) in PROFILES.items():
        if args.only and name != args.only:
            continue
        data = render(peak, attack, decay, notes)
        path = HERE / f"{name}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(data)
        print(f"wrote {path.name:<14} {len(data)/1024:>4.0f} KB  "
              f"{len(data)/2/RATE:.2f}s")
        if args.play:
            play(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
