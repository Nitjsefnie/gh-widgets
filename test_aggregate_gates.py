"""Offline behavior and drift tests for the always-reporting CI gate."""
import unittest
from unittest import mock

from scripts.ci import aggregate_gates as ag


class FakeTransport:
    """Return real API-shaped fixtures without opening a connection."""

    def __init__(self, runs=None, branches=None, responses=None):
        self.runs = runs or [[]]
        self.branches = branches or []
        self.responses = responses or {}
        self.calls = []
        self.polls = 0

    def api(self, path, **kw):
        self.calls.append((path, kw))
        if path in self.responses:
            response = self.responses[path]
            if isinstance(response, Exception):
                raise response
            return response
        if 'head_sha=' in path:
            result = self.runs[min(self.polls, len(self.runs) - 1)]
            self.polls += 1
            return {'workflow_runs': result}
        if 'branch=' in path:
            return {'workflow_runs': self.branches}
        raise AssertionError(f'unexpected API call: {path}')


class Clock:
    def __init__(self):
        self.now = 0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def run(name, identifier=1, conclusion: str | None = 'success', event='push', status='completed', branch='topic'):
    return {'name': name, 'id': identifier, 'event': event,
            'head_sha': 'a' * 40, 'head_branch': branch,
            'status': status, 'conclusion': conclusion,
            'html_url': f'https://github.com/owner/repo/actions/runs/{identifier}'}


class PathMatcherTests(unittest.TestCase):
    def test_actions_globs_respect_directory_boundaries_and_case(self):
        cases = [
            ('**/*.md', 'README.md', True),
            ('**/*.md', 'x/y.md', True),
            ('**/*.md', 'docs/a/b.md', True),
            ('**/*.md', 'code/a.py', False),
            ('**/*.md', 'PRESENTATION.md.bak', False),
            ('**/*.md', 'README.MD', False),
            ('docs/**', 'docs/a.md', True),
            ('docs/**', 'docs/a/b.md', True),
            ('docs/**', 'docsx/a', False),
            ('docs/*', 'docs/a.md', True),
            ('docs/*', 'docs/a/b.md', False),
            ('.github/workflows/**', '.github/workflows/lint.yml', True),
            ('LICENSE', 'LICENSE', True),
            ('LICENSE', 'x/LICENSE', False),
            ('LICENSE', 'LICENSE.bak', False),
            ('docs/?.md', 'docs/a.md', True),
            ('docs/?.md', 'docs/ab.md', False),
            ('docs/?.md', 'docs//.md', False),
            ('a/**/b.py', 'a/b.py', True),
            ('a/**/b.py', 'a/x/y/b.py', True),
            ('*.py', 'x/a.py', False),
            ('file[1].py', 'file[1].py', True),
            ('file[1].py', 'file1.py', False),
        ]
        for pattern, path, expected in cases:
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(ag.github_path_matches(pattern, path), expected)


class ClassificationTests(unittest.TestCase):
    def test_docs_only_explains_skipped_code_and_workflow_gates(self):
        result = ag.classify({'README.md', 'docs/a/b.md'}, event='push')
        self.assertEqual(set(result.values()), {'not-applicable'})

    def test_workflow_change_runs_actionlint(self):
        result = ag.classify({'.github/workflows/lint.yml'}, event='push')
        self.assertEqual(result['actionlint'], 'run')
        self.assertEqual(result['tests'], 'run')

    def test_code_change_runs_all_code_gates(self):
        result = ag.classify({'new/code.py'}, event='pull_request', pr_action='synchronize')
        for name in ('tests', 'lint', 'types', 'audit', 'speed', 'codeql'):
            self.assertEqual(result[name], 'run')
        self.assertEqual(result['actionlint'], 'not-applicable')
        self.assertEqual(result['pr gate'], 'not-applicable')

    def test_claude_ignore_is_specific_to_codeql(self):
        result = ag.classify({'.claude/x'}, event='push')
        self.assertEqual(result['tests'], 'run')
        self.assertEqual(result['codeql'], 'not-applicable')

    def test_pr_gate_only_on_its_pr_actions(self):
        for action in ('opened', 'edited', 'reopened', 'ready_for_review'):
            self.assertEqual(ag.classify({'README.md'}, event='pull_request', pr_action=action)['pr gate'], 'run')
        self.assertEqual(ag.classify({'README.md'}, event='push', pr_action='opened')['pr gate'], 'not-applicable')
        self.assertEqual(ag.classify({'README.md'}, event='pull_request', pr_action='labeled')['pr gate'], 'not-applicable')

    def test_unknown_event_fails_closed(self):
        with self.assertRaises(ag.AggregationError):
            ag.classify({'a.py'}, event='unexpected')


