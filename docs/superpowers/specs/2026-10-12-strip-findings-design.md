# SPEC: findings engine for the UtilityBelt strip

Audience: the Claude implementing this. Written to be executed, not persuaded by.
Date: 2026-10-12. Status: design approved by user; implementation plan not yet written.

---

## 1. Build this

A diagnosis layer for `F:\ClodCode\utilitybelt`. Rules join facts already in the
Sampler's `snap` and emit **Findings** — each naming a cause and a fix. The strip shows
findings when they exist and a quiet one-line summary when they do not.

The existing strip shows levels (`cpu 8%`, `vram 15%`). Levels are nearly always normal
and therefore nearly never read. Findings are the product; levels are filler between
them.

**Acceptance test for any rule you write:** it must fill `because` and `fix` with
something that is not a restatement of `title`. If it can only report a number, it is a
metric — put it in the quiet row instead and do not make it a rule.

---

## 2. Hard constraints — read before writing code

| Constraint | Source | Consequence if ignored |
| --- | --- | --- |
| **No unit tests.** No TDD. E2E only at the end, and ask the user which cases first. | user's CLAUDE.md | Wasted work; user has said tests here have not helped |
| **Never write to C:** for new files, venvs, caches, output | user's CLAUDE.md | C: is at ~13% free |
| **Do not touch `F:\ClodCode\utilitybelt-agents`** (branch `agents-live`) | machine-setup.md | Another session owns it |
| `belt.py` is **121 KB**. Do not add this feature to it. | — | Already too large to reason about |
| PowerShell 5.1 mangles UTF-8 no-BOM on `Get-Content -Raw`/`Set-Content` | machine-setup.md | Edit with the Edit tool or Python, never a PS round-trip |
| `.ps1` files are read as ANSI without a BOM | seen 2026-10-12 | Non-ASCII literals in `.ps1` corrupt; build them from `[char]` codes |
| Claude sessions run in an MSIX sandbox; `%APPDATA%` writes are redirected to `…\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\…` | machine-setup.md | **Config lives in `%APPDATA%`. A session testing config writes will see its own copy, not the user's.** Verify via WMI (`Invoke-CimMethod Win32_Process Create`) or by reading the real path explicitly |

**Working tree is dirty:** `belt.py` has uncommitted changes from 2026-10-12 (the
hidden-state visibility gating, §7). Do not revert them. Do not commit anything unless
the user asks.

---

## 3. Existing interfaces — verified 2026-10-12, use exactly

### `Sampler` (belt.py:413)

```python
sampler.read() -> dict          # belt.py:1028, returns dict(self.snap) under self.lock
sampler._note(msg: str)         # belt.py:1020, appends to snap["errors"], keeps last 8
sampler.lock                    # threading.Lock
sampler.stop                    # threading.Event
sampler.hidden                  # threading.Event — set while the strip is slid away
```

Threads started in `__init__` (belt.py:472-481):

| Thread | Interval | Does |
| --- | --- | --- |
| `_fast_loop` | 1.0 s, or `HIDDEN_FAST_EVERY` (5.0) when hidden | cheap counters |
| `_slow_loop` | 3.0 s | `_sample_processes`, `_sample_disks`, `_sample_nets`; `_sample_claude` every 7th tick |
| `_gpu_loop` | streamed | one long-lived PowerShell |
| `_info_loop` | 5 s | Ollama poll, event log every 5 min, inventory once |
| `_agent_loop` | 5 s | transcript tailing |

### `Series` (belt.py:312)

```python
s = Series(); s.push(value)     # deque, maxlen = HIST (300)
s.points                        # deque[float]
```

### `snap` keys (belt.py:439-462) — the rules' entire input

