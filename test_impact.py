#!/usr/bin/env python3
"""Tests for render-impact.py's external dependency, git-fame.

    python3 -m unittest discover -v

render-impact.py shells out to `git fame` for its blame pass. Stock git-fame
blames one file per subprocess, serially: measured on a 477-file repository,
480 processes and 41% of wall time in process spawn alone. We run a patched
build (Nitjsefnie-OSC/git-fame) that adds `--jobs`.

These tests pin the CAPABILITY, not the timing — a timing assertion would be
flaky on a shared box, and a slow run is not a wrong run. The invariant that
matters is that parallelism does not change the numbers.
"""
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


spec = importlib.util.spec_from_file_location(
    "render_impact", Path(__file__).with_name("render-impact.py"))
if spec is None or spec.loader is None:
    raise SystemExit("error: cannot load render-impact.py")
render_impact = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render_impact)


def git(*args, cwd):
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def fame(cwd, *extra):
    # Deliberately the `git fame` form here, not `git-fame`: this is the same
    # `git fame` dispatch path render-impact.py's blame_repo uses, plus `-s`
    # (--silent-progress) so the test run stays quiet. Only `--help` is
    # special-cased by git's dispatcher, which is why the capability probe
    # below uses the hyphenated binary.
    out = subprocess.run(["git", "fame", "-s", "-e", "-w", "--format", "json", *extra],
                         cwd=str(cwd), capture_output=True, text=True, check=True)
    return out.stdout


class TestGitFameParallel(unittest.TestCase):
    """The blame pass depends on a git-fame that supports --jobs."""

    @classmethod
    def setUpClass(cls):
        if shutil.which("git-fame") is None:
            raise unittest.SkipTest("git-fame is not installed")
        cls.tmp = Path(tempfile.mkdtemp(prefix="ghw-fame-"))
        git("init", "-q", "-b", "main", ".", cwd=cls.tmp)
        git("config", "user.email", "t@example.com", cwd=cls.tmp)
        git("config", "user.name", "T", cwd=cls.tmp)
        for i in range(12):
            (cls.tmp / f"f{i}.txt").write_text(f"line one {i}\nline two {i}\n")
        git("add", "-A", cwd=cls.tmp)
        git("commit", "-qm", "initial", cwd=cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_jobs_option_supported(self):
        """The pinned build must accept --jobs.

        Fails on stock git-fame from PyPI, which is the point: without the
        patch the blame pass is serial and render-impact runs take minutes.
        """
        # `git-fame`, NOT `git fame` — git rewrites `git <cmd> --help` into
        # `man git-<cmd>`, which reports no manual entry and would make this
        # assertion fail against a correctly patched build.
        help_text = subprocess.run(["git-fame", "--help"],
                                   capture_output=True, text=True, check=True).stdout
        self.assertIn("--jobs", help_text,
                      "installed git-fame lacks --jobs; expected the pinned "
                      "Nitjsefnie-OSC build (see CLAUDE.md)")

    def test_parallel_matches_serial(self):
        """Parallelism must not change the counts render-impact reads."""
        serial = json.loads(fame(self.tmp, "-j", "1"))
        parallel = json.loads(fame(self.tmp, "-j", "4"))
        self.assertEqual(serial, parallel)
        self.assertEqual(serial["total"]["loc"], 24)


class TestGitFameRuntimeCheck(unittest.TestCase):
    """The runtime check must be self-proving: loud on success, not silent."""

    @classmethod
    def setUpClass(cls):
        if shutil.which("git-fame") is None:
            raise unittest.SkipTest("git-fame is not installed")

    def test_check_is_noisy_on_success(self):
        """Success must print — a silent success cannot be distinguished from
        the check never having run, which is the whole point of it."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            ok = render_impact.check_git_fame()
        out = buf.getvalue()
        self.assertTrue(ok, f"check reported stock git-fame: {out!r}")
        self.assertIn("git-fame:", out)
        self.assertIn("--jobs", out)
        self.assertTrue(out.strip(), "check was silent on success")


class TestGitFameRuntimeCheckAlarms(unittest.TestCase):
    """The alarm branches must also be self-proving; a silent regression is
    the worst kind. These tests deliberately manipulate PATH so they run even
    on boxes where the real git-fame is absent or already patched."""

    def _check_with_path(self, path):
        """Run check_git_fame() with PATH replaced by *path*, capturing stdout."""
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"PATH": path}):
            with redirect_stdout(buf):
                ok = render_impact.check_git_fame()
        return ok, buf.getvalue()

    def test_stock_git_fame_warns_instead_of_lieing(self):
        """A git-fame without --jobs is detected and warned about."""
        with tempfile.TemporaryDirectory() as td:
            fake_bin = Path(td)
            script = fake_bin / "git-fame"
            script.write_text(
                "#!/bin/sh\n"
                "echo 'usage: git-fame [--format FORMAT] [--help]'\n",
                encoding="utf-8")
            script.chmod(0o755)
            path = f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"
            ok, out = self._check_with_path(path)
        self.assertFalse(ok, "stock git-fame must be rejected")
        self.assertIn("WARNING", out)

    def test_missing_git_fame_fails_loudly(self):
        """git-fame missing from PATH must not be treated as success."""
        with tempfile.TemporaryDirectory() as td:
            ok, out = self._check_with_path(td)
        self.assertFalse(ok, "missing git-fame must be rejected")
        self.assertIn("CHECK FAILED", out)


if __name__ == "__main__":
    unittest.main()
