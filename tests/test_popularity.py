"""Offline popularity acquisition, classification, ranking and SVG tests."""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path
from email.message import Message
from unittest import mock
from tests import _ghw_temp

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('popularity', ROOT / 'src/render-popularity.py')
if spec is None or spec.loader is None:
    raise SystemExit('cannot load render-popularity.py')
popularity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(popularity)
USER = {'login': 'Owner', 'id': 42, 'name': 'Owner Name'}


def repo(name='Owner/Repo', **changes):
    result = {'full_name': name, 'visibility': 'public', 'fork': False,
              'archived': False, 'clone_url': f'https://github.com/{name}.git',
              'default_branch': 'trunk', 'stargazers_count': 2, 'forks_count': 3}
    return {**result, **changes}


def response(data, link='', status=200):
    result = mock.MagicMock()
    result.__enter__.return_value = result
    result.status = status
    result.read.return_value = json.dumps(data).encode()
    result.headers = {'Link': link}
    return result


def git_repository(path, author=None):
    """Construct a real bare history without changing configured git identity."""
    subprocess.run(['git', 'init', '--bare', '--initial-branch=trunk', str(path)], check=True, capture_output=True)
    for key in ('gc.autoDetach', 'maintenance.autoDetach'):
        subprocess.run(['git', '-C', str(path), 'config', key, 'false'],
                       check=True, capture_output=True)
    if author is not None:
        tree = subprocess.run(['git', '-C', str(path), 'hash-object', '-t', 'tree',
                               '-w', '--stdin'], input=b'', check=True,
                              capture_output=True).stdout.strip()
        commit = (b'tree ' + tree + b'\nauthor ' + author.encode('utf-8')
                  + b' <real@example.org> 1 +0000\n'
                  b'committer Fixture <fixture@example.org> 1 +0000\n\nfixture\n')
        oid = subprocess.run(['git', '-C', str(path), 'hash-object', '-t', 'commit',
                              '-w', '--stdin'], input=commit, check=True,
                             capture_output=True).stdout.decode().strip()
        subprocess.run(['git', '-C', str(path), 'update-ref', 'refs/heads/trunk', oid],
                       check=True, capture_output=True)
    return repo(clone_url=path.as_uri())


