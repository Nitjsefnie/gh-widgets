"""The commit-scope gate: scripts/ci/commit_scopes.py.

Fixtures are real git repositories with a local bare origin, as in
test_gate_base_freshness.py: the outgoing-range comparison runs
against a filesystem remote, never the network, and never against this
repository or any of its worktrees.
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

from test_gate_base_freshness import _git, _config, _commit

ROOT = Path(__file__).resolve().parent


def _load():
    path = ROOT / 'scripts/ci/commit_scopes.py'
    assert path.is_file(), f'missing gate script: {path}'
    spec = importlib.util.spec_from_file_location('commit_scopes', path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fixture(tmp, *, head_subject, base_files=None):
    """origin (bare) + repo; the head commit's subject is the one under test."""
    repo = Path(tmp) / 'repo'
    _git(tmp, 'init', '-b', 'main', str(repo))
    _config(repo)
    files = {
        '.github/workflows/actionlint.yml':
            'name: actionlint\non: push\njobs:\n  actionlint:\n'
            '    steps:\n      - run: cat README.md\n',
        'README.md': '# fixture\n',
    }
    if base_files:
        files.update(base_files)
    _commit(repo, 'base: seed the fixture', files)
    origin = Path(tmp) / 'origin.git'
    _git(tmp, 'clone', '--bare', str(repo), str(origin))
    _git(repo, 'remote', 'add', 'origin', str(origin))
    _git(repo, 'push', '-u', 'origin', 'main')
    _commit(repo, head_subject, {'README.md': '# changed\n'})
    return repo, origin


def _run_gate(tmp, repo, *extra):
    del tmp
    return subprocess.run(
        [sys.executable, str(ROOT / 'scripts/ci/commit_scopes.py'),
         '--root', str(repo), *extra],
        check=False, capture_output=True, text=True)


