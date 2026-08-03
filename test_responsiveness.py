#!/usr/bin/env python3
"""Tests for render-responsiveness.py. Stdlib only, like the thing it tests.

    python3 -m unittest discover -v

No network at all: the renderer reads the impact cache, so every case here is
a hand-built cache payload.
"""
import datetime
import importlib.util
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "responsiveness", Path(__file__).with_name("render-responsiveness.py"))
if spec is None or spec.loader is None:
    raise SystemExit("error: cannot load render-responsiveness.py")
resp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resp)

INSIDERS = frozenset({"me", "myorg"})


def pr(repo, hours, merged=True, private=False, created="2026-01-01T00:00:00Z",
       end_field="mergedAt"):
    """One cached PR node: `hours` from creation to merge (None = no end)."""
    node = {
        "id": f"{repo}#{hours}#{created}",
        "merged": merged,
        "createdAt": created,
        "mergedAt": None,
        "closedAt": None,
        "repository": {"nameWithOwner": repo, "isPrivate": private,
                       "owner": {"login": repo.split("/")[0]}},
    }
    if hours is not None:
        t = (datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
             + datetime.timedelta(hours=hours))
        node[end_field] = t.isoformat()
    return node


def prs(repo, hours_list, **kw):
    """N cached PR nodes for one repo, each with a distinct createdAt so the
    ids stay unique."""
    return [pr(repo, h, created=f"2026-01-{i + 1:02d}T00:00:00Z", **kw)
            for i, h in enumerate(hours_list)]


class Population(unittest.TestCase):
    """Which PRs are even eligible: merged, public, external."""

    def by_repo(self, nodes):
        return resp.turnaround_by_repo(nodes, INSIDERS)[0]

    def test_excludes_own_account_and_org_repos(self):
        nodes = (prs("me/mine", [1, 1, 1]) + prs("MyOrg/thing", [1, 1, 1])
                 + prs("someone/theirs", [1, 1, 1]))
        self.assertEqual(sorted(self.by_repo(nodes)), ["someone/theirs"])

    def test_insider_match_is_case_insensitive(self):
        self.assertEqual(self.by_repo(prs("MYORG/thing", [1, 1, 1])), {})

    def test_excludes_private_repos(self):
        self.assertEqual(self.by_repo(prs("a/secret", [1, 1, 1], private=True)), {})

    def test_excludes_unmerged_prs(self):
        nodes = prs("a/x", [1, 2], merged=False) + prs("a/x", [3])
        self.assertEqual(self.by_repo(nodes), {"a/x": [3.0]})

    def test_closed_at_stands_in_for_a_missing_merged_at(self):
        # For a merged PR, closedAt IS the merge time; the cache can carry a
        # node that was fetched while the PR was still open.
        nodes = prs("a/x", [5], end_field="closedAt")
        self.assertEqual(self.by_repo(nodes), {"a/x": [5.0]})

    def test_prs_without_any_end_timestamp_are_skipped_and_counted(self):
        nodes = prs("a/x", [1, 2]) + prs("a/x", [None])
        by_repo, skipped = resp.turnaround_by_repo(nodes, INSIDERS)
        self.assertEqual(by_repo, {"a/x": [1.0, 2.0]})
        self.assertEqual(skipped, 1)


class Floor(unittest.TestCase):
    """n >= MIN_PRS to be ranked, with the excluded tail disclosed."""

    def rows(self, by_repo):
        return resp.responsiveness_rows(by_repo, resp.metric_knobs())

    def test_repos_below_the_floor_are_not_ranked(self):
        rows, _, _ = self.rows({"a/x": [1.0] * 3, "b/y": [0.01] * 2})
        self.assertEqual([r.repo for r in rows], ["a/x"])

    def test_excluded_repos_and_prs_are_counted_for_the_footer(self):
        rows, n_repos, n_prs = self.rows(
            {"a/x": [1.0] * 3, "b/y": [1.0] * 2, "c/z": [1.0]})
        self.assertEqual(len(rows), 1)
        self.assertEqual((n_repos, n_prs), (2, 3))

    def test_a_single_instant_merge_cannot_top_the_board(self):
        # The trap the floor exists for: one 2-minute merge on a denominator
        # of 1 is not evidence of anything.
        rows, _, _ = self.rows({"fast/one": [0.03], "real/work": [6.0] * 9})
        self.assertEqual([r.repo for r in rows], ["real/work"])


