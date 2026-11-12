#!/usr/bin/env python3
"""git-fame and targeted counting agree on names Git quotes or argv misreads."""
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
        # Git C-quotes `"`, `\` and control bytes under any setting, a
        # newline-split listing trims the outer spaces, and `blame` reads a
        # leading `-` as an option; both methods must count every one.
        names = ["plain.txt", "sp ace.txt", "α.txt", "-dash.txt"]
        if os.name == "posix":
            names += ['a"b.txt', "a\\b.txt", "tab\tx.txt",
                      " leading.txt", "trailing.txt ", "line\nbreak.txt"]
        cls.lines = 2 * len(names)
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
        self.assertEqual((fame, targeted),
                         ((self.lines,) * 2, (self.lines,) * 2))
        self.assertEqual(recorder.totals[("process", "git-fame")][0], 1)


if __name__ == "__main__":
    unittest.main()
