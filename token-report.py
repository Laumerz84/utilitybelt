#!/usr/bin/env python
"""Token and cost report across Claude Code sessions.

    python token-report.py                 # every session, dearest first
    python token-report.py --by date       # newest first
    python token-report.py --limit 10
    python token-report.py --session e4a4d0e2   # per-call detail for one session
    python token-report.py --models        # totals per model instead

Reads the JSONL transcripts Claude Code writes under ~/.claude/projects/.
Each assistant entry carries a usage object; this sums them.

Two things that make a naive version wrong:
  - The same API response is logged more than once (streaming/retry rows). In
    one measured session 273 of 511 rows were repeats, which doubles the total.
    Everything here dedupes on message.id first.
  - Cache reads dominate. They are usually 80%+ of the bill and are invisible
    if you only look at input/output, so they get their own column.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path.home() / ".claude" / "projects"

# $ per million tokens: (input, output, cache_read, cache_write)
# Cache write is the 1h-TTL rate (2x input) — what Claude Code uses.
PRICES = {
    "claude-fable-5-1":  (10.0, 50.0, 0.25, 20.0),
    "claude-fable-5":    (10.0, 50.0, 0.25, 20.0),
    "claude-opus-5":     (5.0,  25.0, 0.50, 10.0),
    "claude-opus-4-8":   (5.0,  25.0, 0.50, 10.0),
    "claude-opus-4-7":   (5.0,  25.0, 0.50, 10.0),
    "claude-sonnet-5":   (2.0,  10.0, 0.20,  4.0),
    "claude-sonnet-4-6": (3.0,  15.0, 0.30,  6.0),
    "claude-haiku-4-5":  (1.0,   5.0, 0.10,  2.0),
}
LOCAL = ("gpt-oss", "qwen", "llama", "mistral")  # run on your own GPU, $0

FIELDS = (("inp", "input_tokens"), ("cw", "cache_creation_input_tokens"),
          ("cr", "cache_read_input_tokens"), ("out", "output_tokens"))


def price(model: str) -> tuple | None:
    if any(t in model for t in LOCAL):
        return (0.0, 0.0, 0.0, 0.0)
    base = model.rsplit("-2", 1)[0]  # strip a date suffix if present
    return PRICES.get(model) or PRICES.get(base)


def cost_of(model: str, t: dict) -> tuple[float, bool]:
    """-> (dollars, priced). priced=False when the model is unknown."""
    p = price(model)
    if p is None:
        return 0.0, False
    i, o, cr, cw = p
    return (t["inp"] * i + t["out"] * o + t["cr"] * cr + t["cw"] * cw) / 1e6, True


def read_session(path: Path) -> dict:
    seen: set = set()
    per_model: dict = defaultdict(lambda: dict.fromkeys([f[0] for f in FIELDS], 0))
    calls = dupes = 0
    first_user = ""
    stamps: list[str] = []

    try:
        fh = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue

            if not first_user and e.get("type") == "user":
                c = (e.get("message") or {}).get("content")
                if isinstance(c, str):
                    first_user = c.strip().replace("\n", " ")[:52]
                elif isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "text":
                            first_user = b["text"].strip().replace("\n", " ")[:52]
                            break

            if e.get("type") != "assistant":
                continue
            m = e.get("message") or {}
            u = m.get("usage")
            if not u:
                continue
            mid = m.get("id")
            if mid and mid in seen:
                dupes += 1
                continue
            if mid:
                seen.add(mid)
            calls += 1
            if e.get("timestamp"):
                stamps.append(e["timestamp"])
            bucket = per_model[m.get("model") or "unknown"]
            for key, src in FIELDS:
                bucket[key] += u.get(src, 0)

    if not calls:
        return {}
    total = dict.fromkeys([f[0] for f in FIELDS], 0)
    dollars, all_priced = 0.0, True
    for model, t in per_model.items():
        for k in total:
            total[k] += t[k]
        d, ok = cost_of(model, t)
        dollars += d
        all_priced &= ok
    return {
        "path": path, "project": path.parent.name, "id": path.stem[:8],
        "calls": calls, "dupes": dupes, "total": total, "per_model": per_model,
        "cost": dollars, "priced": all_priced,
        "start": min(stamps)[:10] if stamps else "?",
        "title": first_user or "(no user text)",
    }


def detail(sess: dict) -> None:
    """Per-call rows for one session."""
    seen, rows = set(), []
    with sess["path"].open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("type") != "assistant":
                continue
            m = e.get("message") or {}
            u = m.get("usage")
            if not u:
                continue
            mid = m.get("id")
            if mid and mid in seen:
                continue
            if mid:
                seen.add(mid)
            rows.append(((e.get("timestamp") or "")[11:19],
                         (m.get("model") or "?").replace("claude-", ""),
                         u.get("input_tokens", 0),
                         u.get("cache_creation_input_tokens", 0),
                         u.get("cache_read_input_tokens", 0),
                         u.get("output_tokens", 0)))

    hdr = f"{'#':>4} {'time':>9} {'model':<14} {'in':>7} {'cache wr':>9} {'cache rd':>11} {'out':>7}"
    print(f"\n{sess['id']}  {sess['title']}\n")
    print(hdr)
    print("-" * len(hdr))
    for i, r in enumerate(rows, 1):
        print(f"{i:>4} {r[0]:>9} {r[1][:14]:<14} {r[2]:>7,} {r[3]:>9,} {r[4]:>11,} {r[5]:>7,}")
    print("-" * len(hdr))
    t = sess["total"]
    print(f"{'TOTAL':>29} {t['inp']:>7,} {t['cw']:>9,} {t['cr']:>11,} {t['out']:>7,}")
    print(f"\n{sess['calls']} calls · {sess['dupes']} duplicate rows skipped "
          f"· avg {t['cr'] // max(sess['calls'],1):,} cache read/call "
          f"· ~${sess['cost']:,.2f}")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--by", choices=("cost", "date"), default="cost")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--session", help="id prefix — show per-call detail")
    ap.add_argument("--models", action="store_true", help="totals per model")
    ap.add_argument("--root", default=str(ROOT))
    args = ap.parse_args()

    root = Path(args.root)
    if not root.exists():
        print(f"no transcripts under {root}")
        return 1

    sessions = [s for s in (read_session(p) for p in root.glob("*/*.jsonl")) if s]
    if not sessions:
        print(f"no sessions with usage data under {root}")
        return 1

    if args.session:
        hit = [s for s in sessions if s["id"].startswith(args.session.lower())]
        if not hit:
            print(f"no session id starting {args.session!r}")
            return 1
        for s in hit:
            detail(s)
        return 0

    if args.models:
        agg: dict = defaultdict(lambda: dict.fromkeys([f[0] for f in FIELDS], 0))
        for s in sessions:
            for model, t in s["per_model"].items():
                for k in agg[model]:
                    agg[model][k] += t[k]
        hdr = f"{'model':<22} {'in':>9} {'cache wr':>12} {'cache rd':>14} {'out':>10} {'cost':>10}"
        print(hdr)
        print("-" * len(hdr))
        grand = 0.0
        for model, t in sorted(agg.items(), key=lambda kv: -cost_of(kv[0], kv[1])[0]):
            d, ok = cost_of(model, t)
            grand += d
            tag = f"${d:>9,.2f}" if ok else "  unpriced"
            print(f"{model[:22]:<22} {t['inp']:>9,} {t['cw']:>12,} {t['cr']:>14,} {t['out']:>10,} {tag}")
        print("-" * len(hdr))
        print(f"{'TOTAL':<22} {'':>9} {'':>12} {'':>14} {'':>10} ${grand:>9,.2f}")
        return 0

    sessions.sort(key=(lambda s: -s["cost"]) if args.by == "cost"
                  else (lambda s: s["start"]), reverse=(args.by == "date"))
    shown = sessions[:args.limit] if args.limit else sessions

    hdr = (f"{'date':<11} {'session':<9} {'calls':>6} {'cache rd':>13} "
           f"{'out':>9} {'cost':>9}  title")
    print(hdr)
    print("-" * (len(hdr) + 20))
    for s in shown:
        t = s["total"]
        star = "" if s["priced"] else "*"
        print(f"{s['start']:<11} {s['id']:<9} {s['calls']:>6,} {t['cr']:>13,} "
              f"{t['out']:>9,} ${s['cost']:>8,.2f}{star}  {s['title']}")

    tot_cr = sum(s["total"]["cr"] for s in sessions)
    tot_out = sum(s["total"]["out"] for s in sessions)
    tot_calls = sum(s["calls"] for s in sessions)
    tot_cost = sum(s["cost"] for s in sessions)
    print("-" * (len(hdr) + 20))
    print(f"{len(sessions)} sessions{'':<2} {'':<9} {tot_calls:>6,} {tot_cr:>13,} "
          f"{tot_out:>9,} ${tot_cost:>8,.2f}")
    if tot_cr + tot_out:
        pct = tot_cr / (tot_cr + tot_out) * 100
        print(f"\ncache reads are {pct:.2f}% of all tokens — the conversation resent each turn")
        print("cost is at API list rates, NOT your bill: subscription usage is not "
              "billed per token.\nUse /usage for the authoritative figure.")
    if any(not s["priced"] for s in sessions):
        print("* contains a model with no price on file; tokens counted, cost not")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
