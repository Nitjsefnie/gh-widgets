"""Offline workflow invariants, run with ``python3 -m unittest discover``."""
# Existing workflow/aggregate controls stay together in these test modules.
# pylint: disable=too-many-lines
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

from bench_platform import REQUIRES_POSIX_SHELL


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


WORKFLOWS = Path(os.environ.get(
    "GH_WIDGETS_WORKFLOWS", REPO_ROOT / ".github" / "workflows"))
CODEQL_USE = re.compile(r"uses:\s*github/codeql-action/([\w-]+)@(\S+)")
FORK_PIN = re.compile(r'FORK_PIN="git\+https://github\.com/Nitjsefnie-OSC/'
                      r'git-fame@([0-9a-f]{40})"')
DOCUMENTED_PIN = re.compile(r"git\+https://github\.com/Nitjsefnie-OSC/"
                            r"git-fame@([0-9a-f]{40})")
CONTINUE_ON_ERROR_KEY = re.compile(
    r"(?mi)^[ \t]*(?:-[ \t]+)?"
    r"(?:continue-on-error|[\"']continue-on-error[\"'])[ \t]*:")


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


def _run_download_with_failed_digest(script):
    """Run the workflow download script with a failing checksum command."""
    with tempfile.TemporaryDirectory(prefix="gitleaks-download-control-") as tmp:
        directory = Path(tmp)
        curl_args = directory / "curl-args"
        tar_called = directory / "tar-called"
        curl_args.touch()
        stubs = {
            "curl": 'printf \'%s\\n\' "$@" > "$CURL_ARGUMENTS"',
            "sha256sum": "echo digest mismatch >&2; exit 1",
            "tar": 'touch "$TAR_MARKER"',
        }
        for name, body in stubs.items():
            stub = directory / name
            stub.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
            stub.chmod(0o755)
        environment = dict(
            os.environ,
            PATH=f"{directory}{os.pathsep}{os.environ['PATH']}",
            CURL_ARGUMENTS=str(curl_args),
            TAR_MARKER=str(tar_called))
        result = subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", script],
            cwd=directory, env=environment, capture_output=True,
            text=True, check=False)
        return result.returncode, curl_args.read_text(encoding="utf-8"), \
            tar_called.exists()


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


class TestCoverageRatchetCellParity(unittest.TestCase):
    """coverage-ratchet must measure the program tests.yml's cell measures.

    A coverage floor is comparable only within one program-environment, not
    just one interpreter build. The git-fame-gated classes in test_impact.py
    skip when the tool is absent and GH_WIDGETS_REQUIRE_GIT_FAME is unset —
    a green suite whose three check_git_fame() success-path statements go
    unexecuted, moving the reading 903 -> 906 missing and 83.2 -> 83.1
    against an unchanged 5385-statement floor. That reading is what made
    run 37203738774 red on main, not a real coverage change on the tree.
    """

    def test_measure_matches_the_measured_cell_toolchain(self):
        # The pin alone would measure correctly but fail silently if the
        # install ever broke; the env alone would fail the run loudly but
        # never measure. tests.yml's measured cell carries both, and this
        # job's value is only the comparison against a floor measured in
        # that cell's program, so both are required here, and the install
        # and the env must both precede the coverage run they govern.
        job = _job_blocks(
            (WORKFLOWS / "coverage-ratchet.yml").read_text())["measure"]
        documented = set(DOCUMENTED_PIN.findall(
            (REPO_ROOT / "CLAUDE.md").read_text()))
        self.assertEqual(len(documented), 1, documented)
        self.assertEqual(FORK_PIN.findall(job), list(documented), job)
        env = re.search(r"^\s+GH_WIDGETS_REQUIRE_GIT_FAME: \"true\"$",
                        job, re.MULTILINE)
        assert env is not None
        lines = job.splitlines()
        measure_index = next(index for index, line in enumerate(lines)
                             if "coverage run" in line)
        pin_index = next(index for index, line in enumerate(lines)
                         if "FORK_PIN=" in line)
        env_line = env.group(0).strip()
        env_index = next(index for index, line in enumerate(lines)
                         if line.strip() == env_line)
        for name, index in (("the pinned install", pin_index),
                            ("the require env", env_index)):
            self.assertLess(index, measure_index,
                            f"{name} must precede the coverage run")


