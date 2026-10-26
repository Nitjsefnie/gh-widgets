#!/usr/bin/env python3
"""Sentinel: every ``ghw-*`` test directory carries a live owner lock.

The helper locks each ghw-* directory a test creates, and the tests
package import scavenges the dead ones at the start of every run. The
unit tests drive the three verdicts (dead reclaims, live survives,
foreign survives) against the real lock primitives; the guard fails the
suite if any tests/*.py creates a temp directory behind the helper; the
end-to-end test runs the real sequence a killed run leaves behind.

    python3 -m unittest tests.test_ghw_temp
"""
# pylint: disable=protected-access
import ast
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests import _ghw_temp

REPO_ROOT = Path(__file__).resolve().parents[1]


def _dead_ghw_dir(prefix):
    path = Path(tempfile.mkdtemp(prefix=prefix))
    lock = _ghw_temp.impact_clone
    stream = lock._acquire_scratch_owner_lock(path, create=True)
    lock._release_scratch_owner_lock(stream)
    return path


def _held_ghw_dir(prefix):
    path = Path(tempfile.mkdtemp(prefix=prefix))
    lock = _ghw_temp.impact_clone
    return path, lock._acquire_scratch_owner_lock(path, create=True)


def _purge(path, stream=None):
    if stream is not None:
        _ghw_temp.impact_clone._release_scratch_owner_lock(
            stream)
    try:
        _ghw_temp.impact_clone.remove_scratch_dir(path)
    except OSError:
        pass


class TestScavengerVerdicts(unittest.TestCase):
    """Each ghw-* directory gets exactly one of the three verdicts."""

    def test_dead_owner_directory_is_reclaimed(self):
        # Catches the dead-reclaim mutant (an acquired lock treated as a
        # skip): only this test's dead directory stops being removed.
        path = _dead_ghw_dir(prefix="ghw-unit-dead-")
        try:
            _ghw_temp.scavenge()
            self.assertFalse(path.exists())
        finally:
            _purge(path)

    def test_live_owner_directory_survives(self):
        # Catches the held-skip mutant (a BlockingIOError treated as a
        # reclaim): only this test's held directory is removed by it.
        path, stream = _held_ghw_dir(prefix="ghw-unit-live-")
        try:
            _ghw_temp.scavenge()
            self.assertTrue(path.exists())
        finally:
            _purge(path, stream)

    def test_foreign_directory_without_lock_file_survives(self):
        # Catches the no-lock-file-skip mutant (None treated as acquired):
        # only this test's foreign directory is removed by it.
        path = Path(tempfile.mkdtemp(prefix="ghw-unit-foreign-"))
        try:
            _ghw_temp.scavenge()
            self.assertTrue(path.exists())
        finally:
            _purge(path)

    def test_symlink_and_file_entries_are_skipped(self):
        target = Path(tempfile.mkdtemp(prefix="ghw-unit-target-"))
        link = Path(tempfile.gettempdir()) / (
            f"ghw-unit-link-{time.monotonic_ns()}")
        link.symlink_to(target)
        plain_file = Path(tempfile.gettempdir()) / (
            f"ghw-unit-file-{time.monotonic_ns()}")
        plain_file.write_text("", encoding="utf-8")
        try:
            _ghw_temp.scavenge()
            self.assertTrue(link.is_symlink())
            self.assertTrue(target.is_dir())
            self.assertTrue(plain_file.exists())
        finally:
            link.unlink()
            plain_file.unlink()
            _purge(target)

    def test_scavenge_spares_this_processs_registry(self):
        held = _ghw_temp.tempdir(prefix="ghw-unit-registry-")
        made = _ghw_temp.mkdtemp(prefix="ghw-unit-registry-")
        try:
            self.assertIn(Path(held.name), _ghw_temp._OWNED)
            _ghw_temp.scavenge()
            self.assertTrue(Path(held.name).exists())
            self.assertTrue(made.exists())
        finally:
            _purge(made)
            held.cleanup()
            _ghw_temp._release_owned()

    def test_helper_tempdir_lock_is_dropped_at_cleanup(self):
        with _ghw_temp.tempdir(prefix="ghw-unit-cleanup-") as tmp:
            path = Path(tmp)
            self.assertTrue(path.is_dir())
            self.assertTrue(
                _ghw_temp.impact_clone._scratch_owner_lock_path(
                    path).exists())
        self.assertFalse(path.exists())
        self.assertFalse(
            _ghw_temp.impact_clone._scratch_owner_lock_path(
                path).exists())
        self.assertNotIn(path, _ghw_temp._OWNED)

    def test_helper_mkdtemp_registers_and_atexit_unlinks(self):
        made = _ghw_temp.mkdtemp(prefix="ghw-unit-mkdtemp-")
        lock_path = _ghw_temp.impact_clone._scratch_owner_lock_path(
            made)
        try:
            self.assertTrue(lock_path.exists())
            self.assertIn(made, _ghw_temp._OWNED)
        finally:
            _ghw_temp._release_owned()
        self.assertFalse(lock_path.exists())
        self.assertNotIn(made, _ghw_temp._OWNED)
        # Released, not merely forgotten: a fresh acquire must succeed.
        stream = _ghw_temp.impact_clone._acquire_scratch_owner_lock(
            made, create=True)
        _ghw_temp.impact_clone._release_scratch_owner_lock(
            stream)
        _purge(made)

    def test_non_ghw_names_pass_through_unlocked(self):
        with _ghw_temp.tempdir(prefix="diff-coverage-") as tmp:
            self.assertNotIn(Path(tmp), _ghw_temp._OWNED)
            self.assertFalse(Path(str(tmp) + ".owner.lock").exists())
        with _ghw_temp.tempdir() as tmp:
            self.assertNotIn(Path(tmp), _ghw_temp._OWNED)

    def test_an_abandoned_directory_is_foreign_to_the_scavenge(self):
        made = _ghw_temp.mkdtemp(prefix="ghw-unit-abandon-")
        _ghw_temp.abandon(made)
        self.assertNotIn(made, _ghw_temp._OWNED)
        self.assertFalse(
            _ghw_temp.impact_clone._scratch_owner_lock_path(made).exists())
        _ghw_temp.scavenge()
        self.assertTrue(
            made.exists(),
            "an abandoned dir carries no lock file, so the scavenge must "
            "leave it for production's own removal")
        _purge(made)

    def test_abandon_of_a_path_this_process_does_not_hold_raises(self):
        path = Path(tempfile.mkdtemp(prefix="ghw-unit-abandon-foreign-"))
        try:
            with self.assertRaisesRegex(ValueError, "abandon"):
                _ghw_temp.abandon(path)
        finally:
            _purge(path)


