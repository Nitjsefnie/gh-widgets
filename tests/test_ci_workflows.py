"""Offline workflow invariants, run with ``python3 -m unittest discover``."""
# Existing workflow/aggregate controls stay together in these test modules.
import json
import os
import re
import shlex
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

import yaml

from scripts.bench.bench_platform import REQUIRES_POSIX_SHELL
from tests import _ghw_temp


REPO_ROOT = Path(__file__).resolve().parents[1]


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
# A python executable stub that appends ONE JSON array per invocation, so
# the recorded calls keep argv boundaries AND ARGUMENT BYTES: encoding is
# delegated to the interpreter running this suite (a real JSON encoder),
# never to shell command substitution or ad hoc escaping, which lose
# trailing newlines and mangle backslashes. `python -c CODE "$@"` puts the
# stub's own arguments in sys.argv[1:] unchanged, whatever they contain.
_ARGV_JSON_STUB = (
    "#!/bin/sh\n"
    f"exec {shlex.quote(sys.executable)} -c"
    " 'import json, sys; print(json.dumps(sys.argv[1:]))'"
    " \"$@\" >> \"$RECORD\"\n")


def _argv_calls_with_recording_python(installs):
    """Execute each run scalar under bash with a recording python stub.

    Returns every recorded JSON argv vector, in order — the process
    boundary's answer to what the steps actually invoke, with argument
    boundaries preserved.
    """
    calls = []
    with _ghw_temp.tempdir(prefix="ghw-fame-pin-") as tmp:
        stub = Path(tmp) / "python"
        stub.write_text(_ARGV_JSON_STUB)
        stub.chmod(0o755)
        record = Path(tmp) / "calls"
        environment = dict(
            os.environ,
            PATH=f"{tmp}{os.pathsep}{os.environ['PATH']}",
            RECORD=str(record))
        for step in installs:
            subprocess.run(["bash", "-e", "-c", step["run"]],
                           cwd=tmp, env=environment, check=False)
        if record.exists():
            calls = [json.loads(line)
                     for line in record.read_text().splitlines()]
    return calls


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
    with _ghw_temp.tempdir(prefix="gitleaks-download-control-") as tmp:
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


