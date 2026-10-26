#!/usr/bin/env python3
"""What did that command cost, in a quantity runner load cannot move?

THE QUESTION THIS FILE ANSWERS. The speed job used to decide pass/fail from
wall-clock durations taken on a GitHub-hosted runner, which is a multi-tenant
VM: steal time, a neighbour, a different CPU model. The maintainer ruling
behind issue #81 is that paired wall-clock A/B is not a valid magnitude
ANYWHERE — not on a shared box, and not in one job on a CI runner. So the
quantity gated here is not an elapsed time.

THE INSTRUMENT IS A DETERMINISTIC STATEMENT COUNT. It counts
`sys.monitoring` LINE events in the measured Python child (Python 3.12+;
the speed job pins 3.13): every executed line of the child's own code fires
one event, so the same code with the same population executes the same lines
and returns the same count, on any runner, under any load. Measured on the
box that shipped this change (2026-10-26, this tree at 5c3b9d6, env pinned
per `child_environment`), three runs of the full unit suite read:

    99,222,446 / 99,222,443 / 99,220,993 events — spread 0.0015%
    missed: 0 on every run; the suite exited 0 every run

The count is the gated value; the overhead costs CI minutes, not
correctness. The statement callback's own dispatch was measured lighter
than the retired calibrated instrument's two-child protocol: an earlier
prototype measured the same suite at 99.66M events (figures in issue #269),
0.44% above this shipped bootstrap — the bootstrap's own lines are counted
too, so its exact shape moves the number by that much.

WHAT THE COUNT DOES NOT SEE, STATED RATHER THAN HIDDEN. The monitoring
callback fires in the measured child itself, so the count covers the direct
child's own executed lines. Nested Python children (unittest spawns none in
this suite's measured path) and non-Python subprocesses — the renderers'
`git` calls — are not counted. A commit that moved all the renderers' work
into git would show the renderer entry flat; that is what the wall smoke
bound is for. Not a wall or CPU quantity, so neither machine factor nor
load reaches it: this is the property five instruments lost to (#190,
#216, #221, #240, #269 each red main on unchanged code, the last at
+43.4% over budget), and it is why the count is the gate's value.

THE RETIRED CALIBRATED INSTRUMENT. Between #190 and #240 the unit suite was
judged on its raw CPU seconds divided by an adjacent fixed synthetic
workload's. No scalar calibration cancels a heterogeneous runner pool: the
net value still scales with the machine factor whenever the slope is not
exactly the local ratio (d(raw)/d(calibration) measured ~30.1 between two
#269 runs against the committed 21.3716). It was retired by #269; issue
#269 and the baseline's `basis` carry the account. CALIBRATED_METRIC stays
declared in METRICS so schema 3/4 baseline documents still validate against
`metric in counter.METRICS`, and nothing produces it any more.

THIS MODULE IS POSIX-ONLY FOR CPU SECONDS, AND IT REFUSES RATHER THAN
GUESSES. `resource` does not exist on Windows and `/proc` does not exist on
macOS, and this module is imported by scripts/ci/compare_counters.py, which
the test suite exercises on every OS — so a bare `import resource` turned a
platform limitation into an import error in thirty-five unrelated tests, on
a repository whose own CI runs a windows-latest and macos-latest matrix.

So: the module IMPORTS everywhere. `measure()` on a platform with no
RUSAGE_CHILDREN raises CounterError naming the platform, rather than
returning a number that would be a guess. The probe still produces its
output there, and says that the kernel settings it would normally report
are unreadable on this platform — "I looked and there was nothing there"
is a real record, and it is a different record from "the file exists and
says 4". Nothing here falls back to wall time, because a silent fallback
that measures something else is precisely the failure this instrument
exists to prevent. (`measure_statement` needs no `resource` at all — the
callback runs in the child — but it does need Python 3.12+.)

IT IS THE CPU PATH THAT IS NOT DETERMINISTIC, AND THE GATE IS WEAKER FOR
EVERY ENTRY IT STILL JUDGES ON IT. That is a real cost and it is named
rather than buried: measured on this box, the same program measured
repeatedly spreads 21.3% over six runs (unit suite) and 10.8%, 35.2% and
47.3% over eight runs each (the three renderer workloads, the brief ones
among the noisiest because fixed overhead and co-tenant load dominate a
short measurement). A quiet dedicated cell spreads far less — 7.6%
min-to-max for one workload, run 36812285024. The statement count is
deterministic; every entry is judged on it after main's ratchet seeds the
first measurements, and until then the populations are in a declared blind
window. The gross wall smoke bound is unchanged.

THE TWO BETTER CPU INSTRUMENTS, AND WHY NEITHER IS HERE. Both were
measured, not assumed, and both are recorded here so the next reader does
not re-derive them — and so nobody adds one back without also re-deriving
what it costs.

  Instruction counts (`perf stat -e instructions`) — UNAVAILABLE. On
  ubuntu24 image 20260927.320.1, kernel 6.17.0-1022-azure (run 36811152307)
  perf_event_paranoid is 4, `perf` IS installed, and it still exits 255 with
  "Access to performance monitoring and observability operations is limited."

  Syscall counts (`strace -f -c`) — AVAILABLE, DETERMINISTIC, AND NOT
  AFFORDABLE. On that same cell three runs of one workload reported the same
  total every time (870 calls, 89 errors; run 36812466498) while that
  report's own timing columns swung between 17.34% and 48.84%. Its overhead
  is the problem: 0.24-0.25 s against 0.19 s untraced on that small workload,
  but tracing overhead scales with syscall count, and over the real renderer
  workloads it measured 64830/68057/75870 ms per bench round against
  5053/5684/6215 ms untraced — 12.8x, landing almost entirely on
  render-impact.py, which shells out to git. On a cell whose suite round is
  ~20 s, 12.8x on two bench rounds costs minutes to save tens of seconds.

The statement count needs neither kernel facility — the callback runs
inside the child it counts — which is why it can be deterministic where
both of those lose.

ONE PROBE PER JOB, NOT PER CALL SITE. The workflow runs the probe once, in
its own step, and exports the result as GH_WIDGETS_GH_COUNTER_METRIC; the
steps that measure pin the same value again explicitly. Two call sites in
one comparison that picked different instruments would produce two
incomparable numbers.

THE PROBE RECORDS WHY THERE IS NO BETTER CPU INSTRUMENT. It reports the
metric in use together with perf_event_paranoid and yama ptrace_scope — the
two kernel settings that decide whether instruction or syscall counting is
even possible — so a reader can see that the alternatives were measured on
this cell and not an oversight. Existence of a binary proves nothing: `perf`
is installed on the runner and cannot count.

A METRIC MISMATCH IS A REFUSAL, NOT A COMPARISON. Comparing a counter
recorded under one instrument against a baseline recorded under another is a
category error that reads as either a catastrophic regression or a
spectacular speedup. The comparator raises ComparisonError, which is exit 2
and names both; nothing here silently converts one into the other.

THE TIME ATTRIBUTE IN THE JUNIT THIS WRITES IS NOT SECONDS. It is the
instrument's counter — a statement count, or CPU seconds on the raw path.
That is still not the quantity a reader assumes on first sight, so: a
`time="3.2"` written by this file means three-point-two of the named
instrument's unit and never three-point-two seconds of elapsed time.
Elapsed time is recorded separately, as gh-wall, and exists only for the
gross smoke gate — never as a magnitude.

DETERMINISM LEVERS, ALL OF THEM NAMED IN `child_environment`. The headline
one is PYTHONHASHSEED=0, but the one that actually moved the numbers was
PYTHONDONTWRITEBYTECODE=1: without it the first round compiles and writes a
.pyc and the second reads them, so two rounds of identical code do different
amounts of work — which is exactly the kind of split the statement count
would see as two different populations, so it stays pinned. None of this
makes the CPU path deterministic and nothing here pretends that it does.

IT ALSO OWNS THE POPULATION. `population_digest` and `collect_node_ids` are
here rather than in the comparator because a digest computed two different
ways by two tools compares unequal forever, and the resulting refusal would
be indistinguishable from a real population change. The collector is a
COLLECTION pass, not a test run.

WHY THERE IS A --cwd FLAG. It exists because of a defect this file could
not have caused on its own and could not have prevented: the unit-suite
measurement ran `python3 -m unittest discover` from the workflow's working
directory, which is the PARENT of the checkout, so discovery found no tests,
exited 5, and the counter dutifully measured 0.05 CPU seconds of interpreter
startup. The step immediately before it had collected 724 node ids from the
checkout, so one job collected a 724-test population and measured none of it.
The instrument did its job; the question it was asked was the wrong one.
Naming the directory the command runs in makes that question explicit
rather than implicit in a shell's cwd. Two tests cover it — one on
`measure()` directly, one through this CLI — because a re-review once
mutated the flag into a no-op and the whole suite stayed green: every
step-level test points its `working-directory` and its `--cwd` at the same
directory, so from there an ignored flag is invisible.

CLI:

    counter.py --probe
    counter.py --junit-file <path> --name <node-id> --cwd <dir> -- <cmd> ...
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import NamedTuple, Optional, Sequence

try:
    import resource
except ImportError:  # POSIX only. Windows has no RUSAGE_CHILDREN, and this
    # module is imported by the comparator on every OS, so the absence is
    # recorded here and refused at the call site rather than swallowed.
    resource = None  # type: ignore[assignment]


CPU_METRIC = "cpu_time"
# Retired by #269 (see the docstring's calibrated section). Declared so
# schema 3/4 documents keep validating against `metric in counter.METRICS`;
# `measure()` refuses it, and no producer writes it any more.
CALIBRATED_METRIC = "cpu_time_ratio"
# The gated instrument: a deterministic count of the child's executed lines.
STATEMENT_METRIC = "statement_count"
# The metric names this file is willing to record. Anything else arriving in
# METRIC_ENV is a stale export from a shape this file has left behind, and it is refused rather than measured — a number recorded under a name this file does not define is the exact failure the baseline's metric check exists to catch, and it should never get that far.
METRICS = (CPU_METRIC, CALIBRATED_METRIC, STATEMENT_METRIC)
METRIC_ENV = "GH_WIDGETS_GH_COUNTER_METRIC"
# Read by the probe and printed next to the instrument, because a reader
# deciding whether to trust a fallback needs to know whether it was the kernel
# that forbade the primary, not whether the binary was missing.
PARANOID_PATH = Path("/proc/sys/kernel/perf_event_paranoid")
PTRACE_SCOPE_PATH = Path("/proc/sys/kernel/yama/ptrace_scope")
# A trivial command for the probe to count. `/bin/true` is not guaranteed to
# exist everywhere and is not a Python program; `-c pass` starts the
# interpreter, which is the closest available stand-in for "some real work"
# without depending on anything.
PROBE_COMMAND = (sys.executable, "-c", "pass")
PROBE_TIMEOUT = 60

# THE STATEMENT BOOTSTRAP. This is the program the measured child actually
# runs: it counts its own LINE events and writes the total where the parent
# can read it, then dispatches the real subject exactly the way an
# interpreter would. Tool id 5 is arbitrary but fixed; "ghw-speed" names the
# tool in sys.monitoring's internal registry, and a collision would raise at
# use_tool_id, not mislabel the count.
#
# Dispatch mirrors the interpreter's three shapes, because the measured
# commands are exactly those shapes: `-m unittest discover` for the suite,
# a script path for the renderers, `-c` for anything inline. A leading `--`
# is stripped so the parent can spell `[python, "-c", BOOTSTRAP, "--",
# *command]` unambiguously. SystemExit propagates on purpose: atexit runs
# for SystemExit, so a `sys.exit(1)` from the subject still writes its count
# and the parent sees both the count and the verdict.
#
# Not counted: lines executed inside nested Python children and non-Python
# subprocesses (git), because the callback lives in THIS process only.
_STATEMENT_BOOTSTRAP = """\
import atexit, json, os, runpy, sys

