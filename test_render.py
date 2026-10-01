#!/usr/bin/env python3
"""Tests for render.py. Stdlib only, like the thing it tests.

    python3 -m unittest discover -v

No network: every case is a hand-built contribution calendar.
"""
import datetime
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stdout
from pathlib import Path
from typing import Optional
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
        "forkCount": 2,
        "languages": {"edges": [
            {"size": 100, "node": {"name": "Python", "color": "#3572A5"}}]},
    }]},
}

EXTERNAL_REPO = {"nameWithOwner": "other/proj", "isPrivate": False,
                 "owner": {"login": "other"}}

PR_NODE = {"id": "P1", "merged": True, "repository": EXTERNAL_REPO}


def complete_cache():
    """A cache holding every input load_inputs needs, so a failed fetch can
    still render (the durability fallback's precondition)."""
    return {
        "version": render.CACHE_VERSION,
        "fetched_at": "2026-07-20T06:00:00+00:00",
        "user": json.loads(json.dumps(CORE_USER)),
        "calendar_days": recent_days(3),
        "prs": {"P1": json.loads(json.dumps(PR_NODE))},
        "issues": [],
    }


def repo_node(stars=0, forks=0, edges=()):
    """One repositories-connection node. `edges` is (size, name, color)."""
    return {"stargazerCount": stars, "forkCount": forks,
            "languages": {"edges": [
                {"size": size, "node": {"name": name, "color": color}}
                for size, name, color in edges]}}


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
        self.org_pages = []       # one nodes list per organizations call
        self.repo_pages = []      # one nodes list per repositories call
        # repositories.totalCount when the fixture scripts its own pages;
        # None means "whatever this page happens to hold".
        self.repo_total: Optional[int] = None

    def page_info(self, pages_left):
        """pageInfo for a scripted page: `pages_left` further pages are queued.

        The cursor is derived from the remaining depth, so two consecutive
        pages of one connection never hand back the same cursor.
        """
        if not pages_left:
            return {"hasNextPage": False, "endCursor": None}
        return {"hasNextPage": True, "endCursor": f"cursor-{pages_left}"}

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
        if "organizations" in query:
            nodes = self.org_pages.pop(0) if self.org_pages else []
            return {"user": {"login": "me", "name": "Me",
                             "followers": {"totalCount": 5},
                             "organizations": {
                                 "pageInfo": self.page_info(len(self.org_pages)),
                                 "nodes": nodes}}}
        if "repositories" in query:
            if self.repo_pages:
                nodes = self.repo_pages.pop(0)
            else:
                nodes = json.loads(json.dumps(
                    CORE_USER["repositories"]["nodes"]))
            total = (self.repo_total if self.repo_total is not None
                     else len(nodes))
            return {"user": {"repositories": {
                "totalCount": total,
                "pageInfo": self.page_info(len(self.repo_pages)),
                "nodes": nodes}}}
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
        return complete_cache()

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