class TestCheckoutCredentials(unittest.TestCase):
    def test_every_checkout_disables_persisted_credentials(self):
        paths = list(WORKFLOWS.glob("*.yml"))
        paths += list((REPO_ROOT / ".github/actions").glob("**/action.yml"))
        seen = 0
        for path in paths:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            if "jobs" in document:
                steps = [step for job in document["jobs"].values()
                         for step in job.get("steps", [])]
            else:
                steps = document.get("runs", {}).get("steps", [])
            for step in steps:
                if step.get("uses", "").startswith("actions/checkout@"):
                    seen += 1
                    with self.subTest(path=path, step=step.get("name")):
                        self.assertIs(step.get("with", {}).get("persist-credentials"), False)
        self.assertGreater(seen, 0)


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
        self.assertRegex(self.secrets_text, r"(?m)^permissions:\n  contents: read\n\n")
        self.assertRegex(self.secrets_text, "(?m)^concurrency:\n  group: " + re.escape(
            "secrets-${{ github.ref }}${{ github.event_name == 'push' && format('-{0}', github.sha) || '' }}")
            + r"\n  cancel-in-progress: true\n\n")
        self.assertRegex(self.gitleaks_job, r"(?m)^    runs-on: ubuntu-latest$")
        self.assertRegex(self.gitleaks_job, r"(?m)^    timeout-minutes: 10$")
        checkouts = [index for index, line in enumerate(_steps_block(self.gitleaks_job))
                     if line.startswith("      - uses: actions/checkout@")]
        self.assertEqual(len(checkouts), 1)
        download = self._named_step(
            self.gitleaks_job, "Download gitleaks and verify its digest")
        download_script = textwrap.dedent(
            download.split("run: |\n", 1)[1]).strip()
        # Which digest pins the release is the workflow's business; what
        # must hold is that exactly one line hands a sha256 digest to
        # sha256sum verification.
        digest_lines = [
            line.strip() for line in download_script.splitlines()
            if re.fullmatch(r"echo '[0-9a-f]{64}  \S+' \| sha256sum -c -",
                            line.strip())]
        self.assertEqual(len(digest_lines), 1,
                         "the download must verify a sha256 digest")
        self.assertNotRegex(self._named_step(self.gitleaks_job, "Scan the tree and the history"), r"(?m)^\s+if:")

    @REQUIRES_POSIX_SHELL
    def test_the_scan_steps_refuse_the_judged_tree_s_own_exemptions(self):
        steps = {step["name"]: step["run"] for step in yaml.safe_load(self.secrets_text)["jobs"]["gitleaks"]["steps"] if "run" in step}
        trusted, scan = (steps["Take the scan's config and ignore list from the trusted commit"], steps["Scan the tree and the history"])
        with _ghw_temp.tempdir(prefix="gitleaks-steps-") as tmp:
            repo = Path(tmp)
            (repo / "run").mkdir()
            (repo / "gitleaks").write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" > \"$RUNNER_TEMP/argv\"\n[ -e .gitleaksignore ] && echo ignore-present >> \"$RUNNER_TEMP/argv\"\nprintf '%s\\n' \"$(cat .git/info/attributes)\" >> \"$RUNNER_TEMP/argv\"\n")
            (repo / "gitleaks").chmod(0o755)
            git = ["git", "-C", str(repo)]
            subprocess.run(git + ["init", "-q"], check=True)
            # The runner has no configured identity; a fixture commit needs one.
            subprocess.run(git + ["config", "user.name", "fixture"], check=True)
            subprocess.run(git + ["config", "user.email", "fixture@example.com"], check=True)
            (repo / ".gitleaks.toml").write_text("[extend]\nuseDefault = true\n")
            subprocess.run(git + ["add", "-A"], check=True)
            subprocess.run(git + ["commit", "-qm", "base"], check=True)
            base = subprocess.check_output(git + ["rev-parse", "HEAD"], text=True).strip()
            (repo / ".gitleaks.toml").write_text('[extend]\npath = "x"\n')
            (repo / ".gitleaksignore").write_text("judged\n")
            subprocess.run(git + ["add", "-A"], check=True)
            subprocess.run(git + ["commit", "-qm", "head"], check=True)
            head = subprocess.check_output(git + ["rev-parse", "HEAD"], text=True).strip()

            def step(script, **env):
                return subprocess.run(["bash", "-e", "-c", script], cwd=repo, check=False, text=True,
                                      capture_output=True, env=dict(os.environ, RUNNER_TEMP=str(repo / "run"), **env))
            subprocess.run(git + ["update-ref", "refs/remotes/origin/main", base], check=True)
            for event, key in (("pull_request", {"BASE_SHA": base}), ("push", {"BEFORE_SHA": base}), ("schedule", {})):
                done = step(trusted, **{"EVENT_NAME": event, "BASE_SHA": "", "BEFORE_SHA": "", **key})
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(("useDefault" in (repo / "run/gitleaks-trusted/.gitleaks.toml").read_text(), (repo / "run/gitleaks-trusted/.gitleaksignore").read_text()), (True, ""))
            refused = step(trusted, EVENT_NAME="pull_request", BASE_SHA=head, BEFORE_SHA="")
            self.assertNotEqual(refused.returncode, 0, "a trusted [extend] path must be refused")
            done = step(scan, EVENT_NAME="push", BASE_SHA="", BEFORE_SHA=base)
            self.assertEqual(done.returncode, 0, done.stderr)
            saw = (repo / "run/argv").read_text().splitlines()
            self.assertNotIn("ignore-present", saw, "the judged tree's .gitleaksignore must go")
            self.assertEqual((saw[-1], [f for f in ("--ignore-gitleaks-allow", "--redact", f"--config {repo}/run/gitleaks-trusted/.gitleaks.toml", f"--gitleaks-ignore-path {repo}/run/gitleaks-trusted") if f not in saw[0]]), ("* diff", []))

    @REQUIRES_POSIX_SHELL
    def test_digest_failure_gates_extraction_and_the_download_uses_its_url(self):
        download = self._named_step(
            self.gitleaks_job, "Download gitleaks and verify its digest")
        script = textwrap.dedent(download.split("run: |\n", 1)[1]).strip()
        # The release the script downloads is the workflow's business; the
        # assertion is that the failed-digest run fetched the URL the
        # script itself names, not any release a test hard-codes.
        expected_url = re.search(
            r"https://github\.com/gitleaks/gitleaks/releases/download/"
            r"\S+\.tar\.gz", script)
        assert expected_url is not None, script
        returncode, curl_output, was_extracted = (
            _run_download_with_failed_digest(script))
        self.assertIn(expected_url.group(0), curl_output)
        self.assertNotEqual(returncode, 0, "a checksum mismatch must make the download step fail")
        self.assertFalse(was_extracted, "a checksum mismatch must prevent archive extraction")

    def test_scan_failure_cannot_be_swallowed_by_the_gitleaks_job(self):
        self.assertNotRegex(self.gitleaks_job, CONTINUE_ON_ERROR_KEY,
                            "continue-on-error converts a failing step or job into a successful one, "
                            "defeating the aggregate's job-conclusion fold")

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
                         r"(?m)^          GH_WIDGETS_SCAN_WAIT_BOUND_S: '240'$")
        self.assertRegex(step, r"(?m)^          GH_WIDGETS_HEAD_SHA: \$\{\{ github\.event\.pull_request\.head\.sha \|\| github\.sha \}\}$")
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
        self.assertEqual({name.strip() for name in needs.group(1).split(",")},
                         {"changes", "plan", "test", "report", "lint", "pyright", "pip-audit",
                          "speed", "analyze", "actionlint", "gates"})
        trigger = _trigger_block(self.workflow)
        self.assertNotRegex(trigger, r"(?m)^\s+paths(?:-ignore)?:")
        self.assertFalse((self.scripts / "aggregate_gates.py").exists())

    def test_push_filter_matches_secrets_workflow(self):
        workflow = yaml.safe_load(_trigger_block(self.workflow))
        secrets = yaml.safe_load(_trigger_block(
            (WORKFLOWS / "secrets.yml").read_text(encoding="utf-8")))
        trigger_key = "on" if "on" in workflow else True
        self.assertEqual(workflow[trigger_key]["push"],
                         secrets[trigger_key]["push"])

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
        self.assertEqual(normalized, "always() && (github.event_name == 'pull_request' || github.event_name == 'workflow_dispatch' || "
                                     "(github.event_name == 'push' && github.ref == 'refs/heads/main'))")

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
                self.assertEqual(owner_events, ["schedule", "workflow_dispatch"])
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
                self.assertNotRegex(block, r"(?m)^    uses:\s*\./\.github/workflows/")

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
        self.assertEqual(" ".join(group.group(1).split()), "github.event_name == 'pull_request' && format('tests-pr-{0}', github.event.pull_request.number) || github.run_id")

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
        with _ghw_temp.tempdir(prefix='ghw-markers-') as tmp:
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
        with _ghw_temp.tempdir(prefix='ghw-no-git-') as tmp:
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

        self.assertEqual(set(groups), {"github-actions", "github-actions-security"})
        self.assertEqual(groups["github-actions"]["applies-to"], "version-updates")
        self.assertEqual(groups["github-actions"]["patterns"], ["*"])
        self.assertEqual(groups["github-actions-security"]["applies-to"], "security-updates")
        self.assertEqual(groups["github-actions-security"]["patterns"], ["*"])
        # No labels: the fleet's convention, and a label has to exist first.
        self.assertEqual([update for update in document["updates"] if "labels" in update], [])


