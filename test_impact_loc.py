#!/usr/bin/env python3
"""Focused tests for impact_loc's clone, blame, and line-count paths."""
import importlib.util
import io
import os
import signal
import shutil
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock
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


class TestGitFameFailures(unittest.TestCase):
    """A failed or empty git-fame result is a failed count, not zero."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ghw-fame-failures-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.dest = self.tmp / "repo"
        self.dest.mkdir()

    def install_git(self, script):
        fake_git = self.bin / "git"
        fake_git.write_text("#!/bin/sh\n" + script, encoding="utf-8")
        fake_git.chmod(0o755)

    def blame_with_fake_git(self, dest=None):
        with mock.patch.dict(os.environ, {"PATH": str(self.bin)}):
            return impact_loc.blame_repo(
                "outside/project", dest or self.dest, {"us@example.com"})

    def test_nonzero_exit_with_empty_stdout_raises(self):
        self.install_git("exit 1\n")

        with self.assertRaises(subprocess.CalledProcessError):
            self.blame_with_fake_git()

    def test_empty_stdout_at_zero_exit_is_rejected(self):
        self.install_git("exit 0\n")

        with self.assertRaisesRegex(ValueError, "empty git-fame output"):
            self.blame_with_fake_git()

    def test_missing_git_binary_error_propagates(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp)}):
            with self.assertRaises(FileNotFoundError):
                impact_loc.blame_repo(
                    "outside/project", self.dest, {"us@example.com"})

    def test_blame_moved_records_fame_error_and_continues(self):
        self.install_git(
            'case "$PWD" in */failure) exit 1 ;; *) '
            'printf \'%s\' \'{"total":{"loc":2},'
            '"data":[["us@example.com",2]]}\' ;; esac\n')
        failure = self.tmp / "failure"
        success = self.tmp / "success"
        failure.mkdir()
        success.mkdir()
        moved = [("outside/failure", {"branch": "main", "head": "h1"}),
                 ("outside/success", {"branch": "main", "head": "h2"})]

        def prefetch(_moved):
            yield moved[0][0], moved[0][1], failure, 0.0, 0.0, None
            yield moved[1][0], moved[1][1], success, 0.0, 0.0, None

        result = {}
        with mock.patch.object(impact_loc, "BLAME_METHOD", "fame"), \
                mock.patch.dict(os.environ, {"PATH": str(self.bin)}), \
                redirect_stdout(io.StringIO()):
            impact_loc.blame_moved(
                moved, result, {"us@example.com"}, prefetch_fn=prefetch,
                count_fn=impact_loc.counts_for)

        self.assertIn("error", result["outside/failure"])
        self.assertEqual(result["outside/success"], {
            "ours": 2, "total": 2, "branch": "main", "head": "h2"})


class TestScratchLifecycle(unittest.TestCase):
    """Clone scratch is tracked, cleaned on signals, and aged out safely."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ghw-scratch-lifecycle-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    @staticmethod
    def moved():
        return [("outside/project", {"branch": "main", "head": "h1"})]

    def test_new_scratch_is_registered_until_removed(self):
        with mock.patch.object(impact_loc, "clone_repo", return_value=0.0):
            gen = impact_loc.prefetched_clones(self.moved(), depth=1)
            try:
                _repo, _totals, scratch, _clone, _wait, _error = next(gen)
                registry = getattr(impact_loc, "_SCRATCH_DIRS", set())
                self.assertIn(scratch, registry)
                remover = getattr(impact_loc, "remove_scratch_dir", None)
                if remover is None:
                    shutil.rmtree(scratch, ignore_errors=True)
                else:
                    remover(scratch)
                self.assertNotIn(scratch, registry)
                self.assertFalse(scratch.exists())
            finally:
                gen.close()

    def test_signal_handlers_are_idempotent_and_remove_inflight_dirs(self):
        first = self.tmp / "first"
        second = self.tmp / "second"
        first.mkdir()
        second.mkdir()
        registry = {first, second}

        def fake_exit(code):
            raise SystemExit(code)

        with mock.patch.object(impact_loc, "_SCRATCH_DIRS", registry,
                              create=True), \
                mock.patch.object(impact_loc, "_SIGNAL_HANDLERS_INSTALLED",
                                  False, create=True), \
                mock.patch("signal.signal") as register, \
                mock.patch.object(impact_loc.os, "_exit",
                                  side_effect=fake_exit) as exit_process:
            installer = getattr(
                impact_loc, "install_scratch_signal_handlers", None)
            self.assertIsNotNone(installer)
            if installer is not None:
                installer()
                installer()
                registered = {call.args[0]: call.args[1]
                              for call in register.call_args_list}
                self.assertEqual(set(registered),
                                 {signal.SIGTERM, signal.SIGINT})
                with self.assertRaises(SystemExit) as raised:
                    registered[signal.SIGTERM](signal.SIGTERM, None)
            else:
                raised = None

        if raised is None:
            return
        self.assertEqual(raised.exception.code, 128 + signal.SIGTERM)
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        self.assertEqual(registry, set())
        exit_process.assert_called_once_with(128 + signal.SIGTERM)

    def test_blame_start_scavenges_only_old_noninflight_scratch(self):
        scratch_root = self.tmp / "tmp"
        scratch_root.mkdir()
        old = scratch_root / "impact-fame-old"
        inflight = scratch_root / "impact-fame-inflight"
        recent = scratch_root / "impact-fame-recent"
        for path in (old, inflight, recent):
            path.mkdir()
        old_time = time.time() - 2 * 60 * 60
        os.utime(old, (old_time, old_time))
        os.utime(inflight, (old_time, old_time))
        registry = {inflight}

        with mock.patch.object(impact_loc, "_SCRATCH_DIRS", registry,
                              create=True), \
                mock.patch.object(impact_loc.tempfile, "gettempdir",
                                  return_value=str(scratch_root)), \
                mock.patch.dict(os.environ,
                                {"IMPACT_SCRATCH_MAX_AGE_HOURS": "1"}):
            impact_loc.blame_moved([], {}, set(),
                                   prefetch_fn=lambda _moved: iter(()))

        self.assertFalse(old.exists())
        self.assertTrue(inflight.exists())
        self.assertTrue(recent.exists())


if __name__ == "__main__":
    unittest.main()
