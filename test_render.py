#!/usr/bin/env python3
"""Tests for render.py. Stdlib only, like the thing it tests.

    python3 -m unittest discover -v

No network: every case is a hand-built contribution calendar.
"""
import datetime
import importlib.util
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "render", Path(__file__).with_name("render.py"))
if spec is None or spec.loader is None:
    raise SystemExit("error: cannot load render.py")
render = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render)


def calendar(counts):
    """Build a contributionCalendar `weeks` payload from oldest->newest counts."""
    base = datetime.date(2026, 1, 1)
    return [{"contributionDays": [
        {"date": (base + datetime.timedelta(days=i)).isoformat(),
         "contributionCount": c}
        for i, c in enumerate(counts)
    ]}]


class ComputeStreak(unittest.TestCase):
    def current(self, counts):
        return render.compute_streak(calendar(counts))[0]

    def longest(self, counts):
        return render.compute_streak(calendar(counts))[1]

    def test_today_not_logged_yet_forgives_one_zero(self):
        # The whole reason a leading zero is skipped: the newest day may not
        # be recorded yet, or the calendar's UTC day is ahead of the viewer.
        self.assertEqual(self.current([1] * 5 + [0]), 5)

    def test_two_zeros_end_the_streak(self):
        # A second zero is a real gap, not a logging artifact.
        self.assertEqual(self.current([1] * 5 + [0, 0]), 0)

    def test_streak_that_ended_long_ago_is_not_current(self):
        # Regression: skipping *every* leading zero reported a month-old
        # streak as current.
        self.assertEqual(self.current([1] * 5 + [0] * 30), 0)

    def test_active_streak_counts_to_today(self):
        self.assertEqual(self.current([0, 0] + [1] * 7), 7)

    def test_no_contributions(self):
        self.assertEqual(self.current([0] * 10), 0)

    def test_every_day(self):
        self.assertEqual(self.current([1] * 10), 10)

    def test_single_day(self):
        self.assertEqual(self.current([1]), 1)

    def test_single_zero_day(self):
        self.assertEqual(self.current([0]), 0)

    def test_empty_calendar(self):
        self.assertEqual(self.current([]), 0)

    def test_longest_spans_gaps(self):
        self.assertEqual(self.longest([1, 1, 0, 1, 1, 1, 0, 1]), 3)

    def test_longest_ignores_the_leading_zero_rule(self):
        # `longest` looks at the whole window; the skip-one rule is only
        # about what counts as *current*.
        self.assertEqual(self.longest([1] * 5 + [0] * 30), 5)


class ExternalContributions(unittest.TestCase):
    def prs(self, *specs):
        return [{"merged": m,
                 "repository": {"nameWithOwner": nwo,
                                "isPrivate": priv,
                                "owner": {"login": nwo.split("/")[0]}}}
                for nwo, m, priv in specs]

    def test_excludes_own_repos_and_orgs(self):
        prs = self.prs(
            ("me/mine", True, False),          # own account
            ("MyOrg/thing", True, False),      # own org
            ("someone/theirs", True, False),   # external
        )
        self.assertEqual(
            render.external_contributions(prs, "me", ["MyOrg"]), (1, 1, 1))

    def test_org_match_is_case_insensitive(self):
        prs = self.prs(("MYORG/thing", True, False))
        self.assertEqual(
            render.external_contributions(prs, "me", ["myorg"]), (0, 0, 0))

    def test_excludes_private_repos(self):
        prs = self.prs(("someone/secret", True, True))
        self.assertEqual(render.external_contributions(prs, "me", []), (0, 0, 0))

    def test_counts_unmerged_as_opened_only(self):
        prs = self.prs(("a/x", True, False), ("a/y", False, False))
        opened, merged, _ = render.external_contributions(prs, "me", [])
        self.assertEqual((opened, merged), (2, 1))

    def test_repos_are_deduplicated(self):
        prs = self.prs(("a/x", True, False), ("a/x", True, False))
        self.assertEqual(render.external_contributions(prs, "me", [])[2], 1)

    def test_no_external_prs(self):
        self.assertEqual(render.external_contributions([], "me", []), (0, 0, 0))


