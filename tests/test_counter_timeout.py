"""counter.py's timeout path: kill the child's group, then drain it bounded."""
import contextlib
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from scripts.bench.bench_platform import REQUIRES_BENCH, counter
from tests import _ghw_temp, _ghw_watchdog
from tests.test_ghw_watchdog import ROOT, SUITE

SLEEPER = "[sys.executable, '-c', 'import time; time.sleep(120)']"


def time_out(script, drain):
    started = time.monotonic()
    with mock.patch.object(counter, "DRAIN_SECONDS", drain):
        try:
            counter.measure([sys.executable, "-c", script],
                            metric=counter.CPU_METRIC, timeout=2)
        except subprocess.TimeoutExpired as exc:
            return exc, time.monotonic() - started
    raise AssertionError("the child outlived its timeout without one")


@unittest.skipIf(sys.platform == "win32", "process groups and sessions are "
                 "POSIX; the timeout path runs only on ubuntu runners")
@REQUIRES_BENCH
class TestTimeout(unittest.TestCase):
    def test_a_timeout_kills_the_whole_process_group(self):
        # Its grandchild holds the pipes until the group kill, or the drain.
        script = ("import subprocess, sys, time\n"
                  f"subprocess.Popen({SLEEPER})\ntime.sleep(120)\n")
        _, elapsed = time_out(script, drain=30)
        self.assertLess(elapsed, 15)

    def test_a_pipe_held_past_the_group_kill_still_returns_the_output(self):
        # A grandchild in its own session survives the group kill.
        script = ("import subprocess, sys, time\n"
                  f"held = subprocess.Popen({SLEEPER}, start_new_session=True)\n"
                  "print(held.pid, file=sys.stderr, flush=True)\n"
                  "time.sleep(120)\n")
        exc, elapsed = time_out(script, drain=1)
        assert exc.stderr, "the drain lost what the child wrote"
        with contextlib.suppress(ProcessLookupError):
            os.kill(int(exc.stderr), signal.SIGKILL)
        self.assertLess(elapsed, 15)

    @unittest.skipUnless(sys.version_info >= (3, 12), "needs sys.monitoring")
    @REQUIRES_BENCH
    def test_a_watchdog_exit_under_the_statement_counter_keeps_its_stack(self):
        # The watchdog's _exit skips the count file; its dump must still print.
        env = dict(os.environ, **{_ghw_watchdog.ENV: "1",
                                  counter.METRIC_ENV: "statement_count"})
        with _ghw_temp.tempdir(prefix="ghw-stall-") as tmp:
            done = subprocess.run([
                sys.executable, str(ROOT / "scripts" / "ci" / "counter.py"),
                "--junit-file", str(Path(tmp, "unit.xml")), "--name",
                "counter::stall", "--cwd", str(ROOT), "--timeout", "60", "--",
                sys.executable, "-c", SUITE, "60", "1", "test_stalls_here"],
                env=env, capture_output=True, text=True, timeout=120,
                check=False)
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("in __main__.Case.test_stalls_here\n", done.stderr)


if __name__ == "__main__":
    unittest.main()
