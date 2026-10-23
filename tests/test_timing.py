"""Opt-in run attribution, using deterministic clocks and offline transports."""
import contextlib
import io
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from concurrent import futures
from email.message import Message
from unittest import mock

from tests import test_render, test_stars, test_popularity
from tests import test_impact_fallback, test_responsiveness, test_impact_loc
from tests.test_common import common


class RunTiming(unittest.TestCase):
    """Counts and exclusive phases must survive retries and overlapping work."""

    def setUp(self):
        self.clock = 0.0
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(common, 'DEBUG_TIMING', True, create=True).start()
        mock.patch.object(common.time, 'monotonic', lambda: self.clock).start()
        self.assertTrue(hasattr(common, 'TimingRecorder'), 'run recorder missing')
        self.recorder = common.TimingRecorder()
        mock.patch.object(common, 'timing', self.recorder).start()

    def test_retry_attempts_and_sleep_are_reported_separately(self):
        """A 503 costs a request, then back-off, then a second request."""
        def request(_req, **_kwargs):
            self.clock += 2
            if self.clock == 2:
                raise urllib.error.HTTPError('', 503, 'busy', Message(), None)
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = b'{"data": {}}'
            return response

        def sleep(seconds):
            self.clock += seconds

        with mock.patch.object(common.urllib.request, 'urlopen', request), \
                mock.patch.object(common.time, 'sleep', sleep):
            with self.recorder.track('phase', 'fetch'):
                self.assertEqual(common.gql('t', 'query {}'), {})
                self.clock += 1
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.recorder.report('example')
        text = output.getvalue()
        self.assertIn('wall=10.000s cpu=', text)
        self.assertIn('graphql /graphql: count=2 blocked=4.000s', text)
        self.assertIn('rest: count=0', text)
        self.assertIn('retry-sleep: count=1 blocked=5.000s', text)
        self.assertIn('phase fetch: count=1 blocked=1.000s', text)
        self.assertIn('attributed=10.000s wall=10.000s gap=0.000s', text)

    def test_nested_phases_net_blocking_and_concurrent_processes(self):
        """Child phases consume their parent's budget; worker time does not."""
        with self.recorder.track('phase', 'outer'):
            with self.recorder.track('process', 'git'):
                self.clock += 2
            with self.recorder.track('phase', 'inner'):
                self.clock += 3

            def clone():
                with self.recorder.track('process', 'git'):
                    self.clock += 4

            worker = threading.Thread(target=clone)
            worker.start()
            worker.join()
        self.clock += 1  # An unnamed phase must remain visible as a gap.
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.recorder.report('example')
        text = output.getvalue()
        self.assertIn('process git: count=2 blocked=2.000s concurrent=4.000s', text)
        self.assertIn('phase inner: count=1 blocked=3.000s', text)
        self.assertIn('phase outer: count=1 blocked=4.000s', text)
        self.assertIn('attributed=9.000s wall=10.000s gap=1.000s', text)

    def test_endpoint_shapes_drop_names_and_pagination(self):
        """Pin each endpoint family used by popularity and public identity."""
        shapes = {
            '/user': '/user', '/user/orgs?per_page=100': '/user/orgs',
            '/user/repos?affiliation=owner&page=2': '/user/repos',
            '/users/A%2FB/orgs?page=1': '/users/{user}/orgs',
            '/orgs/Company/repos': '/orgs/{org}/repos',
            'https://api.github.com/repos/Owner/Repo/traffic/clones':
                '/repos/{owner}/{repo}/traffic/clones',
            '/repos/Other/Another/traffic/views?x=1':
                '/repos/{owner}/{repo}/traffic/views',
        }
        for url, expected in shapes.items():
            with self.subTest(url=url):
                self.assertEqual(common.endpoint_shape(url), expected)

    def test_disabled_recorder_does_not_read_clocks_or_print(self):
        """Disabled hooks must yield normally without observing or reporting."""
        with mock.patch.object(common, 'DEBUG_TIMING', False), \
                mock.patch.object(common.time, 'monotonic',
                                  side_effect=AssertionError('clock read')), \
                mock.patch('builtins.print', side_effect=AssertionError('print')):
            with self.recorder.track('graphql', '/graphql'):
                pass
            self.recorder.report('disabled')
        self.assertEqual(self.recorder.totals, {})

    def test_public_org_pages_and_failed_requests_are_counted(self):
        """Public memberships use an injected transport outside gql()."""
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'[{"login":"Public"}]'
        response.__enter__.return_value.headers = {}

        def transport(*_args, **_kwargs):
            return response
        self.assertEqual(common.fetch_public_organizations('Account', request_fn=transport),
                         ['Public'])
        with self.assertRaises(RuntimeError):
            common.fetch_public_organizations('Account', request_fn=mock.Mock(
                side_effect=urllib.error.URLError('offline')))
        self.assertEqual(self.recorder.totals[('rest', '/users/{user}/orgs')][0], 2)


