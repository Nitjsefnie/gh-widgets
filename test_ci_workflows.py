"""Invariants over .github/workflows that no workflow run can check itself.

    python3 -m unittest discover -v

Stdlib unittest, matching the rest of this repo's suite.
"""
import re
import os
import shutil
import subprocess
import sys
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
    def _write_report(path, classname, cases):
        """One <testsuite> carrying `cases`, a sequence of (name, duration)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        suite = ET.Element("testsuite", {
            "name": "fixture", "tests": str(len(cases))})
        for name, duration in cases:
            ET.SubElement(suite, "testcase", {
                "classname": classname,
                "name": name,
                "time": str(duration),
            })
        ET.ElementTree(suite).write(path, encoding="utf-8",
                                    xml_declaration=True)

    def _write_comparison_reports(self, root, unit_head, renderer_head,
                                  omit_renderer=None):
        """Two rounds of both report families, as speed.yml produces them.

        `omit_renderer` drops one workload from the HEAD renderer report
        only — the shape issue #36 is about, where the gate keeps measuring
        one fewer shipped renderer and still reports a pass.
        """
        reports = root / "reports"
        workloads = [("bench.render", renderer_head),
                     ("bench.render-impact", 0.20),
                     ("bench.render-responsiveness", 0.05)]
        for round_number in (1, 2):
            self._write_report(reports / f"base-{round_number}.xml", "unit",
                               [("test_unit", 0.10)])
            self._write_report(reports / f"head-{round_number}.xml", "unit",
                               [("test_unit", unit_head)])
            self._write_report(reports / f"bench-base-{round_number}.xml",
                               "e2e", workloads)
            self._write_report(
                reports / f"bench-head-{round_number}.xml", "e2e",
                [case for case in workloads
                 if case[0] != omit_renderer])

    @staticmethod
    def _install_harness(head, harness_edit=None):
        """Copy the real harness into the fake head checkout, optionally edited.

        The Compare block asks the harness for its workload list, so the fake
        checkout needs it — otherwise the block fails for the wrong reason and
        proves nothing. `harness_edit` receives the source and returns the
        modified source, which is how the controls below construct the states
        a retirement and a broken listing actually produce.
        """
        harness = head / "scripts" / "bench" / "e2e_bench.py"
        harness.parent.mkdir(parents=True, exist_ok=True)
        source = (REPO_ROOT / "scripts" / "bench" /
                  "e2e_bench.py").read_text(encoding="utf-8")
        if harness_edit is not None:
            source = harness_edit(source)
        harness.write_text(source, encoding="utf-8")
        return harness

    def _execute_compare(self, root, allowed_removals="",
                         harness_edit=None):
        head = root / "head"
        comparator = head / "scripts" / "ci" / "compare_durations.py"
        comparator.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / "scripts" / "ci" /
                        "compare_durations.py", comparator)
        self._install_harness(head, harness_edit)
        summary = root / "summary.md"
        env = {
            **os.environ,
            "BASE_TAG": "fixture-baseline",
            "GITHUB_STEP_SUMMARY": str(summary),
            "MAX_REGRESSION": "0.30",
            "ALLOWED_WORKLOAD_REMOVALS": allowed_removals,
        }
        completed = subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c",
             TestSpeedWorkflowRendererGate._run_block("Compare")],
            cwd=root, env=env, capture_output=True, text=True,
            check=False)
        return completed, summary

    @unittest.skipIf(
        sys.platform == "win32",
        "runs bash, which resolves to WSL bash.exe without an installed "
        "distribution on Windows; the block runs only on ubuntu runners in "
        "production, and the block-text assertions in "
        "test_baseline_renderer_rounds_share_head_outputs_and_selfcheck run "
        "on every OS",
    )
    def test_compare_runs_once_for_each_report_family(self):
        with tempfile.TemporaryDirectory(prefix="ghw-speed-compare-shape-") as td:
            root = Path(td)
            self._write_comparison_reports(root, 0.11, 0.21)
            completed, summary = self._execute_compare(root)

            self.assertEqual(completed.returncode, 0,
                             completed.stdout + completed.stderr)
            summary_text = summary.read_text(encoding="utf-8")
            self.assertIn("fixture-baseline`", summary_text)
            self.assertIn("fixture-baseline renderer workloads`",
                          summary_text)

    @unittest.skipIf(
        sys.platform == "win32",
        "runs bash, which resolves to WSL bash.exe without an installed "
        "distribution on Windows; the block runs only on ubuntu runners in "
        "production, and the block-text assertions in "
        "test_baseline_renderer_rounds_share_head_outputs_and_selfcheck run "
        "on every OS",
    )
    def test_unit_regression_still_runs_renderer_comparison(self):
        with tempfile.TemporaryDirectory(
                prefix="ghw-speed-compare-failure-shape-") as td:
            root = Path(td)
            self._write_comparison_reports(root, 0.20, 0.20)
            completed, summary = self._execute_compare(root)

            self.assertEqual(completed.returncode, 1)
            summary_text = summary.read_text(encoding="utf-8")
            self.assertIn("fixture-baseline`", summary_text)
            self.assertIn("fixture-baseline renderer workloads`",
                          summary_text)

    @unittest.skipIf(
        sys.platform == "win32",
        "runs bash, which resolves to WSL bash.exe without an installed "
        "distribution on Windows; the block runs only on ubuntu runners in "
        "production, and the block-text assertions in "
        "test_baseline_renderer_rounds_share_head_outputs_and_selfcheck run "
        "on every OS",
    )
    def test_missing_renderer_workload_fails_the_compare_step(self):
        """Issue #36: a shipped renderer that stops being measured is red."""
        with tempfile.TemporaryDirectory(
                prefix="ghw-speed-compare-missing-renderer-") as td:
            root = Path(td)
            self._write_comparison_reports(
                root, 0.11, 0.21, omit_renderer="bench.render-impact")
            completed, summary = self._execute_compare(root)

            self.assertNotEqual(completed.returncode, 0,
                                completed.stdout + completed.stderr)
            summary_text = summary.read_text(encoding="utf-8")
            self.assertIn("e2e::bench.render-impact", summary_text)

    @unittest.skipIf(
        sys.platform == "win32",
        "runs bash, which resolves to WSL bash.exe without an installed "
        "distribution on Windows; the block runs only on ubuntu runners in "
        "production, and the block-text assertions in "
        "test_baseline_renderer_rounds_share_head_outputs_and_selfcheck run "
        "on every OS",
    )
    def test_declared_workload_removal_passes_the_compare_step(self):
        """The exemption surface, for a workload the harness still lists.

        This is the only state ALLOWED_WORKLOAD_REMOVALS can actually reach:
        the harness keeps measuring the workload, so the report contains it,
        and the gate is told not to require it. A genuine retirement — a
        renderer actually deleted — does NOT come through here; see
        test_retiring_a_workload_from_workloads_needs_no_exemption.
        """
        with tempfile.TemporaryDirectory(
                prefix="ghw-speed-compare-allowed-removal-") as td:
            root = Path(td)
            self._write_comparison_reports(
                root, 0.11, 0.21, omit_renderer="bench.render-impact")
            completed, summary = self._execute_compare(
                root, allowed_removals="e2e::bench.render-impact")

            self.assertEqual(completed.returncode, 0,
                             completed.stdout + completed.stderr)
            self.assertIn("renderer workloads`",
                          summary.read_text(encoding="utf-8"))

    @unittest.skipIf(
        sys.platform == "win32",
        "runs bash, which resolves to WSL bash.exe without an installed "
        "distribution on Windows; the block runs only on ubuntu runners in "
        "production, and the block-text assertions in "
        "test_baseline_renderer_rounds_share_head_outputs_and_selfcheck run "
        "on every OS",
    )
    def test_retiring_a_workload_from_workloads_needs_no_exemption(self):
        """The real retirement path, which the variable's old comment got wrong.

        A renderer retired properly is dropped from WORKLOADS, so the harness
        measures neither side: it never appears in the baseline report either,
        the closed-set check has nothing unexplained to complain about, and
        ALLOWED_WORKLOAD_REMOVALS stays empty. That is what this asserts, so
        the exemption surface is not mistaken for the retirement procedure.
        """
        def retire_impact(source):
            return source.replace(
                '    ("render-impact", "render-impact.py", ("impact.svg",),\n'
                '     "impact-cache.json"),\n', "")

        with tempfile.TemporaryDirectory(
                prefix="ghw-speed-compare-retired-workload-") as td:
            root = Path(td)
            reports = root / "reports"
            remaining = [("bench.render", 0.21),
                         ("bench.render-responsiveness", 0.05)]
            for round_number in (1, 2):
                self._write_report(reports / f"base-{round_number}.xml",
                                   "unit", [("test_unit", 0.10)])
                self._write_report(reports / f"head-{round_number}.xml",
                                   "unit", [("test_unit", 0.11)])
                self._write_report(reports / f"bench-base-{round_number}.xml",
                                   "e2e", remaining)
                self._write_report(reports / f"bench-head-{round_number}.xml",
                                   "e2e", remaining)
            completed, summary = self._execute_compare(
                root, allowed_removals="", harness_edit=retire_impact)

            self.assertEqual(completed.returncode, 0,
                             completed.stdout + completed.stderr)
            self.assertIn("renderer workloads`",
                          summary.read_text(encoding="utf-8"))

    @unittest.skipIf(
        sys.platform == "win32",
        "runs bash, which resolves to WSL bash.exe without an installed "
        "distribution on Windows; the block runs only on ubuntu runners in "
        "production, and the block-text assertions in "
        "test_baseline_renderer_rounds_share_head_outputs_and_selfcheck run "
        "on every OS",
    )
    def test_workload_listing_that_fails_stops_the_compare_step(self):
        """A producer that dies must not silently empty the required list.

        bash -e cannot see a process substitution's exit status, so a broken
        listing used to leave no --require-test at all — which is issue 36's
        own false green, reached through a different door.
        """
        def break_flag(source):
            return source.replace('if "--list-workloads" in argv:',
                                  'if "--list-workloads-v2" in argv:')

        with tempfile.TemporaryDirectory(
                prefix="ghw-speed-compare-broken-listing-") as td:
            root = Path(td)
            self._write_comparison_reports(root, 0.11, 0.21)
            completed, _ = self._execute_compare(root, harness_edit=break_flag)

            self.assertNotEqual(completed.returncode, 0,
                                completed.stdout + completed.stderr)
            self.assertIn("workload listing", completed.stderr)

    @unittest.skipIf(
        sys.platform == "win32",
        "runs bash, which resolves to WSL bash.exe without an installed "
        "distribution on Windows; the block runs only on ubuntu runners in "
        "production, and the block-text assertions in "
        "test_baseline_renderer_rounds_share_head_outputs_and_selfcheck run "
        "on every OS",
    )
    def test_empty_workload_listing_stops_the_compare_step(self):
        """A producer that prints nothing is as disabling as one that dies."""
        def print_nothing(source):
            return source.replace("        print(workload_node_id(workload[0]))",
                                  "        pass  # deliberately empty listing")

        with tempfile.TemporaryDirectory(
                prefix="ghw-speed-compare-empty-listing-") as td:
            root = Path(td)
            self._write_comparison_reports(root, 0.11, 0.21)
            completed, _ = self._execute_compare(root, harness_edit=print_nothing)

            self.assertNotEqual(completed.returncode, 0,
                                completed.stdout + completed.stderr)
            self.assertIn("workload listing is empty", completed.stderr)

    @unittest.skipIf(
        sys.platform == "win32",
        "runs bash, which resolves to WSL bash.exe without an installed "
        "distribution on Windows; the block runs only on ubuntu runners in "
        "production, and the block-text assertions in "
        "test_baseline_renderer_rounds_share_head_outputs_and_selfcheck run "
        "on every OS",
    )
    def test_exempting_every_workload_stops_the_compare_step(self):
        """The other self-disable: naming all three empties the required list.

        Same false green as a broken listing, by configuration instead of by
        accident, and it used to be silent.
        """
        every = ",".join([
            "e2e::bench.render",
            "e2e::bench.render-impact",
            "e2e::bench.render-responsiveness",
        ])
        with tempfile.TemporaryDirectory(
                prefix="ghw-speed-compare-all-allowed-") as td:
            root = Path(td)
            self._write_comparison_reports(
                root, 0.11, 0.21, omit_renderer="bench.render-impact")
            completed, _ = self._execute_compare(
                root, allowed_removals=every)

            self.assertNotEqual(completed.returncode, 0,
                                completed.stdout + completed.stderr)
            self.assertIn("every workload is an allowed removal",
                          completed.stderr)

    def test_baseline_renderer_rounds_share_head_outputs_and_selfcheck(self):
        block = self._run_block("Run renderer workloads, interleaved")
        self.assertIn('--work-root "$RUNNER_TEMP/gh7-bench/$side"', block)
        self.assertIn('if [ "$side" = "head" ] && [ "$round" -eq '
                      '"$ROUNDS" ]; then', block)
        self.assertIn("selfcheck=(--selfcheck)", block)


if __name__ == "__main__":
    unittest.main()
