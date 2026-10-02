"""Needs-based verdicts for the consolidated CI aggregate job."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.ci import aggregate_gate as ag
from scripts.ci import changes_detect as cd


def _needs(**results):
    return {name: {"result": result} for name, result in results.items()}


def _all_success():
    needs = _needs(changes="success")
    needs.update({job: {"result": "success"}
                  for job in ag.GATE_JOBS.values()})
    return needs


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
        if isinstance(response, Exception):
            raise response
        return response


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


class SupersessionTests(unittest.TestCase):
    def setUp(self):
        self.mine = _run(1, "2026-09-07T10:00:00Z")
        self.newer = _run(2, "2026-09-07T10:05:00Z")

    def test_only_a_strictly_newer_same_workflow_same_branch_run_supersedes(self):
        self.assertIs(ag.superseding_run(
            self.mine, [self.mine, self.newer], "feature"), self.newer)
        other_branch = _run(3, "2026-09-07T10:06:00Z", branch="other")
        other_workflow = _run(4, "2026-09-07T10:07:00Z", workflow=12)
        self.assertIsNone(ag.superseding_run(
            self.mine, [self.mine, other_branch, other_workflow], "feature"))

    def test_equal_start_times_use_run_id_as_the_tie_breaker(self):
        tied = _run(2, "2026-09-07T10:00:00Z")
        self.assertIs(ag.superseding_run(
            self.mine, [self.mine, tied], "feature"), tied)
        self.assertIsNone(ag.superseding_run(
            tied, [self.mine, tied], "feature"))

    def test_created_time_is_used_when_start_time_is_missing(self):
        mine = _run(1, None, status="in_progress",
                    created_at="2026-09-07T10:00:00Z")
        newer = _run(2, None, created_at="2026-09-07T10:05:00Z")
        self.assertIs(ag.superseding_run(mine, [mine, newer], "feature"),
                      newer)

    def test_naive_and_malformed_times_are_handled_conservatively(self):
        mine = _run(1, "2026-09-07T10:00:00", status="in_progress")
        earlier = _run(2, "2026-09-07T11:00:00+02:00")
        later = _run(3, "2026-09-07T13:00:00")
        self.assertIsNone(ag.superseding_run(
            mine, [mine, earlier], "feature"))
        self.assertIs(ag.superseding_run(
            mine, [mine, later], "feature"), later)
        malformed = _run(4, "not-a-time", created_at="2026-09-07T15:00:00Z")
        self.assertIsNone(ag.superseding_run(
            later, [later, malformed], "feature"))

    def test_workflow_identity_and_branch_must_be_available(self):
        with self.assertRaises(ag.QueryError):
            ag.superseding_run({"id": 1, "head_branch": "feature"},
                               [self.newer], "feature")
        with self.assertRaises(ag.QueryError):
            ag.superseding_run(self.mine, [self.newer], "other")

    def test_cancelled_need_is_proven_by_the_tests_workflow_runs_api(self):
        own_path = "repos/owner/repo/actions/runs/1"
        list_path = (
            "repos/owner/repo/actions/workflows/.github/workflows/tests.yml/runs"
            "?branch=feature%2Fx&per_page=100")
        mine = _run(1, "2026-09-07T10:00:00Z", branch="feature/x")
        newer = _run(2, "2026-09-07T10:05:00Z", branch="feature/x")
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

    def test_deliberate_cancel_fails_and_does_not_fail_other_gate_rows(self):
        own_path = "repos/owner/repo/actions/runs/1"
        list_path = (
            "repos/owner/repo/actions/workflows/.github/workflows/tests.yml/runs"
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


class AggregateSummaryTests(unittest.TestCase):
    def test_table_includes_every_gate_and_escapes_details(self):
        result = ag.decide(_all_success(), _applicability())
        result["tests"] = ag.Result(ag.FAILED, "bad | detail\nnext")
        rendered = ag.render_summary(result)
        self.assertIn("### Aggregate CI gates", rendered)
        self.assertIn("| Gate | Result | Detail |", rendered)
        self.assertIn("| tests | FAILED | bad \\| detail next |", rendered)
        self.assertEqual(ag.exit_code(result), 1)

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
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                status = ag.main(environ=environment,
                                 transport=FakeTransport())
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
                len(cd.GATES))
            self.assertIn("NEEDS_JSON", stderr.getvalue())
            self.assertEqual(summary.read_text(encoding="utf-8"),
                             stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