_COUNT_FILE = os.environ["GH_WIDGETS_STATEMENT_COUNT_FILE"]
_counted = 0
_missed = 0


def _record():
    with open(_COUNT_FILE, "w", encoding="utf-8") as handle:
        json.dump({"count": _counted, "missed": _missed}, handle)


def _count_line(code, instruction_offset):
    global _counted
    _counted += 1


def _count_missed(code, instruction_offset):
    global _missed
    _missed += 1


_events = sys.monitoring.events
sys.monitoring.use_tool_id(5, "ghw-speed")
_watched = _events.LINE
_missed_event = getattr(_events, "MISSED", None)
if _missed_event is not None:
    _watched |= _missed_event
sys.monitoring.set_events(5, _watched)
sys.monitoring.register_callback(5, _events.LINE, _count_line)
if _missed_event is not None:
    sys.monitoring.register_callback(5, _missed_event, _count_missed)
atexit.register(_record)

argv = sys.argv[1:]
if argv[:1] == ["--"]:
    del argv[:1]
if not argv:
    raise SystemExit("statement bootstrap: no target to run")
_target, *_rest = argv
if _target == "-m":
    if not _rest:
        raise SystemExit("statement bootstrap: -m needs a module name")
    sys.argv = [_rest[0], *_rest[1:]]
    runpy.run_module(_rest[0], run_name="__main__", alter_sys=True)
