#!/usr/bin/env python3
"""Tests for ghwidgets_common.py cache-writing helpers. Stdlib only, like the module.

    python3 -m unittest discover -v

No network; no filesystem outside a TemporaryDirectory.
"""
import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "ghwidgets_common", Path(__file__).with_name("ghwidgets_common.py"))
if spec is None or spec.loader is None:
    raise SystemExit("error: cannot load ghwidgets_common.py")
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)


class CacheWriting(unittest.TestCase):
    """Two scripts write the impact cache; the lock is what keeps the cheap
    hourly writer from reverting the expensive twice-daily one."""

    def setUp(self):
        # pylint: disable=consider-using-with
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.path = Path(self.td.name) / "impact-cache.json"

    def write(self, payload):
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    def read(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_save_cache_returns_none_on_success(self):
        self.assertIsNone(common.save_cache(self.path, {}))
        self.assertEqual(self.read(), {})

    @unittest.skipIf(sys.platform == "win32", "requires POSIX file modes")
    def test_lock_and_cache_files_and_directories_are_private(self):
        lock_path = Path(self.td.name) / "lock-dir" / "cache.json"
        lock = Path(str(lock_path) + ".lock")
        cache_path = Path(self.td.name) / "cache-dir" / "cache.json"
        original_umask = os.umask(0o022)
        try:
            with common.cache_lock(lock_path) as held:
                self.assertTrue(held)
            self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)
            self.assertEqual(
                stat.S_IMODE(lock_path.parent.stat().st_mode), 0o700)
            common.save_cache(self.path, {"version": 1})
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
            self.path.chmod(0o644)
            common.save_cache(self.path, {"version": 1, "new": True})
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
            common._write_cache(  # pylint: disable=protected-access
                cache_path, {"version": 1})
            self.assertEqual(
                stat.S_IMODE(cache_path.parent.stat().st_mode), 0o700)
        finally:
            os.umask(original_umask)

    @unittest.skipIf(sys.platform == "win32", "requires POSIX file modes")
    def test_save_cache_does_not_publish_a_world_readable_stale_temp(self):
        stale_temp = Path(str(self.path) + ".tmp")
        stale_temp.write_text("stale cache", encoding="utf-8")
        stale_temp.chmod(0o644)
        original_umask = os.umask(0o022)
        try:
            common.save_cache(self.path, {"version": 1})
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
            self.assertFalse(stale_temp.exists())
        finally:
            os.umask(original_umask)

    def test_write_cache_closes_descriptor_when_fdopen_fails(self):
        path = Path(self.td.name) / "fdopen-dir" / "cache.json"
        opened_descriptors = []

        def fail_fdopen(fd, *args, **kwargs):
            opened_descriptors.append(fd)
            raise OSError("fdopen failed")

        original_umask = os.umask(0o022)
        try:
            with mock.patch.object(common.os, "fdopen",
                                   side_effect=fail_fdopen):
                with self.assertRaises(OSError):
                    common._write_cache(  # pylint: disable=protected-access
                        path, {"version": 1})
            self.assertEqual(len(opened_descriptors), 1)
            with self.assertRaises(OSError):
                os.fstat(opened_descriptors[0])
            self.assertEqual(list(path.parent.glob("*.tmp")), [])
        finally:
            os.umask(original_umask)

    def test_merge_replaces_only_the_listed_keys(self):
        self.write({"version": 1, "prs": {"old": 1},
                    "ourloc": {"a/b": {"ours": 7}}})
        common.merge_cache(self.path, 1, {"prs": {"new": 2}})
        after = self.read()
        self.assertEqual(after["prs"], {"new": 2})
        self.assertEqual(after["ourloc"], {"a/b": {"ours": 7}})

    def test_merge_stamps_the_schema_version(self):
        common.merge_cache(self.path, 1, {"prs": {}})
        self.assertEqual(self.read()["version"], 1)

    def test_merge_of_a_version_mismatch_keeps_nothing(self):
        # load_cache already refuses a foreign schema; carrying its keys into
        # the new payload would mix two layouts in one file.
        self.write({"version": 99, "ourloc": {"a/b": {"ours": 7}}})
        common.merge_cache(self.path, 1, {"prs": {}})
        self.assertNotIn("ourloc", self.read())

    def test_a_held_lock_stops_a_merge_rather_than_racing_it(self):
        # The read-modify-write writer must never proceed unlocked: a
        # whole-file save landing between its read and its write would be
        # silently reverted, ourloc included.
        self.write({"version": 1, "ourloc": {"a/b": {"ours": 7}}})
        before = self.path.read_bytes()
        with common.cache_lock(self.path) as held:
            self.assertTrue(held)
            with self.assertRaises(TimeoutError):
                common.merge_cache(self.path, 1, {"prs": {}}, timeout=0.1)
        self.assertEqual(self.path.read_bytes(), before)

    def test_a_held_lock_stops_a_whole_file_save(self):
        # A whole-file writer must leave old cache data alone.
        self.write({"version": 1, "prs": {"old": 1}})
        before = self.path.read_bytes()
        with common.cache_lock(self.path) as held:
            self.assertTrue(held)
            with self.assertRaises(TimeoutError):
                common.save_cache(self.path, {"version": 1, "prs": {}},
                                  timeout=0.1)
        self.assertEqual(self.path.read_bytes(), before)

    def test_the_lock_is_released_when_the_block_ends(self):
        with common.cache_lock(self.path) as held:
            self.assertTrue(held)
        with common.cache_lock(self.path, timeout=0.1) as held:
            self.assertTrue(held, "lock must not survive its context manager")

    def test_a_failed_write_leaves_neither_a_partial_cache_nor_a_temp_file(self):
        self.write({"version": 1, "prs": {"old": 1}})
        before = self.path.read_bytes()
        with mock.patch.object(common.os, "replace",
                               side_effect=OSError("no space left")):
            with self.assertRaises(OSError):
                common.save_cache(
                    self.path, {"version": 1, "prs": {"new": 2}})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(
            [p.name for p in Path(self.td.name).glob("*.tmp")], [])

    def test_a_failed_merge_raises_and_leaves_the_old_cache(self):
        self.write({"version": 1, "prs": {"old": 1}})
        before = self.path.read_bytes()
        with mock.patch.object(common.os, "replace",
                               side_effect=OSError("no space left")):
            with self.assertRaises(OSError):
                common.merge_cache(self.path, 1, {"prs": {"new": 2}})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(
            [p.name for p in Path(self.td.name).glob("*.tmp")], [])

    def test_neither_locking_api_degrades_to_unlocked(self):
        # With neither API, cache_lock yields False and writers fail rather
        # than bypassing the lock.
        with mock.patch.object(common, "fcntl", None), \
                mock.patch.object(common, "msvcrt", None):
            with common.cache_lock(self.path, timeout=0.1) as held:
                self.assertFalse(held)
        with common.cache_lock(self.path, timeout=0.1) as held:
            self.assertTrue(held)

    def test_the_windows_api_branch_dispatches_to_msvcrt(self):
        # The dispatch, not the OS: a stub stands in for msvcrt so this
        # runs on the POSIX cells too, where a real msvcrt cannot exist.
        calls = []

        class FakeMsvcrt:
            LK_NBLCK = 2

            @staticmethod
            def locking(fd, mode, nbytes):
                calls.append((fd, mode, nbytes))
                return True

        with mock.patch.object(common, "fcntl", None), \
                mock.patch.object(common, "msvcrt", FakeMsvcrt):
            with common.cache_lock(self.path, timeout=0.1) as held:
                self.assertTrue(held)
        self.assertEqual([(mode, size) for _, mode, size in calls],
                         [(FakeMsvcrt.LK_NBLCK, 1)])
