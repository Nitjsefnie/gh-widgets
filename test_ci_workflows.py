"""Invariants over .github/workflows that no workflow run can check itself.

    python3 -m unittest discover -v

Stdlib unittest, matching the rest of this repo's suite.
"""
import re
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
import xml.etree.ElementTree as ET


REPO_ROOT = Path(__file__).resolve().parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
CODEQL_USE = re.compile(r"uses:\s*github/codeql-action/([\w-]+)@(\S+)")
FORK_PIN = re.compile(r'FORK_PIN="git\+https://github\.com/Nitjsefnie-OSC/'
                      r'git-fame@([0-9a-f]{40})"')
DOCUMENTED_PIN = re.compile(r"git\+https://github\.com/Nitjsefnie-OSC/"
                            r"git-fame@([0-9a-f]{40})")


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


class TestSpeedWorkflowRendererGate(unittest.TestCase):
    """Exercise the workflow's unit and renderer comparison argument shape."""

    workflow = WORKFLOWS / "speed.yml"

    @staticmethod
    def _run_block(step_name):
        lines = (WORKFLOWS / "speed.yml").read_text().splitlines()
        step_line = f"      - name: {step_name}"
        start = lines.index(step_line)
        run_line = next(index for index in range(start + 1, len(lines))
                        if lines[index].startswith("        run: |"))
        body = []
        for line in lines[run_line + 1:]:
            if line.startswith("      - "):
                break
            if line.startswith("          "):
                body.append(line[10:])
            elif not line.strip():
                body.append("")
            else:
                break
        return "\n".join(body) + "\n"

    @staticmethod
    def _write_report(path, classname, name, duration):
        path.parent.mkdir(parents=True, exist_ok=True)
        suite = ET.Element("testsuite", {"name": "fixture", "tests": "1"})
        ET.SubElement(suite, "testcase", {
            "classname": classname,
            "name": name,
            "time": str(duration),
        })
        ET.ElementTree(suite).write(path, encoding="utf-8",
                                    xml_declaration=True)

    def test_compare_runs_once_for_each_report_family(self):
        with tempfile.TemporaryDirectory(prefix="ghw-speed-compare-shape-") as td:
            root = Path(td)
            reports = root / "reports"
            for round_number in (1, 2):
                self._write_report(
                    reports / f"base-{round_number}.xml", "unit",
                    "test_unit", 0.10)
                self._write_report(
                    reports / f"head-{round_number}.xml", "unit",
                    "test_unit", 0.11)
                self._write_report(
                    reports / f"bench-base-{round_number}.xml", "e2e",
                    "bench.render", 0.20)
                self._write_report(
                    reports / f"bench-head-{round_number}.xml", "e2e",
                    "bench.render", 0.21)

            comparator = root / "head" / "scripts" / "ci" / (
                "compare_durations.py")
            comparator.parent.mkdir(parents=True)
            shutil.copyfile(REPO_ROOT / "scripts" / "ci" /
                            "compare_durations.py", comparator)
            summary = root / "summary.md"
            env = {
                **os.environ,
                "BASE_TAG": "fixture-baseline",
                "GITHUB_STEP_SUMMARY": str(summary),
                "MAX_REGRESSION": "0.30",
            }
            completed = subprocess.run(
                ["bash", "-e", "-o", "pipefail", "-c",
                 self._run_block("Compare")],
                cwd=root, env=env, capture_output=True, text=True,
                check=False)

            self.assertEqual(completed.returncode, 0,
                             completed.stdout + completed.stderr)
            summary_text = summary.read_text(encoding="utf-8")
            self.assertIn("fixture-baseline`", summary_text)
            self.assertIn("fixture-baseline renderer workloads`",
                          summary_text)

    def test_baseline_renderer_rounds_share_head_outputs_and_selfcheck(self):
        block = self._run_block("Run renderer workloads, interleaved")
        self.assertIn('--work-root "$RUNNER_TEMP/gh7-bench/$side"', block)
        self.assertIn('if [ "$side" = "head" ] && [ "$round" -eq '
                      '"$ROUNDS" ]; then', block)
        self.assertIn("selfcheck=(--selfcheck)", block)


if __name__ == "__main__":
    unittest.main()
