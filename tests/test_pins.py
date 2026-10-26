#!/usr/bin/env python3
"""Tests for scripts/resolve-repo-pins.py.

    python3 -m unittest discover -v

The script resolves, once and up front, the commit every repo in an audit
will be blamed at. Its whole value is that both counting methods and both
arms of a comparison then see identical trees, so the parts tested here are
the ones that decide WHICH commit a repo gets — the network fetch around
them is the renderer's own, already covered by test_impact.py.
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock
from tests import _ghw_temp


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parents[1] / relative)
    if spec is None or spec.loader is None:
        raise SystemExit(f"error: cannot load {relative}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


resolve_pins = _load("resolve_pins", "scripts/resolve-repo-pins.py")
impact_loc = _load("impact_loc_under_test", "src/impact_loc.py")
impact_loc.configure(_load("ghwidgets_common_under_test",
                           "src/ghwidgets_common.py"))


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
        self.tmp = Path(_ghw_temp.mkdtemp(prefix="ghw-pinout-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "pins.json"

    def test_written_manifest_loads_through_the_renderer(self):
        pins = {"o/a": {"branch": "main", "head": "aaa"}}
        resolve_pins.write_manifest(self.path, pins)
        with mock.patch.dict(
                os.environ,
                {"GH_WIDGETS_IMPACT_REPO_PINS": str(self.path)}):
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


class TestEnvFallback(unittest.TestCase):
    # parse_args runs before load_renderer, so the fallback cannot come from
    # the helper and is carried inline; these pin the inline expression.

    def parse(self, env, *argv):
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(sys, 'argv',
                                  ['resolve-repo-pins.py', *argv]):
            return resolve_pins.parse_args()

    def test_the_bare_names_are_read_when_the_prefixed_names_are_unset(self):
        args, _token = self.parse({'GH_USER': 'bare-user',
                                   'GH_TOKEN_FILE': '/bare/t'},
                                  '--token', 't')
        self.assertEqual(args.user, 'bare-user')
        self.assertEqual(args.token_file, '/bare/t')

    def test_the_prefixed_names_win_when_both_spellings_are_set(self):
        args, _token = self.parse({'GH_USER': 'bare-user',
                                   'GH_WIDGETS_GH_USER': 'prefixed-user',
                                   'GH_TOKEN_FILE': '/bare/t',
                                   'GH_WIDGETS_GH_TOKEN_FILE': '/prefixed/t'},
                                  '--token', 't')
        self.assertEqual(args.user, 'prefixed-user')
        self.assertEqual(args.token_file, '/prefixed/t')

    def test_an_empty_prefixed_name_wins_over_a_set_bare_name(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                self.parse({'GH_USER': 'bare-user',
                            'GH_WIDGETS_GH_USER': ''},
                           '--token', 't')
        self.assertIn('--user', stderr.getvalue())

    def test_the_bare_token_file_is_read_for_the_token(self):
        token_path = Path(_ghw_temp.mkdtemp(prefix='ghw-pinenv-')) / 'token'
        token_path.write_text('from-bare-file\n', encoding='utf-8')
        self.addCleanup(shutil.rmtree, token_path.parent, ignore_errors=True)
        _args, token = self.parse({'GH_USER': 'u',
                                   'GH_TOKEN_FILE': str(token_path)})
        self.assertEqual(token, 'from-bare-file')

    def test_the_prefixed_token_file_wins_over_a_missing_bare_file(self):
        token_path = Path(_ghw_temp.mkdtemp(prefix='ghw-pinenv-')) / 'token'
        token_path.write_text('from-prefixed-file\n', encoding='utf-8')
        self.addCleanup(shutil.rmtree, token_path.parent, ignore_errors=True)
        _args, token = self.parse({'GH_USER': 'u',
                                   'GH_TOKEN_FILE': str(token_path.parent /
                                                        'missing'),
                                   'GH_WIDGETS_GH_TOKEN_FILE':
                                       str(token_path)})
        self.assertEqual(token, 'from-prefixed-file')


if __name__ == "__main__":
    unittest.main()
