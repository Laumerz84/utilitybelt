"""A rule to copy. This module is never loaded (it is not in findings.MODULES) and
its rule always returns None. See README.md in this folder for the contract."""
from __future__ import annotations

from findings import Finding                     # and Action, if it gets a button
from rules._util import gb


def swap_heavy(snap, cfg):
    """The first line of the docstring is what `belt.py rules --list` shows."""
    sw = snap.get("swap")                       # 1. read only snap and cfg: no I/O here
    if sw is None:                              # 2. not sampled yet: say nothing
        return None
    if sw.used < 8 * 1024 ** 3:                 # 3. nothing wrong: None, never "all fine"
        return None
    return None                                 # (an example: never fires)
    # 4. Something is wrong: say what, why (with the numbers) and what to do.
    #    The key is "<module>.<function name>"; it is how mutes and history find it.
    return Finding(
        "example.swap_heavy", "warn",
        "Windows is leaning on the page file",                  # title: plain words, no numbers
        f"{gb(sw.used)} swapped out; memory is {snap['mem'].percent:.0f}% used",   # because
        "Close something large, or check Memory (2) for what is holding it.",      # fix
        cites=("mem",),                                         # highlight the quiet-row item
    )


RULES: list = []                                # an empty list: copy the rule, then list it here
