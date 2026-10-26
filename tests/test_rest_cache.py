#!/usr/bin/env python3
"""Conditional-GET store tests: per-URL ETag reuse, counts, and degrades.

    python3 -m unittest discover -v

No network; the only files touched live in a TemporaryDirectory.
"""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest import mock

from tests import _ghw_temp

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'popularity_rest_cache', ROOT / 'src/render-popularity.py')
if spec is None or spec.loader is None:
    raise SystemExit('cannot load render-popularity.py')
popularity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(popularity)
common = popularity.common
ghwidgets_cache = common.ghwidgets_cache

USER = {'login': 'Owner', 'id': 42, 'name': 'Owner Name'}
API = 'https://api.github.com'


def repo(name='Owner/Repo'):
    return {'full_name': name, 'visibility': 'public', 'fork': False,
            'archived': False, 'clone_url': f'https://github.com/{name}.git',
            'default_branch': 'trunk', 'stargazers_count': 2, 'forks_count': 3}


def response(data=None, link='', status=200, etag=None, raw=None):
    result = mock.MagicMock()
    result.__enter__.return_value = result
    result.status = status
    result.read.return_value = (raw if raw is not None
                                else json.dumps(data).encode())
    headers = {'Link': link}
    if etag is not None:
        headers['ETag'] = etag
    result.headers = headers
    return result


def not_modified(url):
    return urllib.error.HTTPError(url, 304, 'Not Modified', Message(), None)


def sent_headers(request):
    """What urllib actually puts on the wire: both header maps merged."""
    merged = dict(request.headers)
    merged.update(request.unredirected_hdrs)
    return merged


class FakeGitHub:
    """One ETag per URL: an equal If-None-Match draws a 304, else a 200.

    Bodies and links are keyed by the path part of the URL; the ETag is the
    URL in quotes, so each case can assert the exact header a request sent.
    """

    def __init__(self, bodies, links=None):
        self.bodies = bodies
        self.links = links or {}
        self.etags = {}
        self.requests = []
        self.headers = []

    def __call__(self, request, **_kwargs):
        url = request.full_url
        path = url.removeprefix(API)
        match = sent_headers(request).get('If-none-match')
        self.requests.append((path, match))
        self.headers.append(sent_headers(request))
        if self.etags.get(url) is not None and match == self.etags[url]:
            raise not_modified(url)
        self.etags[url] = f'"{url}"'
        result = mock.MagicMock()
        result.__enter__.return_value = result
        result.status = 200
        result.read.return_value = json.dumps(self.bodies[path]).encode()
        result.headers = {'Link': self.links.get(path, ''),
                          'ETag': self.etags[url]}
        return result


