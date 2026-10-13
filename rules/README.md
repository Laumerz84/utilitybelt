# Writing a findings rule

A rule turns facts UtilityBelt already samples into a **finding**: something wrong,
*why* (with the numbers), and *what to do*. The strip shows findings; when there are
none it shows one quiet line.

To see what rules exist right now, whether they run, and what is muted:

    python belt.py rules --list

## The contract

```python
from findings import Action, Finding

def my_rule(snap: dict, cfg) -> Finding | None:
    """One line saying what this catches (shown by `rules --list`)."""
```

- **Pure.** A rule reads `snap` and `cfg` and nothing else: no `psutil`, `subprocess`,
  file reads or network. Rules run every few seconds on the sampler thread, and being
  pure means one can be replayed against a saved snapshot. If you need a signal that
  is not in `snap`, add it to the `Sampler` in `belt.py`.
- **Return `None`** when there is nothing to say. Never a finding that says "all fine".
- **The key** is `"<module>.<function name>"`, e.g. `"hygiene.disk_floor"`. It is the
  finding's identity: mutes, "since" and the history log all go by it.
- **Severity:** `info` (worth knowing), `warn` (should act), `critical` (imminent
  loss: the strip turns red and plays a sound once). Use `critical` sparingly.
- **The acceptance test:** `because` and `fix` must say something `title` does not.
  If all a rule can report is a number, it is a metric, not a finding: put it in the
  quiet row instead (`[quiet_row]` in config.toml).
- **Check readiness.** Some keys are empty until their first scan: test
  `snap["claude_ready"]` before saying anything about chats or agents, and treat
  `snap["ollama"] is None` as "not known yet".
- **Actions** come from `actions.py` only (end processes, delete a folder, unload a
  model, open a report). Anything that ends a process, deletes, or ends someone's
  session must keep `confirm=True` and name exactly what it will touch in
  `confirm_label`. Config can switch actions off; it can never define one.
- `cites=("disk",)` highlights that quiet-row item while the finding shows.
- Thresholds come from `cfg.t("name")`; add new ones to `DEFAULTS` and `TEMPLATE` in
  `beltconfig.py`.

## What `snap` holds

| key | what |
| --- | --- |
| `now`, `boot` | epoch seconds |
| `mem`, `swap` | psutil named tuples (None until the first pass) |
| `disks` | `[{device, fstype, total, used, free, percent}]` |
| `disk_history` | `{device: ((t, free), ...)}` over the last ~20 min |
| `procs` | `[{pid, name, cpu, cpu_time, rss, threads, ppid, created, what?}]` (`cpu` is a share of the whole CPU; `cpu_time` is seconds used since start) |
| `leftovers` | script processes whose parent has gone: `[{pid, name, what, age, created, ...}]` |
| `vram_used`, `vram_total`, `gpu_types` | bytes; `{"3D": %, "Compute": %, ...}` |
| `gpu_sensors` | AMD only: `{edge, hotspot, memory, fan_pct, power, ...}` |
| `ollama`, `ollama_loads` | `None` / `{up, models: [{name, size, vram, until}]}`; `((t, name), ...)` |
| `claude`, `claude_ready` | `{sessions: [{title, state, age, cost, last_tool}], today_cost, today_calls}` |
| `live_agents` | working subagents: `[{desc, chat, now: {label, since}}]` |
| `stranded` | `[{var, path, size, now_at}]` caches left behind on C: |
| `sunshine` | remote module only: `{live, since, screen, screen_before}` |
| `events` | Windows event-log entries (hardware errors, crashes) |

## Turning a module on

In `%APPDATA%\UtilityBelt\config.toml`:

```toml
[rules]
models = true
```

A new module needs its name added to `MODULES` in `findings.py` and a default under
`[rules]` in `beltconfig.py`. `example.py` is a commented rule to copy.