class TestSecretsScanWorkflow(unittest.TestCase):
    """Pin the secret scan's trigger, history, and aggregate wiring."""

    def setUp(self):
        self.secrets_path = WORKFLOWS / "secrets.yml"
        self.assertTrue(self.secrets_path.is_file(), "missing secrets.yml")
        self.secrets_text = self.secrets_path.read_text(encoding="utf-8")
        self.secrets_trigger = _trigger_block(self.secrets_text)
        self.secrets_jobs = _job_blocks(self.secrets_text)
        self.gitleaks_job = self.secrets_jobs.get("gitleaks", "")

    def _event_block(self, name):
        events = list(re.finditer(r"(?m)^  ([a-z_]+):\s*$",
                                  self.secrets_trigger))
        names = [match.group(1) for match in events]
        self.assertEqual(names, ["push", "pull_request", "schedule",
                                 "workflow_dispatch"])
        index = names.index(name)
        end = events[index + 1].start() if index + 1 < len(events) else len(
            self.secrets_trigger)
        return (self.secrets_trigger[events[index].start():end]
                .strip("\n").splitlines())

    def _named_step(self, job, name):
        steps = _steps_block(job)
        matches = [index for index, line in enumerate(steps)
                   if line == f"      - name: {name}"]
        self.assertEqual(len(matches), 1, name)
        start = matches[0]
        end = next((index for index in range(start + 1, len(steps))
                    if steps[index].startswith("      - ")), len(steps))
        return "\n".join(steps[start:end])

    def test_secrets_scan_has_the_required_events_and_unique_cron(self):
        self.assertEqual(self._event_block("push"),
                         ["  push:", "    branches: [main]"])
        self.assertEqual(self._event_block("pull_request"),
                         ["  pull_request:"])
        self.assertEqual(self._event_block("schedule"),
                         ["  schedule:", "    - cron: '26 5 * * *'"])
        self.assertEqual(self._event_block("workflow_dispatch"),
                         ["  workflow_dispatch:"])
        all_crons = []
        for path in WORKFLOWS.glob("*.yml"):
            for line in path.read_text(encoding="utf-8").splitlines():
                match = re.fullmatch(
                    r"[ \t]*-[ \t]+cron:[ \t]*(['\"]?)([^'\"\n]+?)"
                    r"\1[ \t]*", line)
                if match:
                    all_crons.append(match.group(2).strip())
        self.assertEqual(all_crons.count("26 5 * * *"), 1, all_crons)
        self.assertEqual(all_crons.count("12 4 * * *"), 1, all_crons)
        self.assertEqual(all_crons.count("47 3 * * 3"), 1, all_crons)

    def test_secrets_scan_uses_a_read_only_full_history_job(self):
        self.assertTrue(self.gitleaks_job)
        self.assertRegex(self.secrets_text,
                         r"(?m)^permissions:\n  contents: read\n\n")
        self.assertRegex(
            self.secrets_text,
            r"(?m)^concurrency:\n"
            r"  group: secrets-\$\{\{ github\.ref \}\}\n"
            r"  cancel-in-progress: true\n\n")
        self.assertRegex(self.gitleaks_job,
                         r"(?m)^    runs-on: ubuntu-latest$")
        self.assertRegex(self.gitleaks_job,
                         r"(?m)^    timeout-minutes: 10$")
        steps = _steps_block(self.gitleaks_job)
        checkouts = [index for index, line in enumerate(steps)
                     if line.startswith("      - uses: actions/checkout@")]
        self.assertEqual(len(checkouts), 1)
        checkout_index = checkouts[0]
        self.assertRegex(
            steps[checkout_index],
            r"^      - uses: actions/checkout@"
            r"3d3c42e5aac5ba805825da76410c181273ba90b1(?: # v7\.0\.1)?$")
        checkout_end = next(
            (index for index in range(checkout_index + 1, len(steps))
             if steps[index].startswith("      - ")),
            len(steps))
        checkout_block = steps[checkout_index:checkout_end]
        settings = []
        for line in checkout_block:
            match = re.fullmatch(r" {10}([a-z-]+):[ \t]*(.*)", line)
            if match:
                value = match.group(2).strip()
                if (len(value) >= 2 and value[0] == value[-1]
                        and value[0] in "\"'"):
                    value = value[1:-1]
                settings.append((match.group(1), value))
        self.assertEqual(settings, [
            ("fetch-depth", "0"), ("persist-credentials", "false")])
        download = self._named_step(
            self.gitleaks_job, "Download gitleaks and verify its digest")
        download_script = textwrap.dedent(
            download.split("run: |\n", 1)[1]).strip()
        digest = (
            "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb")
        digest_lines = [line.strip() for line in download_script.splitlines()
                        if digest in line]
        self.assertEqual(len(digest_lines), 1)
        self.assertTrue(
            digest_lines[0].endswith("| sha256sum -c -"),
            "the pinned digest must be consumed by sha256sum verification")
        scan = self._named_step(
            self.gitleaks_job, "Scan the tree and the history")
        self.assertRegex(
            scan,
            r"(?m)^        run: ./gitleaks detect --verbose --redact "
            r"--config \.gitleaks\.toml$")
        self.assertNotRegex(scan, r"(?m)^\s+if:")

    @REQUIRES_POSIX_SHELL
    def test_digest_failure_gates_extraction_and_the_download_uses_its_url(self):
        download = self._named_step(
            self.gitleaks_job, "Download gitleaks and verify its digest")
        script = textwrap.dedent(download.split("run: |\n", 1)[1]).strip()
        expected_url = (
            "https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/"
            "gitleaks_8.30.1_linux_x64.tar.gz")
        returncode, curl_output, was_extracted = (
            _run_download_with_failed_digest(script))
        self.assertIn(expected_url, curl_output)
        self.assertNotEqual(
            returncode, 0,
            "a checksum mismatch must make the download step fail")
        self.assertFalse(
            was_extracted,
            "a checksum mismatch must prevent archive extraction")

    def test_scan_failure_cannot_be_swallowed_by_the_gitleaks_job(self):
        self.assertNotRegex(
            self.gitleaks_job,
            CONTINUE_ON_ERROR_KEY,
            "continue-on-error converts a failing step or job into a "
            "successful one, defeating the aggregate's job-conclusion fold")

    def test_error_swallowing_control_recognizes_yaml_mapping_keys(self):
        for declaration in (
                "continue-on-error: true",
                '"continue-on-error": ${{ true }}',
                "'continue-on-error': true # comment",
                "- continue-on-error: true",
                '- "continue-on-error": true',
                "- 'continue-on-error': true"):
            with self.subTest(declaration=declaration):
                self.assertRegex(declaration, CONTINUE_ON_ERROR_KEY)
        self.assertNotRegex("# continue-on-error: true",
                            CONTINUE_ON_ERROR_KEY)

    def test_aggregate_passes_its_head_sha_and_wait_bound_to_the_gate(self):
        tests_text = (WORKFLOWS / "tests.yml").read_text(encoding="utf-8")
        aggregate = _job_blocks(tests_text)["aggregate"]
        step = self._named_step(aggregate, "Aggregate CI gates")
        self.assertRegex(step,
                         r"(?m)^          SECRETS_WAIT_BOUND_S: '240'$")
        self.assertRegex(
            step,
            r"(?m)^          HEAD_SHA: \$\{\{ github\.event\.pull_request\."
            r"head\.sha \|\| github\.sha \}\}$")
        self.assertRegex(aggregate,
                         r"(?m)^    timeout-minutes: 5$")
        self.assertNotRegex(step, r"(?m)^\s+if:")

    def test_gitleaks_config_keeps_the_default_rules(self):
        text = (REPO_ROOT / ".gitleaks.toml").read_text(encoding="utf-8")
        settings = [line.strip() for line in text.splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
        self.assertEqual(settings, ["[extend]", "useDefault = true"])


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
             "speed", "analyze", "actionlint", "gates"})
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
                         set(changes_detect.GATES) - {"gate-integrity"})

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


