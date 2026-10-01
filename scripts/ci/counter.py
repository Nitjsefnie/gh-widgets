#!/usr/bin/env python3
"""What did that command cost, in a quantity runner load cannot move?

THE QUESTION THIS FILE ANSWERS. speed.yml used to decide pass/fail from
wall-clock durations taken on a GitHub-hosted runner, which is a multi-tenant
VM: steal time, a neighbour, a different CPU model. The maintainer ruling
behind issue #81 is that paired wall-clock A/B is not a valid magnitude
ANYWHERE — not on a shared box, and not in one job on a CI runner. So the
quantity gated here is not an elapsed time.

ONE INSTRUMENT: CPU SECONDS.
`resource.getrusage(RUSAGE_CHILDREN)`, as a delta across one child. On-CPU
time, so steal time — the thing that makes a duration on a shared runner
meaningless — is excluded, which is what makes this valid under the ruling
at all.

IT IS NOT DETERMINISTIC, AND THE GATE IS WEAKER FOR IT. That is a real cost
and it is named rather than buried: measured on this box, the same program
measured repeatedly spreads 22.2% over six runs (unit suite) and 19.5-72.9%
over eight runs (the three renderer workloads, the short ones worst because
fixed overhead and co-tenant load dominate a brief measurement). A quiet
dedicated cell spreads far less — 7.6% min-to-max for one workload, run
36812285024 — but even there the budget has to be looser than a
deterministic counter would need. The budgets in the committed baseline are
per population for exactly this reason, and at its budget the unit-suite
half is a GROSS-REGRESSION NET, not a sensitive gate.

THE TWO BETTER INSTRUMENTS, AND WHY NEITHER IS HERE. Both were measured, not
assumed, and both are recorded here so the next reader does not re-derive
them — and so nobody adds one back without also re-deriving what it costs.

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

The standing rule this leaves: an instrument the cell cannot use does not
belong in the gate, and neither does one that costs more than the thing it
measures. Both were tried; both lost on numbers.

ONE PROBE PER JOB, NOT PER CALL SITE. The workflow runs the probe once, in
its own step, and exports the result as GH_COUNTER_METRIC; this file honours
that variable and does not have to be asked. Two call sites in one comparison
that picked different instruments would produce two incomparable numbers.

THE PROBE RECORDS WHY THERE IS NO BETTER INSTRUMENT. It reports the metric
in use together with perf_event_paranoid and yama ptrace_scope — the two
kernel settings that decide whether instruction or syscall counting is even
possible — so a reader can see that CPU seconds are a measured choice on
this cell and not an oversight. Existence of a binary proves nothing: `perf`
is installed on the runner and cannot count.

A METRIC MISMATCH IS A REFUSAL, NOT A COMPARISON. Comparing a counter
recorded under one instrument against a baseline recorded under another is a
category error that reads as either a catastrophic regression or a
spectacular speedup. The comparator raises ComparisonError, which is exit 2
and names both; nothing here silently converts one into the other.

THE TIME ATTRIBUTE IN THE JUNIT THIS WRITES IS NOT SECONDS. It is the CPU
seconds the child consumed. That is still not the quantity a reader assumes
on first sight, so: a `time="3.2"` written by this file means three-point-two
CPU seconds of on-CPU time and never three-point-two seconds of elapsed time.
Elapsed time is recorded separately, as gh-wall, and exists only for the
gross smoke gate — never as a magnitude.

DETERMINISM LEVERS, ALL OF THEM NAMED IN `child_environment`. The headline
one is PYTHONHASHSEED=0, but the one that actually moved the numbers was
PYTHONDONTWRITEBYTECODE=1: without it the first round compiles and writes a
.pyc and the second reads them, so two rounds of identical code do different
amounts of work. None of this makes the measurement deterministic and nothing
here pretends that it does.

IT ALSO OWNS THE POPULATION. `population_digest` and `collect_node_ids` are
here rather than in the comparator because a digest computed two different
ways by two tools compares unequal forever, and the resulting refusal would
be indistinguishable from a real population change. The collector is a
COLLECTION pass, not a test run.

CLI:

    counter.py --probe
    counter.py --junit-file <path> --name <node-id> -- <cmd> [args...]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import resource
import signal
import subprocess
import sys
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import NamedTuple, Optional, Sequence


CPU_METRIC = "cpu_time"
# The metric names this file is willing to record. There is exactly one, and
# that is not an accident of implementation: see the docstring for the two
# better instruments and the numbers that put them out. Anything else
# arriving in GH_COUNTER_METRIC is a stale export from a shape this file has
# left behind, and it is refused rather than measured — a number recorded
# under a name this file does not define is the exact failure the baseline's
# metric check exists to catch, and it should never get that far.
METRICS = (CPU_METRIC,)
METRIC_ENV = "GH_COUNTER_METRIC"
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
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def child_environment(env=None) -> dict:
    """The child environment, with every variance source found pinned.

    Each of these was pinned for a stated reason, and none of them makes wall
    time deterministic — they remove sources of drift from the COUNTER, which
    is a different and much smaller claim:

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
    """The instrument for this job, from the env var or from a real probe.

    `forced` is the workflow's one-probe-per-job answer; it is honoured
    verbatim, so every call site in a job measures the same quantity. Without
    it the preference order applies, and the probe is a real measurement
    rather than a check that a binary exists.
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
    """Run one command and return its counter, metric and wall together.

    Raises CounterError when the instrument cannot produce a number at all,
    subprocess.TimeoutExpired on a timeout, and OSError when the command
    cannot be started — the same trio the caller already handles for a plain
    `subprocess.run`, so nothing downstream grows a new failure path.
    """
    chosen = choose_metric(metric)
    code, out, err, wall, cpu = _run_child(
        list(command), cwd, child_environment(env), timeout)
    return Measurement(cpu, chosen, wall, out, err, code)


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
        discovered = loader.discover(str(directory),
                                     top_level_dir=str(directory))
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

    There is one instrument, so nothing here is being chosen — what this step
    exists for is the RECORD of why. It prints the two kernel settings that
    decide whether a deterministic counter is even possible on this cell:
    perf_event_paranoid for instruction counts, yama ptrace_scope for syscall
    counts. A reader who sees CPU seconds in use can then see that the
    alternatives were measured and unavailable-or-too-expensive, rather than
    assuming nobody thought of them.
    """
    return [
        f"metric={CPU_METRIC}",
        "metric_reason=on-CPU seconds, so steal time is excluded; "
        "instruction counts are refused by this kernel's perf_event_paranoid "
        "and syscall counts cost 12.8x the workload they measure, so CPU "
        "seconds is the only instrument this cell can afford to run",
        f"perf_event_paranoid={_setting(PARANOID_PATH)}",
        f"ptrace_scope={_setting(PTRACE_SCOPE_PATH)}",
    ]


def _setting(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip() or "unreadable"


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
        measurement = measure(command, timeout=args.timeout)
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