class FallbackNaming(unittest.TestCase):
    """The fallback line says WHICH acquisition failed and why (issue #55).

    Injecting the same error at each of the three phases used to produce
    three byte-identical lines, so an operator reading a journal had nothing
    to act on beyond "something, somewhere, went wrong".
    """

    PHASES = ("fetch", "fetch_pull_requests", "fetch_issues")
    BOOM = "GraphQL errors: SERVICE_UNAVAILABLE"

    def fallback_run(self, phase, error):
        """Run main() with `phase` failing against a complete cache, and
        return (everything it printed, the output directory)."""
        with tempfile.TemporaryDirectory() as td:
            cache_file = Path(td) / "cache.json"
            out = Path(td) / "out"
            render.save_cache(cache_file, complete_cache())
            api = FakeAPI()
            api.full_calendar = recent_days(3)
            buf = io.StringIO()
            with mock.patch.object(render, phase, side_effect=error), \
                    redirect_stdout(buf):
                run_main(api, out, cache_file)
            # The durability contract is unchanged: cards written, cache
            # timestamp untouched, and main() returned rather than raising
            # (a non-zero exit comes from the __main__ wrapper).
            for name in ("stats.svg", "streak.svg", "languages.svg",
                         "external.svg"):
                self.assertTrue((out / name).exists())
            self.assertEqual(render.load_cache(cache_file)["fetched_at"],
                             "2026-07-20T06:00:00+00:00")
            return buf.getvalue(), out

    def fallback_line(self, phase, error):
        """The run's one fallback line, out of everything it printed.

        Counted over the whole output: the matched line cannot contain a
        newline by construction, so the one-line property is asserted where it
        can fail — see the collapsed-message test.
        """
        text, out = self.fallback_run(phase, error)
        matched = [line for line in text.splitlines()
                   if line.startswith("fetch failed")]
        self.assertEqual(len(matched), 1)
        return matched[0], out

    def test_each_phase_names_itself_and_its_error(self):
        for phase in self.PHASES:
            with self.subTest(phase=phase):
                error = RuntimeError(f"boom during {phase}")
                line, _out = self.fallback_line(phase, error)
                self.assertIn(phase, line)
                self.assertIn("boom during " + phase, line)

    def test_the_three_lines_are_distinguishable(self):
        # The point of the fix: one identical failure at three different
        # phases must not read identically in a journal. The output path
        # differs per run, so it is normalized away — otherwise every line
        # would be "distinct" for a reason that has nothing to do with it.
        lines = set()
        for phase in self.PHASES:
            line, out = self.fallback_line(
                phase, RuntimeError(self.BOOM))
            lines.add(line.replace(str(out), "<out>"))
        self.assertEqual(len(lines), 3)

    def test_the_line_is_still_one_line_without_a_traceback(self):
        # A multi-line error text (a JSON body, a wrapped traceback) must not
        # turn the summary into a journal the operator has to reassemble.
        # Asserted over the WHOLE output: uncollapsed, "line two" and "line
        # three" would be lines of their own, which no assertion confined to
        # the matched line can see.
        text, out = self.fallback_run(
            "fetch", RuntimeError("boom:\n  line two\n  line three"))
        self.assertIn("boom: line two line three", text)
        matched = [line for line in text.splitlines()
                   if line.startswith("fetch failed")]
        self.assertEqual(len(matched), 1)
        self.assertIn("line three", matched[0].replace(str(out), "<out>"))

    def test_an_exception_with_no_message_still_names_its_type(self):
        line, _out = self.fallback_line("fetch_issues", RuntimeError())
        self.assertIn("fetch_issues", line)
        self.assertIn("RuntimeError", line)

    def test_a_terminal_escape_in_the_error_is_stripped(self):
        # This line is the first place these renderers put server-supplied
        # text into a journal, and a terminal acts on ESC: an ANSI clear or an
        # OSC title set here rewrites the operator's screen. The same control
        # characters xml_escape strips for SVG text.
        line, _out = self.fallback_line(
            "fetch", RuntimeError("GraphQL errors: [\x1b[2J\x1b[H pwned "
                                  "\x1b]0;hijacked\x07]"))
        self.assertNotIn("\x1b", line)
        self.assertNotIn("\x07", line)
        self.assertIn("pwned", line)
        self.assertIn("hijacked", line)