class TestAtexitExitPath(unittest.TestCase):
    def test_a_run_exiting_normally_leaves_no_owner_lock(self):
        markers = Path(tempfile.mkdtemp(  # pylint: disable=consider-using-with
            prefix="ghw-atexit-hang-"))
        marker = markers / "dir.txt"
        proc = subprocess.Popen(  # pylint: disable=consider-using-with
            [sys.executable, "-c",
             "import os, pathlib\n"
             "from tests import _ghw_temp\n"
             "tmp = _ghw_temp.mkdtemp(prefix='ghw-atexit-probe-')\n"
             "pathlib.Path(os.environ['GH_WIDGETS_ATPROBE_DIR_FILE'])"
             ".write_text(str(tmp))"],
            cwd=REPO_ROOT,
            env={**os.environ, "GH_WIDGETS_ATPROBE_DIR_FILE": str(marker)})
        self.addCleanup(proc.wait)
        self.addCleanup(
            lambda: _ghw_temp.impact_clone.remove_scratch_dir(markers))
        deadline = time.monotonic() + 60
        while not marker.exists():
            if proc.poll() is not None:
                self.fail("the probe exited before naming its dir")
            if time.monotonic() > deadline:
                self.fail("the probe never named its directory")
            time.sleep(0.02)
        self.assertEqual(proc.wait(), 0)
        made = Path(marker.read_text(encoding="utf-8"))
        self.addCleanup(
            lambda: _ghw_temp.impact_clone.remove_scratch_dir(made))
        self.assertFalse(
            _ghw_temp.impact_clone._scratch_owner_lock_path(made).exists(),
            "a run exiting normally must not leak its owner lock file")


_TEMPFILE = "temp" + "file"
_BANNED_ATTRS = ("Temporary" + "Directory", "mkd" + "temp")
_IMPACT_FAME_PREFIX = "impact-" + "fame-"


def _dotted_tempfile_name(node, module_names, bound):
    """The banned tempfile dotted name a node resolves to, or None."""
    if isinstance(node, ast.Attribute):
        if isinstance(node.value, ast.Name) \
                and node.value.id in module_names \
                and node.attr in _BANNED_ATTRS:
            return f"{_TEMPFILE}.{node.attr}"
    elif isinstance(node, ast.Name):
        return bound.get(node.id)
    return None


def _has_impact_fame_prefix(call):
    """A mkdtemp call testing impact_clone's own mechanism, lock and all."""
    for keyword in call.keywords:
        if keyword.arg == "prefix":
            value = keyword.value
            return isinstance(value, ast.Constant) \
                and isinstance(value.value, str) \
                and value.value.startswith(_IMPACT_FAME_PREFIX)
    return False


