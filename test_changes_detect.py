"""Path classification for the consolidated CI gate selector."""
import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.ci import changes_detect as cd


class FakeTransport:
    """Return API-shaped data and record the request contract."""

    def __init__(self, responses=None, error=None):
        self.responses = responses or {}
        self.error = error
        self.calls = []
        self.timeouts = []

    def api(self, path, *, paginate=False, no_cache=True, timeout=None):
        self.calls.append((path, paginate, no_cache))
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        if path not in self.responses:
            raise AssertionError(f"unexpected API call: {path}")
        return self.responses[path]


class ListResponseTests(unittest.TestCase):
    def test_gh_page_shapes_flatten_and_malformed_items_fail(self):
        expected = [{"filename": "src/one.py"},
                    {"filename": "src/two.py"}]
        responses = (
            {"workflow_runs": expected},
            [[expected[0]], [expected[1]]],
            [{"workflow_runs": [expected[0]]},
             {"workflow_runs": [expected[1]]}],
        )
        path = "repos/owner/repo/pulls/37/files?per_page=100"
        for response in responses:
            with self.subTest(response=response):
                files, capped = cd.changed_files(
                    FakeTransport(responses={path: response}), "owner/repo",
                    event="pull_request", sha="a" * 40, pr_number="37")
                self.assertEqual(files, {"src/one.py", "src/two.py"})
                self.assertFalse(capped)
        for response in (None, {"other": []}, [{"id": 1}, None]):
            with self.subTest(response=response):
                with self.assertRaises(cd.DetectionError):
                    cd.changed_files(
                        FakeTransport(responses={path: response}),
                        "owner/repo", event="pull_request", sha="a" * 40,
                        pr_number="37")

    def test_filename_validation_rejects_missing_paths_and_rename_sources(self):
        invalid = (
            None,
            [{}],
            [{"filename": 1}],
            [{"filename": "new.py", "status": "renamed"}],
        )
        path = "repos/owner/repo/pulls/37/files?per_page=100"
        for response in invalid:
            with self.subTest(response=response):
                with self.assertRaises(cd.DetectionError):
                    cd.changed_files(
                        FakeTransport(responses={path: response}),
                        "owner/repo", event="pull_request", sha="a" * 40,
                        pr_number="37")


