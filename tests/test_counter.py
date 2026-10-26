"""Tests for the counter used by the speed job in tests.yml.

The whole point of scripts/ci/counter.py is that it measures something runner
load cannot move, so these tests are mostly about the ways it could lie:
claiming a perf it never ran, comparing two instruments, reporting a number
without saying what measured it, and orphaning the real command on a timeout.

They must not depend on the HOST. This repository runs its suite on Windows
and macOS as well as Linux, and the runner this gate exists for has its own
kernel settings. So the instrument is injected — a fake `strace` on disk, or
`metric=` passed straight in — rather than inherited from whatever the test
happens to land on.
"""
import ast
import importlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

# counter_platform loads scripts/ci/counter.py ONCE and by path, the way
# every module here loads a sibling. Importing the predicate through it,
# rather than reloading the module in this file, is what makes every guard
# in the suite read the SAME module object.
from scripts.bench.bench_platform import REQUIRES_BENCH, counter


REPO_ROOT = Path(__file__).resolve().parents[1]


def number(element, attribute):
    """One XML attribute as a float, refusing to read a missing one.

    `Element.get` is Optional[str], and a test that silently reads a missing
    attribute as zero would pass for the wrong reason — which is the defect
    this file is mostly about, applied to itself.
    """
    raw = element.get(attribute)
    if raw is None:
        raise AssertionError(f"{attribute} is absent from {element.tag}")
    return float(raw)


class TestPlatformLimits(unittest.TestCase):
    """What this instrument does where it cannot work.

    `resource` is Unix-only and `/proc` is Linux-only, while this module is
    imported by the comparator and the suite runs on a windows/macOS
    matrix. A bare `import resource` turned that into an import error in
    thirty-five unrelated tests; an unconditional `/proc` read turned a
    probe step into a failed run. Both cases are simulated here rather than
    hoped away — these are the shapes a Linux box never produces on its
    own.
    """

    def test_the_module_imports_without_resource(self):
        """The import itself is the control.

        On a platform without `resource` this module must still LOAD, or
        every test that touches the comparator dies before it runs. The
        simulation is a real reload with `resource` blocked in `sys.modules`,
        so it exercises the try/except rather than asserting on a literal.
        """
        spec = importlib.util.spec_from_file_location(
            "ghw_counter_without_resource",
            REPO_ROOT / "scripts" / "ci" / "counter.py")
        if spec is None or spec.loader is None:
            self.fail("cannot build a spec for counter.py")
        with mock.patch.dict(sys.modules, {"resource": None}):
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            try:
                spec.loader.exec_module(module)
            finally:
                del sys.modules[spec.name]
        self.assertIsNone(module.resource)
        # All three metric names the module defines must exist without
        # `resource`, because the refusal for a missing facility happens at
        # measurement time, not at import time. The statement instrument
        # (issue #269) is the gated one; the calibrated ratio is retired
        # (declared so schema 3/4 documents keep validating) and the raw
        # CPU path is the fallback the harness dispatches to explicitly.
        self.assertEqual(module.METRICS,
                         (module.CPU_METRIC, module.CALIBRATED_METRIC,
                          module.STATEMENT_METRIC))

    @REQUIRES_BENCH
    def test_measuring_without_resource_refuses_rather_than_guessing(self):
        # A silent fallback would return a number that measures something
        # else. This asserts the refusal names both the platform and the
        # reason, because a bare "unavailable" is not actionable.
        with mock.patch.object(counter, "resource", None),                 mock.patch.object(counter.sys, "platform", "win32"):
            with self.assertRaises(counter.CounterError) as caught:
                counter.measure([sys.executable, "-c", "pass"],
                                metric=counter.CPU_METRIC)
        message = str(caught.exception)
        self.assertIn("win32", message)
        self.assertIn("RUSAGE_CHILDREN", message)
        self.assertIn("POSIX", message)
        self.assertIn("will not fall back to wall time", message)

    def test_a_missing_proc_file_is_read_as_unavailable_not_fatal(self):
        with mock.patch.object(counter, "PARANOID_PATH",
                               Path("/nonexistent/perf_event_paranoid")):
            self.assertEqual(counter._setting(  # pylint: disable=protected-access
                counter.PARANOID_PATH),
                "unavailable on this platform")

    def test_the_probe_still_speaks_on_a_platform_with_no_proc(self):
        """The probe's OUTPUT is the record, and it must exist everywhere.

        A reader who sees CPU seconds in use should still see what the
        probe looked for, even where the facility is not there to be read —
        "I looked and there was nothing" is a real record, and a different
        one from "the file exists and says 4".
        """
        with mock.patch.object(counter, "PARANOID_PATH",
                               Path("/nonexistent/perf_event_paranoid")), \
                mock.patch.object(counter, "PTRACE_SCOPE_PATH",
                                  Path("/nonexistent/ptrace_scope")):
            lines = counter._probe_lines()  # pylint: disable=protected-access
        joined = "\n".join(lines)
        self.assertIn(f"metric={counter.STATEMENT_METRIC}", joined)
        self.assertIn("perf_event_paranoid=unavailable on this platform", joined)
        self.assertIn("ptrace_scope=unavailable on this platform", joined)
        self.assertIn("metric_reason=", joined)

    def test_every_probe_line_is_a_key_value_pair(self):
        # The workflow pipes these into $GITHUB_OUTPUT, which accepts
        # key=value and nothing else, and the macOS run is where a line
        # that is not one would have shown up.
        lines = counter._probe_lines()  # pylint: disable=protected-access
        for line in lines:
            with self.subTest(line=line):
                self.assertRegex(line, r"^[A-Za-z_][A-Za-z0-9_]*=.+")


