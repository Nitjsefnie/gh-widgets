#!/usr/bin/env python3
"""Tests for render-responsiveness.py. Stdlib only, like the thing it tests.

    python3 -m unittest discover -v

No network at all: the renderer now fetches its own PRs, so every end-to-end
case drives a fake `gql` (see fake_gql) and, where the cache matters, a
hand-built cache payload.
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


class TrimmedMean(unittest.TestCase):
    """Turnaround is a p10..p90 trimmed mean; outliers are excluded."""

    FIXTURE = {"a/typical-fast": [0.5, 0.5, 0.5, 0.5, 200.0],  # trim -> 0.5
               "b/steady": [4.0, 4.0, 4.0, 4.0, 4.0]}          # trim -> 4.0

    def rows(self):
        return resp.responsiveness_rows(self.FIXTURE, resp.metric_knobs())[0]

    def test_one_stalled_pr_does_not_sink_an_otherwise_fast_repo(self):
        # Same n for both, so only the turnaround decides. Under a PLAIN MEAN,
        # a/typical-fast (40.4 h) would rank below b/steady (4.0 h).
        self.assertEqual([r.repo for r in self.rows()],
                         ["a/typical-fast", "b/steady"])

    def test_reported_turnaround_is_the_trimmed_mean_of_the_repo(self):
        self.assertAlmostEqual(self.rows()[0].hours, 0.5)

    def test_even_sample_uses_trimmed_mean(self):
        # [1, 2, 4, 100]: p10 = 1.3, p90 = 71.2, included = [2, 4], mean = 3.0.
        rows = resp.responsiveness_rows({"a/x": [1.0, 2.0, 4.0, 100.0]},
                                        resp.metric_knobs())[0]
        self.assertAlmostEqual(rows[0].hours, 3.0)

    def test_large_outlier_is_excluded_from_trimmed_mean(self):
        # Hand-derived on [1..9, 100, 200], n=11:
        # p10 index = 1.0 -> exactly 2; p90 index = 9.0 -> exactly 100.
        # included = [2..9, 100]; sum = 144, mean = 16.0.
        # (median would be 6.0; the 200 outlier is excluded.)
        fixture = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 100.0, 200.0]
        rows = resp.responsiveness_rows({"a/x": fixture},
                                        resp.metric_knobs())[0]
        self.assertAlmostEqual(rows[0].hours, 16.0)

    def test_n1_uses_plain_mean_fallback(self):
        self.assertAlmostEqual(resp.trimmed_mean([5.0]), 5.0)

    def test_n2_uses_plain_mean_fallback(self):
        # n < 4, so the trim is never applied.
        self.assertAlmostEqual(resp.trimmed_mean([1.0, 3.0]), 2.0)

    def test_n3_uses_plain_mean_fallback(self):
        # n < 4, so the trim is never applied even though it would select [2].
        self.assertAlmostEqual(resp.trimmed_mean([1.0, 2.0, 100.0]),
                               103.0 / 3.0)

    def test_trim_boundary_is_inclusive(self):
        # Hand-derived on [0, 10, ..., 100], n=11:
        # p10 index = 1.0 -> exactly 10; p90 index = 9.0 -> exactly 90.
        # Boundary values 10 and 90 must be counted.
        # included = [10..90]; mean = 450/9 = 50.0.
        fixture = [0.0, 10.0, 20.0, 30.0, 40.0, 50.0,
                   60.0, 70.0, 80.0, 90.0, 100.0]
        rows = resp.responsiveness_rows({"a/x": fixture},
                                        resp.metric_knobs())[0]
        self.assertAlmostEqual(rows[0].hours, 50.0)


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


def fake_gql(nodes=(), orgs=("myorg",), error=None):
    """Stand in for common.gql: answers the identity query from `orgs` and
    serves `nodes` as one page of pull requests.

    `error` makes every call raise it, which is how a fetch failure — the
    whole reason the degraded path exists — is simulated.
    """
    def _gql(token, query, variables=None, **kw):
        if error is not None:
            raise error
        if "organizations" in query:
            return {"user": {"login": "me", "databaseId": 1,
                             "organizations": {
                                 "nodes": [{"login": o} for o in orgs]}}}
        return {"user": {"pullRequests": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": list(nodes)}}}
    return _gql


def run_main(cache_file, out_dir, gql_fn=None, nodes=()):
    argv = ["render-responsiveness.py", "--user", "me", "--token", "t",
            "--out-dir", str(out_dir), "--theme", "tokyonight",
            "--cache-file", str(cache_file)]
    with mock.patch.object(sys, "argv", argv), \
            mock.patch.object(resp, "gql", gql_fn or fake_gql(nodes)):
        resp.main()


class RenderCard(unittest.TestCase):
    def render(self, nodes):
        """One end-to-end run against a cold cache: the fetch supplies every
        PR, so the card is drawn from what this script itself just fetched."""
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            run_main(Path(td) / "impact-cache.json", out, nodes=nodes)
            return (out / "responsiveness.svg").read_text()

    def test_card_lists_repo_count_turnaround_and_score(self):
        svg = self.render(prs("a/x", [0.5, 1.0, 1.5]) + prs("b/y", [48.0] * 4))
        self.assertIn("external responsiveness", svg)
        self.assertIn("a/x", svg)
        self.assertIn(">3</text>", svg)        # n
        self.assertIn("1.0 h", svg)            # mean of a/x (n=3 fallback)
        self.assertIn("10.00", svg)            # leader, rescaled
        self.assertIn("2.0 d", svg)            # mean of b/y

    def test_the_card_carries_no_bookkeeping_footer(self):
        """The card is a leaderboard, not a report.

        The floor, the top-N cut and the skipped-PR count are real and are
        printed by main(); they are deliberately absent from the graphic. If
        a future change starts writing them onto the card again, this fails.
        """
        svg = self.render(prs("a/x", [1.0] * 3) + prs("b/y", [1.0] * 2)
                          + prs("c/z", [1.0]) + prs("a/x", [None]))
        for leaked in ("floor", "below n", "excluded", "showing",
                       "no cached merge time", "untimed"):
            self.assertNotIn(leaked, svg)

    def test_a_live_fetch_is_not_stamped(self):
        """The stamp is a caveat, not a label — same rule as the other cards."""
        svg = self.render(prs("a/x", [1.0] * 3))
        self.assertNotIn("cached data from", svg)

    def test_empty_board_still_renders(self):
        svg = self.render(prs("me/mine", [1.0] * 3))
        self.assertIn("no external merged PRs yet", svg)

    def test_only_the_top_rows_are_drawn(self):
        nodes = []
        for i in range(resp.TOP_N + 3):
            nodes += prs(f"o{i}/r", [float(i + 1)] * 3)
        svg = self.render(nodes)
        drawn = re.findall(r'>(o\d+/r)</text>', svg)
        self.assertEqual(len(drawn), resp.TOP_N)


class CacheContract(unittest.TestCase):
    def test_a_missing_cache_is_not_an_error_when_the_fetch_works(self):
        # This script no longer depends on someone else having written a
        # cache: a cold start fetches everything and creates one.
        with tempfile.TemporaryDirectory() as td:
            cache_file = Path(td) / "nope.json"
            out = Path(td) / "out"
            run_main(cache_file, out, nodes=prs("a/x", [1.0] * 3))
            self.assertIn("a/x", (out / "responsiveness.svg").read_text())
            self.assertEqual(len(json.loads(cache_file.read_text())["prs"]), 3)

    def test_a_cache_without_insiders_is_usable(self):
        # It used to be a hard error, because the cached set was the only way
        # to know who is an insider. Identity now comes from the token, so the
        # cached set is not consulted at all on the fetch path.
        with tempfile.TemporaryDirectory() as td:
            cache_file = Path(td) / "impact-cache.json"
            payload = cache_payload(prs("a/x", [1.0] * 3))
            del payload["insiders"]
            cache_file.write_text(json.dumps(payload))
            out = Path(td) / "out"
            run_main(cache_file, out, nodes=prs("someone/theirs", [1.0] * 3))
            self.assertIn("someone/theirs",
                          (out / "responsiveness.svg").read_text())

    def test_insiders_come_from_the_token_not_from_the_cache(self):
        # The insider set decides what counts as external. A cache naming a
        # different account must not be able to promote our own org's repos
        # into the board.
        with tempfile.TemporaryDirectory() as td:
            cache_file = Path(td) / "impact-cache.json"
            cache_file.write_text(json.dumps(
                cache_payload([], insiders=["nobody"])))
            out = Path(td) / "out"
            run_main(cache_file, out,
                     gql_fn=fake_gql(prs("myorg/thing", [1.0] * 3)))
            self.assertIn("no external merged PRs yet",
                          (out / "responsiveness.svg").read_text())

    def test_schema_version_is_pinned_to_render_impacts_cache(self):
        # This renderer SHARES render-impact.py's cache; a version bump there
        # that is not mirrored here would have the two writing incompatible
        # payloads over each other.
        src = Path(__file__).with_name("render-impact.py").read_text(
            encoding="utf-8")
        self.assertIn(f"CACHE_VERSION = {resp.IMPACT_CACHE_VERSION}", src)


OURLOC = {"someone/theirs": {"ours": 4321, "total": 99999,
                             "branch": "main", "head": "deadbeef"}}
ISSUES = [{"state": "CLOSED", "stateReason": "COMPLETED",
           "repository": {"nameWithOwner": "someone/theirs",
                          "isPrivate": False,
                          "owner": {"login": "someone"}}}]
TOTALS = {"someone/theirs": {"issues": 12, "merged_prs": 34,
                             "branch": "main", "head": "deadbeef"}}


def full_cache(nodes):
    """A cache with every section render-impact.py writes, so a test can tell
    whether this script preserved the ones it does not own."""
    return cache_payload(nodes, ourloc=dict(OURLOC), issues=list(ISSUES),
                         totals=dict(TOTALS))


class CacheMerge(unittest.TestCase):
    """The write is a MERGE into a cache render-impact.py also owns.

    Overwriting it instead would destroy `ourloc` — the git-blame result
    render-impact.py produces — and silently render impact.svg wrong until the
    next weekly resync. That is the primary hazard of this script writing at all.
    """

    def run_and_read(self, cached_nodes, fetched_nodes):
        with tempfile.TemporaryDirectory() as td:
            cache_file = Path(td) / "impact-cache.json"
            cache_file.write_text(json.dumps(full_cache(cached_nodes)))
            run_main(cache_file, Path(td) / "out", nodes=fetched_nodes)
            return json.loads(cache_file.read_text())

    def test_the_expensive_sections_survive_the_merge(self):
        after = self.run_and_read(prs("a/x", [1.0] * 3),
                                  prs("b/y", [2.0] * 3))
        self.assertEqual(after.get("ourloc"), OURLOC,
                         "the merge lost ourloc — the git-blame result "
                         "render-impact.py produces — and impact.svg renders "
                         "wrong until the weekly resync")
        self.assertEqual(after.get("issues"), ISSUES)
        self.assertEqual(after.get("totals"), TOTALS)

    def test_the_pr_half_is_replaced_with_what_was_just_fetched(self):
        fetched = prs("b/y", [2.0] * 3)
        after = self.run_and_read(prs("a/x", [1.0] * 3), fetched)
        for node in fetched:
            self.assertIn(node["id"], after["prs"])

    def test_render_impacts_own_fetch_stamp_is_left_alone(self):
        # fetched_at dates the sections this script does not refresh. Moving
        # it hourly would make an `ourloc` from yesterday look minutes old.
        after = self.run_and_read(prs("a/x", [1.0] * 3), prs("a/x", [1.0] * 3))
        self.assertEqual(after.get("fetched_at"),
                         full_cache([])["fetched_at"])

    def test_the_pr_half_gets_its_own_fetch_stamp(self):
        after = self.run_and_read(prs("a/x", [1.0] * 3), prs("a/x", [1.0] * 3))
        self.assertIn("prs_fetched_at", after)
        self.assertIn("fetched_at", after)
        self.assertGreater(resp.parse_ts(after["prs_fetched_at"]),
                           resp.parse_ts(after["fetched_at"]))

    def test_the_schema_version_is_preserved(self):
        after = self.run_and_read(prs("a/x", [1.0] * 3), prs("a/x", [1.0] * 3))
        self.assertEqual(after["version"], resp.IMPACT_CACHE_VERSION)

    def test_the_derived_insider_set_is_written(self):
        after = self.run_and_read([], prs("someone/theirs", [1.0] * 3))
        self.assertEqual(after["insiders"], sorted(INSIDERS))


class AtomicWrite(unittest.TestCase):
    """A cache write that fails midway must leave nothing observable."""

    def test_a_failed_write_leaves_the_cache_intact_and_no_temp_file(self):
        real_write_text = Path.write_text

        def half_written(self, *a, **kw):
            # Simulate the disk filling up mid-write: the temp file exists and
            # holds partial JSON, then the write raises.
            if self.name.endswith(".tmp"):
                self.write_bytes(b'{"prs": {"partial')
                raise OSError("no space left on device")
            return real_write_text(self, *a, **kw)

        with tempfile.TemporaryDirectory() as td:
            cache_file = Path(td) / "impact-cache.json"
            before = json.dumps(full_cache(prs("a/x", [1.0] * 3)))
            cache_file.write_text(before)
            out = Path(td) / "out"
            with mock.patch.object(Path, "write_text", half_written):
                run_main(cache_file, out, nodes=prs("b/y", [2.0] * 3))
            # The cache is exactly what it was: no half-written state is ever
            # visible at the real path, because os.replace never ran.
            self.assertEqual(cache_file.read_text(), before)
            # And no litter: the partial temp file is cleaned up, not left to
            # be mistaken for a cache or to collide with the next write.
            self.assertEqual(
                [f for f in os.listdir(td) if f.endswith(".tmp")], [])

    def test_a_failed_cache_write_still_renders_the_card(self):
        # The SVG is the product; a cache that could not be updated is a
        # warning, not a reason to leave the widget stale.
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            with mock.patch.object(resp.common, "_write_cache",
                                   side_effect=OSError("read-only fs")):
                run_main(Path(td) / "impact-cache.json", out,
                         nodes=prs("a/x", [1.0] * 3))
            self.assertIn("a/x", (out / "responsiveness.svg").read_text())


class DegradedPath(unittest.TestCase):
    """A failed fetch renders from the cache and does not touch it."""

    BOOM = RuntimeError("GraphQL errors: [{'type': 'SERVICE_UNAVAILABLE'}]")

    def test_fetch_failure_renders_from_the_cache(self):
        with tempfile.TemporaryDirectory() as td:
            cache_file = Path(td) / "impact-cache.json"
            cache_file.write_text(json.dumps(
                full_cache(prs("someone/theirs", [1.0] * 3))))
            out = Path(td) / "out"
            run_main(cache_file, out, gql_fn=fake_gql(error=self.BOOM))
            self.assertIn("someone/theirs",
                          (out / "responsiveness.svg").read_text())

    def test_fetch_failure_leaves_the_cache_byte_identical(self):
        # The cache is the only data left on this path; writing a
        # half-fetched PR set over it would throw that away too.
        with tempfile.TemporaryDirectory() as td:
            cache_file = Path(td) / "impact-cache.json"
            cache_file.write_text(json.dumps(
                full_cache(prs("someone/theirs", [1.0] * 3))))
            snapshot = cache_file.read_bytes()
            run_main(cache_file, Path(td) / "out",
                     gql_fn=fake_gql(error=self.BOOM))
            self.assertEqual(cache_file.read_bytes(), snapshot)

    def test_fetch_failure_with_no_cache_raises_the_fetch_error(self):
        # Nothing to render from: exiting non-zero leaves the previous SVG
        # serving, and the message names what actually broke.
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(RuntimeError):
                run_main(Path(td) / "nope.json", Path(td) / "out",
                         gql_fn=fake_gql(error=self.BOOM))

    def test_a_long_outage_is_stamped_on_the_card(self):
        # A card drawn from data this old must say so — the run's stdout line
        # is for the operator, the stamp is for whoever sees the SVG.
        with tempfile.TemporaryDirectory() as td:
            cache_file = Path(td) / "impact-cache.json"
            payload = full_cache(prs("someone/theirs", [1.0] * 3))
            payload["prs_fetched_at"] = "2020-01-01T00:00:00+00:00"
            cache_file.write_text(json.dumps(payload))
            out = Path(td) / "out"
            run_main(cache_file, out, gql_fn=fake_gql(error=self.BOOM))
            self.assertIn("cached data from 2020-01-01T00:00:00+00:00",
                          (out / "responsiveness.svg").read_text())

    def test_a_recent_cache_is_not_stamped_on_the_degraded_path(self):
        # One failed hourly run is a blip, not a caveat worth printing on a
        # public card.
        with tempfile.TemporaryDirectory() as td:
            cache_file = Path(td) / "impact-cache.json"
            payload = full_cache(prs("someone/theirs", [1.0] * 3))
            payload["prs_fetched_at"] = datetime.datetime.now(
                datetime.timezone.utc).isoformat(timespec="seconds")
            cache_file.write_text(json.dumps(payload))
            out = Path(td) / "out"
            run_main(cache_file, out, gql_fn=fake_gql(error=self.BOOM))
            self.assertNotIn("cached data from",
                             (out / "responsiveness.svg").read_text())

    def test_a_cache_without_insiders_degrades_to_the_account(self):
        # render-impact.py makes the same concession on the same path: one
        # render that can over-report externals beats no render at all.
        with tempfile.TemporaryDirectory() as td:
            cache_file = Path(td) / "impact-cache.json"
            payload = cache_payload(prs("me/mine", [1.0] * 3)
                                    + prs("someone/theirs", [1.0] * 3))
            del payload["insiders"]
            cache_file.write_text(json.dumps(payload))
            out = Path(td) / "out"
            run_main(cache_file, out, gql_fn=fake_gql(error=self.BOOM))
            svg = (out / "responsiveness.svg").read_text()
            self.assertIn("someone/theirs", svg)
            self.assertNotIn("me/mine", svg)


if __name__ == "__main__":
    unittest.main()


class CacheStaleness(unittest.TestCase):
    """The cached-data stamp appears only when the cache is actually old."""

    FRESH = "2026-08-03T05:36:20+00:00"

    def at(self, hours):
        return resp.parse_ts(self.FRESH) + datetime.timedelta(hours=hours)

    def test_a_recent_cache_is_not_stale(self):
        self.assertFalse(resp.cache_is_stale(self.FRESH, now=self.at(4)))

    def test_a_day_old_cache_is_stale(self):
        self.assertTrue(resp.cache_is_stale(self.FRESH, now=self.at(25)))

    def test_the_boundary_is_not_stale(self):
        self.assertFalse(resp.cache_is_stale(self.FRESH, now=self.at(24)))

    def test_a_missing_stamp_is_not_stale(self):
        self.assertFalse(resp.cache_is_stale(None))

    def test_an_unparseable_stamp_is_treated_as_stale(self):
        """Unreadable provenance is a reason to warn, not to stay silent."""
        self.assertTrue(resp.cache_is_stale("not-a-date"))