```
cpu_freq, cpu_freq_max, cpu_physical, cpu_logical, ctx_switches, interrupts
mem, swap                  psutil namedtuples, None until first pass
disks            list[{device, fstype, total, used, free, percent}]
per_disk         {device: {read, write, rcount, wcount}}
nets             list
procs            list[{pid, name, cpu, rss, threads, ppid, created}]
                   NOTE cpu is already divided by logical core count
gpu_types, gpu_adapters
boot             epoch
claude           {sessions: [...], today_calls, today_cost, models, scanned}
claude_ready     False until the first full transcript pass lands — RULES MUST
                   CHECK THIS before reporting agent counts, or they report 0
agents           list
ollama           None until first poll, else {up: bool, models: [...]}
sysinfo          RAM modules, board, BIOS, CPU, GPU driver
events           event-log entries from the last EVENT_DAYS (7)
helpers          {sessions, count, rss}
leftovers        list
tailscale        None = not installed, else up/down
errors           list[str], last 8
```

### `Belt(App)` (belt.py:1710) and the strip

```python
app.sampler, app.mover, app.forced_mode
app._docked, app._shown          # initialised in __init__ as of 2026-10-12
app.ui_hidden() -> bool          # docked AND not shown
app._sync_sampler()              # keeps sampler.hidden in step; call after ANY
                                 #   change to _docked/_shown
```

Screens: `Overview` (belt.py:1481, `CARDS` list of `(key, title)`), `Detail`,
`AgentsScreen`, `AgentView`. Widgets: `Card`, `TopBar`, `MiniStrip`, `DragBar`.
Both `Overview` and `Detail` repaint via a `_tick` wrapper that skips when
`app.ui_hidden()`.

---

## 4. The Finding

New file `findings.py`.

```python
@dataclass(frozen=True)
class Action:
    label: str                  # button text, imperative: "Stop model"
    run: Callable[[], None]     # executed on a worker thread, never the UI thread
    confirm: bool = True        # second click required before run()

@dataclass(frozen=True)
class Finding:
    key: str                    # stable, dotted: "models.starving_game"
                                #   identity for dedupe, mute, and `since`
    severity: str               # "info" | "warn" | "critical"
    title: str                  # plain words, no jargon, no numbers
    because: str                # the joined facts WITH the numbers that prove it
    fix: str                    # what to do, in words
    action: Action | None = None
    since: float = 0.0          # set by the engine, not the rule
```

**Findings are derived, never accumulated.** Every pass recomputes the whole set. A
finding vanishes when its condition stops holding — there is no dismissal and no stale
alert. The engine carries `since` forward by `key` so durations survive recomputation.

Severity ordering for display: `critical` > `warn` > `info`, then most recent `since`
first. Severity must be conveyed by colour **and** position, never colour alone.

---

## 5. Engine

In `findings.py`.

```python
class Engine:
    def __init__(self, cfg: Config): ...
    def evaluate(self, snap: dict) -> list[Finding]: ...
```

Behaviour:

1. Import enabled rule modules (§6), collect their `RULES` lists.
2. Call each rule as `rule(snap, cfg) -> Finding | None`.
3. Wrap every call in `try/except Exception`. On raise: log once per rule key per
   session via `sampler._note`, skip that rule. **One broken rule must never take down
   the strip.**
4. Carry `since` from the previous pass where `key` matches; set `time.time()` for new
   keys.
5. Drop findings whose `key` is muted and unexpired (§9).
6. Sort and return.

**Rules do no I/O.** No `subprocess`, no file reads, no network, no `psutil` calls. They
are pure functions of `(snap, cfg)`. If a rule needs a signal that is not in `snap`, add
it to the Sampler (§8). This keeps rules replayable against a recorded snapshot, which
is how they get checked given the no-unit-tests rule.

---

## 6. Rule modules

```
rules/__init__.py     discovery: import enabled modules, collect RULES
rules/agents.py       ENABLED in v1
rules/hygiene.py      ENABLED in v1
rules/models.py       present, DISABLED by default
rules/remote.py       present, DISABLED by default
```

Each module ends with `RULES = [fn, fn, ...]`.

### v1: `rules/agents.py`