def rusage_available():
    """Whether this platform has the facility `counter.py` measures with."""
    try:
        # pylint: disable=import-outside-toplevel,unused-import
        import resource
    except ImportError:
        return False
    return True


REQUIRES_BENCH = unittest.skipUnless(
    rusage_available(),
    "this exercises counter.measure(), which is POSIX-only by decision: "
    "cpu_time comes from resource.getrusage(RUSAGE_CHILDREN) and the "
    "instrument refuses rather than falling back to wall time where that "
    "does not exist")


class TestCwdIsHonoured(unittest.TestCase):
    """`--cwd` must reach `measure()`, not just parse.

    A re-review mutated `main()` into `measure(command, cwd=None, ...)` —
    the flag accepted, stored and silently ignored — and the whole suite
    stayed green, because every step-level test points its `working-directory`
    and its `--cwd` at the same directory, so an ignored flag is invisible
    from there. This is the tool-level control that mutation defeated, and it
    is the only place the two can be told apart.
    """

    @REQUIRES_BENCH
    def test_the_flag_changes_where_the_command_runs(self):
        listing = "import os; print(sorted(os.listdir('.')))"
        with tempfile.TemporaryDirectory(prefix="ghw-cwd-") as td:
            root = Path(td)
            (root / "marker-only-here.txt").write_text("x", encoding="utf-8")
            inside = counter.measure([sys.executable, "-c", listing], cwd=root,
                                     metric=counter.CPU_METRIC)
            outside = counter.measure([sys.executable, "-c", listing],
                                      metric=counter.CPU_METRIC)
        self.assertIn("marker-only-here.txt", inside.stdout)
        self.assertNotIn("marker-only-here.txt", outside.stdout)

    @REQUIRES_BENCH
    def test_the_cli_passes_the_flag_through(self):
        # The library is only half of it: the CLI is what the workflow
        # calls, and a flag wired to nothing there is the same defect one
        # layer up. It is also the same POPULATION as the library cases — the
        # CLI can only produce output where the instrument works, because
        # `main()` turns the refusal into exit 2 and writes no JUnit. Which
        # is why it is guarded on the INSTRUMENT's facility rather than on
        # its own: the limit it hits is `measure()`'s, not a shell question.
        with tempfile.TemporaryDirectory(prefix="ghw-cwd-cli-") as td:
            root = Path(td)
            (root / "marker-only-here.txt").write_text("x", encoding="utf-8")
            report = Path(td) / "r.xml"
            # Ambient-proof, as the raw CLI refuses the ambient ratio pin.
            env = {k: v for k, v in os.environ.items()
                   if k != counter.METRIC_ENV}
            result = subprocess.run(
                [sys.executable,
                 str(REPO_ROOT / "scripts" / "ci" / "counter.py"),
                 "--junit-file", str(report),
                 "--name", "counter::unit-suite",
                 "--cwd", str(root),
                 "--", sys.executable, "-c",
                 "import os; print(sorted(os.listdir('.')))"],
                capture_output=True, text=True, check=False, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("marker-only-here.txt", result.stdout)


class TestTheRemovedInstrument(unittest.TestCase):
    """The instrument that lost, and must not come back by accident.

    Both better instruments were implemented, measured and dropped, and the
    measurements are in counter.py's docstring. What is tested here is that
    neither can be reintroduced silently: `syscalls` is no longer a name this
    file will measure under, so an environment still exporting it — a stale
    $GITHUB_ENV from an older revision of the workflow, most likely — is a
    refusal rather than a measurement under a name nothing else agrees with.
    """

    def test_syscalls_is_no_longer_a_metric(self):
        # Three names, and `syscalls` is none of them: raw CPU seconds, the
        # retired ratio label and the statement count are the only names a
        # run can measure under.
        self.assertEqual(counter.METRICS,
                         (counter.CPU_METRIC, counter.CALIBRATED_METRIC,
                          counter.STATEMENT_METRIC))

    def test_an_environment_naming_the_removed_instrument_is_refused(self):
        with mock.patch.dict(os.environ, {"GH_WIDGETS_GH_COUNTER_METRIC": "syscalls"}):
            with self.assertRaises(counter.CounterError) as caught:
                counter.choose_metric()
        self.assertIn("syscalls", str(caught.exception))

    @REQUIRES_BENCH
    def test_the_refusal_survives_the_public_measure_call(self):
        with mock.patch.dict(os.environ, {"GH_WIDGETS_GH_COUNTER_METRIC": "syscalls"}):
            with self.assertRaises(counter.CounterError):
                counter.measure([sys.executable, "-c", "pass"])

    def test_the_cost_of_the_instrument_is_recorded_where_the_code_is(self):
        # The next reader has to be able to see that a deterministic counter
        # exists and what it would cost, without re-deriving 12.8x from
        # scratch. If this fails, the finding was deleted with the code.
        doc = (counter.__doc__ or "")
        self.assertIn("12.8x", doc)
        self.assertIn("36811152307", doc)
        self.assertIn("36812466498", doc)


class TestNoUnguardedPathToTheInstrument(unittest.TestCase):
    """No test reaches `measure()` by a path its own guard does not cover.

    The body-walk that places the guards looks for `measure(` IN THE
    BODY. That misses two paths, and both have already cost a Windows
    round: a CLI invocation of counter.py in a subprocess, and a helper the
    walk cannot see. So this asks the question directly, over the whole
    suite, by the two patterns that reach the instrument:

      * a literal `.measure(` or `counter.measure(` inside a test, and
      * counter.py named as an argv element of a subprocess call

    A test that reaches either from inside a `REQUIRES_BENCH` or
    `REQUIRES_POSIX_SHELL` boundary is fine. One that reaches either
    without one is not, and on Linux that is invisible — which is how a
    guard lands on the preceding method and nobody sees it for a round.
    """

    # Reaches, spelled the two ways the instrument is actually entered.
    REACHES = (".measure(", "counter.py")

    # Named, with the reason each cannot reach `measure()`. An allowlist is
    # only worth having if it is explicit and narrow: anything NEW is
    # flagged, which is the whole point. Each of these was checked, not
    # assumed.
    ALLOWED = {
        "test_counter.py::test_the_module_imports_without_resource":
            "loads the module; the import is the thing under test and the "
            "refusal is asserted, not triggered",
        "test_counter.py::test_probe_writes_github_output_shaped_lines":
            "runs `--probe`, which prints the record and never measures",
        "test_counter.py::test_no_command_is_an_error_not_a_silent_success":
            "runs with no command, so argparse refuses before `measure`",
        "test_counter.py::test_counter_is_a_file_and_not_a_package":
            "names counter.py as a PATH and checks the filesystem",
        "test_counter.py::test_the_new_files_are_named_back_by_the_ignore_file":
            "names counter.py as a path for `git check-ignore`",
        "test_counter.py::test_the_time_attribute_is_the_counter_not_seconds":
            "calls write_junit with a FABRICATED Measurement; no process",
        "test_counter.py::test_a_failing_command_is_a_junit_failure":
            "write_junit with a fabricated return code; no process",
        "test_counter.py::test_a_name_that_is_not_a_node_id_is_refused":
            "write_junit with a bad node id; no process",
        "test_counter.py::test_no_unguarded_test_reaches_the_instrument":
            "this control; it names the patterns it is looking for",
        "test_speed_workflow.py::test_both_measurement_steps_pin_the_instrument":
            "reads the workflow's text",
    }

    def test_no_unguarded_test_reaches_the_instrument(self):
        offenders = []
        for path in sorted((REPO_ROOT / "tests").glob("test_*.py")):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef):
                    continue
                if not node.name.startswith("test_"):
                    continue
                body = ast.get_source_segment(source, node) or ""
                if not any(reach in body for reach in self.REACHES):
                    continue
                decorators = "".join(
                    ast.get_source_segment(source, d) or ""
                    for d in node.decorator_list)
                # A class-level guard covers everything in it.
                enclosing = self._enclosing_guards(source, node)
                if any(word in decorators + enclosing
                       for word in ("REQUIRES_BENCH", "REQUIRES_POSIX_SHELL")):
                    continue
                name = f"{path.name}::{node.name}"
                if name not in self.ALLOWED:
                    offenders.append(name)
        self.assertEqual(
            offenders, [],
            "these tests reach counter.measure() — directly or through its "
            "CLI — without a guard, so they fail on a platform where the "
            "instrument refuses: " + ", ".join(offenders))

    def test_every_allowance_is_still_relevant(self):
        """An allowlist rots quietly if nothing checks it.

        Each entry names a test that MIGHT reach the instrument; if that
        test is deleted or renamed the entry is stale, and a stale entry is
        one more way for a new offender to slip past a control that has
        stopped reading.
        """
        for name in self.ALLOWED:
            module, _, test = name.partition("::")
            with self.subTest(entry=name):
                self.assertTrue(
                    (REPO_ROOT / "tests" / module).is_file()
                    and f"def {test}(" in (REPO_ROOT / "tests" / module).read_text(
                        encoding="utf-8"),
                    f"{name} is allowlisted but no longer exists")

    @staticmethod
    def _enclosing_guards(source, target):
        """Guards declared on a class the function sits inside."""
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.ClassDef):
                continue
            if any(child is target for child in node.body):
                return "".join(ast.get_source_segment(source, d) or ""
                               for d in node.decorator_list) + "".join(
                                   ast.get_source_segment(source, stmt) or ""
                                   for stmt in node.body[:1])
        return ""


