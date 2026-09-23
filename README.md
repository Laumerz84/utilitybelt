# UtilityBelt

Local tooling for this machine and the Claude Code work running on it. Plain
Python and PowerShell, no build step.

These files used to live loose in `~/.claude`. They were moved out because that
directory also holds `.credentials.json`, every transcript, and the full prompt
history — none of which can be version controlled. Claude Code's hooks point at
this folder by absolute path; see **Wiring** below before moving it again.

## The dashboards

### `belt.py` — interactive system and Claude dashboard

```
belt.cmd                 # or: python belt.py
python belt.py --probe   # one sample of every metric, no UI
```

Eight panels, each clickable (or press its number) for a detail view. Escape
goes back, `q` quits. Repaints once a second.

| Panel | Detail view shows |
| --- | --- |
| CPU | every thread individually, frequency, context switches and interrupts per second, load average, uptime, top processes |
| Memory | physical breakdown, swap and paging, top processes by resident size |
| GPU | load, VRAM, and a per-engine split (3D, copy, video) |
| Storage | per-volume capacity, live read/write rates, lifetime bytes and op counts per device |
| Network | per-interface totals, packets, errors, drops, link speed, socket counts |
| Claude | per-model token and cost table, most expensive sessions |
| Agents | every subagent transcript: last tool used, workflow, calls, cost, age |
| Processes | grouped by program, then top processes by CPU and by memory |

Metric selection follows what btop and glances treat as the useful set: per-core
rather than an average, memory split by available and cached, I/O rates
alongside capacity, and per-process attribution.

**GPU note.** This machine has an AMD Radeon RX 9070 XT, so there is no
`nvidia-smi`. GPU load comes from Windows performance counters instead. A
`Get-Counter` call costs 200-400 ms, which does not fit inside a one-second
frame, so one PowerShell process is started at launch and streams a JSON line
per second that a reader thread consumes. The upside is the per-engine
breakdown; the cost is that temperature, fan speed and power draw are not
available — those need vendor tooling that does not exist here.

### `dash.py` — the same picture as one static frame

```
dash.cmd
python dash.py --once    # print one frame and exit
python dash.py --tick 2  # slower repaint
```

Predates `belt.py` and is kept because it has no dependencies and `--once`
suits piping into something else. Hand-drawn ANSI, three refresh clocks, and an
incremental transcript scan that only re-reads a file when its mtime or size
changes.

## The rest

| File | What it does |
| --- | --- |
| `notify.py` | Sound dispatcher for Claude Code hooks. Picks a `.wav` from this folder by event name. |
| `make-chime.py` | Generates those `.wav` files. Re-run it to change the sounds. |
| `disk-sentinel.py` | What is eating the disk, and what grew since the last snapshot. |
| `token-report.py` | Token and cost report across all Claude Code sessions. |
| `startup-audit.ps1` | Everything that launches itself at sign-in. |
| `statusline.ps1` | Claude Code status line — live context and cost. |
| `SOUNDS.md` | Notes on the sound design. |

## Setup

```
pip install textual psutil
```

Only `belt.py` needs them. Everything else is standard library.

## Wiring

`~/.claude/settings.json` calls into this folder by absolute path — seven
notification hooks pointing at `notify.py`, and one status line pointing at
`statusline.ps1`. **Moving or renaming this directory breaks all eight
silently:** the hooks still fire and still exit 0, they just stop making any
sound. If you move it, update those paths and then confirm a sound actually
plays rather than trusting the exit code.

The `.wav` files are committed so a fresh clone works without running
`make-chime.py` first.

## Costs

Every cost shown by `belt.py`, `dash.py` and `token-report.py` is API list
rates applied to the local transcript logs. Plan billing works differently —
`/usage` is authoritative.