| key | Fires when | Reads |
| --- | --- | --- |
| `agents.waiting` | ≥1 session awaiting user input | `claude.sessions`, gated on `claude_ready` |
| `agents.unattended` | a session working > `unattended_minutes` with no output | `agents`, `claude.sessions` |
| `agents.spend` | `claude.today_cost` > `spend_today_usd` | `claude.today_cost` |

### v1: `rules/hygiene.py`

| key | Fires when | Reads |
| --- | --- | --- |
| `hygiene.disk_floor` | any disk `free` < `disk_floor_gb` | `disks` |
| `hygiene.disk_trend` | a disk will cross its floor within `disk_trend_minutes` | new `disk_free` series (§8) |
| `hygiene.core_hog` | a process has held ≈1 full core for > `core_hog_hours` | `procs` — compare cumulative CPU against `created`; **not** instantaneous `cpu` |
| `hygiene.orphan` | long-lived process whose `ppid` no longer exists | `procs` |
| `hygiene.stranded_cache` | a cache dir on C: that a moved env var no longer points at | new signal (§8) |

### Disabled by default: `rules/models.py`

`models.starving_game` (model resident while a game runs and VRAM is oversubscribed),
`models.vram_spill` (shared-memory usage above a floor), `models.keepalive_thrash`
(repeated load/unload cycles), `models.two_models` (co-resident models exceeding VRAM).

### Disabled by default: `rules/remote.py`

`remote.stream_live`, `remote.resolution_changed`, `remote.input_injection`.

**Why these two are off by default:** they assume Ollama and Sunshine specifically. The
v1 enabled set must work on any machine running Claude Code.

### 6a. Discovery — both mechanisms (user decision)

**`rules/README.md`** — the rule contract: the signature, that rules must be pure
functions of `(snap, cfg)` and do no I/O, the §1 acceptance test, the `snap` keys
available, and how to enable a module in config.

**`rules/example.py`** — one fully commented rule that returns `None`. Not listed in any
module's `RULES`, never loaded. It exists to be copied.

**`belt rules --list`** — prints every discovered rule: key, module, enabled/disabled,
muted-until, and its one-line description (first line of the function's docstring, so
there is nothing extra to maintain). The two solve different problems: the README says
how to write one, the command says what is actually there right now.

---

## 7. Conflict with current code — resolve this first

`_slow_loop` as committed today (belt.py, uncommitted change) does:

```python
while not self.stop.wait(3.0):
    if self.hidden.is_set():
        continue             # out of sight: skip the process/disk/net walks
```

That also stops rule evaluation, so **the sliver could never alert** — which defeats the
hidden state's whole purpose.

Required change: when hidden, `_slow_loop` must still sample and evaluate, at a reduced
rate (suggest every 4th tick ≈ 12 s). Only *repaint* is skipped when hidden; the
`_tick` wrappers on `Overview` and `Detail` already handle that correctly and need no
change.

---

## 8. Sampler additions — the only Sampler work in scope

1. **`disk_free` series per device.** Add a `Series` per device, pushed in
   `_sample_disks` (belt.py:654). Needed for `hygiene.disk_trend`. Without it, "full in
   ~40 minutes" is impossible and the rule is just another floor check.
   `hygiene.disk_trend` must require a **sustained** slope across several samples — a
   naive two-point slope during a Steam download will cry wolf.

2. **Cumulative CPU per process.** `_sample_processes` (belt.py:556) records
   instantaneous `cpu` only. Add `cpu_time` (`proc.cpu_times()` user+system) to each
   row. `hygiene.core_hog` compares `cpu_time` against `now - created`; the 31-hour hog
   found on 2026-10-12 showed `cpu 6.2%` of total, which looks harmless, but ≈100% of
   one core sustained.

