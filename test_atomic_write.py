#!/usr/bin/env python3
"""Tests for one ghwidgets_common.py helper. Stdlib only, like the module.

    python3 -m unittest discover -v

No network; no filesystem outside a TemporaryDirectory.
"""
import importlib.util
import os
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


class AtomicTextWriting(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "public.svg"

    def test_writes_complete_utf8_text(self):
        content = "complete café\nsecond line"

        common.atomic_write_text(self.path, content)

        self.assertEqual(self.path.read_text(encoding="utf-8"), content)

    @unittest.skipUnless(os.name == "posix", "POSIX permission modes only")
    def test_existing_0600_target_is_healed_to_0644(self):
        self.path.write_text("old", encoding="utf-8")
        os.chmod(self.path, 0o600)

        common.atomic_write_text(self.path, "new")

        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o644)

    def test_new_target_writes_content_under_restrictive_umask(self):
        previous_umask = os.umask(0o077)
        try:
            common.atomic_write_text(self.path, "new content")
            self.assertEqual(self.path.read_text(encoding="utf-8"),
                             "new content")
        finally:
            os.umask(previous_umask)

    @unittest.skipUnless(os.name == "posix", "POSIX permission modes only")
    def test_new_target_mode_is_0644_under_restrictive_umask(self):
        previous_umask = os.umask(0o077)
        try:
            common.atomic_write_text(self.path, "new")
        finally:
            os.umask(previous_umask)

        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o644)

    def test_replace_failure_keeps_old_file_and_removes_temp(self):
        self.path.write_text("old", encoding="utf-8")

        with mock.patch.object(common.os, "replace",
                               side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                common.atomic_write_text(self.path, "new")

        self.assertEqual(self.path.read_text(encoding="utf-8"), "old")
        self.assertEqual(list(self.path.parent.glob("public.svg.*.tmp")), [])

    @unittest.skipUnless(
        sys.platform == "win32", "Windows read-only semantics")
    def test_readonly_failure_cleans_temp_without_masking_error(self):
        self.path.write_text("old", encoding="utf-8")
        os.chmod(self.path, 0o444)
        try:
            with mock.patch.object(
                    common.os, "replace",
                    side_effect=PermissionError("original replacement error")):
                with self.assertRaisesRegex(
                        PermissionError, "original replacement error"):
                    common.atomic_write_text(self.path, "new")
        finally:
            os.chmod(self.path, 0o666)

        self.assertEqual(self.path.read_text(encoding="utf-8"), "old")
        self.assertEqual(list(self.path.parent.glob("public.svg.*.tmp")), [])
