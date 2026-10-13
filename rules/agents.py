"""Claude chats and agents: who is waiting on you, who is stuck, what it costs.

Every rule here checks snap["claude_ready"] first: until the first transcript scan
lands, the session list is empty, and "no chats waiting" would be a lie.
"""
from __future__ import annotations

from findings import Finding
from rules._util import ago, plural


def _sessions(snap: dict, state: str) -> list[dict]:
    if not snap.get("claude_ready"):
        return []
    return [s for s in (snap.get("claude") or {}).get("sessions") or [] if s.get("state") == state]


def _names(sessions: list[dict], room: int = 3) -> str:
    names = [f"“{(s.get('title') or '?')[:40]}” ({ago(s['age'])})" for s in sessions[:room]]
    more = f" and {len(sessions) - room} more" if len(sessions) > room else ""
    return ", ".join(names) + more


def approval(snap, cfg):
    """A chat has gone quiet on a tool call: most likely a permission prompt nobody has answered."""
    limit = cfg.t("approval_minutes") * 60
    stuck = sorted((s for s in _sessions(snap, "approval") if s["age"] >= limit), key=lambda s: -s["age"])
    if not stuck:
        return None
    return Finding(
        "agents.approval", "warn",
        "A chat is probably waiting for your permission" if len(stuck) == 1
        else f"{len(stuck)} chats are probably waiting for your permission",
        f"{_names(stuck)}: the last thing written was a {stuck[0].get('last_tool') or 'tool'} call, "
        f"and nothing since",
        "Open the chat in Claude and answer the prompt; until then it does no work.",
        cites=("agents",))


def waiting(snap, cfg):
    """Chats finished their reply a while ago and have been waiting on you since."""
    limit = cfg.t("unattended_minutes") * 60
    idle = sorted((s for s in _sessions(snap, "waiting") if s["age"] >= limit), key=lambda s: s["age"])
    if not idle:
        return None
    return Finding(
        "agents.waiting", "info",
        "A chat finished and is waiting on you" if len(idle) == 1
        else f"{len(idle)} chats finished and are waiting on you",
        f"{_names(idle)} since the last reply",
        "Reply, or close the ones you are done with (mute this if you leave chats open on purpose).",
        cites=("agents",))


def unattended(snap, cfg):
    """A working agent has been on the same step for a long time without writing anything."""
    if not snap.get("claude_ready"):
        return None
    now = snap.get("now") or 0
    limit = cfg.t("unattended_minutes") * 60
    stuck = []
    for a in snap.get("live_agents") or []:
        step = a.get("now") or {}
        if step.get("since") and now - step["since"] >= limit:
            stuck.append((now - step["since"], a))
    if not stuck:
        return None
    stuck.sort(key=lambda x: -x[0])
    wait, a = stuck[0]
    where = f" for “{a['chat'][:40]}”" if a.get("chat") else ""
    return Finding(
        "agents.unattended", "warn",
        "An agent has been stuck on one step" if len(stuck) == 1
        else f"{len(stuck)} agents have been stuck on one step",
        f"“{(a.get('desc') or a.get('id') or '?')[:50]}”{where} has been on “{step_label(a)[:50]}” "
        f"for {ago(wait)} with no new output",
        "Open Agents (7) to see what it is waiting for; a hung command usually needs stopping from its chat.",
        cites=("agents",))


def step_label(a: dict) -> str:
    return (a.get("now") or {}).get("label") or "?"


def spend(snap, cfg):
    """Claude spend today has passed the limit you set."""
    if not snap.get("claude_ready"):
        return None
    c = snap.get("claude") or {}
    cost, limit = c.get("today_cost") or 0, cfg.t("spend_today_usd")
    if cost <= limit:
        return None
    biggest = sorted((s for s in c.get("sessions") or [] if s.get("today_calls")),
                     key=lambda s: -(s.get("cost") or 0))[:2]
    who = (", biggest chats overall " + ", ".join(f"“{s['title'][:30]}” (${s['cost']:,.0f})" for s in biggest)
           if biggest else "")
    return Finding(
        "agents.spend", "info", "Claude spending is over your daily limit",
        f"${cost:,.0f} today against a ${limit:,.0f} limit, {plural(c.get('today_calls') or 0, 'call')}{who}",
        "See Claude (6) for where it went; long jobs on a smaller model cost a fraction. "
        "Raise spend_today_usd in config.toml if this is normal for you.",
        cites=("spend",))


RULES = [approval, unattended, waiting, spend]