3. **Sunshine session state**, only if `remote.py` is enabled. The reliable source is
   Sunshine's log (`CLIENT CONNECTED` / `CLIENT DISCONNECTED`), default
   `C:\Program Files\Sunshine\config\sunshine.log`. **`netstat` is not a valid source** —
   it shows only a listener, because the stream is UDP; that is precisely why a 4-hour
   live stream was invisible on 2026-10-12. The Sampler tails the log; the path is
   config.

4. **Stranded-cache detection**, for `hygiene.stranded_cache`: compare known cache env
   vars (`UV_CACHE_DIR`, `OLLAMA_MODELS`, `PIP_CACHE_DIR`) against their default
   locations; a populated default directory that the env var no longer points at is the
   finding. Real instance: `UV_CACHE_DIR` moved to `F:\Caches\uv`, leaving 8.73 GB
   stranded on C: that `uv cache clean` could not reach.

---

## 9. Config

TOML at `%APPDATA%\UtilityBelt\config.toml`, created with defaults on first run.
**See the MSIX sandbox warning in §2 before testing writes.**

```toml
actions = true               # top-level keys must precede the first table
sound_on_critical = true     # §12a
history_entries = 50         # §10a, 0 disables the log

[rules]
agents = true
hygiene = true
models = false
remote = false

[thresholds]
disk_floor_gb = 20
disk_trend_minutes = 60
core_hog_hours = 4
spend_today_usd = 100
unattended_minutes = 30

[quiet_row]
items = ["agents", "vram", "spend", "disk", "clock"]

[mutes]
"hygiene.stranded_cache" = "forever"
```

Rules:

- Unknown keys ignored with a logged note — a newer config must not break an older build.
- Missing keys fall back to defaults.
- Only the `[mutes]` table is ever rewritten; the file is never rewritten wholesale, so
  hand edits and comments survive.
- A malformed config falls back to defaults **entirely** and raises one `info` finding
  naming the file. The strip must still start.

**Config cannot define actions.** Thresholds, mutes and enablement only. A config file
that can run arbitrary commands is a security hole, not a feature. The action set is
fixed in code; `actions = false` disables all of them for a read-only strip.

---

## 10. Display

| State | Shows |
| --- | --- |
| Quiet | one low-contrast row: `● all clear`, then `[quiet_row].items`. Smallest, dullest state |
| Findings | findings on top, sorted per §4, each with title, `because`, and `fix` as a real button; quiet row demotes to a darker band below. A quiet-row value a finding cites is highlighted |
| Critical | the strip's ground colour changes. Reserved for imminent loss |
| Sliver (hidden) | finding count, agents waiting, clock. Must alert while hidden — see §7 |

Mockups: https://claude.ai/artifact/JpdNXvfdpz3WditQu6yAqy (artboards `StQuiet`,
`StOne`, `StMany`, `StCritical`, `StSliver`).

### 10a. History — small rolling log (user decision)

Answers "what fired while I was out".

- A bounded `deque(maxlen=50)` of cleared findings, held by the `Engine`. An entry
  records `key`, `severity`, `title`, `since`, `cleared_at`.
- An entry is appended when a finding present in the previous pass is absent from this
  one.
- Viewable in the `Detail` screen under a `findings` card key. **Read-only** — no
  filtering, no search, no export.
- **Kept on disk (user decision, 2026-10-12):** appended to
  `%APPDATA%\UtilityBelt\findings-history.jsonl`, read back at start, trimmed to
  about `history_entries` lines. The user wants "was my machine OK overnight" to
  survive a restart. (The first draft had it in memory only.)
- Muted findings are logged like any other; muting suppresses display, not history.

This is the one place where this feature resembles the dashboard it is otherwise not.
Keep it to a list and a view.

---

## 11. Muting

Without muting, a finding the user has chosen to live with becomes noise, and noise is
the failure mode that makes the whole feature worthless.

- Mute by `key` for 1 h / until reboot / forever.
- Persisted in `[mutes]`, so mutes survive restart and are hand-editable.
- A muted finding still counts in the sliver total; renders collapsed and grey.
- **`critical` cannot be muted forever** — 1 h maximum.

