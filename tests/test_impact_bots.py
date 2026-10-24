"""Bot authors affect only the PR denominator, including stale-cache renders."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_share import CountingAPI, impact, item

VARIABLE = "GH_WIDGETS_IMPACT_COUNT_BOT_PRS"
WATERLINE = '2026-10-22T00:00:00Z'
NEWEST = '2026-10-23T00:00:00Z'


class BotAPI(CountingAPI):
    """Two bot pages; unknown acquisitions retain the strict shared fixture."""

    def __init__(self):
        super().__init__()
        self.nodes = [item('live')]
        self.bot_pages = [
            [{'author': {'__typename': 'Bot'}, 'mergedAt': WATERLINE},
             {'author': {'__typename': 'User'}, 'mergedAt': NEWEST},
             {'author': None, 'mergedAt': WATERLINE}],
            [{'author': {'__typename': 'Bot'}, 'mergedAt': WATERLINE}]]
        self.search_nodes = []

    def __call__(self, request, **kwargs):
        payload = json.loads(request.data)
        if "author { __typename }" not in payload['query']:
            return super().__call__(request, **kwargs)
        if 'mergedAt' not in payload['query'] or 'first: 100' not in payload['query']:
            raise AssertionError(f'unmodelled bot page: {payload["query"]}')
        variables = payload['variables']
        cursor = variables.get('cursor')
        if 'search(' in payload['query']:
            terms = variables['query'].split()
            if terms[:3] != ['repo:other/repo', 'is:pr', 'is:merged'] \
                    or 'type: ISSUE' not in payload['query']:
                raise AssertionError(f'unmodelled bot search: {terms}')
            qualifier = terms[3].removeprefix('merged:>=')
            if not qualifier.endswith('+0000'):
                raise AssertionError(f'unmodelled timestamp: {qualifier}')
            # Search models merged time only: creation order must not affect it.
            # A null mergedAt is served too: GitHub answers this query with it.
            nodes = [n for n in self.search_nodes
                     if n['mergedAt'] is None
                     or n['mergedAt'] >= qualifier.replace('+0000', 'Z')]
            offset = int(cursor or 0)
            following = str(offset + 100) if offset + 100 < len(nodes) else None
            data = {'search': self.page(nodes[offset:offset + 100], following)}
            kind = 'bot-search'
        else:
            nodes = self.bot_pages[0 if cursor is None else 1]
            data = {'repository': {'pullRequests': self.page(
                nodes, 'bot-next' if cursor is None else None)}}
            kind = 'bots'
        self.counts[kind, 'repo', cursor] += 1
        return contextlib.closing(io.BytesIO(json.dumps({'data': data}).encode()))


class ImpactBots(unittest.TestCase):
    def acquire(self, fresh, saved, api, *, resync=False, flag=''):
        with mock.patch.dict(os.environ, {VARIABLE: flag, 'GH_WIDGETS_SHARE_DIR': ''}), \
                mock.patch('urllib.request.urlopen', api), \
                mock.patch.object(impact, 'update_loc', return_value={}), \
                mock.patch.object(impact.common, 'timing_sleep'):
            if fresh != 10:
                with mock.patch.object(impact, 'fetch_repo_totals', return_value={
                        'other/repo': {'merged_prs': fresh, 'issues': 10}}):
                    return impact.fetch_all('t', 'me', {'totals': saved}, resync)[4]
            return impact.fetch_all('t', 'me', {'totals': saved}, resync)[4]

    def test_warm_counts_scale_with_new_merges_including_old_created_pr(self):
        saved = {'other/repo': {'merged_prs': 10, 'bot_merged_prs': 2,
                                'bot_waterline': WATERLINE}}
        for k, searches, bots in ((0, 0, 2), (1, 1, 3), (101, 2, 53)):
            api = BotAPI()
            api.search_nodes = [
                {'createdAt': '2020-01-01T00:00:00Z', 'mergedAt': NEWEST,
                 'author': {'__typename': 'Bot' if i % 2 == 0 else 'User'}}
                for i in range(k)]
            api.search_nodes += [
                {'createdAt': '2020-01-01T00:00:00Z', 'mergedAt': WATERLINE,
                 'author': {'__typename': 'Bot'}},
                {'mergedAt': '2026-10-21T00:00:00Z', 'author': None}]
            with self.subTest(k=k):
                totals = self.acquire(10 + k, saved, api)['other/repo']
                self.assertEqual(totals['bot_merged_prs'], bots)
                self.assertEqual(totals['bot_waterline'], NEWEST if k else WATERLINE)
                self.assertEqual(sum(n for (kind, _, _), n in api.counts.items()
                                     if kind == 'bot-search'), searches)
                self.assertEqual(sum(n for (kind, _, _), n in api.counts.items()
                                     if kind == 'bots'), 0)
        self.assertEqual(saved['other/repo']['bot_waterline'], WATERLINE)

    def test_full_walk_counts_a_null_merged_at_node(self):
        # PullRequest.mergedAt is nullable; a null node is still a merge, so
        # the count must include it and the waterline must not take the null.
        data = {'repository': {'pullRequests': {'nodes': [
            {'mergedAt': WATERLINE, 'author': {'__typename': 'Bot'}},
            {'mergedAt': None, 'author': {'__typename': 'Bot'}},
            {'mergedAt': NEWEST, 'author': None},
        ], 'pageInfo': {'hasNextPage': False}}}}
        totals = {'other/repo': {'merged_prs': 10}}
        with mock.patch.object(impact, 'gql', return_value=data):
            impact.fetch_bot_pr_counts('t', totals)
        self.assertEqual(totals['other/repo']['bot_merged_prs'], 2)
        self.assertEqual(totals['other/repo']['bot_waterline'], NEWEST)

    def test_search_skips_a_null_merged_at_node_and_recounts(self):
        # The search cannot tell whether the null node is new, so it must not
        # count it: the kept-count mismatch then falls back to the full walk.
        api = BotAPI()
        api.search_nodes = [{'mergedAt': None, 'author': {'__typename': 'Bot'}}]
        saved = {'other/repo': {'merged_prs': 9, 'bot_merged_prs': 2,
                                'bot_waterline': WATERLINE}}
        totals = self.acquire(10, saved, api)['other/repo']
        self.assertEqual(totals['bot_merged_prs'], 2)
        self.assertEqual(totals['bot_waterline'], NEWEST)
        self.assertEqual(sum(n for (kind, _, _), n in api.counts.items()
                             if kind == 'bot-search'), 1)
        self.assertEqual(sum(n for (kind, _, _), n in api.counts.items()
                             if kind == 'bots'), 2)

    def test_repository_without_merged_prs_costs_no_bot_request(self):
        api = BotAPI()
        api.bot_pages = [[], []]
        totals = self.acquire(0, {'other/repo': {'merged_prs': 0, 'bot_merged_prs': 0}},
                              api)['other/repo']
        self.assertEqual(totals['bot_merged_prs'], 0)
        self.assertNotIn('bot_waterline', totals)
        self.assertFalse([k for k in api.counts if k[0] in ('bots', 'bot-search')])

    def test_recounts_for_migration_drop_resync_and_search_mismatch(self):
        for saved, fresh, resync, searches, new_merges in (
                ({}, 10, False, 0, 0),
                ({'merged_prs': 10, 'bot_merged_prs': 9}, 10, False, 0, 0),
                ({'merged_prs': 10, 'bot_waterline': WATERLINE}, 10, False, 0, 0),
                ({'merged_prs': 11, 'bot_merged_prs': 9,
                  'bot_waterline': WATERLINE}, 10, False, 0, 0),
                ({'merged_prs': 10, 'bot_merged_prs': 9,
                  'bot_waterline': WATERLINE}, 10, True, 0, 0),
                ({'merged_prs': 9, 'bot_merged_prs': 9,
                  'bot_waterline': WATERLINE}, 10, False, 1, 0),
                ({'merged_prs': 9, 'bot_merged_prs': 1,
                  'bot_waterline': WATERLINE}, 10, False, 1, 2)):
            api = BotAPI()
            if new_merges:
                api.search_nodes = [
                    {'mergedAt': NEWEST, 'author': {'__typename': 'Bot'}}
                    for _ in range(new_merges)] + [
                        {'mergedAt': WATERLINE, 'author': {'__typename': 'Bot'}},
                        {'mergedAt': '2026-10-21T00:00:00Z', 'author': None}]
            with self.subTest(saved=saved, resync=resync):
                totals = self.acquire(fresh, {'other/repo': saved}, api,
                                      resync=resync)['other/repo']
                self.assertEqual((totals['bot_merged_prs'], totals['bot_waterline']),
                                 (2, NEWEST))
                self.assertEqual(sum(n for (kind, _, _), n in api.counts.items()
                                     if kind == 'bots'), 2)
                self.assertEqual(sum(n for (kind, _, _), n in api.counts.items()
                                     if kind == 'bot-search'), searches)

    def test_opt_in_skips_all_bot_requests_and_renders_raw_total(self):
        api = BotAPI()
        totals = self.acquire(10, {}, api, flag='1')
        self.assertNotIn('bot_merged_prs', totals['other/repo'])
        self.assertGreater(sum(api.counts.values()), 0)
        self.assertEqual(sum(n for (kind, _, _), n in api.counts.items()
                             if kind in ('bots', 'bot-search')), 0)
        with mock.patch.dict(os.environ, {VARIABLE: '1'}):
            rows = impact.pr_table([item('p')], totals, frozenset(), impact.metric_knobs())
            svg = impact.render_impact(impact.THEMES['tokyonight'], rows, [], [])
        self.assertEqual(rows[0].total, 10)
        self.assertIn('>10</text>', svg)

    def test_search_stall_publishes_neither_count_nor_waterline(self):
        prior = {'other/repo': {'merged_prs': 9, 'bot_merged_prs': 2,
                                'bot_waterline': WATERLINE}}
        for info in ({'hasNextPage': True, 'endCursor': None},
                     {'hasNextPage': True, 'endCursor': 'repeated'},
                     {'hasNextPage': 'yes', 'endCursor': 'next'}):
            data = {'search': {'nodes': [
                {'mergedAt': NEWEST, 'author': {'__typename': 'Bot'}}], 'pageInfo': info}}
            totals = {'other/repo': {'merged_prs': 10}}
            with self.subTest(info=info), mock.patch.object(impact, 'gql', return_value=data), \
                    self.assertRaisesRegex(impact.common.PaginationLimitError, 'bot PR'):
                impact.fetch_bot_pr_counts('t', totals, prior)
            self.assertEqual(totals, {'other/repo': {'merged_prs': 10}})

    def test_waterline_validation_and_old_cache_loading(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'cache.json'
            for waterline in (None, WATERLINE, '', 1, False):
                entry: dict[str, object] = {
                    'merged_prs': 10, 'issues': 20, 'bot_merged_prs': 2}
                if waterline is not None:
                    entry['bot_waterline'] = waterline
                cache = {'version': 1, 'totals': {'other/repo': entry}}
                path.write_text(json.dumps(cache), encoding='utf-8')
                with self.subTest(waterline=waterline):
                    if waterline in (None, WATERLINE):
                        self.assertEqual(impact.load_cache(path), cache)
                    else:
                        with self.assertRaises(impact.common.CacheShapeError):
                            impact.load_cache(path)

    def test_bot_count_cache_validation_rejects_invalid_counts(self):
        for bots in (-1, True, '2', 11):
            with self.subTest(bots=bots), self.assertRaises(impact.common.CacheShapeError):
                impact.common.validate_cache_shape({'totals': {'other/repo': {
                    'merged_prs': 10, 'issues': 20, 'bot_merged_prs': bots}}})

    def test_bot_pagination_fails_instead_of_publishing_partial_counts(self):
        for info in ({'hasNextPage': True, 'endCursor': None},
                     {'hasNextPage': True, 'endCursor': 'repeated'},
                     {'hasNextPage': 'yes', 'endCursor': 'next'}):
            data = {'repository': {'pullRequests': {'nodes': [], 'pageInfo': info}}}
            totals = {'other/repo': {'merged_prs': 10}}
            with self.subTest(info=info), mock.patch.object(impact, 'gql', return_value=data), \
                    self.assertRaisesRegex(impact.common.PaginationLimitError, 'bot PR'):
                impact.fetch_bot_pr_counts('t', totals)
            self.assertNotIn('bot_merged_prs', totals['other/repo'])

    def test_old_cache_needs_bot_counts_before_default_fallback(self):
        cache = {'fetched_at': 'now', 'prs': {}, 'issues': [], 'ourloc': {},
                 'totals': {'other/repo': {'merged_prs': 10, 'issues': 20}}}
        with mock.patch.dict(os.environ, {VARIABLE: ''}):
            self.assertFalse(impact.cache_complete(cache))
        with mock.patch.dict(os.environ, {VARIABLE: '1'}):
            self.assertTrue(impact.cache_complete(cache))

    def test_live_and_cached_fallback_use_the_same_switch(self):
        api = BotAPI()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cache = root / 'impact-cache.json'
            argv = ['render-impact.py', '--user', 'me', '--token', 't',
                    '--out-dir', td, '--cache-file', str(cache)]
            env = {VARIABLE: '', 'GH_WIDGETS_SHARE_DIR': ''}
            output = io.StringIO()
            with mock.patch.dict(os.environ, env), mock.patch.object(sys, 'argv', argv), \
                    mock.patch('urllib.request.urlopen', api), \
                    mock.patch.object(impact, 'update_loc', return_value={}), \
                    mock.patch.object(impact.common, 'timing_sleep'), \
                    mock.patch.object(impact.common, 'DEBUG_TIMING', True), \
                    contextlib.redirect_stdout(output):
                impact.main()
                live = (root / 'impact.svg').read_text(encoding='utf-8')
                saved = json.loads(cache.read_text(encoding='utf-8'))
                self.assertEqual(saved['totals']['other/repo']['merged_prs'], 10)
                self.assertEqual(saved['totals']['other/repo']['bot_merged_prs'], 2)
                self.assertEqual(sum(n for (kind, _, _), n in api.counts.items()
                                     if kind == 'bots'), 2)
                line = next(line for line in output.getvalue().splitlines()
                            if 'graphql /graphql:' in line)
                self.assertIn(f'count={sum(api.counts.values())}', line)
                for value in ('', '0', '1'):
                    with mock.patch.dict(os.environ, {VARIABLE: value}), \
                            mock.patch.object(impact, 'fetch_all', side_effect=RuntimeError('offline')):
                        impact.main()
                        stale = (root / 'impact.svg').read_text(encoding='utf-8')
                    expected = 10 if value == '1' else 8
                    self.assertIn(f'>{expected}</text>', stale)
                    if value != '1':
                        self.assertEqual(live.split('Pull Requests')[1].split('Issues')[0],
                                         stale.split('Pull Requests')[1].split('Issues')[0])

    def test_switch_changes_share_and_score_but_neither_other_table(self):
        prs = [item('p')]
        issues = [item('i', issue=True)]
        totals = {'other/repo': {'merged_prs': 10, 'bot_merged_prs': 2, 'issues': 20}}
        ourloc = {'other/repo': {'ours': 2, 'total': 30}}
        knobs = impact.metric_knobs()
        with mock.patch.dict(os.environ, {}, clear=True):
            rows = impact.pr_table(prs, totals, frozenset(), knobs)
            issue_rows = impact.issue_table(issues, totals, frozenset(), knobs)
            loc_rows = impact.loc_table(ourloc, knobs)
        self.assertEqual((rows[0].total, rows[0].share), (8, 12.5))
        for value in ('', '0', '1'):
            with mock.patch.dict(os.environ, {VARIABLE: value}):
                actual = impact.pr_table(prs, totals, frozenset(), knobs)
                self.assertEqual(actual[0].total, 10 if value == '1' else 8)
                self.assertEqual(impact.issue_table(issues, totals, frozenset(), knobs), issue_rows)
                self.assertEqual(impact.loc_table(ourloc, knobs), loc_rows)
                if value == '1':
                    self.assertLess(actual[0].score, rows[0].score)
        for value in ('true', '2', ' 1'):
            with mock.patch.dict(os.environ, {VARIABLE: value}), \
                    mock.patch.object(sys, 'argv', ['render-impact.py', '--user', 'me',
                                                    '--token', 't']), \
                    mock.patch.object(impact, 'fetch_all', side_effect=AssertionError('network')), \
                    self.assertRaisesRegex(SystemExit, VARIABLE):
                impact.main()
