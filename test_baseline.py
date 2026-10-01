"""Tests for the committed baseline's shape and its refusals.

`scripts/ci/baseline.py` exists to answer one question — is this document one
we are willing to judge against? — and every line of it is a way of saying
no. So these tests are mostly about the ways it could say yes wrongly, which
is the failure a gate cannot have: a baseline that validates when it should
not is a gate comparing against a number nobody can account for.

`scripts/ci` is not a package and deliberately has no `__init__.py`, so the
module is loaded by path, the way the renderers load `ghwidgets_common.py`.
"""
import contextlib
import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent


def load_baseline():
    path = REPO_ROOT / "scripts" / "ci" / "baseline.py"
    spec = importlib.util.spec_from_file_location("ghw_baseline_test", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["ghw_baseline_test"] = module
    spec.loader.exec_module(module)
    return module


baseline = load_baseline()

DIGEST = "a" * 64


def document():
    """A valid document, for each test to break in one specific way."""
    return {
        "schema": 3,
        "basis": "why this number sits here",
        "cell": "ubuntu-latest, image ubuntu24/20260927.320, runner 2.337.0",
        "measured_commit": "0" * 40,
        "measured_at": "2026-10-01T00:00:00Z",
        "populations": {
            "unit-suite": {
                "metric": "cpu_time",
                "tolerance": 0.25,
                "population": DIGEST,
                "entries": {"counter::unit-suite":
                            {"min": 10.0, "max": 16.0, "n": 6}},
                "wall": {},
            },
        },
    }


class TestValidation(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(
            tempfile.TemporaryDirectory(prefix="ghw-baseline-")))

    def write(self, payload):
        path = self.root / "speed-baseline.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def refusal(self, payload, fragment):
        """The message the refusal must carry, for the rule under test."""
        with self.assertRaises(baseline.ComparisonError) as caught:
            baseline.read_baseline(self.write(payload))
        self.assertIn(fragment, str(caught.exception))

    def test_a_valid_document_is_accepted(self):
        loaded = baseline.read_baseline(self.write(document()))
        self.assertEqual(sorted(loaded["populations"]), ["unit-suite"])

    def test_a_missing_file_is_the_first_push_not_an_error(self):
        # Exit 0 and a report, never a traceback: two stack traces ahead of
        # every green first run train readers to scroll past the red ones.
        with self.assertRaises(baseline.MissingBaseline) as caught:
            baseline.read_baseline(self.root / "absent.json")
        self.assertIn("first push", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_a_missing_key_is_named(self):
        payload = document()
        del payload["basis"]
        self.refusal(payload, "missing required key(s): basis")

    def test_an_unknown_schema_is_refused_by_number(self):
        payload = document()
        payload["schema"] = 2
        self.refusal(payload, "reads schema 3")

    def test_an_empty_basis_is_refused(self):
        payload = document()
        payload["basis"] = "   "
        self.refusal(payload, "no basis")

    def test_a_population_with_no_metric_is_refused(self):
        payload = document()
        del payload["populations"]["unit-suite"]["metric"]
        self.refusal(payload, "missing required key(s): metric")

    def test_an_unrecognised_metric_is_refused(self):
        payload = document()
        payload["populations"]["unit-suite"]["metric"] = "syscalls"
        self.refusal(payload, "not an instrument")

    def test_a_non_positive_tolerance_is_refused(self):
        payload = document()
        payload["populations"]["unit-suite"]["tolerance"] = 0
        self.refusal(payload, "positive fraction")

    def test_a_population_digest_that_is_not_one_is_refused(self):
        payload = document()
        payload["populations"]["unit-suite"]["population"] = "abc"
        self.refusal(payload, "sha256 digest")

    def test_a_named_population_that_does_not_exist_is_refused(self):
        # No default. Picking one by inference is the quiet accommodation
        # this gate refuses to make.
        loaded = baseline.read_baseline(self.write(document()))
        with self.assertRaises(baseline.ComparisonError) as caught:
            baseline.read_population(loaded, "renderer-workloads")
        self.assertIn("unit-suite", str(caught.exception))


class TestEnvelopes(unittest.TestCase):
    """The shape this schema was changed to, and the ways it is not one."""

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.path = (Path(stack.enter_context(
            tempfile.TemporaryDirectory(prefix="ghw-envelope-")))
            / "speed-baseline.json")

    def refusal(self, envelope, fragment):
        payload = document()
        payload["populations"]["unit-suite"]["entries"][
            "counter::unit-suite"] = envelope
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(baseline.ComparisonError) as caught:
            baseline.read_baseline(self.path)
        self.assertIn(fragment, str(caught.exception))

    def test_a_single_value_where_an_envelope_belongs_is_refused(self):
        # This is what schema 2 looked like, and the message has to say so:
        # a reader holding an old file should learn what changed and why.
        self.refusal(16.0, "records the observed RANGE")

    def test_an_envelope_from_one_observation_is_refused(self):
        # A single sample's maximum is a measurement, not a worst case.
        self.refusal({"min": 10.0, "max": 16.0, "n": 1}, "at least 2")

    def test_an_envelope_missing_its_count_is_refused(self):
        self.refusal({"min": 10.0, "max": 16.0}, "n")

    def test_an_envelope_with_min_above_max_is_refused(self):
        self.refusal({"min": 20.0, "max": 16.0, "n": 6}, "not a range")

    def test_a_negative_bound_is_refused(self):
        self.refusal({"min": -1.0, "max": 16.0, "n": 6}, "non-negative")

    def test_a_fractional_count_is_refused(self):
        self.refusal({"min": 10.0, "max": 16.0, "n": 2.5}, "not an integer")

    def test_an_empty_entries_map_is_refused(self):
        payload = document()
        payload["populations"]["unit-suite"]["entries"] = {}
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(baseline.ComparisonError) as caught:
            baseline.read_baseline(self.path)
        self.assertIn("no entries", str(caught.exception))

    def test_maxima_are_the_numbers_the_head_is_judged_against(self):
        entries = {"a": {"min": 1.0, "max": 9.0, "n": 6},
                   "b": {"min": 2.0, "max": 3.0, "n": 6}}
        self.assertEqual(baseline.envelope_maxima(entries),
                         {"a": 9.0, "b": 3.0})


class TestRatchet(unittest.TestCase):
    """An envelope's ceiling may go down; nothing else may go up."""

    def population(self, render_max):
        payload = document()
        payload["populations"]["renderer-workloads"] = copy.deepcopy(
            payload["populations"]["unit-suite"])
        payload["populations"]["renderer-workloads"]["entries"] = {
            "e2e::bench.render": {"min": 0.05, "max": render_max, "n": 6}}
        payload["populations"]["unit-suite"] = {
            "metric": "cpu_time", "tolerance": 0.25, "population": DIGEST,
            "entries": {"counter::unit-suite":
                        {"min": 1.0, "max": 1.0, "n": 6}}, "wall": {}}
        return payload

    def test_a_raised_maximum_is_a_raise(self):
        moved = baseline.raised_entries(self.population(0.09),
                                        self.population(0.12))
        self.assertEqual(list(moved),
                         ["renderer-workloads:e2e::bench.render"])
        self.assertEqual(moved["renderer-workloads:e2e::bench.render"],
                         (0.09, 0.12))

    def test_a_lowered_maximum_is_not(self):
        moved = baseline.raised_entries(self.population(0.12),
                                        self.population(0.09))
        self.assertEqual(moved, {})

    def test_a_minimum_moving_up_alone_is_not_a_raise(self):
        # The ceiling is the recorded maximum. A faster minimum is an
        # improvement and must not red the ratchet.
        old = self.population(0.09)
        new = self.population(0.09)
        renderer = new["populations"]["renderer-workloads"]["entries"]
        renderer["e2e::bench.render"]["min"] = 0.07
        self.assertEqual(baseline.raised_entries(old, new), {})

    def test_a_population_the_base_never_had_is_a_raise_by_definition(self):
        old = self.population(0.09)
        del old["populations"]["renderer-workloads"]
        moved = baseline.raised_entries(old, self.population(0.09))
        self.assertEqual(list(moved),
                         ["renderer-workloads:e2e::bench.render"])


class TestTheSmokeGate(unittest.TestCase):
    """`wall` is an envelope, and it is armed for the renderer workloads."""

    def test_a_wall_value_that_is_not_an_envelope_is_refused(self):
        # The same refusal as `entries`: with wall spreading 52-63% on this
        # cell, a single stored figure would be an arbitrary pick among min,
        # median and max, and the wrong pick either trips the gate on noise
        # or leaves it deaf.
        payload = document()
        payload["populations"]["unit-suite"]["wall"] = {
            "counter::unit-suite": 5.0}
        with tempfile.TemporaryDirectory(prefix="ghw-wall-") as td:
            path = Path(td) / "b.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(baseline.ComparisonError) as caught:
                baseline.read_baseline(path)
        self.assertIn("wall['counter::unit-suite']", str(caught.exception))
        self.assertIn("observed RANGE", str(caught.exception))

    def test_the_committed_gate_is_armed_for_the_renderer_workloads(self):
        loaded = baseline.read_baseline(REPO_ROOT / "speed-baseline.json")
        wall = loaded["populations"]["renderer-workloads"]["wall"]
        self.assertEqual(
            sorted(wall),
            ["e2e::bench.render", "e2e::bench.render-impact",
             "e2e::bench.render-responsiveness"])
        for node, envelope in wall.items():
            with self.subTest(entry=node):
                self.assertGreaterEqual(envelope["n"],
                                        baseline.MIN_ENVELOPE_SAMPLES)

    def test_the_unit_suite_smoke_bound_is_armed_too(self):
        # It was unarmed for two rounds because no wall observation had been
        # supplied for it — not because the suite should be exempt. Now that
        # the gh-wall attributes of the same six dispatches have been read,
        # both populations are armed, and their ceilings differ by two orders
        # of magnitude for the reason the basis records: the same machine
        # effect measured at two scales.
        loaded = baseline.read_baseline(REPO_ROOT / "speed-baseline.json")
        wall = loaded["populations"]["unit-suite"]["wall"]
        self.assertEqual(list(wall), ["counter::unit-suite"])
        self.assertEqual(wall["counter::unit-suite"],
                         {"min": 41.09, "max": 46.84, "n": 6})

    def test_both_populations_carry_a_smoke_bound(self):
        loaded = baseline.read_baseline(REPO_ROOT / "speed-baseline.json")
        for name, population in loaded["populations"].items():
            with self.subTest(population=name):
                # A population with an empty wall map is a gate that claims
                # less than it could, and it is exactly the gap that went
                # unnoticed for two rounds. Both are armed; this pins it.
                self.assertTrue(population["wall"], name)
                for envelope in population["wall"].values():
                    self.assertGreaterEqual(envelope["n"],
                                            baseline.MIN_ENVELOPE_SAMPLES)


class TestTheCommittedBaseline(unittest.TestCase):
    """The file in the repository is itself valid and says what it is."""

    def test_the_committed_baseline_validates(self):
        loaded = baseline.read_baseline(REPO_ROOT / "speed-baseline.json")
        self.assertEqual(
            sorted(loaded["populations"]), ["renderer-workloads", "unit-suite"])

    def test_every_entry_records_more_than_one_observation(self):
        loaded = baseline.read_baseline(REPO_ROOT / "speed-baseline.json")
        for name, population in loaded["populations"].items():
            for node, envelope in population["entries"].items():
                with self.subTest(population=name, entry=node):
                    self.assertGreaterEqual(envelope["n"],
                                            baseline.MIN_ENVELOPE_SAMPLES)

    def test_the_tolerances_are_small_enough_to_fire(self):
        # The whole reason the baseline is an envelope rather than a number.
        # A tolerance at or above 1.0 on any entry would be a gate that
        # cannot fail, which is the shape this repository keeps rejecting.
        loaded = baseline.read_baseline(REPO_ROOT / "speed-baseline.json")
        for name, population in loaded["populations"].items():
            with self.subTest(population=name):
                self.assertLess(population["tolerance"], 1.0)

    def test_the_recorded_maxima_clear_the_observed_spread(self):
        # A ceiling below the population's own minimum would mean the
        # envelope was built from different runs than the ones recorded.
        loaded = baseline.read_baseline(REPO_ROOT / "speed-baseline.json")
        for name, population in loaded["populations"].items():
            for node, envelope in population["entries"].items():
                with self.subTest(population=name, entry=node):
                    self.assertGreaterEqual(envelope["max"], envelope["min"])


if __name__ == "__main__":
    unittest.main()
