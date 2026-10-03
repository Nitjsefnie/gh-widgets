"""Needs-based verdicts for the consolidated CI aggregate job."""
import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest import mock

from scripts.ci import aggregate_gate as ag
from scripts.ci import changes_detect as cd


def _needs(**results):
    return {name: {"result": result} for name, result in results.items()}


def _all_success():
    needs = _needs(changes="success")
    needs.update({job: {"result": "success"}
                  for job in ag.GATE_JOBS.values()})
    return needs


def _with_secret_result(results, verdict=ag.PASSED):
    complete = dict(results)
    complete["secrets"] = ag.Result(verdict, "gitleaks job concluded success")
    return complete


def _applicability(**overrides):
    decisions = {name: "run" for name in cd.GATES}
    decisions.update(overrides)
    return decisions


def _run(identifier, started, workflow=11, branch="feature", **fields):
    run = {
        "id": identifier,
        "status": "completed",
        "conclusion": "success",
        "run_started_at": started,
        "workflow_id": workflow,
        "head_branch": branch,
        "head_repository": {"id": 100, "full_name": "contrib/repo"},
        "pull_requests": [{"number": 42}],
        "event": "pull_request",
        "html_url": f"https://github.com/owner/repo/actions/runs/{identifier}",
    }
    run.update(fields)
    return run


class FakeTransport:
    """Return API-shaped values and record every request option."""

    def __init__(self, responses=None, error=None):
        self.responses = responses or {}
        self.error = error
        self.calls = []

    def api(self, path, *, paginate=False, no_cache=True):
        self.calls.append((path, paginate, no_cache))
        if self.error is not None:
            raise self.error
        if path not in self.responses:
            raise AssertionError(f"unexpected API call: {path}")
        response = self.responses[path]
        if isinstance(response, deque):
            response = response[0] if len(response) == 1 else response.popleft()
        if isinstance(response, Exception):
            raise response
        return response


HEAD_SHA = "a" * 40
SECRETS_RUNS_PATH = (
    "repos/owner/repo/actions/workflows/secrets.yml/runs"
    f"?head_sha={HEAD_SHA}&per_page=100")


def _secret_run(identifier, started, *, status="completed",
                conclusion: str | None = "success", created=None):
    return {
        "id": identifier,
        "status": status,
        "conclusion": conclusion,
        "head_sha": HEAD_SHA,
        "run_started_at": started,
        "created_at": created or started,
        "html_url": f"https://github.com/owner/repo/actions/runs/{identifier}",
    }


def _secret_job(conclusion: str | None = "success", name="gitleaks"):
    return {"name": name, "status": "completed", "conclusion": conclusion}


def _dispatch_environment():
    return {
        "NEEDS_JSON": json.dumps(_all_success()),
        "EVENT_NAME": "workflow_dispatch",
        "REPOSITORY": "owner/repo",
        "RUN_ID": "5",
        "HEAD_BRANCH": "feature",
        "HEAD_SHA": HEAD_SHA,
        "DEFAULT_BRANCH": "main",
        "EVENT_PATH": "",
    }


class FakeClock:
    """Advance only when the injected wait function is called."""

    def __init__(self, advances=None):
        self.now = 1000.0
        self.advances = deque(advances or [])
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += self.advances.popleft() if self.advances else seconds


