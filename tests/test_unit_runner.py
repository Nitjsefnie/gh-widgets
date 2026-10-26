"""Execute the deployed schedules with offline, delayed renderer commands."""
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests import _ghw_temp

ROOT = Path(__file__).resolve().parents[1]
UNITS = ('gh-widgets.service', 'gh-widgets-resync.service')
STEPS = ('lookup', 'profile', 'impact', 'responsiveness', 'stars', 'popularity')
COMMANDS = ('gh', 'render-gh-widgets.py', 'render-impact.py',
            'render-responsiveness.py', 'render-stars.py', 'render-popularity.py')
STUB = '''import json, os, sys, time
from pathlib import Path
root = Path(os.environ['GH_WIDGETS_OUT_DIR'])
step = sys.argv[1]
(root / (step + '.start')).write_text(json.dumps({
    'time': time.monotonic(), 'args': sys.argv[2:],
    'orgs': os.environ.get('GH_WIDGETS_GH_EXTRA_ORGS'),
    'cache': os.environ.get('GH_WIDGETS_CACHE_FILE')}))
time.sleep(json.loads(os.environ['GH_WIDGETS_STUB_DELAYS']).get(step, 0))
failed = os.environ.get('GH_WIDGETS_STUB_FAIL') == step
if step == 'lookup':
    print('A\\nB')
elif not failed:
    (root / (step + '.svg')).write_text(step)
(root / (step + '.end')).write_text(str(time.monotonic()))
sys.exit(7 if failed else 0)
'''


def run_unit(unit, fail='', delays=None, timing='', token_name='prefixed'):
    # pylint: disable=too-many-locals
    """Relocate deployed command paths only; execute the unit's actual argv."""
    starts = [line.split('=', 1)[1] for line in
              (ROOT / 'units' / unit).read_text(encoding='utf-8').splitlines()
              if line.startswith('ExecStart=')]
    if len(starts) != 1:
        raise AssertionError('the unit must invoke one complete schedule')
    with _ghw_temp.tempdir(prefix='ghwidgets-unit-') as directory:
        root = Path(directory)
        token = root / 'token'
        token.write_text('offline', encoding='utf-8')
        stub = root / 'stub.py'
        stub.write_text(STUB, encoding='utf-8')
        text = (ROOT / 'scripts/run-gh-widgets.sh').read_text(encoding='utf-8')
        functions = []
        for step, command in zip(STEPS, COMMANDS):
            functions.append(f'fake_{step}() {{ {shlex.quote(sys.executable)} '
                             f'{shlex.quote(str(stub))} {step} "$@"; }}\n')
            text = text.replace('/usr/local/bin/' + command, 'fake_' + step)
        runner = root / 'runner.sh'
        runner.write_text(''.join(functions) + text, encoding='utf-8')
        argv = [str(runner) if arg == '/usr/local/bin/run-gh-widgets.sh' else arg
                for arg in shlex.split(starts[0])]
        # Both token spellings leave the ambient environment, so the case a
        # scenario builds is the only source of either name.
        env = {name: value for name, value in os.environ.items()
               if name not in ('GH_TOKEN_FILE', 'GH_WIDGETS_GH_TOKEN_FILE')}
        if token_name == 'prefixed':
            env['GH_WIDGETS_GH_TOKEN_FILE'] = str(token)
        elif token_name == 'bare':
            env['GH_TOKEN_FILE'] = str(token)
        elif token_name == 'both':
            env['GH_TOKEN_FILE'] = str(root / 'bare-token-missing')
            env['GH_WIDGETS_GH_TOKEN_FILE'] = str(token)
        elif token_name == 'empty-prefixed':
            env['GH_TOKEN_FILE'] = str(token)
            env['GH_WIDGETS_GH_TOKEN_FILE'] = ''
        env.update({'GH_WIDGETS_OUT_DIR': directory,
                    'GH_WIDGETS_CACHE_FILE': str(root / 'profile-cache.json'),
                    'GH_WIDGETS_DEBUG_TIMING': timing,
                    'GH_WIDGETS_STUB_FAIL': fail,
                    'GH_WIDGETS_STUB_DELAYS': json.dumps(delays or {})})
        # Files avoid communicate() waiting for inherited pipes after a runner
        # that wrongly exits before its children: observe the runner's own exit.
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            start = time.monotonic()
            result = subprocess.run(argv, env=env, stdout=stdout, stderr=stderr,
                                    text=True, check=False, timeout=20)
            elapsed = time.monotonic() - start
            stdout.seek(0)
            stderr.seek(0)
            result.stdout = stdout.read().decode()
            result.stderr = stderr.read().decode()
        files = {p.name: p.read_text(encoding='utf-8') for p in root.iterdir()
                 if p.suffix in ('.start', '.end', '.svg')}
        return result, elapsed, files


@unittest.skipUnless(sys.platform.startswith('linux'),
                     'the deployed units target Linux and use bash EPOCHREALTIME')