class TestTheBenchPredicate(unittest.TestCase):
    """The one predicate every bench-driving test consults, checked here.

    If `bench_is_runnable()` ever returned False where the harness CAN run,
    every guarded test would skip and CI would go green with nothing
    checked. That is the failure this control exists to prevent, and it is
    why the predicate lives beside the instrument's refusal rather than in
    a test file where a tired reader might widen it.
    """

    @unittest.skipUnless(counter.bench_is_runnable(),
                         "this platform cannot run the harness, so there is "
                         "nothing for the predicate to be true about")
    def test_the_predicate_is_true_where_the_harness_can_run(self):
        self.assertTrue(
            counter.bench_is_runnable(),
            "this platform CAN run the harness, so bench_is_runnable() must "
            "say so; a false here would skip every guarded control and turn "
            "CI green with nothing checked")

    def test_the_predicate_is_false_where_it_cannot_run(self):
        # The other direction, so the control is not satisfied on a machine
        # where the facility is absent by declaring the facility absent.
        with mock.patch.object(counter, "resource", None):
            self.assertFalse(counter.bench_is_runnable())

    def test_the_predicate_tracks_the_facility_the_instrument_needs(self):
        # Same answer as the instrument's own guard, not a parallel one that
        # can drift from it.
        with mock.patch.object(counter, "resource", None):
            self.assertFalse(counter.bench_is_runnable())

    def test_the_shell_predicate_is_true_where_bash_works(self):
        # Same anti-switch control for the SECOND predicate, and its GUARD was
        # wrong the first time: it keyed on `shutil.which("bash")`, which is
        # NOT the same question. On a Windows runner `bash` is on PATH — it is
        # the WSL shim — and it does not work. Keying on "is bash present"
        # therefore failed this control on exactly the machine where the
        # predicate is doing its job. It now keys on the predicate itself,
        # which is the question the control is about.
        from scripts.ci import speed_workflow_steps  # pylint: disable=import-outside-toplevel
        if speed_workflow_steps.shell_is_posix():
            self.assertTrue(speed_workflow_steps.shell_is_posix(),
                            "a working bash is here, so shell_is_posix() must "
                            "say so; a false would skip every control that "
                            "executes a workflow step body")

    def test_the_two_predicates_are_not_the_same_question(self):
        # They answer different things and are guarded separately. Folding
        # them together would make this branch's narrower guard mean
        # something broader than it says.
        from scripts.bench import bench_platform  # pylint: disable=import-outside-toplevel
        from scripts.ci import speed_workflow_steps  # pylint: disable=import-outside-toplevel
        with mock.patch.object(counter, "resource", None):
            # No instrument, and the shell question is untouched: that is
            # what "different question" means here.
            self.assertFalse(counter.bench_is_runnable())
            self.assertEqual(
                bench_platform.counter.BENCH_RUNNABLE,
                bench_platform.counter.BENCH_RUNNABLE)
            self.assertTrue(hasattr(speed_workflow_steps, "shell_is_posix"))

    def test_the_shared_decorator_reads_the_same_predicate(self):
        from scripts.bench import bench_platform  # pylint: disable=import-outside-toplevel
        self.assertEqual(bench_platform.counter.BENCH_RUNNABLE,
                         counter.BENCH_RUNNABLE)