class NeedsClassificationTests(unittest.TestCase):
    def test_only_changes_is_strict(self):
        self.assertEqual(ag.STRICT, frozenset({"changes"}))
        self.assertEqual(ag.allowed_results("changes"), {"success"})
        for job in ag.GATE_JOBS.values():
            with self.subTest(job=job):
                self.assertEqual(ag.allowed_results(job),
                                 {"success", "skipped"})

    def test_classify_needs_separates_hard_results_and_cancellations(self):
        needs = _needs(changes="success", unittest="skipped",
                       lint="failure", pyright="cancelled")
        hard, cancelled = ag.classify_needs(needs)
        self.assertEqual(hard, ["lint"])
        self.assertEqual(cancelled, ["pyright"])

    def test_unknown_or_malformed_need_results_fail_closed(self):
        for details in ({"result": "neutral"}, {"result": None}, {}):
            with self.subTest(details=details):
                hard, cancelled = ag.classify_needs({"speed": details})
                self.assertEqual(hard, ["speed"])
                self.assertEqual(cancelled, [])

    def test_changes_must_succeed(self):
        for result in ("failure", "cancelled", "skipped"):
            needs = _all_success()
            needs["changes"]["result"] = result
            with self.subTest(result=result):
                with self.assertRaises(ag.AggregationError):
                    ag.decide(needs, _applicability())

    def test_all_successful_required_jobs_pass(self):
        result = ag.decide(_all_success(), _applicability())
        self.assertEqual(set(result), set(cd.GATES))
        self.assertEqual({row.verdict for row in result.values()},
                         {ag.PASSED})

    def test_applicable_skips_and_successful_nonapplicable_jobs_fail(self):
        needs = _all_success()
        needs["unittest"]["result"] = "skipped"
        result = ag.decide(needs, _applicability())
        self.assertEqual(result["tests"].verdict, ag.FAILED)

        needs = _all_success()
        result = ag.decide(needs, _applicability(actionlint="skip"))
        self.assertEqual(result["actionlint"].verdict, ag.FAILED)

    def test_skips_pass_only_when_classification_says_skip(self):
        needs = _all_success()
        needs["unittest"]["result"] = "skipped"
        needs["actionlint"]["result"] = "skipped"
        result = ag.decide(
            needs, _applicability(tests="skip", actionlint="skip"))
        self.assertEqual(result["tests"].verdict, ag.SKIPPED)
        self.assertEqual(result["actionlint"].verdict, ag.SKIPPED)

    def test_failed_and_unknown_gate_results_fail_their_rows(self):
        for state in ("failure", "timed_out", "neutral", "unexpected"):
            needs = _all_success()
            needs["speed"]["result"] = state
            with self.subTest(state=state):
                result = ag.decide(needs, _applicability())
                self.assertEqual(result["speed"].verdict, ag.FAILED)
                self.assertEqual(result["tests"].verdict, ag.PASSED)

    def test_missing_and_extra_need_jobs_are_rejected(self):
        needs = _all_success()
        del needs["actionlint"]
        with self.assertRaises(ag.AggregationError):
            ag.decide(needs, _applicability())
        needs = _all_success()
        needs["unexpected"] = {"result": "success"}
        with self.assertRaises(ag.AggregationError):
            ag.decide(needs, _applicability())


class CancelledRunMixin:
    def _cancelled_speed(self, mine, *candidates):
        run_id = str(mine["id"])
        own_path = f"repos/owner/repo/actions/runs/{run_id}"
        branch = mine["head_branch"]
        list_path = (
            "repos/owner/repo/actions/workflows/tests.yml/runs"
            f"?branch={branch}&per_page=100")
        transport = FakeTransport(responses={
            own_path: mine, list_path: [mine, *candidates],
        })
        needs = _all_success()
        needs["speed"]["result"] = "cancelled"
        results = ag.evaluate(
            needs, _applicability(), repository="owner/repo", run_id=run_id,
            branch=branch, transport=transport)
        return results, transport


