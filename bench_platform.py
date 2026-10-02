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
from speed_workflow_steps import shell_is_posix

# Loading by path, the way every module here loads a sibling: the repository
# root is on sys.path under `discover`, and an explicit path keeps this
# importable from any of the entry points.
counter = load_counter()

# The SECOND predicate, and it means something else. A workflow's `run:` body
# is a bash script, so a control that slices one out of the tests.yml speed
# job and
# executes it needs a POSIX shell — which Windows has no `bash` for unless a
# real one is on PATH; its `bash` is the WSL shim, and it exits 1 having
# printed "Windows Subsystem for Linux has no installed distributions".
#
# Deliberately NOT folded into REQUIRES_BENCH. `bench_is_runnable` means
# "the instrument's facility exists"; this means "a POSIX shell exists".
# Merging them would widen a guard that currently means one precise thing,
# and the control asserting `bench_is_runnable()` is TRUE wherever the
# harness can run would then guard something broader than it says — which is
# how a guard becomes a way to make CI green.
REQUIRES_POSIX_SHELL = unittest.skipUnless(
    shell_is_posix(),
    "this slices a `run:` body out of the tests.yml speed job and executes it under bash; "
    "on Windows `bash` is the WSL shim, which fails because no distribution "
    "is installed. That is a missing shell, not a missing instrument, and "
    "the two are guarded separately on purpose")

# The skip DECORATOR. Applied to a method or a class.
REQUIRES_BENCH = unittest.skipUnless(
    counter.BENCH_RUNNABLE,
    "this drives the bench harness, which measures CPU seconds with "
    "resource.getrusage(RUSAGE_CHILDREN); the instrument is POSIX-only by "
    "decision and refuses rather than falling back to wall time where that "
    "does not exist. This is a platform guard, not a defect.")