class CommitScopesTests(unittest.TestCase):  # pylint: disable=too-many-public-methods
    """Rehearse the guard against isolated offline git repositories."""

    def setUp(self):
        # addCleanup owns the fixture until this test finishes.
        temporary = tempfile.TemporaryDirectory(  # pylint: disable=consider-using-with
            prefix='ghw-commit_scopes-')
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

    def test_names_derived_from_this_repository(self):
        module = _load()
        assert module.workflow_name_set(ROOT) == {
            'tests', 'claim', 'pr gate', 'pr-gate', 'secrets', 'release',
            'coverage comment', 'coverage-comment',
            'coverage-ratchet', 'audit', 'codeql', 'scorecard',
            'targeted-blame-audit',
            'gitfame-resync-memory',
            'gitfame-pool-probe',
        }

    def test_violation_red_for_workflow_scope_with_non_ci_type(self):
        tmp = self.tmp
        repo, _ = _fixture(tmp, head_subject='fix(actionlint): rewritten flag')
        done = _run_gate(tmp, repo)
        assert done.returncode == 1, (done.stdout, done.stderr)
        assert 'a workflow-name scope with a type other than `ci`' in done.stdout
        assert 'fix(actionlint)' in done.stdout
        assert ('scope `actionlint` is the name of a workflow under '
                '.github/workflows/') in done.stdout

    def test_ci_type_on_workflow_scope_passes(self):
        tmp = self.tmp
        repo, _ = _fixture(tmp, head_subject='ci(actionlint): the workflow moves')
        done = _run_gate(tmp, repo)
        assert done.returncode == 0, (done.stdout, done.stderr)
        assert 'No commit pairs a workflow-name scope' in done.stdout

    def test_non_workflow_scope_with_any_type_passes(self):
        tmp = self.tmp
        repo, _ = _fixture(tmp, head_subject='fix(ci): not a workflow name')
        done = _run_gate(tmp, repo)
        assert done.returncode == 0, (done.stdout, done.stderr)
        assert 'No commit pairs a workflow-name scope' in done.stdout

    def test_subjects_that_do_not_parse_are_listed_not_failed(self):
        tmp = self.tmp
        repo, _ = _fixture(
            tmp, head_subject='Fix round 1: honest comments')
        done = _run_gate(tmp, repo)
        assert done.returncode == 0, (done.stdout, done.stderr)
        assert 'not examined' in done.stdout
        sha = _git(repo, 'rev-parse', '--short=7', 'HEAD')
        assert sha.strip() in done.stdout

    def test_only_outgoing_range_is_judged(self):
        tmp = self.tmp
        repo, _ = _fixture(tmp, head_subject='fix(actionlint): rewritten flag')
        # Push the violation, then put a clean commit on top: only the outgoing
        # range is ever judged, so the merged violation is never re-tried over
        # commits nobody can amend.
        _git(repo, 'push', 'origin', 'main')
        _commit(repo, 'fix(readme): a clean head commit', {'README.md': '# again\n'})
        done = _run_gate(tmp, repo)
        assert done.returncode == 0, (done.stdout, done.stderr)
        assert 'No commit pairs a workflow-name scope' in done.stdout
        assert 'Examined 1 commit subject in origin/main..HEAD' in done.stdout

    def test_quoted_name_refused(self):
        module = _load()
        try:
            module.workflow_name(
                'w.yml', 'name: "tests"\njobs:\n  suites:\n    steps: []\n')
        except module.GateError as refusal:
            assert 'not a plain scalar' in str(refusal)
        else:
            raise AssertionError('a quoted `name:` must refuse, not parse')

    def test_missing_name_refused(self):
        module = _load()
        text = 'on: push\njobs:\n  suites:\n    steps: []\n'
        try:
            module.workflow_name('w.yml', text)
        except module.GateError as refusal:
            assert 'no top-level `name:`' in str(refusal)
        else:
            raise AssertionError('a workflow with no `name:` must refuse')

    def test_trailing_comment_refused(self):
        module = _load()
        text = 'name: tests # prose\njobs:\n  suites:\n    steps: []\n'
        try:
            module.workflow_name('w.yml', text)
        except module.GateError as refusal:
            assert 'trailing comment' in str(refusal)
        else:
            raise AssertionError('a trailing comment must refuse, not strip')

    def test_name_continuation_refused(self):
        module = _load()
        text = 'name: tests\n  continued: yes\njobs:\n  suites:\n    steps: []\n'
        try:
            module.workflow_name('w.yml', text)
        except module.GateError as refusal:
            assert 'continues onto an indented line' in str(refusal)
        else:
            raise AssertionError('a continued name must refuse, not join')

    def test_two_names_refused(self):
        module = _load()
        text = 'name: a\nname: b\njobs:\n  suites:\n    steps: []\n'
        try:
            module.workflow_name('w.yml', text)
        except module.GateError as refusal:
            assert 'cannot be established' in str(refusal)
        else:
            raise AssertionError('two names must refuse, not guess')

    def test_job_level_name_is_not_collected(self):
        module = _load()
        text = ('name: pr gate\n'
                'on: push\n'
                'jobs:\n'
                '  suites:\n'
                '    name: scorecard\n'
                '    steps:\n'
                '      - run: python run_tests.py\n')
        assert module.workflow_name('w.yml', text) == 'pr gate'

    def test_no_exemption_for_any_scope(self):
        tmp = self.tmp
        # gh-widgets has no script sharing a workflow's name, so claim's exemption
        # clause does not port: the rule reaches every workflow-name scope.
        module = _load()
        text = ('name: claim\n'
                'on: push\n'
                'jobs:\n'
                '  claim:\n'
                '    steps:\n'
                '      - run: cat README.md\n')
        assert module.workflow_name('w.yml', text) == 'claim'
        repo, _ = _fixture(
            tmp,
            head_subject='fix(claim): not the workflow, so the rule fires',
            base_files={
                '.github/workflows/claim.yml':
                    'name: claim\non: push\njobs:\n  claim:\n'
                    '    steps:\n      - run: cat README.md\n'})
        done = _run_gate(tmp, repo)
        assert done.returncode == 1, (done.stdout, done.stderr)
        assert 'fix(claim)' in done.stdout

    def test_conventional_subjects_parse_including_breaking_markers(self):
        module = _load()
        for subject, expected_type in (
                ('ci(tests)!: a breaking scope marker', 'ci'),
                ('ci!(tests): a breaking type marker', 'ci'),
                ('ci(tests): plain', 'ci'),
                ('fix(ci): not a workflow name', 'fix')):
            match = module.SUBJECT.match(subject)
            assert match is not None, subject
            assert match['type'] == expected_type, subject

    def test_exit_codes_and_argv(self):
        tmp = self.tmp
        repo, _ = _fixture(tmp, head_subject='ci: clean')
        done = _run_gate(tmp, repo, '--nonsense')
        assert done.returncode == 2
        assert 'usage' in done.stderr

    def test_the_green_line_states_its_reach(self):
        tmp = self.tmp
        repo, _ = _fixture(tmp, head_subject='ci: clean')
        module = _load()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = module.check(repo)
        assert code == 0
        out = buffer.getvalue()
        assert 'Only the OUTGOING range is examined' in out
        assert 'never a failure' in out
        assert 'listed below' in out
        assert 'No commit pairs a workflow-name scope' in out

    def test_contributing_names_the_rule_the_gate_enforces(self):
        contributing = ' '.join((ROOT / 'CONTRIBUTING.md').read_text(
            encoding='utf-8').split())
        assert 'only with the `ci` type' in contributing, (
            'CONTRIBUTING must state the workflow-name scope rule this gate '
            'enforces, so the prose and the gate cannot drift apart silently')
        assert 'commit_scopes.py' in contributing, (
            'CONTRIBUTING must name the gate that enforces the rule')

    # --- in-process coverage of the refusal and comparison helpers --------------
    def test_git_refusal_names_what_was_attempted(self):
        module = _load()
        try:
            module.git(ROOT, 'rev-parse', '--verify', 'definitely-not-a-ref^{commit}',
                       what='resolve the impossible')
        except module.GateError as refusal:
            assert 'cannot resolve the impossible' in str(refusal)
        else:
            raise AssertionError('a failing git call must refuse')

    def test_name_value_refusals(self):
        module = _load()
        for text, fragment in (
                ('name: \njobs:\n  suites:\n    steps: []\n', 'carries no value'),
                ('name: >\n  folded\njobs:\n  suites:\n    steps: []\n',
                 'not a plain scalar')):
            try:
                module.workflow_name('w.yml', text)
            except module.GateError as refusal:
                assert fragment in str(refusal), (fragment, str(refusal))
            else:
                raise AssertionError(f'{fragment!r} must refuse')

    def test_fetch_base_brings_main_in(self):
        tmp = self.tmp
        repo, _ = _fixture(tmp, head_subject='ci: clean')
        _git(repo, 'push', 'origin', 'main')
        module = _load()
        module.fetch_base(repo)
        base = _git(repo, 'rev-parse', '--verify', 'refs/remotes/origin/main').strip()
        head = _git(repo, 'rev-parse', 'HEAD').strip()
        assert base == head

    def test_check_reports_violations_in_process(self):
        tmp = self.tmp
        repo, _ = _fixture(
            tmp, head_subject='fix(actionlint): rewritten flag')
        module = _load()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = module.check(repo)
        assert code == 1
        out = buffer.getvalue()
        assert '1 commit in origin/main..HEAD pairs a workflow-name scope' in out
        assert 'type `fix` is not `ci`.' in out
        assert 'or take a scope that names what' in out

    def test_check_lists_unexamined_subjects_in_process(self):
        tmp = self.tmp
        repo, _ = _fixture(tmp, head_subject='Fix round 1: honest comments')
        module = _load()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = module.check(repo)
        assert code == 0
        out = buffer.getvalue()
        assert 'Examined 0 commit subjects in origin/main..HEAD; 1 not examined.' in out
        assert 'Fix round 1: honest comments' in out

    def test_main_returns_the_check_exit_code(self):
        tmp = self.tmp
        repo, _ = _fixture(tmp, head_subject='ci(actionlint): the workflow moves')
        module = _load()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
            code = module.main(['commit_scopes.py', '--root', str(repo)])
        assert code == 0
        assert 'No commit pairs a workflow-name scope' in buffer.getvalue()

    def test_main_usage_refusal(self):
        module = _load()
        with contextlib.redirect_stderr(io.StringIO()):
            code = module.main(['commit_scopes.py', '--nonsense'])
        assert code == 2

    def test_main_handlers_and_fetch_arms(self):
        tmp = self.tmp
        repo = Path(tmp) / 'repo'
        _git(tmp, 'init', '-b', 'main', str(repo))
        _config(repo)
        module = _load()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
            code = module.main(['commit_scopes.py', '--root', str(repo)])
        assert code == 1
        saved_path = os.environ['PATH']
        try:
            os.environ['PATH'] = ''
            with contextlib.redirect_stderr(io.StringIO()):
                code = module.main(['commit_scopes.py', '--root', str(ROOT)])
            assert code == 1
        finally:
            os.environ['PATH'] = saved_path

    def test_fetch_base_deepens_a_shallow_clone(self):
        tmp = self.tmp
        _, origin = _fixture(tmp, head_subject='ci: clean')
        shallow = Path(tmp) / 'shallow'
        # file:// keeps --depth from being ignored; the fixture is a separate
        # repository from this one and its worktrees throughout.
        _git(tmp, 'clone', '--depth', '1', origin.as_uri(), str(shallow))
        _config(shallow)
        module = _load()
        module.fetch_base(shallow)
        assert _git(shallow, 'rev-parse', '--is-shallow-repository').strip() == 'false'

    def test_clean_main_history_passes(self):
        repo, _ = _fixture(self.tmp, head_subject='ci(tests): clean')
        _git(repo, 'push', 'origin', 'main')
        done = _run_gate(self.tmp, repo)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('Examined 0 commit subjects', done.stdout)

    def test_workflow_stem_is_a_scope_even_when_display_name_has_spaces(self):
        repo, _ = _fixture(
            self.tmp, head_subject='fix(pr-gate): changed workflow',
            base_files={'.github/workflows/pr-gate.yml':
                        'name: pr gate\njobs:\n  policy:\n    steps: []\n'})
        done = _run_gate(self.tmp, repo)
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn('scope `pr-gate`', done.stdout)

    def test_unusual_workflow_paths_cannot_disappear_from_the_scope_set(self):
        for stem in ('über', 'tab\tname', 'line\nname'):
            # Windows rejects tab/newline filenames; Unicode still runs there.
            if sys.platform == 'win32' and ('\t' in stem or '\n' in stem):
                continue
            with self.subTest(stem=stem), tempfile.TemporaryDirectory() as tmp:
                repo, _ = _fixture(
                    Path(tmp), head_subject='fix(unusual): must fail',
                    base_files={f'.github/workflows/{stem}.yml':
                                'name: unusual\njobs:\n  check:\n    steps: []\n'})
                # Both quotePath settings must enumerate byte-exact paths.
                for quote_path in ('true', 'false'):
                    _git(repo, 'config', 'core.quotePath', quote_path)
                    done = _run_gate(tmp, repo)
                    self.assertEqual(done.returncode, 1, done.stdout)
                    self.assertIn('fix(unusual)', done.stdout)

    @unittest.skipIf(sys.platform == 'win32',
                     'Windows rejects CR/LF filenames')
    def test_cr_and_lf_workflow_paths_cannot_alias_and_bypass_scope_rule(self):
        repo = self.tmp / 'repo'
        _git(self.tmp, 'init', '-b', 'main', str(repo))
        _config(repo)
        # Text-mode ls-tree turns the CR spelling into the LF spelling,
        # reading the decoy twice and losing the protected workflow's name.
        _commit(repo, 'ci: seed the two distinct workflow paths', {
            '.github/workflows/a\rb.yml': 'name: protected\njobs: {}\n',
            '.github/workflows/a\nb.yml': 'name: decoy\njobs: {}\n',
        })
        origin = self.tmp / 'origin.git'
        _git(self.tmp, 'clone', '--bare', str(repo), str(origin))
        _git(repo, 'remote', 'add', 'origin', str(origin))
        _git(repo, 'commit', '--allow-empty', '-m',
             'fix(protected): forbidden workflow scope')
        done = _run_gate(self.tmp, repo)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn('scope `protected`', done.stdout)
        self.assertIn('type `fix` is not `ci`', done.stdout)


if __name__ == "__main__":
    unittest.main()
