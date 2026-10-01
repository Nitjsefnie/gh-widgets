#!/usr/bin/env python3
"""Deterministic coverage for impact_loc's guarded cleanup paths."""
# These tests deliberately exercise module-private lifecycle boundaries.
# pylint: disable=protected-access
import importlib.util
import io
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import impact_clone


spec = importlib.util.spec_from_file_location(
    "render_impact_loc_coverage_tests",
    Path(__file__).with_name("render-impact.py"))
if spec is None or spec.loader is None:
    raise SystemExit("error: cannot load render-impact.py")
render_impact = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render_impact)
impact_loc = getattr(render_impact, "_LOC_MODULE")


class SiblingLoaderLifecycle(unittest.TestCase):
    """Exercise actual donor execution in virgin, isolated interpreters."""

    def run_scenario(self, body, fallback=False):
        preamble = '''
            import importlib.machinery
            import importlib.util
            import sys
            import threading
            import types
            from pathlib import Path
            from unittest.mock import patch

            root, filename, sibling, public_name, fallback = sys.argv[1:]
            if fallback == 'True':
                # Hide the private API from donors, preserving normal import.
                bootstrap = types.SimpleNamespace(
                    **vars(importlib._bootstrap))
                bootstrap._ModuleLockManager = None
                importlib._bootstrap = bootstrap
            assert sibling not in sys.modules
            assert Path(root) not in [Path(p).resolve() for p in sys.path]
            entered = threading.Event()
            release = threading.Event()
            started = threading.Event()
            finished = threading.Event()
            results = []
            errors = []

            def load(alias):
                spec = importlib.util.spec_from_file_location(
                    alias, Path(root) / filename)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                return module

            def worker(alias):
                started.set()
                try:
                    results.append(load(alias))
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    finished.set()

            def join(threads):
                release.set()
                for thread in threads:
                    thread.join(timeout=5)
                    assert not thread.is_alive(), 'loader thread hung'
        '''
        # Common has two siblings; exercise each independently, plus clone.
        for filename, sibling, public_name in (
                ('ghwidgets_common.py', 'ghwidgets_cache', 'load_cache'),
                ('ghwidgets_common.py', 'ghwidgets_journal', 'acquisition'),
                ('impact_loc.py', 'impact_clone', 'clone_repo')):
            with self.subTest(sibling=sibling, fallback=fallback):
                env = dict(os.environ)
                env.pop('PYTHONPATH', None)
                with tempfile.TemporaryDirectory() as unrelated:
                    result = subprocess.run(
                        [sys.executable, '-I', '-B', '-c',
                         textwrap.dedent(preamble) + textwrap.dedent(body),
                         str(Path(__file__).resolve().parent), filename,
                         sibling, public_name, str(fallback)],
                        cwd=unrelated, env=env, capture_output=True, text=True,
                        timeout=15, check=False)
                self.assertEqual(result.returncode, 0,
                                 result.stdout + result.stderr)

    def test_overlapping_path_loads_share_modules_and_registries(self, fallback=False):
        self.run_scenario('''
            original = importlib.util.module_from_spec
            def allocate(spec):
                module = original(spec)
                if spec.name == sibling and not entered.is_set():
                    entered.set()
                    assert release.wait(timeout=5), 'allocation not released'
                return module

            with patch('importlib.util.module_from_spec', allocate):
                first = threading.Thread(target=worker, args=('first',),
                                         daemon=True)
                second = threading.Thread(target=worker, args=('second',),
                                          daemon=True)
                first.start()
                try:
                    assert entered.wait(timeout=5), 'pause not engaged'
                    started.clear()
                    second.start()
                    assert started.wait(timeout=5), 'second loader not started'
                    finished.wait(timeout=0.2)
                finally:
                    join([first] + ([second] if second.ident else []))
            assert not errors, errors
            assert len(results) == 2, results
            assert getattr(results[0], sibling) is getattr(results[1], sibling), \
                'overlapping loads published separate sibling modules'
            if sibling == 'impact_clone':
                assert results[0]._SCRATCH_DIRS is results[1]._SCRATCH_DIRS
                assert results[0]._CLONE_PROCESSES is results[1]._CLONE_PROCESSES
        ''', fallback=fallback)

    def test_normal_import_waits_for_pending_execution(self, fallback=False):
        self.run_scenario('''
            original = importlib.machinery.SourceFileLoader.exec_module
            imported = []
            def pause(loader, module):
                if module.__name__ == sibling:
                    entered.set()
                    assert release.wait(timeout=5), 'execution not released'
                return original(loader, module)
            def normal_import():
                started.set()
                try:
                    imported.append(importlib.import_module(sibling))
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    finished.set()

            with patch.object(importlib.machinery.SourceFileLoader,
                              'exec_module', pause):
                first = threading.Thread(target=worker, args=('pending',),
                                         daemon=True)
                second = threading.Thread(target=normal_import, daemon=True)
                first.start()
                try:
                    assert entered.wait(timeout=5), 'pause not engaged'
                    started.clear()
                    second.start()
                    assert started.wait(timeout=5), 'importer not started'
                    returned_early = finished.wait(timeout=0.2)
                finally:
                    join([first] + ([second] if second.ident else []))
            if fallback == 'True':
                assert not imported, 'fallback returned a pending module'
                assert len(errors) == 1 and isinstance(errors[0], ImportError)
                complete = importlib.import_module(sibling)
            else:
                assert not returned_early, 'normal import observed pending exec'
                assert not errors, errors
                assert len(imported) == 1
                complete = imported[0]
            assert hasattr(complete, public_name), 'partial sibling returned'
            assert complete is getattr(results[0], sibling)
        ''', fallback=fallback)

    def test_failed_execution_uncaches_attempt_and_preserves_exception(self, fallback=False):
        self.run_scenario('''
            original = importlib.machinery.SourceFileLoader.exec_module
            failure = OSError('injected transient source read failure')
            def fail(loader, module):
                if module.__name__ == sibling:
                    raise failure
                return original(loader, module)

            with patch.object(importlib.machinery.SourceFileLoader,
                              'exec_module', fail):
                try:
                    load('failed')
                except OSError as exc:
                    assert exc is failure, 'original exception replaced'
                else:
                    raise AssertionError('injected failure did not propagate')
            assert sibling not in sys.modules, 'failed sibling left cached'
            retry = load('retry')
            module = getattr(retry, sibling)
            assert hasattr(module, public_name), 'retry returned partial sibling'
            assert module is sys.modules[sibling]
        ''', fallback=fallback)

        self.run_scenario('''
            original = importlib.machinery.SourceFileLoader.exec_module
            replacement = object()
            failure = OSError('injected failure after registry replacement')
            def fail(loader, module):
                if module.__name__ == sibling:
                    sys.modules[sibling] = replacement
                    raise failure
                return original(loader, module)
            with patch.object(importlib.machinery.SourceFileLoader,
                              'exec_module', fail):
                try:
                    load('failed')
                except OSError as exc:
                    assert exc is failure
                else:
                    raise AssertionError('injected failure did not propagate')
            assert sys.modules[sibling] is replacement, 'foreign entry deleted'
        ''', fallback=fallback)

    def test_fallback_serializes_rolls_back_and_refuses_partial_import(self):
        self.test_overlapping_path_loads_share_modules_and_registries(True)
        self.test_normal_import_waits_for_pending_execution(True)
        self.test_failed_execution_uncaches_attempt_and_preserves_exception(True)


