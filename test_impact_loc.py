#!/usr/bin/env python3
"""Focused tests for impact_loc's clone, blame, and line-count paths."""
import importlib.util
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "render_impact_loc_tests", Path(__file__).with_name("render-impact.py"))
if spec is None or spec.loader is None:
    raise SystemExit("error: cannot load render-impact.py")
render_impact = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render_impact)
impact_loc = render_impact._LOC_MODULE


def git(*args, cwd):
    """Run a fixture-repository Git command without network access."""
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class TestCQuotedPaths(unittest.TestCase):
    """Git's default C quoting must not lose attributed lines."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ghw-quoted-path-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        git("init", "-q", "-b", "main", ".", cwd=self.tmp)
        git("config", "user.name", "Us", cwd=self.tmp)
        git("config", "user.email", "us@example.com", cwd=self.tmp)
        git("config", "core.quotePath", "true", cwd=self.tmp)

    def test_non_ascii_filename_is_counted_under_default_quote_path(self):
        (self.tmp / "café.py").write_text("one\ntwo\n", encoding="utf-8")
        git("add", "--all", cwd=self.tmp)
        git("commit", "-qm", "two lines by us", cwd=self.tmp)

        self.assertEqual(
            impact_loc.targeted_counts(self.tmp, {"us@example.com"}), (2, 2))

    def test_control_byte_paths_are_excluded_from_both_counts(self):
        (self.tmp / "safe.py").write_text("one\ntwo\n", encoding="utf-8")
        (self.tmp / "line\nbreak.py").write_text(
            "three\nfour\nfive\n", encoding="utf-8")
        git("add", "--all", cwd=self.tmp)
        git("commit", "-qm", "safe and unparseable paths", cwd=self.tmp)

        touched = impact_loc.our_touched_files(self.tmp, {"us@example.com"})
        self.assertEqual(touched, {"safe.py"})
        self.assertEqual(
            impact_loc.targeted_counts(self.tmp, {"us@example.com"}), (2, 2))

    def test_git_grep_no_matches_is_a_valid_empty_result(self):
        (self.tmp / "tracked.txt").write_text("present\n", encoding="utf-8")
        git("add", "--all", cwd=self.tmp)
        git("commit", "-qm", "tracked file", cwd=self.tmp)
        self.assertEqual(
            impact_loc.git_out(self.tmp, "grep", "-I", "--name-only",
                               "absent-needle", "HEAD"), "")

    def test_other_nonzero_git_status_raises(self):
        with self.assertRaises(subprocess.CalledProcessError):
            impact_loc.git_out(self.tmp, "not-a-git-command")


if __name__ == "__main__":
    unittest.main()