class TestMeasuredCellName(unittest.TestCase):
    """One spelling for the cell `report` gates, wherever it is named."""

    def test_every_site_holds_that_cell_name(self):
        def cell(path, job, action):
            workflow = yaml.safe_load((WORKFLOWS / path).read_text(encoding="utf-8"))
            return next(step["with"]["cell"] for step in workflow["jobs"][job]["steps"]
                        if f"test-ratchet/{action}@" in step.get("uses", ""))

        name = "ubuntu-latest-py3.15"
        self.assertEqual(cell("tests.yml", "test", "run"), "${{ matrix.os }}-py${{ matrix.python-version }}")
        self.assertEqual(cell("tests.yml", "report", "report"), name)
        self.assertEqual(cell("test-ratchet-comment.yml", "comment", "report"), name)
        thresholds, times = (json.loads((REPO_ROOT / path).read_text(encoding="utf-8")) for path in (".github/ci-thresholds.json", ".github/test-times.json"))
        self.assertEqual(thresholds["coverage"]["python"]["cell"], name)
        # A recorded key must be spelled as the run step reports it, or the
        # timings stop matching the cell they measured.
        self.assertEqual(sorted(times["cells"]), [f"{os}-py{v}" for os in ("macos-latest", "ubuntu-latest", "windows-latest") for v in ("3.14", "3.15")])