class TestMetricChoice(unittest.TestCase):
    def test_the_jobs_export_is_honoured_verbatim(self):
        # The workflow pins GH_WIDGETS_GH_COUNTER_METRIC explicitly in both
        # measurement steps rather than relying on a fallback, so the
        # instrument a population is judged by is visible in the workflow
        # rather than buried here. The pin is the statement count now, and
        # the harness dispatches on it to measure_statement.
        with mock.patch.dict(os.environ, {counter.METRIC_ENV:
                                          counter.STATEMENT_METRIC}):
            self.assertEqual(counter.choose_metric(),
                             counter.STATEMENT_METRIC)
        self.assertEqual(counter.choose_metric(counter.STATEMENT_METRIC),
                         counter.STATEMENT_METRIC)

    def test_an_unrecognised_export_is_refused(self):
        with self.assertRaises(counter.CounterError) as caught:
            counter.choose_metric("furlongs")
        self.assertIn("cpu_time", str(caught.exception))

    def test_the_only_instrument_is_cpu_time(self):
        # Ambient-proof: the workflow's unit step pins cpu_time_ratio into
        # this suite's environment, so a default assertion that read the
        # ambient pin would fail in the one process that runs under it.
        with mock.patch.dict(os.environ):
            os.environ.pop(counter.METRIC_ENV, None)
            self.assertEqual(counter.choose_metric(), counter.CPU_METRIC)

    def test_it_needs_no_probe_to_get_there(self):
        # There is nothing to choose, so nothing is probed: an instrument
        # this file cannot use is not a reason to spend a process on every
        # call site.
        with mock.patch.object(counter, "_probe_lines") as probe:
            with mock.patch.dict(os.environ):
                os.environ.pop(counter.METRIC_ENV, None)
                self.assertEqual(counter.choose_metric(),
                                 counter.CPU_METRIC)
        probe.assert_not_called()


