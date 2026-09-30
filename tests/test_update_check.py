"""update_check against real throwaway git repos (a local bare repo stands in for GitHub; offline).

    python -m unittest discover -s tests
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import update_check  # noqa: E402

TMP_BASE = ROOT / ".tmp-tests"  # inside the repo (git-ignored), never the system temp drive


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)
    return done.stdout


def commit(repo: Path, name: str, text: str, message: str) -> None:
    (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message)


@unittest.skipIf(shutil.which("git") is None, "git is not installed")
class UpdateCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        TMP_BASE.mkdir(exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(dir=TMP_BASE))
        self.github = self.tmp / "github.git"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.github))
        self.dev = self.tmp / "dev"  # the maintainer's copy
        git(self.tmp, "clone", "-q", str(self.github), str(self.dev))
        git(self.dev, "checkout", "-q", "-b", "main")
        commit(self.dev, "belt.py", "v1", "First version")
        git(self.dev, "push", "-q", "-u", "origin", "main")
        self.user = self.tmp / "user"  # someone who cloned it
        git(self.tmp, "clone", "-q", str(self.github), str(self.user))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def publish(self, name: str, text: str, message: str) -> None:
        commit(self.dev, name, text, message)
        git(self.dev, "push", "-q")

    def test_up_to_date_copy_reports_nothing_new(self):
        info = update_check.check(self.user)
        self.assertIsNotNone(info)
        self.assertEqual(info.behind, 0)
        self.assertFalse(info.available)

    def test_new_commits_on_github_are_counted_with_their_titles_newest_first(self):
        self.publish("belt.py", "v2", "Faster disk panel")
        self.publish("notify.py", "x", "Quieter sounds")
        info = update_check.check(self.user)
        self.assertEqual(info.behind, 2)
        self.assertTrue(info.available)
        self.assertEqual(info.titles, ["Quieter sounds", "Faster disk panel"])
        self.assertFalse(info.deps_changed)
        self.assertEqual(info.commands(), [f'git -C "{self.user}" pull'])

    def test_changed_requirements_add_the_pip_step(self):
        self.publish("requirements.txt", "textual>=9\n", "Need Textual 9")
        info = update_check.check(self.user)
        self.assertTrue(info.deps_changed)
        self.assertEqual(len(info.commands()), 2)
        self.assertIn("pip install -r", info.commands()[1])
        self.assertIn(str(self.user / "requirements.txt"), info.commands()[1])

    def test_the_suggested_pull_command_really_updates_the_copy(self):
        self.publish("belt.py", "v2", "Faster disk panel")
        info = update_check.check(self.user)
        subprocess.run(info.pull_command, shell=True, check=True, capture_output=True)
        self.assertEqual((self.user / "belt.py").read_text(encoding="utf-8"), "v2")
        self.assertEqual(update_check.check(self.user).behind, 0)

    def test_a_maintainer_ahead_of_github_is_not_told_to_update(self):
        commit(self.dev, "belt.py", "local", "Not pushed yet")
        self.assertEqual(update_check.check(self.dev).behind, 0)

    def test_cannot_tell_gives_none(self):
        self.assertIsNone(update_check.check(self.tmp))  # not a git clone (e.g. a ZIP download)
        git(self.user, "checkout", "-q", "--detach")
        self.assertIsNone(update_check.check(self.user))  # detached HEAD tracks nothing

    def test_unreachable_github_gives_none_instead_of_hanging_or_prompting(self):
        git(self.user, "remote", "set-url", "origin", str(self.tmp / "gone.git"))
        self.assertIsNone(update_check.check(self.user))


if __name__ == "__main__":
    unittest.main()
