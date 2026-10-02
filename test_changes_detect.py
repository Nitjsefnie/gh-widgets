"""Path classification for the consolidated CI gate selector."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.ci import changes_detect as cd


class FakeTransport:
    """Return API-shaped data and record the request contract."""

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
        return self.responses[path]


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
        self.assertEqual(set(result.values()), {"skip"})

    def test_workflow_change_runs_code_gates_and_actionlint(self):
        result = cd.classify({".github/workflows/tests.yml"})
        self.assertEqual(set(result.values()), {"run"})

    def test_code_change_skips_only_the_allow_gate(self):
        result = cd.classify({"new/code.py"})
        for name in ("tests", "lint", "types", "audit", "speed", "codeql"):
            self.assertEqual(result[name], "run")
        self.assertEqual(result["actionlint"], "skip")

    def test_codeql_ignores_its_complete_eight_path_set(self):
        self.assertEqual(len(cd.CODEQL_IGNORES), 8)
        for path in ("README.md", "PRESENTATION.txt", "docs/a.md",
                     "examples/example.py", ".claude/settings.json",
                     "LICENSE", "NOTICE", ".gitignore"):
            with self.subTest(path=path):
                self.assertEqual(cd.classify({path})["codeql"], "skip")

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
                {expected} if expected else set())
            self.assertEqual(transport.calls, [])

    def test_dispatch_runs_every_gate_without_an_api_read(self):
        transport = FakeTransport()
        result = cd.classify_event(
            "workflow_dispatch", transport=transport, repository="owner/repo",
            payload={})
        self.assertEqual(set(result.values()), {"run"})
        self.assertEqual(transport.calls, [])


class AcquisitionTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
