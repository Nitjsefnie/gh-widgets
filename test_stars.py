"""Star-history acquisition, graph geometry, and isolated CLI tests."""
import importlib.util
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('stars', ROOT / 'render-stars.py')
if spec is None or spec.loader is None:
    raise SystemExit('cannot load render-stars.py')
stars = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stars)
NOW = datetime(2026, 10, 13, tzinfo=timezone.utc)
DATES = ['2026-09-04T00:00:00Z', '2026-09-05T00:00:00Z']


def page(dates, total=2, more=False, cursor=None, private=False):
    return {'repository': {'isPrivate': private, 'stargazerCount': total,
                           'nameWithOwner': 'Owner/Repo',
                           'stargazers': {
                               'edges': [{'starredAt': d} for d in dates],
                               'pageInfo': {'hasNextPage': more,
                                            'endCursor': cursor}}}}


class Acquisition(unittest.TestCase):
    def test_pages_all_dates_in_order(self):
        with mock.patch.object(stars, 'gql', side_effect=[
                page(DATES[:1], more=True, cursor='next'), page(DATES[1:])]) as gql:
            history = stars.fetch_stars('secret', 'owner/repo')
        self.assertEqual(history['starred_at'], DATES)
        self.assertEqual(history['total'], 2)
        self.assertEqual(gql.call_args_list[1].args[2]['cursor'], 'next')
        self.assertIn('STARRED_AT', gql.call_args.args[1])
        self.assertNotIn('secret', json.dumps(history))

    def test_empty_public_repo(self):
        with mock.patch.object(stars, 'gql', return_value=page([], total=0)):
            self.assertEqual(stars.fetch_stars('t', 'o/r')['total'], 0)

    def test_private_and_missing_repos_are_refused(self):
        for response in (page([], private=True), {'repository': None}):
            with self.subTest(response=response), mock.patch.object(
                    stars, 'gql', return_value=response):
                with self.assertRaises(stars.RepositoryUnavailableError):
                    stars.fetch_stars('t', 'o/r')

    def test_cursor_failures_do_not_publish_partial_history(self):
        for cursor in (None, ''):
            with mock.patch.object(stars, 'gql', return_value=page(
                    DATES[:1], more=True, cursor=cursor)):
                with self.assertRaises(common_pagination_error()):
                    stars.fetch_stars('t', 'o/r')
        with mock.patch.object(stars, 'gql', return_value=page(
                DATES[:1], more=True, cursor='same')):
            with self.assertRaises(common_pagination_error()):
                stars.fetch_stars('t', 'o/r')

    def test_cap_stops_at_40000_and_retains_current_total(self):
        response = page([DATES[0]] * 100, total=40005, more=True)

        def fetch(_token, _query, variables):
            response['repository']['stargazers']['pageInfo']['endCursor'] = str(
                int(variables['cursor'] or 0) + 1)
            return response
        with mock.patch.object(stars, 'gql', side_effect=fetch) as gql:
            history = stars.fetch_stars('t', 'o/r')
        self.assertEqual(len(history['starred_at']), 40000)
        self.assertEqual(history['total'], 40005)
        self.assertEqual(gql.call_count, 400)

    def test_incomplete_and_malformed_results_fail(self):
        for response in (page(DATES[:1]), page(['nonsense'], total=1),
                         page(['2026-09-04'], total=1),
                         page(DATES, total=1)):
            with self.subTest(response=response), mock.patch.object(
                    stars, 'gql', return_value=response):
                with self.assertRaises(ValueError):
                    stars.fetch_stars('t', 'o/r')

    def test_exhausted_pages_and_oversized_response_fail(self):
        def empty_page(_token, _query, variables):
            return page([], total=1, more=True,
                        cursor=str(int(variables['cursor'] or 0) + 1))
        with mock.patch.object(stars, 'gql', side_effect=empty_page):
            with self.assertRaises(common_pagination_error()):
                stars.fetch_stars('t', 'o/r')
        with mock.patch.object(stars, 'gql', return_value=page(
                [DATES[0]] * 40001, total=40001)):
            with self.assertRaisesRegex(ValueError, 'exceeds'):
                stars.fetch_stars('t', 'o/r')


