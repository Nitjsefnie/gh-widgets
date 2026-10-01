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
    # pylint: disable=too-many-public-methods
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
    # The two populations the workflow compares, and the instrument each is
    # held to. These names are the baseline's keys, so they are spelled the
    # same in speed.yml, here and in the document itself.
    UNIT_POPULATION = "unit-suite"
    RENDERER_POPULATION = "renderer-workloads"
    UNIT_METRIC = "cpu_time"
    RENDERER_METRIC = "cpu_time"
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
                    omit_renderer=None, metric=RENDERER_METRIC, wall=None,
                    baseline=True, population=True, tolerance=0.25,
                    unit_metric=UNIT_METRIC):
        """The tree speed.yml's Compare step expects, entirely synthetic."""
        head = root / "head"
        for relative in ("scripts/ci/compare_durations.py",
                         "scripts/bench/e2e_bench.py"):
            self._install(head, REPO_ROOT / relative)
        self._install_counter(head)
        reports = root / "reports"
        self._write_reports(reports, unit_counter, workload_counter,
                            omit_renderer, metric, wall, unit_metric)
        if population:
            (reports / "unit-population.txt").write_text(
                "\n".join(sorted(self.POPULATION)) + "\n", encoding="utf-8")
        if baseline:
            self._write_baseline(root, metric=metric, tolerance=tolerance,
                                 wall=wall, population=population,
                                 unit_metric=unit_metric)
        return head

    def _write_reports(self, reports, unit_counter, workload_counter,
                       omit_renderer, metric, wall, unit_metric=UNIT_METRIC):
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
            # Two populations, two instruments: the whole point of the
            # per-population schema, and the reason a document-wide `metric`
            # could not have worked.
            self._write_report(reports / f"unit-{round_number}.xml", "counter",
                               [("unit-suite", unit_counter)], unit_metric,
                               {"unit-suite": unit_wall})
            self._write_report(reports / f"bench-head-{round_number}.xml",
                               "e2e", cases, metric, walls)

    @staticmethod
    def _install(head, source):
        destination = head / source.relative_to(REPO_ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        return destination

    def _write_baseline(self, root, metric=RENDERER_METRIC, tolerance=0.25,
                        wall=None, population=True,
                        unit_metric=UNIT_METRIC, unit_tolerance=0.25,
                        unit_wall=None):
        """The committed document, with one sub-document per population."""
        counter = self._install_counter(root / "head")
        digest = counter.population_digest(self.POPULATION)
        workloads = {f"e2e::{name}": value
                     for name, value, _ in self.WORKLOADS}
        unit_walls = {self.UNIT_NODE: 5.0} if unit_wall is None else unit_wall
        document = {
            "schema": 2,
            "basis": "fixture baseline for the workflow tests",
            "cell": "ubuntu-latest / 3.13",
            "measured_commit": "0" * 40,
            "measured_at": "2026-10-01T00:00:00Z",
            "populations": {
                self.UNIT_POPULATION: {
                    "metric": unit_metric,
                    "tolerance": unit_tolerance,
                    "population": digest if population else "0" * 64,
                    "entries": {self.UNIT_NODE: 10.0},
                    "wall": unit_walls,
                },
                self.RENDERER_POPULATION: {
                    "metric": metric,
                    "tolerance": tolerance,
                    # The renderer population's digest is over the workload
                    # node ids, which the closed-set check already guards;
                    # this copy is for uniformity and for the record.
                    "population": counter.population_digest(
                        [f"e2e::{name}" for name, _, _ in self.WORKLOADS]),
                    "entries": workloads,
                    "wall": wall or {},
                },
            },
        }
        (root / "speed-baseline.json").write_text(
            json.dumps(document, indent=2), encoding="utf-8")
        return document

    def _execute_compare(self, root, allowed_removals="", env_extra=None):
        env = {
            **os.environ,
            "BASELINE": "speed-baseline.json",
            "GITHUB_STEP_SUMMARY": str(root / "summary.md"),
            "TOTAL_BUDGET": "0.30",
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

    def _execute_probe(self, root):
        """Run the REAL probe step out of the YAML and read what it exported."""
        env = {
            **os.environ,
            "GITHUB_OUTPUT": str(root / "output.txt"),
            "GITHUB_ENV": str(root / "env.txt"),
        }
        completed = subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c",
             TestSpeedWorkflowRendererGate._run_block(
                 "Probe the counter instrument")],
            cwd=root, env=env, capture_output=True, text=True, check=False)
        return completed, (root / "output.txt").read_text(encoding="utf-8")

    # -- the probe -----------------------------------------------------------

    def test_probe_records_the_instrument_and_the_kernel_settings(self):
        # There is one instrument, so the step chooses nothing. What it
        # exists for is the RECORD of why, and a reader who sees CPU seconds
        # has to be able to see that the deterministic alternatives were
        # measured rather than assumed away.
        root = self._run_with_tree("ghw-speed-probe-")
        completed, output = self._execute_probe(root)

        self.assertEqual(completed.returncode, 0,
                         completed.stdout + completed.stderr)
        self.assertIn("metric=cpu_time", output)
        self.assertIn("metric_reason=", output)
        self.assertIn("perf_event_paranoid=", output)
        self.assertIn("ptrace_scope=", output)

    def test_both_measurement_steps_pin_the_instrument(self):
        # Pinned rather than inherited: the instrument a population is judged
        # by should be visible in the workflow, not buried in a fallback
        # chain inside counter.py.
        for step in ("Measure the unit suite", "Run renderer workloads"):
            with self.subTest(step=step):
                self.assertIn("GH_COUNTER_METRIC: cpu_time",
                              self._run_block_env(step))

    @staticmethod
    def _run_block_env(step_name):
        """The `env:` mapping of one named step, as text."""
        lines = (WORKFLOWS / "speed.yml").read_text().splitlines()
        start = lines.index(f"      - name: {step_name}")
        block = []
        for line in lines[start:]:
            if line.startswith("      - ") and line is not lines[start]:
                break
            block.append(line)
        return "\n".join(block)

    def test_no_step_takes_its_instrument_from_the_probe_output(self):
        # The instrument is pinned literally in both measurement steps, so a
        # $GITHUB_ENV left behind by an older revision — exporting the
        # instrument this gate no longer has — cannot reach counter.py at
        # all. The probe step's output is still recorded in the job summary,
        # but nothing consumes it as a contract.
        text = (WORKFLOWS / "speed.yml").read_text(encoding="utf-8")
        self.assertNotIn("GH_COUNTER_METRIC: ${{", text)
        self.assertIn("id: probe", text)

    # -- the cwd defect (runs 36820664288, 36820703818, 36820741554) --------

    def test_the_unit_suite_is_measured_inside_the_checkout(self):
        """The step must name the directory discovery runs in.

        Three CI runs collected 724 node ids in the step BEFORE this one and
        then measured 0.05 CPU seconds of interpreter startup, because
        discovery ran from the parent of the checkout, found nothing, and
        exited 5. The counter reported exactly what it was asked for; the
        question was the wrong one. `--cwd` makes the directory part of the
        command rather than something the shell's working directory decides.
        """
        block = self._run_block("Measure the unit suite")
        self.assertIn('--cwd "$GITHUB_WORKSPACE/head"', block)
        self.assertIn("--name 'counter::unit-suite'", block)
        # And the reports are written outside the checkout, so that naming
        # the cwd can never also move where this step's own output lands.
        self.assertIn('--junit-file "$GITHUB_WORKSPACE/reports/unit-$round.xml"',
                      block)

    def test_a_measurement_runs_in_the_directory_it_was_given(self):
        """counter.py's half of the cwd fix, at the tool rather than the YAML.

        The assertion above is about the workflow's text. This is about the
        tool's behaviour, so that the next way of getting the directory
        wrong — a stale flag, a missing one, a wrapper shell — fails a test
        rather than a CI run. The two measurements differ only in `cwd`, and
        only one of them can see the marker file.
        """
        counter = self._install_counter(
            self._temp_root("ghw-counter-cwd-") / "head")
        listing = "import os; print(sorted(os.listdir('.')))"
        with tempfile.TemporaryDirectory(prefix="ghw-cwd-probe-") as td:
            root = Path(td)
            (root / "marker-only-here.txt").write_text("x", encoding="utf-8")
            inside = counter.measure([sys.executable, "-c", listing],
                                     cwd=root,
                                     metric=counter.CPU_METRIC)
            outside = counter.measure(
                [sys.executable, "-c", listing],
                metric=counter.CPU_METRIC)
        self.assertIn("marker-only-here.txt", inside.stdout)
        self.assertNotIn("marker-only-here.txt", outside.stdout)

    # -- the missing baseline is an expected outcome, not a traceback -------

    def test_a_missing_baseline_prints_no_traceback(self):
        """Two stack traces ahead of every green first run train readers to
        scroll past the red ones."""
        root = self._run_with_tree("ghw-speed-missing-baseline-",
                                   baseline=False)
        completed, summary = self._execute_compare(root)

        self.assertEqual(completed.returncode, 0,
                         completed.stdout + completed.stderr)
        for stream in (completed.stdout, completed.stderr):
            self.assertNotIn("Traceback", stream)
            self.assertNotIn("MissingBaseline", stream)
        self.assertIn("No baseline to compare against", summary)

    def test_a_missing_baseline_with_a_failed_suite_is_still_clean(self):
        """The combination that produced it: no baseline AND a suite that did
        not pass. The failure is about the head, so it is reported as one —
        exit 2 with a message — rather than escaping the handler as a
        traceback."""
        root = self._run_with_tree("ghw-speed-missing-and-failed-",
                                   baseline=False)
        for round_number in (1, 2):
            suite = ET.Element("testsuite", {
                "name": "counter", "tests": "1", "failures": "1",
                "errors": "0", "skipped": "0", "time": "0.05",
                "gh-metric": "cpu_time"})
            case = ET.SubElement(suite, "testcase", {
                "classname": "counter", "name": "unit-suite",
                "time": "0.05", "gh-wall": "0.6"})
            ET.SubElement(case, "failure", {
                "message": "command exited with code 5",
                "type": "CommandFailure"})
            ET.ElementTree(suite).write(
                root / "reports" / f"unit-{round_number}.xml",
                encoding="utf-8", xml_declaration=True)
        completed, summary = self._execute_compare(root)

        self.assertEqual(completed.returncode, 1,
                         completed.stdout + completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)
        self.assertIn("COULD NOT COMPARE", summary)
        self.assertIn("no baseline to compare against", summary)

    def test_the_total_budget_is_not_named_like_the_threshold(self):
        """`tolerance` in the baseline is the gate. An env var reading as
        "the maximum" would be read as the threshold and is not."""
        text = (WORKFLOWS / "speed.yml").read_text(encoding="utf-8")
        # As an env key it is gone; the name survives in the comment that
        # explains why it was renamed, which is the point of renaming it.
        self.assertNotIn("      MAX_REGRESSION:", text)
        self.assertIn('TOTAL_BUDGET: "0.30"', text)
        self.assertIn("NOT THE THRESHOLD", text)

    # -- refusals ------------------------------------------------------------

    def test_a_population_measured_under_another_instrument_refuses(self):
        # The instrument is one now, so this cannot happen by accident today.
        # It is here for the next instrument, and it is why every baseline
        # entry carries its metric name rather than leaving it to a reader's
        # memory: a count measured under one instrument and compared against
        # a baseline recorded under another is a category error that reads
        # as a spectacular speedup or a catastrophic regression, and neither
        # reading would be true.
        root = self._run_with_tree("ghw-speed-metric-mismatch-")
        for round_number in (1, 2):
            self._write_report(
                root / "reports" / f"bench-head-{round_number}.xml", "e2e",
                [(name, value) for name, value, _ in self.WORKLOADS],
                "syscalls")
        completed, summary = self._execute_compare(root)

        self.assertEqual(completed.returncode, 1,
                         completed.stdout + completed.stderr)
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
        # 12.6 against a 10.0 baseline is +26%: inside the 30% total budget,
        # outside the unit population's own 25% tolerance. That separation is
        # the point — the per-entry ratchet is not the total restated.
        over = self._run_with_tree("ghw-speed-over-tolerance-",
                                   unit_counter=12.6)
        completed, summary = self._execute_compare(over)
        self.assertEqual(completed.returncode, 1,
                         completed.stdout + completed.stderr)
        self.assertIn("down-only tolerance", completed.stderr)
        self.assertIn("past the down-only tolerance", summary)
        self.assertIn("within budget", summary)

        under = self._run_with_tree("ghw-speed-under-tolerance-",
                                    unit_counter=11.0)
        completed, summary = self._execute_compare(under)
        self.assertEqual(completed.returncode, 0,
                         completed.stdout + completed.stderr)
        self.assertIn("within budget", summary)
        self.assertNotIn("down-only tolerance", summary)

    def test_the_smoke_gate_fires_on_a_gross_outlier_without_a_number(self):
        # A wall figure at all is the thing being removed; the smoke verdict
        # is the one wall-derived output that survives, and it survives as a
        # verdict.
        # The baseline recorded five seconds; this run took six hundred. The
        # smoke gate is a cliff detector, not a measurement.
        root = self._run_with_tree("ghw-speed-smoke-", baseline=False,
                                   wall={"unit-suite": 600.0})
        # Keyed by node id, like every other map in the baseline, and in the
        # population that actually measured it.
        self._write_baseline(root, unit_wall={self.UNIT_NODE: 5.0})
        completed, summary = self._execute_compare(root)

        self.assertEqual(completed.returncode, 1,
                         completed.stdout + completed.stderr)
        self.assertIn("SMOKE FAIL", summary)
        self.assertIn(self.UNIT_NODE, summary)
        # 600 is the wall seconds the fixture used. A verdict, no number.
        self.assertNotIn("600", summary)
        self.assertIn("Elapsed time is not reported", summary)

    def test_the_baseline_ratchet_refuses_a_raised_entry_and_allows_a_lower(self):
        root = self._temp_root("ghw-speed-ratchet-")
        self._write_tree(root, baseline=True)
        comparator = root / "head" / "scripts" / "ci" / "compare_durations.py"
        target = root / "speed-baseline.json"

        def run(old_render_entries):
            """The merge base's copy of the baseline, one entry moved."""
            document = json.loads(target.read_text(encoding="utf-8"))
            document["populations"][self.RENDERER_POPULATION]["entries"] = \
                old_render_entries
            old = root / "old.json"
            old.write_text(json.dumps(document), encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(comparator),
                 "--ratchet-baselines", str(old), str(target)],
                capture_output=True, text=True, check=False)

        current = {"e2e::bench.render": 1000.0,
                   "e2e::bench.render-impact": 200.0,
                   "e2e::bench.render-responsiveness": 50.0}

        # The ceiling is raised in the same push that is measured against it.
        raised = run({**current, "e2e::bench.render": 900.0})
        self.assertEqual(raised.returncode, 1, raised.stdout + raised.stderr)
        self.assertIn("went UP", raised.stderr)
        self.assertIn("e2e::bench.render", raised.stderr)
        self.assertIn(self.RENDERER_POPULATION, raised.stderr)

        # Lowering is what the ratchet is for, and is always allowed.
        lowered = run({**current, "e2e::bench.render": 1500.0})
        self.assertEqual(lowered.returncode, 0,
                         lowered.stdout + lowered.stderr)

        # A population the merge base did not have at all is a raise by
        # definition, not a silent addition nobody reviewed.
        added = subprocess.run(
            [sys.executable, str(comparator), "--ratchet-baselines",
             str(root / "old.json"), str(target)],
            capture_output=True, text=True, check=False)
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)

    # -- the closed-set renderer gate, unchanged by any of the above ---------

    def test_compare_runs_once_for_each_report_family(self):
        root = self._run_with_tree("ghw-speed-compare-shape-")
        completed, summary = self._execute_compare(root)

        self.assertEqual(completed.returncode, 0,
                         completed.stdout + completed.stderr)
        self.assertIn("committed baseline, unit suite", summary)
        self.assertIn("committed baseline, renderer workloads", summary)

    def test_each_population_is_judged_by_its_own_metric(self):
        """The collision a single document-wide `metric` could not express.

        The renderer population is measured in syscalls and the unit suite in
        CPU seconds, from one document. If either comparison were reading the
        other's contract it would refuse outright rather than divide one by
        the other — which is what this asserts by watching both halves pass
        at once with two different instruments in play.
        """
        root = self._run_with_tree("ghw-speed-two-instruments-")
        self.assertIn(f"Metric: **{self.RENDERER_METRIC}**",
                      self._execute_compare(root)[1])
        self.assertIn(f"Metric: **{self.UNIT_METRIC}**",
                      self._execute_compare(root)[1])

    def test_a_baseline_without_a_population_is_refused(self):
        # One document carries several contracts. Guessing which one this run
        # is being held to is the accommodation the gate exists to refuse.
        root = self._run_with_tree("ghw-speed-no-population-")
        # The step's own block with the flag removed, so what is under test is
        # the comparator's refusal and not a shell quoting accident.
        block = "".join(
            line for line in self._run_block("Compare").splitlines(True)
            if "--population unit-suite" not in line)
        block = block.replace("--population renderer-workloads \\\n", "")
        env = {
            **os.environ,
            "BASELINE": "speed-baseline.json",
            "GITHUB_STEP_SUMMARY": str(root / "summary.md"),
            "TOTAL_BUDGET": "0.30",
            "ALLOWED_WORKLOAD_REMOVALS": "",
        }
        completed = subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", block],
            cwd=root, env=env, capture_output=True, text=True, check=False)
        self.assertNotEqual(completed.returncode, 0,
                            completed.stdout + completed.stderr)
        self.assertIn("needs --population", completed.stderr)

    def test_unit_regression_still_runs_renderer_comparison(self):
        root = self._run_with_tree("ghw-speed-compare-failure-shape-",
                                   unit_counter=20.0)
        completed, summary = self._execute_compare(root)

        self.assertEqual(completed.returncode, 1)
        self.assertIn("committed baseline, unit suite", summary)
        self.assertIn("committed baseline, renderer workloads", summary)

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
            self.RENDERER_METRIC)
        shutil.copyfile(root / "reports" / "bench-head-1.xml",
                        root / "reports" / "bench-head-2.xml")
        # A retired workload's baseline entry goes with it. That edit is
        # visible in the diff, and the ratchet step permits it precisely
        # because removing work is what it is for.
        document = json.loads((root / "speed-baseline.json").read_text(
            encoding="utf-8"))
        document["populations"]["renderer-workloads"]["entries"].pop(
            "e2e::bench.render-impact")
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
