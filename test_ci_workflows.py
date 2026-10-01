"""Invariants over .github/workflows that no workflow run can check itself.

    python3 -m unittest discover -v

Stdlib unittest, matching the rest of this repo's suite.
"""
import contextlib
import importlib.util
import json
import os
import re
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
    """Drive the real step scripts out of speed.yml against a synthetic tree.

    The property being preserved is that the WORKFLOW's own shell decides
    things — which reports exist, which names are required, which refusals
    stop the step — so every test here runs the step body extracted from the
    YAML rather than a re-implementation of it. A test that reimplemented the
    comparison would keep passing after the workflow stopped calling it.

    Everything the tree feeds in is synthetic and offline: no checkout, no
    fixture build, no perf, no network. The instrument is injected through a
    fake `perf` on PATH, because the real runner cannot count instructions at
    all (perf_event_paranoid=4) and a test that depended on the host's perf
    would pass on this box and fail there.
    """

    workflow = WORKFLOWS / "speed.yml"

    UNIT_NODE = "counter::unit-suite"
    WORKLOADS = (("bench.render", 1000.0, 2.0),
                 ("bench.render-impact", 200.0, 1.0),
                 ("bench.render-responsiveness", 50.0, 0.5))
    # Deterministic stand-in for the collected unit-test population. The
    # digest is over the SET, so the fixture's exact membership does not
    # matter — only that it does not change under the test's feet.
    POPULATION = ["test_alpha.TestOne.test_a", "test_beta.TestTwo.test_b"]

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
    def _write_report(path, classname, cases, metric, wall=None):
        """One <testsuite> whose `time` is the COUNTER, not seconds."""
        path.parent.mkdir(parents=True, exist_ok=True)
        suite = ET.Element("testsuite", {
            "name": "fixture",
            "tests": str(len(cases)),
            "failures": "0",
            "errors": "0",
            "skipped": "0",
            "time": str(sum(value for _, value in cases)),
            "gh-metric": metric,
        })
        for name, value in cases:
            ET.SubElement(suite, "testcase", {
                "classname": classname,
                "name": name,
                "time": str(value),
                "gh-wall": str((wall or {}).get(name, value)),
            })
        ET.ElementTree(suite).write(path, encoding="utf-8",
                                    xml_declaration=True)

    def _install_counter(self, head):
        """The real counter, loaded by path, exactly as the workflow calls it."""
        path = head / "scripts" / "ci" / "counter.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / "scripts" / "ci" / "counter.py", path)
        spec = importlib.util.spec_from_file_location("ghw_counter_gate_test",
                                                      path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules["ghw_counter_gate_test"] = module
        spec.loader.exec_module(module)
        return module

    def _write_tree(self, root, unit_counter=10.0, workload_counter=None,
                    omit_renderer=None, metric="cpu_time", wall=None,
                    baseline=True, population=True, tolerance=0.10):
        """The tree speed.yml's Compare step expects, entirely synthetic."""
        head = root / "head"
        for relative in ("scripts/ci/compare_durations.py",
                         "scripts/bench/e2e_bench.py"):
            self._install(head, REPO_ROOT / relative)
        self._install_counter(head)
        reports = root / "reports"
        self._write_reports(reports, unit_counter, workload_counter,
                            omit_renderer, metric, wall)
        if population:
            (reports / "unit-population.txt").write_text(
                "\n".join(sorted(self.POPULATION)) + "\n", encoding="utf-8")
        if baseline:
            self._write_baseline(root, metric=metric, tolerance=tolerance,
                                 wall=wall, population=population)
        return head

    def _write_reports(self, reports, unit_counter, workload_counter,
                       omit_renderer, metric, wall):
        """Two rounds of both report families, as speed.yml produces them.

        `omit_renderer` drops one workload from the renderer reports, which is
        the shape issue #36 is about: a gate that keeps measuring one fewer
        shipped renderer and still reports a pass.
        """
        counts = workload_counter or {}
        cases = [(name, counts.get(name, value))
                 for name, value, _ in self.WORKLOADS
                 if name != omit_renderer]
        walls = wall or {name: seconds for name, _, seconds in self.WORKLOADS
                         if name != omit_renderer}
        unit_wall = walls.get("unit-suite", 5.0)
        for round_number in (1, 2):
            self._write_report(reports / f"unit-{round_number}.xml", "counter",
                               [("unit-suite", unit_counter)], metric,
                               {"unit-suite": unit_wall})
            self._write_report(reports / f"bench-head-{round_number}.xml",
                               "e2e", cases, metric, walls)

    @staticmethod
    def _install(head, source):
        destination = head / source.relative_to(REPO_ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        return destination

    def _write_baseline(self, root, metric="cpu_time", tolerance=0.10,
                        wall=None, population=True, overrides=None):
        counter = self._install_counter(root / "head")
        document = {
            "schema": 1,
            "basis": "fixture baseline for the workflow tests",
            "metric": metric,
            "cell": "ubuntu-latest / 3.13",
            "tolerance": tolerance,
            "measured_commit": "0" * 40,
            "measured_at": "2026-10-01T00:00:00Z",
            "population": (counter.population_digest(self.POPULATION)
                           if population else ""),
            "entries": {
                self.UNIT_NODE: 10.0,
                **{f"e2e::{name}": counter_value
                   for name, counter_value, _ in self.WORKLOADS},
            },
            "wall": wall or {},
        }
        if overrides:
            document.update(overrides)
        if not document["population"]:
            document["population"] = "0" * 64
        (root / "speed-baseline.json").write_text(
            json.dumps(document, indent=2), encoding="utf-8")
        return document

    def _execute_compare(self, root, allowed_removals="", env_extra=None):
        env = {
            **os.environ,
            "BASELINE": "speed-baseline.json",
            "GITHUB_STEP_SUMMARY": str(root / "summary.md"),
            "MAX_REGRESSION": "0.30",
            "ALLOWED_WORKLOAD_REMOVALS": allowed_removals,
            "GITHUB_OUTPUT": str(root / "output.txt"),
            "GITHUB_ENV": str(root / "env.txt"),
            **(env_extra or {}),
        }
        completed = subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c",
             TestSpeedWorkflowRendererGate._run_block("Compare")],
            cwd=root, env=env, capture_output=True, text=True,
            check=False)
        summary = root / "summary.md"
        return completed, (summary.read_text(encoding="utf-8")
                           if summary.is_file() else "")

    def _temp_root(self, prefix):
        """A directory that outlives this test but not the next one."""
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        return Path(stack.enter_context(
            tempfile.TemporaryDirectory(prefix=prefix)))

    def _run_with_tree(self, prefix, **tree):
        root = self._temp_root(prefix)
        self._write_tree(root, **tree)
        return root

    def _execute_probe(self, root, tracer_script):
        """Run the REAL probe step with a chosen `strace` first on PATH."""
        bindir = root / "fakebin"
        bindir.mkdir(exist_ok=True)
        tracer = bindir / "strace"
        tracer.write_text(tracer_script, encoding="utf-8")
        tracer.chmod(0o755)
        env = {
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "GITHUB_OUTPUT": str(root / "output.txt"),
            "GITHUB_ENV": str(root / "env.txt"),
        }
        completed = subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c",
             TestSpeedWorkflowRendererGate._run_block(
                 "Probe the counter instrument")],
            cwd=root, env=env, capture_output=True, text=True, check=False)
        return completed, (root / "output.txt").read_text(encoding="utf-8")

    WORKING_STRACE = ("#!/usr/bin/env python3\n"
                      "import sys\n"
                      "a = sys.argv[1:]\n"
                      "open(a[a.index('-o') + 1], 'w').write("
                      "'100.00    0.000010           1       870          89 "
                      "total\\n')\n")
    BROKEN_STRACE = ("#!/usr/bin/env python3\n"
                     "import sys\n"
                     "sys.stderr.write('strace: Operation not permitted\\n')\n"
                     "sys.exit(1)\n")

    # -- the probe -----------------------------------------------------------

    def test_probe_picks_syscalls_when_the_tracer_really_counts(self):
        root = self._run_with_tree("ghw-speed-probe-syscalls-")
        completed, output = self._execute_probe(root, self.WORKING_STRACE)

        self.assertEqual(completed.returncode, 0,
                         completed.stdout + completed.stderr)
        self.assertIn("metric=syscalls", output)

    def test_probe_falls_back_to_cpu_time_when_the_tracer_cannot_trace(self):
        # strace IS on PATH here and still traces nothing — the shape of a
        # cell whose kernel forbids it. A probe that only checked for the
        # binary would pick syscalls and every comparison downstream would be
        # nonsense, with nothing anywhere recording that it was.
        root = self._run_with_tree("ghw-speed-probe-cputime-")
        completed, output = self._execute_probe(root, self.BROKEN_STRACE)

        self.assertEqual(completed.returncode, 0,
                         completed.stdout + completed.stderr)
        self.assertIn("metric=cpu_time", output)
        self.assertNotIn("metric=syscalls", output)
        # The reader has to be able to judge the fallback, which means the
        # reason has to say what forbade it.
        self.assertIn("metric_reason=", output)
        self.assertIn("ptrace_scope=", output)

    # -- refusals ------------------------------------------------------------

    def test_metric_mismatch_is_exit_two_not_a_comparison(self):
        root = self._run_with_tree("ghw-speed-metric-mismatch-",
                                   baseline=True)
        # Re-measure the head on the other instrument; the baseline still
        # says cpu_time.
        self._write_report(root / "reports" / "unit-1.xml", "counter",
                           [("unit-suite", 10.0)], "syscalls",
                           {"unit-suite": 5.0})
        self._write_report(root / "reports" / "unit-2.xml", "counter",
                           [("unit-suite", 10.0)], "syscalls",
                           {"unit-suite": 5.0})
        completed, summary = self._execute_compare(root)

        self.assertEqual(completed.returncode, 1,
                         completed.stdout + completed.stderr)
        # Nothing was compared for the family whose instrument moved: the
        # refusal names both metrics rather than dividing one by the other.
        self.assertIn("COULD NOT COMPARE", summary)
        self.assertIn("metric mismatch", summary)
        self.assertIn("cpu_time", summary)
        self.assertIn("syscalls", summary)

    def test_a_different_collected_population_refuses(self):
        root = self._run_with_tree("ghw-speed-population-mismatch-")
        (root / "reports" / "unit-population.txt").write_text(
            "test_newly_added.TestThing.test_new\n", encoding="utf-8")
        completed, summary = self._execute_compare(root)

        self.assertEqual(completed.returncode, 1,
                         completed.stdout + completed.stderr)
        self.assertIn("different unit-test population", summary)
        self.assertIn("Re-derive the baseline", summary)

    def test_missing_baseline_exits_zero_with_the_measured_values(self):
        root = self._run_with_tree("ghw-speed-no-baseline-", baseline=False)
        completed, summary = self._execute_compare(root)

        self.assertEqual(completed.returncode, 0,
                         completed.stdout + completed.stderr)
        # A green gate with an empty summary is the defect this path exists
        # to avoid, so the measured counters have to be in it, pasteable.
        self.assertIn("No baseline to compare against", summary)
        self.assertIn(self.UNIT_NODE, summary)
        self.assertIn("cpu_time", summary)

    # -- verdicts ------------------------------------------------------------

    def test_a_counter_above_tolerance_is_red_and_one_below_is_green(self):
        over = self._run_with_tree("ghw-speed-over-tolerance-",
                                   unit_counter=12.0)
        completed, summary = self._execute_compare(over)
        self.assertEqual(completed.returncode, 1,
                         completed.stdout + completed.stderr)
        self.assertIn("down-only tolerance", completed.stderr)
        self.assertIn("past the down-only tolerance", summary)

        under = self._run_with_tree("ghw-speed-under-tolerance-",
                                    unit_counter=10.5)
        completed, summary = self._execute_compare(under)
        self.assertEqual(completed.returncode, 0,
                         completed.stdout + completed.stderr)
        self.assertIn("within budget", summary)

    def test_the_smoke_gate_fires_on_a_gross_outlier_without_a_number(self):
        # A wall figure at all is the thing being removed; the smoke verdict
        # is the one wall-derived output that survives, and it survives as a
        # verdict.
        # The baseline recorded five seconds; this run took six hundred. The
        # smoke gate is a cliff detector, not a measurement.
        root = self._run_with_tree("ghw-speed-smoke-", baseline=False,
                                   wall={"unit-suite": 600.0})
        # Keyed by node id, like every other map in the baseline.
        self._write_baseline(root, wall={self.UNIT_NODE: 5.0})
        completed, summary = self._execute_compare(root)

        self.assertEqual(completed.returncode, 1,
                         completed.stdout + completed.stderr)
        self.assertIn("SMOKE FAIL", summary)
        self.assertIn(self.UNIT_NODE, summary)
        # 600 is the wall seconds the fixture used. A verdict, no number.
        self.assertNotIn("600", summary)
        self.assertIn("No elapsed time is reported", summary)

    def test_the_baseline_ratchet_refuses_a_raised_entry_and_allows_a_lower(self):
        root = self._temp_root("ghw-speed-ratchet-")
        self._write_tree(root, baseline=True)
        comparator = root / "head" / "scripts" / "ci" / "compare_durations.py"

        def run(old_entries):
            old = root / "old.json"
            document = json.loads(
                (root / "speed-baseline.json").read_text(encoding="utf-8"))
            document["entries"] = old_entries
            old.write_text(json.dumps(document), encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(comparator),
                 "--ratchet-baselines", str(old),
                 str(root / "speed-baseline.json")],
                capture_output=True, text=True, check=False)

        current = {self.UNIT_NODE: 10.0, "e2e::bench.render": 1000.0,
                   "e2e::bench.render-impact": 200.0,
                   "e2e::bench.render-responsiveness": 50.0}

        raised = run({**current, "e2e::bench.render": 900.0})
        self.assertEqual(raised.returncode, 1, raised.stdout + raised.stderr)
        self.assertIn("went UP", raised.stderr)
        self.assertIn("e2e::bench.render", raised.stderr)

        lowered = run({**current, "e2e::bench.render": 1500.0})
        self.assertEqual(lowered.returncode, 0,
                         lowered.stdout + lowered.stderr)

    # -- the closed-set renderer gate, unchanged by any of the above ---------

    def test_compare_runs_once_for_each_report_family(self):
        root = self._run_with_tree("ghw-speed-compare-shape-")
        completed, summary = self._execute_compare(root)

        self.assertEqual(completed.returncode, 0,
                         completed.stdout + completed.stderr)
        self.assertIn("committed baseline (unit suite)", summary)
        self.assertIn("committed baseline (renderer workloads)", summary)

    def test_unit_regression_still_runs_renderer_comparison(self):
        root = self._run_with_tree("ghw-speed-compare-failure-shape-",
                                   unit_counter=20.0)
        completed, summary = self._execute_compare(root)

        self.assertEqual(completed.returncode, 1)
        self.assertIn("committed baseline (unit suite)", summary)
        self.assertIn("committed baseline (renderer workloads)", summary)

    def test_missing_renderer_workload_fails_the_compare_step(self):
        """Issue #36: a shipped renderer that stops being measured is red."""
        root = self._run_with_tree("ghw-speed-compare-missing-renderer-",
                                   omit_renderer="bench.render-impact")
        completed, summary = self._execute_compare(root)

        self.assertNotEqual(completed.returncode, 0,
                            completed.stdout + completed.stderr)
        self.assertIn("e2e::bench.render-impact", summary)

    def test_declared_workload_removal_passes_the_compare_step(self):
        """The exemption surface, for a workload the harness still lists.

        This is the only state ALLOWED_WORKLOAD_REMOVALS can actually reach:
        the harness keeps measuring the workload, so the report contains it,
        and the gate is told not to require it. A genuine retirement — a
        renderer actually deleted — does NOT come through here; see
        test_retiring_a_workload_from_workloads_needs_no_exemption.
        """
        root = self._run_with_tree("ghw-speed-compare-allowed-removal-",
                                   omit_renderer="bench.render-impact")
        completed, summary = self._execute_compare(
            root, allowed_removals="e2e::bench.render-impact")

        self.assertEqual(completed.returncode, 0,
                         completed.stdout + completed.stderr)
        self.assertIn("renderer workloads", summary)

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

        root = self._temp_root("ghw-speed-compare-retired-workload-")
        head = self._write_tree(root)
        harness = head / "scripts" / "bench" / "e2e_bench.py"
        harness.write_text(
            retire_impact(harness.read_text(encoding="utf-8")),
            encoding="utf-8")
        self._write_report(
            root / "reports" / "bench-head-1.xml", "e2e",
            [("bench.render", 1000.0), ("bench.render-responsiveness", 50.0)],
            "cpu_time")
        shutil.copyfile(root / "reports" / "bench-head-1.xml",
                        root / "reports" / "bench-head-2.xml")
        # A retired workload's baseline entry goes with it. That edit is
        # visible in the diff, and the ratchet step permits it precisely
        # because removing work is what it is for.
        document = json.loads((root / "speed-baseline.json").read_text(
            encoding="utf-8"))
        document["entries"].pop("e2e::bench.render-impact")
        (root / "speed-baseline.json").write_text(
            json.dumps(document, indent=2), encoding="utf-8")
        completed, summary = self._execute_compare(root)

        self.assertEqual(completed.returncode, 0,
                         completed.stdout + completed.stderr)
        self.assertIn("renderer workloads", summary)

    def test_workload_listing_that_fails_stops_the_compare_step(self):
        """A producer that dies must not silently empty the required list.

        bash -e cannot see a process substitution's exit status, so a broken
        listing used to leave no --require-test at all — which is issue 36's
        own false green, reached through a different door.
        """
        root = self._run_with_tree("ghw-speed-compare-broken-listing-")
        self._edit_harness(root, 'if "--list-workloads" in argv:',
                           'if "--list-workloads-v2" in argv:')
        completed, _ = self._execute_compare(root)

        self.assertNotEqual(completed.returncode, 0,
                            completed.stdout + completed.stderr)
        self.assertIn("workload listing", completed.stderr)

    def test_empty_workload_listing_stops_the_compare_step(self):
        """A producer that prints nothing is as disabling as one that dies."""
        root = self._run_with_tree("ghw-speed-compare-empty-listing-")
        self._edit_harness(root, "        print(workload_node_id(workload[0]))",
                           "        pass  # deliberately empty listing")
        completed, _ = self._execute_compare(root)

        self.assertNotEqual(completed.returncode, 0,
                            completed.stdout + completed.stderr)
        self.assertIn("workload listing is empty", completed.stderr)

    def test_exempting_every_workload_stops_the_compare_step(self):
        """The other self-disable: naming all three empties the required list.

        Same false green as a broken listing, by configuration instead of by
        accident, and it used to be silent.
        """
        root = self._run_with_tree("ghw-speed-compare-all-allowed-",
                                   omit_renderer="bench.render-impact")
        completed, _ = self._execute_compare(
            root, allowed_removals=",".join(
                f"e2e::{name}" for name, _, _ in self.WORKLOADS))

        self.assertNotEqual(completed.returncode, 0,
                            completed.stdout + completed.stderr)
        self.assertIn("every workload is an allowed removal",
                      completed.stderr)

    def _edit_harness(self, root, old, new):
        harness = root / "head" / "scripts" / "bench" / "e2e_bench.py"
        source = harness.read_text(encoding="utf-8")
        self.assertIn(old, source)
        harness.write_text(source.replace(old, new), encoding="utf-8")

    # -- the block text that no synthetic tree can execute -------------------

    def test_renderer_rounds_keep_the_head_side_and_the_selfcheck(self):
        block = self._run_block("Run renderer workloads")
        self.assertIn('--work-root "$RUNNER_TEMP/gh7-bench/head"', block)
        self.assertIn('if [ "$round" -eq "$ROUNDS" ]; then', block)
        self.assertIn("selfcheck=(--selfcheck)", block)
        # Head only: there is no base checkout any more. A leftover --side
        # loop would measure nothing, because there is nothing to measure it
        # against.
        self.assertIn("--side head", block)
        self.assertNotIn("--side base", block)

    def test_the_ratchet_step_judges_the_merge_base_on_a_pull_request(self):
        text = (WORKFLOWS / "speed.yml").read_text(encoding="utf-8")
        step_start = text.index("      - name: The committed baseline only "
                                "ratchets down")
        step = text[step_start:text.index("\n      - ", step_start + 10)]
        # A gate that judges the wrong base is worse than no gate: the base
        # SHA is the whole question, and a push to main has no base at all.
        self.assertIn("github.event_name == 'pull_request'", step)
        self.assertIn("github.event.pull_request.base.sha", step)
        self.assertIn("--ratchet-baselines", step)
        self.assertIn('$BASE_SHA:$BASELINE', step)

    def test_no_workflow_still_looks_up_a_release_for_the_speed_gate(self):
        # The whole point of issue #81: the comparison point is committed
        # data now, so a tag lookup here would be a leftover argument for a
        # method this gate no longer uses.
        text = (WORKFLOWS / "speed.yml").read_text(encoding="utf-8")
        self.assertNotIn("releases/latest", text)
        self.assertNotIn("--base-label \"$BASE_TAG\"", text)


if __name__ == "__main__":
    unittest.main()
