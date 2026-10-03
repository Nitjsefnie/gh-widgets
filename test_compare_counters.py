"""Tests for the test-speed comparator.

This file is part of the stdlib unittest suite so its gate's own tests run in
the gate that measures coverage. The suite runner is unittest, and the speed
gate counts CPU seconds while scripts/ci/counter.py writes its own JUnit
output. The speed job installs requirements-test.txt because unittest
discovery imports the full suite; this file itself still uses only the
standard library. pytest remains pinned for optional local `pytest` runs, but
the suite itself runs on unittest.

The comparator is a gate, so its own failure modes matter more than most
code here: a false red teaches people to ignore it, and a false green
means the gate is decorative. The cases below are exactly the ways it
could be wrong — added tests, removed tests, flaky rounds, non-passing
testcases — plus the arithmetic.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent


def _load():
    """Import scripts/ci/compare_counters.py by path.

    scripts/ci is not a package and deliberately has no __init__.py — it
    holds standalone CI entry points, not an importable library.
    """
    path = REPO_ROOT / "scripts" / "ci" / "compare_counters.py"
    spec = importlib.util.spec_from_file_location("compare_counters", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["compare_counters"] = module
    spec.loader.exec_module(module)
    return module


cd = _load()


def write_junit(path: Path, cases: dict, not_passed: dict | None = None) -> Path:
    """Minimal but real pytest --junitxml output."""
    not_passed = not_passed or {}
    parts = ['<?xml version="1.0" encoding="utf-8"?>', "<testsuites><testsuite>"]
    for nid, seconds in cases.items():
        classname, _, name = nid.partition("::")
        child = not_passed.get(nid)
        body = f"<{child}/>" if child else ""
        parts.append(
            f'<testcase classname="{classname}" name="{name}" '
            f'time="{seconds}">{body}</testcase>'
        )
    parts.append("</testsuite></testsuites>")
    path.write_text("".join(parts), encoding="utf-8")
    return path


class TestComparator(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp_path = Path(stack.enter_context(tempfile.TemporaryDirectory()))

    def test_node_id_joins_class_and_name(self):
        report = write_junit(self.tmp_path / "r.xml", {"tests.test_a::test_one": 1.0})
        self.assertEqual(set(cd.parse_junit(report)), {"tests.test_a::test_one"})

    def test_non_passing_cases_are_excluded(self):
        report = write_junit(
            self.tmp_path / "r.xml",
            {"m::ok": 1.0, "m::bad": 9.0, "m::skip": 9.0},
            not_passed={"m::bad": "failure", "m::skip": "skipped"},
        )
        # A failure can be fast or slow for reasons unrelated to speed; a skip
        # is not a measurement at all.
        self.assertEqual(set(cd.parse_junit(report)), {"m::ok"})

    def test_rounds_fold_to_the_minimum(self):
        a = write_junit(self.tmp_path / "a.xml", {"m::t": 1.00})
        b = write_junit(self.tmp_path / "b.xml", {"m::t": 3.00})
        # The minimum estimates the floor; the mean would track the noise.
        self.assertEqual(cd.fold_rounds([str(a), str(b)]), {"m::t": 1.00})

    def test_a_test_missing_from_one_round_is_dropped(self):
        a = write_junit(self.tmp_path / "a.xml", {"m::t": 1.0, "m::flaky": 1.0})
        b = write_junit(self.tmp_path / "b.xml", {"m::t": 1.0})
        self.assertEqual(set(cd.fold_rounds([str(a), str(b)])), {"m::t"})

    def test_added_tests_cannot_trip_the_gate(self):
        """The whole reason this compares an intersection."""
        base = {"m::a": 1.0, "m::b": 1.0}
        head = {"m::a": 1.0, "m::b": 1.0, "m::brand_new": 50.0}

        result = cd.compare(base, head)

        self.assertAlmostEqual(result["ratio"], 1.0)
        self.assertEqual(result["head_only"], ["m::brand_new"])

    def test_removed_tests_cannot_hide_a_regression(self):
        base = {"m::a": 1.0, "m::slow_one_being_deleted": 100.0}
        head = {"m::a": 2.0}

        result = cd.compare(base, head)

        # Total wall time fell from 101s to 2s; the shared test still doubled.
        self.assertAlmostEqual(result["ratio"], 2.0)
        self.assertEqual(result["base_only"], ["m::slow_one_being_deleted"])

    def test_ratio_is_over_the_shared_population_only(self):
        base = {"m::a": 2.0, "m::b": 8.0}
        head = {"m::a": 3.0, "m::b": 9.0}

        self.assertAlmostEqual(cd.compare(base, head)["ratio"], 12.0 / 10.0)

    def test_no_shared_tests_is_an_error_not_a_pass(self):
        with self.assertRaisesRegex(cd.ComparisonError, "share no passing test"):
            cd.compare({"m::a": 1.0}, {"m::z": 1.0})

    def test_zero_baseline_is_an_error_not_a_division_by_zero(self):
        with self.assertRaisesRegex(cd.ComparisonError, "baseline total is zero"):
            cd.compare({"m::a": 0.0}, {"m::a": 1.0})

    def test_empty_report_is_an_error(self):
        report = write_junit(self.tmp_path / "r.xml", {})
        with self.assertRaisesRegex(
                cd.ComparisonError,
                "no passing testcases.*measuring environment"):
            cd.parse_junit(report)

    def test_unparseable_report_is_an_error(self):
        bad = self.tmp_path / "bad.xml"
        bad.write_text("<testsuites", encoding="utf-8")
        with self.assertRaisesRegex(
                cd.ComparisonError,
                "not parseable.*measuring environment"):
            cd.parse_junit(bad)

    def test_collection_failure_names_environment_not_regression(self):
        base = write_junit(self.tmp_path / "base.xml", {"m::a": 1.0})
        failed = self.tmp_path / "failed.xml"
        output = (
            "test_ci_workflows (unittest.loader._FailedTest.test_ci_workflows) "
            "... ERROR\n"
            "ImportError: Failed to import test module: test_ci_workflows\n"
            "ModuleNotFoundError: No module named 'yaml'\n"
        )
        measurement = cd.counter.Measurement(
            0.01, "cpu_time", 0.02, output, "", 1)
        cd.counter.write_junit(
            failed, "counter::unit-suite", measurement,
            ["python", "-m", "unittest", "discover"])

        argv = sys.argv
        stderr = io.StringIO()
        try:
            sys.argv = ["compare_counters.py", "--base", str(base),
                        "--head", str(failed)]
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(stderr):
                self.assertEqual(cd.main(), 2)
        finally:
            sys.argv = argv

        self.assertIn("measuring environment", stderr.getvalue())
        self.assertIn("not a measured regression", stderr.getvalue())
        self.assertIn("unittest collection/import error", stderr.getvalue())
        self.assertNotIn("REGRESSION", stderr.getvalue())

    def test_real_unittest_import_error_is_classified_as_collection_failure(self):
        root = ET.fromstring(
            "<testsuite><testcase><error>"
            "ImportError: Failed to import test module: test_broken"
            "</error></testcase></testsuite>")
        self.assertTrue(cd._is_unittest_collection_error(root))

    def test_runtime_assertion_is_not_classified_as_collection_failure(self):
        root = ET.fromstring(
            "<testsuite><testcase><failure>"
            "AssertionError: expected value"
            "</failure></testcase></testsuite>")
        self.assertFalse(cd._is_unittest_collection_error(root))

    def test_empty_success_is_not_classified_as_collection_failure(self):
        root = ET.fromstring(
            "<testsuite><testcase time='0.01'><system-out>"
            "Ran 0 tests\nOK"
            "</system-out></testcase></testsuite>")
        self.assertFalse(cd._is_unittest_collection_error(root))

    def test_sub_50ms_tests_stay_out_of_the_table_but_count_in_the_total(self):
        base = {"m::tiny": 0.002, "m::real": 1.0}
        head = {"m::tiny": 0.008, "m::real": 1.0}

        result = cd.compare(base, head)

        # 4x on a 2 ms test is noise and would bury genuine entries.
        self.assertEqual([row[2] for row in result["per_test"]], ["m::real"])
        # ...but it is still in the totals, where it belongs.
        self.assertAlmostEqual(result["head_total"], 1.008)

    def test_render_marks_a_regression_and_names_the_budget(self):
        result = cd.compare({"m::a": 1.0}, {"m::a": 2.0})

        text = cd.render(result, 0.30, "v1.2.3")

        self.assertIn("REGRESSION", text)
        self.assertIn("+100.0%", text)
        self.assertIn("v1.2.3", text)

    def test_render_marks_an_acceptable_change(self):
        result = cd.compare({"m::a": 1.0}, {"m::a": 1.10})

        text = cd.render(result, 0.30, "v1.2.3")

        self.assertIn("within budget", text)
        self.assertNotIn("REGRESSION", text)

    def test_render_disclaims_the_per_test_table(self):
        """The table misleads without this, and that is measured, not assumed.

        Two runs of identical code on one machine moved individual tests by up
        to +370% while the total moved 1.7%. A reader who takes a row as a
        regression is reading noise.
        """
        result = cd.compare({"m::a": 1.0, "m::b": 1.0}, {"m::a": 1.0, "m::b": 1.4})

        text = cd.render(result, 0.30, "v1.0.0")

        self.assertIn("not a regression", text.lower())
        self.assertIn("<details>", text)
        self.assertIn("</details>", text)

    def test_render_survives_a_result_with_no_movers(self):
        result = cd.compare({"m::a": 1.0}, {"m::a": 1.0})

        text = cd.render(result, 0.30, "v0.1.0")

        self.assertNotIn("| Test |", text)
        self.assertIn("within budget", text)

    def test_main_exits_1_over_budget_and_0_under(self):
        base = write_junit(self.tmp_path / "base.xml", {"m::a": 1.0})
        slow = write_junit(self.tmp_path / "slow.xml", {"m::a": 2.0})
        fine = write_junit(self.tmp_path / "fine.xml", {"m::a": 1.05})
        summary = self.tmp_path / "summary.md"
        failure = io.StringIO()

        argv = sys.argv
        try:
            sys.argv = ["compare_counters.py", "--base", str(base),
                        "--head", str(slow), "--max-regression", "0.30",
                        "--summary-file", str(summary)]
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(failure):
                self.assertEqual(cd.main(), 1)

            sys.argv = ["compare_counters.py", "--base", str(base),
                        "--head", str(fine), "--max-regression", "0.30"]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cd.main(), 0)
        finally:
            sys.argv = argv

        # The report reaches the summary file even on the failing run — a red
        # gate with no detail is one nobody can act on.
        self.assertIn("REGRESSION", summary.read_text(encoding="utf-8"))
        self.assertEqual(
            failure.getvalue().strip(),
            "FAIL: the shared entries cost +100.0% more than the committed "
            "baseline, over the +30% budget.")

    def test_main_exits_2_when_it_cannot_compare(self):
        base = write_junit(self.tmp_path / "base.xml", {"m::a": 1.0})
        other = write_junit(self.tmp_path / "other.xml", {"m::z": 1.0})

        argv = sys.argv
        try:
            sys.argv = ["compare_counters.py", "--base", str(base),
                        "--head", str(other)]
            # 2, not 1: "could not compare" is a different thing from "slower",
            # and a workflow that conflates them reports a restructure as a
            # performance regression.
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cd.main(), 2)
        finally:
            sys.argv = argv


# The closed-population gate (issue #36).
#
# Permissive mode is what this tool has always done and several of the tests
# above pin it. These pin the other mode: the one where the population is
# declared, so measuring fewer things than declared is a failure rather than
# a quiet, green, one-program-shorter comparison.


def verify(scans, base_folded, require=(), allow=()):
    """verify_population with the head fold done the way main() does it."""
    return cd.verify_population(scans, base_folded, cd.fold_scans(scans),
                                list(require), list(allow))


class TestVerifyPopulation(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp_path = Path(stack.enter_context(tempfile.TemporaryDirectory()))

    def test_no_flags_leaves_removals_permissive(self):
        """The unit-suite comparison must keep behaving exactly as before."""
        scans = [cd.scan_junit(write_junit(
            self.tmp_path / "h.xml", {"m::a": 1.0, "m::gone": 9.0}))]

        # No exception: the base-only name is reported and excluded, not fatal.
        verify(scans, {"m::a": 1.0, "m::gone": 9.0})

    def test_required_name_absent_from_head_is_an_error(self):
        base = write_junit(self.tmp_path / "base.xml", {"e2e::a": 1.0, "e2e::gone": 2.0})
        head = write_junit(self.tmp_path / "head.xml", {"e2e::a": 1.0})
        scans = [cd.scan_junit(head)]

        with self.assertRaises(cd.ComparisonError) as excinfo:
            verify(scans, cd.fold_rounds([base]), ["e2e::a", "e2e::gone"])

        message = str(excinfo.exception)
        # Both the required-name and the closed-set view of the same fact, so
        # the reader does not have to know which check caught it.
        self.assertIn("e2e::gone", message)
        self.assertIn("absent from all 1 head report(s)", message)

    def test_required_name_skipped_in_one_round_is_an_error(self):
        """Skipping is a different fault from absence, and is reported so."""
        base = write_junit(self.tmp_path / "base.xml", {"e2e::a": 1.0})
        r1 = write_junit(self.tmp_path / "r1.xml", {"e2e::a": 1.0, "e2e::b": 2.0})
        r2 = write_junit(self.tmp_path / "r2.xml", {"e2e::a": 1.0, "e2e::b": 2.0},
                         not_passed={"e2e::b": "skipped"})
        scans = [cd.scan_junit(r1), cd.scan_junit(r2)]

        with self.assertRaises(cd.ComparisonError) as excinfo:
            verify(scans, cd.fold_rounds([base]), ["e2e::a", "e2e::b"])

        message = str(excinfo.exception)
        self.assertIn("e2e::b", message)
        # Present in both rounds, so the diagnosis must not say "absent".
        self.assertIn("present in all 2 head report(s) but did not pass in round(s) 2",
                      message)

    def test_required_name_missing_from_one_round_only_names_that_round(self):
        """The mixed branch: absent somewhere, not-passing somewhere else.

        A required name present-and-passing in round 1 and absent from round 2 is
        a real failure, and the sentence must not also claim round 1 failed —
        that was a false observation about a passing round, which is exactly the
        kind of thing a gate message must not say.
        """
        base = write_junit(self.tmp_path / "base.xml", {"e2e::a": 1.0})
        r1 = write_junit(self.tmp_path / "r1.xml", {"e2e::a": 1.0, "e2e::b": 2.0})
        r2 = write_junit(self.tmp_path / "r2.xml", {"e2e::a": 1.0})
        scans = [cd.scan_junit(r1), cd.scan_junit(r2)]

        with self.assertRaises(cd.ComparisonError) as excinfo:
            verify(scans, cd.fold_rounds([base]), ["e2e::a", "e2e::b"])

        message = str(excinfo.exception)
        self.assertIn("absent from head round(s) 2", message)
        # Round 1 passed, so no clause may mention it failing.
        self.assertNotIn("did not pass", message)

    def test_required_name_absent_and_skipped_names_both_rounds(self):
        """One round absent, one round skipped: each is reported as itself."""
        base = write_junit(self.tmp_path / "base.xml", {"e2e::a": 1.0})
        r1 = write_junit(self.tmp_path / "r1.xml", {"e2e::a": 1.0, "e2e::b": 2.0},
                         not_passed={"e2e::b": "skipped"})
        r2 = write_junit(self.tmp_path / "r2.xml", {"e2e::a": 1.0})
        scans = [cd.scan_junit(r1), cd.scan_junit(r2)]

        with self.assertRaises(cd.ComparisonError) as excinfo:
            verify(scans, cd.fold_rounds([base]), ["e2e::a", "e2e::b"])

        message = str(excinfo.exception)
        self.assertIn("absent from head round(s) 2", message)
        self.assertIn("did not pass in round(s) 1", message)

    def test_required_name_present_and_passing_is_not_an_error(self):
        base = write_junit(self.tmp_path / "base.xml", {"e2e::a": 1.0})
        r1 = write_junit(self.tmp_path / "r1.xml", {"e2e::a": 1.0, "e2e::b": 2.0})
        r2 = write_junit(self.tmp_path / "r2.xml", {"e2e::a": 1.0, "e2e::b": 3.0})
        scans = [cd.scan_junit(r1), cd.scan_junit(r2)]

        verify(scans, cd.fold_rounds([base]), ["e2e::a", "e2e::b"])

    def test_baseline_only_removal_is_an_error_in_closed_set_mode(self):
        """The deleted-testcase variant: nothing in the head report says skip."""
        base = write_junit(self.tmp_path / "base.xml", {"e2e::a": 1.0, "e2e::b": 2.0})
        head = write_junit(self.tmp_path / "head.xml", {"e2e::a": 1.0})
        scans = [cd.scan_junit(head)]

        with self.assertRaises(cd.ComparisonError) as excinfo:
            verify(scans, cd.fold_rounds([base]), ["e2e::a"])

        message = str(excinfo.exception)
        self.assertIn("e2e::b", message)
        self.assertIn("--allow-removal", message)

    def test_declared_removal_is_accepted(self):
        base = write_junit(self.tmp_path / "base.xml", {"e2e::a": 1.0, "e2e::b": 2.0})
        head = write_junit(self.tmp_path / "head.xml", {"e2e::a": 1.0})
        scans = [cd.scan_junit(head)]

        verify(scans, cd.fold_rounds([base]), ["e2e::a"], ["e2e::b"])

    def test_allow_removal_alone_still_closes_the_set(self):
        """Either flag turns on closed-set mode; that is what the docs say."""
        base = write_junit(self.tmp_path / "base.xml", {"e2e::a": 1.0, "e2e::b": 2.0})
        head = write_junit(self.tmp_path / "head.xml", {"e2e::a": 1.0})
        scans = [cd.scan_junit(head)]

        with self.assertRaisesRegex(cd.ComparisonError, "e2e::b"):
            verify(scans, cd.fold_rounds([base]), allow=["e2e::a"])

    def test_require_and_allow_the_same_name_is_a_contradiction(self):
        base = write_junit(self.tmp_path / "base.xml", {"e2e::a": 1.0})
        scans = [cd.scan_junit(write_junit(self.tmp_path / "h.xml", {"e2e::a": 1.0}))]

        with self.assertRaisesRegex(cd.ComparisonError, "both required and allowed"):
            verify(scans, cd.fold_rounds([base]), ["e2e::a"], ["e2e::a"])

    def test_fold_scans_rejects_an_empty_list(self):
        """An IndexError here would exit 1, which this file reserves for slower."""
        with self.assertRaisesRegex(cd.ComparisonError, "no JUnit reports given"):
            cd.fold_scans([])

    def test_main_writes_a_comparison_error_to_the_summary_file(self):
        """A red gate with an empty job summary is one nobody can act on."""
        base = write_junit(self.tmp_path / "base.xml", {"e2e::a": 1.0, "e2e::b": 2.0})
        head = write_junit(self.tmp_path / "head.xml", {"e2e::a": 1.0})
        summary = self.tmp_path / "summary.md"

        argv = sys.argv
        try:
            sys.argv = ["compare_counters.py", "--base", str(base),
                        "--head", str(head), "--require-test", "e2e::a",
                        "--base-label", "v1.2.3",
                        "--summary-file", str(summary)]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cd.main(), 2)
        finally:
            sys.argv = argv

        text = summary.read_text(encoding="utf-8")
        self.assertIn("COULD NOT COMPARE", text)
        self.assertIn("v1.2.3", text)
        self.assertIn("e2e::b", text)