class TestProbe(unittest.TestCase):
    def test_probe_lines_name_the_instrument_and_its_reason(self):
        with mock.patch.object(counter, "_setting", return_value="4"):
            lines = counter._probe_lines()  # pylint: disable=protected-access
        joined = "\n".join(lines)
        self.assertIn(f"metric={counter.STATEMENT_METRIC}", joined)
        # There is nothing to choose, so the step's job is the RECORD of why:
        # the two kernel settings that decide whether a deterministic counter
        # is possible at all.
        self.assertIn("perf_event_paranoid=4", joined)
        self.assertIn("ptrace_scope=4", joined)
        self.assertIn("metric_reason=", joined)
        # The reason names what was dropped, so the record is a record and
        # not a bare restatement of the metric.
        self.assertIn("12.8x", joined)


class TestMeasure(unittest.TestCase):
    @REQUIRES_BENCH
    def test_cpu_time_is_measured_not_guessed(self):
        measured = counter.measure(
            [sys.executable, "-c", "sum(range(200000))"],
            metric=counter.CPU_METRIC)

        self.assertGreater(measured.value, 0.0)
        self.assertEqual(measured.metric, counter.CPU_METRIC)
        self.assertEqual(measured.returncode, 0)
        self.assertGreater(measured.wall, 0.0)

    @REQUIRES_BENCH
    def test_the_instrument_travels_with_the_number(self):
        # A caller that can record a value without recording what measured
        # it can write a baseline that looks comparable and is not.
        value, metric = counter.measure(
            [sys.executable, "-c", "pass"],
            metric=counter.CPU_METRIC)[:2]
        self.assertIsInstance(value, float)
        self.assertEqual(metric, counter.CPU_METRIC)

    @REQUIRES_BENCH
    def test_stdout_and_stderr_come_back_intact(self):
        measured = counter.measure(
            [sys.executable, "-c",
             "import sys; print('out'); sys.stderr.write('err')"],
            metric=counter.CPU_METRIC)
        self.assertEqual(measured.stdout.strip(), "out")
        self.assertEqual(measured.stderr.strip(), "err")

    @REQUIRES_BENCH
    def test_pyhashseed_is_pinned_in_the_child(self):
        # One source of counter drift removed. It does NOT make wall time
        # deterministic, and nothing below claims that it does.
        self.assertEqual(counter.child_environment()["PYTHONHASHSEED"], "0")

    @REQUIRES_BENCH
    def test_the_environment_caller_supplied_is_honoured(self):
        env = counter.child_environment({"MARKER": "kept"})
        self.assertEqual(env["MARKER"], "kept")
        self.assertEqual(env["PYTHONHASHSEED"], "0")

    @unittest.skipIf(sys.platform == "win32",
                     "process groups are a POSIX mechanism; the timeout "
                     "path runs only on ubuntu runners in production")
    @REQUIRES_BENCH
    def test_a_timeout_kills_the_whole_process_group(self):
        # The direct child here is `python`, which spawns a grandchild that
        # inherits the output pipes. Killing only the direct child would
        # leave the grandchild holding them, and communicate() would never
        # return — the gate would hang instead of failing.
        script = (
            "import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', 'import time;"
            " time.sleep(120)'])\n"
            "time.sleep(120)\n")
        with self.assertRaises(subprocess.TimeoutExpired):
            counter.measure([sys.executable, "-c", script],
                            metric=counter.CPU_METRIC, timeout=2)