elif _target == "-c":
    if not _rest:
        raise SystemExit("statement bootstrap: -c needs a program")
    sys.argv = ["-c", *_rest[1:]]
    exec(compile(_rest[0], "<cmdline -c>", "exec"),
         {"__name__": "__main__", "__file__": "<cmdline -c>"})
else:
    sys.argv = [_target, *_rest]
    sys.path.insert(0, os.path.dirname(os.path.abspath(_target)))
    runpy.run_path(_target, run_name="__main__")
"""


class CounterError(RuntimeError):
    """A cost could not be counted, or not in the instrument asked for."""


class Measurement(NamedTuple):
    """One measured command: the counter, what kind it is, and the wall time.

    The counter and its metric travel together on purpose. A caller that can
    record a number without recording what it measured can produce a baseline
    that looks comparable and is not, which is the mistake this issue exists
    to make impossible.
    """
    value: float
    metric: str
    wall: float
    stdout: str
    stderr: str
    returncode: int


def _run_child(command, cwd, env, timeout):
    """Run one child, returning (returncode, stdout, stderr, wall, cpu).

    A hand-rolled Popen rather than `subprocess.run` for exactly one reason:
    the renderers shell out to `git`, so a timeout that killed only the
    direct child would orphan the real work and leave it running with its
    output pipes held open — the timeout would then never return at all, and
    the gate would hang rather than fail. The child is therefore started in
    its own session and the whole process group is killed, which is why this
    is not `subprocess.run`. The traced instruments needed it too (the direct
    child was `strace` and the command was its grandchild), which is where
    the requirement came from; the requirement outlived the instrument.
    """
    started = time.perf_counter()
    before = _child_cpu_seconds()
    with subprocess.Popen(
            list(command), cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_group(process)
            stdout, stderr = process.communicate()
            raise subprocess.TimeoutExpired(
                command, timeout, output=stdout, stderr=stderr) from None
        returncode = process.returncode
    return (returncode, stdout or "", stderr or "",
            time.perf_counter() - started, _child_cpu_seconds() - before)


def _kill_process_group(process) -> None:
    """SIGKILL the child's whole process group, not just the direct child."""
    if hasattr(os, "killpg"):
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            return
        except OSError:
            pass
    try:
        process.kill()
    except OSError:
        pass