class UnitSchedule(unittest.TestCase):
    """Both units preserve dependencies, overlap and failure propagation."""

    def test_stubbed_wall_is_longest_chain_plus_two_seconds(self):
        # Chain: 5 * 0.5 = 2.5 s; popularity: 2.5 s. Serial: 5 s > 4.5 s.
        delays = dict.fromkeys(STEPS, 0.5)
        delays['popularity'] = 2.5
        for unit in UNITS:
            with self.subTest(unit=unit):
                result, wall, files = run_unit(unit, delays=delays)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual({s for s in STEPS if s + '.end' in files}, set(STEPS))
                self.assertLessEqual(wall, 2.5 + 2)

    def test_dependencies_and_independent_branches_overlap(self):
        for unit in UNITS:
            with self.subTest(unit=unit):
                result, _, files = run_unit(unit, delays=dict.fromkeys(STEPS, 0.1))
                self.assertEqual(result.returncode, 0, result.stderr)
                starts = {s: json.loads(files[s + '.start'])['time'] for s in STEPS}
                ends = {s: float(files[s + '.end']) for s in STEPS}
                for before, after in (('lookup', 'profile'), ('profile', 'impact'),
                                      ('impact', 'responsiveness'),
                                      ('responsiveness', 'stars')):
                    self.assertLessEqual(ends[before], starts[after])
                self.assertLess(starts['popularity'], ends['stars'])
                self.assertLess(starts['lookup'], ends['popularity'])

    def test_failure_skips_dependents_and_waits_for_all_other_cards(self):
        for unit in UNITS:
            for index, failed in enumerate(STEPS):
                with self.subTest(unit=unit, failed=failed):
                    delays = dict.fromkeys(STEPS, 0.02)
                    delays['popularity'] = 0 if failed == 'popularity' else 0.25
                    result, _, files = run_unit(
                        unit, fail=failed, delays=delays)
                    skipped = set(STEPS[index + 1:5]) if index < 5 else set()
                    started = set(STEPS) - skipped
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(f'{failed} failed', result.stderr)
                    self.assertEqual({s for s in STEPS if s + '.start' in files}, started)
                    self.assertEqual({s for s in STEPS if s + '.end' in files}, started)
                    self.assertEqual({s for s in STEPS if s + '.svg' in files},
                                     started - {'lookup', failed})
                    for step in skipped:
                        self.assertIn(f'{step} skipped', result.stderr)

    def test_resync_reaches_exactly_profile_and_impact_with_original_paths(self):
        for unit in UNITS:
            with self.subTest(unit=unit):
                result, _, files = run_unit(unit)
                self.assertEqual(result.returncode, 0, result.stderr)
                args = {s: json.loads(files[s + '.start'])['args'] for s in STEPS}
                resync = ['--resync'] if unit == UNITS[1] else []
                self.assertEqual(args, {
                    'lookup': ['api', '--paginate', 'user/orgs', '--jq', '.[].login'],
                    'profile': resync, 'impact': resync + [
                        '--cache-file', '/var/lib/gh-widgets/impact-cache.json'],
                    'responsiveness': ['--cache-file', '/var/lib/gh-widgets/impact-cache.json'],
                    'popularity': [],
                    'stars': ['--cache-file', '/var/lib/gh-widgets/stars-cache.json']})
                profile = json.loads(files['profile.start'])
                self.assertEqual(profile['orgs'], 'A,B')
                self.assertTrue(profile['cache'].endswith('/profile-cache.json'))

    def test_org_lookup_timing_is_opt_in_and_lookup_failure_propagates(self):
        for unit in UNITS:
            for timing in ('', '1'):
                with self.subTest(unit=unit, timing=timing):
                    result, _, _ = run_unit(unit, timing=timing)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, '')
                    if timing:
                        self.assertRegex(result.stdout, r'^orgs-lookup: \d+\.\d+s\n$')
                    else:
                        self.assertEqual(result.stdout, '')
                    result, _, files = run_unit(unit, fail='lookup', timing=timing)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn('profile.start', files)

    def test_token_file_reads_prefixed_first_and_bare_on_fallback(self):
        # ${GH_WIDGETS_GH_TOKEN_FILE-$GH_TOKEN_FILE} shares the helper's
        # semantics: unset falls back, present-even-empty wins. Each leg's
        # verdict flips exactly one leg of the expansion: 'both' dies on a
        # bare-first reader, 'bare' dies on prefixed-only, and
        # 'empty-prefixed' dies on ${VAR:-$DEFAULT}, whose empty-value
        # fallback the helper semantics refuse.
        scenarios = (('prefixed', True), ('bare', True), ('both', True),
                     ('empty-prefixed', False))
        for unit in UNITS:
            for token_name, served in scenarios:
                with self.subTest(unit=unit, token=token_name):
                    result, _, files = run_unit(unit, token_name=token_name)
                    if served:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertIn('profile.start', files)
                    else:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn('lookup failed', result.stderr)
                        self.assertNotIn('profile.start', files)
