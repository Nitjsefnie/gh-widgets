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
import stat
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


class FakeTracer:
    """A `strace` on disk that writes whatever summary a test needs.

    Not a mock of the parser: this is a real executable, run the same way
    production runs it, so the test exercises the same path — Popen, a
    session of its own, and a report read out of the file `-o` named. It
    finds that file in its own argv rather than being told where it is,
    because that is how the real strace is told.
    """

    def __init__(self, directory: Path, report: str, returncode: int = 0):
        self.path = directory / "strace"
        self.path.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "args = sys.argv[1:]\n"
            "target = args[args.index('-o') + 1]\n"
            f"open(target, 'w').write({report!r})\n"
            f"sys.exit({returncode})\n", encoding="utf-8")
        self.path.chmod(self.path.stat().st_mode | stat.S_IEXEC)

    def installed(self, _name):
        """A stand-in for shutil.which, for this strace only."""
        return str(self.path)


class TestStraceParsing(unittest.TestCase):
    SUMMARY = ("% time     seconds  usecs/call     calls    errors syscall\n"
               "------ ----------- ----------- --------- --------- ----------------\n"
               " 49.29    0.041234           2         20           0 openat\n"
               " 17.34    0.014110           3         35           7 openat\n"
               "------ ----------- ----------- --------- --------- ----------------\n"
               "100.00    0.083631           1        870          89 total\n")

    def test_reads_the_total_row(self):
        self.assertEqual(counter.parse_strace_total(self.SUMMARY), 870.0)

    def test_it_ignores_a_per_syscall_row_that_says_total(self):
        # The word `total` is the last field, so only the aggregate row can
        # match; a per-syscall row with more trailing columns must not.
        report = ("% time seconds usecs/call calls errors syscall\n"
                  " 50.00 0.010000 1 870 89 total_extra\n"
                  "100.00 0.020000 1 444 63 total\n")
        self.assertEqual(counter.parse_strace_total(report), 444.0)

    def test_the_timing_columns_are_never_read(self):
        # The same report with wildly different timings must give the same
        # answer: those columns swung 17.34%-48.84% on identical runs while
        # the count did not move, and reading them would put the wall clock
        # back into a gate whose whole point is that it is gone.
        swung = self.SUMMARY.replace("0.083631", "0.900000")
        self.assertEqual(counter.parse_strace_total(swung), 870.0)

    def test_an_untraced_or_unreadable_report_is_none_not_zero(self):
        self.assertIsNone(counter.parse_strace_total(""))
        self.assertIsNone(counter.parse_strace_total("strace: Operation not"
                                                     " permitted\n"))
        self.assertIsNone(counter.parse_strace_total(
            "% time seconds usecs/call calls errors syscall\n"))
        self.assertIsNone(counter.parse_strace_total(
            "100.00    0.000000           0          0           0 total\n"))


class TestMetricChoice(unittest.TestCase):
    def test_the_jobs_export_wins_over_the_probe(self):
        # One probe per job: an exported metric is honoured verbatim, and
        # the probe is not even consulted. Two call sites that each probed
        # could pick different instruments in the same job, which is the
        # bug this file exists to prevent.
        with mock.patch.object(counter, "probe_strace") as probe:
            self.assertEqual(counter.choose_metric("cpu_time"), "cpu_time")
        probe.assert_not_called()

    def test_an_unrecognised_export_is_refused(self):
        with self.assertRaises(counter.CounterError) as caught:
            counter.choose_metric("furlongs")
        self.assertIn("cpu_time", str(caught.exception))

    def test_a_failing_probe_falls_through_to_cpu_time(self):
        with mock.patch.object(counter, "probe_strace", return_value=None):
            self.assertEqual(counter.choose_metric(), counter.CPU_METRIC)

    def test_a_working_probe_selects_syscalls(self):
        with mock.patch.object(counter, "probe_strace", return_value=1.0):
            self.assertEqual(counter.choose_metric(), counter.STRACE_METRIC)


class TestProbe(unittest.TestCase):
    def test_probe_lines_name_the_instrument_and_its_reason(self):
        with mock.patch.object(counter, "probe_strace", return_value=None), \
                mock.patch.object(counter.shutil, "which", return_value=None), \
                mock.patch.object(counter, "_setting", return_value="4"):
            lines = counter._probe_lines()  # pylint: disable=protected-access
        joined = "\n".join(lines)
        self.assertIn("metric=cpu_time", joined)
        # The reader of a fallback has to be able to judge it, which means
        # knowing what the kernel said and whether the tracer was present.
        self.assertIn("strace on PATH: no", joined)
        self.assertIn("ptrace_scope: 4", joined)


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
                     "the fake strace is a shebang script; on Windows the "
                     "block runs only on ubuntu runners in production")
    def test_the_syscall_instrument_is_measured_not_assumed(self):
        report = TestStraceParsing.SUMMARY.replace("870", "4242")
        with tempfile.TemporaryDirectory(prefix="ghw-counter-strace-") as td:
            fake = FakeTracer(Path(td), report)
            with mock.patch.object(counter.shutil, "which", fake.installed):
                measured = counter.measure(
                    [sys.executable, "-c", "pass"],
                    metric=counter.STRACE_METRIC)

        self.assertEqual(measured.metric, counter.STRACE_METRIC)
        self.assertEqual(measured.value, 4242.0)

    @unittest.skipIf(sys.platform == "win32",
                     "same reason as the fake-strace test above")
    def test_a_tracer_that_traces_nothing_is_a_failure_not_a_number(self):
        with tempfile.TemporaryDirectory(prefix="ghw-counter-notrace-") as td:
            fake = FakeTracer(Path(td), "strace: Operation not permitted\n",
                              returncode=1)
            with mock.patch.object(counter.shutil, "which", fake.installed):
                with self.assertRaises(counter.CounterError):
                    counter.measure([sys.executable, "-c", "pass"],
                                    metric=counter.STRACE_METRIC)

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