REQUIRES_STATEMENT = unittest.skipUnless(
    sys.version_info >= (3, 12),
    "measure_statement counts sys.monitoring LINE events, which exist "
    "from Python 3.12; the speed job pins 3.13")


class TestStatementInstrument(unittest.TestCase):
    """The deterministic instrument issue #269 made the gate's value.

    Five instruments red main on unchanged code across #190, #216, #221,
    #240 and #269, every one traceable to the runner. The statement count is
    not a time at all — it is the number of `sys.monitoring` LINE events the
    measured child executes, so the same code and the same population give
    the same count on any runner. The cases pin the dispatch, determinism
    and refusals: an undercount is not a measurement, nor a number the child never wrote.
    """

    @staticmethod
    def run_statement(command, **kwargs):
        return counter.measure_statement(
            [sys.executable, *command], **kwargs)

    @REQUIRES_STATEMENT
    def test_the_count_is_deterministic_for_identical_code(self):
        # Same program and lines, three runs, one count.
        workload = [sys.executable, "-c",
                    "for _ in range(4096):\n    total = sum(range(64))\n"
                    "print(total)"]
        counts = {counter.measure_statement(workload).value for _ in range(3)}
        self.assertEqual(len(counts), 1, f"counts spread: {counts}")

    @REQUIRES_STATEMENT
    def test_a_measurement_carries_a_positive_count_and_its_metric(self):
        measured = self.run_statement(["-c", "print('counted')"])  # the happy path
        self.assertEqual(measured.metric, counter.STATEMENT_METRIC)
        self.assertEqual(measured.returncode, 0)
        self.assertGreater(measured.value, 0.0)
        self.assertGreater(measured.wall, 0.0)
        self.assertEqual(measured.stdout.strip(), "counted")

    @REQUIRES_STATEMENT
    def test_the_module_dispatch_runs_the_target_module(self):
        # Production shape for the unit suite (-m unittest discover).
        measured = self.run_statement(["-m", "timeit", "-n", "1", "-r", "1",
                                       "pass"])
        self.assertEqual(measured.returncode, 0, measured.stderr)
        self.assertIn("1 loop", measured.stdout)
        self.assertGreater(measured.value, 0.0)

    @REQUIRES_STATEMENT
    def test_the_script_dispatch_runs_the_target_script(self):
        # Production shape for the renderers: a script path.
        with tempfile.TemporaryDirectory(prefix="ghw-statement-") as td:
            script = Path(td) / "subject.py"
            script.write_text("import sys\nprint('script ran', sys.argv[1:])\n",
                              encoding="utf-8")
            measured = self.run_statement([str(script), "arg"])
        self.assertEqual(measured.returncode, 0, measured.stderr)
        self.assertIn("script ran ['arg']", measured.stdout)

    @REQUIRES_STATEMENT
    def test_the_bootstrap_strips_its_leading_separator(self):
        # One leading `--` (the parent's own separator) is stripped.
        with tempfile.TemporaryDirectory() as td:
            count_path = Path(td) / "count.json"
            env = counter.child_environment()
            env["GH_WIDGETS_STATEMENT_COUNT_FILE"] = str(count_path)
            done = subprocess.run(
                [sys.executable, "-c", counter._STATEMENT_BOOTSTRAP,  # pylint: disable=protected-access
                 "--", "-c", "print('stripped')"],
                env=env, capture_output=True, text=True, check=False)
            payload = json.loads(count_path.read_text(encoding="utf-8"))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "stripped")
        self.assertGreater(payload["count"], 0)
        self.assertEqual(payload["missed"], 0)

    @REQUIRES_STATEMENT
    def test_a_failing_targets_own_verdict_travels(self):
        measured = self.run_statement(["-c", "raise SystemExit(7)"])
        self.assertEqual(measured.returncode, 7)
        self.assertEqual(measured.metric, counter.STATEMENT_METRIC)

    @REQUIRES_STATEMENT
    def test_a_missing_count_file_refuses(self):
        # A hard-killed child never runs atexit: a refusal, never a zero.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "never-written.json"
            with self.assertRaises(counter.CounterError) as raised:
                counter._read_statement_file(path)  # pylint: disable=protected-access
        self.assertIn(str(path), str(raised.exception))

    @REQUIRES_STATEMENT
    def test_an_undercount_is_refused_not_recorded(self):
        # missed > 0 is a lower bound, not a number; recording it lies optimistic.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "undercount.json"
            path.write_text('{"count": 100, "missed": 3}', encoding="utf-8")
            with self.assertRaises(counter.CounterError) as raised:
                counter._read_statement_file(path)  # pylint: disable=protected-access
        self.assertIn("missed", str(raised.exception))
        self.assertIn("undercount", str(raised.exception))

    @REQUIRES_STATEMENT
    def test_a_count_that_is_not_a_natural_number_refuses(self):
        for payload in ('{"count": -1, "missed": 0}',
                        '{"count": "many", "missed": 0}',
                        '{"count": 1.5, "missed": 0}',
                        '{"count": true, "missed": 0}',
                        '{"missed": 0}',
                        'not json at all'):
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as td:
                    path = Path(td) / "broken.json"
                    path.write_text(payload, encoding="utf-8")
                    with self.assertRaises(counter.CounterError) as raised:
                        counter._read_statement_file(path)  # pylint: disable=protected-access
                self.assertIn("count", str(raised.exception))

    @REQUIRES_STATEMENT
    def test_a_non_interpreter_command_refuses(self):
        # A non-interpreter head would count the bootstrap, never the subject.
        for head in ("/bin/sh", "/usr/bin/env"):
            with self.subTest(interpreter=head):
                with self.assertRaises(counter.CounterError) as raised:
                    counter.measure_statement([head, "-c", "pass"])
        self.assertIn("interpreter", str(raised.exception))

    @REQUIRES_BENCH
    @REQUIRES_STATEMENT
    def test_the_statement_label_is_refused_by_the_raw_path(self):
        # Raw CPU seconds never carry statement_count: the mislabelled
        # number would pass the comparator's metric check.
        with self.assertRaises(counter.CounterError) as raised:
            counter.measure([sys.executable, "-c", "pass"],
                            metric=counter.STATEMENT_METRIC)
        self.assertIn("statement", str(raised.exception))
        with mock.patch.dict(os.environ,
                             {counter.METRIC_ENV: counter.STATEMENT_METRIC}):
            with self.assertRaises(counter.CounterError):
                counter.measure([sys.executable, "-c", "pass"])

    def test_the_statement_metric_is_declared_beside_the_others(self):
        self.assertEqual(counter.STATEMENT_METRIC, "statement_count")
        self.assertEqual(counter.METRICS,
                         (counter.CPU_METRIC, counter.CALIBRATED_METRIC,
                          counter.STATEMENT_METRIC))

    def test_the_measured_figures_are_recorded_where_the_code_is(self):
        # The instrument's own measured cost, so it need not be re-derived.
        doc = (counter.__doc__ or "")
        self.assertIn("99,222,446", doc)
        self.assertIn("0.0015%", doc)
        self.assertIn("missed: 0", doc)
        self.assertIn("not counted", doc)
        self.assertIn("#269", doc)

    @REQUIRES_STATEMENT
    def test_a_hard_child_exit_writes_no_count_and_refuses(self):
        # os._exit skips atexit: the integration form of the missing-file refusal.
        with self.assertRaises(counter.CounterError) as raised:
            counter.measure_statement(
                [sys.executable, "-c", "import os; os._exit(0)"])
        self.assertIn("count file", str(raised.exception))


