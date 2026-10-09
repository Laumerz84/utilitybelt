"""notify.py's "silent when you are looking" rule and each chat's own turn time (offline, no sound played).

    python -m unittest discover -s tests
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import notify  # noqa: E402

TMP_BASE = ROOT / ".tmp-tests"  # inside the repo (git-ignored), never the system temp drive


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = TMP_BASE / f"notify-{self._testMethodName}"
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.tmp, True)


class TestSettings(Tmp):
    def cfg(self, shared: dict) -> dict:
        (self.tmp / "sounds.json").write_text(json.dumps(shared), encoding="utf-8")
        with mock.patch.object(notify, "SETTINGS", self.tmp / "sounds.json"), \
                mock.patch.object(notify, "LOCAL", self.tmp / "none.json"):
            return notify.settings()

    def test_the_older_switch_maps_to_a_mode(self):
        self.assertEqual(self.cfg({"mute_when_claude_focused": True})["mute"], "chat")
        self.assertEqual(self.cfg({"mute_when_claude_focused": False})["mute"], "never")
        self.assertEqual(self.cfg({})["mute"], "chat")

    def test_an_explicit_mode_wins_and_a_bad_one_is_ignored(self):
        self.assertEqual(self.cfg({"mute": "app", "mute_when_claude_focused": True})["mute"], "app")
        self.assertEqual(self.cfg({"mute": "loud", "mute_when_claude_focused": False})["mute"], "never")


class TestFocusedChat(Tmp):
    def record(self, name: str, cli: str, focused_ms: float | None):
        d = self.tmp / "acct" / "org"
        d.mkdir(parents=True, exist_ok=True)
        rec = {"sessionId": name, "cliSessionId": cli, "title": name}
        if focused_ms is not None:
            rec["lastFocusedAt"] = focused_ms
        (d / f"{name}.json").write_text(json.dumps(rec), encoding="utf-8")

    def test_the_newest_focus_wins(self):
        self.record("local_a", "cli-a", 1000)
        self.record("local_b", "cli-b", 3000)
        self.record("local_c", "cli-c", None)
        self.assertEqual(notify.focused_chat([self.tmp]), "cli-b")

    def test_no_records_is_unknown(self):
        self.assertIsNone(notify.focused_chat([self.tmp]))
        self.assertIsNone(notify.focused_chat([]))
        bad = self.tmp / "acct" / "org"
        bad.mkdir(parents=True)
        (bad / "local_x.json").write_text("{not json", encoding="utf-8")
        self.assertIsNone(notify.focused_chat([self.tmp]))


class TestSilenced(unittest.TestCase):
    def run_rule(self, mode, front="claude.exe", focused="cli-a", mine="cli-a", sound="done"):
        with mock.patch.object(notify, "foreground_exe", return_value=front), \
                mock.patch.object(notify, "focused_chat", return_value=focused):
            return notify.silenced(mode, sound, {"session_id": mine} if mine else {})

    def test_chat_mode_quiets_only_the_chat_you_are_in(self):
        self.assertTrue(self.run_rule("chat"))                               # this chat, Claude in front
        self.assertFalse(self.run_rule("chat", mine="cli-b"))                # another chat: plays
        self.assertFalse(self.run_rule("chat", front="discord.exe"))         # Claude not in front: plays

    def test_chat_mode_falls_back_to_app_mode(self):
        self.assertTrue(self.run_rule("chat", focused=None, mine="cli-b"))   # records unreadable
        self.assertTrue(self.run_rule("chat", mine=None))                    # no session_id in the hook

    def test_app_and_never(self):
        self.assertTrue(self.run_rule("app", mine="cli-b"))
        self.assertFalse(self.run_rule("app", front="discord.exe"))
        self.assertFalse(self.run_rule("never"))

    def test_always_play_wins(self):
        with mock.patch.object(notify, "ALWAYS_PLAY", {"attention"}):
            self.assertFalse(self.run_rule("chat", sound="attention"))


class TestTurnStarts(Tmp):
    def setUp(self):
        super().setUp()
        for name, value in (("STAMP", self.tmp / ".turn-start"), ("STAMPS", self.tmp / ".turn-starts.json")):
            p = mock.patch.object(notify, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_each_chat_keeps_its_own_start(self):
        now = time.time()
        notify.record_start("cli-a", now - 60)
        notify.record_start("cli-b", now - 2)
        self.assertAlmostEqual(notify.turn_start("cli-a"), now - 60)
        self.assertAlmostEqual(notify.turn_start("cli-b"), now - 2)
        cfg = json.loads(json.dumps(notify.DEFAULTS))
        # chat A's long turn still chimes although chat B started a turn a moment ago
        self.assertEqual(notify.decide("Stop", {"session_id": "cli-a"}, cfg), "done")
        self.assertIsNone(notify.decide("Stop", {"session_id": "cli-b"}, cfg))

    def test_old_starts_are_dropped_and_the_shared_one_is_the_fallback(self):
        now = time.time()
        notify.record_start("old", now - 3 * 86400)
        notify.record_start("new", now)
        self.assertNotIn("old", json.loads(notify.STAMPS.read_text(encoding="utf-8")))
        self.assertAlmostEqual(notify.turn_start("unknown-chat"), now)    # the shared STAMP
        os.remove(notify.STAMP)
        self.assertIsNone(notify.turn_start("unknown-chat"))


if __name__ == "__main__":
    unittest.main()