class ProfilePagination(unittest.TestCase):
    """The profile connections are paged, not truncated at their first 100.

    Everything derived from `user` — stars, forks, languages, and above all
    the insider set that decides whether a PR is the account's own work — was
    silently capped at the first page while `repositories.totalCount` was not.
    """

    LANG = (500, "Python", "#3572A5")

    def paged_api(self):
        api = FakeAPI()
        api.repo_pages = [[repo_node(1, 1) for _ in range(100)],
                          [repo_node(7, 3, [self.LANG])]]
        api.repo_total = 101
        api.org_pages = [[{"login": "first-org"}], [{"login": "late-org"}]]
        api.full_calendar = recent_days(3)
        return api

    def fetched(self, api, **kwargs):
        with mock.patch.object(render, "gql", api):
            user, _days = render.fetch("t", "me", **kwargs)
        return user

    def test_aggregates_cover_every_repository_page(self):
        user = self.fetched(self.paged_api())
        self.assertEqual(user["repositories"]["totalCount"], 101)
        self.assertEqual(len(user["repositories"]["nodes"]), 101)
        _, summary = render.build_svgs(
            render.THEMES["tokyonight"], user, [], [])
        self.assertEqual(summary[0], 107)  # 100×1 stars + the 101st's 7
        self.assertEqual(summary[1], 103)  # 100×1 forks + the 101st's 3

    def test_a_language_only_on_the_last_page_is_aggregated(self):
        user = self.fetched(self.paged_api())
        languages = render.aggregate_languages(
            user["repositories"]["nodes"])
        self.assertEqual(languages[0][0], "Python")
        self.assertEqual(languages[0][1], 500)

    def test_every_organization_reaches_the_insider_set(self):
        user = self.fetched(self.paged_api())
        self.assertEqual(
            [o["login"] for o in user["organizations"]["nodes"]],
            ["first-org", "late-org"])
        pr = {"merged": True,
              "repository": {"nameWithOwner": "late-org/thing",
                             "isPrivate": False,
                             "owner": {"login": "late-org"}}}
        # The membership arrived on page 2, so this is the account's own
        # work — the truncation classified it external and under-reported it.
        self.assertEqual(render.external_counts(user, [pr], []).pr_opened, 0)

    def test_a_genuinely_external_repo_is_still_external(self):
        # Paging in more organizations must not widen the insider set beyond
        # the ones actually returned.
        user = self.fetched(self.paged_api())
        self.assertEqual(
            render.external_counts(user, [PR_NODE], []).pr_opened, 1)

    def test_a_single_page_account_makes_one_call_per_connection(self):
        api = self.paged_api()
        self.fetched(api)
        org_calls = [q for q, _ in api.calls if "organizations" in q]
        repo_calls = [q for q, _ in api.calls if "repositories" in q]
        self.assertEqual(len(org_calls), 2)   # two scripted pages
        self.assertEqual(len(repo_calls), 2)

    def test_the_page_bound_raises_instead_of_truncating(self):
        api = FakeAPI()
        api.org_pages = [[{"login": "only-org"}]]
        api.repo_pages = [[repo_node()], [repo_node()], [repo_node()]]
        with self.assertRaises(render.common.PaginationLimitError) as raised:
            self.fetched(api, max_pages=2)
        self.assertIn("repositories", str(raised.exception))
        self.assertIn("2", str(raised.exception))

    def test_a_connection_without_pageinfo_raises(self):
        # No pageInfo means the end of the connection cannot be established;
        # returning what arrived would be exactly the silent truncation this
        # replaces.
        def gql_fn(_token, query, variables=None, **_kwargs):
            if "repositories" in query:
                return {"user": {"repositories": {
                    "totalCount": 101, "nodes": [repo_node()]}}}
            if "contributionCalendar" in query:
                return {"user": {"contributionsCollection": {
                    "contributionCalendar": {"totalContributions": 0,
                                             "weeks": calendar([0])}}}}
            return {"user": {"login": "me", "name": "Me",
                             "followers": {"totalCount": 5},
                             "organizations": {
                                 "pageInfo": {"hasNextPage": False,
                                              "endCursor": None},
                                 "nodes": []}}}
        with mock.patch.object(render, "gql", gql_fn):
            with self.assertRaises(render.common.PaginationLimitError) as raised:
                render.fetch("t", "me")
        self.assertIn("repositories", str(raised.exception))
        self.assertIn("pageInfo", str(raised.exception))

    def stalling_gql(self, repos_pages, limit=10):
        """A gql that serves `repos_pages` — one repositories connection per
        entry, the last one repeated forever — and answers organizations and
        the calendar from their first page.

        The call limit is not decoration: with a stall guard deleted the walk
        never ends, and an unbounded fake would hang the suite instead of
        failing it. Returns (gql_fn, the cursors it was asked for).
        """
        pages = list(repos_pages)
        cursors = []

        def gql_fn(_token, query, variables=None, **_kwargs):
            if "contributionCalendar" in query:
                return {"user": {"contributionsCollection": {
                    "contributionCalendar": {"totalContributions": 0,
                                             "weeks": calendar([0])}}}}
            if "organizations" in query:
                return {"user": {"login": "me", "name": "Me",
                                 "followers": {"totalCount": 5},
                                 "organizations": {
                                     "pageInfo": {"hasNextPage": False,
                                                  "endCursor": None},
                                     "nodes": []}}}
            cursors.append((variables or {}).get("cursor"))
            if len(cursors) > limit:
                raise AssertionError(
                    f"the repositories walk did not stop after {limit} pages")
            return {"user": {"repositories": {
                "totalCount": 101,
                **pages[min(len(cursors) - 1, len(pages) - 1)]}}}
        return gql_fn, cursors

    def test_a_repeating_end_cursor_raises(self):
        # The server keeps saying "there is another page" and keeps handing
        # back a cursor already used. Walking it would never end.
        gql_fn, cursors = self.stalling_gql([
            {"pageInfo": {"hasNextPage": True, "endCursor": "LOOP"},
             "nodes": [repo_node()]}])
        with mock.patch.object(render, "gql", gql_fn):
            with self.assertRaises(render.common.PaginationLimitError) as raised:
                render.fetch("t", "me")
        self.assertIn("repositories", str(raised.exception))
        self.assertIn("LOOP", str(raised.exception))
        self.assertEqual(cursors, [None, "LOOP"])

    def test_has_next_page_without_an_end_cursor_raises(self):
        # hasNextPage=true and no cursor to continue from: the walk cannot
        # continue and cannot prove it reached the end.
        gql_fn, cursors = self.stalling_gql([
            {"pageInfo": {"hasNextPage": True, "endCursor": None},
             "nodes": [repo_node()]}])
        with mock.patch.object(render, "gql", gql_fn):
            with self.assertRaises(render.common.PaginationLimitError) as raised:
                render.fetch("t", "me")
        self.assertIn("repositories", str(raised.exception))
        self.assertIn("endCursor is missing", str(raised.exception))
        self.assertEqual(cursors, [None])

    def test_the_cached_user_keeps_its_shape(self):
        # The cache stores `user` verbatim; dropping pageInfo keeps it
        # byte-compatible with what older builds wrote and with what the
        # renderers read.
        user = self.fetched(self.paged_api())
        for connection in ("repositories", "organizations"):
            self.assertNotIn("pageInfo", user[connection])
        self.assertIn("totalCount", user["repositories"])
        self.assertEqual(
            set(user["repositories"]), {"totalCount", "nodes"})


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