class TestGateIntegrityWorkflow(unittest.TestCase):
    """The integrity guard runs regardless of the path selector's verdict."""

    def setUp(self):
        self.workflow = yaml.safe_load((WORKFLOWS / 'tests.yml').read_text())
        self.jobs = self.workflow['jobs']
        self.job = self.jobs.get('gates', {})

    def test_guard_runs_unconditionally_and_feeds_the_required_aggregate(self):
        self.assertTrue(self.job, 'missing gates job')
        self.assertNotIn('if', self.job)
        self.assertNotIn('needs', self.job)
        self.assertEqual(self.job['runs-on'], 'ubuntu-latest')
        self.assertEqual(self.job['timeout-minutes'], 5)
        self.assertIn('gates', self.jobs['aggregate']['needs'])
        self.assertEqual(self.jobs['aggregate'].get('name', 'aggregate'),
                         'aggregate')
        self.assertEqual(self.jobs['changes']['outputs']['gate-integrity'],
                         '${{ steps.detect.outputs.gate-integrity }}')
        self.assertLess(list(self.jobs).index('actionlint'),
                        list(self.jobs).index('gates'))
        self.assertLess(list(self.jobs).index('gates'),
                        list(self.jobs).index('aggregate'))

    def test_steps_check_markers_then_freshness_then_scopes(self):
        steps = self.job.get('steps', [])
        self.assertEqual(len(steps), 4)
        self.assertTrue(steps[0]['uses'].startswith('actions/checkout@'))
        self.assertIs(steps[0]['with']['persist-credentials'], False)
        for step in steps:
            self.assertNotIn('if', step)
            self.assertNotIn('continue-on-error', step)
        self.assertEqual(steps[1]['shell'], 'bash')
        self.assertEqual(shlex.split(steps[2]['run']),
                         ['python3', 'scripts/ci/gate_base_freshness.py'])
        self.assertEqual(shlex.split(steps[3]['run']),
                         ['python3', 'scripts/ci/commit_scopes.py'])

    @REQUIRES_POSIX_SHELL
    def test_marker_step_passes_clean_tree_and_refuses_each_git_marker(self):
        self.assertTrue(self.job, 'missing gates job')
        script = self.job['steps'][1]['run']
        with tempfile.TemporaryDirectory(prefix='ghw-markers-') as tmp:
            repo = Path(tmp)
            subprocess.run(['git', 'init', str(repo)], check=True,
                           capture_output=True)
            path = repo / 'README.md'
            cases = ('clean prose\n======== heading\n', '<<<<<<< HEAD\n',
                     '>>>>>>> branch\n', '=======\n', '<<<<<<<\n',
                     '>>>>>>>\n')
            for index, content in enumerate(cases):
                with self.subTest(content=content):
                    path.write_text(content, encoding='utf-8')
                    subprocess.run(['git', '-C', str(repo), 'add', 'README.md'],
                                   check=True, capture_output=True)
                    done = subprocess.run(
                        ['bash', '-e', '-o', 'pipefail', '-c', script],
                        cwd=repo, capture_output=True, text=True, check=False)
                    self.assertEqual(done.returncode, 0 if index == 0 else 1,
                                     done.stdout + done.stderr)

    @REQUIRES_POSIX_SHELL
    def test_marker_step_propagates_git_errors(self):
        self.assertTrue(self.job, 'missing gates job')
        with tempfile.TemporaryDirectory(prefix='ghw-no-git-') as tmp:
            done = subprocess.run(
                ['bash', '-e', '-o', 'pipefail', '-c',
                 self.job['steps'][1]['run']], cwd=tmp,
                capture_output=True, text=True, check=False)
            self.assertGreater(done.returncode, 1, done.stdout + done.stderr)


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
               "cd8ffd8227e94cdf60ed2580016187353b055cf4")
        steps = self.job["steps"]
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].get("uses"), pin)

        pin_lines = [line for line in self.text.splitlines()
                     if re.fullmatch(r"\s+- uses:.*", line)]
        self.assertEqual(len(pin_lines), 1)
        pin_line = re.fullmatch(r"\s+- uses:\s*(\S+)(\s+#.*)?", pin_lines[0])
        assert pin_line is not None
        self.assertEqual(pin_line.group(1), pin)
        self.assertEqual(pin_line.group(2), "  # v2.0.4")
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


