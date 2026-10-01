#!/usr/bin/env python3
"""Tests for render-impact.py's durability fallback.

    python3 -m unittest discover -v

Separate from test_impact.py because that module is about the blame pass and
its external git-fame dependency; this is about what a run prints when the
network fails and the cache carries the run. No network, no git: `fetch_all`
is replaced outright, so the fallback path is exercised without either.
"""
import importlib.util
import io
import json
import shutil
import sys
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


class TestDurabilityFallback(unittest.TestCase):
    """A failed acquisition renders from the cache and says what failed.

    This renderer makes ONE acquisition (`fetch_all`), so there is no phase to
    confuse it with — but the line is the operator's only account of what
    broke, so it must still name the call and the error (issue #55).
    """

    BOOM = RuntimeError("GraphQL errors: [{'type': 'SERVICE_UNAVAILABLE'}]")

    def snapshot(self):
        return {"version": render_impact.CACHE_VERSION,
                "fetched_at": "2026-07-20T06:00:00+00:00",
                "insiders": ["me"],
                "prs": {}, "issues": [], "totals": {}, "ourloc": {}}

    def fallback_run(self, error):
        """main() with fetch_all failing against a complete cache. Returns
        (the fallback line, the output directory, the cache file)."""
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td)
        cache_file = Path(td) / "impact-cache.json"
        cache_file.write_text(json.dumps(self.snapshot()))
        out = Path(td) / "out"
        argv = ["render-impact.py", "--user", "me", "--token", "t",
                "--out-dir", str(out), "--cache-file", str(cache_file)]
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(render_impact, "fetch_all",
                                  side_effect=error), \
                redirect_stdout(buf):
            render_impact.main()
        lines = [line for line in buf.getvalue().splitlines()
                 if line.startswith("fetch failed")]
        self.assertEqual(len(lines), 1)
        return lines[0], out, cache_file

    def test_the_line_names_the_acquisition_and_the_error(self):
        line, out, _ = self.fallback_run(self.BOOM)
        self.assertIn("fetch_all", line)
        self.assertIn("SERVICE_UNAVAILABLE", line)
        self.assertTrue((out / "impact.svg").exists())

    def test_the_cache_is_left_byte_identical(self):
        # The cache is the only data left on this path.
        line, _out, cache_file = self.fallback_run(self.BOOM)
        snapshot = cache_file.read_bytes()
        self.fallback_run(self.BOOM)
        self.assertEqual(cache_file.read_bytes(), snapshot)
        self.assertIn("fetch_all", line)

    def test_a_multiline_error_keeps_the_line_to_one_line(self):
        line, _out, _cache = self.fallback_run(
            RuntimeError("GraphQL errors:\n  one\n  two"))
        self.assertIn("two", line)

    def test_an_incomplete_cache_still_propagates(self):
        # No fallback data: the fetch's own error escapes and the run exits
        # non-zero rather than rendering a partial card.
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td)
        argv = ["render-impact.py", "--user", "me", "--token", "t",
                "--out-dir", str(Path(td) / "out"),
                "--cache-file", str(Path(td) / "impact-cache.json")]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(render_impact, "fetch_all",
                                  side_effect=self.BOOM), \
                redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                render_impact.main()


if __name__ == "__main__":
    unittest.main()