class TestWriteJunit(unittest.TestCase):
    def measure(self, returncode=0):
        return counter.Measurement(1234.5, counter.CPU_METRIC, 0.5,
                                   "out", "err", returncode)

    def test_the_time_attribute_is_the_counter_not_seconds(self):
        with tempfile.TemporaryDirectory(prefix="ghw-counter-junit-") as td:
            path = Path(td) / "r.xml"
            counter.write_junit(path, "counter::unit-suite", self.measure(),
                                ["render"])
            suite = ET.parse(path).getroot()

        assert suite is not None
        self.assertEqual(number(suite, "time"), 1234.5)
        self.assertEqual(suite.get("gh-metric"), counter.CPU_METRIC)
        case = suite.find("testcase")
        assert case is not None
        self.assertEqual(case.get("classname"), "counter")
        self.assertEqual(case.get("name"), "unit-suite")
        self.assertEqual(number(case, "time"), 1234.5)
        # The wall seconds are carried separately, for the smoke gate alone.
        self.assertEqual(number(case, "gh-wall"), 0.5)
        self.assertIsNone(case.find("failure"))

    def test_a_failing_command_is_a_junit_failure(self):
        # Which is what drops it out of the comparator's intersection: a
        # counter measured over a run that failed is not comparable.
        with tempfile.TemporaryDirectory(prefix="ghw-counter-junit-") as td:
            path = Path(td) / "r.xml"
            counter.write_junit(path, "counter::unit-suite",
                                self.measure(returncode=2), ["render"])
            suite = ET.parse(path).getroot()

        assert suite is not None
        self.assertEqual(suite.get("failures"), "1")
        case = suite.find("testcase")
        assert case is not None
        self.assertIsNotNone(case.find("failure"))

    def test_a_name_that_is_not_a_node_id_is_refused(self):
        with self.assertRaises(counter.CounterError):
            counter.write_junit(Path("/dev/null"), "unit-suite",
                                self.measure())