class SupersessionTests(CancelledRunMixin, unittest.TestCase):
    def setUp(self):
        self.mine = _run(1, "2026-09-07T10:00:00Z")
        self.newer = _run(2, "2026-09-07T10:05:00Z")

    def test_only_a_strictly_newer_same_workflow_same_pr_run_supersedes(self):
        proof = ag.superseding_run(
            self.mine, [self.mine, self.newer], "feature")
        self.assertIsNotNone(proof)
        assert proof is not None
        self.assertIs(proof.run, self.newer)
        self.assertFalse(proof.degraded)
        other_pr = _run(3, "2026-09-07T10:06:00Z",
                        pull_requests=[{"number": 43}])
        other_repository = _run(
            4, "2026-09-07T10:07:00Z",
            head_repository={"id": 101, "full_name": "other/repo"})
        other_workflow = _run(4, "2026-09-07T10:07:00Z", workflow=12)
        self.assertIsNone(ag.superseding_run(
            self.mine, [self.mine, other_pr, other_repository,
                        other_workflow], "feature"))

    def test_equal_start_times_use_run_id_as_the_tie_breaker(self):
        tied = _run(2, "2026-09-07T10:00:00Z")
        proof = ag.superseding_run(self.mine, [self.mine, tied], "feature")
        self.assertIsNotNone(proof)
        assert proof is not None
        self.assertIs(proof.run, tied)
        self.assertIsNone(ag.superseding_run(
            tied, [self.mine, tied], "feature"))

    def test_created_time_is_used_when_start_time_is_missing(self):
        mine = _run(1, None, status="in_progress",
                    created_at="2026-09-07T10:00:00Z")
        newer = _run(2, None, created_at="2026-09-07T10:05:00Z")
        proof = ag.superseding_run(mine, [mine, newer], "feature")
        self.assertIsNotNone(proof)
        assert proof is not None
        self.assertIs(proof.run, newer)

    def test_current_run_without_valid_time_cannot_be_proven_superseded(self):
        older = _run(19, "2026-09-07T09:00:00Z")
        for started in (None, "not-a-time"):
            with self.subTest(started=started):
                mine = _run(20, started)
                with self.assertRaises(ag.QueryError):
                    ag.superseding_run(mine, [older], "feature")

    def test_naive_and_malformed_times_are_handled_conservatively(self):
        mine = _run(1, "2026-09-07T10:00:00", status="in_progress")
        earlier = _run(2, "2026-09-07T11:00:00+02:00")
        later = _run(3, "2026-09-07T13:00:00")
        self.assertIsNone(ag.superseding_run(
            mine, [mine, earlier], "feature"))
        proof = ag.superseding_run(mine, [mine, later], "feature")
        self.assertIsNotNone(proof)
        assert proof is not None
        self.assertIs(proof.run, later)
        malformed = _run(4, "not-a-time", created_at="2026-09-07T15:00:00Z")
        self.assertIsNone(ag.superseding_run(
            later, [later, malformed], "feature"))

    def test_workflow_identity_and_branch_must_be_available(self):
        with self.assertRaises(ag.QueryError):
            ag.superseding_run({"id": 1, "head_branch": "feature"},
                               [self.newer], "feature")
        with self.assertRaises(ag.QueryError):
            ag.superseding_run(self.mine, [self.newer], "other")

    def test_push_run_is_never_returned_as_superseded(self):
        mine = _run(1, "2026-09-07T10:00:00Z", event="push")
        newer = _run(2, "2026-09-07T10:05:00Z")
        self.assertIsNone(ag.superseding_run(mine, [newer], "feature"))

    def test_dispatch_run_is_never_returned_as_superseded(self):
        mine = _run(1, "2026-09-07T10:00:00Z", event="workflow_dispatch",
                    pull_requests=[])
        newer = _run(2, "2026-09-07T10:05:00Z")
        self.assertIsNone(ag.superseding_run(mine, [newer], "feature"))

    def test_cancelled_need_is_proven_by_the_tests_workflow_runs_api(self):
        own_path = "repos/owner/repo/actions/runs/1"
        list_path = (
            "repos/owner/repo/actions/workflows/tests.yml/runs"
            "?branch=feature%2Fx&per_page=100")
        mine = _run(1, "2026-09-07T10:00:00Z", branch="feature/x",
                    pull_requests=[{"number": 42}])
        newer = _run(2, "2026-09-07T10:05:00Z", branch="feature/x",
                     pull_requests=[{"number": 42}])
        transport = FakeTransport(responses={
            own_path: mine,
            list_path: [mine, newer],
        })
        needs = _all_success()
        needs["speed"]["result"] = "cancelled"
        result = ag.evaluate(
            needs, _applicability(), repository="owner/repo", run_id="1",
            branch="feature/x", transport=transport)
        self.assertEqual(result["speed"].verdict, ag.SUPERSEDED)
        self.assertIn("run 2", result["speed"].detail)
        self.assertEqual(transport.calls, [
            (own_path, False, True), (list_path, True, True)])

    def test_pr_run_is_not_superseded_by_same_pr_push_run(self):
        mine = _run(1, "2026-09-07T10:00:00Z",
                    pull_requests=[{"number": 42}])
        newer = _run(2, "2026-09-07T10:05:00Z", event="push",
                     pull_requests=[{"number": 42}])
        results, _ = self._cancelled_speed(mine, newer)
        self.assertEqual(results["speed"].verdict, ag.FAILED)
        self.assertEqual(ag.exit_code(_with_secret_result(results)), 1)

    def test_push_cancellation_is_never_excused_by_a_newer_pr_run(self):
        mine = _run(1, "2026-09-07T10:00:00Z", event="push")
        newer = _run(2, "2026-09-07T10:05:00Z")
        results, transport = self._cancelled_speed(mine, newer)
        self.assertEqual(results["speed"].verdict, ag.FAILED)
        self.assertIn("deliberate", results["speed"].detail)
        self.assertEqual(ag.exit_code(_with_secret_result(results)), 1)
        self.assertEqual(transport.calls,
                         [("repos/owner/repo/actions/runs/1", False, True)])

    def test_dispatch_cancellation_is_never_excused_by_a_newer_dispatch(self):
        mine = _run(1, "2026-09-07T10:00:00Z", event="workflow_dispatch",
                    pull_requests=[])
        newer = _run(2, "2026-09-07T10:05:00Z",
                     event="workflow_dispatch", pull_requests=[])
        results, transport = self._cancelled_speed(mine, newer)
        self.assertEqual(results["speed"].verdict, ag.FAILED)
        self.assertIn("deliberate", results["speed"].detail)
        self.assertEqual(ag.exit_code(_with_secret_result(results)), 1)
        self.assertEqual(transport.calls,
                         [("repos/owner/repo/actions/runs/1", False, True)])

    def test_newer_pull_request_run_does_not_supersede_cancelled_push(self):
        own_path = "repos/owner/repo/actions/runs/1"
        list_path = (
            "repos/owner/repo/actions/workflows/tests.yml/runs"
            "?branch=feature&per_page=100")
        mine = _run(1, "2026-09-07T10:00:00Z", event="push")
        newer = _run(2, "2026-09-07T10:05:00Z", event="pull_request")
        transport = FakeTransport(responses={
            own_path: mine, list_path: [mine, newer],
        })
        needs = _all_success()
        needs["speed"]["result"] = "cancelled"
        result = ag.evaluate(
            needs, _applicability(), repository="owner/repo", run_id="1",
            branch="feature", transport=transport)
        self.assertEqual(result["speed"].verdict, ag.FAILED)
        self.assertIn("deliberate", result["speed"].detail)
        self.assertEqual(ag.exit_code(_with_secret_result(result)), 1)

    def test_deliberate_cancel_fails_and_does_not_fail_other_gate_rows(self):
        own_path = "repos/owner/repo/actions/runs/1"
        list_path = (
            "repos/owner/repo/actions/workflows/tests.yml/runs"
            "?branch=feature&per_page=100")
        mine = _run(1, "2026-09-07T10:00:00Z")
        transport = FakeTransport(responses={
            own_path: mine, list_path: [mine],
        })
        needs = _all_success()
        needs["speed"]["result"] = "cancelled"
        result = ag.evaluate(
            needs, _applicability(), repository="owner/repo", run_id="1",
            branch="feature", transport=transport)
        self.assertEqual(result["speed"].verdict, ag.FAILED)
        self.assertIn("deliberate", result["speed"].detail)
        self.assertEqual(result["tests"].verdict, ag.PASSED)

    def test_query_failure_raises_instead_of_excusing_a_cancel(self):
        needs = _all_success()
        needs["speed"]["result"] = "cancelled"
        transport = FakeTransport(error=ag.QueryError("API offline"))
        with self.assertRaisesRegex(ag.QueryError, "API offline"):
            ag.evaluate(
                needs, _applicability(), repository="owner/repo", run_id="1",
                branch="feature", transport=transport)

    def test_non_cancelled_needs_do_not_query_runs(self):
        transport = FakeTransport()
        result = ag.evaluate(
            _all_success(), _applicability(), repository="owner/repo",
            run_id="1", branch="feature", transport=transport)
        self.assertEqual({row.verdict for row in result.values()},
                         {ag.PASSED})
        self.assertEqual(transport.calls, [])


