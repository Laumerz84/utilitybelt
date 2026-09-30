"""agentlog: reading Claude Code subagent transcripts as a live feed.

    python -m unittest discover -s tests
"""
from __future__ import annotations

import json
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agentlog  # noqa: E402

TMP_BASE = ROOT / ".tmp-tests"  # inside the repo (git-ignored), never the system temp drive


def assistant(ts: str, *blocks: dict) -> dict:
    return {"type": "assistant", "timestamp": ts, "message": {"content": list(blocks)}}


def user(ts: str, content) -> dict:
    return {"type": "user", "timestamp": ts, "message": {"content": content}}


class SummarizeTool(unittest.TestCase):
    def test_bash_shows_first_line_of_command(self):
        self.assertEqual(agentlog.summarize_tool("Bash", {"command": "pytest -q\necho done"}),
                         "pytest -q")

    def test_file_tools_show_file_name(self):
        self.assertEqual(agentlog.summarize_tool("Edit", {"file_path": r"F:\x\belt.py"}), "belt.py")

    def test_grep_shows_pattern_and_where(self):
        self.assertEqual(agentlog.summarize_tool("Grep", {"pattern": "def main", "path": "src"}),
                         '"def main" in src')

    def test_unknown_tool_falls_back_to_first_text_argument(self):
        self.assertEqual(agentlog.summarize_tool("mcp__x__lookup", {"n": 3, "query": "cats"}), "cats")


class Events(unittest.TestCase):
    def test_assistant_blocks_become_thinking_output_and_tool_in_order(self):
        evs = agentlog.events_from_entry(assistant(
            "2026-09-30T20:00:00Z",
            {"type": "thinking", "thinking": "", "signature": "x"},
            {"type": "text", "text": "Checking the tests."},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest"}}))
        self.assertEqual([e["kind"] for e in evs], ["thinking", "output", "tool"])
        self.assertEqual(evs[0]["text"], "")                    # thinking not recorded
        self.assertEqual(evs[1]["text"], "Checking the tests.")
        self.assertEqual((evs[2]["tool"], evs[2]["text"], evs[2]["id"]), ("Bash", "pytest", "t1"))
        self.assertIn('"command": "pytest"', evs[2]["full"])

    def test_tool_error_result_is_marked_and_flattened(self):
        evs = agentlog.events_from_entry(user("2026-09-30T20:00:01Z", [
            {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
             "content": [{"type": "text", "text": "boom"}]}]))
        self.assertEqual(len(evs), 1)
        self.assertEqual((evs[0]["kind"], evs[0]["text"], evs[0]["error"], evs[0]["id"]),
                         ("result", "boom", True, "t1"))

    def test_plain_user_prompt_is_the_task(self):
        evs = agentlog.events_from_entry(user("2026-09-30T20:00:00Z", "You are step X."))
        self.assertEqual((evs[0]["kind"], evs[0]["text"]), ("task", "You are step X."))

    def test_claude_code_reminders_are_system_notes_not_the_task(self):
        evs = agentlog.events_from_entry(user("2026-09-30T20:00:00Z", [
            {"type": "text", "text": "<system-reminder>\nUse the handback tool.\n</system-reminder>"}]))
        self.assertEqual(evs[0]["kind"], "system")

    def test_timestamps_become_epoch_seconds(self):
        evs = agentlog.events_from_entry(user("1970-01-01T00:01:00.500Z", "go"))
        self.assertAlmostEqual(evs[0]["ts"], 60.5)


class NowStep(unittest.TestCase):
    def ev(self, kind, ts, **kw):
        return dict({"kind": kind, "ts": ts, "text": "", "tool": "", "id": ""}, **kw)

    def test_unanswered_tool_call_is_what_it_is_doing(self):
        step = agentlog.now_step([self.ev("tool", 100, tool="Bash", text="pytest", id="t1")])
        self.assertEqual((step["label"], step["since"]), ("Bash · pytest", 100))

    def test_after_a_result_it_is_thinking(self):
        step = agentlog.now_step([self.ev("tool", 100, tool="Bash", id="t1"),
                                  self.ev("result", 105, id="t1")])
        self.assertEqual((step["label"], step["since"]), ("thinking", 105))

    def test_no_events_means_starting(self):
        self.assertEqual(agentlog.now_step([])["label"], "starting")


class TailReading(unittest.TestCase):
    def setUp(self):
        TMP_BASE.mkdir(exist_ok=True)
        self.dir = TMP_BASE / self.id().rsplit(".", 1)[-1]
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir()
        self.path = self.dir / "agent-abc.jsonl"

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def append(self, text: str):
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(text)

    def test_poll_returns_only_new_complete_lines(self):
        line1 = json.dumps(user("2026-09-30T20:00:00Z", "task")) + "\n"
        line2 = json.dumps(assistant("2026-09-30T20:00:01Z", {"type": "text", "text": "hi"}))
        self.append(line1)
        log = agentlog.AgentLog(self.path)
        self.assertEqual([e["kind"] for e in log.poll()], ["task"])
        self.append(line2[:20])                                 # half-written line
        self.assertEqual(log.poll(), [])
        self.append(line2[20:] + "\n")
        self.assertEqual([e["kind"] for e in log.poll()], ["output"])
        self.assertEqual(len(log.events), 2)

    def test_meta_file_beside_the_log_is_read(self):
        (self.dir / "agent-abc.meta.json").write_text(
            json.dumps({"description": "LOTS-UI", "agentType": "general-purpose", "model": "sonnet"}))
        self.append("")
        meta = agentlog.AgentLog(self.path).meta
        self.assertEqual((meta["description"], meta["model"]), ("LOTS-UI", "sonnet"))


if __name__ == "__main__":
    unittest.main()
