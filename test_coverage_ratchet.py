#!/usr/bin/env python3
"""Tests for scripts/ci/coverage_ratchet.py.

    python3 -m unittest discover -v

Stdlib only, no network, no subprocess: every case builds the two input
documents the script reads — a `coverage json` report and the committed floor
file — and calls the subcommands directly. What is tested here is the
ratchet's DIRECTION and its refusals, because those are the two things a
ratchet gets wrong: a floor that drifts down without anyone noticing, and an
empty measurement that reads exactly like a clean one.

What these tests deliberately cannot check is whether a movement was
JUSTIFIED — that a gate's own suite can only ever prove its numbers agree
with each other. That is what `basis` is for, and it is required rather than
optional because the reason has to live at the numbers.
"""
import contextlib
import importlib.util
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).parent / relative)
    if spec is None or spec.loader is None:
        raise SystemExit(f"error: cannot load {relative}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ratchet = _load("coverage_ratchet", "scripts/ci/coverage_ratchet.py")

REPO_ROOT = Path(__file__).resolve().parent
COMMITTED_FLOOR = REPO_ROOT / "coverage-floor.json"

SHA = "ce49d0a267d6dc33db5da39cfafdf53848aadcff"


def _report(percent=72.0, statements=2910, missing=705, files=13):
    """A `coverage json` report shaped like the one coverage really writes.

    SYNTHETIC. These defaults are not the repository's coverage and are never
    compared against `coverage-floor.json` — the only document those numbers
    could contradict is the real one, and it is read from disk by the cases in
    TestTheCommittedFileIsTheOneTheGateReads. They match the current population
    (13 files, 2910 statements, 705 missing) so that a reader skimming this file
    for "what does the repository measure" finds the current figures rather
    than figures from three generations ago, which is what a stale fixture
    here looks like to anyone who has not been told otherwise.
    """
    names = [f"module_{index}.py" for index in range(files)]
    return {
        "files": {name: {"summary": {}} for name in names},
        "meta": {"format": 3, "version": "7.16.1"},
        "totals": {
            "covered_lines": statements - missing,
            "num_statements": statements,
            "percent_covered": percent,
            "percent_covered_display": str(int(percent)),
            "missing_lines": missing,
            "excluded_lines": 0,
        },
    }


def _document(floor=71.9, drop=(), **overrides):
    """The committed floor document, with keys a case needs removed."""
    document = {
        "schema": 1,
        "basis": "the population is every shipped source family; "
                 "the floor sits under the measurement on purpose",
        "population": ["--source=.", "--omit=test_*.py"],
        "cell": "ubuntu-latest / 3.13",
        "measured": 71.9,
        "floor": floor,
        "measured_commit": SHA,
        "measured_at": "2026-09-30T19:40:41Z",
        "statements": 2910,
        "missing": 705,
        "files": 13,
    }
    document.update(overrides)
    for key in drop:
        document.pop(key)
    return document


class _Case(unittest.TestCase):
    """A temp directory holding the two documents, and a way to run a verb."""

    def setUp(self):
        # An ExitStack rather than a bare TemporaryDirectory: the directory
        # has to outlive setUp (each case writes two files into it across
        # several calls), and the stack is how a `with` statement's lifetime
        # survives into per-test cleanup.
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(
            tempfile.TemporaryDirectory(prefix="ghw-ratchet-")))
        self.report_path = self.root / "coverage.json"
        self.floor_path = self.root / "coverage-floor.json"

    def write_report(self, **kwargs):
        self.report_path.write_text(json.dumps(_report(**kwargs)),
                                    encoding="utf-8")

    def write_committed(self, floor=71.9, drop=(), **overrides):
        self.floor_path.write_text(
            json.dumps(_document(floor=floor, drop=drop, **overrides),
                       indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")

    def run_verb(self, command, **kwargs):
        """main() with its streams captured, as the CLI would run it."""
        argv = [command, "--coverage-json", str(self.report_path),
                "--floor-file", str(self.floor_path)]
        if "commit" in kwargs:
            argv += ["--commit", kwargs.pop("commit")]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ratchet.main(argv)
        return code, out.getvalue(), err.getvalue()

    def committed(self):
        return json.loads(self.floor_path.read_text(encoding="utf-8"))


class TestGateBoundary(_Case):
    """The comparison, at and either side of the floor."""

    def setUp(self):
        super().setUp()
        self.write_committed()

    def test_a_measurement_equal_to_the_floor_passes(self):
        self.write_report(percent=71.9, statements=1000, missing=281)
        code, _, _ = self.run_verb("gate")
        self.assertEqual(code, 0)

    def test_a_measurement_above_the_floor_passes(self):
        self.write_report(percent=80.0, statements=1000, missing=200)
        code, out, _ = self.run_verb("gate")
        self.assertEqual(code, 0)
        self.assertIn("measured 80.0", out)

    def test_a_measurement_a_hair_below_the_floor_fails(self):
        self.write_report(percent=71.86, statements=1000, missing=281)
        code, _, err = self.run_verb("gate")
        self.assertEqual(code, 1)
        # The message states the OBSERVATION, not a guess at the cause: two
        # numbers the reader can act on, and no diagnosis to be wrong about.
        self.assertIn("measured 71.8 against a floor of 71.9", err)

    def test_a_hair_above_the_floor_rounds_down_onto_it_and_passes(self):
        # 71.94 truncates to 71.9. Rounding to nearest would give the same
        # answer here; the direction that matters is asserted below.
        self.write_report(percent=71.94, statements=1000, missing=280)
        code, out, _ = self.run_verb("gate")
        self.assertEqual(code, 0)
        self.assertIn("measured 71.9", out)


class TestRoundingDirection(unittest.TestCase):
    """Percentages truncate DOWN, so a stored number can never sit above
    the truth it was measured from."""

    def test_one_decimal_truncates_rather_than_rounds(self):
        self.assertEqual(ratchet.round_down(71.99), 71.9)
        self.assertEqual(ratchet.round_down(71.94), 71.9)
        self.assertEqual(ratchet.round_down(72.0), 72.0)
        self.assertEqual(ratchet.round_down(0.0999), 0.0)

    def test_a_truncated_number_never_exceeds_the_measurement(self):
        for value in (0.0, 12.34, 71.94, 71.99, 99.999):
            self.assertLessEqual(ratchet.round_down(value), value)


class TestAnEmptyMeasurementIsNotAMeasurement(_Case):
    """A report that measured nothing must never read as a coverage number."""

    def setUp(self):
        super().setUp()
        # A perfectly good floor document, so a refusal here can only be
        # about the measurement and not about the other file.
        self.write_committed()

    def test_a_report_with_no_files_is_an_error(self):
        self.write_report(files=0)
        code, out, err = self.run_verb("gate")
        self.assertEqual(code, 2)
        self.assertIn("measured no files", err)
        # The count is printed even on the failing path — that is the tell
        # that separates "nothing ran" from "everything ran uncovered".
        self.assertIn("0 file(s)", out)

    def test_a_report_with_no_statements_is_an_error(self):
        self.write_report(statements=0, missing=0)
        code, _, err = self.run_verb("gate")
        self.assertEqual(code, 2)
        self.assertIn("not a measurement", err)

    def test_an_absent_report_is_an_error_rather_than_a_vacuous_pass(self):
        code, _, err = self.run_verb("gate")
        self.assertEqual(code, 2)
        self.assertIn("no coverage report", err)

    def test_a_report_that_is_not_json_is_an_error(self):
        self.report_path.write_text("<html>404</html>", encoding="utf-8")
        code, _, err = self.run_verb("gate")
        self.assertEqual(code, 2)
        self.assertIn("not readable coverage JSON", err)

    def test_a_report_that_is_not_an_object_is_an_error(self):
        self.report_path.write_text("[1, 2, 3]", encoding="utf-8")
        code, _, err = self.run_verb("gate")
        self.assertEqual(code, 2)
        self.assertIn("not a coverage JSON object", err)

    def test_a_report_without_a_percentage_is_an_error(self):
        self.write_committed()
        report = _report()
        del report["totals"]["percent_covered"]
        self.report_path.write_text(json.dumps(report), encoding="utf-8")
        code, _, err = self.run_verb("gate")
        self.assertEqual(code, 2)
        self.assertIn("percent_covered=None", err)

    def test_the_file_count_is_printed_on_a_normal_run_too(self):
        self.write_committed(files=11)
        self.write_report(files=11)
        code, out, _ = self.run_verb("gate")
        self.assertEqual(code, 0)
        self.assertIn("11 file(s)", out)


class TestRaiseNeverLowers(_Case):
    """The direction the ratchet may never move in."""

    def test_a_measurement_below_the_floor_leaves_the_file_byte_identical(self):
        self.write_committed(floor=90.0)
        before = self.floor_path.read_bytes()
        self.write_report(percent=71.9)
        code, out, err = self.run_verb("raise", commit=SHA)
        self.assertEqual(code, 0)
        self.assertEqual(self.floor_path.read_bytes(), before)
        self.assertIn("only moves in one direction", out)
        self.assertEqual(err, "")

    def test_a_measurement_equal_to_the_floor_writes_nothing(self):
        # "Strictly above" is the condition. Equal is not above, and a
        # rewrite on an equal measurement would make every run a commit.
        self.write_committed(floor=71.9)
        before = self.floor_path.read_bytes()
        self.write_report(percent=71.9)
        code, _, _ = self.run_verb("raise", commit=SHA)
        self.assertEqual(code, 0)
        self.assertEqual(self.floor_path.read_bytes(), before)

    def test_no_sequence_of_raises_can_walk_the_floor_down(self):
        # The real property: whatever order the measurements arrive in, the
        # committed floor is the MAXIMUM of everything ever handed to raise.
        floor = 50.0
        for percent in (80.0, 60.0, 75.0, 40.0, 90.0, 90.0):
            self.write_committed(floor=floor)
            self.write_report(percent=percent)
            code, _, _ = self.run_verb("raise", commit=SHA)
            self.assertEqual(code, 0)
            floor = self.committed()["floor"]
        self.assertEqual(floor, 90.0)


class TestAMovedPopulationIsRefused(_Case):
    """A percentage is a ratio, so it cannot announce its own denominator.

    The mirror of a shrunken measured file: adding one shipped module moves
    the number while everything the ratchet compares still looks internally
    consistent, and a ratchet that only ever compares against itself cannot
    see that. The file count is the cheapest announcement available — it is
    already printed on every run — so it is committed and compared.
    """

    def test_a_gate_above_the_floor_with_a_different_file_count_is_refused(self):
        self.write_committed(floor=10.0, measured=10.0, files=11)
        self.write_report(percent=90.0, files=12, statements=3000, missing=300)
        code, _, err = self.run_verb("gate")
        self.assertEqual(code, 2)
        self.assertIn("the population moved", err)

    def test_a_gate_above_the_floor_with_matching_file_counts_passes(self):
        self.write_committed(floor=10.0, measured=10.0, files=11)
        self.write_report(percent=90.0, files=11, statements=3000, missing=300)
        code, out, err = self.run_verb("gate")
        self.assertEqual(code, 0)
        self.assertIn("at or above it", out)
        self.assertNotIn("the population moved", err)

    def test_gate_and_raise_refuse_a_moved_population_with_the_same_message(self):
        for committed_files, measured_files in ((11, 12), (12, 11)):
            with self.subTest(committed=committed_files, measured=measured_files):
                self.write_committed(floor=10.0, files=committed_files)
                before = self.floor_path.read_bytes()
                self.write_report(percent=90.0, files=measured_files)
                gate_code, _, gate_err = self.run_verb("gate")
                raise_code, _, raise_err = self.run_verb("raise", commit=SHA)
                self.assertEqual(gate_code, 2)
                self.assertEqual(raise_code, 2)
                expected = (
                    f"coverage ratchet: {self.floor_path} was measured over "
                    f"{committed_files} file(s) and this run measured "
                    f"{measured_files}: the population moved, so this floor "
                    "is not comparable until `files` here is re-measured "
                    "and committed deliberately\n")
                self.assertEqual(gate_err, expected)
                self.assertEqual(gate_err, raise_err)
                self.assertEqual(self.floor_path.read_bytes(), before)

    def test_a_raise_over_a_different_file_count_is_refused(self):
        self.write_committed(floor=10.0, measured=10.0, files=11)
        before = self.floor_path.read_bytes()
        self.write_report(percent=90.0, files=12, statements=3000, missing=300)
        code, _, err = self.run_verb("raise", commit=SHA)
        self.assertEqual(code, 2)
        self.assertIn("the population moved", err)
        # Not "wrote nothing and exited 0": a moved population is something a
        # human has to record, and exit 0 would report it as routine news.
        self.assertEqual(self.floor_path.read_bytes(), before)

    def test_a_shrinking_population_is_refused_too(self):
        self.write_committed(floor=10.0, measured=10.0, files=12)
        self.write_report(percent=90.0, files=11)
        code, _, err = self.run_verb("raise", commit=SHA)
        self.assertEqual(code, 2)
        self.assertIn("the population moved", err)

    def test_a_gate_below_the_floor_refuses_a_different_file_count_first(self):
        self.write_committed(floor=71.9, files=11)
        self.write_report(percent=60.0, files=12)
        code, _, err = self.run_verb("gate")
        self.assertEqual(code, 2)
        self.assertIn("the population moved", err)
        self.assertNotIn("FAIL: measured", err)

    def test_the_file_count_is_recorded_on_a_raise(self):
        self.write_committed(floor=10.0, measured=10.0, files=11)
        self.write_report(percent=90.0, files=11, statements=3000, missing=300)
        self.run_verb("raise", commit=SHA)
        self.assertEqual(self.committed()["files"], 11)

    def test_a_committed_file_count_of_zero_is_refused(self):
        self.write_committed(files=0)
        self.write_report(percent=90.0)
        code, _, err = self.run_verb("gate")
        self.assertEqual(code, 2)
        self.assertIn("not a measurement", err)

    def test_a_non_numeric_file_count_is_refused(self):
        self.write_committed(files="eleven")
        self.write_report(percent=90.0)
        code, _, err = self.run_verb("gate")
        self.assertEqual(code, 2)
        self.assertIn("is not a number", err)

    def test_a_missing_file_count_is_refused(self):
        self.write_committed(drop=("files",))
        self.write_report(percent=90.0)
        code, _, err = self.run_verb("gate")
        self.assertEqual(code, 2)
        self.assertIn("missing required key(s): files", err)


class TestRaisePreservesTheHumanKeys(_Case):
    """A ratchet run may rewrite numbers, never its own rationale."""

    def setUp(self):
        super().setUp()
        self.write_committed(floor=71.9, measured=71.9,
                             measured_commit="0" * 40,
                             measured_at="2020-01-01T00:00:00Z",
                             statements=1, missing=1)
        self.before = self.committed()
        self.write_report(percent=88.0, statements=3000, missing=360)
        code, _, _ = self.run_verb("raise", commit=SHA)
        self.assertEqual(code, 0)
        self.after = self.committed()

    def test_the_authored_keys_survive_byte_for_byte(self):
        for key in ratchet.AUTHORED_KEYS:
            with self.subTest(key=key):
                self.assertEqual(self.after[key], self.before[key])

    def test_the_numbers_and_provenance_are_rewritten(self):
        self.assertEqual(self.after["floor"], 88.0)
        self.assertEqual(self.after["measured"], 88.0)
        self.assertEqual(self.after["statements"], 3000)
        self.assertEqual(self.after["missing"], 360)
        self.assertEqual(self.after["measured_commit"], SHA)
        self.assertNotEqual(self.after["measured_at"], "2020-01-01T00:00:00Z")

    def test_the_rewrite_leaves_no_unknown_keys_and_adds_none(self):
        self.assertEqual(set(self.after), set(self.before))


class TestRaiseThenGateRoundTrip(_Case):
    """The two verbs are one mechanism, so the output of one must satisfy
    the other — read back off disk, not asserted as a restated number."""

    def test_what_raise_writes_is_what_gate_reads(self):
        self.write_committed(floor=10.0, measured=10.0)
        self.write_report(percent=83.25, statements=400, missing=67)
        code, _, _ = self.run_verb("raise", commit=SHA)
        self.assertEqual(code, 0)
        code, out, err = self.run_verb("gate")
        self.assertEqual(code, 0, out + err)
        self.assertIn("measured 83.2 against a floor of 83.2", out)

    def test_a_fresh_gate_still_reads_green_after_a_raise(self):
        self.write_committed(floor=10.0, measured=10.0)
        self.write_report(percent=83.25, statements=400, missing=67)
        self.run_verb("raise", commit=SHA)
        self.assertEqual(ratchet.read_committed(self.floor_path)["floor"], 83.2)
        code, _, _ = self.run_verb("gate")
        self.assertEqual(code, 0)


class TestAMalformedCommittedFileIsRefused(_Case):
    """A malformed floor must raise. A permissive default is a green light
    wired to a broken configuration."""

    def setUp(self):
        super().setUp()
        self.write_report(percent=99.0, statements=100, missing=1)

    def _refused(self, **kwargs):
        self.write_committed(**kwargs)
        code, _, err = self.run_verb("gate")
        self.assertEqual(code, 2, err)
        return err

    def test_a_missing_floor_is_refused(self):
        # Named as the key it is, not as whatever a per-key check made of a
        # None: a renamed floor has to read as "floor" in the log.
        self.assertIn("missing required key(s): floor",
                      self._refused(drop=("floor",)))

    def test_a_wrong_schema_is_refused(self):
        self.assertIn("schema", self._refused(schema=2))

    def test_a_non_numeric_floor_is_refused(self):
        self.assertIn("is not a number", self._refused(floor="71.9"))

    def test_a_boolean_floor_is_refused(self):
        # `True` is an int in Python and 1.0 is a percentage; neither is a
        # floor, so neither is allowed through.
        self.assertIn("is not a number", self._refused(floor=True))

    def test_a_missing_basis_is_refused(self):
        self.assertIn("missing required key(s): basis",
                      self._refused(drop=("basis",)))

    def test_an_empty_basis_is_refused(self):
        self.assertIn("no basis", self._refused(basis="   "))

    def test_an_empty_population_is_refused(self):
        self.assertIn("population", self._refused(population=[]))

    def test_a_missing_measured_commit_is_refused(self):
        self.assertIn("measured_commit",
                      self._refused(drop=("measured_commit",)))

    def test_raise_refuses_the_same_malformed_files(self):
        self.write_committed(drop=("floor",))
        code, _, err = self.run_verb("raise", commit=SHA)
        self.assertEqual(code, 2)
        self.assertIn("floor", err)

    def test_an_absent_committed_file_is_refused(self):
        self.floor_path.unlink(missing_ok=True)
        code, _, err = self.run_verb("gate")
        self.assertEqual(code, 2)
        self.assertIn("no committed floor", err)

    def test_a_committed_file_that_is_not_json_is_refused(self):
        self.floor_path.write_text("not json at all", encoding="utf-8")
        code, _, err = self.run_verb("gate")
        self.assertEqual(code, 2)
        self.assertIn("not readable JSON", err)

    def test_a_committed_file_that_is_not_an_object_is_refused(self):
        self.floor_path.write_text("[71.9]", encoding="utf-8")
        code, _, err = self.run_verb("gate")
        self.assertEqual(code, 2)
        self.assertIn("not a JSON object", err)

    def test_non_numeric_counts_are_refused(self):
        self.assertIn("statements='lots'",
                      self._refused(statements="lots"))
        self.assertIn("missing=None", self._refused(missing=None))


class TestTheWriteIsAtomicAndPrivate(_Case):
    """The floor file is replaced through a fresh inode, never truncated in
    place: a crash mid-write must not leave the gate unreadable."""

    def setUp(self):
        super().setUp()
        self.write_committed(floor=10.0, measured=10.0)
        self.write_report(percent=88.0, statements=3000, missing=360)

    def test_stale_temp_files_are_not_adopted_and_are_cleaned_up(self):
        # The failure this guards: a writer that opens a FIXED .tmp path with
        # O_TRUNC keeps whatever inode a crashed run left behind. mkstemp
        # cannot collide with any of these, and the finally block removes the
        # temp it actually made — so the directory afterwards holds exactly
        # the litter it started with and not one byte more.
        litter = [self.root / f".coverage-floor.json.stale{index}.tmp"
                  for index in range(3)]
        for path in litter:
            path.write_text('{"floor": 0.0}\x00', encoding="utf-8")
        self.run_verb("raise", commit=SHA)
        self.assertEqual(self.committed()["floor"], 88.0)
        self.assertEqual(set(self.root.glob(".*.tmp")), set(litter))

    def test_the_writer_asks_for_a_world_readable_mode(self):
        """The claim that holds on every platform: the writer REQUESTS 0644.

        mkstemp creates its inode at 0600 unconditionally. This file is
        repo-visible and carries no secret, so publishing it at the mkstemp
        mode would surprise the next person on the box, and — the reason the
        chmod exists — git does not record the non-executable bit, so a raise
        would leave a tracked file at 0600 that a fresh checkout elsewhere
        brings back at 0644.

        Asserted by recording the request rather than by reading `st_mode`
        back, because that is the platform-independent form of the same claim
        and it runs on the Windows cells where `st_mode` reports 0666 for a
        writable file and asserts nothing at all.
        """
        requested = []
        with mock.patch.object(ratchet.os, "chmod",
                               side_effect=lambda path, mode:
                               requested.append((path, mode))):
            self.run_verb("raise", commit=SHA)
        self.assertEqual([mode for _, mode in requested], [0o644],
                         "the writer must ask for 0644 on the file it "
                         "publishes, and exactly once")

    def test_the_written_file_is_not_read_only(self):
        """The property, on every platform: what was published is writable.

        This is the part of the guarantee that must never stop running, and it
        is expressed the way every platform agrees on — `os.access` against the
        file, not a POSIX mode literal. A Windows run asserts exactly this and
        not one bit more, because it cannot.
        """
        self.run_verb("raise", commit=SHA)
        self.assertTrue(os.access(self.floor_path, os.W_OK),
                        "the file the ratchet published is not writable")

    @unittest.skipUnless(os.name == "posix", "POSIX permission modes only")
    def test_the_written_mode_is_0644_where_posix_modes_exist(self):
        """The literal, only where the literal means something.

        On Windows `os.chmod` only toggles the read-only attribute and
        `st_mode` reports 0666 for any writable file, so asserting 0644 there
        asserts a fact the platform does not have — this case went red on all
        four Windows cells for exactly that reason. It is skipped rather than
        rewritten because the fact it checks is genuinely POSIX-only; the two
        cases above carry the guarantee everywhere, and the first of them
        checks the stronger thing (the writer ASKS for 0644) rather than the
        weaker thing (the filesystem ended up at 0644). Same idiom, and the same
        problem, as `test_atomic_write.py`.
        """
        self.run_verb("raise", commit=SHA)
        mode = stat.S_IMODE(self.floor_path.stat().st_mode)
        self.assertEqual(mode, 0o644)

    @unittest.skipUnless(
        sys.platform == "win32", "Windows read-only semantics")
    def test_windows_reports_0666_for_the_file_it_wrote(self):
        """The assumption the skip above rests on, checked where it holds.

        The POSIX-only case is skipped on the strength of a claim about this
        platform, so the claim should be pinned rather than trusted: if Windows
        ever grows real permission bits — or if a change stops publishing a
        writable file — this goes red, and the skip above becomes reviewable
        rather than permanent.
        """
        self.run_verb("raise", commit=SHA)
        mode = stat.S_IMODE(self.floor_path.stat().st_mode)
        self.assertIn(mode, (0o666, 0o644),
                      "a writable file on Windows reports 0666; if that has "
                      "changed, the POSIX-only case can come back")

    def test_the_file_is_never_truncated_in_place(self):
        # An in-place write would replace the inode; this one must not.
        before = os.stat(self.floor_path).st_ino
        self.run_verb("raise", commit=SHA)
        self.assertNotEqual(os.stat(self.floor_path).st_ino, before)

    def test_the_descriptor_closes_when_the_stream_conversion_fails(self):
        # The window between mkstemp and the `with` that takes ownership of
        # the descriptor. A normal write never enters it, and a leak there
        # leaves an open temp on Windows, where the cleanup's unlink then
        # fails too — so this exercises the conversion failure directly and
        # checks all three consequences: the raw fd is closed, the temp is
        # gone, and the committed floor is untouched.
        before = self.floor_path.read_bytes()
        opened = []

        def fdopen_that_fails(fd, *args, **kwargs):
            opened.append(fd)
            raise OSError("cannot convert descriptor")

        with mock.patch.object(ratchet.os, "fdopen", fdopen_that_fails):
            with self.assertRaises(OSError):
                self.run_verb("raise", commit=SHA)
        self.assertEqual(len(opened), 1)
        # Closed exactly once, by the cleanup — a second close would raise.
        with self.assertRaises(OSError):
            os.fstat(opened[0])
        self.assertEqual(list(self.root.glob(".*.tmp")), [])
        self.assertEqual(self.floor_path.read_bytes(), before)


class TestTheCommandLine(unittest.TestCase):
    """The entry point the two workflows actually invoke.

    Everything else here calls main() in-process, which is the fast way to
    test behaviour and the wrong way to test a script whose whole contract is
    its argv. This one spawns it, so the `if __name__ == "__main__"` line
    that CI reaches is not the one line nothing reaches.
    """

    SCRIPT = str(REPO_ROOT / "scripts" / "ci" / "coverage_ratchet.py")

    def test_gate_exits_zero_through_the_real_entry_point(self):
        with tempfile.TemporaryDirectory(prefix="ghw-ratchet-cli-") as root:
            directory = Path(root)
            (directory / "coverage.json").write_text(
                json.dumps(_report(percent=72.0)), encoding="utf-8")
            (directory / "coverage-floor.json").write_text(
                json.dumps(_document(), indent=2) + "\n", encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, self.SCRIPT, "gate",
                 "--coverage-json", str(directory / "coverage.json"),
                 "--floor-file", str(directory / "coverage-floor.json")],
                capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0,
                         completed.stdout + completed.stderr)
        self.assertIn("at or above it", completed.stdout)

    def test_a_below_floor_measurement_exits_non_zero_through_the_entry_point(self):
        with tempfile.TemporaryDirectory(prefix="ghw-ratchet-cli-") as root:
            directory = Path(root)
            (directory / "coverage.json").write_text(
                json.dumps(_report(percent=60.0)), encoding="utf-8")
            (directory / "coverage-floor.json").write_text(
                json.dumps(_document(), indent=2) + "\n", encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, self.SCRIPT, "gate",
                 "--coverage-json", str(directory / "coverage.json"),
                 "--floor-file", str(directory / "coverage-floor.json")],
                capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 1,
                         completed.stdout + completed.stderr)
        self.assertIn("measured 60.0 against a floor of 71.9",
                      completed.stderr)

    def test_a_missing_subcommand_is_refused(self):
        completed = subprocess.run([sys.executable, self.SCRIPT],
                                   capture_output=True, text=True,
                                   check=False)
        self.assertNotEqual(completed.returncode, 0)


