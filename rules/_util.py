"""Small formatting helpers shared by the rules (no I/O)."""
from __future__ import annotations

from datetime import datetime


def gb(n: float) -> str:
    """Bytes as GB with sensible precision: 0.84 GB, 8.7 GB, 120 GB."""
    v = n / 1024 ** 3
    return f"{v:.2f} GB" if v < 1 else f"{v:.1f} GB" if v < 100 else f"{v:,.0f} GB"


def ago(seconds: float) -> str:
    """A duration in words: 40 s, 12 min, 3 h 20 min, 2 days."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 2 * 86400:
        h, m = divmod(s // 60, 60)
        return f"{h} h {m} min" if m and h < 10 else f"{h} h"
    return f"{s // 86400} days"


def at(epoch: float, now: float) -> str:
    """A moment: 14:05 today, else Sun 11:42."""
    d = datetime.fromtimestamp(epoch)
    return f"{d:%H:%M}" if now - epoch < 20 * 3600 else f"{d:%a %H:%M}"


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"