class ExternalIssues(unittest.TestCase):
    def issues(self, *specs):
        # spec: (nameWithOwner, state, stateReason, isPrivate)
        return [{"state": state,
                 "stateReason": reason,
                 "repository": {"nameWithOwner": nwo,
                                "isPrivate": priv,
                                "owner": {"login": nwo.split("/")[0]}}}
                for nwo, state, reason, priv in specs]

    def test_excludes_own_repos_and_orgs(self):
        issues = self.issues(
            ("me/mine", "CLOSED", "COMPLETED", False),        # own account
            ("MyOrg/thing", "CLOSED", "COMPLETED", False),    # own org
            ("someone/theirs", "CLOSED", "COMPLETED", False),  # external
        )
        # one external issue, in one external repo, maintainer-accepted
        self.assertEqual(
            render.external_issues(issues, "me", ["MyOrg"]), (1, 1, 1))

    def test_org_match_is_case_insensitive(self):
        issues = self.issues(("MYORG/thing", "CLOSED", "COMPLETED", False))
        self.assertEqual(
            render.external_issues(issues, "me", ["myorg"]), (0, 0, 0))

    def test_excludes_private_repos(self):
        issues = self.issues(("someone/secret", "CLOSED", "COMPLETED", True))
        self.assertEqual(render.external_issues(issues, "me", []), (0, 0, 0))

    def test_only_completed_closures_count_as_accepted(self):
        # OPEN and NOT_PLANNED issues are opened-but-not-accepted; only a
        # CLOSED issue with stateReason COMPLETED counts as maintainer-
        # accepted (the merged-PR analog).
        issues = self.issues(
            ("a/x", "CLOSED", "COMPLETED", False),
            ("a/y", "CLOSED", "NOT_PLANNED", False),
            ("a/z", "OPEN", None, False),
        )
        opened, accepted, _ = render.external_issues(issues, "me", [])
        self.assertEqual((opened, accepted), (3, 1))

    def test_repos_are_deduplicated(self):
        issues = self.issues(
            ("a/x", "CLOSED", "COMPLETED", False),
            ("a/x", "OPEN", None, False),
        )
        self.assertEqual(render.external_issues(issues, "me", [])[2], 1)

    def test_no_external_issues(self):
        self.assertEqual(render.external_issues([], "me", []), (0, 0, 0))


CORE_USER = {
    "login": "me",
    "name": "Me",
    "followers": {"totalCount": 5},
    "organizations": {"nodes": []},
    "repositories": {"totalCount": 1, "nodes": [{
        "stargazerCount": 3,
        "languages": {"edges": [
            {"size": 100, "node": {"name": "Python", "color": "#3572A5"}}]},
    }]},
}

EXTERNAL_REPO = {"nameWithOwner": "other/proj", "isPrivate": False,
                 "owner": {"login": "other"}}


def weeks_of(days):
    """A one-week contributionCalendar payload from a date -> count map."""
    return [{"contributionDays": [
        {"date": d, "contributionCount": c} for d, c in sorted(days.items())]}]