class ForksReceived(unittest.TestCase):
    def user_with_forks(self, *fork_counts):
        """A build_svgs-ready user whose repos carry the given forkCounts."""
        user = json.loads(json.dumps(CORE_USER))
        user["repositories"] = {
            "totalCount": len(fork_counts),
            "nodes": [{"stargazerCount": 0, "forkCount": n, "languages": {"edges": []}}
                      for n in fork_counts]}
        user["contributionsCollection"] = {"contributionCalendar": {
            "totalContributions": 0, "weeks": calendar([0])}}
        return user

    def test_forks_are_summed_over_the_repo_nodes(self):
        # Same route as total_stars: a plain sum over the nodes the filtered
        # core query returned.
        svgs, summary = render.build_svgs(
            render.THEMES["tokyonight"], self.user_with_forks(2, 5, 0), [], [])
        self.assertIn("forks received", svgs["stats.svg"])
        self.assertIn(">7</text>", svgs["stats.svg"])
        self.assertEqual(summary[1], 7)  # total_forks in the summary tuple

    def test_core_query_keeps_its_filters_and_selects_fork_count(self):
        # Owned-only, non-fork, public repos are filtered server-side; a
        # forked, private, or not-owned repo never reaches the sum because it
        # is never in `nodes`. The same filters feed stars and public repos.
        api = FakeAPI()
        with mock.patch.object(render, "gql", api):
            render.fetch("t", "me", {})
        core = next(q for q, _ in api.calls if "repositories" in q)
        self.assertIn("forkCount", core)
        self.assertIn("ownerAffiliations: OWNER", core)
        self.assertIn("isFork: false", core)
        self.assertIn("privacy: PUBLIC", core)

    def test_pre_forkcount_cache_is_discarded_and_refetched(self):
        # A cache written before forkCount existed (previous schema version)
        # must not render a made-up 0: the version bump discards it, so the
        # run does a cold refetch (12 monthly calendar windows) and renders
        # the real fetched figure.
        with tempfile.TemporaryDirectory() as td:
            cache_file = Path(td) / "cache.json"
            out = Path(td) / "out"
            stale = {
                "version": render.CACHE_VERSION - 1,
                "fetched_at": "2026-07-20T06:00:00+00:00",
                "user": json.loads(json.dumps(CORE_USER)),
                "calendar_days": recent_days(3),
                "prs": {}, "issues": [],
            }
            del stale["user"]["repositories"]["nodes"][0]["forkCount"]
            cache_file.write_text(json.dumps(stale))

            api = FakeAPI()
            api.full_calendar = recent_days(3)
            run_main(api, out, cache_file)

            calendar_vars = [v for q, v in api.calls
                             if "contributionCalendar" in q]
            self.assertEqual(len(calendar_vars), 12)  # cold backfill, not warm
            svg = (out / "stats.svg").read_text()
            self.assertIn("forks received", svg)
            self.assertIn(">2</text>", svg)  # CORE_USER's forkCount, refetched
            self.assertEqual(render.load_cache(cache_file)["version"],
                             render.CACHE_VERSION)


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


