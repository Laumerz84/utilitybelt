"""Read Claude Code subagent transcripts as a live feed of steps.

Each subagent writes ~/.claude/projects/<project>/<session>/subagents/[workflows/<wf>/]
agent-<id>.jsonl, one JSON entry per finished block, plus agent-<id>.meta.json
(description, agentType, model). This module turns those entries into flat
events and reads only what was appended since the last poll, so following a
working agent costs a few kilobytes a second however long its log is.

Event: {"ts": epoch seconds, "kind": task|system|output|thinking|tool|result,
        "text": one-line-able text, "full": everything, "tool": name,
        "id": tool_use id, "error": bool}

Thinking is usually stored without its words (only a signature), so a
thinking event with empty text means "thought here, not recorded".
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

FILE_TOOLS = {"Read", "Write", "Edit", "MultiEdit", "NotebookEdit"}


def _ts(value: str) -> float:
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _first_line(text: str) -> str:
    return next((ln.strip() for ln in str(text).splitlines() if ln.strip()), "")


def summarize_tool(name: str, inp: dict) -> str:
    """The one thing worth reading about a tool call: the command, file or search."""
    inp = inp if isinstance(inp, dict) else {}
    if name in ("Bash", "PowerShell"):
        return _first_line(inp.get("command", ""))
    if name in FILE_TOOLS:
        return Path(str(inp.get("file_path") or inp.get("notebook_path") or "")).name
    if name == "Grep":
        where = inp.get("path") or inp.get("glob") or ""
        return f'"{inp.get("pattern", "")}"' + (f" in {where}" if where else "")
    if name == "Glob":
        return str(inp.get("pattern", ""))
    if name in ("Agent", "Task"):
        return str(inp.get("description") or _first_line(inp.get("prompt", "")))
    for key in ("url", "query", "skill", "description", "prompt"):
        if inp.get(key):
            return _first_line(inp[key])
    return next((_first_line(v) for v in inp.values() if isinstance(v, str) and v.strip()), "")


def _flatten(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif block.get("type") == "image":
                parts.append("[image]")
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(parts)


def events_from_entry(entry: dict) -> list[dict]:
    kind_of_entry = entry.get("type")
    if kind_of_entry not in ("user", "assistant"):
        return []
    ts = _ts(entry.get("timestamp"))
    content = (entry.get("message") or {}).get("content")
    base = {"ts": ts, "tool": "", "id": "", "error": False}
    out = []
    if kind_of_entry == "user":
        if isinstance(content, str):
            return [dict(base, kind="task", text=content, full=content)]
        for block in content or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                text = _flatten(block.get("content"))
                out.append(dict(base, kind="result", text=text, full=text,
                                id=block.get("tool_use_id") or "", error=bool(block.get("is_error"))))
            elif block.get("type") == "text" and (block.get("text") or "").strip():
                text = block["text"]
                # Claude Code injects notes to the agent as user text; they are not the task.
                kind = "system" if text.lstrip().startswith("<system-reminder>") else "task"
                out.append(dict(base, kind=kind, text=text, full=text))
        return out
    for block in content or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "thinking":
            text = str(block.get("thinking") or "")
            out.append(dict(base, kind="thinking", text=text, full=text))
        elif btype == "text" and (block.get("text") or "").strip():
            out.append(dict(base, kind="output", text=block["text"], full=block["text"]))
        elif btype == "tool_use":
            name, inp = block.get("name") or "?", block.get("input") or {}
            out.append(dict(base, kind="tool", tool=name, id=block.get("id") or "",
                            text=summarize_tool(name, inp),
                            full=json.dumps(inp, indent=1, ensure_ascii=False)))
    return out


def now_step(events: list[dict]) -> dict:
    """What the agent is doing at this moment, and since when."""
    if not events:
        return {"label": "starting", "since": 0.0}
    last = events[-1]
    if last["kind"] == "tool":
        return {"label": f"{last['tool']} · {last['text']}".rstrip(" ·"), "since": last["ts"]}
    if last["kind"] == "output":
        return {"label": "writing", "since": last["ts"]}
    return {"label": "thinking", "since": last["ts"]}


class AgentLog:
    """One agent's transcript, read incrementally."""

    MAX_EVENTS = 2000                     # a very long agent keeps its most recent steps

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.offset = 0
        self.partial = b""
        self.events: list[dict] = []
        meta_path = self.path.with_name(self.path.stem + ".meta.json")
        try:
            self.meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.meta = {}

    def poll(self) -> list[dict]:
        """Read whatever was appended since the last call; return its events."""
        try:
            with self.path.open("rb") as fh:
                fh.seek(self.offset)
                chunk = fh.read()
        except OSError:
            return []
        self.offset += len(chunk)
        data = self.partial + chunk
        lines = data.split(b"\n")
        self.partial = lines.pop()                  # incomplete last line waits for the rest
        new = []
        for raw in lines:
            if not raw.strip():
                continue
            try:
                new.extend(events_from_entry(json.loads(raw.decode("utf-8", errors="replace"))))
            except ValueError:
                continue
        self.events.extend(new)
        del self.events[:-self.MAX_EVENTS]
        return new
