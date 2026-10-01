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
                         "scripts/ci/counter.py",
                         "scripts/ci/baseline.py",
                         "scripts/bench/e2e_bench.py"):
            self._install(head, REPO_ROOT / relative)
        self._install_counter(head)
        reports = root / "reports"
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

    def _ratchet_checkout(self, prefix):
        """A workspace whose ONLY repository is at head/, as checkout leaves it."""
        root = self._temp_root(prefix)
        origin = root / "origin.git"
        subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
        checkout = root / "head"
        subprocess.run(["git", "clone", "-q", str(origin), str(checkout)],
                       check=True)
        return root, checkout

    def _ratchet_commit(self, checkout, message, push=False):
        """Commit inside a checkout built by `_ratchet_checkout`."""
        git = ["git", "-C", str(checkout)]
        for args in (["config", "user.email", "bench@example.invalid"],
                     ["config", "user.name", "bench"]):
            subprocess.run(git + args, check=True)
        subprocess.run(git + ["add", "speed-baseline.json",
                              "scripts/ci/compare_durations.py",
                              "scripts/ci/counter.py",
                              "scripts/ci/baseline.py"], check=True)
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
        path = root / "speed-baseline.json"
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
                self.assertEqual(self._step_run(step)[0]
                                 ["GH_COUNTER_METRIC"], "cpu_time")

    def _job_env(self):
        """`jobs.speed.env`, the mappings every step inherits."""
        lines = (WORKFLOWS / "speed.yml").read_text().splitlines()
        # Anchored on the job, because a step-level `env:` earlier in the
        # file would otherwise be picked up instead.
        job = lines.index("  speed:")
        start = next(i for i in range(job, len(lines))
                     if lines[i] == "    env:")
        return self._env_map(lines[start + 1:], "      ")

    def _step_env(self, block):
        """The environment a step runs with: job env, then step env."""
        env = dict(self._job_env())
        working_dir = None
        step_env = []
        inside = False
        for line in block:
            if line.startswith("        env:"):
                inside = True
                step_env = []
                continue
            if inside and (line.startswith("          ") or
                           line.strip().startswith("#")):
                step_env.append(line)
                continue
            if inside and line.strip():
                break
            if line.startswith("        working-directory:"):
                working_dir = line.split(":", 1)[1].strip()
        env.update(self._env_map(step_env, "          "))
        return env, working_dir

    @staticmethod
    def _env_map(lines, indent):
        """`KEY: value` pairs under an `env:` block, comments skipped."""
        env = {}
        for line in lines:
            if not line.startswith(indent):
                break
            text = line.strip()
            if text.startswith("#"):
                continue
            if ":" not in text:
                break
            key, _, value = text.partition(":")
            env[key.strip()] = value.strip().strip("\"'")
        return env

    def _step_run(self, step_name):
        """One named step, as the JOB runs it: (env, working-directory, body).

        GitHub merges a step's `env:` into its environment and runs its
        `working-directory` as the cwd. A test that executes the body has to
        do both or it is executing a different command from the one that
        ships — which is how a step can read clean in a text assertion and
        fail on every pull request.
        """
        lines = (WORKFLOWS / "speed.yml").read_text().splitlines()
        start = lines.index(f"      - name: {step_name}")
        block = []
        for line in lines[start:]:
            if line.startswith("      - ") and line is not lines[start]:
                break
            block.append(line)

        # Job-level env applies to every step and is NOT re-declared on the
        # step, so a test that merged only the step's env would run a
        # different command from the one that ships. The step wins where
        # both declare a key, as in the job itself.
        env, working_dir = self._step_env(block)

        run_line = next(i for i, line in enumerate(block)
                        if line.startswith("        run: |"))
        body = []
        for line in block[run_line + 1:]:
            if line.startswith("          "):
                body.append(line[10:])
            elif not line.strip():
                body.append("")
            else:
                break
        return env, working_dir, "\n".join(body) + "\n"

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

    def test_an_envelope_from_one_observation_is_refused(self):
        """A single sample's maximum is a measurement, not a worst case.

        Refused rather than marked: a file that says PROVISIONAL in a key
        nobody reads reads as authoritative.
        """
        root = self._run_with_tree("ghw-speed-envelope-n1-")
        document = json.loads((root / "speed-baseline.json").read_text(
            encoding="utf-8"))
        document["populations"][self.UNIT_POPULATION]["entries"][
            self.UNIT_NODE]["n"] = 1
        (root / "speed-baseline.json").write_text(
            json.dumps(document, indent=2), encoding="utf-8")
        completed, summary = self._execute_compare(root)

        self.assertEqual(completed.returncode, 1,
                         completed.stdout + completed.stderr)
        self.assertIn("at least 2", summary)
        self.assertIn("COULD NOT COMPARE", summary)

    def test_an_entry_with_min_above_max_is_refused(self):
        root = self._run_with_tree("ghw-speed-envelope-inverted-")
        document = json.loads((root / "speed-baseline.json").read_text(
            encoding="utf-8"))
        document["populations"][self.UNIT_POPULATION]["entries"][
            self.UNIT_NODE] = {"min": 20.0, "max": 10.0, "n": 6}
        (root / "speed-baseline.json").write_text(
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

    def test_the_ratchet_step_is_declared_against_the_base_ref(self):
        text = (WORKFLOWS / "speed.yml").read_text(encoding="utf-8")
        step = self._step_text("The committed baseline only ratchets down")
        # `base.sha` trails the base tip and does not refresh on synchronize,
        # so the comparison would be made against an OLDER, looser baseline —
        # the one direction that is wrong here.
        self.assertIn("github.event.pull_request.base.ref", step)
        self.assertNotIn("github.event.pull_request.base.sha", step)
        self.assertIn("FETCH_HEAD", step)
        self.assertIn("--ratchet-baselines", step)
        self.assertIn("$base_tip:$BASELINE", step)
        self.assertIn("working-directory: head", step)
        self.assertIn("id: probe", text)

    def _step_text(self, step_name):
        """One named step's YAML, from its name to the next step."""
        text = (WORKFLOWS / "speed.yml").read_text(encoding="utf-8")
        start = text.index(f"      - name: {step_name}")
        end = text.index("\n      - ", start + 10)
        return text[start:end]

    @unittest.skipIf(sys.platform == "win32",
                     "runs bash and git; the step runs only on ubuntu runners "
                     "in production")
    def test_the_ratchet_step_EXECUTES_against_a_checkout_at_head(self):
        """The step's `run:` body, executed the way the job runs it.

        It is gated to `pull_request` and every dispatch used as evidence was
        a `workflow_dispatch`, so nothing had ever executed it, and a text
        assertion cannot fail on a wrong working directory — which is what it
        had: `actions/checkout` puts the repository at `head/`, and a step
        without `working-directory` runs one level above it. This builds that
        shape and runs the block under `bash -e`, so the working directory,
        the fetch and the base-tip resolution are exercised, not described.
        """
        declared, working_dir, block = self._step_run(
            "The committed baseline only ratchets down")
        root = self._temp_root("ghw-speed-ratchet-exec-")
        origin = root / "origin.git"
        subprocess.run(["git", "init", "--bare", "-q", str(origin)],
                       check=True)
        checkout = root / "head"
        subprocess.run(["git", "clone", "-q", str(origin), str(checkout)],
                       check=True)
        # The workspace holds only `head/`, and the baseline lives inside the
        # repository — as it does in the job, where actions/checkout put the
        # whole tree under head/ and the step's working-directory is head.
        self._write_baseline(root)
        shutil.copyfile(root / "speed-baseline.json",
                        checkout / "speed-baseline.json")
        for name in ("compare_durations.py", "counter.py", "baseline.py"):
            script = checkout / "scripts" / "ci" / name
            script.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO_ROOT / "scripts" / "ci" / name, script)
        self._ratchet_commit(checkout, "baseline", push=True)
        # The base branch tip holds the baseline as it WAS; the local
        # checkout is the pull request head and RAISES a recorded maximum.
        # Nothing after this commit is pushed, so the block fetches a base
        # tip that genuinely differs — which is the only shape in which the
        # ratchet has anything to refuse.
        baseline_file = checkout / "speed-baseline.json"
        raised = json.loads(baseline_file.read_text(encoding="utf-8"))
        raised["populations"]["unit-suite"]["entries"][self.UNIT_NODE][
            "max"] *= 2
        (checkout / "speed-baseline.json").write_text(
            json.dumps(raised, indent=2), encoding="utf-8")
        self._ratchet_commit(checkout, "raise the ceiling")

        # The step's own env, with the `${{ }}` the job would have expanded
        # replaced by the values this test stands in for. Anything still
        # holding an expression is dropped rather than exported, so the block
        # reads the test's value instead of a literal `${{ ... }}`.
        resolved = {key: value for key, value in declared.items()
                    if "${{" not in value}
        self.assertIn("BASELINE", resolved,
                      "the ratchet step reads $BASELINE from the job env")
        env = {**os.environ, **resolved, "BASE_REF": "main",
               "RUNNER_TEMP": str(root)}
        # GitHub runs a step with no declared working-directory in the
        # workspace, so that is what this does when the key is absent — which
        # is the point: removing `working-directory: head` from the step has
        # to make THIS fail, by executing the commands one level above the
        # only repository on the box.
        completed = subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", block],
            cwd=root / working_dir if working_dir else root, env=env,
            capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 1,
                         completed.stdout + completed.stderr
                         + (root / "base-baseline.json").read_text(
                             encoding="utf-8", errors="replace")[:400])
        self.assertNotIn("not a git repository", completed.stderr)
        self.assertIn("went UP", completed.stderr)
        self.assertIn("Base branch tip:", completed.stdout)

    def test_the_ratchet_step_would_fail_without_its_working_directory(self):
        """The mutation the executed test exists to catch, proven.

        The workspace this builds has NO repository at its top level — only
        `head/`, exactly as actions/checkout leaves it — so running the block
        there is what the step would do if it stopped declaring its working
        directory. This asserts that the failure mode is real rather than
        assumed: a control that only proves it works has not proved it can
        fail, and this one was invisible to the suite for seven rounds.
        """
        root, _ = self._ratchet_checkout("ghw-speed-ratchet-mutation-")
        declared, working_dir, block = self._step_run(
            "The committed baseline only ratchets down")
        self.assertEqual(working_dir, "head",
                         "if this step ever stops declaring one, delete this "
                         "test deliberately — it asserts a key that is gone")
        env = {**os.environ,
               **{k: v for k, v in declared.items() if "${{" not in v},
               "BASE_REF": "main", "RUNNER_TEMP": str(root)}
        broken = subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", block],
            cwd=root, env=env, capture_output=True, text=True, check=False)
        self.assertNotEqual(broken.returncode, 0)
        self.assertIn("no git repository", broken.stderr)

    def test_no_workflow_still_looks_up_a_release_for_the_speed_gate(self):
        # The whole point of issue #81: the comparison point is committed
        # data now, so a tag lookup here would be a leftover argument for a
        # method this gate no longer uses.
        text = (WORKFLOWS / "speed.yml").read_text(encoding="utf-8")
        self.assertNotIn("releases/latest", text)
        self.assertNotIn("--base-label \"$BASE_TAG\"", text)


if __name__ == "__main__":
    unittest.main()
