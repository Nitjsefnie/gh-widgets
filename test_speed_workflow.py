"""Invariants over speed.yml, driven by executing its own step scripts.

    python3 -m unittest discover -v

Not assertions about the workflow's TEXT: each control runs a step's `run:`
body the way the job runs it — merged environment, `working-directory` as cwd
— against a synthetic tree whose only repository is at `head/`.

That distinction is not pedantry. Three defects in this branch were all one
path resolved against the wrong root, and all three shipped because a text
assertion cannot fail on them: the unit suite measured 0.05 CPU seconds
because discovery ran one level above the checkout; the ratchet step could
not execute for the same reason; and `--baseline` named a
repository-relative file that a workspace-rooted step resolved against the
workspace, so four dispatch runs reported success while a baseline sat
unread in the tree. Each of those is now a test that EXECUTES the step.
"""
import contextlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import xml.etree.ElementTree as ET


from speed_workflow_steps import WORKFLOWS, StepRunner  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent


from bench_platform import REQUIRES_BENCH  # noqa: E402


def envelope(value, samples=6):
    """A baseline entry as an OBSERVED RANGE, not a single number.

    The committed baseline records what each entry was seen over — min, max
    and how many observations — because a single stored value plus a budget
    wide enough to cover a 33-49% spread would need a tolerance above 1.0,
    which is a gate that cannot fire. Tests that want a head value to land
    exactly on the ceiling build the envelope around that value.
    """
    return {"min": round(value * 0.8, 6), "max": value, "n": samples}