class AcquisitionTests(unittest.TestCase):
    def test_pr_files_are_paginated_and_cap_is_conservative(self):
        path = 'repos/owner/repo/pulls/37/files?per_page=100'
        transport = FakeTransport(responses={path: [{'filename': f'docs/{index}.md'} for index in range(300)]})
        changed, capped = ag.changed_files(transport, 'owner/repo', event='pull_request', sha='a' * 40, pr_number='37')
        self.assertEqual(len(changed), 300)
        self.assertTrue(capped)
        self.assertTrue(transport.calls[0][1]['paginate'])
        self.assertTrue(transport.calls[0][1]['no_cache'])

    def test_push_compare_uses_before_and_after(self):
        path = 'repos/owner/repo/compare/' + 'b' * 40 + '...' + 'a' * 40
        transport = FakeTransport(responses={path: {'status': 'ahead', 'files': [{'filename': 'code.py'}]}})
        result = ag.changed_files(transport, 'owner/repo', event='push', sha='a' * 40,
                                  payload={'before': 'b' * 40, 'after': 'a' * 40})
        self.assertEqual(result, ({'code.py'}, False))

    def test_new_branch_compares_default_branch(self):
        path = 'repos/owner/repo/compare/main...' + 'a' * 40
        transport = FakeTransport(responses={path: {'status': 'ahead', 'files': [{'filename': 'README.md'}]}})
        result = ag.changed_files(transport, 'owner/repo', event='push', sha='a' * 40, default_branch='main',
                                  payload={'before': '0' * 40, 'after': 'a' * 40})
        self.assertEqual(result, ({'README.md'}, False))

    def test_push_cap_divergence_and_api_error_fail_closed(self):
        path = 'repos/owner/repo/compare/' + 'b' * 40 + '...' + 'a' * 40
        responses = [
            {'status': 'ahead', 'files': [{'filename': 'a.py'}] * 300},
            {'status': 'diverged', 'files': []},
            ag.AggregationError('API unavailable'),
            {'status': 'ahead'},
        ]
        for response in responses:
            with self.subTest(response=str(response)[:50]):
                transport = FakeTransport(responses={path: response})
                with self.assertRaises(ag.AggregationError):
                    ag.changed_files(transport, 'owner/repo', event='push', sha='a' * 40,
                                     payload={'before': 'b' * 40, 'after': 'a' * 40})

    def test_manual_dispatch_rechecks_push_verdicts_for_commit_diff(self):
        compare = 'repos/owner/repo/compare/' + 'b' * 40 + '...' + 'a' * 40
        transport = FakeTransport(responses={
            'repos/owner/repo/git/commits/' + 'a' * 40: {'parents': [{'sha': 'b' * 40}]},
            compare: {'status': 'ahead', 'files': [{'filename': 'code.py'}]},
        })
        self.assertEqual(ag.changed_files(transport, 'owner/repo', event='workflow_dispatch', sha='a' * 40), ({'code.py'}, False))