class TestCloneLaunchCoverage(unittest.TestCase):
    """Spawn gates and failure paths retain their lifecycle guarantees."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ghw-clone-coverage-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_shutdown_gate_rejects_and_unpublishes_a_launch(self):
        starting = set()
        command = ["git", "clone", "outside/project"]
        with mock.patch.object(impact_clone, "_CLONE_STARTING", starting), \
                mock.patch.object(impact_clone, "_CLONE_SHUTDOWN", True), \
                mock.patch.object(impact_loc.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(
                    RuntimeError, "clone interrupted by renderer shutdown"):
                impact_clone._run_clone_command_worker(command, timeout=1)

        self.assertEqual(starting, set())
        popen.assert_not_called()

    def test_shutdown_waits_for_a_published_launch_registration(self):
        class Registration:
            def __init__(self):
                self.waiting = threading.Event()
                self.release = threading.Event()

            def wait(self):
                self.waiting.set()
                self.release.wait()

        launch = mock.Mock(registered=Registration())
        worker = threading.Thread(target=impact_clone._wait_for_clone_launches)
        with mock.patch.object(impact_clone, "_CLONE_STARTING", {launch}):
            worker.start()
            try:
                self.assertTrue(launch.registered.waiting.wait(timeout=3))
                self.assertTrue(worker.is_alive())
            finally:
                launch.registered.release.set()
                worker.join(timeout=3)

        self.assertFalse(worker.is_alive())

    def test_main_thread_reraises_clone_spawn_failure(self):
        failure = RuntimeError("spawn failed")
        with mock.patch.object(
                impact_clone, "_run_clone_command_worker",
                side_effect=failure):
            with self.assertRaises(RuntimeError) as raised:
                impact_clone._run_clone_command(["git", "clone"], timeout=1)

        self.assertIs(raised.exception, failure)

    def test_worker_thread_calls_clone_worker_directly(self):
        command = ["git", "version"]
        completed = subprocess.CompletedProcess(command, 0)
        results = []

        def run_from_worker():
            results.append(impact_clone._run_clone_command(command, timeout=7))

        with mock.patch.object(
                impact_clone, "_run_clone_command_worker",
                return_value=completed) as clone_worker:
            worker = threading.Thread(target=run_from_worker)
            worker.start()
            worker.join(timeout=3)

        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [completed])
        clone_worker.assert_called_once_with(command, 7)

    @unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
    def test_timed_out_clone_escalates_and_reaps_ignoring_child(self):
        ready = self.tmp / "ignore-term-ready"
        script = (
            "import signal, sys, time\n"
            "from pathlib import Path\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "Path(sys.argv[1]).touch()\n"
            "time.sleep(30)\n")
        children = []
        real_popen = subprocess.Popen

        def start_child(*args, **kwargs):
            # The production clone worker owns the returned process context.
            child = real_popen(*args, **kwargs)  # pylint: disable=consider-using-with
            children.append(child)
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not ready.exists():
                child.kill()
                child.wait()
                raise RuntimeError("clone fixture did not install SIGTERM")
            return child

        processes = set()
        starting = set()
        with mock.patch.object(impact_clone, "_CLONE_PROCESSES", processes), \
                mock.patch.object(impact_clone, "_CLONE_STARTING", starting), \
                mock.patch.object(impact_clone, "_CLONE_SHUTDOWN", False), \
                mock.patch.object(impact_loc.subprocess, "Popen",
                                  new=start_child):
            with self.assertRaises(subprocess.TimeoutExpired):
                # Preserve the fractional timeout with the original int default.
                impact_clone._run_clone_command(
                    [sys.executable, "-c", script, str(ready)],
                    timeout=0.1)  # pyright: ignore[reportArgumentType]

        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
        self.assertEqual(processes, set())
        self.assertEqual(starting, set())

    def test_nested_signal_callback_defers_to_active_cleanup(self):
        with mock.patch.object(impact_clone, "_SIGNAL_CLEANUP_CLAIMS",
                               impact_clone.itertools.count(1)), \
                mock.patch.object(impact_clone, "_CLONE_SHUTDOWN", False), \
                mock.patch.object(impact_clone, "_stop_clone_processes") as stop, \
                mock.patch.object(impact_loc.os, "_exit") as exit_process:
            impact_clone._handle_scratch_signal(signal.SIGINT, None)
            self.assertFalse(impact_clone._CLONE_SHUTDOWN)

        stop.assert_not_called()
        exit_process.assert_not_called()


class TestScratchCoverage(unittest.TestCase):
    """Scratch registration and removal errors preserve observable state."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ghw-scratch-coverage-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_registration_rolls_back_when_owner_lock_cannot_be_created(self):
        scratch = self.tmp / "missing-parent" / "impact-fame-lock-failure"
        registry = set()
        locks = {}

        with mock.patch.object(impact_clone, "_SCRATCH_DIRS", registry), \
                mock.patch.object(impact_clone, "_SCRATCH_LOCKS", locks):
            with self.assertRaises(FileNotFoundError):
                impact_loc.register_scratch_dir(scratch)

        self.assertEqual(registry, set())
        self.assertEqual(locks, {})

    def test_prefetch_removes_scratch_if_owner_lock_registration_fails(self):
        created = []
        real_mkdtemp = tempfile.mkdtemp

        def make_scratch(prefix):
            path = Path(real_mkdtemp(prefix=prefix, dir=self.tmp))
            created.append(path)
            return str(path)

        clone = mock.Mock()
        moved = [("outside/project", {"branch": "main", "head": "h1"})]
        with mock.patch.object(impact_clone.tempfile, "mkdtemp",
                               side_effect=make_scratch), \
                mock.patch.object(
                    impact_clone, "register_scratch_dir",
                    side_effect=OSError("owner lock unavailable")):
            with self.assertRaisesRegex(OSError, "owner lock unavailable"):
                next(impact_loc.prefetched_clones(
                    moved, depth=1, clone_fn=clone))

        self.assertEqual(len(created), 1)
        self.assertFalse(created[0].exists())
        clone.assert_not_called()

    def test_blame_failure_keeps_an_existing_count(self):
        repo = "outside/project"
        previous = {repo: {"ours": 2, "total": 3,
                           "branch": "main", "head": "old"}}
        scratch = Path(tempfile.mkdtemp(prefix="impact-fame-", dir=self.tmp))
        output = io.StringIO()
        entry = (repo, {"branch": "main", "head": "new"}, scratch,
                 0.0, 0.0, RuntimeError("clone failed"))

        with mock.patch.object(impact_clone, "_SIGNAL_HANDLERS_INSTALLED", True), \
                mock.patch.object(impact_loc, "scavenge_scratch_dirs"), \
                redirect_stdout(output):
            impact_loc.blame_moved(
                [(repo, {"branch": "main", "head": "new"})], previous,
                set(), prefetch_fn=lambda _moved: iter((entry,)))

        self.assertEqual(previous[repo]["ours"], 2)
        self.assertEqual(previous[repo]["head"], "old")
        self.assertIn("FAIL (kept old count)", output.getvalue())
        self.assertFalse(scratch.exists())

    def test_readonly_cleanup_accepts_legacy_error_and_missing_path(self):
        path = self.tmp / "disappearing-readonly-entry"
        path.write_text("fixture", encoding="utf-8")
        permission_error = PermissionError("read-only")
        real_unlink = os.unlink

        def remove_before_retry(retry_path, *_args, **_kwargs):
            self.assertEqual(retry_path, path)
            real_unlink(retry_path)

        def retry_missing_path(retry_path):
            self.assertEqual(retry_path, path)
            self.assertFalse(retry_path.exists())
            return real_unlink(retry_path)

        with mock.patch.object(impact_loc.os, "chmod",
                               side_effect=remove_before_retry) as chmod, \
                mock.patch.object(impact_loc.os, "unlink",
                                  side_effect=retry_missing_path) as retry_unlink:
            impact_clone._retry_readonly_scratch_removal(
                retry_unlink, path,
                (PermissionError, permission_error, None))
            chmod.assert_called_once_with(
                path, stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
            retry_unlink.assert_called_once_with(path)

        self.assertFalse(path.exists())

    def test_readonly_cleanup_reraises_other_errors(self):
        path = self.tmp / "not-readonly"
        path.write_text("fixture", encoding="utf-8")
        failure = OSError("filesystem failure")

        with self.assertRaises(OSError) as raised:
            impact_clone._retry_readonly_scratch_removal(
                os.unlink, path, failure)

        self.assertIs(raised.exception, failure)
        self.assertTrue(path.exists())


class TestImpactPathCoverage(unittest.TestCase):
    """Malformed inputs and failed clones keep their explicit errors."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ghw-impact-path-coverage-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_load_repo_pins_rejects_a_non_string_branch(self):
        path = self.tmp / "pins.json"
        path.write_text(
            '{"outside/project":{"head":"abc123","branch":7}}',
            encoding="utf-8")

        with mock.patch.dict(os.environ, {"IMPACT_REPO_PINS": str(path)}):
            with self.assertRaisesRegex(SystemExit, "non-string branch"):
                impact_loc.load_repo_pins()

    def test_clone_repo_raises_when_git_clone_fails(self):
        dest = self.tmp / "missing-clone"
        result = subprocess.CompletedProcess(["git", "clone"], 1)
        with mock.patch.dict(os.environ,
                             {"CLONE_SOURCE_DIR": str(self.tmp / "mirrors")}), \
                mock.patch.object(impact_clone, "_run_clone_command",
                                  return_value=result) as run_clone:
            with self.assertRaisesRegex(RuntimeError, "clone_failed"):
                impact_loc.clone_repo("outside/project", "main", dest)

        self.assertFalse(dest.exists())
        self.assertEqual(run_clone.call_args.kwargs["timeout"], 300)

    def test_text_line_total_ignores_truncated_grep_records(self):
        for output in ("truncated-path", "path.py\0 2"):
            with mock.patch.object(impact_loc, "git_out",
                                   return_value=output):
                self.assertEqual(
                    impact_loc._text_line_total(self.tmp, {"path.py"}), 0)

    def test_posix_reader_returns_when_stdout_is_absent(self):
        process = mock.Mock(stdout=None)
        output = bytearray()
        impact_loc._read_bounded_stdout_posix(
            process, output, 10, time.monotonic(), ["git", "blame"], 1)
        self.assertEqual(output, bytearray())


class TestUpdateLocCoverage(unittest.TestCase):
    """A repository without a head preserves cache and uses the default pass."""

    def test_default_blame_skips_headless_repo_and_keeps_its_count(self):
        repo = "outside/removed"
        cached = {repo: {"ours": 2, "total": 5, "head": "old"}}
        totals = {repo: {"branch": "main", "head": None}}

        with mock.patch.dict(os.environ, {"IMPACT_REPO_PINS": ""}), \
                mock.patch.object(impact_loc, "blame_moved") as blame:
            result = impact_loc.update_loc({repo}, totals, cached, False,
                                           {"us@example.com"})

        self.assertEqual(result, cached)
        blame.assert_called_once_with([], cached, {"us@example.com"})


class TestBoundedReaderCoverage(unittest.TestCase):
    """A bounded capture deadline kills and reaps its actual subprocess."""

    def test_reader_timeout_reaps_child(self):
        children = []
        real_popen = subprocess.Popen

        def track_child(*args, **kwargs):
            child = real_popen(*args, **kwargs)  # pylint: disable=consider-using-with
            children.append(child)
            return child

        with mock.patch.object(impact_loc.subprocess, "Popen",
                               new=track_child):
            with self.assertRaises(subprocess.TimeoutExpired) as raised:
                impact_loc._run_bounded(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    timeout=0.2)

        self.assertEqual(raised.exception.timeout, 0.2)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())


class TestImpactMethodCoverage(unittest.TestCase):
    """Targeted and audit modes return and report their computed counts."""

    def test_targeted_mode_records_its_count_and_timing(self):
        repo = "outside/project"
        dest = Path("/fixture/repo")
        emails = {"us@example.com"}
        timings = []
        with mock.patch.object(impact_loc, "BLAME_METHOD", "targeted"), \
                mock.patch.object(impact_loc, "DEBUG_TIMING", True), \
                mock.patch.object(impact_loc, "_TIMINGS", timings), \
                mock.patch.object(impact_loc, "targeted_counts",
                                  return_value=(2, 5)) as count_files, \
                redirect_stdout(io.StringIO()):
            counts = impact_loc.counts_for(
                repo, dest, emails, clone_s=1.0, wait_s=0.25)

        self.assertEqual(counts, (2, 5))
        count_files.assert_called_once_with(dest, emails)
        self.assertEqual(timings[0][0:2], (repo, 1.0))
        self.assertEqual(timings[0][3:], (5, 0.25))

    def test_both_mode_records_and_reports_a_disagreement(self):
        repo = "outside/project"
        dest = Path("/fixture/repo")
        emails = {"us@example.com"}
        disagreements = []
        output = io.StringIO()
        with mock.patch.object(impact_loc, "BLAME_METHOD", "both"), \
                mock.patch.object(impact_loc, "_DISAGREEMENTS", disagreements), \
                mock.patch.object(impact_loc, "blame_repo",
                                  return_value=(3, 5)), \
                mock.patch.object(impact_loc, "targeted_counts",
                                  return_value=(2, 5)) as count_files, \
                redirect_stdout(output):
            counts = impact_loc.counts_for(repo, dest, emails)

        self.assertEqual(counts, (3, 5))
        count_files.assert_called_once_with(dest, emails)
        self.assertEqual(disagreements, [(repo, 3, 5, 2, 5)])
        self.assertIn("MISMATCH", output.getvalue())


class TestTimingCoverage(unittest.TestCase):
    """The opt-in timing path reports phases; the default stays silent."""

    def test_enabled_timing_records_and_summarizes_phases(self):
        phases = {}
        timings = []
        output = io.StringIO()
        started = time.monotonic()

        with mock.patch.object(impact_loc, "DEBUG_TIMING", True), \
                mock.patch.object(impact_loc, "_PHASES", phases), \
                mock.patch.object(impact_loc, "_TIMINGS", timings), \
                mock.patch.object(impact_loc, "_T0", started), \
                redirect_stdout(output):
            with impact_loc.timed_phase("fixture-load"):
                pass
            impact_loc._record_timing(
                "outside/repo", 1.0, 2.0, 20, wait_s=0.5)
            impact_loc.print_timing_summary()

        self.assertIn("fixture-load", phases)
        self.assertEqual(len(timings), 1)
        self.assertEqual(timings[0], ("outside/repo", 1.0, 2.0, 20, 0.5))
        self.assertIn("timing outside/repo", output.getvalue())
        self.assertIn("fixture-load", output.getvalue())
        self.assertIn("phases sum", output.getvalue())
        self.assertIn("unattributed)", output.getvalue())

    def test_disabled_timing_is_inert(self):
        phases = {}
        timings = []
        output = io.StringIO()

        with mock.patch.object(impact_loc, "DEBUG_TIMING", False), \
                mock.patch.object(impact_loc, "_PHASES", phases), \
                mock.patch.object(impact_loc, "_TIMINGS", timings), \
                redirect_stdout(output):
            with impact_loc.timed_phase("ignored"):
                pass
            impact_loc._record_timing("ignored/repo", 1, 2, 3)
            impact_loc.print_timing_summary()

        self.assertEqual(phases, {})
        self.assertEqual(timings, [])
        self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
