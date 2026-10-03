"""Invariants over .github/workflows that no workflow run can check itself.

    python3 -m unittest discover -v

Stdlib unittest, matching the rest of this repo's suite.
"""
import re
import unittest
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent


def envelope(value, samples=6):
    """A baseline entry as an OBSERVED RANGE, not a single number.

    The committed baseline records what each entry was seen over — min, max
    and how many observations — because a single stored value plus a budget
    wide enough to cover a 60-84% spread would need a tolerance above 1.0,
    which is a gate that cannot fire. Tests that want a head value to land
    exactly on the ceiling build the envelope around that value.
    """
    return {"min": round(value * 0.8, 6), "max": value, "n": samples}


WORKFLOWS = REPO_ROOT / ".github" / "workflows"
CODEQL_USE = re.compile(r"uses:\s*github/codeql-action/([\w-]+)@(\S+)")
FORK_PIN = re.compile(r'FORK_PIN="git\+https://github\.com/Nitjsefnie-OSC/'
                      r'git-fame@([0-9a-f]{40})"')
DOCUMENTED_PIN = re.compile(r"git\+https://github\.com/Nitjsefnie-OSC/"
                            r"git-fame@([0-9a-f]{40})")


def _job_blocks(text):
    """Map workflow job ids to their complete YAML text blocks."""
    lines = text.splitlines()
    start = lines.index("jobs:") + 1
    blocks = {}
    index = start
    while index < len(lines):
        match = re.fullmatch(r"  ([a-z][a-z0-9_-]*):", lines[index])
        if not match:
            index += 1
            continue
        job_start = index
        job = match.group(1)
        index += 1
        while (index < len(lines)
               and not re.fullmatch(r"  [a-z][a-z0-9_-]*:", lines[index])):
            index += 1
        blocks[job] = "\n".join(lines[job_start:index]) + "\n"
    return blocks


def _trigger_block(text):
    """Return just the workflow-level trigger mappings."""
    lines = text.splitlines()
    start = lines.index("on:")
    end = next(index for index in range(start + 1, len(lines))
               if lines[index] in {"permissions:", "concurrency:", "jobs:"})
    return "\n".join(lines[start:end])


def _steps_block(job_block):
    """Keep the complete steps block, excluding only its outer blank lines."""
    lines = job_block.splitlines()
    try:
        start = lines.index("    steps:")
    except ValueError:
        return []
    steps = lines[start:]
    while steps and not steps[0].strip():
        steps.pop(0)
    while steps and not steps[-1].strip():
        steps.pop()
    return steps


class TestCodeqlPins(unittest.TestCase):
    """Every github/codeql-action step must run the same release."""

    def test_codeql_action_steps_share_one_ref(self):
        # init writes a config that analyze refuses to load from a different
        # version, so a half-bump fails every CodeQL run. Dependabot treats
        # the two as separate dependencies; dependabot.yml groups them, and
        # this catches a hand edit that splits them again.
        uses = [(path.name, step, ref)
                for path in sorted(WORKFLOWS.glob("*.yml"))
                for step, ref in CODEQL_USE.findall(path.read_text())]
        self.assertTrue(uses, "no github/codeql-action step found")
        self.assertEqual(len({ref for _, _, ref in uses}), 1, uses)


class TestGitFameForkPin(unittest.TestCase):
    """A measurement's `fork` arm must be the build production pins."""

    def test_fork_arm_matches_the_documented_pin(self):
        # The arm is the baseline every upstream comparison is read against.
        # Left on an older fork build, a comparison measures a build nobody
        # runs, and a "switch or not" answer rests on the wrong number.
        documented = set(DOCUMENTED_PIN.findall(
            (REPO_ROOT / "CLAUDE.md").read_text()))
        self.assertEqual(len(documented), 1, documented)
        arms = {(path.name, sha)
                for path in sorted(WORKFLOWS.glob("*.yml"))
                for sha in FORK_PIN.findall(path.read_text())}
        self.assertTrue(arms, "no FORK_PIN found in any workflow")
        self.assertEqual({sha for _, sha in arms}, documented, arms)