class MedianNotMean(unittest.TestCase):
    """Turnaround is a MEDIAN. A mean ranks a repo by its worst outlier."""

    FIXTURE = {"a/typical-fast": [0.5, 0.5, 0.5, 0.5, 200.0],  # median 0.5, mean 40.4
               "b/steady": [4.0, 4.0, 4.0, 4.0, 4.0]}          # median 4.0, mean 4.0

    def rows(self):
        return resp.responsiveness_rows(self.FIXTURE, resp.metric_knobs())[0]

    def test_one_stalled_pr_does_not_sink_an_otherwise_fast_repo(self):
        # Same n for both, so only the turnaround decides. Under a MEAN,
        # a/typical-fast (40.4 h) would rank below b/steady (4.0 h).
        self.assertEqual([r.repo for r in self.rows()],
                         ["a/typical-fast", "b/steady"])

    def test_reported_turnaround_is_the_median_of_the_repo(self):
        self.assertAlmostEqual(self.rows()[0].hours, 0.5)

    def test_even_sample_medians_average_the_two_middle_values(self):
        rows = resp.responsiveness_rows({"a/x": [1.0, 2.0, 4.0, 100.0]},
                                        resp.metric_knobs())[0]
        self.assertAlmostEqual(rows[0].hours, 3.0)


class Scoring(unittest.TestCase):
    def rows(self, by_repo):
        return resp.responsiveness_rows(by_repo, resp.metric_knobs())[0]

    def test_speed_factor_halves_at_24h_and_quarters_at_72h(self):
        k = resp.metric_knobs()
        self.assertAlmostEqual(resp.speed(0.0, k), 1.0)
        self.assertAlmostEqual(resp.speed(24.0, k), 0.5)
        self.assertAlmostEqual(resp.speed(72.0, k), 0.25)

    def test_volume_has_diminishing_returns(self):
        k = resp.metric_knobs()
        self.assertAlmostEqual(resp.volume(4, k), 2.0)
        self.assertAlmostEqual(resp.volume(36, k), 6.0)

    def test_leader_rescales_to_exactly_ten(self):
        scaled = resp.rescale(self.rows({"a/x": [1.0] * 9, "b/y": [1.0] * 4}))
        self.assertEqual(scaled[0].score, 10.0)

    def test_rescaling_preserves_ratios(self):
        # n 9 vs 4 at the same turnaround: sqrt(9)/sqrt(4) = 1.5
        scaled = resp.rescale(self.rows({"a/x": [1.0] * 9, "b/y": [1.0] * 4}))
        self.assertAlmostEqual(scaled[0].score / scaled[1].score, 1.5)

    def test_volume_and_speed_trade_off_against_each_other(self):
        # 4 PRs merged instantly beat 9 PRs that sit for three days:
        # 2.0*1.0 > 3.0*0.25.
        rows = self.rows({"slow/lots": [72.0] * 9, "fast/few": [0.0] * 4})
        self.assertEqual([r.repo for r in rows], ["fast/few", "slow/lots"])

    def test_ties_break_on_volume_then_repo_name(self):
        rows = self.rows({"z/big": [1.0] * 9, "a/small": [1.0] * 4,
                          "b/small": [1.0] * 4})
        self.assertEqual([r.repo for r in rows], ["z/big", "a/small", "b/small"])


class FormatDuration(unittest.TestCase):
    def test_sub_minute(self):
        self.assertEqual(resp.fmt_duration(0.0), "<1 min")
        self.assertEqual(resp.fmt_duration(0.008), "<1 min")  # 29 s

    def test_minutes(self):
        self.assertEqual(resp.fmt_duration(1 / 60), "1 min")
        self.assertEqual(resp.fmt_duration(0.2), "12 min")
        self.assertEqual(resp.fmt_duration(59 / 60), "59 min")

    def test_the_minute_hour_boundary_never_prints_60_min(self):
        self.assertEqual(resp.fmt_duration(59.7 / 60), "1.0 h")
        self.assertEqual(resp.fmt_duration(1.0), "1.0 h")

    def test_hours(self):
        self.assertEqual(resp.fmt_duration(3.14), "3.1 h")
        self.assertEqual(resp.fmt_duration(23.9), "23.9 h")

    def test_the_hour_day_boundary_never_prints_24_h(self):
        self.assertEqual(resp.fmt_duration(23.99), "1.0 d")
        self.assertEqual(resp.fmt_duration(24.0), "1.0 d")

    def test_days(self):
        self.assertEqual(resp.fmt_duration(57.6), "2.4 d")
        self.assertEqual(resp.fmt_duration(24 * 77), "77.0 d")


