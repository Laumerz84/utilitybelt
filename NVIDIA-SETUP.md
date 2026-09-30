# Setting UtilityBelt up on an NVIDIA machine

UtilityBelt was built on a PC with an AMD Radeon card. On an NVIDIA card it
already runs and shows GPU load and video memory (those come from Windows
itself), but it cannot show temperature, fan speed, power draw or clocks.
NVIDIA's driver can report all of those without admin rights, so a short
change adds them.

## Steps

1. Install Python 3.10+ and clone the repo (see **Setup** in `README.md`).
2. `pip install -r requirements.txt`
3. Open Claude Code in the `utilitybelt` folder and paste the prompt below.

---

## Prompt for Claude Code

Copy everything between the lines.

---

I've cloned UtilityBelt, a Windows terminal dashboard (`belt.py`, built on
Textual and psutil). It was written on a PC with an AMD GPU. My PC has an
NVIDIA GPU. Please set it up for my machine and add NVIDIA sensor readings.
Read `README.md` and the docstring at the top of `belt.py` first. They explain
the three views (mini, standard, detail), the display rules, and why sampling
runs on background threads.

**1. Check the basics work as they are**
- Run `nvidia-smi` and tell me the card and driver version.
- Run `pip install -r requirements.txt`, then `python belt.py --probe`. Fix
  anything that fails on my machine before changing anything else.

**2. Add NVIDIA readings through NVML**
- Use the `nvidia-ml-py` package (`import pynvml`). It talks to `nvml.dll`,
  which ships with the NVIDIA driver and needs no admin rights. Add it to
  `requirements.txt` by uncommenting the line that is already there.
- In `Sampler`, try `pynvml.nvmlInit()` once at start. If it fails (no
  NVIDIA card, package missing, old driver), everything must keep working
  exactly as it does now. The AMD path must not change. Keep the import
  inside a try block so the package stays optional.
- If NVML works, sample it on its own thread at 1 Hz (like `_gpu_loop`) and
  store readings in `self.snap` under an `"nvidia"` key. Push load and VRAM
  % into the existing `self.gpu` and `self.vram` series so the graphs keep
  working. Collect for GPU 0 (and each GPU if there are several):
  - name, driver version
  - load % (`nvmlDeviceGetUtilizationRates`) and VRAM used/total
    (`nvmlDeviceGetMemoryInfo`)
  - core temperature (`nvmlDeviceGetTemperature`, `NVML_TEMPERATURE_GPU`),
    plus the slowdown threshold (`nvmlDeviceGetTemperatureThreshold`) if
    the card reports it
  - fan speed % (`nvmlDeviceGetFanSpeed`)
  - power draw and power limit in watts (`nvmlDeviceGetPowerUsage`,
    `nvmlDeviceGetEnforcedPowerLimit`; both are in milliwatts)
  - graphics and memory clocks (`nvmlDeviceGetClockInfo`)
  - why the card is slowing itself down, if it is:
    `nvmlDeviceGetCurrentClocksEventReasons`, or
    `nvmlDeviceGetCurrentClocksThrottleReasons` on older bindings. Turn the
    bit flags into plain words such as "too hot" or "at power limit".
  - video encoder/decoder use (`nvmlDeviceGetEncoderUtilization` /
    `...DecoderUtilization`)
  - Wrap each call separately. Some consumer cards return "not supported"
    for fan or power. Show a dash for those; don't crash or hide the card.
    Don't invent a hotspot temperature: NVML doesn't expose one on most
    consumer cards.
- Keep the PowerShell counter stream as well. It still provides the
  per-engine breakdown (3D, copy, video). If NVML is available, use NVML
  for the headline load and VRAM numbers.

**3. Show it, following the display rules in the README**
- GPU card (standard view): keep one headline line, e.g.
  `42% load  VRAM 61%  67°C  220 W`. The graph fills the space below. The
  bottom line stays the Ollama line.
- Mini strip: add the temperature after GPU load, e.g. `GPU 42% 67°C`.
- GPU detail view: a "sensors" section with temperature (and slowdown
  point), fan, power as "220 of 320 W", both clocks, encoder/decoder, and
  a plain-words reason whenever the card is slowing itself down. Add a
  5-minute graph of temperature and one of power, using the same `_graph`
  helper as the others. Replace the "needs LibreHardwareMonitor" note with
  the real readings when NVML is available.
- Alerts (`compute_alerts`): warn at 83°C and go red at 90°C (or 3°C below
  the card's own slowdown threshold, if it reports one). Also warn when the
  card has been held back for heat or power for more than 30 seconds. Colour
  is for severity only; graphs stay grey.
- Add the NVIDIA readings to `--probe` output.

**4. Wire it to my machine**
- `belt.py`, `belt.cmd` and `belt-mini.cmd` work from any folder, so they
  need nothing.
- The notification sounds and status line are optional and have their own
  installer. If `~/.claude/settings.json` doesn't already point its hooks at
  this folder's `notify.py`, ask whether I want them. If I do, run
  `python sounds.py --install`. It backs up and keeps my existing settings.
- Offer to pin `belt.cmd` and `belt-mini.cmd` to my taskbar.

**5. Prove it works**
- Run `python belt.py --probe` and compare temperature, power and VRAM with
  `nvidia-smi`. They should match within a degree / a few watts.
- Render the standard, mini and GPU detail views without opening a window,
  using Textual's `app.run_test(size=...)` and `app.save_screenshot()`.
  Check that nothing wraps and that nothing is coloured unless it's a
  warning. Test at 160x40, 100x44 and 112x5.
- Confirm that with NVML unavailable (e.g. simulate `nvmlInit` failing) the
  dashboard still starts and looks the same as before.
- Update `README.md` (the GPU note and the Health/GPU rows) to describe the
  NVIDIA support.

When you're done, show me a summary of what changed. If I have push access,
commit on a new branch and open a pull request. Otherwise leave the changes
committed locally and tell me.

---