class SupersessionAttributionTests(CancelledRunMixin, unittest.TestCase):
    def test_newer_same_branch_run_from_another_head_repository_is_deliberate(self):
        mine = _run(1, "2026-09-07T10:00:00Z",
                    pull_requests=[{"number": 42}])
        newer = _run(
            2, "2026-09-07T10:05:00Z",
            head_repository={"id": 101, "full_name": "other/repo"},
            pull_requests=[{"number": 42}])
        results, _ = self._cancelled_speed(mine, newer)
        self.assertEqual(results["speed"].verdict, ag.FAILED)
        self.assertIn("deliberate", results["speed"].detail)
        self.assertEqual(ag.exit_code(_with_secret_result(results)), 1)

    def test_newer_run_with_a_different_pr_number_is_deliberate(self):
        mine = _run(1, "2026-09-07T10:00:00Z",
                    pull_requests=[{"number": 42}])
        newer = _run(2, "2026-09-07T10:05:00Z",
                     pull_requests=[{"number": 43}])
        results, _ = self._cancelled_speed(mine, newer)
        self.assertEqual(results["speed"].verdict, ag.FAILED)
        self.assertIn("deliberate", results["speed"].detail)
        self.assertEqual(ag.exit_code(_with_secret_result(results)), 1)

    def test_unattributed_fork_pr_uses_degraded_same_repo_branch_proof(self):
        mine = _run(1, "2026-09-07T10:00:00Z", pull_requests=[])
        newer = _run(2, "2026-09-07T10:05:00Z", pull_requests=[])
        results, _ = self._cancelled_speed(mine, newer)
        self.assertEqual(results["speed"].verdict, ag.SUPERSEDED)
        self.assertIn("proof degraded", results["speed"].detail)
        self.assertIn("same head repository and head branch",
                      results["speed"].detail)
        self.assertIn("two PRs from one fork branch are indistinguishable",
                      results["speed"].detail)
        self.assertEqual(ag.exit_code(_with_secret_result(results)), 0)

    def test_missing_association_on_either_run_uses_degraded_fallback(self):
        attributed = _run(1, "2026-09-07T10:00:00Z",
                          pull_requests=[{"number": 42}])
        unattributed = _run(2, "2026-09-07T10:05:00Z", pull_requests=[])
        results, _ = self._cancelled_speed(attributed, unattributed)
        self.assertEqual(results["speed"].verdict, ag.SUPERSEDED)
        self.assertIn("proof degraded", results["speed"].detail)

        unattributed_mine = _run(
            3, "2026-09-07T10:00:00Z", pull_requests=[])
        attributed_newer = _run(
            4, "2026-09-07T10:05:00Z", pull_requests=[{"number": 42}])
        results, _ = self._cancelled_speed(
            unattributed_mine, attributed_newer)
        self.assertEqual(results["speed"].verdict, ag.SUPERSEDED)
        self.assertIn("proof degraded", results["speed"].detail)

    def test_unattributed_fork_pr_requires_the_same_branch(self):
        mine = _run(1, "2026-09-07T10:00:00Z", pull_requests=[])
        newer = _run(2, "2026-09-07T10:05:00Z", branch="other",
                     pull_requests=[])
        results, _ = self._cancelled_speed(mine, newer)
        self.assertEqual(results["speed"].verdict, ag.FAILED)
        self.assertIn("deliberate", results["speed"].detail)
        self.assertEqual(ag.exit_code(_with_secret_result(results)), 1)