class SvgInputEscaping(unittest.TestCase):
    C = render.THEMES["tokyonight"]
    SVG_NS = "http://www.w3.org/2000/svg"

    def test_stats_login_markup_and_controls_are_text(self):
        user = json.loads(json.dumps(CORE_USER))
        user["login"] = "octo\x01<img src=x>"

        svg = render.render_stats(self.C, user, 0, 0, 0)
        root = ET.fromstring(svg)

        self.assertIn("@octo&lt;img src=x&gt;", svg)
        self.assertIsNone(root.find(f".//{{{self.SVG_NS}}}img"))

    def test_hostile_legend_color_uses_fallback_and_parses(self):
        legend = render.language_legend(
            self.C, [("Python", 100, 100.0, '\"><img src=x>')])
        root = ET.fromstring(render.base_card(
            self.C, 420, 230, legend, card="languages",
            title="Top languages", desc="Top language: Python (100%)."))

        rect = next(
            rect for rect in root.findall(f".//{{{self.SVG_NS}}}rect")
            if rect.attrib.get("x") == "20")
        self.assertEqual(rect.attrib["fill"], "#888888")
        self.assertIsNone(root.find(f".//{{{self.SVG_NS}}}img"))

    def test_hostile_language_bar_color_uses_fallback_and_parses(self):
        svg = render.render_languages(
            self.C, [("Python", 100, 100.0, '\"><img src=x>')])
        root = ET.fromstring(svg)
        rects = root.findall(f".//{{{self.SVG_NS}}}rect")

        self.assertIn("#888888", [rect.attrib.get("fill") for rect in rects])
        self.assertIsNone(root.find(f".//{{{self.SVG_NS}}}img"))

    def test_full_stats_card_strips_controls_from_name(self):
        user = json.loads(json.dumps(CORE_USER))
        user["name"] = "A\x01B\x1f"

        root = ET.fromstring(render.render_stats(self.C, user, 0, 0, 0))
        texts = root.findall(f".//{{{self.SVG_NS}}}text")

        self.assertEqual(texts[0].text, "AB")


if __name__ == "__main__":
    unittest.main()
