#!/usr/bin/env python3
"""Tests for render.py. Stdlib only, like the thing it tests.

    python3 -m unittest discover -v

No network: every case is a hand-built contribution calendar.
"""
import datetime
import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "render", Path(__file__).with_name("render.py"))
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
            ("someone/theirs", "CLOSED", "COMPLETED", False), # external
        )
        # one external issue, and it's completed
        self.assertEqual(
            render.external_issues(issues, "me", ["MyOrg"]), (1, 1))

    def test_org_match_is_case_insensitive(self):
        issues = self.issues(("MYORG/thing", "CLOSED", "COMPLETED", False))
        self.assertEqual(
            render.external_issues(issues, "me", ["myorg"]), (0, 0))

    def test_excludes_private_repos(self):
        issues = self.issues(("someone/secret", "CLOSED", "COMPLETED", True))
        self.assertEqual(render.external_issues(issues, "me", []), (0, 0))

    def test_only_completed_closures_count_as_completed(self):
        # OPEN and NOT_PLANNED issues are opened-but-not-completed; only a
        # CLOSED issue with stateReason COMPLETED is the merged-PR analog.
        issues = self.issues(
            ("a/x", "CLOSED", "COMPLETED", False),
            ("a/y", "CLOSED", "NOT_PLANNED", False),
            ("a/z", "OPEN", None, False),
        )
        self.assertEqual(render.external_issues(issues, "me", []), (3, 1))

    def test_no_external_issues(self):
        self.assertEqual(render.external_issues([], "me", []), (0, 0))


if __name__ == "__main__":
    unittest.main()