def common_pagination_error():
    return stars.common.PaginationLimitError


class Graph(unittest.TestCase):
    def render(self, dates=None, total=2, repo='owner/repo', now=NOW):
        dates = DATES if dates is None else dates
        return stars.render_stars(stars.THEMES['tokyonight'], repo,
                                  {'total': total, 'starred_at': dates}, now)

    def test_line_is_cumulative_steps_to_render_time(self):
        root = ET.fromstring(self.render())
        path = root.find('.//{*}path')
        self.assertIsNotNone(path)
        assert path is not None
        commands = path.attrib['d'].split()
        self.assertEqual(commands[:3], ['M', '52.00', '224.00'])
        # First star raises the count to one, second to two; horizontal
        # movement happens before each rise, ending at render time (x=500).
        self.assertEqual(commands[3:7], ['H', '52.00', 'V', '153.00'])
        self.assertEqual(commands[-6:], ['H', '63.49', 'V', '82.00', 'H', '500.00'])
        self.assertIn('2026-09-04', self.render())
        self.assertIn('2026-10-13', self.render())

    def test_duplicate_dates_single_instant_and_empty_are_valid(self):
        for dates in ([], [DATES[0]], [DATES[0]] * 2,
                      ['2026-10-13T00:00:00Z']):
            with self.subTest(dates=dates):
                svg = self.render(dates, len(dates))
                ET.fromstring(svg)
                self.assertNotIn('nan', svg)
                self.assertNotIn('inf', svg)
        self.assertIn('no stars yet', self.render([], 0))

    def test_cap_is_disclosed_and_total_is_separate_from_sample(self):
        svg = self.render([DATES[0]] * 40000, 50000)
        self.assertIn('50,000 stars', svg)
        self.assertIn('first 40,000', svg)
        self.assertIn('current stargazers only', svg)

    def test_all_themes_and_escaped_long_repo_names(self):
        for colors in stars.THEMES.values():
            svg = stars.render_stars(colors, '<owner>/a&' + 'x' * 100,
                                     {'total': 2, 'starred_at': DATES}, NOW)
            ET.fromstring(svg)
            self.assertIn(colors['bg'], svg)
            self.assertNotIn('<owner>', svg)
            self.assertTrue(svg.isascii())


