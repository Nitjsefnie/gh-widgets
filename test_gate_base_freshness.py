"""The base-freshness gate: scripts/ci/gate_base_freshness.py.

The fixtures are real git repositories with a local bare origin, so the
fetch the gate performs is exercised against a filesystem remote — never
the network, and never against this repository or any of its worktrees:
their shared ``.git`` must never be made shallow or have its history move.
"""
import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def _load():
    path = ROOT / 'scripts/ci/gate_base_freshness.py'
    assert path.is_file(), f'missing gate script: {path}'
    spec = importlib.util.spec_from_file_location('gate_base_freshness', path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(root, *arguments):
    done = subprocess.run(
        ['git', '-C', str(root), *arguments], check=True,
        capture_output=True, text=True)
    return done.stdout


def _config(repo):
    # Identity by per-repo config, never by flags or environment: this
    # repository's discipline forbids -c user.name/-c user.email and
    # GIT_AUTHOR_*/GIT_COMMITTER_* on its own commits, and the fixtures
    # keep the same shape.
    _git(repo, 'config', 'user.name', 'fixture')
    _git(repo, 'config', 'user.email', 'fixture@example.com')
    _git(repo, 'config', 'commit.gpgsign', 'false')


def _commit(repo, subject, files):
    for relative, text in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8', newline='\n')
    _git(repo, 'add', '--', *files)
    _git(repo, 'commit', '-m', subject)


def _fixture(tmp):
    """origin (bare) + repo (a full clone whose origin is that bare repo)."""
    repo = Path(tmp) / 'repo'
    _git(tmp, 'init', '-b', 'main', str(repo))
    _config(repo)
    _commit(repo, 'base: seed the fixture', {
        '.github/workflows/actionlint.yml':
            'name: actionlint\n'
            'on: push\n'
            'jobs:\n'
            '  actionlint:\n'
            '    runs-on: ubuntu-latest\n'
            '    steps:\n'
            '      - run: ./actionlint -color .github/workflows/*.yml\n',
        '.github/workflows/tests.yml':
            'name: tests\n'
            'on: push\n'
            'jobs:\n'
            '  aggregate:\n'
            '    runs-on: ubuntu-latest\n'
            '    steps:\n'
            '      - run: python run_tests.py\n'
            '      - run: ./actionlint .github/workflows/*.yml\n',
        'run_tests.py': 'print("suite runner")\n',
        'README.md': '# fixture\n',
    })
    origin = Path(tmp) / 'origin.git'
    _git(tmp, 'clone', '--bare', str(repo), str(origin))
    _git(repo, 'remote', 'add', 'origin', str(origin))
    _git(repo, 'push', '-u', 'origin', 'main')
    return repo, origin


def _advance_main(tmp, origin, subject, files):
    """Push a commit to origin's main from a second clone."""
    other = Path(tmp) / 'other'
    _git(tmp, 'clone', str(origin), str(other))
    _config(other)
    _commit(other, subject, files)
    _git(other, 'push', 'origin', 'main')


class GateBaseFreshnessTests(unittest.TestCase):  # pylint: disable=too-many-public-methods
    """Rehearse the guard against isolated offline git repositories."""

    def setUp(self):
        # addCleanup owns the fixture until this test finishes.
        temporary = tempfile.TemporaryDirectory(  # pylint: disable=consider-using-with
            prefix='ghw-gate_base_freshness-')
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

    def test_required_job_and_gate_paths_on_this_repository(self):
        module = _load()
        assert module.REQUIRED_JOBS == ('aggregate',)
        assert module.BASE_BRANCH == 'main'
        assert module.gate_paths(ROOT) == [
            '.github/workflows/tests.yml',
            'scripts/ci/aggregate_gate.py',
            'scripts/ci/changes_detect.py',
        ]

    def test_green_when_the_head_carries_every_gate_commit(self):
        tmp = self.tmp
        repo, _ = _fixture(tmp)
        done = subprocess.run(
            [sys.executable, str(ROOT / 'scripts/ci/gate_base_freshness.py'),
             '--root', str(repo)], check=False, capture_output=True, text=True)
        assert done.returncode == 0, (done.stdout, done.stderr)
        assert 'carries every commit on main' in done.stdout
        assert 'BY NAME' in done.stdout

    def test_red_when_main_advances_a_gate_file(self):
        tmp = self.tmp
        repo, origin = _fixture(tmp)
        _advance_main(tmp, origin, 'ci: move the gate the head already read', {
            '.github/workflows/actionlint.yml':
                'name: actionlint\n'
                'on: push\n'
                'jobs:\n'
                '  actionlint:\n'
                '    runs-on: changed\n'
                '    steps:\n'
                '      - run: ./actionlint -color .github/workflows/*.yml\n',
        })
        done = subprocess.run(
            [sys.executable, str(ROOT / 'scripts/ci/gate_base_freshness.py'),
             '--root', str(repo)], check=False, capture_output=True, text=True)
        assert done.returncode == 1, (done.stdout, done.stderr)
        assert 'main holds 1 commit this head does not' in done.stdout
        assert 'ci: move the gate the head already read' in done.stdout
        assert '.github/workflows/actionlint.yml' in done.stdout
        assert 'Rebase onto main' in done.stdout

    # The workflow step runs the script from the checkout root with no --root,
    # so the default root has to be the repository root. git resolves pathspecs
    # against the `git -C` chdir, so a default root at scripts/ would empty the
    # stale listing and print the green line over a stale head. The script is
    # COPIED to a scripts/ci/ depth inside the fixture rather than run from this
    # repository: only a copy at that depth exercises the default-root
    # derivation, and the fixture's own git is never this repository's.
    def test_runs_without_root_at_scripts_ci_depth_reds_a_stale_head(self):
        tmp = self.tmp
        repo, origin = _fixture(tmp)
        _advance_main(tmp, origin, 'ci: main moves a gate file the head has not read', {
            '.github/workflows/actionlint.yml':
                'name: actionlint\n'
                'on: push\n'
                'jobs:\n'
                '  actionlint:\n'
                '    runs-on: changed\n'
                '    steps:\n'
                '      - run: ./actionlint -color .github/workflows/*.yml\n',
        })
        script = repo / 'scripts/ci/gate_base_freshness.py'
        script.parent.mkdir(parents=True)
        script.write_text(
            (ROOT / 'scripts/ci/gate_base_freshness.py').read_text(encoding='utf-8'),
            encoding='utf-8', newline='\n')
        done = subprocess.run(
            [sys.executable, str(script)], cwd=str(repo), check=False,
            capture_output=True, text=True)
        assert done.returncode == 1, (done.stdout, done.stderr)
        assert 'main holds 1 commit this head does not' in done.stdout
        assert 'ci: main moves a gate file the head has not read' in done.stdout
        assert '.github/workflows/actionlint.yml' in done.stdout
        assert 'Rebase onto main' in done.stdout

    def test_green_when_main_advances_a_non_gate_file(self):
        tmp = self.tmp
        repo, origin = _fixture(tmp)
        _advance_main(tmp, origin, 'fix: a file no required check reads by name', {
            '.pylintrc': '[MESSAGES CONTROL]\ndisable=\n',
        })
        done = subprocess.run(
            [sys.executable, str(ROOT / 'scripts/ci/gate_base_freshness.py'),
             '--root', str(repo)], check=False, capture_output=True, text=True)
        assert done.returncode == 0, (done.stdout, done.stderr)
        assert 'carries every commit on main' in done.stdout

    def test_red_names_a_merge_commit(self):
        tmp = self.tmp
        _, origin = _fixture(tmp)
        # The stale clone is taken BEFORE the merge lands on origin: its own
        # fetch_base is what brings the merge into view, which is the situation
        # the check exists to answer.
        behind = Path(tmp) / 'behind'
        _git(tmp, 'clone', str(origin), str(behind))
        _config(behind)
        side = Path(tmp) / 'side'
        _git(tmp, 'clone', str(origin), str(side))
        _config(side)
        _commit(side, 'ci: the side parent', {
            '.github/workflows/actionlint.yml':
                'name: actionlint\non: push\njobs:\n  actionlint:\n'
                '    runs-on: side\n    steps:\n'
                '      - run: ./actionlint -color .github/workflows/*.yml\n'})
        # adv diverges BEFORE the side push, so the pull must merge: origin's
        # main then carries a real merge commit for the stale listing to name.
        adv = Path(tmp) / 'adv'
        _git(tmp, 'clone', str(origin), str(adv))
        _config(adv)
        _commit(adv, 'ci: the second parent', {
            '.github/workflows/tests.yml':
                'name: tests\non: push\njobs:\n  aggregate:\n'
                '    steps:\n      - run: python run_tests.py\n'})
        _git(side, 'push', 'origin', 'main')
        _git(adv, 'pull', '--no-rebase', 'origin', 'main')
        _git(adv, 'push', 'origin', 'main')
        done = subprocess.run(
            [sys.executable, str(ROOT / 'scripts/ci/gate_base_freshness.py'),
             '--root', str(behind)], check=False, capture_output=True, text=True)
        assert done.returncode == 1, (done.stdout, done.stderr)
        assert '(a merge commit, which git names no file for' in done.stdout
        assert 'ci: the side parent' in done.stdout
        assert 'ci: the second parent' in done.stdout

    def test_refuses_when_a_required_job_is_missing(self):
        tmp = self.tmp
        repo = Path(tmp) / 'repo'
        _git(tmp, 'init', '-b', 'main', str(repo))
        _config(repo)
        _commit(repo, 'base: seed', {
            '.github/workflows/actionlint.yml':
                'name: actionlint\non: push\njobs:\n  actionlint:\n'
                '    steps:\n      - run: cat README.md\n',
            'README.md': '# fixture\n',
        })
        module = _load()
        try:
            module.gate_paths(repo)
        except module.GateError as refusal:
            assert 'defines the required job' in str(refusal)
        else:
            raise AssertionError('a missing required job must refuse, not pass')

    def test_refuses_shapes_it_cannot_read(self):
        module = _load()
        fixtures = {
            'a flow-mapping jobs:':
                'name: actionlint\n'
                'jobs: {actionlint: {runs-on: ubuntu-latest}}\n',
            'duplicate job names:':
                'name: actionlint\n'
                'jobs:\n'
                '  actionlint:\n'
                '    steps:\n'
                '      - run: cat README.md\n'
                '  actionlint:\n'
                '    steps:\n'
                '      - run: cat README.md\n',
            'a flow `steps:` value:':
                'name: actionlint\n'
                'jobs:\n'
                '  actionlint:\n'
                '    steps: [{run: cat README.md}]\n',
        }
        for why, text in fixtures.items():
            try:
                module.workflow_steps(text, 'fixture.yml')
            except module.WorkflowError:
                continue
            raise AssertionError(f'{why} must refuse, not parse')

    def test_expressions_are_stripped_before_paths_are_taken(self):
        tmp = self.tmp
        module = _load()
        repo = Path(tmp) / 'repo'
        _git(tmp, 'init', '-b', 'main', str(repo))
        _config(repo)
        _commit(repo, 'base: seed', {
            'run_tests.py': 'print("suite runner")\n',
            'matrix.python': 'not a path a step reads\n',
            '.github/workflows/actionlint.yml':
                'name: actionlint\non: push\njobs:\n  actionlint:\n'
                '    steps:\n'
                '      - run: python -m pytest ${{ matrix.python }}\n',
            '.github/workflows/tests.yml':
                'name: tests\non: push\njobs:\n  aggregate:\n'
                '    steps:\n      - run: python run_tests.py ${{ matrix.python }}\n'
                '      - run: ./actionlint .github/workflows/*.yml\n',
        })
        assert 'matrix.python' not in module.gate_paths(repo), (
            'the expression is stripped before the run text is scanned, so its '
            'identifiers never resolve to a tracked file')

    def test_whole_tree_spelling_adds_no_paths(self):
        tmp = self.tmp
        module = _load()
        repo = Path(tmp) / 'repo'
        _git(tmp, 'init', '-b', 'main', str(repo))
        _config(repo)
        marker = ('      - run: git grep -nI -E '
                  '"^(<{7}( |$)|>{7}( |$)|={7}$)" -- .\n')
        _commit(repo, 'base: seed', {
            '.github/workflows/actionlint.yml':
                'name: actionlint\non: push\njobs:\n  actionlint:\n'
                f'    steps:\n{marker}',
            '.github/workflows/tests.yml':
                'name: tests\non: push\njobs:\n  aggregate:\n'
                f'    steps:\n{marker}',
        })
        assert module.gate_paths(repo) == ['.github/workflows/tests.yml']

    def test_a_shallow_clone_is_unshallowed_before_comparing(self):
        tmp = self.tmp
        _, origin = _fixture(tmp)
        _advance_main(tmp, origin, 'ci: main moved while the head was shallow', {
            '.github/workflows/actionlint.yml':
                'name: actionlint\non: push\njobs:\n  actionlint:\n'
                '    runs-on: changed\n    steps:\n'
                '      - run: ./actionlint -color .github/workflows/*.yml\n',
        })
        behind = Path(tmp) / 'behind'
        # --depth is ignored for plain-path clones, so the fixture clones over
        # file:// and stays a separate repository from this one throughout.
        _git(tmp, 'clone', '--depth', '1', origin.as_uri(), str(behind))
        _config(behind)
        assert _git(behind, 'rev-parse', '--is-shallow-repository').strip() == 'true'
        module = _load()
        module.fetch_base(behind)
        assert _git(behind, 'rev-parse', '--is-shallow-repository').strip() == 'false', (
            'the fetch must deepen a shallow checkout before the comparison')

    def test_exit_codes_and_argv(self):
        tmp = self.tmp
        repo, _ = _fixture(tmp)
        script = ROOT / 'scripts/ci/gate_base_freshness.py'
        unknown = subprocess.run(
            [sys.executable, str(script), '--root', str(repo), '--nonsense'],
            check=False, capture_output=True, text=True)
        assert unknown.returncode == 2
        assert 'usage' in unknown.stderr
        missing_value = subprocess.run(
            [sys.executable, str(script), '--root'], check=False,
            capture_output=True, text=True)
        assert missing_value.returncode == 2
        assert 'usage' in missing_value.stderr

    def test_the_green_line_names_its_reach_limit(self):
        tmp = self.tmp
        repo, _ = _fixture(tmp)
        module = _load()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = module.check(repo)
        assert code == 0
        out = buffer.getvalue()
        assert 'reading every tracked file' in out, out
        assert 'merge-marker step' in out, out
        assert 'named reach limit' in out, out
        assert 'BY NAME' in out, out

    # --- in-process coverage of the parser and comparison helpers ---------------
    def test_git_refusal_names_what_was_attempted(self):
        module = _load()
        try:
            module.git(ROOT, 'rev-parse', '--verify', 'definitely-not-a-ref^{commit}',
                       what='resolve the impossible')
        except module.GateError as refusal:
            assert 'cannot resolve the impossible' in str(refusal)
            assert 'exited' in str(refusal)
        else:
            raise AssertionError('a failing git call must refuse')

    def test_block_scalar_run_text_is_read_verbatim(self):
        module = _load()
        text = ('name: actionlint\n'
                'jobs:\n'
                '  actionlint:\n'
                '    steps:\n'
                '      - name: a step\n'
                '        run: |\n'
                '          echo one\n'
                '          # a comment bash receives\n'
                '          grep README.md\n')
        steps = module.workflow_steps(text, 'w.yml')['actionlint']
        assert [step['run'] for step in steps] == [
            'echo one\n# a comment bash receives\ngrep README.md']

    def test_uses_and_with_fields_are_read_and_skipped(self):
        module = _load()
        text = ('name: actionlint\n'
                'jobs:\n'
                '  actionlint:\n'
                '    strategy:\n'
                '      matrix: [a, b]\n'
                '    steps:\n'
                '      - uses: actions/checkout@1111111111111111111111111111111111111111 # v1\n'
                '        with:\n'
                '          fetch-depth: 0\n'
                '      - uses: ./\n')
        steps = module.workflow_steps(text, 'w.yml')['actionlint']
        assert steps[0]['uses'] == 'actions/checkout@1111111111111111111111111111111111111111'
        assert steps[1]['uses'] == './'

    def test_resolve_arms(self):
        module = _load()
        files = ('action.yml', '.github/workflows/tests.yml',
                 '.github/workflows/actionlint.yml')
        assert module.resolve('action.yml', files) == ('action.yml',)
        assert module.resolve('./action.yml', files) == ('action.yml',)
        assert module.resolve('action.yml/', files) == ('action.yml',)
        assert module.resolve('.github/workflows', files) == files[1:]
        assert module.resolve('absent.yml', files) == ()
        assert module.resolve('.', files) == ()

    def test_candidates_strip_expressions(self):
        module = _load()
        # The strip removes the expression, so its identifiers never resolve to
        # a tracked file; the text around it is still scanned as-is.
        assert module.candidates('cat ${{ matrix.python }}/x.yml') == [
            'cat', '/x.yml']
        assert module.candidates('python run_tests.py') == [
            'python', 'run_tests.py']

    def test_stale_commits_refuses_empty_and_oversized_paths(self):
        module = _load()
        try:
            module.stale_commits(ROOT, 'HEAD', 'origin/main', [])
        except module.GateError as refusal:
            assert 'empty' in str(refusal)
        else:
            raise AssertionError('an empty path set must refuse')
        # Sized against the real ceiling: the test never rewrites a module
        # constant, so the refusal is proven against the shipped byte limit.
        oversized = ['a' * (module.PATHSPECS_MAX_BYTES + 1)]
        try:
            module.stale_commits(ROOT, 'HEAD', 'origin/main', oversized)
        except module.GateError as refusal:
            assert 'bytes' in str(refusal)
        else:
            raise AssertionError('an oversized path set must refuse')

    def test_gate_paths_refuses_a_tree_with_no_files(self):
        tmp = self.tmp
        repo = Path(tmp) / 'repo'
        _git(tmp, 'init', '-b', 'main', str(repo))
        _config(repo)
        _git(repo, 'commit', '--allow-empty', '-m', 'base: an empty tree')
        module = _load()
        try:
            module.gate_paths(repo)
        except module.GateError as refusal:
            assert 'tracks no files' in str(refusal)
        else:
            raise AssertionError('an empty tree must refuse')

    def test_check_reports_stale_commits_in_process(self):
        tmp = self.tmp
        repo, origin = _fixture(tmp)
        _advance_main(tmp, origin, 'ci: a gate move the head lacks', {
            '.github/workflows/actionlint.yml':
                'name: actionlint\non: push\njobs:\n  actionlint:\n'
                '    steps:\n'
                '      - run: ./actionlint -color .github/workflows/*.yml\n'})
        module = _load()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = module.check(repo)
        assert code == 1
        out = buffer.getvalue()
        assert 'main holds 1 commit this head does not:' in out
        assert 'Rebase onto main and push again' in out
        assert '.github/workflows/actionlint.yml' in out

    def test_main_returns_the_check_exit_code(self):
        tmp = self.tmp
        repo, _ = _fixture(tmp)
        module = _load()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
            code = module.main(['gate_base_freshness.py', '--root', str(repo)])
        assert code == 0
        assert 'carries every commit on main' in buffer.getvalue()

    # --- parser arms: every refusal refuses, every loop control line runs -------
    def test_every_tracked_workflow_parses(self):
        module = _load()
        for name in module.workflow_names(module.tracked_files(ROOT)):
            text = module.git(ROOT, 'cat-file', 'blob', f'HEAD:{name}',
                              what=f'read {name}')
            jobs = module.workflow_steps(text, name)
            assert jobs, name

    def test_parser_refusal_arms(self):
        module = _load()
        cases = {
            'a job entry at the wrong indent':
                'name: w\njobs:\n   actionlint:\n',
            'a job key with an inline value':
                'name: w\njobs:\n  actionlint: {runs-on: ubuntu-latest}\n',
            'a job-level key at the wrong indent':
                'name: w\njobs:\n  actionlint:\n   runs-on: ubuntu-latest\n',
            'a step entry without a dash':
                'name: w\njobs:\n  actionlint:\n    steps:\n'
                '        run: echo\n',
            'a dash line that is not a key':
                'name: w\njobs:\n  actionlint:\n    steps:\n          - 123\n',
            'a step key at the wrong indent':
                'name: w\njobs:\n  actionlint:\n    steps:\n'
                '      - run: echo\n         x: 1\n',
            'a non-mapping step key':
                'name: w\njobs:\n  actionlint:\n    steps:\n'
                '      - run: echo\n          [1, 2]\n',
        }
        for why, text in cases.items():
            try:
                module.workflow_steps(text, 'w.yml')
            except module.WorkflowError:
                continue
            raise AssertionError(f'{why} must refuse, not parse')

    def test_block_scalar_and_block_end_loop_arms(self):
        module = _load()
        text = ('name: w\n'
                'jobs:\n'
                '  actionlint:\n'
                '    # a comment inside the job block\n'
                '\n'
                '    runs-on: ubuntu-latest\n'
                '    steps:\n'
                '      - run: |\n'
                '          echo one\n'
                '        name: renamed\n'
                'permissions:\n'
                '  contents: read\n')
        jobs = module.workflow_steps(text, 'w.yml')
        steps = jobs['actionlint']
        assert steps[0]['run'] == 'echo one'
        assert steps[0]['uses'] is None

    def test_whole_action_use_takes_the_directory_whole(self):
        tmp = self.tmp
        repo = Path(tmp) / 'repo'
        _git(tmp, 'init', '-b', 'main', str(repo))
        _config(repo)
        _commit(repo, 'base: seed', {
            'action.yml': 'name: the action\n',
            'README.md': '# fixture\n',
            '.github/workflows/actionlint.yml':
                'name: actionlint\non: push\njobs:\n  aggregate:\n'
                '    steps:\n      - uses: ./\n',
            '.github/workflows/tests.yml':
                'name: tests\non: push\njobs:\n  aggregate:\n'
                '    steps:\n      - run: python run_tests.py\n'
                '      - run: ./actionlint .github/workflows/*.yml\n',
        })
        module = _load()
        assert 'README.md' in module.gate_paths(repo), (
            'uses: ./ reads the action directory whole, so every tracked file '
            'is a gate-read file')

    def test_check_reports_a_merge_commit_in_process(self):
        tmp = self.tmp
        _, origin = _fixture(tmp)
        behind = Path(tmp) / 'behind'
        _git(tmp, 'clone', str(origin), str(behind))
        _config(behind)
        side = Path(tmp) / 'side'
        _git(tmp, 'clone', str(origin), str(side))
        _config(side)
        _commit(side, 'ci: the side parent', {
            '.github/workflows/actionlint.yml':
                'name: actionlint\non: push\njobs:\n  actionlint:\n'
                '    steps:\n'
                '      - run: ./actionlint -color .github/workflows/*.yml\n'})
        adv = Path(tmp) / 'adv'
        _git(tmp, 'clone', str(origin), str(adv))
        _config(adv)
        _commit(adv, 'ci: the second parent', {
            '.github/workflows/tests.yml':
                'name: tests\non: push\njobs:\n  aggregate:\n'
                '    steps:\n      - run: python run_tests.py\n'})
        _git(side, 'push', 'origin', 'main')
        _git(adv, 'pull', '--no-rebase', 'origin', 'main')
        _git(adv, 'push', 'origin', 'main')
        module = _load()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = module.check(behind)
        assert code == 1
        assert '(a merge commit, which git names no file for' in buffer.getvalue()

    def test_main_handlers(self):
        tmp = self.tmp
        repo = Path(tmp) / 'repo'
        _git(tmp, 'init', '-b', 'main', str(repo))
        _config(repo)
        module = _load()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
            code = module.main(['gate_base_freshness.py', '--root', str(repo)])
        assert code == 1
        saved_path = os.environ['PATH']
        try:
            os.environ['PATH'] = ''
            with contextlib.redirect_stderr(io.StringIO()):
                code = module.main(['gate_base_freshness.py', '--root', str(ROOT)])
            assert code == 1
        finally:
            os.environ['PATH'] = saved_path

    def test_aggregate_workflow_runner_and_imported_selector_are_gate_files(self):
        for changed_path in ('.github/workflows/tests.yml',
                             'scripts/ci/aggregate_gate.py',
                             'scripts/ci/changes_detect.py'):
            with self.subTest(path=changed_path), tempfile.TemporaryDirectory() as tmp:
                repo, origin = _fixture(Path(tmp))
                workflow = ('name: tests\njobs:\n  aggregate:\n'
                            '    steps:\n'
                            '      - run: python3 scripts/ci/aggregate_gate.py\n')
                files = {
                    '.github/workflows/tests.yml': workflow,
                    'scripts/ci/aggregate_gate.py':
                        'from scripts.ci import changes_detect as cd\n',
                    'scripts/ci/changes_detect.py': 'GATES = {}\n',
                }
                _commit(repo, 'ci: seed the aggregate', files)
                _git(repo, 'push', 'origin', 'main')
                files[changed_path] += '# main advances\n'
                _advance_main(Path(tmp), origin, 'ci: move aggregate input',
                              {changed_path: files[changed_path]})
                done = subprocess.run(
                    [sys.executable, str(ROOT / 'scripts/ci/gate_base_freshness.py'),
                     '--root', str(repo)], capture_output=True, text=True,
                    check=False)
                self.assertEqual(done.returncode, 1, done.stderr)
                self.assertIn(changed_path, done.stdout)

    def test_local_import_closure_handles_cycles_and_ignores_external_modules(self):
        repo, _ = _fixture(self.tmp)
        _commit(repo, 'ci: import cycle', {
            '.github/workflows/tests.yml':
                'name: tests\njobs:\n  aggregate:\n    steps:\n'
                '      - run: python3 scripts/ci/aggregate_gate.py\n',
            'scripts/ci/aggregate_gate.py':
                'import json\nfrom scripts.ci import changes_detect\n',
            'scripts/ci/changes_detect.py':
                'from scripts.ci.aggregate_gate import check\n',
        })
        self.assertEqual(_load().gate_paths(repo), [
            '.github/workflows/tests.yml',
            'scripts/ci/aggregate_gate.py', 'scripts/ci/changes_detect.py'])

    def test_non_ascii_workflow_path_cannot_leave_the_freshness_set(self):
        repo, origin = _fixture(self.tmp)
        path = '.github/workflows/über.yml'
        text = ('name: other\njobs:\n  aggregate:\n    steps:\n'
                '      - run: cat README.md\n')
        _commit(repo, 'ci: seed unicode workflow', {path: text})
        _git(repo, 'push', 'origin', 'main')
        _advance_main(self.tmp, origin, 'ci: change unicode workflow',
                      {path: text + '# changed\n'})
        module = _load()
        self.assertIn(path, module.gate_paths(repo))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(module.check(repo), 1)


if __name__ == "__main__":
    unittest.main()
