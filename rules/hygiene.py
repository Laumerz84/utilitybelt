"""Housekeeping: disks running out, processes eating a core or left behind, caches
stranded on C: after their folder moved."""
from __future__ import annotations

from pathlib import Path

import actions
from findings import Action, Finding
from rules._util import ago, at, gb, plural

HERE = str(Path(__file__).resolve().parent.parent)
GB = 1024 ** 3
WINDOWS_MIN = 1 * GB            # below about this, Windows itself starts failing
CRITICAL_FREE = 3 * GB

# Always busy by design, or part of Windows: a long run of CPU here is not a hog.
NOT_HOGS = {"system", "registry", "memory compression", "secure system", "msmpeng.exe",
            "audiodg.exe", "dwm.exe", "csrss.exe", "ollama.exe", "ollama app.exe"}


def _show_disk_use(device: str) -> Action:
    root = device.rstrip("\\/") + "/"
    return Action("Show what's using it",
                  actions.open_terminal(["cmd", "/k", "python", "disk-sentinel.py", "--root", root], HERE),
                  confirm=False)


def disk_floor(snap, cfg):
    """A drive has less free space than your floor (disk_floor_gb)."""
    floor = cfg.t("disk_floor_gb") * GB
    low = sorted((d for d in snap.get("disks") or [] if d["free"] < floor), key=lambda d: d["free"])
    if not low:
        return None
    d = low[0]
    others = f"; also {', '.join(x['device'] for x in low[1:])}" if len(low) > 1 else ""
    roomy = max(snap.get("disks") or [], key=lambda x: x["free"])
    elsewhere = (f" Put new things on {roomy['device']} ({gb(roomy['free'])} free)."
                 if roomy["device"] != d["device"] else "")
    return Finding(
        "hygiene.disk_floor", "critical" if d["free"] < CRITICAL_FREE else "warn",
        f"{d['device']} is nearly full",
        f"{gb(d['free'])} free of {gb(d['total'])} ({100 - d['percent']:.0f}%), under your "
        f"{cfg.t('disk_floor_gb'):g} GB floor{others}",
        "See what grew and move or delete it." + elsewhere,
        action=_show_disk_use(d["device"]), cites=("disk",))


def _trend(points: list, now: float, window: float = 15 * 60):
    """(bytes per second falling, seconds observed) when the free space has
    fallen steadily over the window; None for a blip, a flat line, or too little data."""
    pts = [(t, f) for t, f in points if now - t <= window]
    if len(pts) < 6 or pts[-1][0] - pts[0][0] < 5 * 60:
        return None
    falls = sum(1 for (_, a), (_, b) in zip(pts, pts[1:]) if b < a - 1024 ** 2)
    if falls < 0.7 * (len(pts) - 1) or pts[0][1] - pts[-1][1] < GB:
        return None                                   # not sustained, or not much
    n = len(pts)
    mt = sum(t for t, _ in pts) / n
    mf = sum(f for _, f in pts) / n
    slope = sum((t - mt) * (f - mf) for t, f in pts) / (sum((t - mt) ** 2 for t, _ in pts) or 1)
    return (-slope, pts[-1][0] - pts[0][0]) if slope < 0 else None


def disk_trend(snap, cfg):
    """A drive is filling fast enough to reach your floor (or empty) within disk_trend_minutes."""
    now = snap.get("now") or 0
    horizon = cfg.t("disk_trend_minutes") * 60
    floor = cfg.t("disk_floor_gb") * GB
    worst = None
    for d in snap.get("disks") or []:
        trend = _trend((snap.get("disk_history") or {}).get(d["device"]) or [], now)
        if not trend:
            continue
        rate, seen = trend
        to_empty = (d["free"] - WINDOWS_MIN) / rate
        to_floor = (d["free"] - floor) / rate
        if to_empty <= horizon:
            sev, eta, what = "critical", to_empty, "full"
        elif 0 < to_floor <= horizon:
            sev, eta, what = "warn", to_floor, f"under {cfg.t('disk_floor_gb'):g} GB"
        else:
            continue
        if worst is None or eta < worst[1]:
            worst = (d, eta, what, sev, rate, seen)
    if worst is None:
        return None
    d, eta, what, sev, rate, seen = worst
    return Finding(
        "hygiene.disk_trend", sev, f"{d['device']} will be {what} in about {ago(max(60, eta))}",
        f"{gb(d['free'])} left, falling {gb(rate * 60)}/min for the last {ago(seen)}",
        "Pause whatever is downloading, installing or recording to it, or free space now: "
        "Windows stops working properly below about 1 GB.",
        action=_show_disk_use(d["device"]), cites=("disk",))


