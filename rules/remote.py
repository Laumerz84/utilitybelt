"""Remote streaming (Sunshine, played from Moonlight). Off by default: it assumes
Sunshine. Turn on with remote = true under [rules] in config.toml; the log it reads
is [remote] sunshine_log.

The stream is UDP, so netstat only ever shows Sunshine listening: whether someone is
actually connected is only in Sunshine's own log, which the sampler tails into
snap["sunshine"] = {"live": bool, "since": epoch, "screen": "1920x1080", "screen_before": "..."}.
Quiet when Sunshine is not installed."""
from __future__ import annotations

from findings import Finding
from rules._util import ago, at


def stream_live(snap, cfg):
    """Someone is connected to this PC through Sunshine right now."""
    s = snap.get("sunshine") or {}
    if not s.get("live"):
        return None
    now = snap.get("now") or 0
    long = now - s["since"] >= 3600
    return Finding(
        "remote.stream_live", "warn" if long else "info",
        f"A stream has been live for {ago(now - s['since'])}",
        f"A Moonlight client connected at {at(s['since'], now)}; this screen, its sound and your "
        f"keyboard and mouse are shared with it",
        "If that is not you, end it from Sunshine's web page (Troubleshooting > Force close) "
        "or stop the Sunshine service.")


def resolution_changed(snap, cfg):
    """The desktop resolution was changed for a stream and has not been put back."""
    s = snap.get("sunshine") or {}
    before, now_res = s.get("screen_before"), s.get("screen")
    if not before or not now_res or before == now_res or s.get("live"):
        return None
    return Finding(
        "remote.resolution_changed", "info", "The screen was left at the stream's resolution",
        f"It was {before} before the last stream and is {now_res} now",
        "Set it back in Settings > System > Display; Sunshine's 'restore display' option does it by itself.")


RULES = [stream_live, resolution_changed]
