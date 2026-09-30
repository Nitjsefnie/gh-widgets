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
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def run(name, identifier=1, conclusion: str | None = 'success', event='push', status='completed', branch='topic',
        created_at='2026-09-30T12:00:00Z'):
    return {'name': name, 'id': identifier, 'event': event,
            'head_sha': 'a' * 40, 'head_branch': branch,
            'status': status, 'conclusion': conclusion,
            'created_at': created_at,
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
            ('docs/**', 'docs/evil\nname.py', True),
            ('**/*.md', 'dir\nname/README.md', True),
            ('docs/*', 'docs/evil\nname.py', True),
            ('docs/*', 'docs/evil\nname/a.py', False),
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
    def evaluate(self, transport, changed=None, event='push', action=None, timeout=0, capped=False,
                 policy_updated_at='2026-09-30T12:00:00Z'):
        clock = Clock()
        return ag.evaluate_gates(transport, 'owner/repo', 'a' * 40,
                                 changed={'code.py'} if changed is None else changed,
                                 event=event, pr_action=action, capped=capped,
                                 policy_updated_at=policy_updated_at,
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

    def test_non_applicable_gates_ignore_existing_failed_pending_and_cancelled_runs(self):
        for status, conclusion in [('completed', 'failure'), ('in_progress', None), ('completed', 'cancelled')]:
            with self.subTest(status=status, conclusion=conclusion):
                runs = [run(name, conclusion=conclusion, status=status) for name in ag.GATES]
                transport = FakeTransport(runs=[runs])
                result = self.evaluate(transport, changed={'README.md'}, timeout=2)
                self.assertEqual(ag.exit_code(result), 0)
                self.assertEqual({row.verdict for row in result.values()}, {'skipped-not-applicable'})
                self.assertIn('paths-ignore', result['tests'].detail)
                self.assertEqual(transport.polls, 1)
                self.assertFalse(any('branch=' in path for path, _ in transport.calls))

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

    def test_other_event_does_not_supersede_a_cancelled_run(self):
        for event, other_event in [('push', 'pull_request'), ('pull_request', 'push')]:
            with self.subTest(event=event):
                runs = self.code_runs(event=event) + [run('tests', 10, 'cancelled', event=event)]
                transport = FakeTransport(runs=[runs], branches=[run('tests', 11, event=other_event)])
                result = self.evaluate(transport, event=event)
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
        clock = Clock()
        result = ag.evaluate_gates(FakeTransport(runs=[runs]), 'owner/repo', 'a' * 40,
                                   changed={'code.py'}, event='push', timeout=2, poll_interval=0.75,
                                   clock=clock.time, sleep=clock.sleep)
        self.assertEqual(result['tests'].verdict, 'timed-out')
        self.assertEqual(ag.exit_code(result), 1)
        self.assertEqual(clock.now, 1002)

    def test_pending_succeeds_in_latter_half_of_window_with_nonzero_clock_origin(self):
        clock = Clock()
        pending = self.code_runs() + [run('tests', 2, None, status='in_progress')]
        completed = self.code_runs() + [run('tests', 2)]
        transport = FakeTransport()
        request = transport.api

        def available_runs(path, **kw):
            transport.runs = [completed if clock.time() >= 1001.5 else pending]
            return request(path, **kw)

        with mock.patch.object(transport, 'api', side_effect=available_runs):
            result = ag.evaluate_gates(transport, 'owner/repo', 'a' * 40, changed={'code.py'}, event='push',
                                       timeout=2, poll_interval=0.75, clock=clock.time, sleep=clock.sleep)
        self.assertEqual(ag.exit_code(result), 0)
        self.assertEqual(clock.now, 1001.5)
        self.assertEqual(transport.polls, 3)

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

    def test_policy_edit_waits_for_delayed_failing_run(self):
        old = run('pr gate', 9, event='pull_request_target', created_at='2026-09-30T11:59:59Z')
        fresh = run('pr gate', 10, 'failure', event='pull_request_target', created_at='2026-09-30T12:00:01Z')
        transport = FakeTransport(runs=[[old], [old, fresh]])
        result = self.evaluate(transport, changed={'README.md'}, event='pull_request', action='edited', timeout=2)
        self.assertEqual(result['pr gate'].verdict, 'FAILED')
        self.assertIn('/10', result['pr gate'].detail)
        self.assertEqual(ag.exit_code(result), 1)
        self.assertEqual(transport.polls, 2)

    def test_stale_policy_success_never_reports_for_current_state(self):
        old = run('pr gate', 9, event='pull_request_target', created_at='2026-09-30T11:59:59Z')
        result = self.evaluate(FakeTransport(runs=[[old]]), changed={'README.md'},
                               event='pull_request', action='edited', timeout=2)
        self.assertEqual(result['pr gate'].verdict, 'never-reported')
        self.assertEqual(ag.exit_code(result), 1)

    def test_policy_timestamps_fail_closed_when_missing_or_malformed(self):
        malformed = [None, '', {}, '2026-09-30T12:00:00+00:00', '2026-09-30T12:00:00.1Z',
                     '2026-02-30T12:00:00Z', '0000-09-30T12:00:00Z']
        for value in malformed:
            with self.subTest(field='policy_updated_at', value=value):
                with self.assertRaises(ag.AggregationError):
                    self.evaluate(FakeTransport(), changed={'README.md'}, event='pull_request',
                                  action='edited', policy_updated_at=value)
            with self.subTest(field='created_at', value=value):
                candidate = run('pr gate', 9, event='pull_request_target')
                candidate['created_at'] = value
                with self.assertRaises(ag.AggregationError):
                    self.evaluate(FakeTransport(runs=[[candidate]]), changed={'README.md'},
                                  event='pull_request', action='edited')

    def test_policy_boundary_accepts_equal_timestamp_and_skipped_run(self):
        candidate = run('pr gate', 9, 'skipped', event='pull_request_target')
        result = self.evaluate(FakeTransport(runs=[[candidate]]), changed={'README.md'},
                               event='pull_request', action='opened')
        self.assertEqual(ag.exit_code(result), 0)
        self.assertEqual(result['pr gate'].verdict, 'passed')

    def test_non_required_policy_needs_no_timestamp(self):
        for event, action in [('push', None), ('workflow_dispatch', None), ('pull_request', 'synchronize')]:
            with self.subTest(event=event):
                result = self.evaluate(FakeTransport(), changed={'README.md'}, event=event,
                                       action=action, policy_updated_at=None)
                self.assertEqual(ag.exit_code(result), 0)

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


class WorkflowDriftTests(unittest.TestCase):
    def trigger_block(self, text):
        lines = text.splitlines()
        start = lines.index('on:') + 1
        end = next(index for index in range(start, len(lines))
                   if lines[index] in ('permissions:', 'concurrency:'))
        return lines[start:end]

    def triggers(self, text):
        result = {}
        event = None
        key = None
        for line in self.trigger_block(text):
            match = ag.re.match(r'^  ([a-z_]+):', line)
            if match:
                event, key = match.group(1), None
                result[event] = {}
                continue
            match = ag.re.match(r'^    (paths|paths-ignore):', line)
            if match and event:
                key = match.group(1)
                result[event][key] = []
                continue
            match = ag.re.match(r"^      - '([^']+)'", line)
            if match and event and key:
                result[event][key].append(match.group(1))
        return result

    def test_every_commit_gate_is_in_table_and_filters_match(self):
        root = ag.Path(__file__).resolve().parent
        observed = set()
        for path in sorted((root / '.github/workflows').glob('*.yml')):
            text = path.read_text(encoding='utf-8')
            name_match = ag.re.search(r'^name: (.+)$', text, ag.re.MULTILINE)
            if name_match is None:
                self.fail(f'{path}: workflow has no name')
            name = name_match.group(1)
            triggers = self.triggers(text)
            if not set(triggers) & {'push', 'pull_request', 'pull_request_target'}:
                continue
            # release is push-only and waits on CI itself; including it here
            # would deadlock. aggregate must never evaluate its own run.
            if name in {'release', 'aggregate'}:
                continue
            with self.subTest(workflow=path.name):
                self.assertIn(name, ag.GATES, 'new commit gate must join GATES')
                observed.add(name)
                gate = ag.GATES[name]
                if gate.kind == 'pr':
                    self.assertEqual(set(triggers), {'pull_request_target'})
                    self.assertEqual(gate.events, {'pull_request': 'pull_request_target'})
                    type_match = ag.re.search(r'types: \[([^]]+)\]', text)
                    if type_match is None:
                        self.fail(f'{path}: PR gate has no action types')
                    types = type_match.group(1)
                    self.assertEqual(set(item.strip() for item in types.split(',')), ag.PR_ACTIONS)
                    continue
                key = 'paths-ignore' if gate.kind == 'deny' else 'paths'
                self.assertIn('push', triggers)
                self.assertIn('pull_request', triggers)
                self.assertEqual(triggers['push'][key], triggers['pull_request'][key])
                self.assertEqual(tuple(triggers['push'][key]), gate.patterns)
                self.assertEqual(gate.events['push'], 'push')
                self.assertEqual(gate.events['pull_request'], 'pull_request')
        self.assertEqual(observed, set(ag.GATES))

    def test_aggregate_always_reports_and_actions_are_pinned(self):
        path = ag.Path(__file__).resolve().parent / '.github/workflows/aggregate.yml'
        text = path.read_text(encoding='utf-8')
        triggers = self.triggers(text)
        self.assertEqual(set(triggers), {'push', 'pull_request', 'workflow_dispatch'})
        for event in ('push', 'pull_request'):
            self.assertEqual(triggers[event], {})
        self.assertNotIn('branches:', '\n'.join(self.trigger_block(text)))
        self.assertIn('actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1', text)
        self.assertIn('actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97 # v7.0.0', text)
        block = text.split('        run: |', 1)[1]
        self.assertNotIn('${{', block)
        self.assertIn('python3 scripts/ci/aggregate_gates.py', block)
        self.assertIn('GATE_POLICY_UPDATED_AT: ${{ github.event.pull_request.updated_at }}', text)

    def test_contributing_count_matches_all_workflow_files(self):
        root = ag.Path(__file__).resolve().parent
        numbers: dict[str, int] = dict(zip((
            'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven', 'Eight', 'Nine', 'Ten',
            'Eleven', 'Twelve', 'Thirteen', 'Fourteen', 'Fifteen', 'Sixteen', 'Seventeen',
            'Eighteen', 'Nineteen', 'Twenty'), range(1, 21)))
        text = (root / 'CONTRIBUTING.md').read_text(encoding='utf-8')
        match = ag.re.search(r'(\w+) workflows run, and a green suite is one of them\.', text)
        if match is None:
            self.fail('CONTRIBUTING is missing the workflow count')
        word = match.group(1)
        self.assertEqual(numbers.get(word, int(word) if word.isdigit() else 0),
                         len(list((root / '.github/workflows').glob('*.yml'))))


class CliTests(unittest.TestCase):
    def test_cli_uses_the_policy_timestamp_from_environment(self):
        path = 'repos/owner/repo/pulls/37/files?per_page=100'
        candidate = run('pr gate', 9, event='pull_request_target', created_at='2026-09-30T11:00:00Z')
        transport = FakeTransport(runs=[[candidate]], responses={path: [{'filename': 'README.md'}]})
        environment = {'GATE_REPO': 'owner/repo', 'GATE_EVENT': 'pull_request', 'GATE_SHA': 'a' * 40,
                       'GATE_PR_NUMBER': '37', 'GATE_PR_ACTION': 'edited',
                       'GATE_POLICY_UPDATED_AT': 'malformed'}
        with mock.patch('builtins.print'):
            self.assertEqual(ag.main([], transport=transport, environ=environment), 2)

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
