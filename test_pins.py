#!/usr/bin/env python3
"""Tests for scripts/resolve-repo-pins.py.

    python3 -m unittest discover -v

The script resolves, once and up front, the commit every repo in an audit
will be blamed at. Its whole value is that both counting methods and both
arms of a comparison then see identical trees, so the parts tested here are
the ones that decide WHICH commit a repo gets — the network fetch around
them is the renderer's own, already covered by test_impact.py.
"""
import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).parent / relative)
    if spec is None or spec.loader is None:
        raise SystemExit(f"error: cannot load {relative}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


resolve_pins = _load("resolve_pins", "scripts/resolve-repo-pins.py")
impact_loc = _load("impact_loc_under_test", "impact_loc.py")


class TestPinManifest(unittest.TestCase):
    """Which repos get a pin, and which commit each one gets."""

    def test_every_blamed_repo_is_pinned_at_its_head(self):
        totals = {"o/a": {"branch": "main", "head": "aaa"},
                  "o/b": {"branch": "trunk", "head": "bbb"}}
        self.assertEqual(
            resolve_pins.pin_manifest({"o/a", "o/b"}, totals),
            {"o/a": {"branch": "main", "head": "aaa"},
             "o/b": {"branch": "trunk", "head": "bbb"}})

    def test_a_repo_the_run_will_not_blame_is_not_pinned(self):
        totals = {"o/a": {"branch": "main", "head": "aaa"},
                  "o/idle": {"branch": "main", "head": "iii"}}
        self.assertEqual(list(resolve_pins.pin_manifest({"o/a"}, totals)),
                         ["o/a"])

    def test_a_headless_repo_is_omitted_as_update_loc_skips_it(self):
        totals = {"o/gone": {"branch": "", "head": ""}}
        self.assertEqual(resolve_pins.pin_manifest({"o/gone"}, totals), {})

    def test_a_repo_missing_from_totals_is_omitted(self):
        self.assertEqual(resolve_pins.pin_manifest({"o/gone"}, {}), {})

    def test_repos_are_ordered_so_manifests_diff_cleanly(self):
        totals = {r: {"branch": "main", "head": r} for r in ("o/c", "o/a", "o/b")}
        self.assertEqual(list(resolve_pins.pin_manifest(set(totals), totals)),
                         ["o/a", "o/b", "o/c"])


class TestWriteManifest(unittest.TestCase):
    """The file the renderer reads back."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ghw-pinout-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "pins.json"

    def test_written_manifest_loads_through_the_renderer(self):
        pins = {"o/a": {"branch": "main", "head": "aaa"}}
        resolve_pins.write_manifest(self.path, pins)
        with mock.patch.dict(os.environ, {"IMPACT_REPO_PINS": str(self.path)}):
            self.assertEqual(impact_loc.load_repo_pins(), pins)

    def test_an_empty_manifest_is_refused(self):
        """An empty pin file would un-pin the whole run without saying so."""
        with self.assertRaises(SystemExit):
            resolve_pins.write_manifest(self.path, {})

    def test_the_file_is_json_an_operator_can_read(self):
        resolve_pins.write_manifest(
            self.path, {"o/a": {"branch": "main", "head": "aaa"}})
        self.assertEqual(json.loads(self.path.read_text())["o/a"]["head"],
                         "aaa")


if __name__ == "__main__":
    unittest.main()