# The one `coverage run` line tests.yml executes, from the `python -m coverage
# run` up to the runner it is handed. Anchored on the invocation rather than
# on the whole file so a second, differently-configured measurement elsewhere
# in the workflow cannot be read as this one.
MEASURED_RUN = re.compile(r"python -m coverage run\b(.*?)-m unittest discover",
                          re.DOTALL)
SOURCE_FLAG = re.compile(r"--source='?([^'\s\\]+)'?")
OMIT_FLAG = re.compile(r"--omit='?([^'\s\\]+)'?")


def _measured_population():
    """The `--source`/`--omit` flags tests.yml actually measures with."""
    workflow = (REPO_ROOT / ".github" /
                "workflows" / "tests.yml").read_text(encoding="utf-8")
    command = MEASURED_RUN.search(workflow)
    if command is None:
        return None
    source, omit = SOURCE_FLAG.search(command.group(1)), OMIT_FLAG.search(command.group(1))
    if source is None or omit is None:
        return None
    return [f"--source={source.group(1)}", f"--omit={omit.group(1)}"]


class TestTheCommittedFileIsTheOneTheGateReads(unittest.TestCase):
    """The repository's own coverage-floor.json, checked against the shape
    the script demands and against the workflow that measures the population
    it claims to describe."""

    def test_the_committed_file_is_well_formed(self):
        document = ratchet.read_committed(COMMITTED_FLOOR)
        self.assertEqual(document["schema"], 1)
        self.assertGreater(document["floor"], 0)
        self.assertLessEqual(document["floor"], document["measured"])

    def test_its_population_is_the_one_tests_measures(self):
        # EQUALITY, both directions, and specifically not "the committed flag
        # appears somewhere in the workflow": `--omit=test_*.py` is a PREFIX
        # of `--omit=test_*.py,scripts/*`, so a one-way substring check is
        # satisfied by exactly the regression this case exists to catch —
        # putting `scripts/*` back into the omit list. The population is read
        # out of the command the workflow actually runs and compared as a
        # whole, so an EXTRA exclusion is as loud as a missing one.
        document = ratchet.read_committed(COMMITTED_FLOOR)
        measured = _measured_population()
        self.assertIsNotNone(measured, "tests.yml runs no coverage command")
        self.assertEqual(measured, document["population"])

    def test_it_names_a_commit_and_a_time(self):
        document = ratchet.read_committed(COMMITTED_FLOOR)
        self.assertRegex(document["measured_commit"], r"^[0-9a-f]{40}$")
        self.assertRegex(document["measured_at"],
                         r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


if __name__ == "__main__":
    unittest.main()