class TestGuardSuiteOwnsTempDirCreation(unittest.TestCase):
    def test_no_test_module_creates_temp_dirs_behind_the_helper(self):
        violations = []
        helper = Path(_ghw_temp.__file__).resolve()
        own = Path(__file__).resolve()
        for path in sorted((REPO_ROOT / "tests").glob("*.py")):
            if path.resolve() in (helper, own):
                continue
            source = path.read_text(encoding="utf-8")
            # Any import or reference of the module spells it out, so a
            # file that never says the name cannot carry a banned spelling.
            if _TEMPFILE not in source:
                continue
            tree = ast.parse(source, filename=str(path))
            module_names = {_TEMPFILE}
            bound = {}
            attrs = []
            names = []
            calls = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == _TEMPFILE and alias.asname:
                            module_names.add(alias.asname)
                elif isinstance(node, ast.ImportFrom) \
                        and node.module == _TEMPFILE and node.level == 0:
                    for alias in node.names:
                        if alias.name in _BANNED_ATTRS:
                            bound[alias.asname or alias.name] = \
                                f"{_TEMPFILE}.{alias.name}"
                elif isinstance(node, ast.Call) \
                        and isinstance(node.func, ast.Attribute) \
                        and node.func.attr in _BANNED_ATTRS:
                    calls.append(node)
                elif isinstance(node, ast.Attribute) \
                        and isinstance(node.value, ast.Name) \
                        and node.attr in _BANNED_ATTRS:
                    attrs.append(node)
                elif isinstance(node, ast.Name):
                    names.append(node)
            exempt = {call.func for call in calls
                      if _dotted_tempfile_name(
                          call.func, module_names, bound) is not None
                      and _has_impact_fame_prefix(call)}
            for node in attrs:
                dotted = _dotted_tempfile_name(node, module_names, bound)
                if dotted is not None and node not in exempt:
                    violations.append(f"{path.name}: {dotted}")
            for node in names:
                dotted = bound.get(node.id)
                if dotted is not None:
                    violations.append(f"{path.name}: {dotted}")
        self.assertEqual(
            violations, [],
            "convert these sites to tests/_ghw_temp.py; an impact-fame- "
            "literal prefix, and the _ghw_temp.tempfile hand-through in "
            "test_impact_loc_coverage.py, name the guarded exceptions")


@unittest.skipUnless(os.name == "posix",
                     "the end-to-end kill needs POSIX signals")
class TestScavengeReclaimsAKilledRun(unittest.TestCase):
    def test_killed_run_is_reclaimed_and_live_run_survives(self):
        markers = Path(tempfile.mkdtemp(prefix="ghw-e2e-hang-"))
        marker_a = markers / "dir-a.txt"
        marker_b = markers / "dir-b.txt"

        def spawn(marker):
            return subprocess.Popen(
                [sys.executable, "-m", "unittest", "tests._ghw_hang_probe"],
                cwd=REPO_ROOT, env={
                    **os.environ,
                    "GH_WIDGETS_HANG_PROBE_DIR_FILE": str(marker)})

        def wait_for_dir(proc, marker):
            deadline = time.monotonic() + 60
            while not marker.exists():
                if proc.poll() is not None:
                    self.fail("the hang probe exited before naming its dir")
                if time.monotonic() > deadline:
                    self.fail("the hang probe never named its directory")
                time.sleep(0.02)
            return Path(marker.read_text(encoding="utf-8"))

        proc_a = proc_b = None
        try:
            proc_a = spawn(marker_a)
            self.addCleanup(proc_a.wait)
            dir_a = wait_for_dir(proc_a, marker_a)
            proc_b = spawn(marker_b)
            self.addCleanup(proc_b.wait)
            dir_b = wait_for_dir(proc_b, marker_b)
            self.assertTrue(dir_a.is_dir())
            self.assertTrue(dir_b.is_dir())

            os.kill(proc_a.pid, signal.SIGKILL)
            proc_a.wait()
            self.assertTrue(dir_a.exists(),
                            "SIGKILL must leave the dead run's dir behind")

            scavenge_run = subprocess.run(
                [sys.executable, "-m", "unittest", "tests.test_owner_locks"],
                cwd=REPO_ROOT, timeout=120, check=False)
            self.assertEqual(scavenge_run.returncode, 0,
                             "the reclaiming run must itself pass its suite")
            self.assertFalse(
                dir_a.exists(),
                "the fresh run's start-of-run scavenge must reclaim the "
                "killed run's directory")
            self.assertTrue(
                dir_b.exists(),
                "the live run's directory must survive the fresh run's "
                "scavenge")

            os.kill(proc_b.pid, signal.SIGKILL)
            proc_b.wait()
            self.assertTrue(dir_b.exists())
            _ghw_temp.scavenge()
            self.assertFalse(dir_b.exists())
            self.assertTrue(
                markers.exists(),
                "a foreign ghw-* dir (raw mkdtemp, no lock file) must "
                "survive the scavenge")
        finally:
            for proc in (proc_a, proc_b):
                if proc is not None and proc.poll() is None:
                    os.kill(proc.pid, signal.SIGKILL)
            _ghw_temp.impact_clone.remove_scratch_dir(markers)
            _ghw_temp.scavenge()
