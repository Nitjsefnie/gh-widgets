#!/usr/bin/env python3
"""Focused tests for impact_loc's clone, blame, and line-count paths."""
import importlib.util
import io
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager, redirect_stdout
from unittest import mock
from pathlib import Path

import impact_clone


spec = importlib.util.spec_from_file_location(
    "render_impact_loc_tests", Path(__file__).with_name("render-impact.py"))
if spec is None or spec.loader is None:
    raise SystemExit("error: cannot load render-impact.py")
render_impact = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render_impact)
impact_loc = getattr(render_impact, "_LOC_MODULE")


class CloneModuleExports(unittest.TestCase):
    def test_clone_names_are_reexports_of_impact_clone(self):
        for name in (
                "load_repo_pins", "checkout_pin", "clone_repo",
                "clone_lookahead", "register_scratch_dir",
                "remove_scratch_dir", "install_scratch_signal_handlers",
                "scavenge_scratch_dirs", "prefetched_clones"):
            with self.subTest(name=name):
                self.assertIs(getattr(impact_loc, name),
                              getattr(impact_clone, name))


def git(*args, cwd):
    """Run a fixture-repository Git command without network access."""
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@contextmanager
def run_python_git_fixture(script, processes=None):
    """Launch fake Git commands as real, portable Python subprocesses."""
    real_popen = subprocess.Popen

    def launch(command, *args, **kwargs):
        is_git = (isinstance(command, (list, tuple)) and command
                  and command[0] == "git")
        if is_git:
            command = [sys.executable, str(script), *command[1:]]
        # Production owns this real Popen's context and reap lifecycle.
        proc = real_popen(command, *args, **kwargs)  # pylint: disable=consider-using-with
        if is_git and processes is not None:
            processes.append(proc)
        return proc

    with mock.patch.object(subprocess, "Popen", new=launch):
        yield


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

    @unittest.skipUnless(os.name == "posix",
                         "Windows does not permit newline in file names")
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

    def test_control_paths_are_filtered_by_nul_parsers_portably(self):
        paths = "safe.py\0line\nbreak.py\0escape\x1b.py\0"
        counts = ("HEAD:safe.py\0 2\n"
                  "HEAD:line\nbreak.py\0 3\n"
                  "HEAD:escape\x1b.py\0 4\n")
        blame = ("a" * 40 + " 1 1 2\n"
                 "author-mail <us@example.com>\nfilename safe.py\n")
        with mock.patch.object(impact_loc, "git_out", side_effect=[
                paths, counts, paths, blame]) as git_output:
            self.assertEqual(
                impact_loc.targeted_counts(self.tmp, {"us@example.com"}),
                (2, 2))
        self.assertEqual(git_output.call_count, 4)
        self.assertEqual(git_output.call_args_list[-1].args[-1], "safe.py")

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

    def install_git(self, body):
        fake_git = self.bin / "git-fixture.py"
        expected = ["fame", "-e", "-w", "--format", "json"]
        source = (
            "import sys\n"
            f"expected = {expected!r}\n"
            "if sys.argv[1:] != expected:\n"
            "    sys.stderr.write('unexpected fake git argv: %r\\n' "
            "                     % (sys.argv[1:],))\n"
            "    sys.exit(97)\n"
            + body)
        fake_git.write_text(source, encoding="utf-8")

    def blame_with_fake_git(self, dest=None):
        with mock.patch.dict(os.environ, {"PATH": str(self.bin)}), \
                run_python_git_fixture(self.bin / "git-fixture.py"):
            return impact_loc.blame_repo(
                "outside/project", dest or self.dest, {"us@example.com"})

    def test_nonzero_exit_with_empty_stdout_raises(self):
        self.install_git("sys.exit(1)\n")

        with self.assertRaises(subprocess.CalledProcessError):
            self.blame_with_fake_git()

    def test_empty_stdout_at_zero_exit_is_rejected(self):
        self.install_git("pass\n")

        with self.assertRaisesRegex(ValueError, "empty git-fame output"):
            self.blame_with_fake_git()

    def test_missing_git_binary_error_propagates(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp)}):
            with self.assertRaises(FileNotFoundError):
                impact_loc.blame_repo(
                    "outside/project", self.dest, {"us@example.com"})

    def test_blame_moved_records_fame_error_and_continues(self):
        self.install_git(
            "import json, os, sys\n"
            "if os.path.basename(os.getcwd()) == 'failure':\n"
            "    sys.exit(1)\n"
            "print(json.dumps({'total': {'loc': 2}, 'data': "
            "[['us@example.com', 2]]}), end='')\n")
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
                run_python_git_fixture(self.bin / "git-fixture.py"), \
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

    def make_paused_clone_renderer(self):
        """Build a renderer with a release-gated, deadline-bounded clone.

        An interrupted run on 2026-10-01 left its fake git shim sleeping for
        about 27 hours after the worktree seat ended, so its test-finally
        cleanup died with the seat. The 600-second shim deadline is the
        last-resort defense when the harness itself is SIGKILLed; in-process
        cleanup cannot run in that case.
        """
        paths = {
            "scratch_root": self.tmp / "scratch",
            "mirror_root": self.tmp / "mirrors",
            "bin_dir": self.tmp / "bin",
            "signals": self.tmp / "signals",
            "boundary": self.tmp / "spawn-boundary",
            "release": self.tmp / "release-spawn",
            "scratch_path": self.tmp / "scratch-path",
            "wait_path": self.tmp / "handler-waiting-for-spawn",
        }
        paths["scratch_root"].mkdir()
        paths["mirror_root"].mkdir()
        paths["mirror"] = paths["mirror_root"] / "offline__repo"
        paths["mirror"].mkdir()
        paths["bin_dir"].mkdir()
        paths["signals"].mkdir()
        fake_git = paths["bin_dir"] / "git"
        expected = [
            "-c", "pack.threads=1", "-c", "pack.windowMemory=32m",
            "clone", "--single-branch", str(paths["mirror"])]
        fake_git.write_text(
            f"#!{sys.executable}\n"
            "import os, sys, time\n"
            f"expected = {expected!r}\n"
            "args = sys.argv[1:]\n"
            "if (len(args) != len(expected) + 1 or args[:-1] != expected "
            "or not args[-1].startswith(os.environ['TMPDIR'] + os.sep + "
            "'impact-fame-')):\n"
            "    sys.stderr.write('unexpected fake git argv: %r\\n' % args)\n"
            "    sys.exit(97)\n"
            "deadline = time.monotonic() + float(os.environ.get("
            "'GHW_FAKE_GIT_MAX_LIFE', '600'))\n"
            "remaining = deadline - time.monotonic()\n"
            "while remaining > 0:\n"
            "    time.sleep(min(1, remaining))\n"
            "    remaining = deadline - time.monotonic()\n",
            encoding="utf-8")
        fake_git.chmod(0o755)

        driver = self.tmp / "renderer.py"
        driver.write_text(
            "import importlib.util\n"
            "import os\n"
            "import subprocess\n"
            "import tempfile\n"
            "import threading\n"
            "import time\n"
            "from pathlib import Path\n"
            f"render_path = Path({str(Path(__file__).with_name('render-impact.py'))!r})\n"
            "spec = importlib.util.spec_from_file_location('signal_renderer', "
            "render_path)\n"
            "renderer = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(renderer)\n"
            "loc = renderer._LOC_MODULE\n"
            "clone = importlib.import_module('impact_clone')\n"
            "real_handler = clone._handle_scratch_signal\n"
            "def observed_handler(signum, frame):\n"
            "    Path(os.environ['SIGNAL_DIR'], str(signum)).write_text('seen')\n"
            "    real_handler(signum, frame)\n"
            "    Path(os.environ['SIGNAL_DIR'], str(signum) + '-done').write_text('done')\n"
            "clone._handle_scratch_signal = observed_handler\n"
            "wait_for_launches = getattr(clone, '_wait_for_clone_launches', None)\n"
            "if wait_for_launches is not None:\n"
            "    def observed_wait():\n"
            "        Path(os.environ['HANDLER_WAIT_FILE']).write_text('waiting')\n"
            "        wait_for_launches()\n"
            "    clone._wait_for_clone_launches = observed_wait\n"
            "loc.install_scratch_signal_handlers()\n"
            "real_popen = subprocess.Popen\n"
            "def paused_popen(*args, **kwargs):\n"
            "    proc = real_popen(*args, **kwargs)\n"
            "    command = args[0]\n"
            "    if command[0] == 'git' and 'clone' in command:\n"
            "        boundary = Path(os.environ['SPAWN_BOUNDARY_FILE'])\n"
            "        completed = boundary.with_name(boundary.name + '.tmp')\n"
            "        completed.write_text(\n"
            "            threading.current_thread().name + '\\n' + str(proc.pid),\n"
            "            encoding='utf-8')\n"
            "        os.replace(completed, boundary)\n"
            "        while not Path(os.environ['SPAWN_RELEASE_FILE']).exists():\n"
            "            proc.poll()\n"
            "            time.sleep(0.01)\n"
            "    return proc\n"
            "subprocess.Popen = paused_popen\n"
            "scratch = Path(tempfile.mkdtemp(prefix='impact-fame-', "
            "dir=os.environ['TMPDIR']))\n"
            "Path(os.environ['SCRATCH_PATH_FILE']).write_text(str(scratch))\n"
            "loc.register_scratch_dir(scratch)\n"
            "loc.clone_repo('offline/repo', 'main', scratch)\n",
            encoding="utf-8")

        env = {
            "PATH": str(paths["bin_dir"]),
            "TMPDIR": str(paths["scratch_root"]),
            "CLONE_SOURCE_DIR": str(paths["mirror_root"]),
            "SIGNAL_DIR": str(paths["signals"]),
            "SPAWN_BOUNDARY_FILE": str(paths["boundary"]),
            "SPAWN_RELEASE_FILE": str(paths["release"]),
            "SCRATCH_PATH_FILE": str(paths["scratch_path"]),
            "HANDLER_WAIT_FILE": str(paths["wait_path"]),
            "GHW_FAKE_GIT_MAX_LIFE": "600",
        }
        return driver, env, paths

    def run_fake_git_with_clock(self, lifetime):
        """Execute the generated shim with a monotonic clock that advances on sleep."""
        _driver, env, paths = self.make_paused_clone_renderer()
        env["GHW_FAKE_GIT_MAX_LIFE"] = str(lifetime)
        fake_git = paths["bin_dir"] / "git"
        expected = [
            "-c", "pack.threads=1", "-c", "pack.windowMemory=32m",
            "clone", "--single-branch", str(paths["mirror"])]
        destination = paths["scratch_root"] / "impact-fame-clock"

        class FakeClock:
            def __init__(self):
                self.now = 0.0
                self.sleeps = []

            def monotonic(self):
                return self.now

            def sleep(self, seconds):
                self.sleeps.append((self.now, seconds))
                self.now += seconds

        clock = FakeClock()
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(
                    sys, "argv", ["git", *expected, str(destination)]), \
                mock.patch.object(time, "monotonic",
                                  side_effect=clock.monotonic), \
                mock.patch.object(time, "sleep", side_effect=clock.sleep):
            # Executing the generated shim under this clock is the only deterministic lifetime-boundary pin.
            exec(  # pylint: disable=exec-used
                compile(fake_git.read_text(encoding="utf-8"),
                        str(fake_git), "exec"),
                {"__name__": "__main__"})
        return clock

    @staticmethod
    def wait_for_file(path, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return True
            time.sleep(0.01)
        return path.exists()

    def assert_renderer_and_clone_stopped(self, renderer, child_pid,
                                          paths, expected_status):
        stdout, stderr = renderer.communicate(timeout=5)
        self.assertEqual(renderer.returncode, expected_status,
                         f"stdout={stdout!r} stderr={stderr!r}")
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)
        self.assertFalse(Path(paths["scratch_path"].read_text()).exists())
        self.assertEqual(list(paths["scratch_root"].glob("impact-fame-*")), [])

    @staticmethod
    def stop_test_processes(renderer, child_pid, release, boundary=None):
        release.touch()
        if child_pid is None and boundary is not None and boundary.exists():
            boundary_lines = boundary.read_text(encoding="utf-8").splitlines()
            if len(boundary_lines) > 1:
                child_pid = int(boundary_lines[1])
        if child_pid is not None:
            try:
                os.killpg(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if renderer.poll() is None:
            renderer.kill()
            renderer.communicate(timeout=5)

    def test_new_scratch_is_registered_until_removed(self):
        with mock.patch.object(impact_clone, "clone_repo", return_value=0.0):
            gen = impact_loc.prefetched_clones(self.moved(), depth=1)
            try:
                _repo, _totals, scratch, _clone, _wait, _error = next(gen)
                registry = getattr(impact_clone, "_SCRATCH_DIRS", set())
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

    def test_owner_lock_sidecar_keeps_real_clone_destination_empty(self):
        mirror = self.tmp / "mirrors" / "offline__repo"
        mirror.mkdir(parents=True)
        git("init", "-q", "-b", "main", ".", cwd=mirror)
        git("config", "user.name", "Us", cwd=mirror)
        git("config", "user.email", "us@example.com", cwd=mirror)
        (mirror / "tracked.txt").write_text(
            "offline clone\n", encoding="utf-8")
        git("add", "--all", cwd=mirror)
        git("commit", "-qm", "fixture", cwd=mirror)

        scratch_root = self.tmp / "tmp"
        scratch_root.mkdir()
        dest = Path(tempfile.mkdtemp(prefix="impact-fame-", dir=scratch_root))
        impact_loc.register_scratch_dir(dest)
        sidecar = dest.with_name(dest.name + ".owner.lock")
        try:
            with mock.patch.dict(os.environ,
                                 {"CLONE_SOURCE_DIR": str(mirror.parent)}):
                impact_loc.clone_repo("offline/repo", "main", dest)
            self.assertTrue((dest / ".git").is_dir())
            self.assertTrue(sidecar.exists())
        finally:
            impact_loc.remove_scratch_dir(dest)

        self.assertFalse(dest.exists())
        self.assertFalse(sidecar.exists())

    def test_remove_scratch_dir_retries_readonly_entries(self):
        dest = self.tmp / "impact-fame-readonly"
        dest.mkdir()
        readonly = dest / "git-object"
        readonly.write_text("fixture", encoding="utf-8")
        readonly.chmod(0o444)
        impact_loc.register_scratch_dir(dest)

        real_unlink = os.unlink
        real_chmod = os.chmod
        unlink_attempts = []
        chmod_attempts = []

        def fail_first_unlink(path, *args, **kwargs):
            if os.path.basename(os.fsdecode(path)) == readonly.name:
                unlink_attempts.append(path)
                if len(unlink_attempts) == 1:
                    raise PermissionError("fixture read-only file")
            return real_unlink(path, *args, **kwargs)

        def record_chmod(path, mode):
            if os.path.basename(os.fsdecode(path)) == readonly.name:
                chmod_attempts.append((path, mode))
            return real_chmod(path, mode)

        with mock.patch("os.unlink", side_effect=fail_first_unlink), \
                mock.patch("os.chmod", side_effect=record_chmod):
            impact_loc.remove_scratch_dir(dest)

        self.assertEqual(len(unlink_attempts), 2)
        self.assertEqual(len(chmod_attempts), 1)
        self.assertFalse(dest.exists())

    def test_signal_handlers_are_idempotent_and_remove_inflight_dirs(self):
        first = self.tmp / "first"
        second = self.tmp / "second"
        first.mkdir()
        second.mkdir()
        registry = {first, second}

        def fake_exit(code):
            raise SystemExit(code)

        with mock.patch.object(impact_clone, "_SCRATCH_DIRS", registry,
                               create=True), \
                mock.patch.object(impact_clone, "_CLONE_SHUTDOWN", False,
                                  create=True), \
                mock.patch.object(impact_clone, "_SIGNAL_CLEANUP_CLAIMS",
                                  impact_clone.itertools.count(), create=True), \
                mock.patch.object(impact_clone, "_SIGNAL_HANDLERS_INSTALLED",
                                  False, create=True), \
                mock.patch("signal.signal") as register, \
                mock.patch.object(impact_loc.os, "_exit",
                                  side_effect=fake_exit) as exit_process:
            impact_loc.install_scratch_signal_handlers()
            impact_loc.install_scratch_signal_handlers()
            self.assertEqual(register.call_count, 2)
            registrations = [call.args for call in register.call_args_list]
            self.assertEqual([call[0] for call in registrations],
                             [signal.SIGTERM, signal.SIGINT])
            with self.assertRaises(SystemExit) as raised:
                registrations[0][1](signal.SIGTERM, None)

        self.assertEqual(raised.exception.code, 128 + signal.SIGTERM)
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        self.assertEqual(registry, set())
        exit_process.assert_called_once_with(128 + signal.SIGTERM)

    def test_signal_handlers_are_not_installed_from_a_worker_thread(self):
        with mock.patch.object(impact_clone, "_SIGNAL_HANDLERS_INSTALLED",
                               False, create=True), \
                mock.patch("signal.signal") as register:
            worker = threading.Thread(
                target=impact_loc.install_scratch_signal_handlers)
            worker.start()
            worker.join(timeout=3)

        self.assertFalse(worker.is_alive())
        register.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "requires POSIX signal delivery")
    def test_paused_clone_shim_exits_after_configured_lifetime(self):
        driver, env, paths = self.make_paused_clone_renderer()
        env["GHW_FAKE_GIT_MAX_LIFE"] = "1"
        shim_source = (paths["bin_dir"] / "git").read_text(encoding="utf-8")

        with subprocess.Popen(
                [sys.executable, str(driver)], env=env, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
                start_new_session=True) as renderer:
            child_pid = None
            self.addCleanup(
                self.stop_test_processes, renderer, None, paths["release"],
                paths["boundary"])
            try:
                self.assertTrue(self.wait_for_file(paths["boundary"]))
                child_pid = int(paths["boundary"].read_text().splitlines()[1])
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    try:
                        os.kill(child_pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.02)
                else:
                    self.fail("fake git child remained alive past its deadline")

                self.assertIn(
                    "float(os.environ.get('GHW_FAKE_GIT_MAX_LIFE', '600'))",
                    shim_source)
                os.kill(renderer.pid, signal.SIGTERM)
                self.assertTrue(self.wait_for_file(
                    paths["signals"] / str(int(signal.SIGTERM))))
                paths["release"].touch()
                self.assert_renderer_and_clone_stopped(
                    renderer, child_pid, paths, 128 + signal.SIGTERM)
            finally:
                self.stop_test_processes(renderer, child_pid, paths["release"])

    def test_fake_git_shim_stays_alive_inside_configured_lifetime(self):
        clock = self.run_fake_git_with_clock(lifetime=4)

        self.assertTrue(
            any(start < 4 for start, _seconds in clock.sleeps),
            "fake git shim exited before sleeping inside its configured lifetime")

    def test_fake_git_shim_exits_at_configured_lifetime(self):
        clock = self.run_fake_git_with_clock(lifetime=4)

        self.assertEqual(
            clock.now, 4,
            "fake git shim did not exit when its configured lifetime elapsed")

    @unittest.skipUnless(os.name == "posix", "requires POSIX signal delivery")
    def test_renderer_signal_stops_clone_writer_before_removing_scratch(self):
        scratch_root = self.tmp / "scratch"
        scratch_root.mkdir()
        mirror_root = self.tmp / "mirrors"
        (mirror_root / "offline__repo").mkdir(parents=True)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        pid_file = self.tmp / "clone.pid"
        dest_file = self.tmp / "clone.dest"
        late_file = self.tmp / "late-write"
        fake_git = bin_dir / "git"
        expected_prefix = [
            "-c", "pack.threads=1", "-c", "pack.windowMemory=32m",
            "clone", "--single-branch",
            str(mirror_root / "offline__repo")]
        fake_git.write_text(
            f"#!{sys.executable}\n"
            "import os, signal, sys, time\n"
            "from pathlib import Path\n"
            f"expected_prefix = {expected_prefix!r}\n"
            "args = sys.argv[1:]\n"
            "if args[:-1] != expected_prefix or not args[-1].startswith("
            "os.environ['TMPDIR'] + os.sep + 'impact-fame-'):\n"
            "    sys.exit(97)\n"
            "dest = Path(args[-1])\n"
            "Path(os.environ['CLONE_CHILD_PID_FILE']).write_text("
            "str(os.getpid()))\n"
            "Path(os.environ['CLONE_DEST_FILE']).write_text(str(dest))\n"
            "os.kill(os.getppid(), signal.SIGTERM)\n"
            "time.sleep(0.5)\n"
            "dest.mkdir(parents=True, exist_ok=True)\n"
            "Path(os.environ['CLONE_LATE_FILE']).write_text('late')\n",
            encoding="utf-8")
        fake_git.chmod(0o755)

        driver = self.tmp / "renderer.py"
        driver.write_text(
            "import importlib.util\n"
            "from pathlib import Path\n"
            f"path = Path({str(Path(__file__).with_name('render-impact.py'))!r})\n"
            "spec = importlib.util.spec_from_file_location('signal_renderer', path)\n"
            "renderer = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(renderer)\n"
            "loc = renderer._LOC_MODULE\n"
            "loc.blame_moved([('offline/repo', "
            "{'branch': 'main', 'head': None})], {}, set())\n",
            encoding="utf-8")
        env = {
            "PATH": str(bin_dir),
            "TMPDIR": str(scratch_root),
            "CLONE_SOURCE_DIR": str(mirror_root),
            "CLONE_CHILD_PID_FILE": str(pid_file),
            "CLONE_DEST_FILE": str(dest_file),
            "CLONE_LATE_FILE": str(late_file),
        }
        renderer = subprocess.run(
            [sys.executable, str(driver)], env=env, capture_output=True,
            text=True, timeout=15, check=False)
        time.sleep(0.6)

        self.assertEqual(renderer.returncode, 128 + signal.SIGTERM,
                         renderer.stderr)
        child_pid = int(pid_file.read_text(encoding="utf-8"))
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)
        self.assertFalse(late_file.exists())
        destination = Path(dest_file.read_text(encoding="utf-8"))
        self.assertFalse(destination.exists())
        self.assertEqual(list(scratch_root.glob("impact-fame-*")), [])

    @unittest.skipUnless(os.name == "posix", "requires POSIX signal delivery")
    def test_signal_at_main_thread_spawn_boundary_stops_clone_before_exit(self):
        driver, env, paths = self.make_paused_clone_renderer()
        with subprocess.Popen(
                [sys.executable, str(driver)], env=env, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
                start_new_session=True) as renderer:
            child_pid = None
            self.addCleanup(
                self.stop_test_processes, renderer, None, paths["release"],
                paths["boundary"])
            try:
                self.assertTrue(self.wait_for_file(paths["boundary"]))
                boundary = paths["boundary"].read_text().splitlines()
                child_pid = int(boundary[1])
                os.kill(renderer.pid, signal.SIGTERM)
                self.assertTrue(self.wait_for_file(
                    paths["signals"] / str(int(signal.SIGTERM))))
                paths["release"].touch()
                self.assert_renderer_and_clone_stopped(
                    renderer, child_pid, paths, 128 + signal.SIGTERM)
                self.assertNotEqual(boundary[0], "MainThread")
            finally:
                self.stop_test_processes(renderer, child_pid, paths["release"])

    @unittest.skipUnless(os.name == "posix", "requires POSIX signal delivery")
    def test_second_signal_during_spawn_cleanup_does_not_deadlock(self):
        driver, env, paths = self.make_paused_clone_renderer()
        with subprocess.Popen(
                [sys.executable, str(driver)], env=env, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
                start_new_session=True) as renderer:
            child_pid = None
            self.addCleanup(
                self.stop_test_processes, renderer, None, paths["release"],
                paths["boundary"])
            try:
                self.assertTrue(self.wait_for_file(paths["boundary"]))
                child_pid = int(paths["boundary"].read_text().splitlines()[1])
                os.kill(renderer.pid, signal.SIGTERM)
                handler_waiting = self.wait_for_file(
                    paths["wait_path"], timeout=2)
                os.kill(renderer.pid, signal.SIGINT)
                nested_returned = self.wait_for_file(
                    paths["signals"] / f"{signal.SIGINT}-done", timeout=2)
                paths["release"].touch()
                self.assertTrue(handler_waiting,
                                "handler did not wait for pending clone spawn")
                self.assertTrue(nested_returned,
                                "nested signal handler did not return")
                self.assert_renderer_and_clone_stopped(
                    renderer, child_pid, paths, 128 + signal.SIGTERM)
            finally:
                self.stop_test_processes(renderer, child_pid, paths["release"])

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

        with mock.patch.object(impact_clone, "_SCRATCH_DIRS", registry,
                               create=True), \
                mock.patch.object(impact_clone.tempfile, "gettempdir",
                                  return_value=str(scratch_root)), \
                mock.patch.dict(os.environ,
                                {"IMPACT_SCRATCH_MAX_AGE_HOURS": "1"}):
            impact_loc.blame_moved([], {}, set(),
                                   prefetch_fn=lambda _moved: iter(()))

        self.assertFalse(old.exists())
        self.assertTrue(inflight.exists())
        self.assertTrue(recent.exists())

    def test_scavenger_preserves_live_process_scratch_and_removes_abandoned(self):
        scratch_root = self.tmp / "tmp"
        scratch_root.mkdir()
        live = scratch_root / "impact-fame-live-owner"
        abandoned = scratch_root / "impact-fame-abandoned"
        abandoned.mkdir()
        old_time = time.time() - 2 * 60 * 60
        os.utime(abandoned, (old_time, old_time))
        owner_script = (
            "import os, sys, time\n"
            "from pathlib import Path\n"
            "scratch = Path(sys.argv[1])\n"
            "scratch.mkdir()\n"
            "lock_file = scratch.with_name(scratch.name + '.owner.lock')\n"
            "with lock_file.open('a+b') as stream:\n"
            "    if os.name == 'nt':\n"
            "        import msvcrt\n"
            "        if lock_file.stat().st_size == 0:\n"
            "            stream.write(b'0')\n"
            "            stream.flush()\n"
            "        stream.seek(0)\n"
            "        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)\n"
            "    else:\n"
            "        import fcntl\n"
            "        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | "
            "fcntl.LOCK_NB)\n"
            "    old = time.time() - 7200\n"
            "    os.utime(scratch, (old, old))\n"
            "    print('READY', flush=True)\n"
            "    sys.stdin.readline()\n")
        with subprocess.Popen(
                [sys.executable, "-c", owner_script, str(live)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True) as owner:
            assert owner.stdout is not None
            assert owner.stderr is not None
            assert owner.stdin is not None
            owner_stdout = owner.stdout
            owner_stderr = owner.stderr
            owner_stdin = owner.stdin
            try:
                ready = owner_stdout.readline()
                if ready != "READY\n":
                    self.fail(f"scratch owner did not become ready: "
                              f"{owner_stderr.read()}")
                with mock.patch.object(impact_clone, "_SCRATCH_DIRS", set(),
                                       create=True), \
                        mock.patch.object(impact_clone.tempfile, "gettempdir",
                                          return_value=str(scratch_root)), \
                        mock.patch.dict(
                            os.environ, {"IMPACT_SCRATCH_MAX_AGE_HOURS": "1"}):
                    impact_loc.scavenge_scratch_dirs()

                self.assertFalse(abandoned.exists())
                self.assertTrue(live.exists())
                owner_stdin.write("release\n")
                owner_stdin.flush()
                self.assertEqual(owner.wait(timeout=5), 0)
                with mock.patch.object(impact_clone, "_SCRATCH_DIRS", set(),
                                       create=True), \
                        mock.patch.object(impact_clone.tempfile, "gettempdir",
                                          return_value=str(scratch_root)), \
                        mock.patch.dict(
                            os.environ,
                            {"IMPACT_SCRATCH_MAX_AGE_HOURS": "1"}):
                    impact_loc.scavenge_scratch_dirs()
                self.assertFalse(live.exists())
            finally:
                if owner.poll() is None:
                    owner.kill()
                owner.communicate(timeout=5)


class TestBoundedGitOutput(unittest.TestCase):
    """Git stdout capture and clone fan-out have explicit ceilings."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ghw-bounded-git-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.dest = self.tmp / "repo"
        self.dest.mkdir()

    def install_git(self, source, allowed_args):
        fake_git = self.bin / "git-fixture.py"
        wrapper = (
            "import sys\n"
            f"allowed_args = {allowed_args!r}\n"
            "if sys.argv[1:] not in allowed_args:\n"
            "    sys.stderr.write('unexpected fake git argv: %r\\n' "
            "                     % (sys.argv[1:],))\n"
            "    sys.exit(97)\n"
            + source)
        fake_git.write_text(wrapper, encoding="utf-8")

    def git_out_args(self, command):
        return ["-C", str(self.dest), "-c", "core.quotePath=false", command]

    @staticmethod
    def fame_args():
        return ["fame", "-e", "-w", "--format", "json"]

    def cap_environment(self):
        return {"PATH": str(self.bin), "GIT_OUTPUT_CAP_MB": "0.001"}

    def assert_child_killed(self, processes, pid_file, marker):
        self.assertTrue(pid_file.is_file(), "fake git did not start")
        pid = int(pid_file.read_text(encoding="utf-8"))
        self.assertEqual(len(processes), 1)
        self.assertEqual(processes[0].pid, pid)
        self.assertIsNotNone(processes[0].poll(),
                             "overflow subprocess is still running")
        self.assertFalse(marker.exists(), "overflow child completed normally")

    def overflow_program(self, pid_file, marker):
        return (
            "import os, time\n"
            "from pathlib import Path\n"
            f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
            "os.write(1, b'x' * 4096)\n"
            "time.sleep(2)\n"
            f"Path({str(marker)!r}).write_text('survived')\n")

    def test_git_out_rejects_overflow_and_kills_the_child(self):
        pid_file = self.tmp / "git.pid"
        marker = self.tmp / "git-survived"
        self.install_git(self.overflow_program(pid_file, marker),
                         [self.git_out_args("version")])
        processes = []

        with mock.patch.dict(os.environ, {
                **self.cap_environment(),
                "GHW_CHILD_PID_FILE": str(pid_file)}), \
                run_python_git_fixture(self.bin / "git-fixture.py", processes):
            with self.assertRaisesRegex(RuntimeError, "output exceeded"):
                impact_loc.git_out(self.dest, "version")

        self.assert_child_killed(processes, pid_file, marker)

    def test_git_fame_rejects_overflow_and_kills_the_child(self):
        pid_file = self.tmp / "fame.pid"
        marker = self.tmp / "fame-survived"
        self.install_git(self.overflow_program(pid_file, marker),
                         [self.fame_args()])
        processes = []

        with mock.patch.dict(os.environ, {
                **self.cap_environment(),
                "GHW_CHILD_PID_FILE": str(pid_file)}), \
                run_python_git_fixture(self.bin / "git-fixture.py", processes):
            with self.assertRaisesRegex(RuntimeError, "output exceeded"):
                impact_loc.blame_repo(
                    "outside/project", self.dest, {"us@example.com"})

        self.assert_child_killed(processes, pid_file, marker)

    def test_output_under_cap_is_preserved_for_git_and_git_fame(self):
        self.install_git(
            "import sys\n"
            "sys.stdout.write('{\"total\":{\"loc\":2},"
            "\"data\":[[\"us@example.com\",2]]}')\n",
            [self.git_out_args("version"), self.fame_args()])
        with mock.patch.dict(os.environ, self.cap_environment()), \
                run_python_git_fixture(self.bin / "git-fixture.py"):
            self.assertEqual(impact_loc.git_out(self.dest, "version"),
                             '{"total":{"loc":2},"data":[["us@example.com",2]]}')
            self.assertEqual(
                impact_loc.blame_repo(
                    "outside/project", self.dest, {"us@example.com"}),
                (2, 2))

    def test_posix_bounded_reader_does_not_start_a_reader_thread(self):
        child = "import os; os.write(1, b'bounded output\\n')"
        started = []
        original_start = threading.Thread.start

        def record_start(thread, *args, **kwargs):
            started.append(thread.name)
            return original_start(thread, *args, **kwargs)

        with mock.patch.object(threading.Thread, "start", record_start):
            result = impact_loc._run_bounded(  # pylint: disable=protected-access
                [sys.executable, "-c", child], timeout=5)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"bounded output\n")
        expected_reader_threads = [] if os.name == "posix" else [
            "git-output-reader"]
        self.assertEqual(
            [name for name in started if name == "git-output-reader"],
            expected_reader_threads)

    def test_fake_git_rejects_unmodeled_command_shapes(self):
        self.install_git("pass\n", [self.git_out_args("version")])
        with mock.patch.dict(os.environ, self.cap_environment()), \
                run_python_git_fixture(self.bin / "git-fixture.py"):
            with self.assertRaises(subprocess.CalledProcessError) as raised:
                impact_loc.git_out(self.dest, "status")
        self.assertEqual(raised.exception.returncode, 97)

    def test_clone_lookahead_is_clamped_above_eight(self):
        with mock.patch.dict(os.environ, {"CLONE_LOOKAHEAD": "100000"}):
            self.assertEqual(impact_loc.clone_lookahead(), 16)

    def test_clone_lookahead_keeps_normal_values_and_default(self):
        with mock.patch.dict(os.environ, {"CLONE_LOOKAHEAD": "8"}):
            self.assertEqual(impact_loc.clone_lookahead(), 8)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(impact_loc.clone_lookahead(), 3)


if __name__ == "__main__":
    unittest.main()