class SecretScanTests(unittest.TestCase):
    def setUp(self):
        self.secret_run = _secret_run(7, "2026-09-07T11:00:00Z")
        self.jobs_path = "repos/owner/repo/actions/runs/7/jobs?per_page=100"

    def _scan(self, runs, jobs, *, bound=30, clock=None):
        transport = FakeTransport({
            SECRETS_RUNS_PATH: runs,
            self.jobs_path: jobs,
        })
        timer = clock or FakeClock()
        result = ag.require_secret_scan(
            "owner/repo", HEAD_SHA, bound, transport,
            clock=timer.clock, sleep=timer.sleep)
        return result, transport, timer

    def test_green_dispatch_requires_and_reports_a_successful_gitleaks_job(self):
        transport = FakeTransport({
            SECRETS_RUNS_PATH: [self.secret_run],
            self.jobs_path: [_secret_job()],
        })
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = ag.main(environ=_dispatch_environment(),
                             transport=transport)
        self.assertEqual(status, 0)
        self.assertIn("| secrets | PASSED |", stdout.getvalue())
        self.assertEqual(ag.exit_code(_with_secret_result(
            ag.decide(_all_success(), _applicability()))), 0)

    def test_run_success_does_not_hide_a_failed_gitleaks_job(self):
        run = _secret_run(7, "2026-09-07T11:00:00Z", conclusion="success")
        transport = FakeTransport({
            SECRETS_RUNS_PATH: [run],
            self.jobs_path: [_secret_job("failure")],
        })
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = ag.main(environ=_dispatch_environment(),
                             transport=transport)
        self.assertEqual(status, 1)
        row = next(line for line in stdout.getvalue().splitlines()
                   if line.startswith("| secrets |"))
        self.assertIn("FAILED", row)
        self.assertIn("gitleaks", row)
        self.assertIn("concluded failure", row)

    def test_failed_run_summary_does_not_override_a_successful_gitleaks_job(self):
        run = _secret_run(7, "2026-09-07T11:00:00Z", conclusion="failure")
        transport = FakeTransport({
            SECRETS_RUNS_PATH: [run],
            self.jobs_path: [_secret_job("success")],
        })
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = ag.main(environ=_dispatch_environment(),
                             transport=transport)
        self.assertEqual(status, 0)
        self.assertIn("| secrets | PASSED |", stdout.getvalue())

    def test_runs_returned_for_another_head_sha_are_rejected(self):
        wrong_sha = _secret_run(7, "2026-09-07T11:00:00Z")
        wrong_sha["head_sha"] = "b" * 40
        transport = FakeTransport({SECRETS_RUNS_PATH: [wrong_sha]})
        timer = FakeClock()
        result = ag.require_secret_scan(
            "owner/repo", HEAD_SHA, 30, transport,
            clock=timer.clock, sleep=timer.sleep)
        self.assertEqual(result.verdict, ag.FAILED)
        self.assertIn("outside HEAD_SHA", result.detail)
        self.assertEqual(len(transport.calls), 1)

    def test_newest_run_on_the_sha_wins_independent_of_response_order(self):
        older = _secret_run(8, "2026-09-07T11:00:00Z")
        newer = _secret_run(9, "2026-09-07T11:05:00Z", conclusion="failure")
        transport = FakeTransport({
            SECRETS_RUNS_PATH: [older, newer],
            "repos/owner/repo/actions/runs/9/jobs?per_page=100": [
                _secret_job("failure")],
        })
        timer = FakeClock()
        result = ag.require_secret_scan(
            "owner/repo", HEAD_SHA, 30, transport,
            clock=timer.clock, sleep=timer.sleep)
        self.assertEqual(result.verdict, ag.FAILED)
        self.assertIn("run 9", result.detail)
        self.assertIn("failure", result.detail)

        newer_success = _secret_run(11, "2026-09-07T11:05:00Z")
        older_failure = _secret_run(10, "2026-09-07T11:00:00Z",
                                    conclusion="failure")
        transport = FakeTransport({
            SECRETS_RUNS_PATH: [newer_success, older_failure],
            "repos/owner/repo/actions/runs/11/jobs?per_page=100": [
                _secret_job()],
        })
        timer = FakeClock()
        result = ag.require_secret_scan(
            "owner/repo", HEAD_SHA, 30, transport,
            clock=timer.clock, sleep=timer.sleep)
        self.assertEqual(result.verdict, ag.PASSED)
        self.assertIn("run 11", result.detail)

    def test_newest_run_uses_created_time_then_numeric_id(self):
        older = _secret_run(20, None, created="2026-09-07T11:00:00Z")
        newer_lower_id = _secret_run(21, None,
                                     created="2026-09-07T11:05:00Z")
        newest = _secret_run(22, None, created="2026-09-07T11:05:00Z")
        transport = FakeTransport({
            SECRETS_RUNS_PATH: [newest, older, newer_lower_id],
            "repos/owner/repo/actions/runs/22/jobs?per_page=100": [
                _secret_job()],
        })
        timer = FakeClock()
        result = ag.require_secret_scan(
            "owner/repo", HEAD_SHA, 30, transport,
            clock=timer.clock, sleep=timer.sleep)
        self.assertEqual(result.verdict, ag.PASSED)
        self.assertIn("run 22", result.detail)

    def test_no_run_within_bound_fails_with_its_own_detail(self):
        result, transport, _ = self._scan([], [], bound=30)
        self.assertEqual(result.verdict, ag.FAILED)
        self.assertIn("no secrets.yml run", result.detail)
        self.assertIn(HEAD_SHA, result.detail)
        self.assertGreater(len(transport.calls), 1)

    def test_unfinished_newest_run_at_bound_fails_with_status_detail(self):
        running = _secret_run(7, "2026-09-07T11:00:00Z",
                              status="in_progress", conclusion=None)
        result, _transport, _ = self._scan([running], [], bound=30)
        self.assertEqual(result.verdict, ag.FAILED)
        self.assertIn("run 7", result.detail)
        self.assertIn("in_progress", result.detail)
        self.assertIn("after 30 seconds", result.detail)

    def test_run_without_a_gitleaks_job_fails_and_names_available_jobs(self):
        result, _transport, _ = self._scan(
            [self.secret_run], [_secret_job(name="setup")])
        self.assertEqual(result.verdict, ag.FAILED)
        self.assertIn("no job named gitleaks", result.detail)
        self.assertIn("setup", result.detail)

    def test_every_non_success_gitleaks_conclusion_fails_with_that_conclusion(self):
        for conclusion in ("skipped", "cancelled", "timed_out", "neutral", None):
            with self.subTest(conclusion=conclusion):
                result, _transport, _ = self._scan(
                    [self.secret_run], [_secret_job(conclusion)])
                self.assertEqual(result.verdict, ag.FAILED)
                label = "no conclusion" if conclusion is None else conclusion
                self.assertIn(f"concluded {label}", result.detail)

    def test_deadline_accepts_a_success_inside_and_rejects_one_outside(self):
        inside_clock = FakeClock([239.9])
        inside_transport = FakeTransport({
            SECRETS_RUNS_PATH: deque([[], [self.secret_run]]),
            self.jobs_path: [_secret_job()],
        })
        inside = ag.require_secret_scan(
            "owner/repo", HEAD_SHA, 240, inside_transport,
            clock=inside_clock.clock, sleep=inside_clock.sleep)
        self.assertEqual(inside.verdict, ag.PASSED)

        outside_clock = FakeClock([240.1])
        outside_transport = FakeTransport({
            SECRETS_RUNS_PATH: deque([[], [self.secret_run]]),
            self.jobs_path: [_secret_job()],
        })
        outside = ag.require_secret_scan(
            "owner/repo", HEAD_SHA, 240, outside_transport,
            clock=outside_clock.clock, sleep=outside_clock.sleep)
        self.assertEqual(outside.verdict, ag.FAILED)
        self.assertIn("within 240 seconds", outside.detail)
        self.assertEqual(len(outside_transport.calls), 1)

    def test_queries_pin_head_sha_pagination_and_the_jobs_path(self):
        jobs_path = "repos/owner/repo/actions/runs/7/jobs?per_page=100"
        transport = FakeTransport({
            SECRETS_RUNS_PATH: [self.secret_run],
            jobs_path: [_secret_job()],
        })
        result = ag.require_secret_scan(
            "owner/repo", HEAD_SHA, 30, transport,
            clock=FakeClock().clock, sleep=FakeClock().sleep)
        self.assertEqual(result.verdict, ag.PASSED)
        self.assertEqual(transport.calls, [
            (SECRETS_RUNS_PATH, True, True),
            (jobs_path, True, True),
        ])

    def test_gh_argv_queries_the_head_sha_with_no_cache_and_pagination(self):
        jobs_path = "repos/owner/repo/actions/runs/7/jobs?per_page=100"
        responses = [
            subprocess.CompletedProcess(
                ["gh"], 0, json.dumps([{"workflow_runs": [self.secret_run]}]), ""),
            subprocess.CompletedProcess(
                ["gh"], 0, json.dumps([{"jobs": [_secret_job()]}]), ""),
        ]
        with mock.patch.object(cd.subprocess, "run", side_effect=responses) as gh:
            result = ag.require_secret_scan(
                "owner/repo", HEAD_SHA, 30, cd.GhTransport({}),
                clock=FakeClock().clock, sleep=FakeClock().sleep)
        self.assertEqual(result.verdict, ag.PASSED)
        argv = gh.call_args_list[0].args[0]
        self.assertEqual(argv, [
            "gh", "api", "--method", "GET",
            "-H", "Accept: application/vnd.github+json",
            "-H", "Cache-Control: no-cache",
            "--paginate", "--slurp", SECRETS_RUNS_PATH,
        ])
        self.assertEqual(gh.call_args_list[1].args[0][-1], jobs_path)

    def test_transport_error_fails_closed_with_a_query_message(self):
        transport = FakeTransport(error=OSError("connection unavailable"))
        result = ag.require_secret_scan(
            "owner/repo", HEAD_SHA, 30, transport,
            clock=FakeClock().clock, sleep=FakeClock().sleep)
        self.assertEqual(result.verdict, ag.FAILED)
        self.assertIn("could not read", result.detail)
        self.assertIn("connection unavailable", result.detail)

    def test_nonzero_gh_exit_and_rate_limit_fail_closed(self):
        error = subprocess.CalledProcessError(
            1, ["gh"], stderr="API rate limit exceeded")
        with mock.patch.object(cd.subprocess, "run", side_effect=error):
            result = ag.require_secret_scan(
                "owner/repo", HEAD_SHA, 30, cd.GhTransport({}),
                clock=FakeClock().clock, sleep=FakeClock().sleep)
        self.assertEqual(result.verdict, ag.FAILED)
        self.assertIn("rate limit", result.detail)

    def test_unparseable_api_body_fails_closed_with_its_own_message(self):
        malformed = subprocess.CompletedProcess(["gh"], 0, "not JSON", "")
        with mock.patch.object(cd.subprocess, "run", return_value=malformed):
            result = ag.require_secret_scan(
                "owner/repo", HEAD_SHA, 30, cd.GhTransport({}),
                clock=FakeClock().clock, sleep=FakeClock().sleep)
        self.assertEqual(result.verdict, ag.FAILED)
        self.assertIn("could not read", result.detail)
        self.assertIn("JSON", result.detail)

    def test_workflow_dispatch_without_a_scan_fails_after_the_bound(self):
        transport = FakeTransport({SECRETS_RUNS_PATH: []})
        timer = FakeClock()
        stdout = io.StringIO()
        with mock.patch.object(ag.time, "monotonic", timer.clock), \
                mock.patch.object(ag.time, "sleep", timer.sleep), \
                contextlib.redirect_stdout(stdout):
            status = ag.main(environ=_dispatch_environment(),
                             transport=transport)
        self.assertEqual(status, 1)
        row = next(line for line in stdout.getvalue().splitlines()
                   if line.startswith("| secrets |"))
        self.assertIn("FAILED", row)
        self.assertIn("no secrets.yml run", row)


