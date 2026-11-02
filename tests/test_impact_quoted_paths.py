#!/usr/bin/env python3
"""Targeted counting and git-fame agree on file names Git quotes.

    python3 -m unittest discover -v

git-fame lists files with `git ls-files` and blames each name it prints.
Under the default core.quotePath that name is C-quoted for any non-ASCII
byte, and `git blame` rejects the quoted form, so git-fame dropped the file
without a word while targeted counting kept it: the weekly `both` audit
failed on three Greek-lettered files in one repository (+964 lines).
"""
import importlib.util
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from tests import _ghw_temp


spec = importlib.util.spec_from_file_location(
    "render_impact", Path(__file__).resolve().parents[1] / "src" / "render-impact.py")
if spec is None or spec.loader is None:
    raise SystemExit("error: cannot load render-impact.py")
render_impact = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render_impact)
impact_loc = getattr(render_impact, "_LOC_MODULE")


def git(*args, cwd):
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class TestQuotedNamesAgree(unittest.TestCase):
    """Both methods count the same files whatever their names look like."""

    @classmethod
    def setUpClass(cls):
        if shutil.which("git-fame") is None:
            if os.environ.get("GH_WIDGETS_REQUIRE_GIT_FAME"):
                raise AssertionError(
                    "GH_WIDGETS_REQUIRE_GIT_FAME is set but git-fame is absent; "
                    "expected the pinned Nitjsefnie-OSC/git-fame build")
            raise unittest.SkipTest("git-fame is not installed")
        cls.tmp = Path(_ghw_temp.mkdtemp(prefix="ghw-quoted-names-"))
        git("init", "-q", "-b", "main", ".", cwd=cls.tmp)
        git("config", "user.name", "Us", cwd=cls.tmp)
        git("config", "user.email", "us@example.com", cwd=cls.tmp)
        git("config", "core.quotePath", "true", cwd=cls.tmp)
        # Counted: plain, space (never quoted) and non-ASCII (quoted only
        # under core.quotePath=true). Skipped by both: `"`, `\` and control
        # bytes, which Git quotes whatever the setting.
        names = ["plain.txt", "sp ace.txt", "α.txt"]
        if os.name == "posix":
            names += ['a"b.txt', "a\\b.txt", "tab\tx.txt"]
        for name in names:
            (cls.tmp / name).write_text("one\ntwo\n", encoding="utf-8")
        git("add", "--all", cwd=cls.tmp)
        git("commit", "-qm", "two lines per name", cwd=cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_blame_repo_and_targeted_counts_agree(self):
        emails = {"us@example.com"}
        shared = impact_loc.common
        recorder = shared.TimingRecorder()
        with mock.patch.object(shared, "DEBUG_TIMING", True), \
                mock.patch.object(shared, "timing", recorder):
            fame = impact_loc.blame_repo("outside/quoted", self.tmp, emails)
        targeted = impact_loc.targeted_counts(self.tmp, emails)
        self.assertEqual((fame, targeted), ((6, 6), (6, 6)))
        # The -c in front of `fame` must not move the child off its label.
        self.assertEqual(recorder.totals[("process", "git-fame")][0], 1)


class TestCountablePath(unittest.TestCase):
    """The shared filter rejects exactly the names Git quotes regardless."""

    def test_names_git_always_quotes_are_rejected(self):
        for name in ('a"b.txt', "a\\b.txt", "tab\tx.txt", "del\x7f.txt"):
            self.assertFalse(impact_loc._countable_path(name), name)  # pylint: disable=protected-access

    def test_names_git_leaves_unquoted_are_kept(self):
        for name in ("plain.txt", "sp ace.txt", "α.txt", "café.py"):
            self.assertTrue(impact_loc._countable_path(name), name)  # pylint: disable=protected-access


if __name__ == "__main__":
    unittest.main()