class StoreContract(unittest.TestCase):
    """RestCache entries, conditional_get, and the save degrade contract."""

    def setUp(self):
        # pylint: disable=consider-using-with
        self.directory = _ghw_temp.tempdir(prefix='ghw-rest-cache-')
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'rest-cache.json'

    def fetch(self, served):
        """Drive popularity.rest with a recorded urlopen double."""
        served = iter(served)
        seen = []

        def urlopen(request, **_kwargs):
            seen.append(sent_headers(request).get('If-none-match'))
            return next(served)

        def drive(store):
            with mock.patch.object(popularity.urllib.request, 'urlopen', urlopen):
                result = popularity.rest('t', '/user', store=store)
            return result

        return drive, seen

    def test_a_stored_etag_is_sent_and_a_304_reuses_body_and_link(self):
        drive, seen = self.fetch([response(USER, etag='"etag-1"'),
                                  not_modified(API + '/user'),
                                  response(USER, etag='"etag-1"')])
        store = ghwidgets_cache.RestCache(self.path)
        self.assertEqual(drive(store), (USER, ''))
        store.save()
        self.assertEqual(store.requests, 1)
        self.assertEqual(store.not_modified, 0)
        on_disk = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(on_disk['version'], 1)
        self.assertEqual(on_disk[API + '/user'], {
            'etag': '"etag-1"', 'body': json.dumps(USER), 'link': ''})

        warmed = ghwidgets_cache.RestCache(self.path)
        self.assertEqual(warmed.entry(API + '/user')['etag'], '"etag-1"')
        with mock.patch.object(popularity.time, 'sleep',
                               side_effect=AssertionError('a 304 is not retried')):
            self.assertEqual(drive(warmed), (USER, ''))
        self.assertEqual(seen, [None, '"etag-1"'])
        self.assertEqual(warmed.requests, 1)
        self.assertEqual(warmed.not_modified, 1)

    def test_a_fresh_200_after_a_304_updates_the_entry(self):
        drive, seen = self.fetch([
            response(USER, etag='"etag-1"'), not_modified(API + '/user'),
            response(USER, etag='"etag-2"'), not_modified(API + '/user')])
        store = ghwidgets_cache.RestCache(None)
        self.assertEqual(drive(store), (USER, ''))
        self.assertEqual(store.entry(API + '/user')['etag'], '"etag-1"')
        self.assertEqual(drive(store), (USER, ''))
        self.assertEqual(store.entry(API + '/user')['etag'], '"etag-1"')
        self.assertEqual(drive(store), (USER, ''))
        self.assertEqual(store.entry(API + '/user')['etag'], '"etag-2"')
        self.assertEqual(drive(store), (USER, ''))
        self.assertEqual(seen, [None, '"etag-1"', '"etag-1"', '"etag-2"'])

    def test_a_response_without_an_etag_stores_nothing(self):
        drive, _seen = self.fetch([response(USER)])
        store = ghwidgets_cache.RestCache(self.path)
        self.assertEqual(drive(store), (USER, ''))
        self.assertEqual(store.updates, {})
        store.save()
        self.assertFalse(self.path.exists())

    def test_a_non_304_error_propagates_unchanged(self):
        denied = urllib.error.HTTPError('url', 403, 'denied', Message(), None)
        drive, _seen = self.fetch([response(USER, etag='"etag-1"'), denied])
        store = ghwidgets_cache.RestCache(self.path)
        self.assertEqual(drive(store), (USER, ''))
        with self.assertRaises(urllib.error.HTTPError) as caught:
            drive(store)
        self.assertEqual(caught.exception.code, 403)

    def test_a_reported_non_200_status_raises_before_reading_or_storing(self):
        drive, _seen = self.fetch([response({}, status=202, etag='"etag-1"')])
        store = ghwidgets_cache.RestCache(self.path)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            drive(store)
        self.assertEqual(caught.exception.code, 202)
        self.assertEqual(store.updates, {})

    def test_a_corrupt_store_degrades_to_a_full_fetch(self):
        self.path.write_text('{oops', encoding='utf-8')
        drive, seen = self.fetch([response(USER, etag='"etag-1"')])
        store = ghwidgets_cache.RestCache(self.path)
        self.assertEqual(store.entries, {})
        self.assertEqual(drive(store), (USER, ''))
        self.assertEqual(seen, [None])
        store.save()
        self.assertEqual(
            json.loads(self.path.read_text(encoding='utf-8'))['version'], 1)

    def test_a_malformed_entry_degrades_to_a_full_fetch(self):
        self.path.write_text(
            json.dumps({'version': 1, API + '/user': 'garbage'}), encoding='utf-8')
        store = ghwidgets_cache.RestCache(self.path)
        self.assertIsNone(store.entry(API + '/user'))
        drive, seen = self.fetch([response(USER, etag='"etag-1"')])
        self.assertEqual(drive(store), (USER, ''))
        self.assertEqual(seen, [None])

    def test_an_undecodable_body_is_not_stored(self):
        store = ghwidgets_cache.RestCache(None)
        store.remember(API + '/user', '"etag-1"', b'\xff\xfe bad', '')
        self.assertEqual(store.updates, {})

    def test_a_failed_save_degrades_with_a_warning(self):
        store = ghwidgets_cache.RestCache(self.path)
        store.remember(API + '/user', '"etag-1"', json.dumps(USER).encode(), '')
        with mock.patch.object(ghwidgets_cache, 'merge_cache',
                               side_effect=OSError('no space left')), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            store.save()
        self.assertIn('warning: could not save REST cache', stderr.getvalue())

    def test_save_without_a_path_or_updates_writes_nothing(self):
        ghwidgets_cache.RestCache(None).save()
        store = ghwidgets_cache.RestCache(self.path)
        store.save()
        self.assertFalse(self.path.exists())


