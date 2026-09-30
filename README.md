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
belt-mini.cmd            # the mini strip, in the top-right corner of the screen
python belt.py --mini    # the mini strip in the current window
python belt.py --probe   # one sample of every metric, no UI
```

Three levels of detail:

- **Mini** — three lines: headline numbers with small graphs, network and
  Claude status, then whatever needs attention. Used automatically when the
  window is under 90 columns or 16 rows; `m` toggles it at any size.
- **Standard** — eight cards, each leading with one number and a 5-minute
  graph that fills the card's spare height. A top bar lists what needs
  attention (click it, or press `h`, for the health view).
- **Detail** — click a card or press its number. Escape goes back, `q` quits.

Colour means severity only: grey is normal, yellow worth a look, red needs
action. Graphs stay grey; the number and the card border carry the colour.
Lines never wrap — they end in an ellipsis.

| Panel | Detail view shows |
| --- | --- |
| CPU | 5-minute graph, every thread, frequency, context switches and interrupts per second, uptime, top processes |
| Memory | 5-minute graph, breakdown, each RAM module with its running speed against the rated speed in its part number (catches EXPO being off), top processes |
| GPU | load and VRAM graphs, what Ollama has loaded and when it unloads, per-engine split, driver version |
| Storage | read/write graph, per-volume capacity, lifetime bytes and op counts per device |
| Network | download and upload graphs, Tailscale, per-interface totals, errors, sockets |
| Claude | which chats are working, waiting on you, or probably stuck on a permission prompt; per-model token and cost table |
| Agents | every working subagent live: its task, type, model, which chat started it, what it is doing right now and for how long, and its last 8 steps; then the recently finished ones. Click one (or ↑/↓ and Enter) to watch it: its task, Claude's output in full, thinking (when the log kept the words), every tool call with its full input, and results, following new steps like `tail -f` while scrolled to the bottom |
| Processes | Claude Code helpers (each loaded chat's MCP servers), leftover script processes whose parent has closed, then top processes |
| Health (`h`) | everything flagged, the Windows event log for 7 days (hardware/WHEA errors, blue screens, GPU driver resets, unexpected restarts, app crashes), board, BIOS, CPU, RAM and driver versions |

Chat state is read from how each transcript ends: a finished reply means
waiting on you; a tool call followed by silence usually means a permission
prompt. Nothing here is authoritative about billing — costs are API list rates.

Metric selection follows what btop and glances treat as the useful set: per-core
rather than an average, memory split by available and cached, I/O rates
alongside capacity, and per-process attribution.

**GPU note.** This machine has an AMD Radeon RX 9070 XT, so there is no
`nvidia-smi`. GPU load comes from Windows performance counters instead. A
`Get-Counter` call costs 200-400 ms, which does not fit inside a one-second
frame, so one PowerShell process is started at launch and streams a JSON line
per second that a reader thread consumes. The upside is the per-engine
breakdown; the cost is that temperature, fan speed and power draw are not
available from Windows. LibreHardwareMonitor can supply them (planned).

Slower facts come from a fourth thread: Ollama's `/api/ps` every 5 s, the
event log every 5 minutes (about 0.3 s, no admin needed), and RAM, board, BIOS
and driver details once at start.

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
| `requirements.txt` | Python packages for `belt.py`. |
| `NVIDIA-SETUP.md` | Setup steps and a ready-to-paste Claude Code prompt that adds NVIDIA temperature, fan, power and clock readings. |

## Setup

Needs Windows 10 or 11, Python 3.10 or newer, Windows PowerShell (built in),
and Windows Terminal for `belt-mini.cmd`.

```
git clone https://github.com/Laumerz84/utilitybelt.git
cd utilitybelt
pip install -r requirements.txt
python belt.py --probe     # checks every reading; should list your CPU, RAM, GPU, disks
belt.cmd                   # the dashboard
python sounds.py --install # optional: Claude Code sounds and status line (restart Claude Code after)
```

Only `belt.py` needs the packages. Everything else is standard library.
Ollama and Tailscale are optional: their panels say "not running" without them.

**Updates.** The dashboard checks GitHub for a newer version when it starts and
every 6 hours (a quiet `git fetch`; it never updates anything by itself). When
there is one, the bottom line says so; press `u` to see what's new and the
command to run, and `c` to copy it. You can also check by hand with
`python update_check.py`. This needs the `git clone` install above; a ZIP
download can't tell which version it is.

**NVIDIA card?** GPU load and VRAM work as-is, but this was built on an AMD
card. `NVIDIA-SETUP.md` has a prompt to give Claude Code that adds NVIDIA's
temperature, fan, power and clock readings.

## Wiring

Optional — only if you want the notification sounds and status line.
`~/.claude/settings.json` calls into this folder by absolute path — seven
notification hooks pointing at `notify.py`, and one status line pointing at
`statusline.ps1`. `python sounds.py --install` writes all eight for wherever
this folder is. **Moving or renaming this directory breaks them silently:**
the hooks still fire and still exit 0, they just stop making any sound. If you
move it, run `python sounds.py --install` again from the new place and confirm
a sound actually plays rather than trusting the exit code.

The `.wav` files are committed so a fresh clone works without running
`make-chime.py` first.

## Tests

```
python -m unittest discover -s tests
```

`agentlog.py` reads agent logs incrementally (only what was appended since the last read), so following working agents stays cheap however long their logs get.

## Costs

Every cost shown by `belt.py`, `dash.py` and `token-report.py` is API list
rates applied to the local transcript logs. Plan billing works differently —
`/usage` is authoritative.

## Sounds: settings and another computer

- Change the sounds: `python sounds.py`. Pick which sound each Claude Code event plays (or off), the quiet-turn
  length, and whether to stay silent while Claude is in front. `s` saves `sounds.json`.
- Share them: commit and push `sounds.json`; on the other computer, pull.
- Set up another computer (Windows, Python on PATH): clone this repo, then run `python sounds.py --install` in it.
  It points that computer's Claude Code hooks at this folder's `notify.py` and its status line at
  `statusline.ps1` (an existing status line from something else is left alone). It creates
  `~/.claude/settings.json` and `sounds.json` if either is missing, backs up an existing settings.json first,
  keeps any other hooks and settings, and refuses to touch a settings.json it cannot read. Running it twice
  is harmless. Restart Claude Code afterwards.
