"""Invariants over .github/workflows that no workflow run can check itself.

    python3 -m unittest discover -v

Stdlib unittest, matching the rest of this repo's suite.
"""
import re
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
