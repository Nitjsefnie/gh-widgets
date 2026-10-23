"""Independent acquisitions overlap; dependent cursor pages stay ordered."""
import contextlib
import io
import json
import os
import re
import statistics
import tempfile
import threading
import time
import unittest
from concurrent import futures
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.test_share import CountingAPI, profile, impact, item
from tests import test_render


class DelayedAPI(CountingAPI):
    """Count real client attempts with fixed transport latency."""

    def __init__(self):
        super().__init__()
        self.lock = threading.Lock()
        self.active = self.peak = 0
        self.durations = []

    def __call__(self, request, **kwargs):
        start = time.monotonic()
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(.08)
            return super().__call__(request, **kwargs)
        finally:
            with self.lock:
                self.active -= 1
                self.durations.append((json.loads(request.data)['query'], time.monotonic() - start))


class AcquisitionOverlap(unittest.TestCase):
    def test_profile_independent_walks_overlap_without_adding_requests(self):
        # Org/repo/extra-org walks, calendar and authored PR/issues are independent.
        api = DelayedAPI()
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.dict(os.environ, {"GH_WIDGETS_SHARE_DIR": "", "GH_EXTRA_ORGS": ""}), \
                mock.patch("urllib.request.urlopen", api), \
                mock.patch.object(profile.common, "DEBUG_TIMING", True):
            recorder = profile.common.TimingRecorder()
            with mock.patch.object(profile.common, "timing", recorder):
                start = time.monotonic()
                user, prs, issues, fallback = profile.load_inputs(
                    SimpleNamespace(user="me", resync=False), "t", Path(td) / "cache.json")
                elapsed = time.monotonic() - start
            text = io.StringIO()
            with contextlib.redirect_stdout(text):
                recorder.report("profile")
        # 2 profile requests + 12 sequential calendar windows + 2 PR + 2 issue pages.
        self.assertEqual(sum(api.counts.values()), 18)
        self.assertEqual(len(prs), 4)
        self.assertEqual(len(issues), 2)
        self.assertEqual(user["login"], "me")
        self.assertIsNone(fallback)
        self.assertEqual(api.peak, 2)
        # Use observed stub latency: sleep(.08) can overshoot on hosted runners.
        # Calendar's 12 windows follow the parallel profile walks; four typical
        # requests allow for cap contention, still rejecting sequential acquisition.
        calendar = sum(duration for query, duration in api.durations
                       if 'contributionCalendar' in query)
        profile_walk = max(duration for query, duration in api.durations
                           if 'organizations' in query or 'repositories' in query)
        self.assertLess(elapsed, calendar + profile_walk
                        + 4 * statistics.median(duration for _, duration in api.durations))
        line = next(line for line in text.getvalue().splitlines() if 'graphql /graphql:' in line)
        self.assertIn('count=18', line)
        concurrent = re.search(r'concurrent=([\d.]+)s', line)
        assert concurrent is not None
        self.assertGreater(float(concurrent[1]), 0)

    def test_totals_groups_overlap_keep_union_count_and_pacing(self):
        repos = [f"other/r{i}" for i in range(31)]
        api = DelayedAPI()
        with mock.patch("urllib.request.urlopen", api), \
                mock.patch.object(impact.common, "DEBUG_TIMING", True), \
                mock.patch.object(impact.common, "timing_sleep") as pacing:
            recorder = impact.common.TimingRecorder()
            with mock.patch.object(impact.common, "timing", recorder):
                start = time.monotonic()
                totals = impact.fetch_repo_totals("t", repos)
                elapsed = time.monotonic() - start
            text = io.StringIO()
            with contextlib.redirect_stdout(text):
                recorder.report("impact")
        self.assertEqual(set(totals), set(repos))
        self.assertEqual(sum(api.counts.values()), 3)
        # Two dependent rounds plus scheduling slack; reject three serial calls.
        typical = statistics.median(duration for _, duration in api.durations)
        self.assertLess(elapsed, 2.875 * typical)
        self.assertEqual(pacing.call_args_list, [mock.call(.3, 'pacing')] * 3)
        line = next(line for line in text.getvalue().splitlines() if 'graphql /graphql:' in line)
        self.assertIn('count=3', line)
        concurrent = re.search(r'concurrent=([\d.]+)s', line)
        assert concurrent is not None
        self.assertGreater(float(concurrent[1]), 0)

    def test_totals_cover_the_full_shared_pr_and_issue_union(self):
        nodes = []
        for i in range(31):
            node = item(str(i), issue=i > 14)
            node['repository']['nameWithOwner'] = f'other/r{i}'
            nodes.append(node)
        shared = {'identity': {'login': 'me', 'databaseId': 1, 'orgs': [],
                               'insiders': ['me'], 'emails': []},
                  'prs': nodes[:15], 'issues': nodes[15:]}
        with mock.patch.object(impact.common.share, 'load', return_value=shared), \
                mock.patch('urllib.request.urlopen', CountingAPI()), \
                mock.patch.object(impact.common, 'timing_sleep'), \
                mock.patch.object(impact, 'update_loc', return_value={}):
            *_, totals, _loc = impact.fetch_all('t', 'me', {}, False)
        self.assertEqual(set(totals), {f'other/r{i}' for i in range(31)})

    def test_process_cap_applies_across_independent_callers(self):
        api = DelayedAPI()
        with mock.patch("urllib.request.urlopen", api), futures.ThreadPoolExecutor(6) as pool:
            list(pool.map(lambda _: profile.common.gql("t", profile.ORG_QUERY,
                                                       {"login": "me"}), range(6)))
        self.assertEqual(api.peak, 2)
        self.assertEqual(sum(api.counts.values()), 6)

    def test_overlapped_failure_names_the_actual_profile_acquisition(self):
        for phase in ("fetch", "fetch_pull_requests", "fetch_issues"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as td, \
                    mock.patch.dict(os.environ, {"GH_WIDGETS_SHARE_DIR": ""}), \
                    mock.patch.object(profile, "gql", test_render.FakeAPI()), \
                    mock.patch.object(profile, phase, side_effect=RuntimeError("forced")):
                path = Path(td) / "cache.json"
                path.write_text(json.dumps(test_render.complete_cache()), encoding="utf-8")
                *_, fallback = profile.load_inputs(
                    SimpleNamespace(user="me", resync=False), "t", path)
                self.assertEqual(fallback.phase, phase)