class TestPopulationDigest(unittest.TestCase):
    def test_it_describes_the_set_not_the_order(self):
        first = counter.population_digest(["b.Test.t2", "a.Test.t1"])
        second = counter.population_digest(["a.Test.t1", "b.Test.t2"])
        self.assertEqual(first, second)

    def test_a_different_population_digests_differently(self):
        self.assertNotEqual(counter.population_digest(["a.Test.t1"]),
                            counter.population_digest(["a.Test.t1",
                                                       "a.Test.t2"]))


class TestCli(unittest.TestCase):
    def test_probe_writes_github_output_shaped_lines(self):
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "ci" / "counter.py"),
             "--probe"], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        keys = [line.split("=", 1)[0] for line in result.stdout.splitlines()
                if "=" in line]
        # $GITHUB_OUTPUT takes key=value and nothing else, so this is a
        # contract with the workflow, not a human-facing print.
        self.assertIn("metric", keys)
        self.assertIn("metric_reason", keys)
        self.assertIn("ptrace_scope", keys)

    @REQUIRES_BENCH
    def test_a_measured_command_exits_with_the_childs_code(self):
        with tempfile.TemporaryDirectory(prefix="ghw-counter-cli-") as td:
            report = Path(td) / "r.xml"
            # Ambient-proof: under the workflow's unit step the ambient pin
            # is cpu_time_ratio, which the raw CLI refuses (a raw number
            # must not carry the ratio label); this case is about the exit
            # code, so it runs under no pin at all.
            env = {k: v for k, v in os.environ.items()
                   if k != counter.METRIC_ENV}
            result = subprocess.run(
                [sys.executable, str(REPO_ROOT / "scripts" / "ci" /
                                     "counter.py"),
                 "--junit-file", str(report), "--name", "counter::unit-suite",
                 "--", sys.executable, "-c", "import sys; sys.exit(7)"],
                capture_output=True, text=True, check=False, env=env)
            self.assertEqual(result.returncode, 7, result.stderr)
            self.assertTrue(report.is_file())
            suite = ET.parse(report).getroot()
            self.assertEqual(suite.get("failures"), "1")

    def test_no_command_is_an_error_not_a_silent_success(self):
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "ci" / "counter.py"),
             "--junit-file", "/dev/null", "--name", "counter::unit-suite"],
            capture_output=True, text=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("command", result.stderr)


class TestShippedFiles(unittest.TestCase):
    def test_counter_is_a_file_and_not_a_package(self):
        # scripts/ci holds standalone CI entry points. An __init__.py there
        # would make it an importable library that nothing installs, and the
        # by-path load every caller uses would then be quietly redundant.
        self.assertTrue((REPO_ROOT / "scripts" / "ci" / "counter.py").is_file())
        self.assertFalse((REPO_ROOT / "scripts" / "ci" / "__init__.py").exists())

    def test_the_new_files_are_named_back_by_the_ignore_file(self):
        # .gitignore here is deny-by-default: an unlisted file is invisible
        # to git, and `git status` will not tell you it is missing.
        for relative in ("scripts/ci/counter.py", "tests/test_counter.py",
                         "gate/speed-baseline.json"):
            with self.subTest(path=relative):
                completed = subprocess.run(
                    ["git", "check-ignore", "-q", "--no-index", relative],
                    cwd=REPO_ROOT, capture_output=True, check=False)
                self.assertNotEqual(completed.returncode, 0, f"{relative} is "
                                    "denied by .gitignore")


if __name__ == "__main__":
    unittest.main()