---

## 12. Actions

- `Action.run` executes on a worker thread. The UI thread never blocks.
- `confirm=True` (the default) requires a second click on a changed button. Not a modal.
- Anything that kills a process, deletes files, or ends another party's session **must**
  confirm. A delete action names the exact path in its confirm state.
- An action that fails surfaces as an `info` finding carrying the error text. It never
  crashes the UI and never fails silently.
- Correct behaviour after a successful action: the finding clears by itself on the next
  pass, because findings are derived (§4). Do not clear it manually.

### 12a. Sound on critical — on by default (user decision)

- Uses the repo's existing `sounds.py`.
- Fires **only** for `severity == "critical"`, and **once per finding key** — on the
  transition from absent to present. Not on every pass.
- A finding that clears and returns later may sound again; that is a genuinely new
  event.
- Suppressed entirely while that key is muted.
- Config: `sound_on_critical = true` (top-level, §9). On by default because `critical`
  means imminent loss, which is the one case where interrupting the user is justified.

**The trap:** the evaluation pass runs every 3 s. Keying the sound off presence rather
than transition produces a beep every 3 seconds until the condition clears. Track which
keys have already sounded.

---

## 13. Error handling

| Situation | Behaviour |
| --- | --- |
| Rule raises | caught per rule, logged once per key per session, skipped |
| Config malformed | full fallback to defaults + one `info` finding naming the file |
| Action fails | `info` finding with the error text |
| Signal absent (no Ollama, no Sunshine) | its rules quietly disable. Absence is not an error and must not produce a finding |

---

## 14. Done when

1. Strip shows the quiet row with `● all clear` when no rule fires.
2. Each v1 rule can be made to fire, and its finding names a real cause and a real fix.
3. A rule raising an exception does not disturb the strip.
4. A finding clears by itself once its condition ends.
5. Mutes survive a restart.
6. The sliver shows a finding count while hidden (requires §7).
7. A malformed config still starts the strip.
8. `belt.py` grew only by: constructing the engine, rendering findings, wiring buttons.
9. `belt rules --list` prints every rule with its enabled and muted state (§6a).
10. A cleared finding appears in the history view; the log never exceeds its bound (§10a).
11. A critical finding sounds **once**, not once per pass (§12a).

---

## 15. Do not

- Do not put the engine, rules, or config loading in `belt.py`.
- Do not let rules call `psutil`, `subprocess`, the filesystem or the network.
- Do not add a metric as a rule because it was easy. Apply §1's acceptance test.
- Do not write unit tests (§2).
- Do not commit without the user asking.
- Do not build a notification centre or a settings UI. The history log (§10a) is a
  bounded in-memory list and a read-only view — not a feature to grow.
- Do not play the critical sound on every evaluation pass. Once per finding key, per
  §12a. Passes run every 3 s; getting this wrong means a beep every 3 seconds forever.

---

## 16. Open

None. The three questions that were here were answered by the user on 2026-10-12 and
have moved into §17 and the sections they affect (§6a discovery, §10a history,
§12a sound).

The one author's choice flagged here (history in memory only) was put to the user on
2026-10-12: the history log is kept on disk (§10a).

---

## 17. Decided — do not re-litigate

| Decision | By |
| --- | --- |
| All four jobs (early warning, ambient, command centre, day companion), early warning as core | user |
| Built for this machine, structured to be shareable; possibly sold cheaply | user |
| v1 = agents + hygiene enabled; models + remote present but off | user |
| Python plugin rules, not declarative config — real diagnoses need logic TOML cannot express | user chose from two options |
| Findings over a demoted quiet row; sliver alerts while hidden | user, from mockups |
| Calendar and weather are out — commodity, and every other tool has them | user |
| Rule discovery: both a `rules/README.md` + example rule **and** `belt rules --list` | user |
| Cleared findings go into a small rolling log, viewable in Detail | user |
| Critical findings play a sound, on by default | user |
