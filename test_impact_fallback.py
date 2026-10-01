#!/usr/bin/env python3
"""Tests for render-impact.py's durability fallback.

    python3 -m unittest discover -v

Separate from test_impact.py because that module is about the blame pass and
its external git-fame dependency; this is about what a run prints when the
network fails and the cache carries the run.

No network and no git: the acquisitions are stubbed, so the fallback path is
exercised without either. `fetch_all` itself is stubbed for the cases that
only care about the fallback, but the cases that care WHICH acquisition failed
run it for real, one inner acquisition broken at a time — a fallback line that
names `fetch_all` for all five of them is exactly issue #55's defect, moved
into this renderer.
"""
import contextlib
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


# Each inner acquisition of fetch_all, and where the callable it calls lives.
# `fetch_identity` is the one fetch_all reaches through `common`, the other
# four are its own module globals — so the patch target differs per phase.
INNER_ACQUISITIONS = {
    "fetch_identity": "common",
    "fetch_pull_requests": "module",
    "fetch_issues": "module",
    "fetch_repo_totals": "module",
    "update_loc": "module",
}

EMPTY_PAGE = {"pageInfo": {"hasNextPage": False, "endCursor": None}}


def fake_gql(token, query, variables=None, **kwargs):
    """Just enough of the API for fetch_all to reach its fifth acquisition:
    identity, then one empty page each of PRs and issues. With no external
    repos there is nothing to blame, so the run never reaches git."""
    if "organizations" in query:
        return {"user": {"login": "me", "databaseId": 1,
                         "organizations": {**EMPTY_PAGE, "nodes": []}}}
    if "pullRequests" in query:
        return {"user": {"pullRequests": {**EMPTY_PAGE, "nodes": []}}}
    return {"user": {"issues": {**EMPTY_PAGE, "nodes": []}}}


# A PR node missing the fields `is_external` indexes, so fetch_all raises on
# `repos = sorted(...)` — a statement that sits OUTSIDE all five acquisition
# blocks, and therefore has no label of its own.
MALFORMED_PR = {"id": "P1", "merged": True,
                "repository": {"nameWithOwner": "someone/theirs"}}


def malformed_gql(token, query, variables=None, **kwargs):
    """fake_gql, but the PR page carries a node is_external() cannot read."""
    if "pullRequests" in query:
        return {"user": {"pullRequests": {**EMPTY_PAGE,
                                          "nodes": [MALFORMED_PR]}}}
    return fake_gql(token, query, variables, **kwargs)


class TestDurabilityFallback(unittest.TestCase):
    """A failed acquisition renders from the cache and says which one failed."""

    BOOM = RuntimeError("GraphQL errors: [{'type': 'SERVICE_UNAVAILABLE'}]")

    def snapshot(self):
        return {"version": render_impact.CACHE_VERSION,
                "fetched_at": "2026-07-20T06:00:00+00:00",
                "insiders": ["me"],
                "prs": {}, "issues": [], "totals": {}, "ourloc": {}}

    def run_main(self, *patchers, gql=None, cache=True):
        """main() with `patchers` in place. `cache` False means no cache file
        at all, which is the incomplete-cache path. Returns (everything main
        printed, the output directory, the cache file)."""
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td)
        cache_file = Path(td) / "impact-cache.json"
        if cache:
            cache_file.write_text(json.dumps(self.snapshot()))
        out = Path(td) / "out"
        argv = ["render-impact.py", "--user", "me", "--token", "t",
                "--out-dir", str(out), "--cache-file", str(cache_file)]
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(render_impact, "gql", gql or fake_gql), \
                redirect_stdout(buf), \
                contextlib.ExitStack() as stack:
            for patcher in patchers:
                stack.enter_context(patcher)
            render_impact.main()
        return buf.getvalue(), out, cache_file

    def broken(self, name, error):
        """A patcher that makes the named callable raise `error`."""
        target = (render_impact.common if INNER_ACQUISITIONS[name] == "common"
                  else render_impact)
        return mock.patch.object(target, name, side_effect=error)

    def fallback_line(self, text):
        """The run's one fallback line, out of everything the run printed.

        Counted over the whole output rather than asserted on a
        `splitlines()` element: the line cannot contain a newline by
        construction, so that property has to be asserted somewhere else
        (see the collapsed-message test).
        """
        matched = [line for line in text.splitlines()
                   if line.startswith("fetch failed")]
        self.assertEqual(len(matched), 1)
        return matched[0]

    def whole_fetch_failure(self, error):
        """main() with the whole fetch entry point failing."""
        return self.run_main(
            mock.patch.object(render_impact, "fetch_all", side_effect=error))

    def test_the_line_names_the_acquisition_and_the_error(self):
        text, out, _cache = self.whole_fetch_failure(self.BOOM)
        line = self.fallback_line(text)
        self.assertIn("fetch_all", line)
        self.assertIn("SERVICE_UNAVAILABLE", line)
        self.assertTrue((out / "impact.svg").exists())

    def test_each_inner_acquisition_is_named_in_its_own_line(self):
        # The whole point: five acquisitions behind one entry point, five
        # names. Naming only the entry point is issue #55's defect.
        for phase in INNER_ACQUISITIONS:
            with self.subTest(phase=phase):
                text, _out, _cache = self.run_main(self.broken(phase, self.BOOM))
                self.assertIn(phase, self.fallback_line(text))

    def test_the_five_lines_are_distinguishable(self):
        # The output directory differs per run, so everything from "; rendered"
        # on is normalised away — otherwise the lines would be "distinct" for
        # a reason that has nothing to do with the phase.
        lines = set()
        for phase in INNER_ACQUISITIONS:
            text, _out, _cache = self.run_main(self.broken(
                phase, RuntimeError("GraphQL errors: SERVICE_UNAVAILABLE")))
            lines.add(self.fallback_line(text).split("; rendered")[0])
        self.assertEqual(len(lines), len(INNER_ACQUISITIONS))

    def test_a_leftover_label_does_not_name_the_next_run(self):
        # Two runs in one process, which no single-run control can see. Run 1
        # fails inside an acquisition with NO cache, so it re-raises and
        # nothing consumes the label. Run 2's acquisitions all succeed and the
        # failure lands on a statement outside every one of them — the honest
        # name is the entry point, and a leftover from run 1 would suppress
        # exactly that.
        with self.assertRaises(RuntimeError):
            self.run_main(self.broken("fetch_issues", self.BOOM), cache=False)
        text, _out, _cache = self.run_main(gql=malformed_gql)
        line = self.fallback_line(text)
        self.assertIn("fetch_all", line)
        self.assertNotIn("fetch_issues", line)

    def test_the_fallback_run_does_not_write_the_cache(self):
        # The cache is the only data left on this path; writing a
        # half-fetched set over it would throw that away too.
        _text, _out, cache_file = self.run_main(
            mock.patch.object(render_impact, "fetch_all", side_effect=self.BOOM))
        before = cache_file.read_bytes()
        self.run_main(mock.patch.object(render_impact, "fetch_all",
                                        side_effect=self.BOOM))
        self.assertEqual(cache_file.read_bytes(), before)

    def test_a_multiline_error_keeps_the_line_to_one_line(self):
        # Asserted over the WHOLE output: an uncollapsed message would spill
        # "one" and "two" onto lines of their own, which no per-line
        # assertion on the matched line would see.
        text, _out, _cache = self.whole_fetch_failure(
            RuntimeError("GraphQL errors:\n  one\n  two"))
        line = self.fallback_line(text)
        self.assertIn("GraphQL errors: one two", text)
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