def bench_is_runnable() -> bool:
    """Whether this platform can run the bench harness AT ALL.

    The single answer, kept next to the refusal rather than restated per test
    file. Three Windows rounds failed on the same cause — the harness
    refusing to measure and every assertion downstream reporting the refusal
    wearing a costume — because each round guarded the files that happened to
    be in the log. A test written tomorrow must INHERIT this answer, not
    rediscover it on a runner.

    It is deliberately narrow. It says nothing about whether the gate is
    armed, whether a baseline is present, or whether anything is correct: it
    answers one question, and a test asserting it is TRUE wherever the
    harness can run is what stops it becoming a way to make CI green.
    """
    return resource is not None


BENCH_RUNNABLE = bench_is_runnable()


def _child_cpu_seconds() -> float:
    """CPU seconds this process has spent in children it has already reaped.

    `resource.getrusage(RUSAGE_CHILDREN)` is a running total, so a DELTA
    across one child is the child's cost. That is only true because this
    module runs its children one at a time and reaps nothing else in
    between — two concurrent callers would each be charged for both
    children. The alternative, `time.process_time()`, measures THIS
    interpreter's CPU and would report close to nothing for a subprocess,
    which is exactly the number that would then be compared against the
    baseline.
    """
    if resource is None:
        raise CounterError(
            "cpu_time is measured with resource.getrusage(RUSAGE_CHILDREN), "
            f"which {sys.platform} does not provide. This instrument is "
            "POSIX-only: it will not fall back to wall time, because a "
            "number that quietly measures something else is worse than no "
            "number.")
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def child_environment(env=None) -> dict:
    """The child environment, with every variance source found pinned.

    Each of these was pinned for a stated reason, and none of them makes wall
    time deterministic — they remove sources of drift from the CPU COUNTER,
    which is a different and much smaller claim:

      PYTHONHASHSEED=0       hash-order drift, which reaches the work
                             wherever a set or dict is iterated.
      PYTHONDONTWRITEBYTECODE=1
                             the big one. Without it, round 1 of a run
                             compiles every module and writes a .pyc while
                             round 2 reads them — so the two rounds do
                             measurably different amounts of work on
                             identical code, and the second looks cheaper for
                             a reason that has nothing to do with the commit.
                             The minimum across rounds is taken, so it would
                             be defensible; but a baseline re-derived on a
                             cold tree and one measured on a warm one would
                             not be the same number at all.
      LC_ALL=C, LANG=C      locale-dependent sorting and formatting. Not a
                             run-to-run source on one cell, but it is a
                             cell-to-cell one, and this baseline is meant to
                             travel between them.
      TZ=UTC                the renderers stamp their output; a runner whose
                             clock zone differs would produce different work
                             and different bytes for the same commit.
    """
    child = dict(os.environ if env is None else env)
    child["PYTHONHASHSEED"] = "0"
    child["PYTHONDONTWRITEBYTECODE"] = "1"
    child["LC_ALL"] = "C"
    child["LANG"] = "C"
    child["TZ"] = "UTC"
    return child


