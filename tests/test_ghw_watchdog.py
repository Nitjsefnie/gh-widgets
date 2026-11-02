"""The per-test watchdog names a stalled test and re-arms for every test."""
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

from tests import _ghw_watchdog

ROOT = Path(__file__).resolve().parents[1]
SUITE = """\
import sys, time, unittest
import tests


class Case(unittest.TestCase):
    def setUp(self):
        if sys.argv[3] == "setUp":
            time.sleep(float(sys.argv[1]))

    def test_stalls_here(self):
        if sys.argv[3] == "test_stalls_here":
            time.sleep(float(sys.argv[1]))


suite = unittest.TestSuite(Case("test_stalls_here") for _ in range(int(sys.argv[2])))
sys.exit(not unittest.TextTestRunner().run(suite).wasSuccessful())
"""


def run_suite(limit, seconds, count, where="test_stalls_here"):
    env = dict(os.environ, **{_ghw_watchdog.ENV: str(limit)})
    started = time.monotonic()
    done = subprocess.run(
        [sys.executable, "-c", SUITE, str(seconds), str(count), where], cwd=ROOT,
        env=env, capture_output=True, text=True, timeout=120, check=False)
    return done, time.monotonic() - started


class TestWatchdog(unittest.TestCase):
    def test_a_stalled_test_is_named_with_its_stack(self):
        for where in ("test_stalls_here", "setUp"):
            with self.subTest(where=where):
                done, elapsed = run_suite(1, 60, 1, where)
                self.assertEqual(done.returncode, 1)
                self.assertIn("Timeout (0:00:01)!", done.stderr)
                self.assertIn(f"in {where}\n", done.stderr)
                self.assertIn("in __main__.Case.test_stalls_here\n",
                              done.stderr)
                self.assertLess(elapsed, 30)

    def test_each_test_gets_the_whole_limit(self):
        done, _ = run_suite(1.5, 0.6, 4)
        self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == "__main__":
    unittest.main()