class RendererTiming(unittest.TestCase):
    """Exercise main() and real I/O seams rather than just the accumulator."""

    def test_all_main_entry_points_report_even_on_failure(self):
        """Each CLI owns its report lifetime, including an early failure."""
        modules = (test_render.render, test_stars.stars, test_popularity.popularity,
                   test_impact_fallback.render_impact, test_responsiveness.resp)
        for module in modules:
            with self.subTest(module=module.__name__):
                output = io.StringIO()
                with mock.patch.object(module.common, 'DEBUG_TIMING', True), \
                        mock.patch.object(module, 'parse_args', side_effect=ValueError('stop')), \
                        contextlib.redirect_stdout(output):
                    with self.assertRaisesRegex(ValueError, 'stop'):
                        module.main()
                self.assertIn('wall=', output.getvalue())
                self.assertIn('cpu=', output.getvalue())
                self.assertIn('unattributed', output.getvalue())

    def test_profile_full_render_is_byte_identical_with_flag_off_and_on(self):
        """Run the real profile publisher against the existing fake API."""
        render = test_render.render
        self.assertTrue(hasattr(render.main, '__wrapped__'), 'main timing missing')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / 'cache.json'
            baseline = None
            stdout = None
            fixed_now = render.datetime.now(render.timezone.utc)
            for enabled, instrumented in ((False, False), (False, True), (True, True)):
                with mock.patch.object(render.common, 'DEBUG_TIMING', enabled), \
                        mock.patch.object(render, 'main', render.main if instrumented
                                          else render.main.__wrapped__), \
                        mock.patch.object(render, 'datetime', wraps=render.datetime) as dates:
                    dates.now.return_value = fixed_now
                    text, _ = test_render.run_main_capturing_output(
                        test_render.FakeAPI(), root, cache)
                # Every arm uses the same acquisition timestamp so complete
                # cache bytes, as well as published SVG bytes, can be compared.
                files = {p.name: p.read_bytes() for p in root.glob('*.svg')}
                files['cache'] = cache.read_bytes()
                if baseline is None:
                    baseline, stdout = files, text
                self.assertEqual(files, baseline)
                if enabled:
                    self.assertIn('=== render-gh-widgets timing ===', text)
                else:
                    self.assertEqual(text, stdout)
                    self.assertNotIn('timing', text)
                # Each arm starts cold, with the same API input population.
                cache.unlink()

    def test_env_name_is_read_once_by_common_and_impact(self):
        """Both import-time switches read the new name and retain that value."""
        # Load fresh modules without mutating the suite's shared donor modules.
        assert common.__file__ is not None
        source = Path(common.__file__)
        for name in ('ghwidgets_common.py', 'impact_loc.py'):
            for enabled in ('', '1'):
                env = {**os.environ, 'GH_WIDGETS_DEBUG_TIMING': enabled}
                code = (
                    'import importlib.util, os, sys; '
                    's=importlib.util.spec_from_file_location("fresh", sys.argv[1]); '
                    'm=importlib.util.module_from_spec(s); s.loader.exec_module(m); '
                    'os.environ["GH_WIDGETS_DEBUG_TIMING"]="changed"; '
                    'print(m.DEBUG_TIMING)')
                result = subprocess.run([sys.executable, '-c', code, str(source.with_name(name))],
                                        env=env, capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout.strip(), str(bool(enabled)))
        # These assertions carry the wrapper contract on Windows, where the
        # POSIX execution test is skipped.
        for unit, suffix in (('gh-widgets.service', ''),
                             ('gh-widgets-resync.service', ' --resync')):
            with self.subTest(unit=unit):
                unit_file = source.resolve().parents[1] / 'units' / unit
                line = next(line for line in unit_file.read_text(encoding='utf-8').splitlines()
                            if line.startswith('ExecStart=/bin/bash'))
                for fragment in ('set -o pipefail',
                                 'gh api --paginate user/orgs --jq ".[].login"',
                                 'paste -sd, -', 'GH_EXTRA_ORGS=',
                                 'printf "orgs-lookup:'):
                    self.assertIn(fragment, line)
                self.assertEqual(
                    line.count('if [ -n "$$GH_WIDGETS_DEBUG_TIMING" ]; then'), 2)
                self.assertTrue(line.endswith(
                    f'exec /usr/local/bin/render-gh-widgets.py{suffix}\''))

    def test_popularity_rest_retries_and_git_commands_reach_recorder(self):
        """Exercise popularity's own transport and its empty-history probes."""
        module = test_popularity.popularity
        with mock.patch.object(module.common, 'DEBUG_TIMING', True):
            recorder = module.common.TimingRecorder()
            with mock.patch.object(module.common, 'timing', recorder), \
                    mock.patch.object(module.urllib.request, 'urlopen', side_effect=[
                        urllib.error.HTTPError('', 503, 'busy', Message(), None),
                        test_popularity.response({'uniques': 2})]), \
                    mock.patch.object(module.common.time, 'sleep'):
                module.rest('t', '/repos/A/B/traffic/views')
                # The existing empty-remote fixture exercises clone, show-ref,
                # symbolic-ref and ls-remote without needing a real network.
                result = subprocess.CompletedProcess([], 1, b'', b'')
                empty = subprocess.CompletedProcess([], 0, b'', b'')
                with mock.patch.object(module.subprocess, 'run',
                                       side_effect=[empty, result, empty, empty]):
                    count = module.outsider_commits(test_popularity.repo(), test_popularity.USER, set())
                    self.assertEqual(count, 0)
            self.assertEqual(recorder.totals[('rest', '/repos/{owner}/{repo}/traffic/views')][0], 2)
            self.assertEqual(recorder.totals[('retry-sleep', '')][0], 1)
            self.assertEqual(recorder.totals[('process', 'git')][0], 4)

    def test_impact_bounded_clone_and_fame_probe_processes_are_counted(self):
        """The bounded runner and clone launcher each own a real child."""
        module = test_impact_loc.impact_loc
        clone = module.impact_clone
        # Configure the path-loaded donor just as render-impact does.
        module.configure(test_impact_loc.render_impact.common)
        shared = module.common
        with mock.patch.object(shared, 'DEBUG_TIMING', True):
            recorder = shared.TimingRecorder()
            with mock.patch.object(shared, 'timing', recorder):
                module._run_bounded([sys.executable, '-c', 'pass'])  # pylint: disable=protected-access
                with futures.ThreadPoolExecutor(max_workers=1) as pool:
                    result = pool.submit(clone._run_clone_command,  # pylint: disable=protected-access
                                         [sys.executable, '-c', 'pass']).result()
                    self.assertEqual(result.returncode, 0)
                with mock.patch.object(module.subprocess, 'run', side_effect=[
                        subprocess.CompletedProcess([], 0, '--jobs', ''),
                        subprocess.CompletedProcess([], 0, 'fork', '')]), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertTrue(module.check_git_fame())
                # A launch failure starts no child; a nonzero exit after a
                # successful launch still belongs to the attempted program.
                with mock.patch.object(module.subprocess, 'run',
                                       side_effect=FileNotFoundError('missing')):
                    with self.assertRaises(FileNotFoundError):
                        shared.run_process(['missing'])
                with mock.patch.object(module.subprocess, 'run',
                                       side_effect=subprocess.CalledProcessError(1, ['git'])):
                    with self.assertRaises(subprocess.CalledProcessError):
                        shared.run_process(['git'])
                renderer = test_impact_loc.render_impact
                with mock.patch.object(renderer, 'gql', return_value={}), \
                        mock.patch.object(shared.time, 'sleep'):
                    self.assertEqual(renderer.fetch_repo_totals('t', ['a/b']), {})
            self.assertEqual(sum(v[0] for (kind, _), v in recorder.totals.items()
                                 if kind == 'process'), 5)
            self.assertEqual(recorder.totals[('process', 'git-fame')][0], 2)
            self.assertNotIn(('process', 'missing'), recorder.totals)
            self.assertEqual(recorder.totals[('pacing', '')][0], 1)
            python_row = recorder.totals[('process', Path(sys.executable).name)]
            self.assertEqual(python_row[0], 2)
            self.assertGreater(python_row[1], 0)
            self.assertGreater(python_row[2], 0)