class TestPatchCoverageWorkflows(unittest.TestCase):
    """Keep patch coverage informational and its writer trusted."""

    def setUp(self):
        self.tests_text = (WORKFLOWS / "tests.yml").read_text(encoding="utf-8")
        self.tests = yaml.safe_load(self.tests_text)
        self.jobs = self.tests["jobs"]
        self._stub_interpreter = None

    def _post_step_script(self):
        workflow = yaml.safe_load(
            (WORKFLOWS / "coverage-comment.yml").read_text(encoding="utf-8"))
        return next(
            step["run"]
            for step in workflow["jobs"]["comment"]["steps"]
            if step.get("name") == "Post or update the pull request comment")

    def _run_post_step(self, *, claimed_number="42", current_sha=None,
                       current_repo="owner/repo", comments=(), failure=None):
        """Run the shipped writer script with an allow-listed offline gh stub."""
        with tempfile.TemporaryDirectory(
                prefix="diff-coverage-comment-") as directory:
            cwd = Path(directory)
            (cwd / "body.md").write_text(
                "### Patch coverage of this change\n", encoding="utf-8")
            (cwd / "pr-number.txt").write_text(
                f"{claimed_number}\n", encoding="utf-8")
            calls_path = cwd / "gh-calls.jsonl"
            stub = cwd / "gh"
            stub.with_suffix(".py").write_text(textwrap.dedent("""\
                import json
                import os
                import sys

                def refuse(message):
                    print("strict gh stub refused: " + message, file=sys.stderr)
                    raise SystemExit(90)

                args = sys.argv[1:]
                with open(os.environ["GH_CALLS"], "a", encoding="utf-8") as log:
                    log.write(json.dumps(args) + "\\n")
                if not args or args.pop(0) != "api":
                    refuse("only gh api is allowed")
                method = None
                if "-X" in args:
                    index = args.index("-X")
                    if index + 1 >= len(args):
                        refuse("missing method")
                    method = args[index + 1]
                    del args[index:index + 2]
                endpoints = [arg for arg in args if arg.startswith("repos/")]
                if len(endpoints) != 1:
                    refuse("expected exactly one API endpoint")
                endpoint = endpoints[0]
                scenario = json.loads(os.environ["GH_SCENARIO"])

                def require(condition, message):
                    if not condition:
                        refuse(message)

                headers = ["-H", "Cache-Control: no-cache"]
                if (endpoint == "repos/owner/repo/issues/42/comments"
                        and method is None):
                    require(method is None, "comment listing must be a read")
                    require(args == headers + ["--paginate", endpoint,
                            "--jq", ".[]"], "unexpected comment-list arguments")
                    if scenario["failure"] == "comments":
                        print("comment lookup failed", file=sys.stderr)
                        raise SystemExit(17)
                    for comment in scenario["comments"]:
                        print(json.dumps(comment))
                elif endpoint == "repos/owner/repo/pulls/42":
                    require(method is None, "head lookup must be a read")
                    require(args == headers + [endpoint, "--jq",
                            '[.head.sha // "", .head.repo.full_name // ""] | @tsv'],
                            "unexpected head-lookup arguments")
                    if scenario["failure"] == "head":
                        print("head lookup failed", file=sys.stderr)
                        raise SystemExit(18)
                    print(scenario["current_sha"] + "\\t" +
                          scenario["current_repo"])
                elif endpoint == "repos/owner/repo/issues/comments/314":
                    require(method == "PATCH", "only marker PATCH is allowed")
                    require(args == [endpoint, "-F", "body=@comment.md"],
                            "unexpected PATCH arguments")
                    require("<!-- gh-widgets-diff-coverage -->" in
                            open("comment.md", encoding="utf-8").read(),
                            "PATCH body lacks the marker")
                elif (endpoint == "repos/owner/repo/issues/42/comments"
                      and method == "POST"):
                    require(method == "POST", "only comment POST is allowed")
                    require(args == [endpoint, "-F", "body=@comment.md"],
                            "unexpected POST arguments")
                    require("<!-- gh-widgets-diff-coverage -->" in
                            open("comment.md", encoding="utf-8").read(),
                            "POST body lacks the marker")
                else:
                    refuse("unexpected endpoint " + endpoint)
                raise SystemExit(0)
                """), encoding="utf-8")
            stub.write_text(
                "#!/bin/sh\nexec " +
                shlex.quote(str(self._stub_interpreter or sys.executable)) + " " +
                shlex.quote(str(stub.with_suffix(".py"))) + ' "$@"\n',
                encoding="utf-8")
            stub.chmod(0o755)
            expected_sha = "a" * 40
            scenario = {
                "current_sha": current_sha or expected_sha,
                "current_repo": current_repo,
                "comments": list(comments),
                "failure": failure,
            }
            environment = dict(
                os.environ,
                PATH=f"{cwd}{os.pathsep}{os.environ['PATH']}",
                GH_TOKEN="test-token",
                GH_CALLS=str(calls_path),
                GH_SCENARIO=json.dumps(scenario),
                REPO="owner/repo",
                HEAD_SHA=expected_sha,
                HEAD_REPO="owner/repo",
                PR_NUMBER="42")
            result = subprocess.run(
                ["bash", "-e", "-o", "pipefail", "-c",
                 self._post_step_script()],
                cwd=cwd, env=environment, capture_output=True, text=True,
                check=False, timeout=10)
            calls = ([json.loads(line) for line in
                      calls_path.read_text(encoding="utf-8").splitlines()]
                     if calls_path.exists() else [])
            return result, calls

    def test_diff_coverage_runs_only_for_successful_pull_request_tests(self):
        job = self.jobs.get("diff-coverage")
        self.assertIsNotNone(job, "tests.yml has no diff-coverage job")
        assert job is not None

        self.assertEqual(job.get("needs"), ["unittest"])
        condition = " ".join(job.get("if", "").split())
        self.assertIn("needs.unittest.result == 'success'", condition)
        self.assertIn("github.event_name == 'pull_request'", condition)
        self.assertIn("!cancelled()", condition)
        self.assertEqual(job.get("permissions"), {"contents": "read"})
        self.assertNotIn("diff-coverage", self.jobs["aggregate"].get("needs", []))
        self.assertLess(self.tests_text.index("\n  diff-coverage:"),
                        self.tests_text.index("\n  aggregate:"))

    def test_diff_coverage_measures_the_merge_commit_and_uploads_its_comment(self):
        job = self.jobs.get("diff-coverage")
        self.assertIsNotNone(job, "tests.yml has no diff-coverage job")
        assert job is not None
        steps = job.get("steps", [])
        checkout = next(step for step in steps
                        if step.get("uses", "").startswith("actions/checkout@"))
        self.assertEqual(checkout["with"].get("ref"), "${{ github.sha }}")
        self.assertEqual(checkout["with"].get("fetch-depth"), 0)
        self.assertIs(checkout["with"].get("persist-credentials"), False)

        downloads = [step for step in steps
                     if step.get("uses", "").startswith(
                         "actions/download-artifact@")]
        self.assertEqual(len(downloads), 1)
        self.assertEqual(
            downloads[0]["uses"],
            "actions/download-artifact@"
            "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c")
        self.assertEqual(downloads[0].get("with", {}).get("name"), "coverage-xml")
        self.assertRegex(
            self.tests_text,
            r"(?m)^      - uses: actions/download-artifact@"
            r"3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c # v8\.0\.1$")

        measure = next(step for step in steps
                       if step.get("name") == "Measure the coverage of this change")
        run = measure.get("run", "")
        self.assertIn("--text --no-ext-diff --no-textconv --unified=0", run)
        self.assertIn("HEAD^1..HEAD", run)
        self.assertIn("scripts/ci/diff_coverage.py", run)
        self.assertNotIn("--js-coverage", run)
        self.assertIn("$GITHUB_STEP_SUMMARY", run)

        uploads = [step for step in steps
                   if step.get("uses", "").startswith("actions/upload-artifact@")]
        self.assertEqual(len(uploads), 1)
        self.assertEqual(uploads[0].get("with", {}).get("name"),
                         "diff-coverage-comment")
        self.assertEqual(set(uploads[0]["with"]["path"].splitlines()),
                         {"body.md", "pr-number.txt"})
        self.assertEqual(uploads[0]["with"].get("if-no-files-found"), "error")

    def test_coverage_comment_workflow_has_one_trusted_writer_and_updates_in_place(self):
        path = WORKFLOWS / "coverage-comment.yml"
        self.assertTrue(path.is_file(), "missing coverage-comment.yml")
        text = path.read_text(encoding="utf-8")
        workflow = yaml.safe_load(text)
        trigger_key = "on" if "on" in workflow else True
        self.assertEqual(workflow[trigger_key]["workflow_run"], {
            "workflows": ["tests"], "types": ["completed"],
        })
        self.assertEqual(workflow.get("permissions"), {
            "pull-requests": "write", "actions": "read",
        })
        self.assertRegex(text, r"(?m)^  workflow_run:  # zizmor: ignore\[dangerous-triggers\]$")
        self.assertNotIn("actions/checkout", text)
        self.assertEqual(set(workflow.get("jobs", {})), {"comment"})
        job = workflow["jobs"]["comment"]
        self.assertEqual(
            " ".join(job.get("if", "").split()),
            "github.event.workflow_run.event == 'pull_request'")
        steps = job.get("steps", [])
        self.assertFalse(any(step.get("uses", "").startswith("actions/checkout@")
                             for step in steps))
        downloads = [step for step in steps
                     if step.get("uses", "").startswith(
                         "actions/download-artifact@")]
        self.assertEqual(len(downloads), 1)
        self.assertEqual(downloads[0]["with"].get("name"),
                         "diff-coverage-comment")
        self.assertEqual(downloads[0]["with"].get("run-id"),
                         "${{ github.event.workflow_run.id }}")
        self.assertRegex(
            text,
            r"(?m)^        uses: actions/download-artifact@"
            r"3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c # v8\.0\.1$")

        scripts = "\n".join(step.get("run", "") for step in steps)
        artifact_lookup = next(step for step in steps
                               if step.get("name") == "Check for the comment artifact")
        self.assertIn("Cache-Control: no-cache", artifact_lookup.get("run", ""))
        self.assertIn("--paginate", artifact_lookup.get("run", ""))
        self.assertEqual([step["uses"] for step in steps if "uses" in step],
                         [downloads[0]["uses"]])
        self.assertIn("HEAD_REPO", scripts)
        self.assertIn(".head.sha", scripts)
        self.assertIn(".head.repo.full_name", scripts)
        self.assertIn("test -f pr-number.txt", scripts)
        self.assertIn('if [ "$claimed" != "$PR_NUMBER" ]; then', scripts)
        self.assertIn("<!-- gh-widgets-diff-coverage -->", scripts)
        self.assertIn("gh api -X POST", scripts)
        self.assertIn("gh api -X PATCH", scripts)
        self.assertIn("Cache-Control: no-cache", scripts)
        self.assertIn("--paginate", scripts)
        post_step = next(step for step in steps
                         if step.get("name") == "Post or update the pull request comment")
        post_script = post_step.get("run", "")
        number_check = post_script.index('if [ "$claimed" != "$PR_NUMBER" ]; then')
        self.assertLess(number_check, post_script.index("gh api -X POST"))
        self.assertLess(number_check, post_script.index("gh api -X PATCH"))
        self.assertRegex(
            post_script,
            r"gh api -H 'Cache-Control: no-cache' --paginate \\\n\s+\"repos/\$REPO/issues/\$PR_NUMBER/comments\"")
        self.assertIn("[ \"$current_head_repo\" != \"$HEAD_REPO\" ]", post_script)

    @REQUIRES_POSIX_SHELL
    def test_trusted_writer_refuses_each_head_identity_mismatch(self):
        mismatches = (
            ("sha", "b" * 40, "owner/repo"),
            ("repository", "a" * 40, "fork/repo"),
        )
        for identity, current_sha, current_repo in mismatches:
            with self.subTest(identity=identity):
                result, calls = self._run_post_step(
                    current_sha=current_sha, current_repo=current_repo)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse([call for call in calls if "-X" in call], calls)

    @REQUIRES_POSIX_SHELL
    def test_trusted_writer_refuses_an_artifact_for_another_pull_request(self):
        result, calls = self._run_post_step(claimed_number="43")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to post", result.stderr)
        self.assertFalse([call for call in calls if "-X" in call], calls)
        self.assertEqual(calls, [])

    @REQUIRES_POSIX_SHELL
    def test_trusted_writer_refuses_comment_or_head_lookup_failures(self):
        for failure in ("comments", "head"):
            with self.subTest(failure=failure):
                result, calls = self._run_post_step(failure=failure)

                self.assertNotEqual(result.returncode, 0)
                self.assertFalse([call for call in calls if "-X" in call], calls)

    @REQUIRES_POSIX_SHELL
    def test_trusted_writer_posts_or_patches_exactly_one_marker_comment(self):
        cases = (
            ("post", (), "POST", "repos/owner/repo/issues/42/comments"),
            ("patch", ({
                "id": 314,
                "user": {"login": "github-actions[bot]"},
                "body": "<!-- gh-widgets-diff-coverage --> previous report",
            },), "PATCH", "repos/owner/repo/issues/comments/314"),
        )
        for action, comments, method, endpoint in cases:
            with self.subTest(action=action):
                result, calls = self._run_post_step(comments=comments)

                self.assertEqual(result.returncode, 0, result.stderr)
                writes = [call for call in calls if "-X" in call]
                self.assertEqual(len(writes), 1, calls)
                self.assertEqual(writes[0][writes[0].index("-X") + 1], method)
                self.assertIn(endpoint, writes[0])

    @REQUIRES_POSIX_SHELL
    def test_trusted_writer_runs_stub_with_interpreter_path_containing_spaces(self):
        with tempfile.TemporaryDirectory(prefix="python interpreter ") as root:
            interpreter = Path(root) / "python"
            interpreter.symlink_to(sys.executable)
            self._stub_interpreter = str(interpreter)

            result, calls = self._run_post_step(comments=())
            del self._stub_interpreter

        self.assertEqual(result.returncode, 0, result.stderr)
        writes = [call for call in calls if "-X" in call]
        self.assertEqual(len(writes), 1, calls)
        self.assertEqual(writes[0][writes[0].index("-X") + 1], "POST")


if __name__ == "__main__":
    unittest.main()