class FakeAPI:
    """Scriptable replacement for render.gql; routes on the query text."""

    def __init__(self):
        self.calls = []           # (query, variables) actually sent
        self.full_calendar = {}   # the whole year's date -> count map
        self.served = []          # sorted dates returned per calendar call
        self.pr_pages = []        # one nodes list per pullRequests call
        self.issues = []
        self.fail = False

    def __call__(self, token, query, variables=None, retries=3):
        if self.fail:
            raise RuntimeError("simulated fetch failure")
        variables = variables or {}
        self.calls.append((query, variables))
        if "pullRequests" in query:
            nodes = self.pr_pages.pop(0) if self.pr_pages else []
            return {"user": {"pullRequests": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": nodes}}}
        if "issues" in query:
            return {"user": {"issues": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": self.issues}}}
        if "contributionCalendar" in query:
            days = self.full_calendar
            if "from" in variables:
                # Serve the slice the real API would: half-open [from, to),
                # matching "to: only contributions made before this time".
                frm = datetime.datetime.fromisoformat(variables["from"])
                to = datetime.datetime.fromisoformat(variables["to"])
                days = {d: c for d, c in days.items()
                        if frm <= datetime.datetime.fromisoformat(d)
                        .replace(tzinfo=datetime.timezone.utc) < to}
            self.served.append(sorted(days))
            return {"user": {"contributionsCollection": {
                "contributionCalendar": {
                    "totalContributions": sum(days.values()),
                    "weeks": weeks_of(days)}}}}
        return {"user": json.loads(json.dumps(CORE_USER))}  # fresh copy per call


def run_main(api, out_dir, cache_file):
    """Invoke render.main() against the fake API with argv/env patched in."""
    argv = ["render.py", "--user", "me", "--token", "fake",
            "--theme", "tokyonight", "--out-dir", str(out_dir)]
    with mock.patch.object(render, "gql", api), \
            mock.patch.object(sys, "argv", argv), \
            mock.patch.dict(os.environ, {"CACHE_FILE": str(cache_file)}):
        render.main()


def recent_days(n):
    """date -> count for the n days ending today (UTC)."""
    today = datetime.datetime.now(datetime.timezone.utc).date()
    return {(today - datetime.timedelta(days=i)).isoformat(): (i * 7 + 3) % 5
            for i in range(n)}


class CacheFile(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "cache.json"
            payload = {"version": render.CACHE_VERSION, "fetched_at": "x",
                       "calendar_days": {"2026-01-01": 1}}
            render.save_cache(f, payload)
            self.assertEqual(render.load_cache(f), payload)

    def test_version_mismatch_is_discarded(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "cache.json"
            f.write_text(json.dumps({"version": render.CACHE_VERSION + 1}))
            self.assertEqual(render.load_cache(f), {})

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(render.load_cache("/nonexistent/dir/cache.json"), {})


class WindowedCalendar(unittest.TestCase):
    def calendar_vars(self, api):
        return [v for q, v in api.calls if "contributionCalendar" in q]

    def test_cold_backfill_merges_12_monthly_windows(self):
        # The cold path must rebuild, from 12 monthly windowed responses,
        # exactly the day map the old single full-year query produced:
        # same dates, same counts (the fake serves window-correct slices
        # of the one full-year map, so equality is the whole point, not
        # the call count).
        all_days = recent_days(365)
        api = FakeAPI()
        api.full_calendar = all_days
        with mock.patch.object(render, "gql", api):
            _, cold_days = render.fetch("t", "me")

        self.assertEqual(cold_days, all_days)

        window_vars = self.calendar_vars(api)
        self.assertEqual(len(window_vars), 12)
        for v in window_vars:
            self.assertIn("from", v)
            self.assertIn("to", v)
            span = (datetime.datetime.fromisoformat(v["to"])
                    - datetime.datetime.fromisoformat(v["from"]))
            # Monthly windows: each far below the node limit. (On a
            # Feb-29 run the prune cutoff lands on Feb 28, so the final
            # window absorbs the extra day and spans 32.)
            self.assertGreaterEqual(span.days, 28)
            self.assertLessEqual(span.days, 32)

    def test_windows_cover_the_year_without_gaps_or_duplicates(self):
        all_days = recent_days(366)  # reaches the prune cutoff date itself
        api = FakeAPI()
        api.full_calendar = all_days
        with mock.patch.object(render, "gql", api):
            render.fetch("t", "me")

        served = api.served  # one sorted date list per calendar call
        self.assertEqual(len(served), 12)

        # Consecutive windows share their boundary instant exactly.
        window_vars = self.calendar_vars(api)
        for prev, cur in zip(window_vars, window_vars[1:]):
            self.assertEqual(prev["to"], cur["from"])

        # No date is served by two windows...
        flat = [d for dates in served for d in dates]
        self.assertEqual(len(flat), len(set(flat)))
        # ...the union is the full year the single query would return...
        self.assertEqual(set(flat), set(all_days))
        # ...and coverage is contiguous: no skipped day anywhere.
        dates = [datetime.date.fromisoformat(d) for d in sorted(flat)]
        for prev, cur in zip(dates, dates[1:]):
            self.assertEqual((cur - prev).days, 1)

    def test_warm_path_still_fetches_a_single_7_day_window(self):
        all_days = recent_days(30)
        today = datetime.datetime.now(datetime.timezone.utc).date()
        cutoff = (today - datetime.timedelta(days=6)).isoformat()
        cached = {d: c for d, c in all_days.items() if d < cutoff}

        api = FakeAPI()
        api.full_calendar = all_days
        with mock.patch.object(render, "gql", api):
            user_cold, cold_days = render.fetch("t", "me")
            api.calls.clear()
            user_warm, warm_days = render.fetch("t", "me", cached)

        # The cached history plus a 7-day window must rebuild exactly what
        # the cold backfill returns — same days, same renderer structure.
        self.assertEqual(cold_days, warm_days)
        self.assertEqual(user_cold["contributionsCollection"],
                         user_warm["contributionsCollection"])

        # The warm path must ask for exactly one trailing 7-day window.
        window_vars = self.calendar_vars(api)
        self.assertEqual(len(window_vars), 1)
        self.assertIn("from", window_vars[0])
        span = (datetime.datetime.fromisoformat(window_vars[0]["to"])
                - datetime.datetime.fromisoformat(window_vars[0]["from"]))
        self.assertEqual(span.days, 7)


class DurabilityFallback(unittest.TestCase):
    def snapshot(self):
        return {
            "version": render.CACHE_VERSION,
            "fetched_at": "2026-07-20T06:00:00+00:00",
            "user": json.loads(json.dumps(CORE_USER)),
            "calendar_days": recent_days(3),
            "prs": {"P1": {"id": "P1", "merged": True,
                           "repository": EXTERNAL_REPO}},
            "issues": [],
        }

    def test_failed_fetch_renders_from_cache_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as td:
            cache_file = Path(td) / "cache.json"
            out = Path(td) / "out"
            render.save_cache(cache_file, self.snapshot())
            api = FakeAPI()
            api.fail = True
            # main() returning normally (not propagating the fetch error) is
            # the exit-0 path; the __main__ wrapper only exits non-zero when
            # an exception escapes.
            run_main(api, out, cache_file)
            for name in ("stats.svg", "streak.svg", "languages.svg", "external.svg"):
                svg = (out / name).read_text()
                self.assertIn("cached data from 2026-07-20T06:00:00+00:00", svg)
            self.assertEqual((out / "last-updated.txt").read_text(),
                             "2026-07-20T06:00:00+00:00")
            # A fallback run must not refresh the cache's timestamp.
            self.assertEqual(render.load_cache(cache_file)["fetched_at"],
                             "2026-07-20T06:00:00+00:00")

    def test_failed_fetch_without_cache_still_fails(self):
        with tempfile.TemporaryDirectory() as td:
            api = FakeAPI()
            api.fail = True
            with self.assertRaises(RuntimeError):
                run_main(api, Path(td) / "out", Path(td) / "cache.json")


class PullRequestCache(unittest.TestCase):
    def big_numbers(self, svg):
        # The six 36px figures on external.svg, in render order:
        # PR opened/merged/repos, then issue opened/accepted/repos.
        return re.findall(r'font-size="36"[^>]*>([^<]+)</text>', svg)

    def test_open_to_merged_transition_counts_exactly_once(self):
        with tempfile.TemporaryDirectory() as td:
            cache_file = Path(td) / "cache.json"
            out = Path(td) / "out"
            api = FakeAPI()
            api.full_calendar = recent_days(3)
            pr = {"id": "P1", "merged": False, "repository": EXTERNAL_REPO}

            # Run 1 (cold): the PR is OPEN, queried with all three states.
            api.pr_pages = [[pr]]
            run_main(api, out, cache_file)
            nums = self.big_numbers((out / "external.svg").read_text())
            self.assertEqual(nums[:3], ["1", "0", "1"])

            # Run 2 (warm): the PR has merged, so the OPEN/CLOSED query no
            # longer returns it. It must move to the cached half and be
            # counted exactly once: still 1 opened, now 1 merged.
            api.calls.clear()
            api.pr_pages = [[]]
            run_main(api, out, cache_file)
            nums = self.big_numbers((out / "external.svg").read_text())
            self.assertEqual(nums[:3], ["1", "1", "1"])

            pr_queries = [q for q, _ in api.calls if "pullRequests" in q]
            self.assertEqual(len(pr_queries), 2)
            self.assertIn("[OPEN, CLOSED]", pr_queries[0])
            self.assertNotIn("MERGED", pr_queries[0])
            # A warm run additionally makes ONE single-page MERGED sweep so a
            # PR opened and merged between renders is not lost (issue #3).
            self.assertIn("[MERGED]", pr_queries[1])
            self.assertTrue(render.load_cache(cache_file)["prs"]["P1"]["merged"])


class CorruptCache(unittest.TestCase):
    def test_corrupt_cache_falls_back_to_full_fetch(self):
        with tempfile.TemporaryDirectory() as td:
            cache_file = Path(td) / "cache.json"
            cache_file.write_text("{not valid json")
            out = Path(td) / "out"
            api = FakeAPI()
            api.full_calendar = recent_days(3)
            api.pr_pages = [[{"id": "P1", "merged": True,
                              "repository": EXTERNAL_REPO}]]
            run_main(api, out, cache_file)

            calendar_vars = [v for q, v in api.calls
                             if "contributionCalendar" in q]
            # A cold cache backfills the whole year in 12 monthly windows.
            self.assertEqual(len(calendar_vars), 12)
            for v in calendar_vars:
                self.assertIn("from", v)
            pr_queries = [q for q, _ in api.calls if "pullRequests" in q]
            self.assertIn("MERGED", pr_queries[0])  # all states refetched
            for name in ("stats.svg", "streak.svg", "languages.svg", "external.svg"):
                self.assertTrue((out / name).exists())
            # The successful run rewrites a clean, current-version cache.
            self.assertEqual(render.load_cache(cache_file)["version"],
                             render.CACHE_VERSION)


if __name__ == "__main__":
    unittest.main()