class TestSpeedWorkflowRendererGate(unittest.TestCase):
    """The workflow's own shell decides things, so every case runs a step.

    Synthetic and offline throughout: no checkout, no fixture build, no
    network, and the instrument comes from `metric=` rather than from
    whatever kernel the test lands on.
    """

    # pylint: disable=too-many-public-methods
    steps = StepRunner()

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
                         "scripts/ci/counter.py",
                         "scripts/ci/baseline.py",
                         "scripts/bench/e2e_bench.py"):
            self._install(head, REPO_ROOT / relative)
        self._install_counter(head)
        reports = root / "reports"          # == $REPORTS, outside the checkout
        self._write_reports(reports, unit_counter, workload_counter,
                            omit_renderer, metric, wall, unit_metric)
        if population:
            (reports / "unit-population.txt").write_text(
                "\n".join(sorted(self.POPULATION)) + "\n", encoding="utf-8")
        self._write_workload_population(root)
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

    def _write_workload_population(self, root):
        """`reports/renderer-population.txt`, from the INSTALLED harness.

        The workflow writes this from `--list-workloads`, so a test that
        wrote the three names itself would be checking the fixture rather
        than the harness — and a workload retired from WORKLOADS would leave
        a stale id in a file the comparator then refuses.
        """
        harness = root / "head" / "scripts" / "bench" / "e2e_bench.py"
        listing = subprocess.run([sys.executable, str(harness),
                                  "--list-workloads"],
                                 capture_output=True, text=True, check=True)
        target = root / "reports" / "renderer-population.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(listing.stdout, encoding="utf-8")

    def _ratchet_commit(self, checkout, message, push=False):
        """Commit inside a checkout built by `_ratchet_checkout`."""
        git = ["git", "-C", str(checkout)]
        for args in (["config", "user.email", "bench@example.invalid"],
                     ["config", "user.name", "bench"]):
            subprocess.run(git + args, check=True)
        subprocess.run(git + ["add", "speed-baseline.json",
                              "scripts/ci/compare_durations.py",
                              "scripts/ci/counter.py",
                              "scripts/ci/baseline.py",
                              "scripts/bench/e2e_bench.py"], check=True)
        subprocess.run(git + ["commit", "-qm", message], check=True)
        if push:
            subprocess.run(git + ["push", "-q", "origin",
                                  "HEAD:refs/heads/main"], check=True)
        return git

    def _rederive_renderer_population(self, root):
        """Re-derive the renderer population digest from the edited harness."""
        counter = self._install_counter(root / "head")
        listing = subprocess.run(
            [sys.executable, str(root / "head" / "scripts" / "bench" /
                                 "e2e_bench.py"), "--list-workloads"],
            capture_output=True, text=True, check=True).stdout.split()
        path = root / "head" / "speed-baseline.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["populations"][self.RENDERER_POPULATION]["population"] = \
            counter.population_digest(listing)
        path.write_text(json.dumps(document, indent=2), encoding="utf-8")

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
        workloads = {f"e2e::{name}": envelope(value)
                     for name, value, _ in self.WORKLOADS}
        unit_walls = ({self.UNIT_NODE: envelope(5.0)}
                      if unit_wall is None else unit_wall)
        document = {
            "schema": 3,
            "basis": "fixture baseline for the workflow tests",
            "cell": "ubuntu-latest / 3.13",
            "measured_commit": "0" * 40,
            "measured_at": "2026-10-01T00:00:00Z",
            "populations": {
                self.UNIT_POPULATION: {
                    "metric": unit_metric,
                    "tolerance": unit_tolerance,
                    "population": digest if population else "0" * 64,
                    "entries": {self.UNIT_NODE: envelope(10.0)},
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
                    "wall": ({name: envelope(seconds, samples=3)
                              for name, seconds in wall.items()}
                             if wall else {}),
                },
            },
        }
        # Inside the checkout, which is where the job's convention puts every
        # repository file. It used to be written one level up, beside `head/`,
        # which is exactly the mistake the workflow just made.
        (root / "head" / "speed-baseline.json").write_text(
            json.dumps(document, indent=2), encoding="utf-8")
        return document

    def _execute_compare(self, root, allowed_removals="", env_extra=None,
                         block=None):
        """The Compare step, run the way the job runs it.

        Merged job+step env, and `working-directory` applied as the cwd — so a
        repository-relative path resolves INSIDE the checkout, and a regression
        that moves a path out of it fails here instead of on a dispatch.
        """
        declared, working_dir, step_block = self.steps.step_run("Compare")
        env = self.steps.env_for(
            declared, root,
            ALLOWED_WORKLOAD_REMOVALS=allowed_removals, **(env_extra or {}))
        completed = self.steps.bash(block or step_block, root, working_dir, env)
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
        """The REAL probe step, run the way the job runs it."""
        declared, working_dir, block = self.steps.step_run(
            "Probe the counter instrument")
        completed = self.steps.bash(block, root, working_dir,
                                    self.steps.env_for(declared, root))
        return completed, (root / "output.txt").read_text(encoding="utf-8")

    # -- the probe -----------------------------------------------------------

    @REQUIRES_BENCH
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
                self.assertEqual(self.steps.step_run(step)[0]
                                 ["GH_COUNTER_METRIC"], "cpu_time")

    def _edit_harness(self, root, old, new):
        harness = root / "head" / "scripts" / "bench" / "e2e_bench.py"
        source = harness.read_text(encoding="utf-8")
        self.assertIn(old, source)
        harness.write_text(source.replace(old, new), encoding="utf-8")

    # -- refusals ------------------------------------------------------------

    @REQUIRES_BENCH
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

    @REQUIRES_BENCH
    def test_a_different_collected_population_refuses(self):
        root = self._run_with_tree("ghw-speed-population-mismatch-")
        (root / "reports" / "unit-population.txt").write_text(
            "test_newly_added.TestThing.test_new\n", encoding="utf-8")
        completed, summary = self._execute_compare(root)

        self.assertEqual(completed.returncode, 1,
                         completed.stdout + completed.stderr)
        self.assertIn("different unit suite", summary)
        self.assertIn("Re-derive the baseline", summary)

    @REQUIRES_BENCH
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

    @REQUIRES_BENCH
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

    @REQUIRES_BENCH
    def test_the_smoke_gate_fires_on_a_gross_outlier_without_a_number(self):
        # Armed, and armed on an ENVELOPE: the head has to exceed the
        # recorded wall MAXIMUM times the factor, so the bound is a multiple
        # of the worst wall actually observed rather than of an arbitrary
        # pick from inside the range.
        # A wall figure at all is the thing being removed; the smoke verdict
        # is the one wall-derived output that survives, and it survives as a
        # verdict.
        # The baseline recorded five seconds; this run took six hundred. The
        # smoke gate is a cliff detector, not a measurement.
        root = self._run_with_tree("ghw-speed-smoke-", baseline=False,
                                   wall={"unit-suite": 600.0})
        # Keyed by node id and shaped as an envelope, like every other map
        # in the baseline, in the population that actually measured it.
        self._write_baseline(root, unit_wall={self.UNIT_NODE: envelope(5.0)})
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
        target = root / "head" / "speed-baseline.json"

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

        current = {f"e2e::{name}": envelope(value)
                   for name, value, _ in self.WORKLOADS}

        # The ceiling is raised in the same push that is measured against
        # it: the merge base recorded 800 and the head records 1200.
        raised = run({**current,
                      "e2e::bench.render": envelope(800.0)})
        self.assertEqual(raised.returncode, 1, raised.stdout + raised.stderr)
        self.assertIn("went UP", raised.stderr)
        self.assertIn("e2e::bench.render", raised.stderr)
        self.assertIn(self.RENDERER_POPULATION, raised.stderr)

        # Lowering the recorded maximum is what the ratchet is for.
        lowered = run({**current,
                       "e2e::bench.render": envelope(1200.0)})
        self.assertEqual(lowered.returncode, 0,
                         lowered.stdout + lowered.stderr)

    @REQUIRES_BENCH
    def test_an_envelope_from_one_observation_is_refused(self):
        """A single sample's maximum is a measurement, not a worst case.

        Refused rather than marked: a file that says PROVISIONAL in a key
        nobody reads reads as authoritative.
        """
        root = self._run_with_tree("ghw-speed-envelope-n1-")
        document = json.loads((root / "head" / "speed-baseline.json").read_text(
            encoding="utf-8"))
        document["populations"][self.UNIT_POPULATION]["entries"][
            self.UNIT_NODE]["n"] = 1
        (root / "head" / "speed-baseline.json").write_text(
            json.dumps(document, indent=2), encoding="utf-8")
        completed, summary = self._execute_compare(root)

        self.assertEqual(completed.returncode, 1,
                         completed.stdout + completed.stderr)
        self.assertIn("at least 2", summary)
        self.assertIn("COULD NOT COMPARE", summary)

    @REQUIRES_BENCH
    def test_an_entry_with_min_above_max_is_refused(self):
        root = self._run_with_tree("ghw-speed-envelope-inverted-")
        document = json.loads((root / "head" / "speed-baseline.json").read_text(
            encoding="utf-8"))
        document["populations"][self.UNIT_POPULATION]["entries"][
            self.UNIT_NODE] = {"min": 20.0, "max": 10.0, "n": 6}
        (root / "head" / "speed-baseline.json").write_text(
            json.dumps(document, indent=2), encoding="utf-8")
        completed, summary = self._execute_compare(root)

        self.assertEqual(completed.returncode, 1,
                         completed.stdout + completed.stderr)
        self.assertIn("not a range", summary)

    def test_the_gate_is_measured_against_the_recorded_maximum(self):
        """The head clears `max x (1 + tolerance)`, not `min x (...)`.

        This is the whole point of the envelope: the observed spread is
        already inside the recorded maximum, so a budget over the middle of
        the range would fire on noise and a budget over a single stored value
        would need a tolerance above 1.0.
        """
        root = self._run_with_tree("ghw-speed-envelope-ceiling-",
                                   unit_counter=12.5)
        # max 10.0 x 1.25 = 12.5 exactly, so this is the boundary.
        completed, summary = self._execute_compare(root)
        self.assertEqual(completed.returncode, 0,
                         completed.stdout + completed.stderr)
        # The report says what the gate is, every time, so nobody has to
        # infer it from a threshold.
        self.assertIn("step-change detector", summary.lower())

        above = self._run_with_tree("ghw-speed-envelope-over-",
                                    unit_counter=12.6)
        completed, summary = self._execute_compare(above)
        self.assertEqual(completed.returncode, 1,
                         completed.stdout + completed.stderr)
        self.assertIn("past the down-only tolerance", summary)

    # -- the closed-set renderer gate, unchanged by any of the above ---------

    @REQUIRES_BENCH
    def test_compare_runs_once_for_each_report_family(self):
        root = self._run_with_tree("ghw-speed-compare-shape-")
        completed, summary = self._execute_compare(root)

        self.assertEqual(completed.returncode, 0,
                         completed.stdout + completed.stderr)
        self.assertIn("committed baseline, unit suite", summary)
        self.assertIn("committed baseline, renderer workloads", summary)

    @REQUIRES_BENCH
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

    def _small_checkout(self, root):
        """A workspace whose only repository is a checkout at head/.

        The SHAPE the job produces — repository at `head/`, discovery run
        over it, reports written outside it — with the smallest contents that
        shape can hold: the scripts the step invokes and one trivial test
        module. What this control is about is WHERE the step writes, and the
        size of the population does not bear on that. Running the whole suite
        here instead would have made the control the most expensive thing in
        the suite, on a box where a leaked run can outlive its own test.
        """
        head = root / "head"
        head.mkdir(parents=True, exist_ok=True)
        shutil.copytree(REPO_ROOT / "scripts", head / "scripts",
                        copy_function=shutil.copy2)
        (head / "test_one.py").write_text(
            "import unittest\n\n\n"
            "class TestOne(unittest.TestCase):\n"
            "    def test_a(self):\n"
            "        self.assertTrue(True)\n", encoding="utf-8")
        return head

    # Every step that runs something, by name. A step added to speed.yml must
    # be added here too, or this enumeration silently stops covering the file.
    REPOSITORY_STEPS = (
        "Probe the counter instrument",
        "Build renderer fixtures",
        "Measure the unit suite",
        "Collect the unit-test population",
        "Run renderer workloads",
        "Compare",
        "The committed baseline only ratchets down",
    )

    def _steps(self):
        """(name, declares-working-directory, body) for every step."""
        lines = (WORKFLOWS / "speed.yml").read_text().splitlines()
        steps = []
        name = None
        working_dir = False
        body = []
        for line in lines:
            if line.startswith("      - name: "):
                if name:
                    steps.append((name, working_dir, "\n".join(body)))
                name = line.split(": ", 1)[1]
                working_dir = False
                body = []
                continue
            if line.startswith("      - ") and name:
                steps.append((name, working_dir, "\n".join(body)))
                name = None
                continue
            if name is None:
                continue
            if line.startswith("        working-directory:"):
                working_dir = True
            if line.startswith("        run:") or body:
                body.append(line)
        if name:
            steps.append((name, working_dir, "\n".join(body)))
        return steps

    def test_every_repository_step_declares_the_working_directory(self):
        """Every repository-touching step, checked by NAME.

        Four defects on this branch were a path resolved against the wrong
        root, and this control is the one that has to catch the next. An
        earlier version classified steps by the literal substring
        ``python3 scripts/``, which a reviewer's mutation defeated: removing a
        step's `working-directory` AND rewriting its invocation as
        `python3 ./scripts/...` left it green, because the substring no longer
        matched and the count floor still held. A predicate that can be
        defeated by rewriting the invocation is not a predicate.

        So the SET is compared against a named list, and the list is checked
        for completeness: a step that stops running, or starts running without
        being here, is itself a failure rather than a silent skip.
        """
        steps = dict((n, (w, b)) for n, w, b in self._steps())
        running = {n for n, (w, b) in steps.items() if "run:" in b}
        self.assertEqual(running, set(self.REPOSITORY_STEPS),
                         "speed.yml's running steps and REPOSITORY_STEPS have "
                         f"drifted: only in the file {sorted(running - set(self.REPOSITORY_STEPS))}, "
                         f"only in the list {sorted(set(self.REPOSITORY_STEPS) - running)}. "
                         "A step added or removed must be reflected here or "
                         "this enumeration stops covering the file.")
        for name in self.REPOSITORY_STEPS:
            with self.subTest(step=name):
                self.assertTrue(
                    steps[name][0],
                    f"{name!r} runs repository code and does not declare "
                    "working-directory: head, so its paths resolve against "
                    "the workspace one level above the checkout")

    @REQUIRES_BENCH
    def test_the_unit_suite_step_WRITES_where_the_next_step_READS(self):
        """The step executed, and its output proved to land where it must.

        The push that introduced the path convention failed here: the step
        declared the convention's `working-directory` on five steps and left
        it off the sixth, so discovery ran one level above the checkout, found
        no tests, and the counter measured interpreter startup. Text could not
        have caught it — only running the step and finding its JUnit where the
        next step looks for it.
        """
        root = self._temp_root("ghw-speed-unit-step-exec-")
        self._small_checkout(root)
        declared, working_dir, block = self.steps.step_run(
            "Measure the unit suite")
        self.assertEqual(working_dir, "head")
        env = self.steps.env_for(declared, root)
        env.update({"GH_COUNTER_METRIC": "cpu_time", "ROUNDS": "1"})
        reports = Path(env["REPORTS"])
        completed = self.steps.bash(block, root, working_dir, env)
        self.assertEqual(completed.returncode, 0,
                         completed.stdout[-2000:] + completed.stderr[-2000:])
        written = sorted(p.name for p in reports.glob("unit-*.xml"))
        self.assertEqual(written, ["unit-1.xml"],
                         "the step must write its JUnit into $REPORTS, where "
                         "the Compare step looks for it")

    @REQUIRES_BENCH
    def test_the_unit_suite_step_would_write_nothing_without_its_directory(self):
        """The mutation, proven.

        The same step run from the workspace cannot even find the counter, and
        reports nothing. A control that only proves it works has not proved
        it can fail.
        """
        root = self._temp_root("ghw-speed-unit-step-mutation-")
        self._small_checkout(root)
        declared, working_dir, block = self.steps.step_run(
            "Measure the unit suite")
        self.assertEqual(working_dir, "head")
        env = self.steps.env_for(declared, root)
        env.update({"GH_COUNTER_METRIC": "cpu_time", "ROUNDS": "1"})
        broken = self.steps.bash(block, root, None, env)
        self.assertNotEqual(broken.returncode, 0)
        written = sorted(p.name for p in Path(env["REPORTS"]).glob("unit-*.xml"))
        self.assertEqual(written, [])

    @REQUIRES_BENCH
    def test_the_compare_step_FINDS_the_baseline_in_the_checkout(self):
        """The defect that made four dispatch runs green for the wrong reason.

        `--baseline "$BASELINE"` names a REPOSITORY-relative file. A step
        rooted at the workspace resolves it against the workspace, finds
        nothing, and the comparator takes its missing-baseline path — a
        legitimate GREEN, with a baseline unread in the tree. This builds the
        job's shape and requires a VERDICT, where the text assertions that
        shipped the bug stayed green.
        """
        root = self._run_with_tree("ghw-speed-baseline-found-")
        baseline = root / "head" / "speed-baseline.json"
        self.assertTrue(baseline.is_file(),
                        "the fixture puts the baseline INSIDE the checkout, "
                        "which is where the job's convention puts it")
        # A decoy at the workspace root: the bug being guarded against would
        # have found this one and reported a verdict from it. Nothing should.
        (root / "speed-baseline.json").write_text("{}", encoding="utf-8")

        completed, summary = self._execute_compare(root)

        # Checked before the exit code, so that a regression reports the
        # thing it is: the comparator having found no baseline.
        self.assertNotIn("No baseline to compare against",
                         completed.stdout + completed.stderr,
                         "the step could not reach the baseline: its path is "
                         "repository-relative, so this step's working "
                         "directory must be the checkout")
        self.assertEqual(completed.returncode, 0,
                         completed.stdout + completed.stderr)
        self.assertNotIn("No baseline to compare against", summary)
        self.assertIn("within budget", summary)
        self.assertIn("committed baseline, unit suite", summary)
        self.assertIn("committed baseline, renderer workloads", summary)

    @REQUIRES_BENCH
    def test_the_compare_step_would_miss_a_baseline_it_cannot_reach(self):
        """The mutation, proven: run the same step from the workspace.

        With the baseline inside the checkout and the cwd one level up, the
        comparator finds nothing and exits 0. That is the exact shape of the
        defect, asserted so the passing test above is known to be able to fail.
        """
        root = self._run_with_tree("ghw-speed-baseline-mutation-")
        declared, working_dir, block = self.steps.step_run("Compare")
        self.assertEqual(working_dir, "head")
        # The scripts are reachable from the workspace too, so the step runs
        # all the way to the comparison — which is the shape of the defect:
        # the step found everything it was given and reported success on a
        # baseline it never opened. Without this the block would die earlier,
        # on the first missing script, and prove nothing about the baseline.
        shutil.copytree(root / "head" / "scripts", root / "scripts")
        broken = self.steps.bash(block, root, None,
                                 self.steps.env_for(declared, root))
        self.assertEqual(broken.returncode, 0,
                         broken.stdout + broken.stderr)
        self.assertIn("No baseline to compare against", broken.stdout)

    @REQUIRES_BENCH
    def test_a_missing_baseline_with_a_failed_suite_is_still_clean(self):
        """The combination that produced the original traceback.

        No baseline AND a suite that did not pass. `_no_baseline` reads the
        head reports to know what it measured, and that read can fail in its
        own right — which is what used to escape as a traceback and land on
        exit 1 by accident. It is a problem with the HEAD, so it reports as
        one: exit 2 with the reason.
        """
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

    @REQUIRES_BENCH
    def test_a_missing_baseline_prints_no_traceback(self):
        """The green first-push path, asserted on OUTPUT not just its code.

        The exit code alone would not have caught the original: the handler
        raised, the traceback reached stderr, and the shell's status
        happened to be right. This pins the property that failed.
        """
        root = self._run_with_tree("ghw-speed-missing-baseline-",
                                   baseline=False)
        completed, summary = self._execute_compare(root)
        self.assertEqual(completed.returncode, 0,
                         completed.stdout + completed.stderr)
        for stream in (completed.stdout, completed.stderr):
            self.assertNotIn("Traceback", stream)
            self.assertNotIn("MissingBaseline", stream)
        self.assertIn("No baseline to compare against", summary)

    def test_the_total_budget_is_not_named_like_the_threshold(self):
        """`tolerance` in the baseline is the gate. An env var reading as
        "the maximum" would be read as the threshold and is not."""
        text = (WORKFLOWS / "speed.yml").read_text(encoding="utf-8")
        self.assertNotIn("      MAX_REGRESSION:", text)
        self.assertIn('TOTAL_BUDGET: "0.30"', text)
        self.assertIn("NOT THE THRESHOLD", text)

    @REQUIRES_BENCH
    @REQUIRES_BENCH
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

    @REQUIRES_BENCH
    def test_empty_workload_listing_stops_the_compare_step(self):
        """A producer that prints nothing is as disabling as one that dies."""
        root = self._run_with_tree("ghw-speed-compare-empty-listing-")
        self._edit_harness(root, "        print(workload_node_id(workload[0]))",
                           "        pass  # deliberately empty listing")
        completed, _ = self._execute_compare(root)

        self.assertNotEqual(completed.returncode, 0,
                            completed.stdout + completed.stderr)
        self.assertIn("workload listing is empty", completed.stderr)

    @REQUIRES_BENCH
    def test_exempting_every_workload_stops_the_compare_step(self):
        """The other self-disable: naming all three empties the required list.

        Same false green as a broken listing, by configuration.
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

    def test_a_baseline_without_a_population_is_refused(self):
        # One document carries several contracts. Guessing which one this run
        # is being held to is the accommodation the gate exists to refuse.
        root = self._run_with_tree("ghw-speed-no-population-")
        # The step's own block with the flag removed, so what is under test is
        # the comparator's refusal and not a shell quoting accident — and it
        # still runs with the step's env and working directory, or it would be
        # testing a different command from the one that ships.
        stripped = "".join(
            line for line in self.steps.step_run("Compare")[2].splitlines(True)
            if "--population unit-suite" not in line)
        stripped = stripped.replace(
            "--population renderer-workloads \\\n", "")
        completed, _ = self._execute_compare(root, block=stripped)
        self.assertNotEqual(completed.returncode, 0,
                            completed.stdout + completed.stderr)
        self.assertIn("needs --population", completed.stderr)

    @REQUIRES_BENCH
    def test_unit_regression_still_runs_renderer_comparison(self):
        root = self._run_with_tree("ghw-speed-compare-failure-shape-",
                                   unit_counter=20.0)
        completed, summary = self._execute_compare(root)

        self.assertEqual(completed.returncode, 1)
        self.assertIn("committed baseline, unit suite", summary)
        self.assertIn("committed baseline, renderer workloads", summary)

    @REQUIRES_BENCH
    def test_missing_renderer_workload_fails_the_compare_step(self):
        """Issue #36: a shipped renderer that stops being measured is red."""
        root = self._run_with_tree("ghw-speed-compare-missing-renderer-",
                                   omit_renderer="bench.render-impact")
        completed, summary = self._execute_compare(root)

        self.assertNotEqual(completed.returncode, 0,
                            completed.stdout + completed.stderr)
        self.assertIn("e2e::bench.render-impact", summary)

    @REQUIRES_BENCH
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

    @REQUIRES_BENCH
    def test_retiring_a_workload_from_workloads_needs_no_exemption(self):
        """The real retirement path: dropped from WORKLOADS, no exemption.

        The harness then measures neither side, so the closed-set check has
        nothing unexplained to complain about. This asserts it, so the
        exemption surface is not mistaken for the retirement procedure.
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
        # The workload id list is regenerated from the EDITED harness, the
        # way the workflow regenerates it, so retiring a workload really does
        # remove it from the population — and the baseline records that
        # population, so retiring one legitimately requires re-deriving the
        # renderer digest in the same commit. That is the discipline working,
        # not an obstacle: the change is visible in the diff, and the ratchet
        # does not object because a digest is a description of what was
        # measured rather than a ceiling on it.
        self._write_workload_population(root)
        self._rederive_renderer_population(root)
        self._write_report(
            root / "reports" / "bench-head-1.xml", "e2e",
            [("bench.render", 1000.0), ("bench.render-responsiveness", 50.0)],
            self.RENDERER_METRIC)
        shutil.copyfile(root / "reports" / "bench-head-1.xml",
                        root / "reports" / "bench-head-2.xml")
        # A retired workload's baseline entry goes with it. The COMPARE half
        # is what this test asserts; the RATCHET half — that the deletion is
        # accepted because the harness no longer lists the workload, and
        # refused when nothing declares it — is in the four cases below.
        document = json.loads((root / "head" / "speed-baseline.json").read_text(
            encoding="utf-8"))
        document["populations"]["renderer-workloads"]["entries"].pop(
            "e2e::bench.render-impact")
        (root / "head" / "speed-baseline.json").write_text(
            json.dumps(document, indent=2), encoding="utf-8")
        completed, summary = self._execute_compare(root)

        self.assertEqual(completed.returncode, 0,
                         completed.stdout + completed.stderr)
        self.assertIn("renderer workloads", summary)