class InputsAndCLI(unittest.TestCase):
    def setUp(self):
        # pylint: disable=consider-using-with
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = self.root / 'stars-cache.json'

    def test_canonical_casing_survives_live_and_cache_fallback_render(self):
        argv = ['render-stars.py', '--repos', 'owner/repo,OWNER/REPO', '--token', 't',
                '--cache-file', str(self.cache), '--out-dir', str(self.root)]
        with mock.patch.object(sys, 'argv', argv), mock.patch.object(
                stars, 'gql', return_value=page(DATES)) as gql:
            stars.main()
        self.assertEqual(gql.call_count, 1)
        cached = json.loads(self.cache.read_text(encoding='utf-8'))
        output = self.root / 'stars-owner-repo.svg'
        for fallback in (False, True):
            if fallback:
                with mock.patch.object(sys, 'argv', argv), mock.patch.object(
                        stars, 'fetch_stars', side_effect=RuntimeError('offline')):
                    stars.main()
            root = ET.fromstring(output.read_text(encoding='utf-8'))
            self.assertEqual(root.findtext('{*}title'), 'Star history for Owner/Repo')
            self.assertTrue((root.findtext('{*}desc') or '').startswith('Owner/Repo:'))
            self.assertIn('Owner/Repo', [node.text for node in root.findall('{*}text')])
        self.assertEqual(list(self.root.glob('stars-*.svg')), [output])
        self.assertEqual(cached['owner/repo']['nameWithOwner'], 'Owner/Repo')

    def test_old_cache_uses_configured_spelling_with_lowercase_key_and_filename(self):
        self.cache.write_text(json.dumps({'version': 1, 'owner/repo': {
            'total': 2, 'starred_at': DATES, 'fetched_at': DATES[0]}}), encoding='utf-8')
        argv = ['render-stars.py', '--repos', 'OWNER/Repo,owner/repo', '--token', 't',
                '--cache-file', str(self.cache), '--out-dir', str(self.root)]
        with mock.patch.object(sys, 'argv', argv), mock.patch.object(
                stars, 'fetch_stars', side_effect=RuntimeError('offline')):
            stars.main()
        root = ET.fromstring((self.root / 'stars-owner-repo.svg').read_text(encoding='utf-8'))
        self.assertEqual(root.findtext('{*}title'), 'Star history for OWNER/Repo')
        self.assertTrue((root.findtext('{*}desc') or '').startswith('OWNER/Repo:'))
        self.assertIn('OWNER/Repo', [node.text for node in root.findall('{*}text')])

    def test_live_fetch_merges_repo_cache_and_fallback_does_not_write(self):
        history = {'total': 2, 'starred_at': DATES}
        with mock.patch.object(stars, 'fetch_stars', return_value=history):
            actual, fallback = stars.load_inputs(self.cache, 't', 'o/r')
            stars.load_inputs(self.cache, 't', 'another/repo')
        self.assertEqual(actual['total'], 2)
        self.assertIsNone(fallback)
        before = self.cache.read_bytes()
        self.assertIn('another/repo', json.loads(before))
        with mock.patch.object(stars, 'fetch_stars', side_effect=RuntimeError('offline')):
            actual, fallback = stars.load_inputs(self.cache, 't', 'o/r')
        self.assertEqual(actual['starred_at'], DATES)
        self.assertIsNotNone(fallback)
        self.assertEqual(self.cache.read_bytes(), before)

    def test_corrupt_cache_and_uncached_repo_fail_with_fetch_error(self):
        for contents in ('bad json', '{"version": 9}',
                         '{"version": 1, "o/r": {"total": 2}}'):
            self.cache.write_text(contents, encoding='utf-8')
            with mock.patch.object(stars, 'fetch_stars', side_effect=RuntimeError('offline')):
                with self.assertRaisesRegex(RuntimeError, 'offline'):
                    stars.load_inputs(self.cache, 't', 'o/r')

    def test_private_repo_never_uses_cached_public_data(self):
        with mock.patch.object(stars, 'fetch_stars', return_value={
                'total': 2, 'starred_at': DATES}):
            stars.load_inputs(self.cache, 't', 'o/r')
        with mock.patch.object(stars, 'fetch_stars', side_effect=stars.RepositoryUnavailableError()):
            with self.assertRaises(stars.RepositoryUnavailableError):
                stars.load_inputs(self.cache, 't', 'o/r')

    def test_main_writes_per_repo_and_stamps_fallback(self):
        argv = ['render-stars.py', '--repos', 'o/r,other/repo', '--token', 't',
                '--cache-file', str(self.cache), '--out-dir', str(self.root)]
        with mock.patch.object(sys, 'argv', argv), mock.patch.object(
                stars, 'fetch_stars', return_value={'total': 2, 'starred_at': DATES}):
            stars.main()
        with mock.patch.object(sys, 'argv', argv), mock.patch.object(
                stars, 'fetch_stars', side_effect=RuntimeError('offline')):
            stars.main()
        for name in ('stars-o-r.svg', 'stars-other-repo.svg'):
            svg = (self.root / name).read_text(encoding='utf-8')
            self.assertIn('cached data from', svg)
            ET.fromstring(svg)
            self.assertNotIn('"t"', self.cache.read_text(encoding='utf-8'))

    def test_config_validation_and_env_token_file(self):
        token_file = self.root / 'token'
        token_file.write_text('from-file\n', encoding='utf-8')
        with mock.patch.dict(os.environ, {'GH_STAR_REPOS': ' o/r, O/R ,a/b ',
                                          'GH_TOKEN_FILE': str(token_file)}, clear=True), \
                mock.patch.object(sys, 'argv', ['render-stars.py']):
            args, token = stars.parse_args()
            self.assertEqual(args.repos, ['o/r', 'a/b'])
            self.assertEqual(token, 'from-file')
        for repos in ('', '../repo', 'o/../../x', 'o/r\\x', 'o/r,<x>/y'):
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                    sys, 'argv', ['render-stars.py', '--repos', repos, '--token', 't']), \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    stars.parse_args()
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                sys, 'argv', ['render-stars.py', '--repos', 'o/r']), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                stars.parse_args()

    def test_repo_filename_collisions_are_refused(self):
        with mock.patch.object(sys, 'argv', [
                'render-stars.py', '--repos', 'o-a/b,o/a-b', '--token', 't']), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                stars.parse_args()

    def test_cli_override_and_cache_env_do_not_use_profile_cache(self):
        with mock.patch.dict(os.environ, {'GH_STAR_REPOS': 'old/repo',
                                          'GH_TOKEN': 'env-token',
                                          'CACHE_FILE': 'profile-cache.json'}, clear=True), \
                mock.patch.object(sys, 'argv', ['render-stars.py', '--repos', 'new/repo']):
            args, token = stars.parse_args()
        self.assertEqual(args.repos, ['new/repo'])
        self.assertEqual(token, 'env-token')
        self.assertEqual(args.cache_file, stars.DEFAULT_CACHE_FILE)
        with mock.patch.dict(os.environ, {'GH_STAR_REPOS': 'o/r', 'GH_TOKEN': 't',
                                          'STARS_CACHE_FILE': 'star-cache.json'}, clear=True), \
                mock.patch.object(sys, 'argv', ['render-stars.py']):
            args, _ = stars.parse_args()
        self.assertEqual(args.cache_file, 'star-cache.json')

    def test_failed_uncached_run_preserves_previous_svg(self):
        output = self.root / 'stars-o-r.svg'
        output.write_text('previous SVG', encoding='utf-8')
        argv = ['render-stars.py', '--repos', 'o/r', '--token', 't',
                '--cache-file', str(self.cache), '--out-dir', str(self.root)]
        with mock.patch.object(sys, 'argv', argv), mock.patch.object(
                stars, 'fetch_stars', side_effect=RuntimeError('offline')):
            with self.assertRaisesRegex(RuntimeError, 'offline'):
                stars.main()
        self.assertEqual(output.read_text(encoding='utf-8'), 'previous SVG')

    def test_bad_cache_dates_and_counts_cannot_stand_in(self):
        for total, dates, stamp in ((True, DATES, DATES[0]),
                                    (-1, [], DATES[0]),
                                    (1, ['invalid'], DATES[0]),
                                    (2, DATES, 'invalid')):
            self.cache.write_text(json.dumps({'version': 1, 'o/r': {
                'total': total, 'starred_at': dates, 'fetched_at': stamp}}), encoding='utf-8')
            with mock.patch.object(stars, 'fetch_stars', side_effect=RuntimeError('offline')):
                with self.assertRaisesRegex(RuntimeError, 'offline'):
                    stars.load_inputs(self.cache, 't', 'o/r')

    def test_help_starts_from_another_directory(self):
        proc = subprocess.run([sys.executable, str(ROOT / 'render-stars.py'), '--help'],
                              cwd=self.root, capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('--repos', proc.stdout)
        self.assertIn('--cache-file', proc.stdout)


if __name__ == '__main__':
    unittest.main()
