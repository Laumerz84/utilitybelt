#!/usr/bin/env python
"""UtilityBelt - interactive dashboard for this machine and the Claude work on it.

    python belt.py            # full interactive dashboard
    python belt.py --mini     # start as the small always-visible strip
    python belt.py --probe    # print one sample of every metric and exit

Three levels of detail:
  mini       a three-line strip for a corner of the screen. Chosen automatically
             when the window is small, or with --mini / the m key.
  standard   eight cards, each with one headline number and a 5-minute graph.
  detail     click a card or press its number (1-8); h opens the health view.
             Escape goes back.

Everything repaints once a second; nothing here starts, stops, or changes
anything on the machine. The one exception is the update check: at start and
every 6 hours a background `git fetch` asks GitHub whether a newer UtilityBelt
exists (update_check.py). If so, the hint line says so and u shows what's new
and the command to run - nothing is ever updated automatically.

Display rules
-------------
Problems first: the top bar names whatever needs attention, or says all good.
Colour carries meaning only - grey is normal, yellow is worth a look, red needs
action. Each card leads with one number; everything else lives in its detail
view. Lines never wrap; they end in an ellipsis instead.

Why the sampling is threaded
----------------------------
A 1 Hz repaint cannot afford to block. psutil counters are cheap and read on a
background thread; the GPU is not. There is no nvidia-smi on this box (the card
is an AMD RX 9070 XT), so GPU load has to come from Windows performance
counters, and Get-Counter costs 200-400 ms per call. Spawning PowerShell every
second would cost more than the frame. Instead one PowerShell process is
started once and left running, printing a JSON line per second that a reader
thread consumes. The UI only ever touches already-sampled values.

Slower facts (Ollama's loaded models, the Windows event log, RAM and BIOS
details) come from a third thread on their own clocks: Ollama every 5 s, the
event log every 5 min, hardware inventory once at start.

The sampling itself (the Sampler class and everything it needs) lives in
sampler.py, so other programs can use the same readings without this app.

Metric selection follows what btop and glances consider the useful set: per-core
CPU rather than just an average, memory split by cached/available, disk I/O
rates alongside capacity, network throughput, and per-process attribution.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psutil
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

import agentlog
import beltconfig
import findings
import notify
import signals
import winprocs
import cpusensors
import gpusensors
import update_check
from sampler import (  # noqa: F401  (every reading lives in sampler.py; all its names stay reachable as belt.X)
    AGENT_LIVE_SECONDS, AGENT_RECENT, AGENT_RECENT_DETAILED, ALERT_EVENT_HOURS, CHAT_TAIL_BYTES,
    CLAUDE, CRIT, DDR_GRADES, EVENT_DAYS, EVENT_KINDS, EVENTS_PS, GPU_STREAMER, HIDDEN_FAST_EVERY,
    HIDDEN_SLOW_EVERY, HIST, HOME, LAB_FRESH, LEFTOVER_NAMES, LOCAL, OK, OLLAMA_PS, PRICES,
    PROJECTS, SESSION_RECENT_HOURS, STATE_LABEL, STRANDED_EVERY, SYSINFO_PS, WARN, compute_alerts,
    cost_of, describe_cmd, disk_level, duration, human_bytes, level, new_log, orphan_kind, ps_json,
    ram_speed, rate, rated_speed, Sampler, Series, session_state, tokens, TranscriptTally, worst,
)


def escape(text) -> str:
    """Make any text safe to show inside Textual markup. Every "[" is escaped:
    rich's and Textual's own escape functions both let some code through
    (e.g. `[[T("x", c="dim")]]` from an agent's tool input) and the whole view
    then fails to render."""
    return str(text).replace("[", "\\[")


UPDATE_EVERY = 6 * 3600         # seconds between checks for a newer version on GitHub


# Severity is the only thing colour is used for. Every bar, graph, number and
# border goes through these so "yellow" means the same thing everywhere.
# (OK, WARN and CRIT themselves live in sampler.py.)
FILL = {OK: "grey62", WARN: "yellow", CRIT: "red"}
NUM = {OK: "bold", WARN: "bold yellow", CRIT: "bold red"}


def bar(frac: float, width: int = 18, lvl: str = OK) -> str:
    frac = max(0.0, min(1.0, float(frac or 0)))
    filled = int(round(frac * width))
    return f"[{FILL[lvl]}]{'█' * filled}[/][grey30]{'─' * (width - filled)}[/]"


BLOCKS = " ▁▂▃▄▅▆▇█"


def squeeze(points: list[float], width: int) -> list[float]:
    """Fit a history into `width` columns, keeping each column's peak so a
    short spike is never averaged away."""
    if width <= 0:
        return []
    if len(points) <= width:
        return points
    step = len(points) / width
    return [max(points[int(i * step):int((i + 1) * step)] or [0.0]) for i in range(width)]


def chart(points, width: int, height: int = 1, top: float = 100.0, lvl: str = OK) -> list[str]:
    """A block-character graph `height` rows tall, top row first. History
    grows in from the right; zero reads as a faint baseline, not as nothing."""
    vals = squeeze(list(points), width)
    pad = width - len(vals)
    top = top or 1.0
    # Graphs stay grey even when the reading is bad: a 90% memory graph is a
    # solid slab, and a solid yellow slab shouts louder than the problem. The
    # number and the card border carry the severity instead.
    colour = "grey58"
    rows = []
    for r in range(height - 1, -1, -1):
        cells = []
        for v in vals:
            eighths = max(0.0, min(1.0, v / top)) * height * 8 - r * 8
            idx = 0 if eighths <= 0 else 8 if eighths >= 8 else int(round(eighths))
            if r == 0 and v > 0 and idx == 0:
                idx = 1                                # never draw a live value as nothing
            cells.append(BLOCKS[idx])
        body = "".join(cells)
        if r == 0:
            base = body.replace(" ", "\0")             # mark empty bottom cells
            body = "".join(f"[grey27]▁[/]" if ch == "\0" else f"[{colour}]{ch}[/]" for ch in base)
            rows.append(" " * pad + body)
        else:
            rows.append(" " * pad + f"[{colour}]{body}[/]")
    return rows


# -------------------------------------------------------------------- views

class Card(Static):
    """One clickable panel on the standard grid."""

    def __init__(self, key: str, title: str, index: int) -> None:
        super().__init__(id=f"card-{key}", classes="card")
        self.key = key
        self.border_title = f"{index} {title}"

    def on_click(self, event: events.Click) -> None:
        self.app.open_detail(self.key)


def drag_target(window: tuple, grab: tuple, mouse: tuple) -> tuple:
    """Where the window goes: it moves exactly as far as the mouse has since the grab."""
    return window[0] + mouse[0] - grab[0], window[1] + mouse[1] - grab[1]


def is_click(press: tuple, release: tuple, slop: int = 4) -> bool:
    """A press and release this close together is a click, not a drag."""
    return abs(release[0] - press[0]) <= slop and abs(release[1] - press[1]) <= slop


BAR_BUTTONS_FULL = (("▾", "small"), ("–", "minimize"), ("×", "close"))
BAR_BUTTONS_SMALL = (("▴", "full"), ("–", "minimize"), ("×", "close"))
SMALL_COLS, SMALL_ROWS = 88, 4          # the small strip's window, in terminal cells (4 rows:
                                        # about the shortest Windows Terminal will make one)
PEEK = 4                                # px of a hidden strip left showing at the top edge
HIDE_AFTER = 0.5                        # s the mouse must be away before it slides up
STRIP_MAX_FINDINGS = 3                  # findings shown on the strip; the rest under f
CONFIRM_SECONDS = 6                     # how long a "click again" button waits
SEV_MARK = {"critical": "■", "warn": "▲", "info": "●"}   # shape as well as colour
SEV_STYLE = {
    "critical": {"mark": "bold red", "title": "bold #f3c6bf", "why": "#b09a96",
                 "button": "bold black on #e0705f"},
    "warn": {"mark": "bold #e8a33d", "title": "bold #f0d9a8", "why": "grey62",
             "button": "bold black on #e8a33d"},
    "info": {"mark": "#6fa8dc", "title": "#c7ced8", "why": "grey50", "button": "grey85 on grey23"},
}


def button_strip(buttons) -> str:
    """The buttons as drawn at the very end of a bar line, two spaces apart."""
    return "  ".join(mark for mark, _ in buttons)


def button_at(x: int, width: int, buttons):
    """Which button (its action) column x of a `width`-wide line lands on, if any.
    Uses the same layout as button_strip, so drawing and clicking agree."""
    strip = button_strip(buttons)
    i = x - (width - len(strip))
    if 0 <= i < len(strip) and strip[i] != " ":
        return buttons[i // 3][1]
    return None


def small_rect(cell: tuple, edges: tuple, area: tuple,
               cols: int = SMALL_COLS, rows: int = SMALL_ROWS) -> tuple:
    """(x, y, w, h) for the small strip: `cols` x `rows` cells of the current
    size plus the window's own edges, at the top centre of the work area
    (left, top, right, bottom), like Zoom's meeting controls."""
    w = round(cols * cell[0]) + edges[0]
    h = round(rows * cell[1]) + edges[1]
    left, top, right, _ = area
    return left + (right - left - w) // 2, top, w, h


def hidden_y(top: int, height: int, peek: int = PEEK) -> int:
    """Window y when slid up out of sight, leaving `peek` px at the top edge."""
    return top - height + peek


def slide_steps(start: int, end: int, n: int = 6) -> list:
    """The y positions of a short ease-out slide from start to end."""
    return [round(start + (end - start) * (1 - (1 - i / n) ** 2)) for i in range(1, n + 1)]


def docks(y: int, top: int, snap: int = 24) -> bool:
    """A strip dropped this close to the top edge docks there (and hides)."""
    return y - top <= snap


def wants_open(mouse: tuple, left: int, width: int, top: int,
               peek: int = PEEK, slack: int = 2) -> bool:
    """Is the mouse on the sliver a hidden strip leaves at the top edge?"""
    return left <= mouse[0] < left + width and top - slack <= mouse[1] <= top + peek + slack


class WindowMover:
    """Finds the Windows Terminal window this program runs in and moves it.

    In focus mode the terminal has no title bar, so the top bar has to do its
    job. The console window Windows hands this process is owned by the
    terminal's real window (class CASCADIA_HOSTING_WINDOW_CLASS), which is what
    gets moved. Everything is a no-op off Windows or outside Windows Terminal.
    """

    def __init__(self) -> None:
        self.hwnd = None
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes
            self._wt = wintypes
            self._u32 = ctypes.WinDLL("user32", use_last_error=True)
            self._ct = ctypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.GetConsoleWindow.restype = wintypes.HWND
            self._u32.GetAncestor.restype = wintypes.HWND
            self._u32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
            console = k32.GetConsoleWindow()
            root = self._u32.GetAncestor(console, 3) if console else None   # GA_ROOTOWNER
            name = ctypes.create_unicode_buffer(64)
            if root and self._u32.GetClassNameW(root, name, 64) and \
                    name.value == "CASCADIA_HOSTING_WINDOW_CLASS":
                self.hwnd = root
        except Exception:
            self.hwnd = None

    def mouse(self) -> tuple:
        pt = self._wt.POINT()
        self._u32.GetCursorPos(self._ct.byref(pt))
        return pt.x, pt.y

    def button_down(self) -> bool:
        return bool(self._u32.GetAsyncKeyState(0x01) & 0x8000)     # VK_LBUTTON

    def position(self) -> tuple:
        r = self._wt.RECT()
        self._u32.GetWindowRect(self.hwnd, self._ct.byref(r))
        return r.left, r.top

    def move_to(self, x: int, y: int) -> None:
        # SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
        self._u32.SetWindowPos(self.hwnd, None, int(x), int(y), 0, 0, 0x0001 | 0x0004 | 0x0010)

    def rect(self) -> tuple:
        r = self._wt.RECT()
        self._u32.GetWindowRect(self.hwnd, self._ct.byref(r))
        return r.left, r.top, r.right - r.left, r.bottom - r.top

    def client_size(self) -> tuple:
        r = self._wt.RECT()
        self._u32.GetClientRect(self.hwnd, self._ct.byref(r))
        return r.right - r.left, r.bottom - r.top

    def set_rect(self, x: int, y: int, w: int, h: int) -> None:
        # SWP_NOZORDER | SWP_NOACTIVATE
        self._u32.SetWindowPos(self.hwnd, None, int(x), int(y), int(w), int(h), 0x0004 | 0x0010)

    def work_area(self) -> tuple:
        """(left, top, right, bottom) of the usable area (taskbar excluded) of this window's monitor."""
        ct, wt = self._ct, self._wt

        class MonitorInfo(ct.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                        ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]

        self._u32.MonitorFromWindow.restype = wt.HANDLE
        mon = self._u32.MonitorFromWindow(self.hwnd, 2)          # MONITOR_DEFAULTTONEAREST
        info = MonitorInfo()
        info.cbSize = ct.sizeof(MonitorInfo)
        if mon and self._u32.GetMonitorInfoW(wt.HANDLE(mon), ct.byref(info)):
            r = info.rcWork
            return r.left, r.top, r.right, r.bottom
        return 0, 0, 1920, 1040

    def topmost(self, on: bool) -> None:
        # HWND_TOPMOST / HWND_NOTOPMOST; SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
        self._u32.SetWindowPos(self.hwnd, self._wt.HWND(-1 if on else -2), 0, 0, 0, 0,
                               0x0002 | 0x0001 | 0x0010)

    def minimize(self) -> None:
        self._u32.ShowWindow(self.hwnd, 6)                       # SW_MINIMIZE

    def shadow(self, on: bool) -> None:
        """Windows draws a drop shadow below the window even when the window
        itself is slid up off screen. Switching off its non-client rendering
        (DWMWA_NCRENDERING_POLICY = disabled) removes the shadow; "use window
        style" puts it back."""
        value = self._ct.c_int(0 if on else 1)
        try:
            self._ct.windll.dwmapi.DwmSetWindowAttribute(
                self._wt.HWND(self.hwnd), 2, self._ct.byref(value), 4)
        except Exception:
            pass

    def shadow_is_on(self) -> bool:
        """Whether Windows is drawing the frame (and so the shadow) right now."""
        value = self._ct.c_int(0)
        try:
            self._ct.windll.dwmapi.DwmGetWindowAttribute(
                self._wt.HWND(self.hwnd), 1, self._ct.byref(value), 4)   # DWMWA_NCRENDERING_ENABLED
        except Exception:
            return False
        return bool(value.value)


class DragBar(Static):
    """A bar that stands in for the missing title bar: drag it to move the
    window, and while the mouse is over it, buttons appear at the right end of
    its last line.

    While the mouse button is held the real mouse position is polled (~60 Hz)
    rather than waiting for terminal mouse events: the window moves under the
    cursor, so the cursor barely moves relative to it and the terminal stops
    reporting."""

    BUTTONS = BAR_BUTTONS_FULL
    BUTTON_ROW = 0                                         # the text line the buttons are drawn on

    def on_mount(self) -> None:
        self._grab = None
        self.hover = False
        self.hot = None                                    # the button under the mouse

    def _button(self, event):
        """The button action under the mouse, if the event is on a button."""
        off = event.get_content_offset(self)
        if off is None or off.y != self.BUTTON_ROW:
            return None
        return button_at(off.x, self.content_size.width, self.BUTTONS)

    def _refresh(self) -> None:
        screen = self.screen
        if isinstance(screen, Overview):
            screen.redraw_bar()

    def on_enter(self, event: events.Enter) -> None:
        self.hover = True
        self._refresh()

    def on_leave(self, event: events.Leave) -> None:
        self.hover, self.hot = False, None
        self._refresh()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        hot = self._button(event)
        if hot != self.hot:
            self.hot = hot
            self._refresh()

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 1:
            return
        if self._button(event):                            # buttons act on the click (release),
            return                                          # not here: see on_click
        mover = self.app.mover
        if mover.hwnd is None:
            return
        old = getattr(self, "_timer", None)
        if old is not None:
            old.stop()                                      # a press while one is still followed
        self._grab = (mover.position(), mover.mouse())
        self._dragging = False
        self._timer = self.set_interval(1 / 60, self._follow)

    def _follow(self) -> None:
        mover = self.app.mover
        if self._grab is None:                              # a tick already queued when released
            return
        window, grab = self._grab
        mouse = mover.mouse()
        if mover.button_down():
            self._dragging = self._dragging or not is_click(grab, mouse)
            if self._dragging:
                mover.move_to(*drag_target(window, grab, mouse))
            return
        self._timer.stop()                                  # released
        self._grab = None
        if self._dragging:
            self.on_drag_end()
        else:
            self.on_plain_click()

    def on_click(self, event: events.Click) -> None:
        # Buttons act on release: acting on press changes the layout under the
        # mouse, and the release then lands on (and clicks) whatever moved there.
        action = self._button(event)
        if action:
            self.app.bar_action(action)
        elif self.app.mover.hwnd is None:
            self.on_plain_click()                           # no dragging here: plain click

    def on_plain_click(self) -> None:
        pass

    def on_drag_end(self) -> None:
        pass


class TopBar(DragBar):
    """The standard view's top line: drag to move, click for Health."""

    BUTTONS = BAR_BUTTONS_FULL

    def on_plain_click(self) -> None:
        self.app.open_detail("health")


class MiniStrip(DragBar):
    """The small strip: drag anywhere to move; window buttons on its last line,
    each finding's own buttons (act, mute) at the end of its title line."""

    BUTTONS = BAR_BUTTONS_SMALL
    BUTTON_ROW = 0                                         # set on each draw: the quiet row

    def on_mount(self) -> None:
        super().on_mount()
        self.hits: dict = {}                               # row -> [(x0, x1, hit)], from render_mini

    def _hit(self, event):
        off = event.get_content_offset(self)
        if off is None:
            return None
        under = [hit for x0, x1, hit in self.hits.get(off.y, ()) if x0 <= off.x < x1]
        # a real button wins over a link (a link cut short by the line's end can sit under one)
        return next((h for h in under if h[0] != "view"), under[0] if under else None)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        hit = self._hit(event) if event.button == 1 else None
        if hit and hit[0] != "view":
            return                                          # a finding button: acts on the click
        self._press_hit = hit                               # a link: opens on a click, not a drag
        super().on_mouse_down(event)

    def on_click(self, event: events.Click) -> None:
        hit = self._hit(event)
        if hit and hit[0] != "view":
            self.app.strip_hit(hit)
        elif hit and self.app.mover.hwnd is None:
            self.app.strip_hit(hit)                         # no dragging here: a link opens at once
        elif not hit:
            super().on_click(event)

    def on_plain_click(self) -> None:
        hit, self._press_hit = getattr(self, "_press_hit", None), None
        if hit:
            self.app.strip_hit(hit)

    def on_drag_end(self) -> None:
        self.app.strip_dropped()


class Overview(Screen):
    CARDS = [
        ("cpu", "CPU"), ("memory", "MEMORY"), ("gpu", "GPU"), ("storage", "STORAGE"),
        ("network", "NETWORK"), ("claude", "CLAUDE"), ("agents", "AGENTS"),
        ("processes", "PROCESSES"),
    ]

    def compose(self) -> ComposeResult:
        yield TopBar(id="topbar")
        with Grid(id="grid"):
            for i, (key, title) in enumerate(self.CARDS, start=1):
                yield Card(key, title, i)
        yield Static(id="hint")
        yield MiniStrip(id="mini")

    def on_mount(self) -> None:
        self.set_interval(1.0, self._tick)
        self.apply_mode()
        self.redraw()

    def _tick(self) -> None:
        app = self.app
        if not app.ui_hidden():
            self.redraw()
            return
        # Out of sight: repaint only when the sliver's one line would change
        sig = app.sliver_signature(app.sampler.read())
        if sig != getattr(self, "_sliver_sig", None):
            self._sliver_sig = sig
            self.redraw()

    def on_resize(self, event: events.Resize) -> None:
        self.apply_mode()
        self.redraw()

    def is_mini(self) -> bool:
        forced = self.app.forced_mode
        if forced:
            return forced == "mini"
        return self.size.height < 16 or self.size.width < 90

    def apply_mode(self) -> None:
        self.set_class(self.is_mini(), "-mini")
        grid = self.query_one("#grid", Grid)
        wide = self.size.width >= 128
        grid.styles.grid_size_columns = 4 if wide else 2
        grid.styles.grid_size_rows = 2 if wide else 4

    def redraw_bar(self) -> None:
        """Only the top bar (or the strip): what a hover changes. Redrawing all eight cards
        for that made the buttons lag on a busy PC. Uses the last full redraw's readings."""
        last = getattr(self, "_last", None)
        if last is None:
            self.redraw()
            return
        app, (snap, alerts) = self.app, last
        if self.is_mini():
            strip = self.query_one("#mini", MiniStrip)
            strip.update(app.render_mini(snap, alerts, self.size.width - 2, bar=strip))
            return
        bar = self.query_one("#topbar", TopBar)
        bar.update(app.render_topbar(alerts, self.size.width - 4, bar=bar, found=snap.get("findings")))

    def redraw(self) -> None:
        app = self.app
        snap = app.sampler.read()
        alerts = compute_alerts(snap, app.sampler)
        self._last = (snap, alerts)
        if self.is_mini():
            strip = self.query_one("#mini", MiniStrip)
            strip.update(app.render_mini(snap, alerts, self.size.width - 2, bar=strip))
            strip.set_class(app._strip_crit, "-crit")
            app.fit_strip()
            return
        bar = self.query_one("#topbar", TopBar)
        bar.update(app.render_topbar(alerts, self.size.width - 4, bar=bar, found=snap.get("findings")))
        for key, _ in self.CARDS:
            try:
                card = self.query_one(f"#card-{key}", Card)
            except Exception:
                continue
            w = max(10, card.size.width - 4)
            h = max(3, card.size.height - 2)
            try:
                lines, lvl = getattr(app, f"_card_{key}")(snap, app.sampler, w, h)
            except Exception as exc:
                lines, lvl = [f"[red]{escape(type(exc).__name__)}: {escape(str(exc))}[/]"], WARN
            card.set_class(lvl == WARN, "warn")
            card.set_class(lvl == CRIT, "crit")
            card.update("\n".join(lines[:h]))
        errs = snap.get("errors") or []
        note = f"   [red]{escape(errs[-1])}[/]" if errs else ""
        info = app.update_info
        if info is not None and info.available:
            n = info.behind
            note = f"   [bold yellow]⬆ Update available ({n} new change{'s' if n != 1 else ''}) · u to update[/]" + note
        self.query_one("#hint", Static).update(
            "[dim]1-8 open a panel · f findings · h health · m mini view · q quit[/]" + note)


class Detail(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Back"), ("q", "app.pop_screen", "Back")]

    TITLES = {"cpu": "CPU", "memory": "Memory", "gpu": "GPU", "storage": "Storage",
              "network": "Network", "claude": "Claude", "agents": "Agents",
              "processes": "Processes", "health": "Health", "update": "Update",
              "findings": "Findings"}

    def __init__(self, key: str) -> None:
        super().__init__()
        self.key = key

    def compose(self) -> ComposeResult:
        yield Static(id="dtitle")
        with VerticalScroll(id="detail-body"):
            yield Static(id="detail-content")
        yield Static(id="dhint")

    def on_mount(self) -> None:
        self.set_interval(1.0, self._tick)
        self.redraw()

    def _tick(self) -> None:
        if not self.app.ui_hidden():        # out of sight: nothing to repaint
            self.redraw()

    def redraw(self) -> None:
        if not self.query("#detail-content"):              # closed again while still opening
            return
        snap = self.app.sampler.read()
        self.query_one("#dtitle", Static).update(
            f"[bold]UtilityBelt[/] [dim]›[/] [bold]{self.TITLES.get(self.key, self.key)}[/]"
            f"   [dim]{datetime.now():%H:%M:%S}[/]")
        self.query_one("#detail-content", Static).update(
            self.app.render_detail(self.key, snap, max(40, self.size.width - 8)))
        self.query_one("#dhint", Static).update(
            "[dim]esc back · 1-8 other panels · f findings · h health · u update · q back[/]")


class AgentBlock(Static):
    """One agent in the Agents list; click (or Enter) opens its live view."""

    def __init__(self, path: Path) -> None:
        super().__init__(classes="agent-block")
        self.path = path

    def on_click(self, event: events.Click) -> None:
        self.app.open_agent(self.path)


class AgentsScreen(Detail):
    """Working agents as live blocks, then the recently finished ones."""

    BINDINGS = Detail.BINDINGS + [
        Binding("up", "move(-1)", "Up", priority=True),
        Binding("down", "move(1)", "Down", priority=True),
        Binding("enter", "open", "Open", priority=True),
    ]

    def __init__(self) -> None:
        super().__init__("agents")
        self.selected = 0
        self.paths: list[Path] = []

    def on_mount(self) -> None:
        self.app.sampler.agents_open += 1                 # the detailed feed, only while open
        super().on_mount()

    def on_unmount(self) -> None:
        self.app.sampler.agents_open -= 1

    def compose(self) -> ComposeResult:
        yield Static(id="dtitle")
        with VerticalScroll(id="detail-body"):
            yield Static(id="agents-head")
            yield Vertical(id="agent-blocks")
            yield Static(id="agents-foot")
        yield Static(id="dhint")

    def redraw(self) -> None:
        app = self.app
        snap = app.sampler.read()
        live = snap.get("live_agents") or []
        chats = snap.get("live_chats") or []
        live_paths = {a["path"] for a in live}
        done = [a for a in snap.get("agents") or [] if a["path"] not in live_paths][:10]
        rows = [("live", a) for a in chats] + [("live", a) for a in live] + [("done", a) for a in done]
        paths = [a["path"] for _, a in rows]
        box = self.query_one("#agent-blocks", Vertical)
        if paths != self.paths:                           # agent set changed: rebuild the blocks
            keep = self.paths[self.selected] if self.paths and self.selected < len(self.paths) else None
            box.remove_children()
            box.mount_all([AgentBlock(p) for p in paths])
            self.paths = paths
            self.selected = paths.index(keep) if keep in paths else 0
        width = max(40, self.size.width - 10)
        for i, (block, (kind, a)) in enumerate(zip(box.query(AgentBlock), rows)):
            block.set_class(i == self.selected, "-selected")
            block.set_class(kind == "done", "-done")
            block.update(app.render_agent_block(a, width) if kind == "live"
                         else app.render_agent_done(a))
        self.query_one("#dtitle", Static).update(
            f"[bold]UtilityBelt[/] [dim]›[/] [bold]Agents[/]   [dim]{datetime.now():%H:%M:%S}[/]")
        seen = (f"{len(snap.get('agents') or [])} seen in total" if snap.get("claude_ready")
                else "scanning older agents…")
        self.query_one("#agents-head", Static).update(
            f"[bold]{len(chats)}[/] chat{'s' if len(chats) != 1 else ''} and [bold]{len(live)}[/] "
            f"agent{'s' if len(live) != 1 else ''} working now  [dim]· {seen} · click one, or ↑/↓ "
            f"and Enter, to watch it live[/]"
            + ("" if live or chats else "\n\n[dim]Nothing is working right now.[/]"))
        self.query_one("#agents-foot", Static).update(
            f"\n[dim]'Working' means its log was written to in the last {AGENT_LIVE_SECONDS}s. "
            f"Steps appear as each one finishes.[/]")
        self.query_one("#dhint", Static).update(
            "[dim]↑/↓ choose · enter or click to watch · esc back · 1-8 other panels · q back[/]")

    def action_move(self, step: int) -> None:
        if not self.paths:
            return
        self.selected = (self.selected + step) % len(self.paths)
        blocks = list(self.query(AgentBlock))
        for i, block in enumerate(blocks):
            block.set_class(i == self.selected, "-selected")
        if self.selected < len(blocks):
            blocks[self.selected].scroll_visible()

    def action_open(self) -> None:
        if self.paths:
            self.app.open_agent(self.paths[self.selected])


class AgentView(Detail):
    """Everything one agent has done, from its task on, following new steps
    as they land (like tail -f) while the view is scrolled to the bottom."""

    BINDINGS = Detail.BINDINGS + [("f", "follow", "Follow"), ("end", "follow", "Follow")]

    def __init__(self, path: Path) -> None:
        super().__init__("agent")
        self.path = path
        self.following = True
        self.rendered = -1

    def compose(self) -> ComposeResult:
        yield Static(id="dtitle")
        yield Static(id="agent-head")
        with VerticalScroll(id="detail-body"):
            yield Static(id="detail-content")
        yield Static(id="dhint")

    def on_mount(self) -> None:
        self.app.sampler.watch_agent(self.path)
        self.app.sampler.agents_open += 1
        super().on_mount()

    def on_unmount(self) -> None:
        self.app.sampler.watch_agent(self.path, on=False)
        self.app.sampler.agents_open -= 1

    def redraw(self) -> None:
        app = self.app
        evs, a = app.sampler.agent_view(self.path)
        body = self.query_one("#detail-body", VerticalScroll)
        if self.rendered >= 0:                            # at the bottom = following
            self.following = body.scroll_y >= body.max_scroll_y - 1
        self.query_one("#dtitle", Static).update(
            f"[bold]UtilityBelt[/] [dim]› Agents ›[/] [bold]{escape(a['desc'][:70] or a['id'])}[/]"
            f"   [dim]{datetime.now():%H:%M:%S}[/]")
        self.query_one("#agent-head", Static).update(app.render_agent_head(a, evs))
        if len(evs) != self.rendered:
            self.query_one("#detail-content", Static).update(app.render_agent_feed(evs))
            self.rendered = len(evs)
            if self.following:
                self.call_after_refresh(body.scroll_end, animate=False)
        state = "[green]following new steps[/]" if self.following else "[yellow]paused[/] [dim]· f to follow[/]"
        self.query_one("#dhint", Static).update(
            f"{state}   [dim]scroll up to pause · f / end follow · esc back[/]")

    def action_follow(self) -> None:
        self.following = True
        self.query_one("#detail-body", VerticalScroll).scroll_end(animate=False)


# ---------------------------------------------------------------------- app

class Belt(App):
    CSS = """
    Screen { background: $surface; }
    #topbar { height: 1; padding: 0 2; background: $panel; }
    #grid {
        layout: grid;
        grid-size: 4 2;
        grid-gutter: 0 1;
        padding: 1 1 0 1;
    }
    .card {
        border: round $panel-lighten-3;
        border-title-color: $text-muted;
        border-title-style: bold;
        padding: 0 1;
        height: 100%;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    .card.warn { border: round $warning 70%; border-title-color: $warning; }
    .card.crit { border: round $error; border-title-color: $error; }
    .card:hover { border: round $accent; }
    #hint { height: 1; padding: 0 2; }
    #mini { display: none; padding: 0 1; text-wrap: nowrap; text-overflow: ellipsis; }
    Overview.-mini #topbar, Overview.-mini #grid, Overview.-mini #hint { display: none; }
    Overview.-mini #mini { display: block; height: 100%; }
    #mini.-crit { background: #2a1416; }
    #dtitle { height: 1; padding: 0 2; background: $panel; }
    #detail-body { padding: 1 3; }
    #dhint { height: 1; padding: 0 2; }
    #agent-head { height: auto; padding: 0 3; background: $panel; }
    #agent-blocks { height: auto; }
    .agent-block { height: auto; padding: 0 1; margin: 1 0 0 0; border-left: blank; }
    .agent-block.-done { margin: 0; }
    .agent-block:hover { background: $boost; }
    .agent-block.-selected { border-left: thick $accent; background: $boost; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("1", "detail('cpu')", "CPU"),
        ("2", "detail('memory')", "Memory"),
        ("3", "detail('gpu')", "GPU"),
        ("4", "detail('storage')", "Storage"),
        ("5", "detail('network')", "Network"),
        ("6", "detail('claude')", "Claude"),
        ("7", "detail('agents')", "Agents"),
        ("8", "detail('processes')", "Processes"),
        ("h", "detail('health')", "Health"),
        ("f", "detail('findings')", "Findings"),
        ("m", "toggle_mini", "Mini"),
        ("u", "detail('update')", "Update"),
        ("c", "copy_update", "Copy update command"),
    ]

    TITLE = "UtilityBelt"

    def __init__(self, sampler: Sampler, mini: bool = False) -> None:
        super().__init__()
        self.sampler = sampler
        self.mover = WindowMover()          # lets the top bar drag a title-bar-less window
        self.forced_mode = "mini" if mini else None
        self.update_info: update_check.UpdateInfo | None = None
        self.update_checked_at: datetime | None = None
        self._update_thread: threading.Thread | None = None
        self._docked = False                # docked at the top edge in slide-away mode
        self._shown = True                  # ...and currently in view
        # findings on the strip
        self._strip_rows = 1                # lines the strip drew last time
        self._strip_crit = False            # ...and whether one was critical
        self._confirm = None                # (finding key, deadline): waiting for the second click
        self._acting: dict = {}            # finding key -> until when its button says "working"
        self._mute_menu = None              # finding key whose mute choices are open
        self._show_muted = False
        self._seen: set = set()             # (key, since) of findings shown on the open strip

    def ui_hidden(self) -> bool:
        """Docked and slid up out of sight: nothing on screen can be read."""
        return self._docked and not self._shown

    def _sync_sampler(self) -> None:
        """Keep the sampler's back-off flag in step with what is actually on screen.
        Called from every place that changes _docked or _shown: undocking the strip
        while it is hidden must put the sampler back to full rate, not strand it."""
        if self.ui_hidden():
            self.sampler.hidden.set()
        else:
            self.sampler.hidden.clear()

    def on_mount(self) -> None:
        self.push_screen(Overview())
        self.start_update_check()
        self.set_interval(UPDATE_EVERY, self.start_update_check)

    # ---- is there a newer UtilityBelt on GitHub? (background; never blocks a frame)

    def start_update_check(self) -> None:
        if self._update_thread is not None and self._update_thread.is_alive():
            return

        def run() -> None:
            info = update_check.check()
            if info is not None:  # offline etc. keeps the last answer
                self.update_info = info
            self.update_checked_at = datetime.now()

        self._update_thread = threading.Thread(target=run, name="update-check", daemon=True)
        self._update_thread.start()

    def action_copy_update(self) -> None:
        info = self.update_info
        if info is None or not info.available:
            return
        text = "\n".join(info.commands())
        try:
            subprocess.run(["clip"], input=text, text=True, check=True, timeout=5,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError):
            self.copy_to_clipboard(text)  # terminals that support OSC 52
        self.notify("Update command copied - quit UtilityBelt (q), then paste it into a terminal.")

    def action_detail(self, key: str) -> None:
        self.open_detail(key)

    def action_toggle_mini(self) -> None:
        while isinstance(self.screen, Detail):
            self.pop_screen()
        overview = self.screen
        if isinstance(overview, Overview):
            self.set_small(not overview.is_mini())

    def set_small(self, small: bool) -> None:
        """Small mode: the strip view in a small window in the screen's top-left
        corner. Leaving it puts the window back where and as big as it was."""
        mover = self.mover
        if mover.hwnd is not None:
            x, y, w, h = mover.rect()
            cw, ch = mover.client_size()
            cell = (cw / max(1, self.size.width), ch / max(1, self.size.height))
            if small:
                self._full_rect = (x, y, w, h)
                self._cell, self._edges = cell, (w - cw, h - ch)   # for fitting it to its lines
                area = mover.work_area()
                mover.set_rect(*small_rect(cell, self._edges, area, rows=max(SMALL_ROWS, self._strip_rows)))
                mover.topmost(True)
                self._start_autohide(area[1])
            else:
                self._stop_autohide()
                # Leaving slide-away mode: the window is about to be fully visible
                # again, so drop the docked/hidden state with it or the repaint timer
                # and the sampler would stay backed off on a window in plain view.
                self._docked, self._shown = False, True
                self._sync_sampler()
                mover.topmost(False)
                mover.shadow(True)
                if getattr(self, "_full_rect", None):
                    mover.set_rect(*self._full_rect)
                else:                                       # started small: grow to a standard size
                    mover.set_rect(x, y, round(150 * cell[0]) + w - cw, round(42 * cell[1]) + h - ch)
        self.forced_mode = "mini" if small else "full"
        if isinstance(self.screen, Overview):
            self.screen.apply_mode()
            self.screen.redraw()

    # ---- slide-away small mode: docked at the top edge, the strip slides up out
    # of sight leaving a thin sliver, and slides down when the mouse reaches it.

    def _start_autohide(self, top: int) -> None:
        self._dock_top, self._docked, self._shown = top, True, True
        self._away_since, self._sliding = None, False
        self._sync_sampler()
        self._stop_autohide()
        self._autohide_timer = self.set_interval(0.05, self._autohide)

    def _stop_autohide(self) -> None:
        timer = getattr(self, "_autohide_timer", None)
        if timer is not None:
            timer.stop()
            self._autohide_timer = None

    def _autohide(self) -> None:
        mover = self.mover
        if not self._docked or self._sliding or mover.button_down():
            return                                          # not docked, mid-slide, or being dragged
        x, y, w, h = mover.rect()
        mouse = mover.mouse()
        if not self._shown:
            peek = self.strip_peek()
            if wants_open(mouse, x, w, self._dock_top, peek=peek):
                self._slide(self.shown_y(), shown=True)
            elif y != hidden_y(self._dock_top, h, peek):
                # something to report appeared (or went) while hidden: show one row of it, or not
                self.mover.move_to(x, hidden_y(self._dock_top, h, peek))
            elif mover.shadow_is_on():
                # Windows Terminal redraws its frame after a move and turns the
                # shadow back on; keep it off while the strip is out of sight.
                mover.shadow(False)
            return
        inside = x - 8 <= mouse[0] <= x + w + 8 and y - 8 <= mouse[1] <= y + h + 8
        if inside:
            self._away_since = None
        elif self._away_since is None:
            self._away_since = time.time()
        elif time.time() - self._away_since >= HIDE_AFTER:
            self._mute_menu = self._confirm = None          # half-done clicks do not survive hiding
            self._slide(hidden_y(self._dock_top, h, self.strip_peek()), shown=False)

    def _slide(self, target_y: int, shown: bool) -> None:
        x, y, _, _ = self.mover.rect()
        steps = slide_steps(y, target_y)
        self._sliding, self._shown, self._away_since = True, shown, None
        self._sync_sampler()
        if shown:                                           # whatever it shows now has been seen
            self._seen |= {(f.key, f.since) for f in self.sampler.read().get("findings") or []}
        if isinstance(self.screen, Overview):
            self.screen.redraw()                            # the full strip, or the sliver's line
        if shown:
            self.mover.shadow(True)                         # back before it comes into view

        def step() -> None:
            self.mover.move_to(x, steps.pop(0))
            if not steps:
                timer.stop()
                self._sliding = False
                if not shown:
                    self.mover.shadow(False)                # hidden: no shadow left on screen

        timer = self.set_interval(0.025, step)

    def strip_dropped(self) -> None:
        """After dragging the strip: near the top edge it docks (and hides when
        the mouse leaves); anywhere else it stays put and visible."""
        if self.mover.hwnd is None or getattr(self, "_autohide_timer", None) is None:
            return
        x, y, _, _ = self.mover.rect()
        self._docked = docks(y, self._dock_top)
        self.mover.shadow(True)
        if self._docked:
            self.mover.move_to(x, self.shown_y())
            self._shown, self._away_since = True, None
        self._sync_sampler()

    def bar_action(self, action: str) -> None:
        if action in ("small", "full"):
            self.set_small(action == "small")
        elif action == "minimize" and self.mover.hwnd is not None:
            self.mover.minimize()
        elif action == "close":
            self.exit()

    def open_detail(self, key: str) -> None:
        while isinstance(self.screen, Detail):
            self.pop_screen()
        self.push_screen(AgentsScreen() if key == "agents" else Detail(key))

    def open_agent(self, path: Path) -> None:
        self.push_screen(AgentView(path))

    # ---- agents

    @staticmethod
    def _event_line(e: dict, width: int) -> str:
        """One step as a single line: time, kind mark, the gist."""
        t = f"[dim]{datetime.fromtimestamp(e['ts']):%H:%M:%S}[/]" if e["ts"] else "[dim]--:--:--[/]"
        first = escape(agentlog._first_line(e["text"])[:width])
        kind = e["kind"]
        if kind == "tool":
            return f"{t}  [bold]▸ {escape(e['tool'])}[/] {first}"
        if kind == "output":
            return f"{t}  ✎ {first}"
        if kind == "thinking":
            return f"{t}  [dim italic]· thinking{': ' + first if first else ''}[/]"
        if kind == "result" and e["error"]:
            return f"{t}  [red]✗ {first or 'failed'}[/]"
        if kind == "result":
            return f"{t}  [dim]← {first or '(no output)'}[/]"
        return f"{t}  [dim]task: {first}[/]"

    @staticmethod
    def _since(ts: float) -> str:
        return duration(time.time() - ts) if ts else "—"

    def render_agent_block(self, a: dict, width: int) -> str:
        step = a["now"]
        waited = time.time() - step["since"] if step["since"] else 0
        slow = "yellow" if step["label"] not in ("thinking", "writing") and waited > 120 else "bold"
        where = " · ".join(x for x in (a["chat"] and f"from {escape(a['chat'][:40])}",
                                       a["workflow"] and escape(a["workflow"][:16])) if x)
        errs = f" · [red]{a['errors']} failed[/]" if a["errors"] else ""
        mark = "[cyan]◆[/]" if a.get("kind") == "chat" else "[green]●[/]"
        since = "live chat" if a.get("kind") == "chat" else f"running {self._since(a['started'])}"
        lines = [f"{mark} [bold]{escape(a['desc'][:width - 30] or a['id'])}[/]  "
                 f"[dim]{escape(a['type'])} · {escape(a['model'])} · {since} · "
                 f"{a['tools']} tool calls[/]{errs}",
                 f"  [dim]{where}[/]" if where else "",
                 f"  now  [{slow}]{escape(step['label'][:width - 20])}[/]  [dim]for {duration(waited)}[/]"]
        if a.get("context") or a.get("tokens_out"):
            lines.append(f"  [dim]context {tokens(a['context'])} · {tokens(a['tokens_out'])} written"
                         f"{' (since the feed opened)' if a.get('kind') == 'chat' else ''}[/]")
        lines += [f"  {self._event_line(e, width - 14)}" for e in a["recent"]]
        said = next((e for e in reversed(a["recent"]) if e["kind"] == "output"), None)
        if said and len(a["recent"]) > AGENT_RECENT:          # the detailed feed: its latest words
            text = " ".join(said["text"].split())
            lines.append(f"  [dim]last said:[/] {escape(text[:2 * (width - 16)])}")
        return "\n".join(x for x in lines if x)

    def render_agent_done(self, a: dict) -> str:
        """A finished agent from the full transcript scan: one compact line."""
        desc = self._agent_desc(a["path"]) or a["title"]
        return (f"[dim]○[/] {escape(desc[:60])}  [dim]{a['calls']} calls · last tool "
                f"{escape(a['last_tool'] or '—')} · finished {duration(a['age'])} ago[/]")

    def _agent_desc(self, path: Path) -> str:
        cache = self.__dict__.setdefault("_desc_cache", {})
        if path not in cache:
            try:
                cache[path] = json.loads(path.with_name(path.stem + ".meta.json")
                                         .read_text(encoding="utf-8")).get("description") or ""
            except (OSError, ValueError):
                cache[path] = ""
        return cache[path]

    def render_agent_head(self, a: dict, evs: list[dict]) -> str:
        step = a["now"]
        live = evs and time.time() - max(e["ts"] for e in evs[-3:]) < AGENT_LIVE_SECONDS
        where = " · ".join(x for x in (a["chat"] and f"from {escape(a['chat'][:50])}",
                                       a["workflow"] and escape(a["workflow"])) if x)
        status = (f"now [bold]{escape(step['label'][:90])}[/] [dim]for {self._since(step['since'])}[/]"
                  if live else f"[dim]finished · last step {self._since(step['since'])} ago[/]")
        return (f"[dim]{escape(a['type'])} · {escape(a['model'])} · {where} · started "
                f"{self._since(a['started'])} ago · {a['tools']} tool calls"
                + (f" · [red]{a['errors']} failed[/]" if a["errors"] else "") + f"[/]\n{status}")

    @staticmethod
    def render_agent_feed(evs: list[dict], limit: int = 500) -> str:
        """The whole log, each step labelled by what it is. Output is Claude's
        actual words, full brightness; thinking is dim italic; tools show their
        full input; results are cut to their first lines."""
        out = []
        if len(evs) > limit:
            out.append(f"[dim]… {len(evs) - limit} earlier steps not shown[/]\n")
            evs = evs[-limit:]

        def clip(text: str, n: int) -> tuple[str, str]:
            lines = text.rstrip().splitlines()
            more = f"\n[dim]  (+{len(lines) - n} more lines)[/]" if len(lines) > n else ""
            return escape("\n".join(lines[:n])), more

        hidden_thinking = 0
        for e in evs:
            if e["kind"] == "thinking" and not e["text"].strip():
                hidden_thinking += 1
                continue
            if hidden_thinking:
                out.append(f"[dim italic]thinking{f' ×{hidden_thinking}' if hidden_thinking > 1 else ''}"
                           f" — not recorded[/]\n")
                hidden_thinking = 0
            t = f"[dim]{datetime.fromtimestamp(e['ts']):%H:%M:%S}[/]  " if e["ts"] else ""
            kind = e["kind"]
            if kind == "task":
                text, more = clip(e["text"], 8)
                out.append(f"{t}[bold]TASK[/]\n[dim]{text}[/]{more}\n")
            elif kind == "system":
                inner = e["text"].replace("<system-reminder>", "").replace("</system-reminder>", "").strip()
                text, more = clip(inner, 2)
                out.append(f"{t}[dim]NOTE FROM CLAUDE CODE\n{text}[/]{more}\n")
            elif kind == "thinking":
                out.append(f"{t}[dim italic]THINKING\n{escape(e['text'].rstrip())}[/]\n")
            elif kind == "output":
                out.append(f"{t}[bold]OUTPUT[/]\n{escape(e['text'].rstrip())}\n")
            elif kind == "tool":
                if e["tool"] in ("Bash", "PowerShell"):
                    detail = escape(json.loads(e["full"]).get("command", "")) if e["full"] else ""
                    out.append(f"{t}[bold]▸ {escape(e['tool'])}[/]\n{detail}\n")
                else:
                    text, more = clip(e["full"], 15)
                    out.append(f"{t}[bold]▸ {escape(e['tool'])}[/]  {escape(e['text'])}\n[dim]{text}[/]{more}\n")
            elif kind == "result" and e["error"]:
                text, more = clip(e["text"], 12)
                out.append(f"{t}[red]✗ FAILED\n{text}[/]{more}\n")
            else:
                text, more = clip(e["text"], 6)
                out.append(f"{t}[dim]← RESULT\n{text or '(no output)'}[/]{more}\n")
        if hidden_thinking:
            out.append(f"[dim italic]thinking{f' ×{hidden_thinking}' if hidden_thinking > 1 else ''}"
                       f" — not recorded[/]")
        return "\n".join(out) or "[dim]Nothing in this agent's log yet.[/]"

    # ---- top bar and mini strip

    @staticmethod
    def _alert_text(alerts: list[dict], limit: int = 4) -> str:
        if not alerts:
            return "[green]✓[/] [dim]all good[/]"
        shown = " [dim]·[/] ".join(
            f"[{FILL[a['lvl']]}]{escape(a['short'])}[/]" for a in alerts[:limit])
        more = f" [dim]+{len(alerts) - limit} more[/]" if len(alerts) > limit else ""
        mark = "[red]●[/]" if alerts[0]["lvl"] == CRIT else "[yellow]⚠[/]"
        return f"{mark} {shown}{more}"

    @staticmethod
    def _line(left: str, right: str, width: int) -> Text:
        """left-aligned markup and a right-aligned tail on one line, the left
        side cut short (with an ellipsis) rather than wrapping."""
        l, r = Text.from_markup(left), Text.from_markup(right)
        room = width - r.cell_len - 2
        if l.cell_len > room:
            l.truncate(max(0, room), overflow="ellipsis")
        l.append(" " * max(1, width - l.cell_len - r.cell_len))
        l.append_text(r)
        return l

    @staticmethod
    def _buttons(bar) -> str:
        """The hover buttons as markup, or nothing when the mouse is elsewhere."""
        if bar is None or not getattr(bar, "hover", False):
            return ""
        marks = [f"[bold]{m}[/]" if a == bar.hot else f"[dim]{m}[/]" for m, a in bar.BUTTONS]
        return "   " + "  ".join(marks)

    def render_topbar(self, alerts: list[dict], width: int, bar=None, found=None) -> Text:
        live = [f for f in found or [] if not f.muted]
        lead = ""
        if live:
            sev = SEV_STYLE[live[0].severity]
            lead = (f"[{sev['mark']}]{SEV_MARK[live[0].severity]} {len(live)} "
                    f"finding{'s' if len(live) > 1 else ''}[/] [dim](f)[/]   ")
        return self._line(f"[bold]UtilityBelt[/]   {lead}{self._alert_text(alerts)}",
                          f"[dim]{datetime.now():%H:%M:%S}[/]{self._buttons(bar)}", width)

    # ---- the strip: findings when something is wrong, one quiet line when not

    def render_mini(self, snap: dict, alerts: list[dict], width: int, bar=None) -> Text:
        """Findings on top (worst first), each a title line with its buttons and a
        line of why and what to do; the quiet row below. Nothing wrong: only the
        quiet row. Slid out of sight: one centred line for the sliver.
        Records where each button is (bar.hits) for MiniStrip's clicks."""
        found = snap.get("findings") or []
        live = [f for f in found if not f.muted]
        muted = [f for f in found if f.muted]
        hits: dict = {}
        lines: list[Text] = []

        if self.ui_hidden() and self.wants_attention():    # only the bottom row shows
            rows = max(1, self.screen.size.height if self.screen else 1)
            lines = [Text("")] * (rows - 1) + [self._sliver_line(snap, live, muted, width)]
            if bar is not None:
                bar.hits, bar.BUTTON_ROW = {}, -1
            return Text("\n").join(lines)

        for f in live[:STRIP_MAX_FINDINGS]:
            sev = SEV_STYLE[f.severity]
            head = f"[{sev['mark']}]{SEV_MARK[f.severity]}[/] "
            if f.severity == "critical":
                head += f"[{sev['mark']}]ACT NOW[/]  "
            head += f"[{sev['title']}]{escape(f.title)}[/]"
            pieces = []
            if f.action is not None:
                if f.key in self._acting and time.time() < self._acting[f.key]:
                    pieces.append(("[dim] working… [/]", 10, None))
                elif self._confirm and self._confirm[0] == f.key and time.time() < self._confirm[1]:
                    text = f.action.confirm_label or f"{f.action.label}? click again"
                    pieces.append((f"[bold reverse red] {escape(text)} [/]", len(text) + 2, ("act", f.key)))
                else:
                    pieces.append((f"[{sev['button']}] {escape(f.action.label)} [/]",
                                   len(f.action.label) + 2, ("act", f.key)))
            pieces.append(("[on grey23] mute [/]", 6, ("mute", f.key)))
            row = len(lines)
            lines.append(self._line_with_buttons(head, pieces, width, row, hits))
            buttons_at = min((x0 for x0, _, _ in hits.get(row, ())), default=width)
            hits.setdefault(row, []).append((0, max(0, buttons_at - 1), ("view", "findings")))
            if self._mute_menu != f.key:
                hits.setdefault(row + 1, []).append((0, width, ("view", "findings")))
            if self._mute_menu == f.key:
                opts = [("1 hour", "1h")] + ([] if f.severity == "critical" else
                                             [("until restart", "reboot"), ("forever", "forever")])
                opts.append(("cancel", "cancel"))
                lines.append(self._left_buttons("   [dim]mute for:[/] ", 13,
                                                [(f" {t} ", ("mute_for", f.key, how)) for t, how in opts],
                                                width, row + 1, hits))
            else:
                lines.append(self._line(f"   [{sev['why']}]{escape(f.because)}[/] [dim]→ {escape(f.fix)}[/]",
                                        "", width))
        if self._show_muted:
            for f in muted:
                row = len(lines)
                lines.append(self._line_with_buttons(
                    f"[dim]○ {escape(f.title)} · muted[/]", [("[on grey23] unmute [/]", 8, ("unmute", f.key))],
                    width, row, hits))

        # the quiet row: demoted below the findings, the whole strip when there are none
        cited = {c for f in live for c in f.cites}
        crit = any(f.severity == "critical" for f in live)
        hover = bool(bar is not None and getattr(bar, "hover", False))
        right = []
        if muted:
            label = f"{len(muted)} muted"
            right.append((f"[dim underline]{label}[/]", len(label), ("muted", "")))
        info = self.update_info
        if info is not None and info.available:
            right.append(("[yellow]⬆ update (u)[/]", 12, None))
        if "clock" in self._quiet_row():
            right.append((f"[dim]{datetime.now():%H:%M}[/]", 5, None))
        # columns the left part may use before it reaches the right-hand items
        room = (width - Text.from_markup(self._buttons(bar)).cell_len
                - sum(n for _, n, _ in right) - 2 * len(right) - 2)
        segs = []                                           # (markup, cells, view it opens)
        if not live:
            segs.append(("[green]●[/] [dim]all clear[/]", 11, "findings"))
        more = len(live) - STRIP_MAX_FINDINGS
        if more > 0:
            segs.append((f"[bold]+{more} more[/] [dim](f)[/]", len(f"+{more} more (f)"), "findings"))
        segs += self._quiet_items(snap, cited, crit, hover)
        if hover:                                           # views with no number of their own,
            for v in ("findings", "health"):                # where there is room for the whole word
                used = sum(n for _, n, _ in segs) + 2 * len(segs)
                if used + len(v) <= room and not any(view == v for _, _, view in segs):
                    segs.append((f"[underline]{v}[/]", len(v), v))
        x, parts = 0, []
        for markup, cells, view in segs:
            if parts:
                x += 2
            parts.append(markup)
            if view and x < room:                           # a link cut short still opens, up to the cut
                hits.setdefault(len(lines), []).append((x, min(x + cells, room), ("view", view)))
            x += cells
        left = "  ".join(parts)
        row = len(lines)
        btns = self._buttons(bar)
        lines.append(self._line_with_buttons(left, right, width - Text.from_markup(btns).cell_len,
                                             row, hits, tail=btns))
        self._strip_rows = len(lines)
        pad = (self.screen.size.height if self.screen else 0) - len(lines)
        if self._docked and pad > 0:                       # docked: lines sit at the bottom (see shown_y)
            lines = [Text("")] * pad + lines
            hits = {r + pad: v for r, v in hits.items()}
            row += pad
        if bar is not None:
            bar.hits, bar.BUTTON_ROW = hits, row
        self._strip_crit = crit
        return Text("\n").join(lines)

    def _quiet_row(self) -> list:
        engine = self.sampler.engine
        items = engine.cfg.quiet_row if engine else ["agents", "vram", "spend", "disk", "clock"]
        return items if "lab" in items else items + ["lab"]   # shows itself only while a lab search runs

    QUIET_VIEWS = {"agents": "agents", "vram": "gpu", "spend": "claude", "disk": "storage", "cpu": "cpu",
                   "gpu": "gpu", "mem": "memory", "net": "network", "gputemp": "gpu", "cputemp": "cpu", "lab": "lab"}

    def _quiet_items(self, snap: dict, cited: set, crit: bool, hover: bool = False) -> list:
        """The quiet row's values as (markup, cells, view): low contrast, except one a
        finding is about; underlined while the mouse is over the strip, since each
        opens its panel."""
        s = self.sampler
        hot = "bold red" if crit else "bold yellow"
        out = []
        for item in self._quiet_row():
            label, value = item, None
            if item == "agents":
                if not snap.get("claude_ready"):
                    value = "scanning…"
                else:
                    states = Counter(x["state"] for x in snap["claude"]["sessions"])
                    working = states["working"] + sum(1 for a in snap.get("agents") or [] if a["live"])
                    bits = [f"{states['waiting']} waiting" if states["waiting"] else "",
                            f"{states['approval']} need ok" if states["approval"] else "",
                            f"{working} working" if working else ""]
                    value = " ".join(b for b in bits if b) or "idle"
            elif item == "vram":
                total = snap.get("vram_total") or 0
                if total:
                    value = f"{(snap.get('vram_used') or 0) / 1024 ** 3:.1f}/{total / 1024 ** 3:.1f}"
            elif item == "spend":
                if snap.get("claude_ready"):
                    value = f"${snap['claude']['today_cost']:,.0f}"
            elif item == "disk":
                tight = sorted(snap.get("disks") or [], key=lambda d: d["free"])
                if tight:
                    label, value = tight[0]["device"], human_bytes(tight[0]["free"])
            elif item == "cpu":
                value = f"{s.cpu.now:.0f}%"
            elif item == "gpu":
                value = f"{s.gpu.now:.0f}%"
            elif item == "mem":
                vm = snap.get("mem")
                value = f"{vm.percent:.0f}%" if vm else None
            elif item == "net":
                value = f"↓{rate(s.net_down.now)} ↑{rate(s.net_up.now)}"
            elif item == "gputemp":
                t = (snap.get("gpu_sensors") or {}).get("edge")
                value = f"{t}°C" if t is not None else None
            elif item == "cputemp":
                t = (snap.get("cpu_sensors") or {}).get("temp")
                value = f"{t:.0f}°C" if t is not None else None
            elif item == "lab":
                lab = snap.get("lab")
                if lab:
                    value = "paused" if lab["state"] == "paused" else f"gen {lab['generation']}"
            if value is None:
                continue
            style = hot if item in cited else "grey70"
            if hover:
                style += " underline"
            out.append((f"[grey50{' underline' if hover else ''}]{escape(label)}[/] [{style}]{escape(value)}[/]",
                        Text(f"{label} {value}").cell_len, self.QUIET_VIEWS.get(item)))
        return out

    def _sliver_line(self, snap: dict, live: list, muted: list, width: int) -> Text:
        """What the slid-away strip still shows: how many findings, chats
        waiting, the time. Centred, like a tab hanging from the top edge."""
        worst = live[0].severity
        sev = SEV_STYLE[worst]
        bits = [f"[{sev['mark']}]{SEV_MARK[worst]} {len(live) + len(muted)}[/]"]
        if snap.get("claude_ready"):
            waiting = sum(1 for x in snap["claude"]["sessions"] if x["state"] == "waiting")
            if waiting:
                bits.append(f"[grey70]{waiting} waiting[/]")
        bits.append(f"[dim]{datetime.now():%H:%M}[/]")
        t = Text.from_markup("  [grey30]│[/]  ".join(bits))
        t.pad_left(max(0, (width - t.cell_len) // 2))
        return t

    def sliver_signature(self, snap: dict) -> tuple:
        """Changes exactly when the hidden strip's one line would."""
        found = snap.get("findings") or []
        live = [f for f in found if not f.muted]
        waiting = (sum(1 for x in snap["claude"]["sessions"] if x["state"] == "waiting")
                   if snap.get("claude_ready") else 0)
        return (len(found), live[0].severity if live else "", waiting, datetime.now().strftime("%H:%M"))

    def _line_with_buttons(self, left: str, pieces: list, width: int, row: int, hits: dict,
                           tail: str = "") -> Text:
        """`left` cut to fit, then right-aligned buttons; each button's columns go in hits."""
        right = "  ".join(m for m, _, _ in pieces)
        rlen = sum(n for _, n, _ in pieces) + 2 * max(0, len(pieces) - 1)
        x = width - rlen
        for _, n, hit in pieces:
            if hit is not None:
                hits.setdefault(row, []).append((x, x + n, hit))
            x += n + 2
        return self._line(left, right + tail, width + Text.from_markup(tail).cell_len)

    @staticmethod
    def _left_buttons(prefix: str, prefix_len: int, pieces: list, width: int, row: int, hits: dict) -> Text:
        """A row of buttons from the left, after a label."""
        x, parts = prefix_len, []
        for text, hit in pieces:
            parts.append(f"[on grey23]{escape(text)}[/]")
            hits.setdefault(row, []).append((x, x + len(text), hit))
            x += len(text) + 2
        t = Text.from_markup(prefix + "  ".join(parts))
        t.truncate(width, overflow="ellipsis")
        return t

    def strip_hit(self, hit: tuple) -> None:
        """A click on one of the strip's finding buttons."""
        if hit[:2] == ("view", "lab"):
            threading.Thread(target=self.open_zoo, daemon=True).start()
            return
        engine, kind, key = self.sampler.engine, hit[0], hit[1]
        if engine is None:
            return
        f = {x.key: x for x in self.sampler.read().get("findings") or []}.get(key)
        if kind == "act" and f is not None and f.action is not None:
            pending = self._confirm and self._confirm[0] == key and time.time() < self._confirm[1]
            if f.action.confirm and not pending:
                self._confirm = (key, time.time() + CONFIRM_SECONDS)
            else:
                self._confirm = None
                self._acting[key] = time.time() + 10
                engine.run_action(f, done=self._reevaluate)
        elif kind == "mute":
            self._mute_menu = None if self._mute_menu == key else key
        elif kind == "mute_for":
            self._mute_menu = None
            if hit[2] != "cancel" and f is not None:
                engine.mute(key, hit[2], f.severity)
                self._reevaluate()
        elif kind == "muted":
            self._show_muted = not self._show_muted
        elif kind == "view":
            self.open_view(key)
            return
        elif kind == "unmute":
            engine.unmute(key)
            self._reevaluate()
        if isinstance(self.screen, Overview):
            self.screen.redraw()

    def open_zoo(self) -> None:
        """The strip's "lab" item: the Search Zoo page (the lab search as live creatures) in the
        browser; when the zoo is not running, start it from the screener repo (it opens the page)."""
        engine = self.sampler.engine
        cfg = engine.cfg.lab if engine is not None else beltconfig.DEFAULTS["lab"]
        url = cfg.get("zoo") or "http://127.0.0.1:8784"
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/api/ping", timeout=3.0):  # a busy zoo can be slow
                pass
        except OSError:
            repo = Path(cfg.get("repo") or "")
            exe = repo / ".venv" / "Scripts" / "pythonw.exe"
            if exe.is_file():
                subprocess.Popen([str(exe), "-m", "lab.zoo.server", "--open"], cwd=str(repo),
                                 creationflags=0x00000008 | 0x00000200 | 0x08000000)  # detached, own group, no window
                return
        import webbrowser
        webbrowser.open(url)

    def open_view(self, key: str) -> None:
        """Open a panel from the strip: the strip is too small to show one, so
        the window goes back to its full size first."""
        screen = self.screen
        if isinstance(screen, Overview) and screen.is_mini():
            self.set_small(False)
        self.open_detail(key)

    def _reevaluate(self) -> None:
        """Work the findings out again now (off the UI thread), not at the next pass."""
        threading.Thread(target=self.sampler._evaluate, daemon=True).start()

    def _visible(self, rows: int) -> int:
        """Pixels from the window's bottom edge up to the top of its last `rows`
        text rows: how much of a docked strip must be on screen to show them."""
        c = self._cell[1]
        _, ch = self.mover.client_size()
        slack = max(0.0, ch - max(1, self.size.height) * c)   # the terminal's padding, top + bottom
        return round(rows * c + slack / 2 + self._edges[1] / 2 + 4)

    def strip_peek(self) -> int:
        """How much of the hidden strip stays on screen: a sliver, or its whole
        last row (count, waiting, time) while there is something to report."""
        if not self.wants_attention() or not getattr(self, "_cell", None):
            return PEEK
        return self._visible(1)

    def wants_attention(self) -> bool:
        """Should the hidden strip keep a row showing? For a critical finding,
        always; for a warning, until the strip has been opened since it appeared;
        for info, never (those can sit there for hours: a permanent row over the
        top of every window is worse than the thing it reports)."""
        for f in self.sampler.read().get("findings") or []:
            if f.muted:
                continue
            if f.severity == "critical":
                return True
            if f.severity == "warn" and (f.key, f.since) not in self._seen:
                return True
        return False

    def shown_y(self) -> int:
        """Where a docked, slid-down strip sits. Its lines are drawn at the bottom
        of the window; a window taller than its lines (Windows Terminal will not go
        below about 4 rows) is pushed up past the screen edge by the difference,
        so 'all clear' is one line on screen, not a line and three empty rows."""
        _, _, _, h = self.mover.rect()
        rows = max(1, self._strip_rows or 1)
        if not getattr(self, "_cell", None) or rows >= self.size.height:
            return self._dock_top
        return self._dock_top - max(0, h - self._visible(rows))

    def fit_strip(self) -> None:
        """Small mode: make the window as tall as the strip's lines (but not under
        the terminal's minimum), and keep a docked strip showing just its lines.
        Pixels per row are only roughly known, as the terminal pads its text area:
        resize, see how many rows that gave, and correct by whole rows."""
        mover = self.mover
        if (mover.hwnd is None or getattr(self, "_autohide_timer", None) is None
                or not getattr(self, "_cell", None) or getattr(self, "_sliding", False)):
            return
        rows = max(SMALL_ROWS, getattr(self, "_min_rows", 0), self._strip_rows or 1)
        x, y, w, h = mover.rect()
        pending = getattr(self, "_fit_pending", None)
        if rows != self.size.height:
            if pending and pending[0] == h and pending[1] == self.size.height:
                if time.time() - pending[2] < 1.5:
                    return                                     # asked already; the resize is on its way
                if rows < self.size.height:                    # asked to shrink and nothing happened:
                    self._min_rows = self.size.height          # the terminal's own minimum
                    return
            want = max(self._edges[1] + 4, round(h + (rows - self.size.height) * self._cell[1]))
            self._fit_pending = (want, self.size.height, time.time())
            if self._docked:
                y = hidden_y(self._dock_top, want, self.strip_peek()) if not self._shown else y
            mover.set_rect(x, y, w, want)
            return
        if self._docked:                                       # right size: right place?
            target = self.shown_y() if self._shown else hidden_y(self._dock_top, h, self.strip_peek())
            if y != target:
                mover.move_to(x, target)

    @staticmethod
    def _claude_summary(snap: dict) -> str:
        if not snap.get("claude_ready"):
            return "[dim]Claude: scanning…[/]"
        c = snap["claude"]
        states = Counter(x["state"] for x in c["sessions"])
        live = sum(1 for a in snap.get("agents") or [] if a["live"])
        bits = [f"Claude [bold]${c['today_cost']:,.0f}[/] today"]
        if states["waiting"]:
            bits.append(f"[bold]{states['waiting']}[/] waiting on you")
        if states["approval"]:
            bits.append(f"[yellow]{states['approval']} may need approval[/]")
        if states["working"] or live:
            bits.append(f"{states['working'] + live} working")
        return " · ".join(bits)

    # ---- standard cards: (lines, severity)

    @staticmethod
    def _graph_rows(h: int, fixed: int) -> int:
        """Graphs take whatever height the card has left over."""
        return max(1, min(12, h - fixed))

    def _card_cpu(self, snap, s, w, h):
        lvl = level(s.cpu.recent(10), 85, 95)
        freq = snap.get("cpu_freq") or 0
        busiest = max((c.now for c in s.cpu_cores), default=0)
        lines = [f"[{NUM[lvl]}]{s.cpu.now:.0f}%[/]{self._cpu_temp(snap)}  [dim]{freq / 1000:.1f} GHz[/]"]
        lines += chart(s.cpu.points, w, self._graph_rows(h, 2), lvl=lvl)
        lines.append(f"[dim]busiest core {busiest:.0f}% · 5 min avg {s.cpu.mean:.0f}%[/]")
        return lines, lvl

    def _card_memory(self, snap, s, w, h):
        vm = snap.get("mem")
        if not vm:
            return ["[dim]sampling…[/]"], OK
        lvl = level(s.mem.recent(30), 85, 93)
        lines = [f"[{NUM[lvl]}]{vm.percent:.0f}%[/]  [dim]{human_bytes(vm.used)} of "
                 f"{human_bytes(vm.total)}[/]"]
        lines += chart(s.mem.points, w, self._graph_rows(h, 2), lvl=lvl)
        running, rated = ram_speed(snap)
        if running and rated and running < rated - 100:
            lines.append(f"[yellow]RAM {running} MT/s · rated {rated}[/]")
            lvl = worst(lvl, WARN)
        elif running:
            lines.append(f"[dim]RAM {running} MT/s · {human_bytes(vm.available)} free[/]")
        else:
            lines.append(f"[dim]{human_bytes(vm.available)} free[/]")
        return lines, lvl

    def _card_gpu(self, snap, s, w, h):
        used = snap.get("vram_used") or 0
        total = snap.get("vram_total") or 0
        if total == 0 and s.gpu.now == 0:
            return ["[dim]waiting for counters…[/]"], OK
        vpct = used / total * 100 if total else 0
        v_l = level(vpct, 90, 97)
        sensors = snap.get("gpu_sensors")
        heat = ""
        if sensors:
            fan = f"  [dim]fan {sensors['fan_pct']}%[/]" if sensors.get("fan_pct") is not None else ""
            heat = f" {self._gpu_temp(snap)}{fan}"
            v_l = worst(v_l, gpusensors.heat_level(sensors))
        lines = [f"[bold]{s.gpu.now:.0f}%[/] [dim]load[/]   VRAM [{NUM[level(vpct, 90, 97)]}]{vpct:.0f}%[/]{heat}"]
        lines += chart(s.gpu.points, w, self._graph_rows(h, 2))
        ol = snap.get("ollama")
        if ol is None:
            lines.append("[dim]checking Ollama…[/]")
        elif not ol["up"]:
            lines.append("[dim]Ollama not running[/]")
        elif ol["models"]:
            m = ol["models"][0]
            more = f" +{len(ol['models']) - 1}" if len(ol["models"]) > 1 else ""
            lines.append(f"[dim]ollama[/] {escape(m['name'])} [dim]{human_bytes(m['vram'])}{more}[/]")
        else:
            lines.append("[dim]Ollama idle · no model loaded[/]")
        return lines, v_l

    @staticmethod
    def _cpu_temp(snap) -> str:
        """' 62°C' coloured by heat, or '' when LibreHardwareMonitor isn't running."""
        sensors = snap.get("cpu_sensors")
        if not sensors:
            return ""
        t = sensors["temp"]
        return f" [{NUM[cpusensors.heat_level(t)]}]{t:.0f}°C[/]"

    @staticmethod
    def _gpu_temp(snap) -> str:
        """' 63°C' coloured by the card's worst temperature, or '' without sensors."""
        sensors = snap.get("gpu_sensors")
        if not sensors or sensors.get("edge") is None:
            return ""
        return f" [{NUM[gpusensors.heat_level(sensors)]}]{sensors['edge']}°C[/]"

    def _card_storage(self, snap, s, w, h):
        disks = snap.get("disks") or []
        if not disks:
            return ["[dim]sampling…[/]"], OK
        lines, lvl = [], OK
        bw = max(4, w - 17)
        for d in disks[:max(1, h - 1)]:
            dl = disk_level(d)
            lvl = worst(lvl, dl)
            lines.append(f"{d['device']:<3}{bar(d['percent'] / 100, bw, dl)} "
                         f"[{NUM[dl] if dl != OK else 'default'}]{human_bytes(d['free']):>8}[/] [dim]free[/]")
        lines.append(f"[dim]read {rate(s.disk_read.now)} · write {rate(s.disk_write.now)}[/]")
        return lines, lvl

    def _card_network(self, snap, s, w, h):
        lines = [f"↓ [bold]{rate(s.net_down.now)}[/]   ↑ [bold]{rate(s.net_up.now)}[/]"]
        top = max(s.net_down.peak, s.net_up.peak, 64 * 1024)
        lines += chart(s.net_down.points, w, self._graph_rows(h, 2), top=top)
        ts = snap.get("tailscale")
        tail = [f"[dim]peak ↓ {rate(s.net_down.peak)}[/]"]
        if ts is not None:
            tail.insert(0, "Tailscale " + ("[green]connected[/]" if ts else "[yellow]off[/]"))
        lines.append(" [dim]·[/] ".join(tail))
        return lines, OK

    def _card_claude(self, snap, s, w, h):
        if not snap.get("claude_ready"):
            return ["[dim]scanning transcripts…[/]"], OK
        c = snap["claude"]
        lines = [f"[bold]${c['today_cost']:,.2f}[/] [dim]today · {c['today_calls']:,} calls[/]"]
        order = {"approval": 0, "waiting": 1, "working": 2}
        active = sorted((x for x in c["sessions"] if x["state"] in order),
                        key=lambda x: (order[x["state"]], x["age"]))
        if not active:
            lines.append("[dim]no chats active in the last 12h[/]")
        for x in active[:h - 1]:
            mark = {"approval": "[yellow]?[/]", "waiting": "[bold]◆[/]",
                    "working": "[green]●[/]"}[x["state"]]
            lines.append(f"{mark} {escape(x['title'])}")
        lvl = WARN if any(x["state"] == "approval" for x in active) else OK
        return lines, lvl

    def _card_agents(self, snap, s, w, h):
        if not snap.get("claude_ready"):
            return ["[dim]scanning transcripts…[/]"], OK
        agents = snap.get("agents") or []
        if not agents:
            return ["[dim]no agent transcripts[/]"], OK
        live = snap.get("live_agents") or []
        lines = [f"[bold]{len(live)}[/] [dim]working now · 7 to watch[/]"]
        for a in live[:max(1, (h - 1) // 2)]:           # two lines each: what it is, what it's doing
            step = a["now"]
            lines.append(f"[green]●[/] {escape(a['desc'] or a['id'])}")
            lines.append(f"  [dim]{escape(step['label'])} · {self._since(step['since'])}[/]")
        if not live:
            lines.append(f"[dim]last one finished {duration(agents[0]['age'])} ago[/]")
        return lines, OK

    def _card_processes(self, snap, s, w, h):
        procs = snap.get("procs") or []
        if not procs:
            return ["[dim]sampling…[/]"], OK
        left = snap.get("leftovers") or []
        hp = snap.get("helpers") or {}
        tail = []
        if hp.get("sessions"):
            tail.append(f"[dim]{hp['sessions']} Claude chats loaded · "
                        f"{hp['count']} helpers · {human_bytes(hp['rss'])}[/]")
        if left:
            tail.append(f"[yellow]{len(left)} leftover process{'es' if len(left) > 1 else ''}[/]")
        top = [f"{escape(p['name'][:16]):<16} [bold]{p['cpu']:4.1f}%[/] [dim]{human_bytes(p['rss'])}[/]"
               for p in procs[:max(1, min(5, h - len(tail) - 1))]]
        gap = [""] * max(0, h - len(top) - len(tail))
        return top + gap + tail, WARN if left else OK

    # ---- detail bodies

    def render_detail(self, key: str, snap: dict, width: int) -> str:
        try:
            return getattr(self, f"_detail_{key}")(snap, self.sampler, width)
        except Exception as exc:
            return f"[red]{escape(type(exc).__name__)}: {escape(str(exc))}[/]"

    def _detail_findings(self, snap, s, width) -> str:
        engine = s.engine
        if engine is None:
            return "[dim]Findings are off in this window.[/]"
        found = sorted(snap.get("findings") or [], key=lambda f: f.muted)   # muted last
        out = []
        if not any(not f.muted for f in found):
            out.append("[green]●[/] All clear: nothing needs you right now.\n")
        for f in found:
            sev = SEV_STYLE[f.severity]
            muted = "  [dim](muted)[/]" if f.muted else ""
            out += [f"[{sev['mark']}]{SEV_MARK[f.severity]} {f.severity.upper()}[/]  "
                    f"[{sev['title']}]{escape(f.title)}[/]{muted}  [dim]since {datetime.fromtimestamp(f.since):%H:%M}[/]",
                    f"   [dim]why[/]  {escape(f.because)}",
                    f"   [dim]fix[/]  {escape(f.fix)}"]
            if f.action is not None:
                out.append(f"   [dim]button on the strip:[/] {escape(f.action.label)}")
            out.append("")
        out += ["", "[bold]Cleared recently[/]  [dim](newest first; kept across restarts)[/]"]
        if not engine.history:
            out.append("  [dim]Nothing has come and gone yet.[/]")
        for e in reversed(engine.history):
            sev = SEV_STYLE.get(e["severity"], SEV_STYLE["info"])
            start, end = e.get("since") or 0, e.get("cleared_at") or 0
            out.append(f"  [dim]{datetime.fromtimestamp(end):%a %H:%M}[/]  [{sev['mark']}]{SEV_MARK.get(e['severity'], '●')}[/] "
                       f"{escape(e['title'])}  [dim]for {duration(end - start)}"
                       f"{' · muted' if e.get('muted') else ''}[/]")
        out += ["", f"[dim]Settings, thresholds and mutes: {escape(str(engine.cfg.path))}"
                    " · every rule: python belt.py rules --list[/]"]
        return "\n".join(out)

    def _detail_update(self, snap, s, width) -> str:
        info, at = self.update_info, self.update_checked_at
        when = f"[dim]Last checked {at:%H:%M}; checks again every {UPDATE_EVERY // 3600} hours.[/]" if at else ""
        if info is None:
            if at is None:
                return "[dim]Checking GitHub for a newer version…[/]"
            return ("Can't check for updates from here: git isn't installed, this copy wasn't made "
                    "with git clone, or GitHub couldn't be reached.\n\n" + when)
        if not info.available:
            return f"[green]✓[/] UtilityBelt is up to date.\n\n{when}"
        n = info.behind
        lines = [f"[bold yellow]⬆ {n} new change{'s' if n != 1 else ''} on GitHub[/]", "", "[bold]What's new[/]"]
        lines += [f"  • {escape(t)}" for t in info.titles]
        if n > len(info.titles):
            lines.append(f"  [dim]…and {n - len(info.titles)} more[/]")
        lines += ["", "[bold]To update[/]", "  1. Quit UtilityBelt (q).",
                  "  2. Open a terminal and run:"]
        lines += [f"       [bold]{escape(cmd)}[/]" for cmd in info.commands()]
        if info.deps_changed:
            lines.append("     [dim](the second line installs new packages this version needs)[/]")
        lines += ["  3. Start UtilityBelt again.", "",
                  "[dim]c copies the command" + ("s" if len(info.commands()) > 1 else "") + ".[/]", when]
        return "\n".join(lines)

    @staticmethod
    def _graph(points, width, lvl=OK, top=100.0, label="") -> list[str]:
        rows = chart(points, min(width, 120), 5, top=top, lvl=lvl)
        return rows + [f"[dim]{'5 minutes ago':<{min(width, 120) - 3}}now[/]"
                       + (f"  [dim]{label}[/]" if label else "")]

    def _detail_cpu(self, snap, s, width) -> str:
        lvl = level(s.cpu.recent(10), 85, 95)
        out = [f"[{NUM[lvl]}]{s.cpu.now:.1f}%[/]  [dim]{snap['cpu_physical']} cores / "
               f"{snap['cpu_logical']} threads · 5 min avg {s.cpu.mean:.1f}% · "
               f"peak {s.cpu.peak:.1f}%[/]", ""]
        out += self._graph(s.cpu.points, width, lvl)
        sensors = snap.get("cpu_sensors")
        if sensors:
            tl = cpusensors.heat_level(sensors["temp"])
            out += ["", f"[bold]temperature[/]  [{NUM[tl]}]{sensors['temp']:.1f}°C[/]  [dim]5 min peak "
                        f"{s.cpu_temp.peak:.0f}°C · AMD X3D chips slow down at 89°C[/]"]
            out += self._graph(s.cpu_temp.points, width, tl, top=100.0)
            others = "  ".join(f"{escape(k)} {v:.1f}°C" for k, v in sensors["temps"].items())
            power = f" · package {sensors['power']:.0f} W" if sensors.get("power") is not None else ""
            out.append(f"  [dim]{others}{power}[/]")
        out += ["", "[bold]per thread[/]"]
        for i, core in enumerate(s.cpu_cores):
            cl = level(core.now, 85, 95)
            out.append(f"  {i:>2}  {bar(core.now / 100, 26, cl)} {core.now:5.1f}% "
                       f"[dim]avg {core.mean:4.1f}%[/]")
        freq, fmax = snap.get("cpu_freq") or 0, snap.get("cpu_freq_max") or 0
        info = snap.get("sysinfo") or {}
        out += ["", "[bold]clocks and interrupts[/]"]
        if info.get("cpu"):
            out.append(f"  processor        {escape(info['cpu'])}")
        out += [f"  frequency        {freq / 1000:.2f} GHz"
                + (f" [dim]of {fmax / 1000:.2f} GHz base[/]" if fmax else ""),
                f"  context switches {snap.get('ctx_switches', 0):,}/s",
                f"  interrupts       {snap.get('interrupts', 0):,}/s",
                f"  uptime           {duration(time.time() - snap.get('boot', time.time()))}"]
        procs = snap.get("procs") or []
        out += ["", "[bold]top by CPU[/]"]
        for p in procs[:12]:
            out.append(f"  {p['cpu']:5.1f}%  [dim]{p['pid']:>7}[/]  {escape(p['name'][:28]):<28}"
                       f"[dim]{human_bytes(p['rss'])}[/]")
        if not sensors:
            out += ["", "[dim]No CPU temperature: Windows only gives it to programs with a kernel driver. Run "
                    "LibreHardwareMonitor with its web server on (port 8085) and it shows up here.[/]"]
        return "\n".join(out)

    def _detail_memory(self, snap, s, width) -> str:
        vm, sw = snap.get("mem"), snap.get("swap")
        if not vm:
            return "[dim]sampling…[/]"
        lvl = level(s.mem.recent(30), 85, 93)
        out = [f"[{NUM[lvl]}]{vm.percent:.1f}%[/]  [dim]{human_bytes(vm.used)} of "
               f"{human_bytes(vm.total)} used · {human_bytes(vm.available)} available[/]", ""]
        out += self._graph(s.mem.points, width, lvl)
        out += ["", "[bold]breakdown[/]",
                f"  total      {human_bytes(vm.total)}",
                f"  used       {human_bytes(vm.used)}",
                f"  available  {human_bytes(vm.available)}"]
        if sw:
            out.append(f"  swap       {human_bytes(sw.used)} of {human_bytes(sw.total)} "
                       f"[dim]({sw.percent:.0f}%)[/]")
        mods = (snap.get("sysinfo") or {}).get("ram") or []
        if isinstance(mods, dict):
            mods = [mods]
        if mods:
            running, rated = ram_speed(snap)
            out += ["", "[bold]modules[/]"]
            for m in mods:
                out.append(f"  {escape(m.get('slot') or '?'):<10} {human_bytes(m.get('size') or 0):>8}  "
                           f"{escape(m.get('maker') or '')} {escape(m.get('part') or '')}  "
                           f"[dim]{m.get('configured') or m.get('speed')} MT/s[/]")
            if running and rated and running < rated - 100:
                out += ["", f"[yellow]Running at {running} MT/s but rated for {rated}.[/] "
                        "EXPO is probably off. In the BIOS: F7 for Advanced Mode, then "
                        "Ai Tweaker > Ai Overclock Tuner > EXPO I, then F10 to save.",
                        "[dim]The first boot after that trains the memory and can sit on a "
                        "black screen for a few minutes. Let it finish.[/]"]
            elif rated:
                out.append(f"  [green]running at its rated {rated} MT/s[/]")
        out += ["", "[bold]top by memory[/]"]
        for p in sorted(snap.get("procs") or [], key=lambda r: -r["rss"])[:15]:
            frac = p["rss"] / vm.total if vm.total else 0
            out.append(f"  {bar(frac, 12)} {human_bytes(p['rss']):>9}  "
                       f"[dim]{p['pid']:>7}[/]  {escape(p['name'][:30])}")
        return "\n".join(out)

    def _detail_gpu(self, snap, s, width) -> str:
        used = snap.get("vram_used") or 0
        total = snap.get("vram_total") or 0
        vpct = used / total * 100 if total else 0
        v_l = level(vpct, 90, 97)
        out = [f"[bold]{s.gpu.now:.0f}%[/] load   VRAM [{NUM[v_l]}]{vpct:.0f}%[/] "
               f"[dim]{human_bytes(used)} of {human_bytes(total)} · 5 min avg load "
               f"{s.gpu.mean:.0f}%[/]", ""]
        out += self._graph(s.gpu.points, width, label="load")
        out += ["", "[bold]video memory[/]"] + self._graph(s.vram.points, width, v_l, label="VRAM")

        ol = snap.get("ollama")
        out += ["", "[bold]Ollama (local models)[/]"]
        if ol is None:
            out.append("  [dim]checking…[/]")
        elif not ol["up"]:
            out.append("  [dim]not running[/]")
        elif not ol["models"]:
            out.append("  [dim]running, no model loaded — nothing held in video memory[/]")
        else:
            for m in ol["models"]:
                until = (f"unloads at {datetime.fromtimestamp(m['until']):%H:%M}"
                         if m["until"] else "stays loaded")
                share = f" · {m['vram'] / total * 100:.0f}% of VRAM" if total else ""
                out.append(f"  [bold]{escape(m['name'])}[/]  {human_bytes(m['vram'])}{share}  "
                           f"[dim]{m['params']} · {m['context']:,} context · {until}[/]")
            out.append("  [dim]Ollama frees the memory itself when the model unloads. "
                       "`ollama stop <model>` frees it now.[/]")

        types = snap.get("gpu_types") or {}
        if types:
            out += ["", "[bold]by engine[/]  [dim]3D is rendering and most compute; copy "
                    "moves data to and from the card; video is the media blocks[/]"]
            for name, value in sorted(types.items(), key=lambda kv: -float(kv[1] or 0)):
                v = min(100.0, float(value or 0))
                out.append(f"  {escape(name[:20]):<20} {bar(v / 100, 22)} {v:5.1f}%")
        gpus = (snap.get("sysinfo") or {}).get("gpus") or []
        if isinstance(gpus, dict):
            gpus = [gpus]
        for g in gpus:
            out += ["", f"[dim]{escape(g.get('name') or '')} · driver {g.get('driver')} "
                    f"({g.get('date')})[/]"]
        sensors = snap.get("gpu_sensors")
        if sensors:
            lvl = gpusensors.heat_level(sensors)

            def val(key, unit, fmt="{}"):
                v = sensors.get(key)
                return "—" if v is None else fmt.format(v) + unit

            out += ["", f"[bold]sensors[/]  [dim]from AMD's driver (ADL), every 2 s[/]",
                    f"  temperature  [{NUM[lvl]}]{val('edge', ' °C')}[/]  "
                    f"[dim]hotspot {val('hotspot', ' °C')} · memory {val('memory', ' °C')} · "
                    f"slows itself down around 110 °C hotspot[/]",
                    f"  fan          {val('fan_pct', '%')}  [dim]{val('fan_rpm', ' RPM')}[/]",
                    f"  power        {val('power', ' W')}",
                    f"  clocks       {val('core_clock', ' MHz')} core  [dim]· {val('mem_clock', ' MHz')} memory · "
                    f"{val('voltage', ' V', '{:.3f}')}[/]",
                    "", "[bold]temperature[/]"]
            out += self._graph(s.gpu_temp.points, width, top=max(100.0, s.gpu_temp.peak), label="°C, 0-100")
            out += ["", "[bold]power[/]"]
            out += self._graph(s.gpu_power.points, width, top=max(50.0, s.gpu_power.peak),
                               label=f"W, peak {s.gpu_power.peak:.0f}")
        else:
            out += ["[dim]No temperature or fan readings: these come from AMD's driver, and this "
                    "card isn't an AMD one (see NVIDIA-SETUP.md for NVIDIA cards).[/]"]
        return "\n".join(out)

    def _detail_storage(self, snap, s, width) -> str:
        out = [f"read [bold]{rate(s.disk_read.now)}[/]   write [bold]{rate(s.disk_write.now)}[/]"
               f"  [dim]peaks {rate(s.disk_read.peak)} / {rate(s.disk_write.peak)}[/]", ""]
        top = max(s.disk_read.peak + s.disk_write.peak, 1024 ** 2)
        out += self._graph([r + w for r, w in zip(s.disk_read.points, s.disk_write.points)],
                           width, top=top, label="read + write")
        out.append("")
        for d in snap.get("disks") or []:
            dl = disk_level(d)
            out += [f"[bold]{d['device']}[/] [dim]{d['fstype']}[/]",
                    f"  {bar(d['percent'] / 100, 30, dl)} [{NUM[dl]}]{human_bytes(d['free'])} free[/]"
                    f"  [dim]{human_bytes(d['used'])} used of {human_bytes(d['total'])}[/]", ""]
        per = snap.get("per_disk") or {}
        if per:
            out.append("[bold]lifetime per device[/]")
            for name, v in sorted(per.items(), key=lambda kv: -(kv[1]["read"] + kv[1]["write"]))[:8]:
                out.append(f"  {escape(name[:22]):<22} read {human_bytes(v['read']):>9}  "
                           f"write {human_bytes(v['write']):>9}  "
                           f"[dim]{v['rcount'] + v['wcount']:,} ops[/]")
        out += ["", "[dim]disk-sentinel.py answers the other storage question — "
                "what is actually eating the space.[/]"]
        return "\n".join(out)

    def _detail_network(self, snap, s, width) -> str:
        top = max(s.net_down.peak, s.net_up.peak, 64 * 1024)
        out = [f"↓ [bold]{rate(s.net_down.now)}[/]   ↑ [bold]{rate(s.net_up.now)}[/]  "
               f"[dim]peaks ↓ {rate(s.net_down.peak)} ↑ {rate(s.net_up.peak)}[/]", ""]
        out += self._graph(s.net_down.points, width, top=top, label="download")
        out += [""] + self._graph(s.net_up.points, width, top=top, label="upload")
        ts = snap.get("tailscale")
        if ts is not None:
            out += ["", "Tailscale " + ("[green]connected[/]" if ts else "[yellow]not connected[/]")]
        out += ["", "[bold]interfaces[/]"]
        for n in snap.get("nets") or []:
            speed = f"{n['speed']} Mb/s link" if n["speed"] else "speed unknown"
            out += [f"  [bold]{escape(n['name'][:34])}[/] [dim]{speed}[/]",
                    f"    received {human_bytes(n['recv']):>10}  "
                    f"[dim]{n['pkt_recv']:,} packets[/]",
                    f"    sent     {human_bytes(n['sent']):>10}  "
                    f"[dim]{n['pkt_sent']:,} packets[/]"]
            if n["errin"] or n["errout"] or n["dropin"] or n["dropout"]:
                out.append(f"    [yellow]errors in {n['errin']} out {n['errout']} · "
                           f"dropped in {n['dropin']} out {n['dropout']}[/]")
        try:
            conns = psutil.net_connections(kind="inet")
            established = sum(1 for c in conns if c.status == "ESTABLISHED")
            listening = sum(1 for c in conns if c.status == "LISTEN")
            out += ["", f"[bold]sockets[/]  {established} established · "
                    f"{listening} listening · {len(conns)} total"]
        except (psutil.AccessDenied, PermissionError):
            out += ["", "[dim]socket counts need elevation[/]"]
        return "\n".join(out)

    def _detail_claude(self, snap, s, width) -> str:
        c = snap["claude"]
        out = [f"[bold]${c['today_cost']:,.2f}[/] today  [dim]{c['today_calls']:,} calls · "
               f"{len(c['sessions'])} chats · ${sum(x['cost'] for x in c['sessions']):,.2f} "
               f"all time[/]", ""]
        order = {"approval": 0, "waiting": 1, "working": 2, "idle": 3}
        active = sorted((x for x in c["sessions"] if x["state"] in order),
                        key=lambda x: (order[x["state"]], x["age"]))
        out.append("[bold]chats in the last 12 hours[/]")
        if not active:
            out.append("  [dim]none[/]")
        for x in active:
            colour = {"approval": "yellow", "waiting": "bold", "working": "green", "idle": "dim"}[x["state"]]
            out.append(f"  [{colour}]{STATE_LABEL[x['state']]:<18}[/] {escape(x['title'][:50]):<50} "
                       f"[dim]{duration(x['age'])} ago[/]")
        out += ["[dim]  'may need approval' means the chat stopped on a tool call and has been "
                "quiet since — usually a permission prompt, sometimes a long command.[/]", ""]
        models = c.get("models") or {}
        if models:
            out.append("[bold]by model[/]  [dim]input · cache write · cache read · output[/]")
            for name, v in sorted(models.items(), key=lambda kv: -cost_of(kv[0], kv[1])):
                out.append(f"  {escape(name[:26]):<26} {v[0] / 1e6:7.1f}M {v[1] / 1e6:7.1f}M "
                           f"{v[2] / 1e6:9.1f}M {v[3] / 1e6:7.1f}M   "
                           f"[bold]${cost_of(name, v):,.2f}[/]")
            cr = sum(v[2] for v in models.values())
            out_tok = sum(v[3] for v in models.values())
            if cr + out_tok:
                out += ["", f"[dim]{cr / (cr + out_tok) * 100:.1f}% of all tokens are "
                        f"cache reads — the conversation being re-read each turn, "
                        f"not new work.[/]"]
        out += ["", "[bold]most expensive chats[/]"]
        for sess in c["sessions"][:12]:
            out.append(f"  [bold]${sess['cost']:>8,.2f}[/]  {sess['calls']:>5} calls  "
                       f"{escape(sess['title'][:50])}")
        out += ["", "[dim]Costs are API list rates applied to the transcript logs. "
                "Your plan is billed differently — /usage is authoritative.[/]"]
        return "\n".join(out)

    def _detail_processes(self, snap, s, width) -> str:
        procs = snap.get("procs") or []
        vm = snap.get("mem")
        out = [f"[bold]{len(procs)}[/] processes running", ""]
        hp = snap.get("helpers") or {}
        left = snap.get("leftovers") or []
        out.append("[bold]Claude Code helpers[/]")
        if hp.get("sessions"):
            out.append(f"  {hp['sessions']} chats are loaded in the Claude app. Each runs its "
                       f"own MCP servers: {hp['count']} processes using "
                       f"{human_bytes(hp['rss'])} in total.")
            out.append("  [dim]Expected, not a leak. Archiving chats you no longer need "
                       "closes theirs.[/]")
        else:
            out.append("  [dim]no Claude Code chats loaded[/]")
        servers = snap.get("servers") or []
        if servers:
            out += ["", "[bold]background servers[/]  [dim]started by a launcher that has since "
                    "exited — on purpose, e.g. from the Startup folder[/]"]
            for p in servers:
                port = ", ".join(f":{n}" for n in p["ports"]) or "windowless"
                out.append(f"  {p['pid']:>7}  {escape(p['name']):<16} {human_bytes(p['rss']):>9}  "
                           f"[dim]{duration(p['age'])} old[/]  {escape(port):<10} {escape(p['what'])}")
        out += ["", "[bold]leftover processes[/]  [dim]script processes whose parent has closed[/]"]
        if not left:
            out.append("  [green]none[/]")
        for p in left:
            out.append(f"  [yellow]{p['pid']:>7}[/]  {escape(p['name']):<16} "
                       f"{human_bytes(p['rss']):>9}  [dim]{duration(p['age'])} old[/]  "
                       f"{escape(p['what'])}")
        if left:
            out.append("  [dim]End them in Task Manager (Details tab, by PID) if you do not "
                       "recognise them. UtilityBelt never kills anything itself.[/]")
        groups: dict = defaultdict(lambda: [0, 0.0, 0])
        for p in procs:
            g = groups[p["name"]]
            g[0] += 1
            g[1] += p["cpu"]
            g[2] += p["rss"]
        out += ["", "[bold]by program[/]  [dim]instances · cpu · memory[/]"]
        for name, (count, cpu, rss) in sorted(groups.items(), key=lambda kv: -kv[1][2])[:14]:
            out.append(f"  {escape(name[:26]):<26} [dim]{count:>3}×[/]  {cpu:5.1f}%  "
                       f"{human_bytes(rss):>9}")
        out += ["", "[bold]top single processes by CPU[/]"]
        for p in procs[:14]:
            out.append(f"  {p['cpu']:5.1f}%  [dim]{p['pid']:>7}[/]  {escape(p['name'][:30]):<30}"
                       f"{human_bytes(p['rss']):>9}  [dim]{p['threads']} threads[/]")
        if vm:
            out += ["", "[bold]top single processes by memory[/]"]
            for p in sorted(procs, key=lambda r: -r["rss"])[:14]:
                out.append(f"  {human_bytes(p['rss']):>9}  [dim]{p['pid']:>7}[/]  "
                           f"{escape(p['name'][:30]):<30}{p['cpu']:5.1f}%")
        return "\n".join(out)

    def _detail_health(self, snap, s, width) -> str:
        alerts = compute_alerts(snap, s)
        out = ["[bold]needs attention[/]"]
        if not alerts:
            out.append("  [green]✓ nothing — all readings are normal[/]")
        for a in alerts:
            mark = "[red]●[/]" if a["lvl"] == CRIT else "[yellow]⚠[/]"
            out.append(f"  {mark} {escape(a['long'])}")

        evs = snap.get("events")
        out += ["", f"[bold]event log, last {EVENT_DAYS} days[/]  [dim]hardware errors, "
                "blue screens, driver resets, unexpected restarts and app crashes[/]"]
        if evs is None:
            out.append("  [dim]reading…[/]")
        else:
            counts = Counter(e["kind"] for e in evs)
            for kind in ("hardware error", "blue screen", "GPU driver crashed and recovered",
                         "unexpected shutdown or restart", "app crash"):
                n = counts.get(kind, 0)
                sev = next((e["sev"] for e in evs if e["kind"] == kind), OK)
                colour = "green" if not n else FILL[sev] if sev != OK else "default"
                out.append(f"  [{colour}]{n:>3}[/]  {kind}")
            serious = [e for e in evs if e["kind"] != "app crash"]
            if serious:
                out += ["", "  [bold]most recent[/]"]
                for e in serious[:8]:
                    out.append(f"  [dim]{datetime.fromtimestamp(e['when']):%a %d %b %H:%M}[/]  "
                               f"{e['kind']}")
            crashes = Counter(e["detail"] for e in evs if e["kind"] == "app crash")
            if crashes:
                out += ["", "  [bold]apps that crashed[/]"]
                for app, n in crashes.most_common(6):
                    out.append(f"  {n:>3}×  {escape(app)}")
            if not counts.get("hardware error"):
                out += ["", "  [dim]No hardware (WHEA) errors. After changing RAM or BIOS "
                        "settings this is the first place instability shows up.[/]"]

        info = snap.get("sysinfo") or {}
        out += ["", "[bold]this machine[/]"]
        if info:
            bios = info.get("bios") or {}
            running, rated = ram_speed(snap)
            gpus = info.get("gpus") or []
            if isinstance(gpus, dict):
                gpus = [gpus]
            rows = [("board", info.get("board")),
                    ("BIOS", f"{bios.get('version', '?')} ({bios.get('date', '?')})"),
                    ("processor", info.get("cpu")),
                    ("RAM", f"{running} MT/s" + (f" of {rated} rated" if rated else ""))]
            rows += [("graphics", f"{g.get('name')} · driver {g.get('driver')} ({g.get('date')})")
                     for g in gpus]
            for label, value in rows:
                out.append(f"  {label:<10} {escape(str(value or '?'))}")
        else:
            out.append("  [dim]reading…[/]")
        return "\n".join(out)


# --------------------------------------------------------------------- main

def probe() -> int:
    """One sample of everything, as plain text. Used to check the plumbing."""
    sampler = Sampler()
    make_engine(sampler)
    # The slow loop walks every process and every transcript; on a cold start
    # that takes a good few seconds. Wait for it rather than reporting zeros.
    deadline = time.time() + 60
    while time.time() < deadline:
        time.sleep(1.0)
        snap = sampler.read()
        if (snap.get("procs") and snap.get("disks") and snap["claude"]["scanned"]
                and snap.get("sysinfo") is not None and snap.get("events") is not None):
            break
        print(f"\r[waiting] procs={len(snap.get('procs') or [])} "
              f"disks={len(snap.get('disks') or [])} "
              f"transcripts={snap['claude']['scanned']}", end="", flush=True)
    print("\r" + " " * 70 + "\r", end="")
    snap = sampler.read()
    print(f"cpu          {sampler.cpu.now:.1f}%  ({len(sampler.cpu_cores)} threads)")
    vm = snap.get("mem")
    if vm:
        print(f"memory       {vm.percent:.1f}%  {human_bytes(vm.used)} / {human_bytes(vm.total)}")
    running, rated = ram_speed(snap)
    print(f"ram speed    {running} MT/s, rated {rated or '?'}")
    print(f"gpu          {sampler.gpu.now:.1f}%  engines={list((snap.get('gpu_types') or {}).keys())}")
    print(f"gpu sensors  {snap.get('gpu_sensors')}")
    print(f"vram         {human_bytes(snap.get('vram_used') or 0)} / "
          f"{human_bytes(snap.get('vram_total') or 0)}")
    ol = snap.get("ollama") or {}
    print(f"ollama       up={ol.get('up')}  "
          f"{[(m['name'], human_bytes(m['vram'])) for m in ol.get('models') or []]}")
    print(f"disks        {[(d['device'], human_bytes(d['free'])) for d in snap.get('disks') or []]}")
    print(f"disk io      r {rate(sampler.disk_read.now)}  w {rate(sampler.disk_write.now)}")
    print(f"net          down {rate(sampler.net_down.now)}  up {rate(sampler.net_up.now)}  "
          f"tailscale={snap.get('tailscale')}")
    print(f"processes    {len(snap.get('procs') or [])}")
    hp = snap.get("helpers") or {}
    print(f"helpers      {hp.get('sessions')} chats, {hp.get('count')} procs, "
          f"{human_bytes(hp.get('rss') or 0)}")
    print(f"leftovers    {[(p['pid'], p['what']) for p in snap.get('leftovers') or []]}")
    evs = snap.get("events") or []
    print(f"events       {dict(Counter(e['kind'] for e in evs))}")
    c = snap["claude"]
    print(f"claude       {c['today_calls']} calls today, ${c['today_cost']:.2f}, "
          f"{len(c['sessions'])} sessions, "
          f"states={dict(Counter(x['state'] for x in c['sessions']))}")
    print(f"agents       {len(snap.get('agents') or [])} "
          f"({sum(1 for a in snap.get('agents') or [] if a['live'])} live)")
    print("alerts       " + " | ".join(a["short"] for a in compute_alerts(snap, sampler)))
    sampler._evaluate()
    for f in sampler.read().get("findings") or []:
        print(f"finding      [{f.severity}{' muted' if f.muted else ''}] {f.key}: {f.title}")
        print(f"               because {f.because}")
        print(f"               fix     {f.fix}")
    if snap.get("errors"):
        print("errors      ", snap["errors"])
    sampler.stop.set()
    return 0


def make_engine(sampler: Sampler) -> findings.Engine:
    """The findings engine, fed by the sampler's slow loop (findings.py, rules/)."""
    def play() -> None:                                    # critical findings: once each
        notify.play("attention", float(notify.settings().get("volume") or 1.0))
    engine = findings.Engine(beltconfig.load(), note=sampler._note, play=play)
    sampler.engine = engine
    return engine


def main() -> int:
    if sys.argv[1:2] == ["rules"]:                         # belt.py rules --list
        print(findings.list_rules())
        return 0
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--probe", action="store_true",
                    help="print one sample of every metric and exit")
    ap.add_argument("--mini", action="store_true",
                    help="start as the small strip (m switches back)")
    args = ap.parse_args()
    if args.probe:
        return probe()
    try:                    # stay responsive while something heavy has every core busy
        psutil.Process().nice(psutil.ABOVE_NORMAL_PRIORITY_CLASS if sys.platform == "win32" else -5)
    except (psutil.Error, OSError, AttributeError):
        pass                # not allowed (e.g. negative nice without root): run as before
    sampler = Sampler()
    make_engine(sampler)
    app = Belt(sampler, mini=args.mini)
    try:
        app.run()
    finally:
        sampler.stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
