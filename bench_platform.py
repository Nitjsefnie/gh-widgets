"""The one place a test asks whether this platform can run the bench harness.

    python3 -m unittest discover -v

Not a test module: `unittest discover` collects `test_*.py` only. It exists
so that every control which drives `scripts/bench/e2e_bench.py` — in any
file, written now or later — consults the SAME predicate, which lives beside
the instrument's own refusal in `scripts/ci/counter.py` rather than being
restated per test file.

Three Windows rounds failed on one cause and each was fixed by guarding
whichever files were in that round's log. The predicate is what stops the
fourth: a test that drives the harness inherits the answer.
"""
import unittest

from counter_platform import load_counter

# Loading by path, the way every module here loads a sibling: the repository
# root is on sys.path under `discover`, and an explicit path keeps this
# importable from any of the entry points.
counter = load_counter()

# The skip DECORATOR. Applied to a method or a class.
REQUIRES_BENCH = unittest.skipUnless(
    counter.BENCH_RUNNABLE,
    "this drives the bench harness, which measures CPU seconds with "
    "resource.getrusage(RUSAGE_CHILDREN); the instrument is POSIX-only by "
    "decision and refuses rather than falling back to wall time where that "
    "does not exist. This is a platform guard, not a defect.")