def choose_metric(forced: Optional[str] = None) -> str:
    """The instrument for this job, from the env var or the default.

    `forced` is the workflow's one-probe-per-job answer; it is honoured
    verbatim, so every call site in a job measures the same quantity. The
    workflow pins statement_count; without a pin the raw CPU path applies.
    """
    if forced is None:
        forced = os.environ.get(METRIC_ENV) or None
    if forced:
        if forced not in METRICS:
            raise CounterError(
                f"{METRIC_ENV}={forced!r} is not one of "
                + ", ".join(METRICS) + "; refusing to measure under a name "
                "this file does not define")
        return forced
    return CPU_METRIC


def measure(command: Sequence[str], cwd=None, env=None, timeout=None,
            metric: Optional[str] = None) -> Measurement:
    """Run one command and return its raw CPU counter, metric and wall.

    Raises CounterError when the instrument cannot produce a number at all,
    subprocess.TimeoutExpired on a timeout, and OSError when the command
    cannot be started — the same trio the caller already handles for a plain
    `subprocess.run`, so nothing downstream grows a new failure path.
    """
    chosen = choose_metric(metric)
    if chosen != CPU_METRIC:
        # A label this function cannot earn: raw CPU seconds carrying the
        # ratio or the statement name would pass the comparator's metric
        # check while measuring something else. Each name belongs to the
        # instrument that produces it.
        raise CounterError(
            f"{chosen} is not the raw path's label; the raw path records "
            f"{CPU_METRIC} only. Pass metric={CPU_METRIC!r} or none, or "
            "call the instrument that owns the name.")
    code, out, err, wall, cpu = _run_child(
        list(command), cwd, child_environment(env), timeout)
    return Measurement(cpu, chosen, wall, out, err, code)