class Authors(unittest.TestCase):
    def test_real_empty_git_repository_counts_zero(self):
        with _ghw_temp.tempdir() as directory:
            repository = git_repository(Path(directory) / 'empty.git')
            self.assertEqual(popularity.outsider_commits(repository, USER, set()), 0)

    def test_real_nonempty_history_with_missing_default_branch_fails(self):
        with _ghw_temp.tempdir() as directory:
            path = Path(directory) / 'history.git'
            repository = git_repository(path, 'Real Person')
            repository['default_branch'] = 'missing'
            with self.assertRaisesRegex(RuntimeError, 'Owner/Repo'):
                popularity.outsider_commits(repository, USER, set())
            subprocess.run(['git', '-C', str(path), 'symbolic-ref', 'HEAD',
                            'refs/heads/missing'], check=True, capture_output=True)
            with self.assertRaisesRegex(RuntimeError, 'Owner/Repo'):
                popularity.outsider_commits(repository, USER, set())

    def test_real_corrupt_repository_is_not_treated_as_empty(self):
        with _ghw_temp.tempdir() as directory:
            path = Path(directory) / 'corrupt.git'
            repository = git_repository(path, 'Real Person')
            (path / 'refs' / 'heads' / 'trunk').write_bytes(b'1' * 40 + b'\n')
            with self.assertRaisesRegex(RuntimeError, 'Owner/Repo'):
                popularity.outsider_commits(repository, USER, set())

    def test_real_git_unicode_line_separators_are_author_data(self):
        with _ghw_temp.tempdir() as directory:
            for index, author in enumerate(('Real\u2028Person', 'Real\u0085Person')):
                with self.subTest(author=author):
                    repository = git_repository(Path(directory) / f'history{index}.git', author)
                    self.assertEqual(popularity.outsider_commits(repository, USER, set()), 1)

    def test_malformed_git_records_fail_with_repository_context(self):
        for data in (b'no-fields\0', b'name\x1femail\x1fextra\0', b'name\x1femail'):
            def git(command, data=data, **kwargs):
                if 'log' in command:
                    output = data.decode() if kwargs.get('text') else data
                    return subprocess.CompletedProcess(command, 0, output, b'')
                return subprocess.CompletedProcess(command, 0, b'refs', b'')

            with self.subTest(data=data), mock.patch.object(
                    popularity.subprocess, 'run', side_effect=git):
                with self.assertRaisesRegex(RuntimeError, 'Owner/Repo.*malformed'):
                    popularity.outsider_commits(repo(), USER, set())

    def test_exact_exclusions_and_real_outsiders(self):
        emails = popularity.common.our_emails('Owner', 42, ['work@example.org'])
        cases = [
            ('Else', 'OWNER@users.noreply.github.com', False),
            ('Else', '42+Owner@users.noreply.github.com', False),
            ('Else', 'WORK@example.org', False),
            ('Owner', 'someone@example.org', False),
            ('Owner Name', 'someone@example.org', False),
            ('Deploy[BOT]', 'someone@example.org', False),
            ('Deploy', 'deploy[BOT]@example.org', False),
            ('GitHub', 'noreply@github.com', False),
            ('Real Person', 'real@example.org', True),
            ('Owner Namesake', 'not-work@example.org', True),
            ('Other', '142+Owner@users.noreply.github.com', True),
            ('owner', 'other@example.org', True),
        ] + [('Agent', email, False) for email in (
            'noreply@anthropic.com', 'NOREPLY@openai.com', 'noreply@z.ai',
            'noreply@openrouter.ai')]
        for name, email, expected in cases:
            with self.subTest(name=name, email=email):
                self.assertEqual(popularity.is_outsider(name, email, USER, emails), expected)

    def test_clone_default_branch_count_and_cleanup(self):
        paths = []

        def git(command, **kwargs):
            if command[1] == 'clone':
                self.assertIn('--filter=blob:none', command)
                self.assertIn('--single-branch', command)
                self.assertEqual(command[-2], 'https://github.com/Owner/Repo.git')
                paths.append(Path(command[-1]))
                self.assertTrue(paths[-1].parent.exists())
                return subprocess.CompletedProcess(command, 0, '', '')
            if command[3] == 'show-ref':
                return subprocess.CompletedProcess(command, 0, b'refs', b'')
            self.assertEqual(command, ['git', '-C', str(paths[-1]), 'log',
                                       '-z', '--format=%an%x1f%ae',
                                       'refs/remotes/origin/trunk', '--'])
            self.assertFalse(kwargs['text'])
            return subprocess.CompletedProcess(command, 0,
                                               b'Owner\x1fa@b\0Real\x1fr@e\0Real\x1fr@e\0', b'')

        with mock.patch.object(popularity.subprocess, 'run', side_effect=git):
            for _ in range(2):
                self.assertEqual(popularity.outsider_commits(repo(), USER, set()), 2)
        self.assertNotEqual(paths[0], paths[1])
        self.assertTrue(all(not path.parent.exists() for path in paths))

    def test_git_failures_name_repo_and_clean_temp(self):
        paths = []
        for phase in ('clone', 'log'):
            paths.clear()

            def git(command, phase=phase, **_kwargs):
                if command[1] == 'clone':
                    paths.append(Path(command[-1]))
                if phase in command:
                    raise subprocess.CalledProcessError(128, command, stderr='git failed')
                return subprocess.CompletedProcess(command, 0, '', '')

            with mock.patch.object(popularity.subprocess, 'run', side_effect=git):
                with self.assertRaisesRegex(RuntimeError, 'Owner/Repo'):
                    popularity.outsider_commits(repo(), USER, set())
            self.assertFalse(paths[0].parent.exists())


class Ranking(unittest.TestCase):
    def test_rescale_ties_single_and_zero(self):
        for scores, expected in (
                ([('b/r', 20, 2, 3, 11), ('A/r', 20, 4, 5, 12),
                  ('a/r', 20, 6, 7, 13), ('c/r', 3, 8, 9, 14)],
                 [('A/r', '10.00', 4, 5, 12), ('a/r', '10.00', 6, 7, 13),
                  ('b/r', '10.00', 2, 3, 11), ('c/r', '1.50', 8, 9, 14)]),
                ([('only/r', 3, 1, 0, 2)], [('only/r', '10.00', 1, 0, 2)]),
                ([('b/r', 0, 0, 0, 0), ('a/r', 0, 0, 0, 0)],
                 [('a/r', '0.00', 0, 0, 0), ('b/r', '0.00', 0, 0, 0)]),
                ([], [])):
            self.assertEqual(popularity.rank_repositories(scores), expected)

    def test_top_five_cut(self):
        scores = [(f'o/r{i}', i, i, 0, i * 10) for i in range(7)]
        self.assertEqual(popularity.rank_repositories(scores), [
            ('o/r6', '10.00', 6, 0, 60), ('o/r5', '8.33', 5, 0, 50), ('o/r4', '6.67', 4, 0, 40),
            ('o/r3', '5.00', 3, 0, 30), ('o/r2', '3.33', 2, 0, 20)])