class GhTransportTests(unittest.TestCase):
    def test_api_paginates_and_sends_no_cache_headers(self):
        pages = [[{"filename": "one.py"}], [{"filename": "two.py"}]]
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(pages), stderr="")
        with patch.object(cd.subprocess, "run", return_value=completed) as run:
            files = cd.GhTransport({"GH_TOKEN": "test-token"}).api(
                "repos/owner/repo/pulls/7/files", paginate=True)
        self.assertEqual(files, pages[0] + pages[1])
        command = run.call_args.args[0]
        self.assertIn("Cache-Control: no-cache", command)
        self.assertIn("--paginate", command)
        self.assertIn("--slurp", command)
        self.assertEqual(command[-1], "repos/owner/repo/pulls/7/files")
        self.assertEqual(run.call_args.kwargs["env"], {"GH_TOKEN": "test-token"})

    def test_api_flattens_paginated_job_pages(self):
        jobs = [{"name": "gitleaks"}, {"name": "summary"}]
        pages = [{"jobs": [jobs[0]]}, {"jobs": [jobs[1]]}]
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(pages), stderr="")
        with patch.object(cd.subprocess, "run", return_value=completed):
            result = cd.GhTransport({}).api(
                "repos/owner/repo/actions/runs/7/jobs", paginate=True)
        self.assertEqual(result, jobs)

    def test_api_flattens_paginated_workflow_run_pages(self):
        runs = [{"id": 7}, {"id": 8}]
        pages = [{"workflow_runs": [runs[0]]},
                 {"workflow_runs": [runs[1]]}]
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(pages), stderr="")
        with patch.object(cd.subprocess, "run", return_value=completed):
            result = cd.GhTransport({}).api(
                "repos/owner/repo/actions/workflows/secrets.yml/runs",
                paginate=True)
        self.assertEqual(result, runs)

    def test_api_rejects_malformed_nested_collection_on_any_page(self):
        cases = (
            ("jobs", [{"jobs": [{"name": "gitleaks"}]}, {"jobs": {}}]),
            ("jobs", [{"jobs": [{"name": "gitleaks"}]}, {"jobs": ""}]),
            ("jobs", [{"jobs": [{"name": "gitleaks"}]}, {"other": []}]),
            ("workflow_runs", [{"workflow_runs": [{"id": 7}]},
                               {"workflow_runs": {}}]),
        )
        for collection, pages in cases:
            with self.subTest(collection=collection, pages=pages):
                completed = subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=json.dumps(pages), stderr="")
                with patch.object(cd.subprocess, "run", return_value=completed):
                    with self.assertRaisesRegex(
                            cd.DetectionError,
                            f"malformed paginated {collection} collection"):
                        cd.GhTransport({}).api(
                            "repos/owner/repo/actions/resource",
                            paginate=True)

    def test_api_timeout_defaults_to_sixty_seconds_and_accepts_a_cap(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="{}", stderr="")
        transport = cd.GhTransport({})
        with patch.object(cd.subprocess, "run", return_value=completed) as run:
            transport.api("repos/owner/repo/resource")
            self.assertEqual(run.call_args.kwargs["timeout"], 60)

            transport.api("repos/owner/repo/resource", timeout=12.5)
            self.assertEqual(run.call_args.kwargs["timeout"], 12.5)

    def test_api_reports_cli_and_decode_failures(self):
        transport = cd.GhTransport({})
        with patch.object(cd.subprocess, "run",
                          side_effect=subprocess.CalledProcessError(
                              1, ["gh"], stderr="API unavailable")):
            with self.assertRaisesRegex(cd.DetectionError, "API unavailable"):
                transport.api("repos/owner/repo/compare/base...head")
        invalid_json = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not-json", stderr="")
        with patch.object(cd.subprocess, "run", return_value=invalid_json):
            with self.assertRaisesRegex(cd.DetectionError,
                                        "not valid JSON"):
                transport.api("repos/owner/repo/compare/base...head")
        with patch.object(cd.subprocess, "run",
                          side_effect=FileNotFoundError("gh missing")):
            with self.assertRaisesRegex(cd.DetectionError, "gh missing"):
                transport.api("repos/owner/repo/compare/base...head")

    def test_paginated_api_refuses_a_non_list_response(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="1", stderr="")
        with patch.object(cd.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(cd.DetectionError,
                                        "missing paginated JSON pages"):
                cd.GhTransport({}).api("repos/owner/repo/actions/runs",
                                       paginate=True)


class PathMatcherTests(unittest.TestCase):
    def test_actions_globs_respect_directory_boundaries_and_case(self):
        cases = [
            ("**/*.md", "README.md", True),
            ("**/*.md", "x/y.md", True),
            ("**/*.md", "docs/a/b.md", True),
            ("**/*.md", "code/a.py", False),
            ("**/*.md", "PRESENTATION.md.bak", False),
            ("**/*.md", "README.MD", False),
            ("docs/**", "docs/a.md", True),
            ("docs/**", "docs/a/b.md", True),
            ("docs/**", "docsx/a", False),
            ("docs/*", "docs/a.md", True),
            ("docs/*", "docs/a/b.md", False),
            (".github/workflows/**", ".github/workflows/tests.yml", True),
            ("LICENSE", "LICENSE", True),
            ("LICENSE", "x/LICENSE", False),
            ("LICENSE", "LICENSE.bak", False),
            ("docs/?.md", "docs/a.md", True),
            ("docs/?.md", "docs/ab.md", False),
            ("docs/?.md", "docs//.md", False),
            ("a/**/b.py", "a/b.py", True),
            ("a/**/b.py", "a/x/y/b.py", True),
            ("*.py", "x/a.py", False),
            ("file[1].py", "file[1].py", True),
            ("file[1].py", "file1.py", False),
            ("docs/**", "docs/evil\nname.py", True),
            ("**/*.md", "dir\nname/README.md", True),
            ("docs/*", "docs/evil\nname.py", True),
            ("docs/*", "docs/evil\nname/a.py", False),
        ]
        for pattern, path, expected in cases:
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(cd.github_path_matches(pattern, path),
                                 expected)


class ClassificationTests(unittest.TestCase):
    def test_docs_only_skips_the_deny_and_allow_gates(self):
        result = cd.classify({"README.md", "docs/a/b.md"})
        self.assertEqual(result.pop("gate-integrity"), "run")
        self.assertEqual(set(result.values()), {"skip"})

    def test_workflow_change_runs_code_gates_and_actionlint(self):
        result = cd.classify({".github/workflows/tests.yml"})
        self.assertEqual(set(result.values()), {"run"})
        manifest_result = cd.classify({"requirements-zizmor.txt"})
        self.assertEqual(manifest_result["actionlint"], "run")

    def test_code_change_skips_only_the_allow_gate(self):
        result = cd.classify({"new/code.py"})
        for name in ("tests", "lint", "types", "audit", "speed", "codeql"):
            self.assertEqual(result[name], "run")
        self.assertEqual(result["actionlint"], "skip")

    def test_codeql_keeps_its_seven_paths_and_runs_on_presentation_only(self):
        expected = ("**/*.md", "docs/**", "examples/**", ".claude/**",
                    "LICENSE", "NOTICE", ".gitignore")
        self.assertEqual(cd.CODEQL_IGNORES, expected)
        self.assertIn("PRESENTATION.txt", cd.CODE_IGNORES)
        self.assertNotIn("PRESENTATION.txt", cd.CODEQL_IGNORES)
        for path in ("README.md", "docs/a.md", "examples/example.py",
                     ".claude/settings.json", "LICENSE", "NOTICE",
                     ".gitignore"):
            with self.subTest(path=path):
                self.assertEqual(cd.classify({path})["codeql"], "skip")
        self.assertEqual(cd.classify({"PRESENTATION.txt"})["codeql"], "run")

    def test_cap_runs_all_gates_when_path_data_is_truncated(self):
        result = cd.classify({"README.md"}, capped=True)
        self.assertEqual(set(result.values()), {"run"})

    def test_schedules_run_only_the_matching_gate_without_api_reads(self):
        cases = {
            "12 4 * * *": "audit",
            "47 3 * * 3": "codeql",
            "0 0 * * *": None,
        }
        for cron, expected in cases.items():
            transport = FakeTransport()
            result = cd.classify_event(
                "schedule", transport=transport, repository="owner/repo",
                payload={}, schedule=cron)
            self.assertEqual(
                {name for name, decision in result.items()
                 if decision == "run"},
                ({expected, "gate-integrity"} if expected
                 else {"gate-integrity"}))
            self.assertEqual(transport.calls, [])

    def test_dispatch_runs_every_gate_without_an_api_read(self):
        transport = FakeTransport()
        result = cd.classify_event(
            "workflow_dispatch", transport=transport, repository="owner/repo",
            payload={})
        self.assertEqual(set(result.values()), {"run"})
        self.assertEqual(transport.calls, [])

    def test_tag_pushes_run_every_gate_before_classifying_paths(self):
        after = "a" * 40
        cases = (
            ("0" * 40, "main", {"status": "identical", "files": []}),
            ("b" * 40, "b" * 40,
             {"status": "ahead", "files": [{"filename": "README.md"}]}),
        )
        expected = {name: "run" for name in (
            "tests", "lint", "types", "audit", "speed", "codeql",
            "actionlint", "gate-integrity")}
        for before, comparison_base, response in cases:
            with self.subTest(before=before[:8], status=response["status"]):
                path = (f"repos/owner/repo/compare/{comparison_base}..."
                        f"{after}")
                transport = FakeTransport(responses={path: response})
                result = cd.classify_event(
                    "push", transport=transport, repository="owner/repo",
                    payload={"ref": "refs/tags/v1.0", "before": before,
                             "after": after},
                    sha=after, default_branch="main")
                self.assertEqual(result, expected)
                self.assertEqual(transport.calls, [])


class AcquisitionTests(unittest.TestCase):
    def test_api_wrapper_converts_unexpected_transport_errors(self):
        transport = FakeTransport(error=ValueError("bad response"))
        with self.assertRaisesRegex(cd.DetectionError, "bad response"):
            cd.changed_files(transport, "owner/repo", event="pull_request",
                             sha="a" * 40, pr_number="37")

    def test_changed_files_validates_event_identity_and_payload(self):
        with self.assertRaisesRegex(cd.DetectionError, "REPOSITORY"):
            cd.changed_files(FakeTransport(), "bad repo", event="push",
                             sha="a" * 40,
                             payload={"before": "b" * 40, "after": "a" * 40})
        with self.assertRaisesRegex(cd.DetectionError, "PR_NUMBER"):
            cd.changed_files(FakeTransport(), "owner/repo",
                             event="pull_request", sha="a" * 40)
        with self.assertRaisesRegex(cd.DetectionError, "does not match"):
            cd.changed_files(FakeTransport(), "owner/repo", event="push",
                             sha="c" * 40,
                             payload={"before": "b" * 40, "after": "a" * 40})
        with self.assertRaisesRegex(cd.DetectionError, "cannot acquire"):
            cd.changed_files(FakeTransport(), "owner/repo", event="release",
                             sha="a" * 40)

    def test_pr_payload_supplies_number_and_bad_filenames_fail(self):
        path = "repos/owner/repo/pulls/37/files?per_page=100"
        transport = FakeTransport(responses={path: [{"filename": "src/a.py"}]})
        changed, capped = cd.changed_files(
            transport, "owner/repo", event="pull_request", sha="a" * 40,
            payload={"pull_request": {"number": 37}})
        self.assertEqual((changed, capped), ({"src/a.py"}, False))
        bad = FakeTransport(responses={path: [{"filename": ""}]})
        with self.assertRaisesRegex(cd.DetectionError, "missing filename"):
            cd.changed_files(
                bad, "owner/repo", event="pull_request", sha="a" * 40,
                pr_number="37")

    def test_pr_files_are_paginated_and_rename_counts_both_paths(self):
        path = "repos/owner/repo/pulls/37/files?per_page=100"
        transport = FakeTransport(responses={path: [
            {"filename": "src/new.py", "status": "renamed",
             "previous_filename": "docs/old.md"},
        ]})
        result = cd.changed_files(
            transport, "owner/repo", event="pull_request", sha="a" * 40,
            pr_number="37")
        self.assertEqual(result, ({"src/new.py", "docs/old.md"}, False))
        self.assertEqual(transport.calls, [(path, True, True)])

    def test_pr_file_cap_marks_the_classification_conservative(self):
        path = "repos/owner/repo/pulls/37/files?per_page=100"
        transport = FakeTransport(responses={path: [
            {"filename": f"docs/{index}.md"} for index in range(300)
        ]})
        changed, capped = cd.changed_files(
            transport, "owner/repo", event="pull_request", sha="a" * 40,
            pr_number="37")
        self.assertEqual(len(changed), 300)
        self.assertTrue(capped)

    def test_push_compare_uses_payload_before_and_after(self):
        path = "repos/owner/repo/compare/" + "b" * 40 + "..." + "a" * 40
        transport = FakeTransport(responses={
            path: {"status": "ahead", "files": [{"filename": "code.py"}]}
        })
        result = cd.changed_files(
            transport, "owner/repo", event="push", sha="a" * 40,
            payload={"before": "b" * 40, "after": "a" * 40})
        self.assertEqual(result, ({"code.py"}, False))
        self.assertEqual(transport.calls, [(path, False, True)])

    def test_new_branch_compares_with_the_default_branch(self):
        path = "repos/owner/repo/compare/main..." + "a" * 40
        transport = FakeTransport(responses={
            path: {"status": "ahead", "files": [{"filename": "README.md"}]}
        })
        result = cd.changed_files(
            transport, "owner/repo", event="push", sha="a" * 40,
            default_branch="main",
            payload={"before": "0" * 40, "after": "a" * 40})
        self.assertEqual(result, ({"README.md"}, False))

    def test_rebased_push_falls_back_to_default_branch_comparison(self):
        first = "repos/owner/repo/compare/" + "b" * 40 + "..." + "a" * 40
        fallback = "repos/owner/repo/compare/main..." + "a" * 40
        transport = FakeTransport(responses={
            first: {"status": "diverged", "files": []},
            fallback: {"status": "ahead",
                       "files": [{"filename": "src/current.py"}]},
        })
        result = cd.changed_files(
            transport, "owner/repo", event="push", sha="a" * 40,
            default_branch="main",
            payload={"before": "b" * 40, "after": "a" * 40})
        self.assertEqual(result, ({"src/current.py"}, False))
        self.assertEqual([call[0] for call in transport.calls],
                         [first, fallback])
        self.assertTrue(all(call[2] for call in transport.calls))

    def test_push_cap_is_reported_for_run_everything(self):
        path = "repos/owner/repo/compare/" + "b" * 40 + "..." + "a" * 40
        transport = FakeTransport(responses={
            path: {"status": "ahead",
                   "files": [{"filename": "docs/a.md"}] * 300}
        })
        changed, capped = cd.changed_files(
            transport, "owner/repo", event="push", sha="a" * 40,
            payload={"before": "b" * 40, "after": "a" * 40})
        self.assertTrue(capped)
        self.assertEqual(set(cd.classify(changed, capped=capped).values()),
                         {"run"})

    def test_bad_rename_and_comparison_responses_are_refused(self):
        pull_files = "repos/owner/repo/pulls/37/files?per_page=100"
        malformed_rename = FakeTransport(responses={
            pull_files: [{"filename": "new.py", "status": "renamed"}]
        })
        with self.assertRaises(cd.DetectionError):
            cd.changed_files(
                malformed_rename, "owner/repo", event="pull_request",
                sha="a" * 40, pr_number="37")
        path = "repos/owner/repo/compare/" + "b" * 40 + "..." + "a" * 40
        for response in ({"status": "behind", "files": []},
                         {"status": "ahead"}):
            with self.subTest(response=str(response)[:30]):
                transport = FakeTransport(responses={path: response})
                with self.assertRaises(cd.DetectionError):
                    cd.changed_files(
                        transport, "owner/repo", event="push", sha="a" * 40,
                        payload={"before": "b" * 40, "after": "a" * 40})

    def test_diverged_push_requires_a_valid_default_branch_fallback(self):
        initial = "repos/owner/repo/compare/" + "b" * 40 + "..." + "a" * 40
        fallback = "repos/owner/repo/compare/main..." + "a" * 40
        with self.assertRaisesRegex(cd.DetectionError, "default branch"):
            cd.changed_files(
                FakeTransport(responses={initial: {"status": "diverged"}}),
                "owner/repo", event="push", sha="a" * 40,
                payload={"before": "b" * 40, "after": "a" * 40})
        responses = (
            cd.DetectionError("fallback unavailable"),
            {"status": "diverged", "files": []},
            {"status": "ahead"},
        )
        for response in responses:
            with self.subTest(response=response):
                transport = FakeTransport(responses={
                    initial: {"status": "diverged"}, fallback: response,
                })
                with self.assertRaises(cd.DetectionError):
                    cd.changed_files(
                        transport, "owner/repo", event="push", sha="a" * 40,
                        default_branch="main",
                        payload={"before": "b" * 40, "after": "a" * 40})

    def test_gate_integrity_runs_for_empty_and_documentation_diffs(self):
        for changed in (set(), {'README.md'}, {'new/code.py'}):
            with self.subTest(changed=changed):
                self.assertEqual(cd.classify(changed)['gate-integrity'], 'run')

    def test_schedule_reports_gate_integrity_as_unconditional(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = cd.main(environ={'EVENT_NAME': 'schedule',
                                      'SCHEDULE': cd.AUDIT_CRON},
                             transport=FakeTransport())
        self.assertEqual(status, 0)
        self.assertIn('gate-integrity=run', output.getvalue())
        self.assertIn('gate-integrity: run — gate integrity runs on every event',
                      output.getvalue())


class FailClosedTests(unittest.TestCase):
    def test_api_failure_writes_run_for_every_gate(self):
        with tempfile.TemporaryDirectory(prefix="ghw-changes-fail-") as temp:
            root = Path(temp)
            payload_path = root / "event.json"
            payload_path.write_text(json.dumps({
                "before": "b" * 40, "after": "a" * 40,
                "repository": {"default_branch": "main"},
            }), encoding="utf-8")
            output_path = root / "github-output.txt"
            environment = {
                "EVENT_NAME": "push",
                "EVENT_PATH": str(payload_path),
                "REPOSITORY": "owner/repo",
                "DEFAULT_BRANCH": "main",
                "HEAD_SHA": "a" * 40,
                "GITHUB_OUTPUT": str(output_path),
            }
            output, error = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(output):
                with contextlib.redirect_stderr(error):
                    status = cd.main(environ=environment,
                                     transport=FakeTransport(
                                         error=cd.DetectionError("API offline")))
            self.assertEqual(status, 0, error.getvalue())
            outputs = output_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                outputs,
                [f"{name}=run" for name in cd.GATES])
            self.assertTrue(all("API offline" in line
                                for line in output.getvalue().splitlines()
                                if ": run —" in line))
            self.assertNotIn("=skip", "\n".join(outputs))


class DefaultBranchPushTests(unittest.TestCase):
    """Unjudgeable default-branch pushes must still schedule every gate."""

    ALL_RUN = {
        "gate-integrity": "run", "tests": "run", "lint": "run",
        "types": "run", "audit": "run", "speed": "run", "codeql": "run",
        "actionlint": "run",
    }
    EMPTY_RUN = {
        "gate-integrity": "run", "tests": "skip", "lint": "skip",
        "types": "skip", "audit": "skip", "speed": "skip", "codeql": "skip",
        "actionlint": "skip",
    }

    def _push_result(self, before, ref, transport, default_branch="main"):
        with tempfile.TemporaryDirectory(prefix="ghw-default-push-") as temp:
            root = Path(temp)
            event_path = root / "event.json"
            event_path.write_text(json.dumps({
                "before": before, "after": "a" * 40, "ref": ref,
            }), encoding="utf-8")
            output_path = root / "github-output.txt"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = cd.main(environ={
                    "EVENT_NAME": "push", "EVENT_PATH": str(event_path),
                    "REPOSITORY": "owner/repo", "HEAD_SHA": "a" * 40,
                    "DEFAULT_BRANCH": default_branch,
                    "GITHUB_OUTPUT": str(output_path),
                }, transport=transport)
            self.assertEqual(status, 0, output.getvalue())
            decisions = dict(line.split("=", 1) for line in
                             output_path.read_text(encoding="utf-8").splitlines())
            return decisions, output.getvalue()

    def test_new_default_branch_runs_all_gates_without_self_comparison(self):
        for branch in ("main", "trunk"):
            with self.subTest(branch=branch):
                fallback = f"repos/owner/repo/compare/{branch}..." + "a" * 40
                transport = FakeTransport(responses={
                    fallback: {"status": "identical", "files": []},
                })
                decisions, reason = self._push_result(
                    "0" * 40, f"refs/heads/{branch}", transport, branch)
                self.assertEqual(decisions, self.ALL_RUN)
                self.assertIn("created default branch", reason)
                self.assertIn("no judgeable changed-path basis", reason)
                self.assertIn("running every gate", reason)
                self.assertEqual(transport.calls, [])

    def test_diverged_default_branch_runs_all_gates_without_self_comparison(self):
        initial = "repos/owner/repo/compare/" + "b" * 40 + "..." + "a" * 40
        for branch in ("main", "trunk"):
            with self.subTest(branch=branch):
                fallback = f"repos/owner/repo/compare/{branch}..." + "a" * 40
                transport = FakeTransport(responses={
                    initial: {"status": "diverged", "files": []},
                    fallback: {"status": "identical", "files": []},
                })
                decisions, reason = self._push_result(
                    "b" * 40, f"refs/heads/{branch}", transport, branch)
                self.assertEqual(decisions, self.ALL_RUN)
                self.assertIn("default-branch history rewritten", reason)
                self.assertIn("running every gate", reason)
                self.assertEqual(transport.calls, [(initial, False, True)])

    def test_new_working_branch_keeps_default_branch_substitution(self):
        fallback = "repos/owner/repo/compare/main..." + "a" * 40
        for ref in ("refs/heads/feature", "refs/heads/main-topic"):
            with self.subTest(ref=ref):
                transport = FakeTransport(responses={
                    fallback: {"status": "identical", "files": []},
                })
                decisions, reason = self._push_result("0" * 40, ref, transport)
                self.assertEqual(decisions, self.EMPTY_RUN)
                self.assertNotIn("detection failed", reason)
                self.assertEqual(transport.calls, [(fallback, False, True)])

    def test_diverged_working_branch_keeps_default_branch_substitution(self):
        initial = "repos/owner/repo/compare/" + "b" * 40 + "..." + "a" * 40
        fallback = "repos/owner/repo/compare/main..." + "a" * 40
        for ref in ("refs/heads/feature", "refs/heads/main-topic"):
            with self.subTest(ref=ref):
                transport = FakeTransport(responses={
                    initial: {"status": "diverged", "files": []},
                    fallback: {"status": "identical", "files": []},
                })
                decisions, reason = self._push_result("b" * 40, ref, transport)
                self.assertEqual(decisions, self.EMPTY_RUN)
                self.assertNotIn("detection failed", reason)
                self.assertEqual(transport.calls,
                                 [(initial, False, True), (fallback, False, True)])


class ReportingTests(unittest.TestCase):
    def test_tag_push_explains_run_everywhere_in_gate_outputs(self):
        with tempfile.TemporaryDirectory(prefix="ghw-tag-push-") as temp:
            event_path = Path(temp) / "event.json"
            event_path.write_text(json.dumps({
                "ref": "refs/tags/v1.0", "before": "0" * 40,
                "after": "a" * 40,
            }), encoding="utf-8")
            compare = "repos/owner/repo/compare/main..." + "a" * 40
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                status = cd.main(environ={
                    "EVENT_NAME": "push",
                    "EVENT_PATH": str(event_path),
                    "REPOSITORY": "owner/repo",
                    "HEAD_SHA": "a" * 40,
                    "DEFAULT_BRANCH": "main",
                }, transport=FakeTransport(responses={
                    compare: {"status": "identical", "files": []},
                }))
            self.assertEqual(status, 0)
            lines = stdout.getvalue().splitlines()
            for gate in ("tests", "lint", "types", "audit", "speed",
                         "codeql", "actionlint", "gate-integrity"):
                self.assertIn(
                    f"{gate}: run — tag push refs/tags/v1.0 runs all 8 gates",
                    lines)
                self.assertIn(f"{gate}=run", lines)

    def test_dispatch_and_schedule_print_a_reason_for_each_gate(self):
        dispatch = io.StringIO()
        with contextlib.redirect_stdout(dispatch):
            self.assertEqual(cd.main(environ={
                "EVENT_NAME": "workflow_dispatch",
            }, transport=FakeTransport()), 0)
        lines = dispatch.getvalue().splitlines()
        self.assertEqual(len(lines), len(cd.GATES) + len(cd.GATES))
        self.assertIn("tests: run — manual dispatch runs every gate", lines)
        self.assertIn("tests=run", lines)

        schedule = io.StringIO()
        with contextlib.redirect_stdout(schedule):
            self.assertEqual(cd.main(environ={
                "EVENT_NAME": "schedule",
                "SCHEDULE": cd.AUDIT_CRON,
            }, transport=FakeTransport()), 0)
        self.assertIn("audit: run — schedule matches 12 4 * * *",
                      schedule.getvalue())
        self.assertIn("tests: skip — gate is not scheduled for this event",
                      schedule.getvalue())

    def test_pull_request_docs_prints_skip_reasons_for_both_gate_kinds(self):
        with tempfile.TemporaryDirectory(prefix="ghw-docs-pr-") as temp:
            event_path = Path(temp) / "event.json"
            event_path.write_text(json.dumps({}), encoding="utf-8")
            api_path = "repos/owner/repo/pulls/37/files?per_page=100"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                status = cd.main(environ={
                    "EVENT_NAME": "pull_request",
                    "EVENT_PATH": str(event_path),
                    "REPOSITORY": "owner/repo",
                    "PR_NUMBER": "37",
                }, transport=FakeTransport(responses={
                    api_path: [{"filename": "docs/guide.md"}],
                }))
            self.assertEqual(status, 0)
            self.assertIn("tests: skip — every changed path is denied",
                          stdout.getvalue())
            self.assertIn("actionlint: skip — no changed path matches",
                          stdout.getvalue())

    def test_payload_and_output_errors_are_reported(self):
        with tempfile.TemporaryDirectory(prefix="ghw-payload-error-") as temp:
            path = Path(temp) / "event.json"
            for event_path, reason in (
                    ("", "no EVENT_PATH"),
                    (str(path), "cannot read event"),
                    (str(path), "not a JSON object")):
                path.write_text("{" if reason == "cannot read event" else "[]",
                                encoding="utf-8")
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    status = cd.main(environ={
                        "EVENT_NAME": "push",
                        "EVENT_PATH": event_path,
                        "GITHUB_OUTPUT": "",
                    }, transport=FakeTransport())
                self.assertEqual(status, 0)
                self.assertTrue(all(f"{name}=run" in stdout.getvalue()
                                    for name in cd.GATES))
                self.assertIn(reason, stdout.getvalue())

            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                with contextlib.redirect_stderr(stderr):
                    status = cd.main(environ={
                        "EVENT_NAME": "workflow_dispatch",
                        "GITHUB_OUTPUT": temp,
                    }, transport=FakeTransport())
            self.assertEqual(status, 1)
            self.assertIn("cannot write GITHUB_OUTPUT", stderr.getvalue())

    def test_capped_reason_describes_the_run_everything_fallback(self):
        with tempfile.TemporaryDirectory(prefix="ghw-capped-push-") as temp:
            path = Path(temp) / "event.json"
            path.write_text(json.dumps({
                "before": "b" * 40, "after": "a" * 40,
            }), encoding="utf-8")
            api_path = ("repos/owner/repo/compare/" + "b" * 40
                        + "..." + "a" * 40)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                status = cd.main(environ={
                    "EVENT_NAME": "push",
                    "EVENT_PATH": str(path),
                    "REPOSITORY": "owner/repo",
                    "HEAD_SHA": "a" * 40,
                    "GITHUB_OUTPUT": "",
                }, transport=FakeTransport(responses={
                    api_path: {"status": "ahead", "files": [
                        {"filename": f"docs/{index}.md"}
                        for index in range(300)
                    ]},
                }))
            self.assertEqual(status, 0)
            self.assertIn("300-file cap", stdout.getvalue())
            self.assertNotIn("=skip", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