class PopularityRuns(unittest.TestCase):
    """The renderer end to end: counts line, env-unset identity, 304 savings."""

    def setUp(self):
        # pylint: disable=consider-using-with
        self.directory = _ghw_temp.tempdir(prefix='ghw-rest-cache-')
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'rest-cache.json'
        self.out = Path(self.directory.name) / 'widgets'

    def git_double(self, command, **_kwargs):
        if command[1] == 'clone':
            return subprocess.CompletedProcess(command, 0, '', '')
        if command[3] == 'show-ref':
            return subprocess.CompletedProcess(command, 0, b'refs', b'')
        return subprocess.CompletedProcess(
            command, 0, b'Real\x1freal@example.org\0', b'')

    def run_main(self, github):
        argv = ['render-popularity.py', '--token', 't', '--out-dir', str(self.out)]
        with mock.patch.object(sys, 'argv', argv), \
                mock.patch.object(popularity.urllib.request, 'urlopen', github), \
                mock.patch.object(popularity.subprocess, 'run',
                                  side_effect=self.git_double), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            popularity.main()
        svg = (self.out / 'popularity.svg').read_text(encoding='utf-8')
        return stdout.getvalue(), svg

    def single_repo_bodies(self):
        return {
            '/user': USER,
            '/user/orgs?per_page=100': [],
            '/user/repos?affiliation=owner&per_page=100': [repo()],
            '/repos/Owner/Repo/traffic/clones': {'uniques': 7},
            '/repos/Owner/Repo/traffic/views': {'uniques': 11},
        }

    def test_counts_surface_after_the_run_and_a_304_run_spends_no_quota(self):
        github = FakeGitHub(self.single_repo_bodies())
        with mock.patch.dict(os.environ, {'GH_WIDGETS_REST_CACHE': str(self.path)}):
            first_out, first_svg = self.run_main(github)
            self.assertIn('rest-cache: requests=5 not-modified=0', first_out)
            second_out, second_svg = self.run_main(github)
            self.assertIn('rest-cache: requests=5 not-modified=5', second_out)
        self.assertEqual(first_svg, second_svg)
        expected = []
        for path in ('/user', '/user/orgs?per_page=100',
                     '/user/repos?affiliation=owner&per_page=100',
                     '/repos/Owner/Repo/traffic/clones',
                     '/repos/Owner/Repo/traffic/views'):
            expected.append((path, None))
        for path in ('/user', '/user/orgs?per_page=100',
                     '/user/repos?affiliation=owner&per_page=100',
                     '/repos/Owner/Repo/traffic/clones',
                     '/repos/Owner/Repo/traffic/views'):
            expected.append((path, f'"{API}{path}"'))
        self.assertEqual(github.requests, expected)

    def test_env_unset_behaves_exactly_as_before(self):
        github = FakeGitHub(self.single_repo_bodies())
        env = {name: value for name, value in os.environ.items()
               if name not in ('GH_WIDGETS_REST_CACHE', 'REST_CACHE')}
        with mock.patch.dict(os.environ, env, clear=True):
            output, _svg = self.run_main(github)
        self.assertNotIn('rest-cache:', output)
        self.assertFalse(self.path.exists())
        self.assertEqual([match for _path, match in github.requests if match], [])


class PublicOrganizations(unittest.TestCase):
    """fetch_public_organizations through the store, via its request_fn seam."""

    def setUp(self):
        # pylint: disable=consider-using-with
        self.directory = _ghw_temp.tempdir(prefix='ghw-rest-cache-')
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'rest-cache.json'

    def run_fetch(self, github):
        with mock.patch.dict(os.environ, {'GH_WIDGETS_REST_CACHE': str(self.path)}), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            orgs = common.fetch_public_organizations('Octocat', request_fn=github)
        return orgs, stdout.getvalue()

    def test_stored_pages_are_reused_and_the_next_page_is_still_followed(self):
        page_one = '/users/Octocat/orgs?per_page=100&page=1'
        page_two = '/users/Octocat/orgs?per_page=100&page=2'
        github = FakeGitHub(
            {page_one: [{'login': 'PublicOne'}],
             page_two: [{'login': 'PublicTwo'}]},
            {page_one: f'<{API}{page_two}>; rel="next"'})
        first, first_out = self.run_fetch(github)
        second, second_out = self.run_fetch(github)
        self.assertEqual(first, ['PublicOne', 'PublicTwo'])
        self.assertEqual(second, first)
        self.assertIn('rest-cache: requests=2 not-modified=0', first_out)
        self.assertIn('rest-cache: requests=2 not-modified=2', second_out)
        self.assertEqual(github.requests, [
            (page_one, None), (page_two, None),
            (page_one, f'"{API}{page_one}"'), (page_two, f'"{API}{page_two}"')])
        self.assertEqual([match for match in github.headers[0]
                          if match[0].lower() == 'authorization'], [])

    def test_a_304_still_counts_as_a_request_over_the_wire(self):
        recorder = common.TimingRecorder()
        github = FakeGitHub({'/users/Octocat/orgs?per_page=100&page=1': []})
        with mock.patch.dict(os.environ, {'GH_WIDGETS_REST_CACHE': str(self.path)}), \
                mock.patch.object(common, 'DEBUG_TIMING', True), \
                mock.patch.object(common, 'timing', recorder), \
                contextlib.redirect_stdout(io.StringIO()):
            common.fetch_public_organizations('Octocat', request_fn=github)
            common.fetch_public_organizations('Octocat', request_fn=github)
        self.assertEqual(recorder.totals[('rest', '/users/{user}/orgs')][0], 2)


if __name__ == '__main__':
    unittest.main()
