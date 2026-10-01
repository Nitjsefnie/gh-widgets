"""Tests for the counter that replaced wall-clock durations in speed.yml.

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
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent


def load_counter():
    """Import scripts/ci/counter.py by path — scripts/ci is not a package."""
    path = REPO_ROOT / "scripts" / "ci" / "counter.py"
    spec = importlib.util.spec_from_file_location("ghw_counter_under_test",
                                                  path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


counter = load_counter()


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


class TestCwdIsHonoured(unittest.TestCase):
    """`--cwd` must reach `measure()`, not just parse.

    A re-review mutated `main()` into `measure(command, cwd=None, ...)` —
    the flag accepted, stored and silently ignored — and the whole suite
    stayed green, because every step-level test points its `working-directory`
    and its `--cwd` at the same directory, so an ignored flag is invisible
    from there. This is the tool-level control that mutation defeated, and it
    is the only place the two can be told apart.
    """

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

    def test_the_cli_passes_the_flag_through(self):
        # The library is only half of it: the CLI is what the workflow calls,
        # and a flag wired to nothing there is the same defect one layer up.
        with tempfile.TemporaryDirectory(prefix="ghw-cwd-cli-") as td:
            root = Path(td)
            (root / "marker-only-here.txt").write_text("x", encoding="utf-8")
            report = Path(td) / "r.xml"
            result = subprocess.run(
                [sys.executable,
                 str(REPO_ROOT / "scripts" / "ci" / "counter.py"),
                 "--junit-file", str(report),
                 "--name", "counter::unit-suite",
                 "--cwd", str(root),
                 "--", sys.executable, "-c",
                 "import os; print(sorted(os.listdir('.')))"],
                capture_output=True, text=True, check=False)
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
        self.assertEqual(counter.METRICS, (counter.CPU_METRIC,))

    def test_an_environment_naming_the_removed_instrument_is_refused(self):
        with mock.patch.dict(os.environ, {"GH_COUNTER_METRIC": "syscalls"}):
            with self.assertRaises(counter.CounterError) as caught:
                counter.choose_metric()
        self.assertIn("syscalls", str(caught.exception))

    def test_the_refusal_survives_the_public_measure_call(self):
        with mock.patch.dict(os.environ, {"GH_COUNTER_METRIC": "syscalls"}):
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


class TestMetricChoice(unittest.TestCase):
    def test_the_jobs_export_is_honoured_verbatim(self):
        # The workflow pins GH_COUNTER_METRIC explicitly in both measurement
        # steps rather than relying on a fallback, so the instrument a
        # population is judged by is visible in the workflow rather than
        # buried here. This is what that pin resolves to.
        with mock.patch.dict(os.environ, {"GH_COUNTER_METRIC": "cpu_time"}):
            self.assertEqual(counter.choose_metric(), counter.CPU_METRIC)
        self.assertEqual(counter.choose_metric("cpu_time"), counter.CPU_METRIC)

    def test_an_unrecognised_export_is_refused(self):
        with self.assertRaises(counter.CounterError) as caught:
            counter.choose_metric("furlongs")
        self.assertIn("cpu_time", str(caught.exception))

    def test_the_only_instrument_is_cpu_time(self):
        self.assertEqual(counter.choose_metric(), counter.CPU_METRIC)

    def test_it_needs_no_probe_to_get_there(self):
        # There is nothing to choose, so nothing is probed: an instrument
        # this file cannot use is not a reason to spend a process on every
        # call site.
        with mock.patch.object(counter, "_probe_lines") as probe:
            self.assertEqual(counter.choose_metric(), counter.CPU_METRIC)
        probe.assert_not_called()


class TestProbe(unittest.TestCase):
    def test_probe_lines_name_the_instrument_and_its_reason(self):
        with mock.patch.object(counter, "_setting", return_value="4"):
            lines = counter._probe_lines()  # pylint: disable=protected-access
        joined = "\n".join(lines)
        self.assertIn("metric=cpu_time", joined)
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
    def test_cpu_time_is_measured_not_guessed(self):
        measured = counter.measure(
            [sys.executable, "-c", "sum(range(200000))"],
            metric=counter.CPU_METRIC)

        self.assertGreater(measured.value, 0.0)
        self.assertEqual(measured.metric, counter.CPU_METRIC)
        self.assertEqual(measured.returncode, 0)
        self.assertGreater(measured.wall, 0.0)

    def test_the_instrument_travels_with_the_number(self):
        # A caller that can record a value without recording what measured
        # it can write a baseline that looks comparable and is not.
        value, metric = counter.measure(
            [sys.executable, "-c", "pass"],
            metric=counter.CPU_METRIC)[:2]
        self.assertIsInstance(value, float)
        self.assertEqual(metric, counter.CPU_METRIC)

    def test_stdout_and_stderr_come_back_intact(self):
        measured = counter.measure(
            [sys.executable, "-c",
             "import sys; print('out'); sys.stderr.write('err')"],
            metric=counter.CPU_METRIC)
        self.assertEqual(measured.stdout.strip(), "out")
        self.assertEqual(measured.stderr.strip(), "err")

    def test_pyhashseed_is_pinned_in_the_child(self):
        # One source of counter drift removed. It does NOT make wall time
        # deterministic, and nothing below claims that it does.
        self.assertEqual(counter.child_environment()["PYTHONHASHSEED"], "0")

    def test_the_environment_caller_supplied_is_honoured(self):
        env = counter.child_environment({"MARKER": "kept"})
        self.assertEqual(env["MARKER"], "kept")
        self.assertEqual(env["PYTHONHASHSEED"], "0")

    @unittest.skipIf(sys.platform == "win32",
                     "process groups are a POSIX mechanism; the timeout "
                     "path runs only on ubuntu runners in production")
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

    def test_a_measured_command_exits_with_the_childs_code(self):
        with tempfile.TemporaryDirectory(prefix="ghw-counter-cli-") as td:
            report = Path(td) / "r.xml"
            result = subprocess.run(
                [sys.executable, str(REPO_ROOT / "scripts" / "ci" /
                                     "counter.py"),
                 "--junit-file", str(report), "--name", "counter::unit-suite",
                 "--", sys.executable, "-c", "import sys; sys.exit(7)"],
                capture_output=True, text=True, check=False)
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
        for relative in ("scripts/ci/counter.py", "test_counter.py",
                         "speed-baseline.json"):
            with self.subTest(path=relative):
                completed = subprocess.run(
                    ["git", "check-ignore", "-q", "--no-index", relative],
                    cwd=REPO_ROOT, capture_output=True, check=False)
                self.assertNotEqual(completed.returncode, 0, f"{relative} is "
                                    "denied by .gitignore")


if __name__ == "__main__":
    unittest.main()