class VerdictTests(unittest.TestCase):
    def evaluate(self, transport, changed=None, event='push', action=None, timeout=0, capped=False):
        clock = Clock()
        return ag.evaluate_gates(transport, 'owner/repo', 'a' * 40,
                                 changed={'code.py'} if changed is None else changed,
                                 event=event, pr_action=action, capped=capped,
                                 timeout=timeout, poll_interval=1,
                                 clock=clock.time, sleep=clock.sleep)

    def code_runs(self, conclusion='success', event='push'):
        return [run(name, conclusion=conclusion, event=event) for name in ('tests', 'lint', 'types', 'audit', 'speed', 'codeql')]

    def test_all_success_passes(self):
        result = self.evaluate(FakeTransport(runs=[self.code_runs()]))
        self.assertEqual(ag.exit_code(result), 0)
        self.assertEqual(result['tests'].verdict, 'passed')

    def test_non_applicable_gates_pass_with_explanations(self):
        result = self.evaluate(FakeTransport(), changed={'README.md'})
        self.assertEqual(ag.exit_code(result), 0)
        self.assertEqual(result['tests'].verdict, 'skipped-not-applicable')
        self.assertIn('paths-ignore', result['tests'].detail)
        self.assertIn('skipped-not-applicable', ag.render_summary(result))

    def test_failed_gate_fails(self):
        runs = self.code_runs() + [run('tests', 2, 'failure')]
        result = self.evaluate(FakeTransport(runs=[runs]))
        self.assertEqual(ag.exit_code(result), 1)
        self.assertEqual(result['tests'].verdict, 'FAILED')

    def test_missing_gate_at_deadline_fails_and_lists_all_missing(self):
        result = self.evaluate(FakeTransport(runs=[[run('tests')]]), timeout=2)
        self.assertEqual(ag.exit_code(result), 1)
        self.assertEqual(result['lint'].verdict, 'never-reported')
        self.assertIn('should have run but never reported', result['lint'].detail)
        self.assertEqual([name for name, row in result.items() if row.verdict == 'never-reported'],
                         ['lint', 'types', 'audit', 'speed', 'codeql'])

    def test_superseded_cancel_passes(self):
        runs = self.code_runs() + [run('tests', 10, 'cancelled')]
        transport = FakeTransport(runs=[runs], branches=[run('tests', 11, branch='topic')])
        result = self.evaluate(transport)
        self.assertEqual(ag.exit_code(result), 0)
        self.assertEqual(result['tests'].verdict, 'superseded')
        self.assertIn('11', result['tests'].detail)

    def test_deliberate_cancel_fails(self):
        runs = self.code_runs() + [run('tests', 10, 'cancelled')]
        result = self.evaluate(FakeTransport(runs=[runs], branches=[run('lint', 11), run('tests', 12, branch='other')]))
        self.assertEqual(ag.exit_code(result), 1)
        self.assertEqual(result['tests'].verdict, 'FAILED')

    def test_newest_run_wins(self):
        runs = self.code_runs() + [run('tests', 5, 'failure'), run('tests', 6)]
        result = self.evaluate(FakeTransport(runs=[runs]))
        self.assertEqual(ag.exit_code(result), 0)
        self.assertIn('/6', result['tests'].detail)

    def test_pending_polls_then_passes(self):
        pending = self.code_runs() + [run('tests', 2, None, status='in_progress')]
        completed = self.code_runs() + [run('tests', 2)]
        transport = FakeTransport(runs=[pending, completed])
        self.assertEqual(ag.exit_code(self.evaluate(transport, timeout=2)), 0)
        self.assertEqual(transport.polls, 2)
        self.assertTrue(all(kw['paginate'] and kw['no_cache'] for _, kw in transport.calls))

    def test_pending_at_deadline_times_out(self):
        runs = self.code_runs() + [run('tests', 2, None, status='queued')]
        result = self.evaluate(FakeTransport(runs=[runs]))
        self.assertEqual(result['tests'].verdict, 'timed-out')
        self.assertEqual(ag.exit_code(result), 1)

    def test_each_gate_matches_its_own_event(self):
        runs = self.code_runs(event='pull_request') + [
            run('pr gate', 10, event='pull_request_target'),
            run('tests', 100, 'failure', event='push'),
            run('pr gate', 101, 'failure', event='pull_request'),
            run('aggregate', 200, 'failure', event='pull_request'),
            run('release', 201, 'failure', event='pull_request'),
        ]
        result = self.evaluate(FakeTransport(runs=[runs]), event='pull_request', action='opened')
        self.assertEqual(ag.exit_code(result), 0)
        self.assertIn('/10', result['pr gate'].detail)

    def test_skipped_neutral_and_failure_conclusions(self):
        for conclusion in ('skipped', 'neutral', 'startup_failure', 'timed_out'):
            result = self.evaluate(FakeTransport(runs=[self.code_runs() + [run('tests', 2, conclusion)]]))
            self.assertEqual(ag.exit_code(result), 0 if conclusion in ('skipped', 'neutral') else 1)

    def test_unknown_run_state_and_transport_error_fail_closed(self):
        for status, conclusion in [('mystery', None), ('completed', 'action_required'), ('completed', None)]:
            with self.subTest(status=status, conclusion=conclusion):
                with self.assertRaises(ag.AggregationError):
                    self.evaluate(FakeTransport(runs=[self.code_runs() + [run('tests', 2, conclusion, status=status)]]))
        path = 'repos/owner/repo/actions/runs?head_sha=' + 'a' * 40 + '&per_page=100'
        with self.assertRaises(ag.AggregationError):
            self.evaluate(FakeTransport(responses={path: ag.AggregationError('offline')}))

    def test_malformed_run_metadata_fails_closed(self):
        for field, value in [('id', True), ('head_sha', None), ('status', {}), ('conclusion', {})]:
            with self.subTest(field=field):
                malformed = run('tests', 2)
                malformed[field] = value
                with self.assertRaises(ag.AggregationError):
                    self.evaluate(FakeTransport(runs=[self.code_runs() + [malformed]]))

    def test_pr_cap_requires_code_gates_even_for_docs(self):
        result = self.evaluate(FakeTransport(), changed={'README.md'}, event='pull_request', action='synchronize', capped=True)
        self.assertEqual(result['tests'].verdict, 'never-reported')
        self.assertEqual(result['codeql'].verdict, 'never-reported')