def cache_payload(nodes, **over):
    payload = {
        "version": resp.IMPACT_CACHE_VERSION,
        "fetched_at": "2026-08-03T05:36:20+00:00",
        "insiders": sorted(INSIDERS),
        "prs": {n["id"]: n for n in nodes},
    }
    payload.update(over)
    return payload


def run_main(cache_file, out_dir):
    argv = ["render-responsiveness.py", "--out-dir", str(out_dir),
            "--theme", "tokyonight", "--cache-file", str(cache_file)]
    with mock.patch.object(sys, "argv", argv):
        resp.main()


class RenderCard(unittest.TestCase):
    def render(self, nodes, **over):
        with tempfile.TemporaryDirectory() as td:
            cache_file = Path(td) / "impact-cache.json"
            cache_file.write_text(json.dumps(cache_payload(nodes, **over)))
            out = Path(td) / "out"
            run_main(cache_file, out)
            return (out / "responsiveness.svg").read_text()

    def test_card_lists_repo_count_turnaround_and_score(self):
        svg = self.render(prs("a/x", [0.5, 1.0, 1.5]) + prs("b/y", [48.0] * 4))
        self.assertIn("external responsiveness", svg)
        self.assertIn("a/x", svg)
        self.assertIn(">3</text>", svg)        # n
        self.assertIn("1.0 h", svg)            # median of a/x
        self.assertIn("10.00", svg)            # leader, rescaled
        self.assertIn("2.0 d", svg)            # median of b/y

    def test_floor_exclusions_are_disclosed_on_the_card(self):
        svg = self.render(prs("a/x", [1.0] * 3) + prs("b/y", [1.0] * 2)
                          + prs("c/z", [1.0]))
        self.assertIn("2 repos (3 merged PRs) below the n ≥ 3 floor", svg)

    def test_skipped_prs_are_disclosed_on_the_card(self):
        svg = self.render(prs("a/x", [1.0] * 3) + prs("a/x", [None]))
        self.assertIn("1 merged PR with no cached merge time", svg)

    def test_no_skip_line_when_every_pr_has_timestamps(self):
        svg = self.render(prs("a/x", [1.0] * 3))
        self.assertNotIn("no cached merge time", svg)

    def test_cache_fetch_time_is_stamped(self):
        svg = self.render(prs("a/x", [1.0] * 3))
        self.assertIn("cached data from 2026-08-03T05:36:20+00:00", svg)

    def test_empty_board_still_renders(self):
        svg = self.render(prs("me/mine", [1.0] * 3))
        self.assertIn("no external merged PRs yet", svg)

    def test_only_the_top_rows_are_drawn_and_the_rest_disclosed(self):
        nodes = []
        for i in range(resp.TOP_N + 3):
            nodes += prs(f"o{i}/r", [float(i + 1)] * 3)
        svg = self.render(nodes)
        drawn = re.findall(r'>(o\d+/r)</text>', svg)
        self.assertEqual(len(drawn), resp.TOP_N)
        self.assertIn(f"showing {resp.TOP_N} of {resp.TOP_N + 3} ranked repos", svg)


class CacheContract(unittest.TestCase):
    def test_missing_cache_is_a_hard_error(self):
        # Nothing to render from and no way to fetch: exiting non-zero leaves
        # the previous SVG serving, same contract as the other renderers.
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit):
                run_main(Path(td) / "nope.json", Path(td) / "out")

    def test_cache_without_insiders_is_a_hard_error(self):
        # Guessing "just the account" would silently count our own org's
        # repos as external, and this renderer has no --user to guess from.
        with tempfile.TemporaryDirectory() as td:
            cache_file = Path(td) / "impact-cache.json"
            payload = cache_payload(prs("a/x", [1.0] * 3))
            del payload["insiders"]
            cache_file.write_text(json.dumps(payload))
            with self.assertRaises(SystemExit):
                run_main(cache_file, Path(td) / "out")

    def test_schema_version_is_pinned_to_render_impacts_cache(self):
        # This renderer READS render-impact.py's cache; a version bump there
        # that is not mirrored here would silently render nothing.
        src = Path(__file__).with_name("render-impact.py").read_text(
            encoding="utf-8")
        self.assertIn(f"CACHE_VERSION = {resp.IMPACT_CACHE_VERSION}", src)


if __name__ == "__main__":
    unittest.main()