class Acquisition(unittest.TestCase):
    def test_weighted_score_components_have_independent_inputs(self):
        repositories = [repo('Owner/Stars', stargazers_count=4, forks_count=0),
                        repo('Owner/Forks', stargazers_count=0, forks_count=5),
                        repo('Owner/Outsiders', stargazers_count=0, forks_count=0),
                        repo('Owner/Cloners', stargazers_count=0, forks_count=0),
                        repo('Owner/Visitors', stargazers_count=0, forks_count=0)]

        def fetch(_token, path, **_kwargs):
            if path == '/user':
                return USER, ''
            uniques = 0
            if path == '/repos/Owner/Cloners/traffic/clones':
                uniques = 7
            if path == '/repos/Owner/Visitors/traffic/views':
                uniques = 11
            return {'count': 9999, 'uniques': uniques}, ''

        def outsiders(repository, _user, _emails):
            return 3 if repository['full_name'] == 'Owner/Outsiders' else 0

        with mock.patch.object(popularity, 'rest', side_effect=fetch), \
                mock.patch.object(popularity, 'rest_pages', side_effect=[[], repositories]), \
                mock.patch.object(popularity, 'outsider_commits', side_effect=outsiders):
            _, scores = popularity.fetch_scores('t')
        self.assertEqual(scores, [('Owner/Stars', 200, 4, 0, 0), ('Owner/Forks', 200, 0, 5, 0),
                                  ('Owner/Outsiders', 15, 0, 0, 0), ('Owner/Cloners', 14, 0, 0, 0),
                                  ('Owner/Visitors', 11, 0, 0, 11)])

    def test_fetched_profile_email_excludes_old_git_name_without_replacing_emails(self):
        user = {**USER, 'email': 'Owner@Example.ORG'}
        repository = repo(stargazers_count=0, forks_count=0)
        pages = [response(user), response([]), response([repository]),
                 response({'uniques': 0}), response({'uniques': 0})]
        git = [subprocess.CompletedProcess([], 0, '', ''),
               subprocess.CompletedProcess([], 0, b'refs', b''),
               subprocess.CompletedProcess([], 0,
                                           b'Old Name\x1fowner@example.org\0'
                                           b'Other Name\x1fwork@example.org\0'
                                           b'Past Name\x1f42+Owner@users.noreply.github.com\0', b'')]
        with mock.patch.dict(os.environ,
                             {'GH_WIDGETS_GH_EXTRA_EMAILS':
                              'work@example.org'}), \
                mock.patch.object(popularity.urllib.request, 'urlopen', side_effect=pages), \
                mock.patch.object(popularity.subprocess, 'run', side_effect=git):
            _, scores = popularity.fetch_scores('t')
        self.assertEqual(scores, [('Owner/Repo', 0, 0, 0, 0)])

    def test_owned_and_org_pages_filter_deduplicate_and_score_uniques(self):
        urls = []
        fixtures = {
            '/user': response(USER),
            '/user/orgs?per_page=100': response(
                [{'login': 'Org'}], '<https://api.github.com/org-page2>; rel="next"'),
            '/org-page2': response([{'login': 'Other'}]),
            '/user/repos?affiliation=owner&visibility=public&per_page=100': response([
                repo(), repo('Owner/Private', visibility='private'),
                repo('Owner/Fork', fork=True), repo('Owner/Old', archived=True)],
                '<https://api.github.com/repo-page2>; rel="next"'),
            '/repo-page2': response([repo('Owner/Second')]),
            '/orgs/Org/repos?type=public&per_page=100': response([repo('Org/Repo')]),
            '/orgs/Other/repos?type=public&per_page=100': response([repo('Org/Repo')]),
        }

        def fetch(request, **_kwargs):
            url = request.full_url.removeprefix('https://api.github.com')
            urls.append(url)
            if url.endswith('/traffic/clones'):
                return response({'count': 9999, 'uniques': 7, 'clones': []})
            if url.endswith('/traffic/views'):
                return response({'count': 9999, 'uniques': 11, 'views': []})
            return fixtures[url]

        with mock.patch.object(popularity.urllib.request, 'urlopen', side_effect=fetch), \
                mock.patch.object(popularity, 'outsider_commits', return_value=4):
            user, scores = popularity.fetch_scores('t')
        self.assertEqual(user, USER)
        self.assertEqual(scores, [('Owner/Repo', 265, 2, 3, 11), ('Owner/Second', 265, 2, 3, 11),
                                  ('Org/Repo', 265, 2, 3, 11)])
        self.assertIn('/org-page2', urls)
        self.assertIn('/repo-page2', urls)
        self.assertEqual(len([url for url in urls if '/traffic/' in url]), 6)

    def test_pagination_cycle_fails_instead_of_partial_data(self):
        link = '<https://api.github.com/user/orgs?per_page=100>; rel="next"'
        with mock.patch.object(popularity.urllib.request, 'urlopen',
                               return_value=response([{'login': 'Org'}], link)):
            with self.assertRaises(popularity.common.PaginationLimitError):
                popularity.rest_pages('t', '/user/orgs?per_page=100')

    def test_rest_retry_shape_and_nontransient_errors(self):
        for error in (urllib.error.HTTPError('url', 503, 'down', Message(), None),
                      urllib.error.URLError('down')):
            with mock.patch.object(popularity.urllib.request, 'urlopen',
                                   side_effect=[error, response(USER)]), \
                    mock.patch.object(popularity.time, 'sleep') as sleep:
                data, _ = popularity.rest('t', '/user')
            self.assertEqual(data, USER)
            sleep.assert_called_once_with(5)
            with mock.patch.object(popularity.urllib.request, 'urlopen', side_effect=error), \
                    mock.patch.object(popularity.time, 'sleep'):
                with self.assertRaises(urllib.error.URLError):
                    popularity.rest('t', '/user', retries=1)
        for status in (403, 404):
            error = urllib.error.HTTPError('url', status, 'denied', Message(), None)
            with mock.patch.object(popularity.urllib.request, 'urlopen', side_effect=error):
                with self.assertRaises(urllib.error.HTTPError):
                    popularity.rest('t', '/user')
        for status in (202, 204):
            with mock.patch.object(popularity.urllib.request, 'urlopen',
                                   return_value=response({}, status=status)):
                with self.assertRaises(urllib.error.HTTPError):
                    popularity.rest('t', '/repos/Owner/Repo/traffic/clones')

    def test_unreadable_traffic_fails_loudly_for_both_endpoints(self):
        for endpoint in ('clones', 'views'):
            def fetch(_token, path, endpoint=endpoint, **_kwargs):
                if path == '/user':
                    return USER, ''
                if path.endswith('/traffic/' + endpoint):
                    raise urllib.error.HTTPError('url', 403, 'denied', Message(), None)
                return {'uniques': 2}, ''

            with mock.patch.object(popularity, 'rest', side_effect=fetch), \
                    mock.patch.object(popularity, 'rest_pages', side_effect=[[], [repo()]]), \
                    mock.patch.object(popularity, 'outsider_commits', return_value=0):
                with self.assertRaisesRegex(RuntimeError, f'Owner/Repo.*{endpoint}'):
                    popularity.fetch_scores('t')

    def test_rate_limited_traffic_error_carries_body_and_headers(self):
        headers = Message()
        headers['retry-after'] = '60'
        headers['x-ratelimit-remaining'] = '0'
        error = urllib.error.HTTPError(
            'url', 403, 'Forbidden', headers,
            io.BytesIO(b'{"message":"You have exceeded a secondary rate limit"}'))
        with mock.patch.object(popularity.urllib.request, 'urlopen', side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                popularity.score_repository('t', {'full_name': 'o/r'}, {}, set())
        message = str(caught.exception)
        self.assertIn('unreadable traffic/clones', message)
        self.assertIn('{"message":"You have exceeded a secondary rate limit"}', message)
        self.assertIn('retry-after: 60', message)
        self.assertIn('x-ratelimit-remaining: 0', message)


class Overlap(unittest.TestCase):
    """Per-repository git and traffic work overlaps inside one run."""

    def test_two_repositories_acquire_concurrently(self):
        """A barrier pairs one repository's traffic read with another's."""
        fixtures = {
            '/user': response(USER),
            '/user/orgs?per_page=100': response([]),
            '/user/repos?affiliation=owner&visibility=public&per_page=100': response(
                [repo('Owner/A'), repo('Owner/B')]),
        }
        barrier = threading.Barrier(2, timeout=10)
        traffic = []
        lock = threading.Lock()

        def fetch(request, **_kwargs):
            url = request.full_url.removeprefix('https://api.github.com')
            if url not in fixtures:
                with lock:
                    traffic.append(url)
                barrier.wait()
                return response({'count': 9999, 'uniques': 5})
            return fixtures[url]

        with mock.patch.object(popularity.urllib.request, 'urlopen', side_effect=fetch), \
                mock.patch.object(popularity, 'outsider_commits', return_value=4):
            _, scores = popularity.fetch_scores('t')
        self.assertEqual(scores, [('Owner/A', 255, 2, 3, 5), ('Owner/B', 255, 2, 3, 5)])
        self.assertEqual(sorted(traffic), [
            '/repos/Owner/A/traffic/clones', '/repos/Owner/A/traffic/views',
            '/repos/Owner/B/traffic/clones', '/repos/Owner/B/traffic/views'])

    def test_one_clone_and_two_traffic_reads_per_repository(self):
        """Nonempty repositories still cost one clone and two traffic reads."""
        fixtures = {
            '/user': response(USER),
            '/user/orgs?per_page=100': response([]),
            '/user/repos?affiliation=owner&visibility=public&per_page=100': response(
                [repo('Owner/A'), repo('Owner/B'), repo('Owner/C')]),
        }
        traffic, clones = [], []
        lock = threading.Lock()

        def fetch(request, **_kwargs):
            url = request.full_url.removeprefix('https://api.github.com')
            if url not in fixtures:
                with lock:
                    traffic.append(url)
                return response({'count': 9999, 'uniques': 5})
            return fixtures[url]

        def git(command, **_kwargs):
            if command[1] == 'clone':
                with lock:
                    clones.append(command[-1])
                return subprocess.CompletedProcess(command, 0, '', '')
            if command[3] == 'show-ref':
                return subprocess.CompletedProcess(command, 0, b'refs', b'')
            return subprocess.CompletedProcess(command, 0, b'Real\x1freal@example.org\0', b'')

        recorder = popularity.common.TimingRecorder()
        with mock.patch.object(popularity.common, 'DEBUG_TIMING', True), \
                mock.patch.object(popularity.common, 'timing', recorder), \
                mock.patch.object(popularity.urllib.request, 'urlopen', side_effect=fetch), \
                mock.patch.object(popularity.subprocess, 'run', side_effect=git):
            _, scores = popularity.fetch_scores('t')
        self.assertEqual(scores, [('Owner/A', 240, 2, 3, 5), ('Owner/B', 240, 2, 3, 5),
                                  ('Owner/C', 240, 2, 3, 5)])
        self.assertEqual(len(clones), 3)
        self.assertEqual(len(traffic), 6)
        self.assertEqual(recorder.totals[('process', 'git')][0], 9)

    def test_failed_traffic_leaves_no_temporary_clones(self):
        """A sibling still cloning on failure cleans its own temporary clone."""
        fixtures = {
            '/user': response(USER),
            '/user/orgs?per_page=100': response([]),
            '/user/repos?affiliation=owner&visibility=public&per_page=100': response(
                [repo('Owner/A'), repo('Owner/B')]),
        }
        denied = urllib.error.HTTPError('url', 403, 'denied', Message(), None)
        parents = []
        lock = threading.Lock()

        def fetch(request, **_kwargs):
            url = request.full_url.removeprefix('https://api.github.com')
            if url not in fixtures:
                if url == '/repos/Owner/A/traffic/clones':
                    raise denied
                return response({'count': 9999, 'uniques': 5})
            return fixtures[url]

        def git(command, **_kwargs):
            if command[1] == 'clone':
                with lock:
                    parents.append(Path(command[-1]).parent)
                return subprocess.CompletedProcess(command, 0, '', '')
            if command[3] == 'show-ref':
                return subprocess.CompletedProcess(command, 0, b'refs', b'')
            return subprocess.CompletedProcess(command, 0, b'Real\x1freal@example.org\0', b'')

        with mock.patch.object(popularity.urllib.request, 'urlopen', side_effect=fetch), \
                mock.patch.object(popularity.subprocess, 'run', side_effect=git):
            with self.assertRaisesRegex(RuntimeError, 'Owner/A.*traffic/clones'):
                popularity.fetch_scores('t')
        self.assertEqual(len(parents), 1)
        self.assertTrue(all(not parent.exists() for parent in parents))
        self.assertEqual(list(Path(tempfile.gettempdir()).glob('ghwidgets-popularity-*')), [])


class CardAndCLI(unittest.TestCase):
    def test_full_render_pins_rows_chrome_and_accessibility(self):
        svg = popularity.render_popularity(popularity.THEMES['tokyonight'], USER,
                                           [('Owner/Repo', 100, 2400, 12, 5600),
                                            ('Org/Other', 25, 0, 1200000, 0)])
        root = ET.fromstring(svg)
        self.assertEqual(root.attrib['viewBox'], '0 0 520 159')
        self.assertEqual(root.attrib['role'], 'img')
        self.assertEqual(root.attrib['aria-labelledby'], 'popularity-title popularity-desc')
        self.assertEqual(root.findtext('{*}title'), 'Repository popularity for Owner')
        self.assertEqual(root.findtext('{*}desc'),
                         'Top repositories for Owner. Owner/Repo: 10.00; Org/Other: 2.50. '
                         'Scores relative to the leader; traffic covers the last 14 days.')
        self.assertEqual([(node.attrib['x'], node.attrib['y'], node.text)
                          for node in root.findall('{*}text')], [
                              ('20', '34', 'repo popularity'),
                              ('20', '52', 'popularity of my public repositories'),
                              ('20', '88', 'repo'), ('290', '88', 'stars'), ('340', '88', 'views'),
                              ('390', '88', 'forks'), ('500', '88', 'popularity'),
                              ('20', '105', 'Owner/Repo'), ('290', '105', '2.4k'),
                              ('340', '105', '5.6k'),
                              ('390', '105', '12'), ('500', '105', '10.00'),
                              ('20', '127', 'Org/Other'), ('290', '127', '0'), ('340', '127', '0'),
                              ('390', '127', '1.2M'), ('500', '127', '2.50')])
        texts = root.findall('{*}text')
        self.assertEqual({key: texts[0].get(key) for key in ('fill', 'font-size', 'font-weight')},
                         {'fill': '#e0af68', 'font-size': '14', 'font-weight': '600'})
        self.assertEqual(texts[1].get('font-size'), '11')
        self.assertEqual(texts[1].get('fill'), '#7a82a2')
        self.assertTrue(all(node.get('font-size') == '10' for node in texts[2:7]))
        for node in (texts[4], texts[9], texts[14]):
            self.assertEqual(node.get('x'), '340')
            self.assertEqual(node.get('fill'), '#7a82a2')
        for node in (texts[11], texts[16]):
            self.assertEqual(node.get('font-weight'), '500')
            self.assertEqual(node.get('font-size'), '11')
        bars = [node.attrib for node in root.findall('{*}rect') if node.get('height') == '3']
        self.assertEqual(bars, [
            {'x': '20', 'y': '110', 'width': '480', 'height': '3', 'rx': '1.5',
             'fill': '#2a2e42', 'fill-opacity': '0.35'},
            {'x': '20', 'y': '110', 'width': '480.0', 'height': '3', 'rx': '1.5', 'fill': '#9ece6a'},
            {'x': '20', 'y': '132', 'width': '480', 'height': '3', 'rx': '1.5',
             'fill': '#2a2e42', 'fill-opacity': '0.35'},
            {'x': '20', 'y': '132', 'width': '120.0', 'height': '3', 'rx': '1.5', 'fill': '#9ece6a'}])

    def test_themes_empty_and_escaping(self):
        for colors in popularity.THEMES.values():
            svg = popularity.render_popularity(colors, USER, [])
            root = ET.fromstring(svg)
            self.assertEqual(root.get('height'), '131')
            self.assertEqual(root.findall('{*}text')[-1].text, 'no public repositories')
            self.assertIn(colors['bg'], svg)
        svg = popularity.render_popularity(popularity.THEMES['tokyonight'],
                                           {**USER, 'login': '<&'}, [('o/<' + 'x' * 90, 1, 0, 0, 0)])
        ET.fromstring(svg)
        self.assertIn('&lt;&amp;', svg)
        self.assertIn('o/&lt;' + 'x' * 30 + '…', svg)

    def test_main_writes_fresh_and_preserves_previous_on_failure(self):
        with _ghw_temp.tempdir() as directory:
            argv = ['render-popularity.py', '--token', 't', '--out-dir', directory]
            output = Path(directory) / 'popularity.svg'
            pages = [response(USER), response([]), response([repo()]),
                     response({'count': 999, 'uniques': 7}),
                     response({'count': 999, 'uniques': 11})]
            git = [subprocess.CompletedProcess([], 0, '', ''),
                   subprocess.CompletedProcess([], 0, b'refs', b''),
                   subprocess.CompletedProcess([], 0, b'Real\x1freal@example.org\0', b'')]
            with mock.patch.object(sys, 'argv', argv), mock.patch.object(
                    popularity.urllib.request, 'urlopen', side_effect=pages), \
                    mock.patch.object(popularity.subprocess, 'run', side_effect=git):
                popularity.main()
            previous = output.read_bytes()
            self.assertIn(b'10.00', previous)
            self.assertIn(b'<text x="340" y="105" text-anchor="end" fill="#7a82a2" '
                          b'font-size="11">11</text>', previous)
            denied = urllib.error.HTTPError('url', 403, 'denied', Message(), None)
            with mock.patch.object(sys, 'argv', argv), mock.patch.object(
                    popularity.urllib.request, 'urlopen', side_effect=[
                        response(USER), response([]), response([repo()]), denied]):
                with self.assertRaisesRegex(RuntimeError, 'Owner/Repo.*traffic/clones'):
                    popularity.main()
            self.assertEqual(output.read_bytes(), previous)
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_token_file_env_and_cli_override(self):
        with _ghw_temp.tempdir() as directory:
            token_file = Path(directory) / 'token'
            token_file.write_text('from-file\n', encoding='utf-8')
            env = {'GH_WIDGETS_GH_TOKEN_FILE': str(token_file),
                   'GH_WIDGETS_OUT_DIR': directory, 'GH_WIDGETS_THEME': 'gruvbox'}
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(sys, 'argv', ['render-popularity.py']):
                args, token = popularity.parse_args()
            self.assertEqual((args.out_dir, args.theme, token), (directory, 'gruvbox', 'from-file'))
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
                    sys, 'argv', ['render-popularity.py', '--token', 'cli', '--theme', 'catppuccin']):
                args, token = popularity.parse_args()
            self.assertEqual((args.theme, token), ('catppuccin', 'cli'))
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(sys, 'argv', ['render-popularity.py']), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                popularity.parse_args()

    def parse(self, env):
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(sys, 'argv',
                                  ['render-popularity.py', '--token', 't']):
            args, _token = popularity.parse_args()
        return args

    def test_token_file_default_reads_the_env_name(self):
        self.assertEqual(self.parse(
            {'GH_TOKEN_FILE': '/bare/t'}).token_file, '/bare/t')
        both = {'GH_TOKEN_FILE': '/bare/t',
                'GH_WIDGETS_GH_TOKEN_FILE': '/prefixed/t'}
        self.assertEqual(self.parse(both).token_file, '/prefixed/t')

    def test_out_dir_default_reads_the_env_name(self):
        self.assertEqual(self.parse(
            {'OUT_DIR': '/bare/out'}).out_dir, '/bare/out')
        both = {'OUT_DIR': '/bare/out', 'GH_WIDGETS_OUT_DIR': '/prefixed/out'}
        self.assertEqual(self.parse(both).out_dir, '/prefixed/out')

    def test_theme_default_reads_the_env_name(self):
        self.assertEqual(self.parse({'THEME': 'gruvbox'}).theme, 'gruvbox')
        both = {'THEME': 'gruvbox', 'GH_WIDGETS_THEME': 'github-dark'}
        self.assertEqual(self.parse(both).theme, 'github-dark')

    def test_help_from_other_directory(self):
        with _ghw_temp.tempdir() as directory:
            proc = subprocess.run([sys.executable, str(ROOT / 'src/render-popularity.py'), '--help'],
                                  cwd=directory, capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('--token-file', proc.stdout)
        self.assertNotIn('--resync', proc.stdout)
        self.assertNotIn('--cache-file', proc.stdout)


if __name__ == '__main__':
    unittest.main()