class AggregateSummaryTests(unittest.TestCase):
    def test_table_includes_every_gate_and_escapes_details(self):
        result = ag.decide(_all_success(), _applicability())
        result["tests"] = ag.Result(ag.FAILED, "bad | detail\nnext")
        result["secrets"] = ag.Result(ag.PASSED, "clean")
        rendered = ag.render_summary(result)
        self.assertIn("### Aggregate CI gates", rendered)
        self.assertIn("| Gate | Result | Detail |", rendered)
        self.assertIn("| tests | FAILED | bad \\| detail next |", rendered)
        self.assertEqual(ag.exit_code(result), 1)

    def test_exit_code_requires_the_secrets_row(self):
        results = ag.decide(_all_success(), _applicability())
        with self.assertRaisesRegex(ag.AggregationError, "incomplete"):
            ag.exit_code(results)

    def test_failed_needs_do_not_wait_for_the_secret_scan(self):
        environment = _dispatch_environment()
        needs = _all_success()
        needs["speed"]["result"] = "failure"
        environment["NEEDS_JSON"] = json.dumps(needs)
        transport = FakeTransport()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = ag.main(environ=environment, transport=transport)
        self.assertEqual(status, 1)
        self.assertEqual(transport.calls, [])
        row = next(line for line in stdout.getvalue().splitlines()
                   if line.startswith("| secrets |"))
        self.assertIn("FAILED", row)
        self.assertIn("another aggregate gate already failed", row)

    def test_dispatch_main_writes_the_table_to_stdout_and_step_summary(self):
        with tempfile.TemporaryDirectory(prefix="ghw-aggregate-summary-") as temp:
            summary = Path(temp) / "summary.md"
            environment = {
                "NEEDS_JSON": json.dumps(_all_success()),
                "EVENT_NAME": "workflow_dispatch",
                "REPOSITORY": "owner/repo",
                "RUN_ID": "5",
                "HEAD_BRANCH": "feature",
                "HEAD_SHA": "a" * 40,
                "DEFAULT_BRANCH": "main",
                "EVENT_PATH": "",
                "GITHUB_STEP_SUMMARY": str(summary),
            }
            transport = FakeTransport({
                SECRETS_RUNS_PATH: [
                    _secret_run(7, "2026-09-07T11:00:00Z")],
                "repos/owner/repo/actions/runs/7/jobs?per_page=100": [
                    _secret_job()],
            })
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                status = ag.main(environ=environment,
                                 transport=transport)
            self.assertEqual(status, 0)
            self.assertIn("### Aggregate CI gates", stdout.getvalue())
            self.assertEqual(summary.read_text(encoding="utf-8"),
                             stdout.getvalue())

    def test_any_exception_renders_all_rows_failed(self):
        with tempfile.TemporaryDirectory(prefix="ghw-aggregate-error-") as temp:
            summary = Path(temp) / "summary.md"
            environment = {
                "NEEDS_JSON": "{",
                "EVENT_NAME": "workflow_dispatch",
                "GITHUB_STEP_SUMMARY": str(summary),
            }
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout):
                with contextlib.redirect_stderr(stderr):
                    status = ag.main(environ=environment,
                                     transport=FakeTransport())
            self.assertEqual(status, 1)
            self.assertEqual(
                sum(1 for line in stdout.getvalue().splitlines()
                    if line.startswith("| ") and " | FAILED | " in line),
                len(cd.GATES) + 1)
            self.assertIn("NEEDS_JSON", stderr.getvalue())
            self.assertEqual(summary.read_text(encoding="utf-8"),
                             stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