def _read_statement_file(path: Path) -> tuple:
    """Parse and vet the count file the bootstrap's atexit handler wrote.

    Every bad shape is a CounterError, never a number: a count this file
    cannot vouch for is a guess, and a guess under a metric name is exactly
    the failure the metric check exists to catch.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CounterError(
            f"the statement count file {path} was never written; the child "
            "died before its atexit handler ran, so there is no "
            "measurement to record") from None
    except (OSError, ValueError) as exc:
        raise CounterError(
            f"the statement count file {path} is not readable JSON: "
            f"{exc}") from exc
    if not isinstance(payload, dict) or "count" not in payload:
        raise CounterError(
            f"the statement count file {path} does not carry a count")
    count = payload["count"]
    missed = payload.get("missed", 0)
    for name, value in (("count", count), ("missed", missed)):
        # `bool` is an int in Python; True is not a count.
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise CounterError(
                f"the statement count file {path} carries {name}="
                f"{value!r}, which is not a natural number")
    if missed > 0:
        raise CounterError(
            f"the statement count file {path} records {missed} missed "
            "event(s); the count is an undercount, and an undercount is "
            "not a measurement this gate can fail against")
    return count, missed


def _is_python_interpreter(name: str) -> bool:
    """Whether `name` names a Python interpreter the bootstrap can wrap."""
    return name == sys.executable or os.path.basename(name).startswith(
        "python")


def measure_statement(command: Sequence[str], cwd=None, env=None,
                      timeout=None) -> Measurement:
    """Count the child's executed lines and return the count as the value.

    The child is `[sys.executable, "-c", _STATEMENT_BOOTSTRAP, "--",
    *command]`: the bootstrap registers a `sys.monitoring` LINE callback on
    itself, dispatches `command[1:]` the way an interpreter would (its
    `command[0]` must BE an interpreter), and writes the total to a temp
    file at exit. Session/process-group semantics are the raw path's, so a
    timeout still kills the whole tree.

    Requires Python 3.12+ (`sys.monitoring`), which the speed job pins.
    Raises the same trio `measure` raises — CounterError, TimeoutExpired,
    OSError.
    """
    command = list(command)
    if sys.version_info < (3, 12):
        raise CounterError(
            "measure_statement counts sys.monitoring LINE events, which "
            f"{sys.version_info[0]}.{sys.version_info[1]} does not have; "
            "they exist from Python 3.12, and the speed job pins 3.13")
    if not command or not _is_python_interpreter(command[0]):
        raise CounterError(
            f"measure_statement counts the Python child's executed lines, "
            f"so command[0] must be a Python interpreter, not "
            f"{command[0] if command else '(nothing)'!r}")
    count_fd, count_name = tempfile.mkstemp(
        prefix="ghw-statement-", suffix=".json")
    os.close(count_fd)
    os.unlink(count_name)
    # Deleted, not truncated: a path that exists empty would let a child
    # that wrote nothing read as {"count": 0} if this side ever changed to
    # tolerate an empty file. Absent means unwritten; that is the contract.
    count_path = Path(count_name)
    child_env = child_environment(env)
    child_env["GH_WIDGETS_STATEMENT_COUNT_FILE"] = str(count_path)
    try:
        code, out, err, wall, _cpu = _run_child(
            [sys.executable, "-c", _STATEMENT_BOOTSTRAP, "--", *command[1:]],
            cwd, child_env, timeout)
        count, _missed = _read_statement_file(count_path)
    finally:
        if count_path.exists():
            count_path.unlink()
    return Measurement(float(count), STATEMENT_METRIC, wall, out, err, code)


def population_digest(node_ids) -> str:
    """A stable digest of a test population: sha256 over the sorted ids.

    Sorted first, so the digest describes the SET of node ids and not the
    order a collector happened to walk them in. Newline-joined and
    newline-terminated, so an id containing a newline cannot be made to
    collide with a different pair of ids.
    """
    joined = "\n".join(sorted(node_ids)) + "\n"
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def collect_node_ids(directory: Path) -> list:
    """The test node ids a discovery pass collects under `directory`.

    A COLLECTION pass, not a test run: `unittest.TestLoader().discover`
    imports every test module and builds the suite but executes nothing. The
    expensive half — running the tests — is what the caller already did.
    """
    loader = unittest.TestLoader()
    try:
        top_level = directory.parent if (directory / "__init__.py").is_file() else directory
        discovered = loader.discover(str(directory),
                                     top_level_dir=str(top_level))
    except Exception as exc:  # pylint: disable=broad-exception-caught
        raise CounterError(
            f"{directory} could not be collected, so the population digest "
            f"cannot be computed: {exc}") from exc
    found = []

    def walk(item):
        for thing in item:
            if isinstance(thing, unittest.TestSuite):
                walk(thing)
            else:
                found.append(thing.id())

    walk(discovered)
    if not found:
        raise CounterError(
            f"{directory} collected no tests, which is not a population")
    return sorted(set(found))


def _node_parts(node_id: str):
    """Split `class::name` the way the comparator reconstructs it."""
    classname, separator, name = node_id.partition("::")
    if not separator or not classname or not name:
        raise CounterError(
            f"--name {node_id!r} is not a `class::name` node id; the "
            "comparator rebuilds it from those two halves")
    return classname, name


def write_junit(path: Path, node_id: str, measurement: Measurement,
                command=None) -> None:
    """One testcase whose `time` is the COUNTER, not seconds. Read the module
    docstring before believing the attribute.

    A non-zero exit is recorded as a JUnit failure, which is what makes a
    failed run fall out of the comparator's intersection instead of being
    compared as though it had succeeded.
    """
    classname, name = _node_parts(node_id)
    suite = ET.Element("testsuite", {
        "name": "counter",
        "tests": "1",
        "failures": "1" if measurement.returncode else "0",
        "errors": "0",
        "skipped": "0",
        # NOT SECONDS. See the module docstring.
        "time": f"{measurement.value:.6f}",
        "gh-metric": measurement.metric,
    })
    case = ET.SubElement(suite, "testcase", {
        "classname": classname,
        "name": name,
        "time": f"{measurement.value:.6f}",
        "gh-wall": f"{measurement.wall:.6f}",
    })
    if measurement.returncode:
        failure = ET.SubElement(case, "failure", {
            "message": f"command exited with code {measurement.returncode}",
            "type": "CommandFailure",
        })
        failure.text = (f"command: {' '.join(command) if command else '?'}\n"
                        f"stdout:\n{measurement.stdout}\n"
                        f"stderr:\n{measurement.stderr}")
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


def _probe_lines() -> list:
    """The `key=value` lines the workflow's probe step writes to $GITHUB_OUTPUT.

    The gate's instrument is chosen here, so the record names it — and it
    prints the two kernel settings that decide whether the CPU-side
    deterministic counters are even possible on this cell:
    perf_event_paranoid for instruction counts, yama ptrace_scope for
    syscall counts. A reader can then see why the count is taken inside the
    child instead: both alternatives were measured and unavailable-or-too-
    expensive, rather than assumed away.
    """
    return [
        f"metric={STATEMENT_METRIC}",
        "metric_reason=the gate counts " + STATEMENT_METRIC + ": "
        "sys.monitoring LINE events in the measured child, deterministic "
        "for identical code; CPU seconds remain the raw fallback and the "
        "CPU-side deterministic counters stay out — instruction counts are "
        "refused by this kernel's perf_event_paranoid and syscall counts "
        "cost 12.8x the workload they measure",
        f"perf_event_paranoid={_setting(PARANOID_PATH)}",
        f"ptrace_scope={_setting(PTRACE_SCOPE_PATH)}",
    ]


def _setting(path: Path) -> str:
    """A kernel setting's value, or what it is that there isn't one.

    Read defensively rather than required to exist: `/proc` is a Linux
    facility and this repository runs its suite on macOS and Windows, where
    a probe step that raised would fail a run that had nothing wrong with
    it. The distinction the value carries — a number, or "unavailable on
    this platform" — is part of the record the probe exists to make.
    """
    try:
        return path.read_text(encoding="utf-8").strip() or "unreadable"
    except OSError:
        return "unavailable on this platform"


def main(argv: Optional[list] = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--probe", action="store_true",
                        help="print `metric=` / `metric_reason=` lines for "
                             "$GITHUB_OUTPUT and exit, without measuring "
                             "anything")
    parser.add_argument("--junit-file", type=Path,
                        help="write a one-testcase JUnit suite here whose "
                             "time attribute is the COUNTER, not seconds")
    parser.add_argument("--name", metavar="NODE-ID",
                        help="node id for the testcase, e.g. "
                             "counter::unit-suite")
    parser.add_argument("--timeout", type=float, default=None,
                        help="seconds before the child's process group is "
                             "killed (default: none)")
    parser.add_argument("--cwd", type=Path, default=None, metavar="DIR",
                        help="directory to run the command in. Stated "
                             "explicitly rather than left to the caller's "
                             "working directory: a discovery command run one "
                             "level above the checkout silently measures "
                             "nothing, and reports no error while doing it")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="-- <command> [args...]")
    args = parser.parse_args(argv)

    if args.probe:
        for line in _probe_lines():
            print(line)
        return 0

    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("no command given; put it after `--`")
    if not args.junit_file or not args.name:
        parser.error("--junit-file and --name are both required")

    try:
        # The CLI is the workflow's entry point, so the job's one probe
        # selects the instrument here too: statement_count is
        # measure_statement, anything else is the raw CPU path. A pin the
        # CLI ignored would exit 2 on the very step that pinned it — and a
        # pin silently ignored would be worse.
        measurement = (
            measure_statement(command, cwd=args.cwd, timeout=args.timeout)
            if choose_metric() == STATEMENT_METRIC
            else measure(command, cwd=args.cwd, timeout=args.timeout))
    except CounterError as exc:
        print(f"counter: {exc}", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired as exc:
        print(f"counter: {command[0]} exceeded its timeout", file=sys.stderr)
        if exc.stdout:
            sys.stdout.write(_decode(exc.stdout))
        if exc.stderr:
            sys.stderr.write(_decode(exc.stderr))
        return 124
    except OSError as exc:
        print(f"counter: could not run {command[0]}: {exc}", file=sys.stderr)
        return 127

    write_junit(args.junit_file, args.name, measurement, command)
    if measurement.stdout:
        sys.stdout.write(measurement.stdout)
    if measurement.stderr:
        sys.stderr.write(measurement.stderr)
    print(f"counter: {args.name} = {measurement.value:.6f} "
          f"{measurement.metric}")
    return measurement.returncode


def _decode(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


if __name__ == "__main__":
    sys.exit(main())