# The env-name test above carries the textual contract on every OS.
@unittest.skipIf(
    sys.platform == 'win32',
    'bash resolves to WSL bash.exe without an installed distribution on Windows; '
    'the units run on Linux in production, and '
    'RendererTiming.test_env_name_is_read_once_by_common_and_impact checks their text on every OS',
)
class UnitTiming(unittest.TestCase):
    """Execute both shipped wrappers with offline gh and renderer commands."""

    def test_org_lookup_flag_output_wiring_and_pipefail(self):
        """Run the actual shell from both units with systemd escapes decoded."""
        assert common.__file__ is not None
        root = Path(common.__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / 'token'
            token.write_text('t', encoding='utf-8')
            # Shell functions avoid executable fixtures or deployment paths.
            functions = ('fake_gh() { printf "A\\nB\\n"; return "${FAIL_GH:-0}"; }; '
                         'fake_render() { printf "render:%s:%s\\n" "$GH_EXTRA_ORGS" "$*"; }; ')
            for unit, suffix in (('gh-widgets.service', ''),
                                 ('gh-widgets-resync.service', '--resync')):
                line = next(line for line in (root / 'units' / unit).read_text().splitlines()
                            if line.startswith('ExecStart=/bin/bash'))
                script = shlex.split(line.split('=', 1)[1])[2]
                script = script.replace('$$', '$').replace('%%', '%')
                script = script.replace('/usr/local/bin/gh', 'fake_gh')
                script = script.replace('exec /usr/local/bin/render-gh-widgets.py', 'fake_render')
                for flag in ('', '1'):
                    env = {**os.environ, 'GH_TOKEN_FILE': str(token),
                           'GH_WIDGETS_DEBUG_TIMING': flag}
                    result = subprocess.run(['bash', '-c', functions + script], env=env,
                                            capture_output=True, text=True, check=True)
                    expected = f'render:A,B:{suffix}\n'
                    if flag:
                        self.assertRegex(result.stdout, r'^orgs-lookup: \d+\.\d+s\n' + expected)
                    else:
                        self.assertEqual(result.stdout, expected)
                    self.assertEqual(result.stderr, '')
                    failed = subprocess.run(['bash', '-c', functions + script],
                                            env={**env, 'FAIL_GH': '1'},
                                            capture_output=True, text=True, check=False)
                    self.assertEqual(failed.returncode, 1)
                    self.assertNotIn('render:', failed.stdout)
