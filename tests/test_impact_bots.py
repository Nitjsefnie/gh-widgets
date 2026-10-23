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


class BotAPI(CountingAPI):
    """Two bot pages; unknown acquisitions retain the strict shared fixture."""

    def __init__(self):
        super().__init__()
        self.nodes = [item('live')]

    def __call__(self, request, **kwargs):
        payload = json.loads(request.data)
        if "author { __typename }" not in payload['query']:
            return super().__call__(request, **kwargs)
        cursor = payload['variables'].get('cursor')
        self.counts['bots', payload['variables']['name'], cursor] += 1
        nodes = ([{'author': {'__typename': 'Bot'}},
                  {'author': {'__typename': 'User'}}, {'author': None}]
                 if cursor is None else [{'author': {'__typename': 'Bot'}}])
        data = {'repository': {'pullRequests': self.page(
            nodes, 'bot-next' if cursor is None else None)}}
        return contextlib.closing(io.BytesIO(json.dumps({'data': data}).encode()))


class ImpactBots(unittest.TestCase):
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