class TestConsolidatedCiControls(unittest.TestCase):
    """Controls for the needs-based aggregate and path selector."""

    def setUp(self):
        self.workflow_path = WORKFLOWS / "tests.yml"
        self.workflow = self.workflow_path.read_text(encoding="utf-8")
        self.jobs = _job_blocks(self.workflow)
        self.aggregate = self.jobs.get("aggregate", "")
        self.scripts = REPO_ROOT / "scripts" / "ci"

    def test_consolidated_workflow_is_needs_based(self):
        self.assertIn("name: tests", self.workflow)
        needs = re.search(r"(?m)^    needs:\s*\[([^]]+)\]$",
                          self.aggregate)
        assert needs is not None
        self.assertEqual(
            {name.strip() for name in needs.group(1).split(",")},
            {"changes", "unittest", "lint", "pyright", "pip-audit",
             "speed", "analyze", "actionlint"})
        trigger = _trigger_block(self.workflow)
        self.assertNotRegex(trigger, r"(?m)^\s+paths(?:-ignore)?:")
        self.assertFalse((self.scripts / "aggregate_gates.py").exists())

    def test_aggregate_has_no_polling_and_short_timeout(self):
        self.assertNotRegex(self.aggregate.lower(), r"\b(?:sleep|poll(?:ing)?)\b")
        timeout = re.search(r"(?m)^    timeout-minutes:\s*(\d+)\s*$",
                            self.aggregate)
        assert timeout is not None
        self.assertLessEqual(int(timeout.group(1)), 5)
        self.assertFalse((self.scripts / "aggregate_gates.py").exists())
        self.assertNotIn("evaluate_gates",
                         (self.scripts / "aggregate_gate.py").read_text(
                             encoding="utf-8"))

    def test_aggregate_reports_pushes_only_on_main(self):
        condition = re.search(r"(?m)^    if:\s*\$\{\{(.*?)\}\}\s*$",
                              self.aggregate, re.DOTALL)
        assert condition is not None
        normalized = " ".join(condition.group(1).split())
        self.assertEqual(
            normalized,
            "always() && (github.event_name == 'pull_request' || "
            "github.event_name == 'workflow_dispatch' || "
            "(github.event_name == 'push' && "
            "github.ref == 'refs/heads/main'))")

    def test_policy_timestamp_filter_is_absent(self):
        forbidden = ("created_after", "GATE_POLICY_UPDATED_AT", "updated_at")
        sources = list(WORKFLOWS.glob("*.yml")) + [
            self.scripts / "aggregate_gate.py",
            self.scripts / "changes_detect.py",
        ]
        for path in sources:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                with self.subTest(path=path.name, token=token):
                    self.assertNotIn(token, text)

    def test_changes_gates_match_consolidated_job_conditions(self):
        from scripts.ci import changes_detect  # pylint: disable=import-outside-toplevel

        workflow_gates = {}
        for job, block in self.jobs.items():
            match = re.search(
                r"(?m)^    if:\s*needs\.changes\.outputs\."
                r"([a-z][a-z0-9_-]*)\s*==\s*'run'\s*$", block)
            if match:
                gate = match.group(1)
                self.assertNotIn(gate, workflow_gates.values())
                workflow_gates[job] = gate
        self.assertEqual(set(workflow_gates.values()),
                         set(changes_detect.GATES))

    def test_schedule_crons_have_separate_unconditional_owners(self):
        trigger = _trigger_block(self.workflow)
        events = re.findall(r"(?m)^  ([a-z_]+):\s*$", trigger)
        self.assertEqual(events, ["push", "pull_request", "workflow_dispatch"])

        owners = (("audit.yml", "12 4 * * *", "pip-audit"),
                  ("codeql.yml", "47 3 * * 3", "analyze"))
        workflow_files = list(WORKFLOWS.glob("*.yml"))
        for filename, cron, job in owners:
            with self.subTest(filename=filename):
                count = sum(path.read_text(encoding="utf-8").count(
                    f"cron: '{cron}'") for path in workflow_files)
                self.assertEqual(count, 1, f"{cron} occurs {count} times")
                path = WORKFLOWS / filename
                self.assertTrue(path.is_file(), f"missing schedule owner {filename}")
                text = path.read_text(encoding="utf-8")
                owner_trigger = _trigger_block(text)
                owner_events = re.findall(
                    r"(?m)^  ([a-z_]+):\s*$", owner_trigger)
                self.assertEqual(
                    owner_events, ["schedule", "workflow_dispatch"])
                self.assertIn(f"cron: '{cron}'", owner_trigger)
                self.assertIn(job, _job_blocks(text))

    def test_scheduled_gate_jobs_have_no_job_condition(self):
        for filename, job in (("audit.yml", "pip-audit"),
                              ("codeql.yml", "analyze")):
            with self.subTest(filename=filename):
                path = WORKFLOWS / filename
                self.assertTrue(path.is_file(), f"missing schedule owner {filename}")
                blocks = _job_blocks(path.read_text(encoding="utf-8"))
                self.assertIn(job, blocks, f"missing job {job} in {filename}")
                block = blocks[job]
                self.assertNotRegex(block, r"(?m)^    if:")
                self.assertNotRegex(block, r"(?m)^    needs:")

    def test_release_manifest_jobs_are_real_tests_workflow_producers(self):
        release = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
        manifest_jobs = re.findall(
            r"(?m)^ {12}([a-z][a-z-]*)\s+# tests\.yml, job `([a-z][a-z-]*)`",
            release)
        self.assertTrue(manifest_jobs, "release manifest has no tests.yml jobs")
        for gate, job in manifest_jobs:
            with self.subTest(gate=gate, job=job):
                self.assertIn(job, self.jobs)
                block = self.jobs[job]
                self.assertRegex(block, r"(?m)^    runs-on:")
                self.assertNotRegex(
                    block, r"(?m)^    uses:\s*\./\.github/workflows/")

    def test_schedule_and_tests_job_steps_are_identical(self):
        copies = (("audit.yml", "pip-audit"),
                  ("codeql.yml", "analyze"))
        for filename, job in copies:
            with self.subTest(filename=filename, job=job):
                owner = _job_blocks(
                    (WORKFLOWS / filename).read_text(encoding="utf-8"))[job]
                self.assertEqual(_steps_block(self.jobs[job]),
                                 _steps_block(owner))

    def test_tests_concurrency_uses_per_pr_or_per_run_group(self):
        block = re.search(r"(?ms)^concurrency:\n(.*?)(?=^jobs:)",
                          self.workflow)
        assert block is not None
        group = re.search(
            r"(?ms)^  group:\s*\$\{\{(.*?)\}\}\s*$", block.group(1))
        assert group is not None
        self.assertEqual(
            " ".join(group.group(1).split()),
            "github.event_name == 'pull_request' && "
            "format('tests-pr-{0}', github.event.pull_request.number) || "
            "github.run_id")

    def test_tests_cancel_in_progress_only_for_pull_requests(self):
        block = re.search(r"(?ms)^concurrency:\n(.*?)(?=^jobs:)",
                          self.workflow)
        assert block is not None
        cancel = re.search(
            r"(?ms)^  cancel-in-progress:\s*\$\{\{(.*?)\}\}\s*$",
            block.group(1))
        assert cancel is not None
        self.assertEqual(" ".join(cancel.group(1).split()),
                         "github.event_name == 'pull_request'")


