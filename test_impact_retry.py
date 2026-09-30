#!/usr/bin/env python3
"""Tests for impact_loc.update_loc's retry of a previously failed repo.

    python3 -m unittest discover -v

A clone or blame failure caches an `{"error", head}` entry. That entry
carries no `ours`/`total`, so it contributes no row to the card, and if
`update_loc` also SKIPS it at an unchanged head the repo stays frozen out
until its HEAD moves or the weekly --resync re-blamed everything. These
tests pin the skip to a real count, and the behaviour the fix must not
break while doing so.
"""
import importlib.util
import os
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


render_impact = _load("render_impact", "render-impact.py")


class TestFailedRepoIsRetried(unittest.TestCase):
    """A cached failure is not a cached count, so it must not suppress a retry.

    Without this, one transient clone or blame failure freezes a repo out
    of the card until its default HEAD moves or the weekly `--resync`
    re-blamed everything.
    """

    loc = getattr(render_impact, "_LOC_MODULE")

    def moved_with(self, totals, cached, *, resync=False):
        seen = []
        with mock.patch.dict(os.environ, {"IMPACT_REPO_PINS": ""}):
            self.loc.update_loc(set(totals), totals, cached, resync, {"e@x"},
                                blame_fn=lambda moved, *_a: seen.extend(moved))
        return seen

    def test_error_at_an_unchanged_head_is_blamed_again(self):
        totals = {"o/r": {"branch": "main", "head": "h1"}}
        seen = self.moved_with(totals,
                               {"o/r": {"error": "clone_failed", "head": "h1"}})
        self.assertEqual([repo for repo, _t in seen], ["o/r"])

    def test_a_count_at_an_unchanged_head_is_still_skipped(self):
        """The retry must not cost every healthy repo a re-clone each hour."""
        totals = {"o/r": {"branch": "main", "head": "h1"}}
        seen = self.moved_with(
            totals,
            {"o/r": {"ours": 3, "total": 9, "branch": "main", "head": "h1"}})
        self.assertEqual(seen, [])

    def test_resync_blames_a_count_at_an_unchanged_head(self):
        totals = {"o/r": {"branch": "main", "head": "h1"}}
        seen = self.moved_with(
            totals,
            {"o/r": {"ours": 3, "total": 9, "branch": "main", "head": "h1"}},
            resync=True)
        self.assertEqual([repo for repo, _t in seen], ["o/r"])

    def test_a_retried_repo_replaces_its_error_with_a_count(self):
        """End to end through blame_moved: the row comes back to the card."""
        totals = {"o/r": {"branch": "main", "head": "h1"}}
        cached = {"o/r": {"error": "clone_failed", "head": "h1"}}
        with mock.patch.dict(os.environ, {"IMPACT_REPO_PINS": ""}), \
                mock.patch.object(self.loc, "prefetched_clones",
                                  self._clones_ok), \
                mock.patch.object(self.loc, "counts_for",
                                  return_value=(11, 40)) as counts:
            out = self.loc.update_loc(set(totals), totals, cached, False,
                                      {"e@x"},
                                      blame_fn=self.loc.blame_moved)
        self.assertEqual(counts.call_count, 1,
                         "the failed repo must actually be re-blamed")
        self.assertEqual(out["o/r"], {"ours": 11, "total": 40,
                                      "branch": "main", "head": "h1"})
        rows = render_impact.loc_table(out, render_impact.metric_knobs())
        self.assertEqual([(row.repo, row.ours, row.total) for row in rows],
                         [("o/r", 11, 40)])

    @staticmethod
    def _clones_ok(moved):
        for repo, t in moved:
            tmp = tempfile.mkdtemp(prefix="ghw-retry-")
            yield repo, t, Path(tmp), 0.1, 0.2, None