class CliTests(unittest.TestCase):
    def test_cli_passes_and_writes_the_summary(self):
        path = 'repos/owner/repo/pulls/37/files?per_page=100'
        transport = FakeTransport(responses={path: [{'filename': 'README.md'}]})
        environment = {'GATE_REPO': 'owner/repo', 'GATE_EVENT': 'pull_request',
                       'GATE_SHA': 'a' * 40, 'GATE_PR_NUMBER': '37',
                       'GATE_PR_ACTION': 'synchronize', 'GITHUB_STEP_SUMMARY': 'summary'}
        with mock.patch('builtins.print'), mock.patch('builtins.open', mock.mock_open()) as output:
            self.assertEqual(ag.main([], transport=transport, environ=environment), 0)
        output.assert_called_once_with('summary', 'a', encoding='utf-8')
        self.assertIn('| tests | skipped-not-applicable |', output().write.call_args.args[0])

    def test_cli_api_error_is_exit_two_with_a_per_gate_failure_table(self):
        path = 'repos/owner/repo/pulls/37/files?per_page=100'
        transport = FakeTransport(responses={path: ag.AggregationError('API unavailable')})
        environment = {'GATE_REPO': 'owner/repo', 'GATE_EVENT': 'pull_request',
                       'GATE_SHA': 'a' * 40, 'GATE_PR_NUMBER': '37'}
        with mock.patch('builtins.print') as output:
            self.assertEqual(ag.main([], transport=transport, environ=environment), 2)
        self.assertIn('| tests | FAILED |', output.call_args.args[0])
        self.assertIn('API unavailable', output.call_args.args[0])

    def test_transport_paginates_both_api_list_shapes(self):
        for pages in ([[{'filename': 'a.py'}], [{'filename': 'b.py'}]],
                      [{'workflow_runs': [run('tests')]}, {'workflow_runs': [run('lint')]}]):
            with self.subTest(pages=pages):
                response = ag.subprocess.CompletedProcess([], 0, stdout=ag.json.dumps(pages), stderr='')
                with mock.patch.object(ag.subprocess, 'run', return_value=response) as request:
                    items = ag.GhTransport().api('repos/owner/repo/list?per_page=100', paginate=True)
                self.assertEqual(len(items), 2)
                command = request.call_args.args[0]
                self.assertIn('--paginate', command)
                self.assertIn('--slurp', command)
                self.assertIn('Cache-Control: no-cache', command)

    def test_exit_code_rejects_incomplete_or_unknown_verdicts(self):
        with self.assertRaises(ag.AggregationError):
            ag.exit_code({})
        results = {name: ag.Result('unknown', 'unknown state') for name in ag.GATES}
        with self.assertRaises(ag.AggregationError):
            ag.exit_code(results)


if __name__ == '__main__':
    unittest.main()