class TestDependabotActionGroups(unittest.TestCase):
    """Keep action update groups scoped to their declared update type."""

    def test_github_actions_has_distinct_version_and_security_groups(self):
        document = yaml.safe_load(
            (REPO_ROOT / ".github" / "dependabot.yml").read_text(
                encoding="utf-8"))
        actions = next(update for update in document["updates"]
                       if update["package-ecosystem"] == "github-actions")
        groups = actions["groups"]

        self.assertEqual(set(groups), {"actions", "github-actions-security"})
        self.assertEqual(
            sum(group.get("applies-to") == "version-updates"
                for group in groups.values()), 1)
        self.assertEqual(
            sum(group.get("applies-to") == "security-updates"
                for group in groups.values()), 1)
        self.assertEqual(groups["actions"]["applies-to"], "version-updates")
        self.assertEqual(groups["actions"]["patterns"], ["*"])
        self.assertEqual(groups["github-actions-security"]["applies-to"],
                         "security-updates")
        self.assertEqual(groups["github-actions-security"]["patterns"], ["*"])


class TestClaimWorkflowShape(unittest.TestCase):
    """Pin the claim workflow's command filter, permissions and action input."""

    def setUp(self):
        self.path = WORKFLOWS / "claim.yml"
        self.text = self.path.read_text(encoding="utf-8")
        self.workflow = yaml.safe_load(self.text)
        self.job = self.workflow["jobs"]["claim"]

    def test_condition_filters_only_bots_and_non_commands(self):
        condition = " ".join(self.job["if"].split())
        self.assertEqual(
            condition,
            "github.event.comment.user.type != 'Bot' && "
            "(contains(github.event.comment.body, '/claim') || "
            "contains(github.event.comment.body, '/unclaim') || "
            "contains(github.event.comment.body, '/release'))")
        self.assertNotIn("github.event.issue.pull_request", condition)
        self.assertNotIn("github.event.issue.state", condition)

    def test_permissions_are_job_scoped(self):
        self.assertEqual(self.job.get("permissions"), {
            "issues": "write",
            "pull-requests": "write",
        })
        self.assertNotIn("permissions", self.workflow)

    def test_concurrency_queues_claims_without_cancelling_them(self):
        concurrency = self.workflow["concurrency"]
        self.assertEqual(concurrency["group"],
                         "claim-${{ github.event.issue.number }}")
        self.assertEqual(concurrency.get("queue"), "max")
        self.assertIs(concurrency.get("cancel-in-progress"), False)

    def test_job_timeout_is_five_minutes(self):
        self.assertEqual(self.job.get("timeout-minutes"), 5)

    def test_action_pin_comment_and_inputs_match_the_release(self):
        pin = ("Nitjsefnie-Actions/claim@"
               "0c79a0325d8ab789a60c2eeaf751690d2875c39c")
        steps = self.job["steps"]
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].get("uses"), pin)

        pin_lines = [line for line in self.text.splitlines()
                     if re.fullmatch(r"\s+- uses:.*", line)]
        self.assertEqual(len(pin_lines), 1)
        pin_line = re.fullmatch(r"\s+- uses:\s*(\S+)(\s+#.*)?", pin_lines[0])
        assert pin_line is not None
        self.assertEqual(pin_line.group(1), pin)
        self.assertEqual(pin_line.group(2), "  # v2.0.3")
        self.assertEqual(steps[0].get("with"), {
            "max-claims": "read=2, triage=4, write=6, maintain=10, admin=-1",
            "expire": "7",
        })