class TestClaimWorkflowShape(unittest.TestCase):
    """Pin the claim workflow's command filter, permissions and action input."""

    def setUp(self):
        self.path = WORKFLOWS / "claim.yml"
        self.text = self.path.read_text(encoding="utf-8")
        self.workflow = yaml.safe_load(self.text)
        self.job = self.workflow["jobs"]["claim"]

    def test_condition_filters_only_bots_and_non_commands(self):
        condition = " ".join(self.job["if"].split())
        self.assertEqual(condition, "github.event.comment.user.type != 'Bot' && (contains(github.event.comment.body, '/claim') || "
                                    "contains(github.event.comment.body, '/unclaim') || contains(github.event.comment.body, '/release'))")
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
        self.assertEqual(condition, "${{ !github.event.repository.fork && "
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


class TestRatchetPushWorkflow(unittest.TestCase):
    """The artifact push job stays data-only and main-scoped."""

    @classmethod
    def setUpClass(cls):
        cls.path = WORKFLOWS / "ratchet-push.yml"
        cls.text = cls.path.read_text(encoding="utf-8")
        cls.workflow = yaml.safe_load(cls.text)
        cls.trigger_key = "on" if "on" in cls.workflow else True
        cls.job = cls.workflow["jobs"]["push"]
        cls.steps = cls.job["steps"]

    def _step(self, name):
        return next(step for step in self.steps if step.get("name") == name)

    def _validator(self):
        run = self._step("Validate and push the ratchet record")["run"]
        marker = ('document_name="$(python3 - "$data" "$repo" '
                  '"$merged_record" <<\'PY\'\n')
        start = run.index(marker) + len(marker)
        end = run.index("\nPY\n)", start)
        return run[start:end]

    def _artifact_preflight(self):
        run = self._step("Validate and push the ratchet record")["run"]
        marker = 'document_name="$(python3 - "$data" <<\'PY\'\n'
        start = run.index(marker) + len(marker)
        end = run.index("\nPY\n)", start)
        return run[start:end]

    @staticmethod
    def _run_validator(script, data, repo, name):
        # The third argument is the merged document the validator judges;
        # these controls validate the raw artifact, so it stands in.
        return subprocess.run(
            [sys.executable, "-c", script, str(data), str(repo),
             str(data / name)],
            capture_output=True, text=True, check=False)

    def test_workflow_run_trigger_and_push_guard_are_exact(self):
        self.assertEqual(self.workflow[self.trigger_key]["workflow_run"], {
            "workflows": ["tests"],
            "types": ["completed"],
        })
        self.assertRegex(self.text, r"(?m)^  workflow_run:  # zizmor: ignore\[dangerous-triggers\]$")
        self.assertEqual(self.workflow["permissions"], {})
        self.assertEqual(self.workflow["concurrency"], {
            "group": "ratchet-push", "cancel-in-progress": False,
            "queue": "max",
        })
        self.assertEqual(" ".join(self.job["if"].split()),
                         "github.event.workflow_run.event == 'push' && github.event.workflow_run.head_branch == 'main' && "
                         "github.event.workflow_run.conclusion == 'success' && "
                         "github.event.workflow_run.head_repository.full_name == github.repository")
        self.assertEqual(self.job["environment"], "main-push")
        self.assertEqual(self.job["timeout-minutes"], 15)
        self.assertEqual(self.job["permissions"], {"actions": "read", "contents": "read"})

    def test_only_the_triggering_runs_ratchet_artifact_is_downloaded(self):
        listing = self._step("List the triggering run's artifacts")
        self.assertIn("Cache-Control: no-cache", listing["run"])
        self.assertIn("--paginate", listing["run"])
        self.assertIn("select(.expired != true)", listing["run"])
        self.assertIn("ratchet-push", listing["run"])
        self.assertEqual(listing["env"]["GH_WIDGETS_RUN_ID"],
                         "${{ github.event.workflow_run.id }}")

        downloads = [step for step in self.steps
                     if step.get("uses", "").startswith(
                         "actions/download-artifact@")]
        self.assertEqual(len(downloads), 1)

    @REQUIRES_POSIX_SHELL
    def test_artifact_listing_executes_the_paginated_nonexpired_lookup(self):
        listing = self._step("List the triggering run's artifacts")
        with _ghw_temp.tempdir(prefix="ghw-ratchet-list-") as tmp:
            root = Path(tmp)
            binary = root / "bin"
            binary.mkdir()
            calls = root / "gh-arguments.txt"
            gh = binary / "gh"
            gh.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$@\" > \"$GH_ARGUMENTS\"\n"
                "printf '%s\\n' ratchet-push\n", encoding="utf-8")
            gh.chmod(0o755)
            output = root / "output.txt"
            environment = dict(
                os.environ,
                PATH=f"{binary}{os.pathsep}{os.environ['PATH']}",
                GH_ARGUMENTS=str(calls),
                GH_TOKEN="fixture-" + "token",
                GH_WIDGETS_REPO="owner/repo",
                GH_WIDGETS_RUN_ID="12345678",
                GITHUB_OUTPUT=str(output))
            result = subprocess.run(
                ["bash", "-e", "-o", "pipefail", "-c", listing["run"]],
                cwd=root, env=environment, capture_output=True, text=True,
                check=False, timeout=10)
            output_text = output.read_text(encoding="utf-8")
            call_lines = calls.read_text(encoding="utf-8").splitlines()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("present=true", output_text)
        self.assertEqual(call_lines, [
            "api", "-H", "Cache-Control: no-cache", "--paginate",
            "repos/owner/repo/actions/runs/12345678/artifacts", "--jq",
            ".artifacts[] | select(.expired != true) | .name",
        ])

    def test_privileged_job_checks_out_and_runs_no_repository_code(self):
        self.assertFalse(any(step.get("uses", "").startswith("actions/checkout@") for step in self.steps))
        self.assertEqual([step["uses"].split("@", maxsplit=1)[0] for step in self.steps if "uses" in step],
                         ["actions/download-artifact"])
        for step in self.steps:
            run = step.get("run", "")
            self.assertNotIn("scripts/ci/", run)
            self.assertNotIn("pip install", run)
            self.assertNotIn("node scripts/", run)
        push = self._step("Validate and push the ratchet record")
        self.assertIn("git init", push["run"])
        self.assertIn("git -C \"$repo\" fetch --quiet origin main", push["run"])
        exports = [shlex.split(line)[1] for line in push["run"].splitlines()
                   if line.startswith("export GIT_SSH_COMMAND=")]
        self.assertEqual(exports, [
            "GIT_SSH_COMMAND=ssh -o StrictHostKeyChecking=yes "
            "-o UserKnownHostsFile=$known_hosts -o BatchMode=yes"])
        self.assertIn("ssh-agent", push["run"])
        self.assertIn("trap 'kill \"$SSH_AGENT_PID\"", push["run"])
        self.assertNotIn("GITHUB_TOKEN", push.get("env", {}))
        self.assertIn("git -C \"$repo\" config user.name 'github-actions[bot]'", push["run"])
        self.assertIn("git -C \"$repo\" config user.email '41898282+github-actions[bot]@users.noreply.github.com'", push["run"])
        self.assertIn('git -C "$repo" commit -m "$subject"', push["run"])
        self.assertIn('git -C "$repo" push origin HEAD:refs/heads/main', push["run"])

    @REQUIRES_POSIX_SHELL
    def test_empty_deploy_key_fails_loudly_before_ssh(self):
        push = self._step("Validate and push the ratchet record")
        with _ghw_temp.tempdir(prefix="ghw-ratchet-empty-key-") as tmp:
            directory = Path(tmp)
            environment = dict(
                os.environ,
                GH_WIDGETS_MASTER_PUSH_DEPLOY_KEY="",
                GH_WIDGETS_REPO="owner/repo",
                GH_WIDGETS_RUN_ID="12345678",
                RUNNER_TEMP=str(directory),
            )
            result = subprocess.run(
                ["bash", "-e", "-o", "pipefail", "-c", push["run"]],
                cwd=directory, env=environment, capture_output=True,
                text=True, check=False, timeout=10)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("GH_WIDGETS_MASTER_PUSH_DEPLOY_KEY is empty",
                      result.stderr)

    @REQUIRES_POSIX_SHELL
    def test_artifact_preflight_accepts_one_sha_line_and_rejects_extra_files(self):
        script = self._artifact_preflight()
        with _ghw_temp.tempdir(prefix="ghw-ratchet-preflight-") as tmp:
            data = Path(tmp) / "ratchet-data"
            data.mkdir()
            (data / "base.txt").write_bytes(b"a" * 40 + b"\n")
            (data / "speed-baseline.json").write_text("{}", encoding="utf-8")
            accepted = subprocess.run(
                [sys.executable, "-c", script, str(data)],
                capture_output=True, text=True, check=False)
            self.assertEqual(accepted.returncode, 0,
                             accepted.stdout + accepted.stderr)
            self.assertEqual(accepted.stdout.strip(), "speed-baseline.json")

            (data / "unexpected.txt").write_text("data", encoding="utf-8")
            refused = subprocess.run(
                [sys.executable, "-c", script, str(data)],
                capture_output=True, text=True, check=False)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("exactly base.txt", refused.stderr)

    @REQUIRES_POSIX_SHELL
    def test_each_document_maps_to_its_gate_record_path(self):
        push = self._step("Validate and push the ratchet record")["run"]
        start = push.index('base="$(cat "$data/base.txt")"')
        end = push.index("eval \"$(ssh-agent -s)\"")
        mapping = push[start:end]
        with _ghw_temp.tempdir(prefix="ghw-ratchet-path-map-") as tmp:
            data = Path(tmp) / "ratchet-data"
            data.mkdir()
            (data / "base.txt").write_bytes(b"a" * 40 + b"\n")
            for document_name in ("speed-baseline.json",):
                with self.subTest(document=document_name):
                    mapped = subprocess.run(
                        ["bash", "-e", "-o", "pipefail", "-c",
                         mapping + "\nprintf '%s\\n' \"$record_path\"\n"],
                        cwd=tmp, env=dict(os.environ,
                                          data=str(data),
                                          document_name=document_name),
                        capture_output=True, text=True, check=False)
                    self.assertEqual(mapped.returncode, 0, mapped.stderr)
                    self.assertEqual(mapped.stdout.strip(),
                                     f"gate/{document_name}")

    def _run_rejected_push(self, remote_main):
        run = self._step("Validate and push the ratchet record")["run"]
        start = run.index(
            'if git -C "$repo" push origin HEAD:refs/heads/main; then')
        fragment = run[start:]
        with _ghw_temp.tempdir(prefix="ghw-ratchet-push-reject-") as tmp:
            root = Path(tmp)
            binary = root / "bin"
            binary.mkdir()
            stub = binary / "git"
            stub.write_text(
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *'push origin HEAD:refs/heads/main') exit 1 ;;\n"
                "  *'fetch --quiet origin main') exit 0 ;;\n"
                "  *'rev-parse FETCH_HEAD') printf '%s\\n' \"$REMOTE_MAIN\" ;;\n"
                "  *) echo \"unexpected git call: $*\" >&2; exit 90 ;;\n"
                "esac\n", encoding="utf-8")
            stub.chmod(0o755)
            summary = root / "summary.md"
            script = "\n".join((
                "set -euo pipefail",
                'repo="ratchet-repo"',
                'fetched="' + "a" * 40 + '"',
                fragment,
            ))
            environment = dict(
                os.environ,
                PATH=f"{binary}{os.pathsep}{os.environ['PATH']}",
                REMOTE_MAIN=remote_main,
                GITHUB_STEP_SUMMARY=str(summary),
            )
            result = subprocess.run(
                ["bash", "-e", "-o", "pipefail", "-c", script],
                cwd=root, env=environment, capture_output=True, text=True,
                check=False, timeout=10)
            summary_text = (summary.read_text(encoding="utf-8")
                            if summary.is_file() else "")
        return result, summary_text

    @REQUIRES_POSIX_SHELL
    def test_rejected_push_after_main_moves_drops_green(self):
        result, summary = self._run_rejected_push("b" * 40)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("::notice::Main moved", result.stdout)
        self.assertIn("Dropped ratchet commit", summary)

    @REQUIRES_POSIX_SHELL
    def test_rejected_push_while_main_stands_still_fails(self):
        result, summary = self._run_rejected_push("a" * 40)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("rejected while main stood still", result.stderr)
        self.assertEqual(summary, "")

    @staticmethod
    def _speed_documents():
        """A recorded speed document and one candidate, over one entry.

        Built here rather than read from the committed baseline: the gate
        must keep its verdict for ANY population shape it accepts, and the
        committed record legitimately carries empty `entries` while its
        seeds await the first post-merge measurement.
        """
        population = {"metric": "cpu_time_ratio", "tolerance": 0.45,
                      "population": "a" * 64,
                      "entries": {"counter::unit-suite": {"min": 5.0,
                                                          "max": 10.0,
                                                          "n": 6}},
                      "seeds": ["counter::unit-suite"],
                      "wall": {}}
        recorded = {"schema": 5, "basis": "fixture", "cell": "fixture", "interpreter": "3.15",
                    "measured_commit": "a" * 40, "measured_at": "2026-10-24",
                    "populations": {"unit-suite": population}}
        candidate = json.loads(json.dumps(recorded))
        candidate["populations"]["unit-suite"]["entries"][
            "counter::unit-suite"]["max"] = 9.0
        return recorded, candidate

    @staticmethod
    def _speed_validator_tree(prefix):
        """Sibling `data` and `main` directories, as the job lays them out.

        The validator reads the candidate from `$data` and fetched main from
        `$repo/gate`; `$repo` must sit BESIDE the artifact, not inside it,
        because the artifact preflight refuses a `$data` holding any third
        entry.
        """
        root = Path(_ghw_temp.mkdtemp(prefix=prefix))
        data, repo = root / "data", root / "main"
        data.mkdir()
        return data, repo

    def _speed_validator_case(self, script, data, repo, recorded, candidate,
                              status, fragment):
        (repo / "gate").mkdir(parents=True, exist_ok=True)
        (repo / "gate/speed-baseline.json").write_text(
            json.dumps(recorded), encoding="utf-8")
        (data / "base.txt").write_text("a" * 40 + "\n", encoding="utf-8")
        (data / "speed-baseline.json").write_text(
            json.dumps(candidate), encoding="utf-8")
        result = self._run_validator(script, data, repo, "speed-baseline.json")
        self.assertEqual(result.returncode, status, result.stderr)
        if fragment:
            self.assertIn(fragment, result.stderr)
        return result

    def test_inline_speed_validator_accepts_tightening_and_refuses_looseners(self):
        script = self._validator()
        data, repo = self._speed_validator_tree("ghw-speed-validator-")
        self.addCleanup(_ghw_temp.rmtree, data.parent)
        recorded, candidate = self._speed_documents()
        self._speed_validator_case(script, data, repo, recorded, candidate, 0, "")
        recorded_max = recorded["populations"]["unit-suite"]["entries"][
            "counter::unit-suite"]["max"]
        candidate["populations"]["unit-suite"]["entries"][
            "counter::unit-suite"]["max"] = recorded_max + 0.001
        refused = self._speed_validator_case(
            script, data, repo, recorded, candidate, 1, "loosened")
        self.assertEqual(refused.stdout.strip(), "")

    @staticmethod
    def _seed_loophole_cases(recorded, seeded):
        """(recorded, candidate, fragment): each route around the seed shape.

        Every case must refuse, naming why. The first one rewrites the
        recorded side too — with no seeds list anywhere, the same addition
        is refused, which is the property that keeps the allowance closed
        for everything it does not name.
        """
        no_seeds = json.loads(json.dumps(recorded))
        no_seeds["populations"]["unit-suite"].pop("seeds")
        no_seeds_seeded = json.loads(json.dumps(no_seeds))
        no_seeds_seeded["populations"]["unit-suite"]["entries"][
            "counter::unit-suite"] = {"min": 9.0, "max": 9.0, "n": 2}
        # No seeds list at all is not the schema-5 shape; the shape rule refuses first.
        yield no_seeds, no_seeds_seeded, "unexpected shape"

        def mutated(fragment, case):
            mutant = json.loads(json.dumps(seeded))
            case(mutant["populations"]["unit-suite"])
            return recorded, mutant, fragment

        def wrong_shape(unit):
            unit["entries"]["counter::unit-suite"] = {"min": 8.0, "max": 9.0,
                                                      "n": 2}
        yield mutated("not a seed-shaped range", wrong_shape)

        def wrong_n(unit):
            unit["entries"]["counter::unit-suite"] = {"min": 9.0, "max": 9.0,
                                                      "n": 1}
        yield mutated("not a valid sample count", wrong_n)

        def on_wall(unit):
            unit["wall"]["counter::unit-suite"] = {"min": 9.0, "max": 9.0,
                                                   "n": 2}
        yield mutated("changed its entries", on_wall)

        def undeclared_node(unit):
            unit["entries"]["counter::undeclared"] = {"min": 9.0, "max": 9.0,
                                                      "n": 2}
        yield mutated("changed its entries", undeclared_node)

        def changed_seeds(unit):
            unit["seeds"] = ["counter::other"]
        yield mutated("changed seeds", changed_seeds)

        def duplicate_seeds(unit):
            unit["seeds"] = ["counter::unit-suite", "counter::unit-suite"]
        yield mutated("invalid seeds", duplicate_seeds)

        def empty_seeds(unit):
            unit["seeds"] = []
        yield mutated("invalid seeds", empty_seeds)

        def non_string_seed(unit):
            unit["seeds"] = [4]
        yield mutated("invalid seeds", non_string_seed)

        def no_change(unit):
            unit["entries"] = {}
        yield mutated("no strict maximum reduction", no_change)

    def test_inline_speed_validator_judges_seeds_and_their_loopholes(self):
        """A seed lands only seed-declared, seed-shaped, entries-only.

        The post-merge state: main records no entries and the first
        measurement seeds them. The validator must accept exactly the seeded
        artifact and refuse every route around the shape — an undeclared
        addition, a non-degenerate range, a wall addition, a changed or
        malformed seeds list, and an artifact with neither a lowering nor a
        seed.
        """
        script = self._validator()
        recorded, seeded = self._speed_documents()
        # Post-merge state: fetched main records no entries yet, so the candidate's entry is an addition.
        recorded["populations"]["unit-suite"]["entries"] = {}
        seeded["populations"]["unit-suite"]["entries"] = {}
        seeded["populations"]["unit-suite"]["entries"][
            "counter::unit-suite"] = {"min": 9.0, "max": 9.0, "n": 2}
        data, repo = self._speed_validator_tree("ghw-speed-seed-validator-")
        self.addCleanup(_ghw_temp.rmtree, data.parent)
        accepted = self._speed_validator_case(
            script, data, repo, recorded, seeded, 0, "")
        self.assertEqual(accepted.stdout.strip(), "speed-baseline.json")
        for recorded_case, candidate_case, fragment in self._seed_loophole_cases(
                recorded, seeded):
            self._speed_validator_case(script, data, repo, recorded_case,
                                       candidate_case, 1, fragment)

    def test_inline_speed_validator_reads_schema_five_only(self):
        """Post-merge main only sees schema 5; a schema-4 artifact has no route."""
        script = self._validator()
        recorded, candidate = self._speed_documents()
        recorded["schema"] = 4
        candidate["schema"] = 4
        recorded["populations"]["unit-suite"]["slopes"] = {
            "counter::unit-suite": 2.0}
        data, repo = self._speed_validator_tree("ghw-speed-schema-validator-")
        self.addCleanup(_ghw_temp.rmtree, data.parent)
        self._speed_validator_case(script, data, repo, recorded, candidate,
                                   1, "must declare schema 5")


class TestActionFamiliesShareOneRef(unittest.TestCase):
    """One revision per action family across the workflows, derived from them.

    Dependabot bumps one dependency across every workflow in one pull
    request, so two revisions of one family mean a hand edit moved only
    some of its uses: lines — jobs then run mixed releases of the same
    action. The refs are read from the workflows, never hard-coded here,
    so a coordinated bump stays green and only the split reddens.
    """

    USES = re.compile(r"(?m)^\s*(?:-\s*)?uses:\s*([\w.-]+/[\w./-]+)@(\S+)")

    def test_every_action_family_shares_one_ref(self):
        refs = {}
        for path in sorted(WORKFLOWS.glob("*.yml")):
            for action, ref in self.USES.findall(path.read_text()):
                if action.startswith("./"):
                    continue
                family = "/".join(action.split("/")[:2])
                refs.setdefault(family, {}).setdefault(ref, []).append(
                    f"{path.name} ({action})")
        self.assertTrue(refs, "no external action reference found")
        split = {family: where for family, where in refs.items()
                 if len(where) > 1}
        self.assertFalse(split, "an action family must use one revision across the workflows: " f"{split}")


if __name__ == "__main__":
    unittest.main()