def core_hog(snap, cfg):
    """A process has used about a whole CPU core, nonstop, for hours (core_hog_hours)."""
    now = snap.get("now") or 0
    hours = cfg.t("core_hog_hours")
    hogs = []
    for p in snap.get("procs") or []:
        age = now - (p.get("created") or now)
        if age < hours * 3600 or p["name"].lower() in NOT_HOGS:
            continue
        cores = (p.get("cpu_time") or 0) / age
        if cores >= 0.8:
            hogs.append((cores, age, p))
    if not hogs:
        return None
    hogs.sort(key=lambda h: -h[1])
    cores, age, p = hogs[0]
    what = p.get("what") or p["name"]
    more = f" (and {plural(len(hogs) - 1, 'other')})" if len(hogs) > 1 else ""
    return Finding(
        "hygiene.core_hog", "warn", "A process has kept a core busy for hours",
        f"{what} (pid {p['pid']}) has averaged {cores * 100:.0f}% of one core for {ago(age)}, "
        f"since {at(p['created'], now)}{more}. As a share of the whole CPU it looks small, "
        f"which is how it hides",
        "End it unless you know it is meant to be doing that much work: it is "
        "costing heat, fan noise and power the whole time.",
        action=Action(f"End {p['name']}", actions.end_processes([(p["pid"], p["created"], p["name"])]),
                      confirm_label=f"End {p['name']} (pid {p['pid']})? click again"))


def orphan(snap, cfg):
    """Script processes are still running long after whatever started them has closed."""
    old = sorted((p for p in snap.get("leftovers") or [] if p.get("age", 0) >= 30 * 60),
                 key=lambda p: -p["age"])
    if not old:
        return None
    named = ", ".join(f"{p['what']} ({ago(p['age'])})" for p in old[:3])
    more = f" and {len(old) - 3} more" if len(old) > 3 else ""
    targets = [(p["pid"], p["created"], p["name"]) for p in old]
    return Finding(
        "hygiene.orphan", "warn",
        "A script was left running" if len(old) == 1 else f"{len(old)} scripts were left running",
        f"{named}{more}: the window or program that started "
        f"{'it' if len(old) == 1 else 'them'} has closed",
        f"End {'it' if len(old) == 1 else 'them'}: nothing is reading "
        f"{'its' if len(old) == 1 else 'their'} output any more. Health (h) lists each one.",
        action=Action("End " + ("it" if len(old) == 1 else f"all {len(old)}"), actions.end_processes(targets),
                      confirm_label=f"End {', '.join(str(p['pid']) for p in old[:6])}? click again"))


def stranded_cache(snap, cfg):
    """A cache folder was moved off C: by an environment variable, but the old copy is still there."""
    found = sorted(snap.get("stranded") or [], key=lambda s: -s["size"])
    if not found:
        return None
    s = found[0]
    more = f" (and {plural(len(found) - 1, 'more')})" if len(found) > 1 else ""
    return Finding(
        "hygiene.stranded_cache", "info", f"{gb(s['size'])} of old cache left on C:",
        f"{s['var']} now points at {s['now_at']}, so nothing uses {s['path']} any more{more}",
        "Delete the old folder. If some files are in use, close the program using them and try again.",
        action=Action("Delete it", actions.delete_folder(s["path"]),
                      confirm_label=f"Delete {s['path']}? click again"),
        cites=("disk",))


RULES = [disk_trend, disk_floor, core_hog, orphan, stranded_cache]
