#!/usr/bin/env python3
"""Fold the workflow gates for one commit into an always-reporting verdict.

WHY THIS CHECK EXISTS. Path-filtered workflows report nothing for an ignored
change. Requiring one of those checks strands a docs-only pull request. This
workflow always reports, explaining each gate that legitimately did not run.

WHY WORKFLOW RUNS. A workflow owns its verdict, including a job skipped for a
draft or Bot author. Check-run names vary across jobs and matrix cells; the
workflow name and event are the stable boundary. Each gate uses its own event,
so push results cannot stand in for pull-request results. The PR policy gate
is the deliberate exception: its own event is pull_request_target.

WHY CANCEL IS NOT ALWAYS RED. Concurrency cancels an obsolete branch revision.
A newer run of the same workflow, branch and event proves supersession, which
gates nothing. Without that evidence a cancellation is a failure.

Release is deliberately absent: it waits on this check, so waiting on release
would deadlock. Network, classification and unknown-state errors fail closed.
All HTTP goes through gh api; the transport and clock are injected in tests.

    python3 scripts/ci/aggregate_gates.py

Context comes from GATE_* and GITHUB_EVENT_PATH. The markdown table is printed
and appended to GITHUB_STEP_SUMMARY when that runner variable is available.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import quote


class AggregationError(RuntimeError):
    """The aggregate cannot safely determine a verdict."""


@dataclass(frozen=True)
class Gate:
    """Path filter and aggregate-event to workflow-event mapping."""

    kind: str
    patterns: tuple[str, ...]
    events: dict[str, str]


@dataclass(frozen=True)
class Result:
    """One summary row; only terminal verdicts leave the polling engine."""

    verdict: str
    detail: str


class Transport(Protocol):
    def api(self, path: str, *, paginate: bool = False, no_cache: bool = True) -> Any:
        """Read a GitHub REST resource."""


CI_EVENTS = {'push': 'push', 'pull_request': 'pull_request',
             'workflow_dispatch': 'push'}
CODE_IGNORES = ('**/*.md', 'PRESENTATION.txt', 'docs/**', 'examples/**',
                'LICENSE', '.gitignore')
PR_ACTIONS = frozenset({'opened', 'edited', 'reopened', 'ready_for_review'})
# test_aggregate_gates.py keeps these filters and the gate inventory honest
# against the actual sibling workflow files. Add a gate there AND here.
GATES = {
    'tests': Gate('deny', CODE_IGNORES, CI_EVENTS),
    'lint': Gate('deny', CODE_IGNORES, CI_EVENTS),
    'types': Gate('deny', CODE_IGNORES, CI_EVENTS),
    'audit': Gate('deny', CODE_IGNORES, CI_EVENTS),
    'speed': Gate('deny', CODE_IGNORES, CI_EVENTS),
    'codeql': Gate('deny', (
        '**/*.md', 'docs/**', 'examples/**', '.claude/**',
        'LICENSE', 'NOTICE', '.gitignore'), CI_EVENTS),
    'actionlint': Gate('allow', ('.github/workflows/**', '.github/dependabot.yml'), CI_EVENTS),
    'pr gate': Gate('pr', (), {'pull_request': 'pull_request_target'}),
}
PASS_CONCLUSIONS = frozenset({'success', 'skipped', 'neutral'})
FAIL_CONCLUSIONS = frozenset({'failure', 'startup_failure', 'timed_out'})
FAIL_VERDICTS = frozenset({'FAILED', 'never-reported', 'timed-out'})


def github_path_matches(pattern: str, path: str) -> bool:
    """Match the case-sensitive Actions path glob subset used by the gates.

    Unlike fnmatch, single stars and question marks cannot cross a slash.
    A double star can, and **/ also admits zero intervening directories.
    All other characters are literal, including regex metacharacters.
    """
    pieces = []
    index = 0
    while index < len(pattern):
        if pattern[index:index + 2] == '**':
            index += 2
            if pattern[index:index + 1] == '/':
                pieces.append('(?:.*/)?')
                index += 1
            else:
                pieces.append('.*')
        elif pattern[index] == '*':
            pieces.append('[^/]*')
            index += 1
        elif pattern[index] == '?':
            pieces.append('[^/]')
            index += 1
        else:
            pieces.append(re.escape(pattern[index]))
            index += 1
    return re.compile(''.join(pieces), re.DOTALL).fullmatch(path) is not None


def classify(changed: set[str], *, event: str, pr_action: str | None = None) -> dict[str, str]:
    """Which workflows should report for this event and changed-file set?"""
    if event not in CI_EVENTS:
        raise AggregationError(f'unsupported aggregate event: {event!r}')
    decisions = {}
    for name, gate in GATES.items():
        if gate.kind == 'pr':
            required = event == 'pull_request' and pr_action in PR_ACTIONS
        elif gate.kind == 'deny':
            required = not all(any(github_path_matches(pattern, path)
                                   for pattern in gate.patterns) for path in changed)
        elif gate.kind == 'allow':
            required = any(github_path_matches(pattern, path)
                           for pattern in gate.patterns for path in changed)
        else:
            raise AggregationError(f'unknown filter kind for {name}: {gate.kind}')
        decisions[name] = 'run' if required else 'not-applicable'
    return decisions


def _list_items(data: Any) -> list[dict]:
    if isinstance(data, dict):
        data = data.get('workflow_runs')
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise AggregationError('API list response is missing or malformed')
    return data


class GhTransport:
    """gh follows Link pagination; slurp keeps each JSON page parseable."""

    def __init__(self, environment: dict[str, str] | None = None):
        self.environment = environment

    def api(self, path: str, *, paginate: bool = False, no_cache: bool = True) -> Any:
        command = ['gh', 'api', '--method', 'GET', '-H', 'Accept: application/vnd.github+json']
        if no_cache:
            command += ['-H', 'Cache-Control: no-cache']
        if paginate:
            command += ['--paginate', '--slurp']
        command.append(path)
        try:
            response = subprocess.run(command, check=True, capture_output=True,
                                      text=True, timeout=60, env=self.environment)
            data = json.loads(response.stdout)
        except subprocess.CalledProcessError as exc:
            raise AggregationError(f'gh api {path}: {(exc.stderr or str(exc)).strip()}') from exc
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise AggregationError(f'gh api {path}: {exc}') from exc
        if not paginate:
            return data
        if not isinstance(data, list):
            raise AggregationError(f'gh api {path}: missing paginated JSON pages')
        return [item for page in data for item in _list_items(page)]


def _api(transport: Transport, path: str, *, paginate: bool = False) -> Any:
    try:
        return transport.api(path, paginate=paginate, no_cache=True)
    except AggregationError:
        raise
    except Exception as exc:
        raise AggregationError(f'API request failed for {path}: {exc}') from exc


def _filenames(data: Any) -> set[str]:
    if not isinstance(data, list):
        raise AggregationError('changed-file response has no files list')
    names = set()
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get('filename'), str) or not item['filename']:
            raise AggregationError('changed-file response has a missing filename')
        names.add(item['filename'])
        # A rename changes both paths. Ignoring the old name could skip a gate
        # when code was moved into an ignored directory.
        if item.get('status') == 'renamed':
            previous = item.get('previous_filename')
            if not isinstance(previous, str) or not previous:
                raise AggregationError('renamed file has no previous_filename')
            names.add(previous)
    return names


def _sha(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch('[0-9a-fA-F]{40}', value) is None:
        raise AggregationError(f'missing or invalid commit SHA: {value!r}')
    return value


def changed_files(transport: Transport, repo: str, *, event: str, sha: str,
                  pr_number: str = '', default_branch: str = '',
                  payload: dict | None = None) -> tuple[set[str], bool]:
    """Changed paths and whether PR path classification reached the 300 cap.

    Dispatch rechecks push verdicts for the selected commit, comparing its
    first parent as the sibling workflows' default checkout selects that tip.
    No event before/after pair exists on a manual dispatch.
    """
    _sha(sha)
    if event == 'pull_request':
        if not str(pr_number).isdigit() or int(pr_number) < 1:
            raise AggregationError('pull_request has no valid GATE_PR_NUMBER')
        files = _list_items(_api(transport, f'repos/{repo}/pulls/{pr_number}/files?per_page=100', paginate=True))
        return _filenames(files), len(files) >= 300
    if event == 'workflow_dispatch':
        commit = _api(transport, f'repos/{repo}/git/commits/{sha}')
        if not isinstance(commit, dict) or not isinstance(commit.get('parents'), list) or not commit['parents']:
            raise AggregationError('manual dispatch commit has no parent to compare')
        parent = commit['parents'][0]
        if not isinstance(parent, dict):
            raise AggregationError('manual dispatch commit has a malformed parent')
        before, after = _sha(parent.get('sha')), sha
    elif event == 'push':
        payload = payload or {}
        before, after = _sha(payload.get('before')), _sha(payload.get('after'))
        if after != sha:
            raise AggregationError('push event after does not match GATE_SHA')
    else:
        raise AggregationError(f'cannot acquire changed files for event {event!r}')
    return _compare_files(transport, repo, before, after, default_branch)


def _compare_files(transport: Transport, repo: str, before: str, after: str,
                   default_branch: str) -> tuple[set[str], bool]:
    if before == '0' * 40:
        if not default_branch:
            raise AggregationError('new-branch push has no default branch to compare')
        before = quote(default_branch, safe='')
    comparison = _api(transport, f'repos/{repo}/compare/{before}...{after}')
    if not isinstance(comparison, dict) or comparison.get('status') not in ('ahead', 'identical'):
        raise AggregationError('push comparison is divergent, behind, or has an unknown status')
    files = comparison.get('files')
    if not isinstance(files, list):
        raise AggregationError('push comparison has no changed-file list')
    if len(files) >= 300:
        raise AggregationError('push comparison reached the 300-file cap; classification is incomplete')
    return _filenames(files), False


def _latest(runs: list[dict], name: str, event: str, sha: str) -> dict | None:
    matching = [run for run in runs if run.get('name') == name
                and run.get('event') == event]
    for run in matching:
        _sha(run.get('head_sha'))
        _run_id(run, name)
    matching = [run for run in matching if run['head_sha'] == sha]
    return max(matching, key=lambda run: run['id']) if matching else None


def _run_id(run: dict, name: str) -> int:
    identifier = run.get('id')
    if not isinstance(identifier, int) or isinstance(identifier, bool) or identifier < 1:
        raise AggregationError(f'{name}: workflow run has no valid id')
    return identifier


def _completed(transport: Transport, repo: str, name: str, run: dict) -> Result:
    conclusion = run.get('conclusion')
    url = run.get('html_url')
    if not isinstance(url, str) or not url:
        raise AggregationError(f'{name}: completed run has no URL')
    if not isinstance(conclusion, str):
        raise AggregationError(f'{name}: unknown completed conclusion {conclusion!r}')
    if conclusion in PASS_CONCLUSIONS:
        return Result('passed', f'{url} ({conclusion})')
    if conclusion in FAIL_CONCLUSIONS:
        return Result('FAILED', f'{url} ({conclusion})')
    if conclusion != 'cancelled':
        raise AggregationError(f'{name}: unknown completed conclusion {conclusion!r}')
    branch = run.get('head_branch')
    if not isinstance(branch, str) or not branch:
        raise AggregationError(f'{name}: cancelled run has no head_branch')
    # Supersession only needs the newest page, as the brief prescribes. This
    # deliberate exception to full list pagination still requests 100 runs
    # and bypasses caches; older pages cannot establish a newer branch tip.
    newer = _list_items(_api(transport, f'repos/{repo}/actions/runs?branch={quote(branch, safe="")}&per_page=100'))
    for candidate in newer:
        if (candidate.get('name') == name and candidate.get('head_branch') == branch
                and candidate.get('event') == run['event']):
            identifier = _run_id(candidate, name)
            if identifier > run['id']:
                return Result('superseded', f'{url}; superseded by run {identifier} on {branch}')
    return Result('FAILED', f'{url}; cancelled without a newer run on {branch}')


def _not_applicable(gate: Gate, event: str, pr_action: str | None) -> Result:
    if gate.kind == 'pr':
        reason = f'PR policy gate is not triggered by {event} / {pr_action or "no PR action"}'
    elif gate.kind == 'deny':
        reason = 'every changed path matches paths-ignore (or the diff is empty)'
    else:
        reason = 'no changed path matches paths'
    return Result('skipped-not-applicable', reason)


def _inspect_gates(transport: Transport, repo: str, sha: str, event: str,
                   pr_action: str | None, decisions: dict[str, str],
                   runs: list[dict]) -> tuple[dict[str, Result], dict[str, Result]]:
    results, pending = {}, {}
    for name, gate in GATES.items():
        # Applicability wins over any stale run at this SHA. In particular,
        # ignored changes must not inherit an earlier failure or cancellation.
        if decisions[name] == 'not-applicable':
            results[name] = _not_applicable(gate, event, pr_action)
            continue
        run = _latest(runs, name, gate.events.get(event, ''), sha)
        if run is None:
            pending[name] = Result('never-reported', 'should have run but never reported')
        elif run.get('status') in ('queued', 'in_progress'):
            pending[name] = Result('timed-out', f'{run.get("html_url", "run URL unavailable")} ({run["status"]})')
        elif run.get('status') == 'completed':
            results[name] = _completed(transport, repo, name, run)
        else:
            raise AggregationError(f'{name}: unknown workflow status {run.get("status")!r}')
    return results, pending


# Separate clock/sleep hooks make deadline tests independent of real time.
def evaluate_gates(transport: Transport, repo: str, sha: str, *, changed: set[str],  # pylint: disable=too-many-arguments
                   event: str, pr_action: str | None = None, capped: bool = False,
                   timeout: float = 40 * 60, poll_interval: float = 20,
                   clock: Callable[[], float] = time.monotonic,
                   sleep: Callable[[float], None] = time.sleep) -> dict[str, Result]:
    """Poll the newest run per workflow/event until all gates are terminal."""
    if timeout < 0 or poll_interval <= 0:
        raise AggregationError('timeout must be nonnegative and poll interval positive')
    decisions = classify(changed, event=event, pr_action=pr_action)
    if capped:
        # At the PR diff cap the code deny-lists cannot justify a skip.
        decisions.update({name: 'run' for name, gate in GATES.items() if gate.kind == 'deny'})
    deadline = clock() + timeout
    while True:
        results, pending = _inspect_gates(
            transport, repo, sha, event, pr_action, decisions,
            _list_items(_api(transport, f'repos/{repo}/actions/runs?head_sha={sha}&per_page=100', paginate=True)))
        if not pending or clock() >= deadline:
            results.update(pending)
            return {name: results[name] for name in GATES}
        sleep(min(poll_interval, max(0, deadline - clock())))


def exit_code(results: dict[str, Result]) -> int:
    terminal = FAIL_VERDICTS | {'passed', 'skipped-not-applicable', 'superseded'}
    if set(results) != set(GATES) or any(row.verdict not in terminal for row in results.values()):
        raise AggregationError('aggregate results are incomplete or contain an unknown verdict')
    return int(any(row.verdict in FAIL_VERDICTS for row in results.values()))


def render_summary(results: dict[str, Result]) -> str:
    def cell(value: str) -> str:
        return value.replace('|', '\\|').replace('\r', ' ').replace('\n', ' ')

    lines = ['### Aggregate CI gates', '', '| Gate | Verdict | Detail |', '| --- | --- | --- |']
    lines.extend(f'| {cell(name)} | {row.verdict} | {cell(row.detail)} |' for name, row in results.items())
    return '\n'.join(lines) + '\n'


def _context_results(transport: Transport, environment: dict[str, str]) -> dict[str, Result]:
    repo = environment.get('GATE_REPO', '')
    if re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) is None:
        raise AggregationError('missing or invalid GATE_REPO')
    event, sha = environment.get('GATE_EVENT', ''), _sha(environment.get('GATE_SHA'))
    payload = {}
    if event == 'push':
        event_path = environment.get('GITHUB_EVENT_PATH', '')
        if not event_path:
            raise AggregationError('push has no GITHUB_EVENT_PATH')
        payload = json.loads(Path(event_path).read_text(encoding='utf-8'))
        if not isinstance(payload, dict):
            raise AggregationError('event payload is not a JSON object')
    changed, capped = changed_files(
        transport, repo, event=event, sha=sha,
        pr_number=environment.get('GATE_PR_NUMBER', ''),
        default_branch=environment.get('GATE_DEFAULT_BRANCH', ''), payload=payload)
    return evaluate_gates(transport, repo, sha, changed=changed, capped=capped,
                          event=event, pr_action=environment.get('GATE_PR_ACTION'))


def main(argv: list[str] | None = None, *, transport: Transport | None = None,
         environ: dict[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.parse_args(argv)
    environment = dict(os.environ if environ is None else environ)
    code = 2
    try:
        results = _context_results(transport or GhTransport(environment), environment)
        code = exit_code(results)
        unresolved = [name for name, row in results.items() if row.verdict in {'never-reported', 'timed-out'}]
        if unresolved:
            print('deadline reached; unresolved gates: ' + ', '.join(unresolved), file=sys.stderr)
    except (AggregationError, OSError, ValueError) as exc:
        print(f'cannot aggregate: {exc}', file=sys.stderr)
        results = {name: Result('FAILED', f'aggregate could not determine verdict: {exc}') for name in GATES}
    report = render_summary(results)
    print(report)
    summary = environment.get('GITHUB_STEP_SUMMARY')
    if summary:
        try:
            with open(summary, 'a', encoding='utf-8') as handle:
                handle.write(report)
        except OSError as exc:
            print(f'cannot write step summary: {exc}', file=sys.stderr)
            return 2
    return code


if __name__ == '__main__':
    sys.exit(main())