class TestScorecardWorkflow(unittest.TestCase):
    """Pin the scheduled Scorecard workflow's fork guard and output scopes."""

    def setUp(self):
        path = WORKFLOWS / "scorecard.yml"
        if path.is_file():
            self.text = path.read_text(encoding="utf-8")
            self.workflow = yaml.safe_load(self.text) or {}
        else:
            self.text = ""
            self.workflow = {}
        self.job = self.workflow.get("jobs", {}).get("analysis", {})

    def test_weekly_schedule_uses_the_reference_cron(self):
        trigger_keys = [key for key in self.workflow
                        if key == "on" or key is True]
        self.assertEqual(len(trigger_keys), 1)
        trigger_key = trigger_keys[0]
        self.assertEqual(set(self.workflow), {
            "name", trigger_key, "permissions", "concurrency", "jobs",
        })
        self.assertEqual(self.workflow[trigger_key], {
            "schedule": [{"cron": "23 2 * * 6"}],
            "workflow_dispatch": None,
        })

    def test_guard_skips_forks_and_non_default_branches(self):
        condition = " ".join(self.job.get("if", "").split())
        self.assertEqual(
            condition,
            "${{ !github.event.repository.fork && "
            "github.ref == format('refs/heads/{0}', "
            "github.event.repository.default_branch) }}")

    def test_permissions_and_job_timeout_are_scoped(self):
        self.assertEqual(self.workflow.get("name"), "scorecard")
        self.assertEqual(self.workflow.get("permissions"), {"contents": "read"})
        self.assertEqual(self.workflow.get("concurrency"), {
            "group": "scorecard-${{ github.ref }}",
            "cancel-in-progress": True,
        })
        self.assertEqual(set(self.workflow.get("jobs", {})), {"analysis"})
        self.assertEqual(set(self.job), {
            "name", "if", "runs-on", "timeout-minutes", "permissions",
            "steps",
        })
        self.assertEqual(self.job.get("name"), "Scorecard analysis")
        self.assertEqual(self.job.get("runs-on"), "ubuntu-latest")
        self.assertEqual(self.job.get("timeout-minutes"), 15)
        self.assertEqual(self.job.get("permissions"), {
            "security-events": "write",
            "id-token": "write",
            "contents": "read",
        })

    def test_uploads_sarif_and_publishes_scorecard_results(self):
        expected_steps = [
            {
                "name": "Checkout code",
                "uses": "actions/checkout@"
                        "3d3c42e5aac5ba805825da76410c181273ba90b1",
                "with": {"persist-credentials": False},
            },
            {
                "name": "Run Scorecard analysis",
                "uses": "ossf/scorecard-action@"
                        "2d1146689b8cda280b9bc96326124645441f03bc",
                "with": {
                    "results_file": "results.sarif",
                    "results_format": "sarif",
                    "publish_results": True,
                },
            },
            {
                "name": "Upload Scorecard results artifact",
                "uses": "actions/upload-artifact@"
                        "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
                "with": {
                    "name": "scorecard-results",
                    "path": "results.sarif",
                    "retention-days": 5,
                },
            },
            {
                "name": "Upload Scorecard results to code scanning",
                "uses": "github/codeql-action/upload-sarif@"
                        "2892aa5e19bbd11bc0cff5427e3b750a04d9e3c2",
                "with": {"sarif_file": "results.sarif"},
            },
        ]
        self.assertEqual(self.job.get("steps"), expected_steps)

        raw_job = _job_blocks(self.text)["analysis"]
        pin_lines = [
            line.strip()
            for line in _steps_block(raw_job)
            if re.fullmatch(r" {8}uses: \S+ # \S+", line)
        ]
        self.assertEqual(pin_lines, [
            "uses: actions/checkout@"
            "3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1",
            "uses: ossf/scorecard-action@"
            "2d1146689b8cda280b9bc96326124645441f03bc # v2.4.4",
            "uses: actions/upload-artifact@"
            "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1",
            "uses: github/codeql-action/upload-sarif@"
            "2892aa5e19bbd11bc0cff5427e3b750a04d9e3c2 # v4.38.2",
        ])


if __name__ == "__main__":
    unittest.main()
