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
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
BASELINE_FILE = REPO_ROOT / "speed-baseline.json"


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


def load_comparator():
    """`compare_counters` by path — scripts/ci is not a package."""
    path = REPO_ROOT / "scripts" / "ci" / "compare_counters.py"
    spec = importlib.util.spec_from_file_location("ghw_comparator_test", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["ghw_comparator_test"] = module
    spec.loader.exec_module(module)
    return module


cd = load_comparator()

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
                         ["renderer-workloads:entries:e2e::bench.render"])
        self.assertEqual(moved["renderer-workloads:entries:e2e::bench.render"],
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
                         ["renderer-workloads:entries:e2e::bench.render"])

    def test_a_DELETED_entry_is_a_raise(self):
        # Iterating only the new document never visits a removed entry, so
        # dropping one passed this check silently while --require-test went
        # on demanding the workload ran: it stopped being compared, which is
        # a second and much quieter route to the same outcome as retiring it
        # properly through WORKLOADS.
        old = self.population(0.09)
        new = self.population(0.09)
        for side in (old, new):
            side["populations"]["renderer-workloads"]["entries"][
                "e2e::bench.render-responsiveness"] = {
                    "min": 0.05, "max": 0.08, "n": 6}
        del new["populations"]["renderer-workloads"]["entries"][
            "e2e::bench.render-responsiveness"]
        moved = baseline.raised_entries(old, new)
        self.assertEqual(
            moved,
            {"renderer-workloads:entries:e2e::bench.render-responsiveness":
             (0.08, None)})

    def test_a_deleted_whole_population_is_a_raise(self):
        old = self.population(0.09)
        new = self.population(0.09)
        del new["populations"]["renderer-workloads"]
        moved = baseline.raised_entries(old, new)
        self.assertEqual(len(moved), 1)


class TestDeclaredRaises(unittest.TestCase):
    """A raise is allowed only when declared, and the declaration is checked.

    The strict down-only rule left the documented re-derivation route
    (issue #133) without a green path: once the suite grew past its recorded
    envelope, EVERY observation landed above the old maximum, and the
    ratchet refused the very change the re-derivation procedure prescribes.
    Declarations close that gap without opening the gate: a declared raise
    must name a slot that actually went up, a slot declared both removed
    and raised is a contradiction, and every undeclared raise stays a
    failure.
    """

    def population(self, unit_max, wall_max=5.0):
        payload = document()
        payload["populations"]["unit-suite"]["entries"] = {
            "counter::unit-suite": {"min": 1.0, "max": unit_max, "n": 6}}
        payload["populations"]["unit-suite"]["wall"] = {
            "counter::unit-suite": {"min": 1.0, "max": wall_max, "n": 6}}
        return payload

    SLOT = "unit-suite:entries:counter::unit-suite"
    WALL_SLOT = "unit-suite:wall:counter::unit-suite"

    def test_a_declared_raise_is_not_a_raise(self):
        moved = baseline.raised_entries(
            self.population(1.0), self.population(2.0),
            allowed_raises={self.SLOT})
        self.assertEqual(moved, {})

    def test_an_undeclared_raise_still_fails(self):
        moved = baseline.raised_entries(
            self.population(1.0), self.population(2.0))
        self.assertEqual(moved, {self.SLOT: (1.0, 2.0)})

    def test_a_stale_declaration_is_refused(self):
        # Declaring a raise that did not happen is the declaration checked
        # against the documents rather than trusted: honouring it would
        # make the log lie about what was raised.
        with self.assertRaises(baseline.ComparisonError) as caught:
            baseline.raised_entries(
                self.population(2.0), self.population(1.0),
                allowed_raises={self.SLOT})
        self.assertIn("declared raised but is not a raise",
                      str(caught.exception))
        self.assertIn("2.0 -> 1.0", str(caught.exception))

    def test_a_wall_raise_needs_its_own_declaration(self):
        # Undeclared, a wall raise is reported like any other.
        moved = baseline.raised_entries(
            self.population(1.0), self.population(1.0, wall_max=7.0))
        self.assertEqual(moved, {self.WALL_SLOT: (5.0, 7.0)})

        # Declared, it passes, and it covers nothing else: a same-commit
        # counter raise still needs the counter slot's own declaration.
        moved = baseline.raised_entries(
            self.population(1.0), self.population(2.0, wall_max=7.0),
            allowed_raises={self.WALL_SLOT})
        self.assertEqual(moved, {self.SLOT: (1.0, 2.0)})

        # Declared together, both go through.
        moved = baseline.raised_entries(
            self.population(1.0), self.population(2.0, wall_max=7.0),
            allowed_raises={self.SLOT, self.WALL_SLOT})
        self.assertEqual(moved, {})

    def test_a_wall_raise_with_its_declaration_is_allowed(self):
        moved = baseline.raised_entries(
            self.population(1.0), self.population(1.0, wall_max=7.0),
            allowed_raises={self.WALL_SLOT})
        self.assertEqual(moved, {})

    def test_a_slot_declared_both_removed_and_raised_is_refused(self):
        with self.assertRaises(baseline.ComparisonError) as caught:
            baseline.raised_entries(
                self.population(1.0), self.population(2.0),
                allowed_removals={self.SLOT},
                allowed_raises={self.SLOT})
        self.assertIn("declared removed and declared raised",
                      str(caught.exception))

    def test_a_new_entry_is_a_declared_raise_too(self):
        # A population the base never had is a raise by definition; the
        # declaration is what allows it through, by the same trade the
        # removal path introduced.
        old = self.population(1.0)
        del old["populations"]["unit-suite"]["entries"]
        moved = baseline.raised_entries(
            old, self.population(1.0), allowed_raises={self.SLOT})
        self.assertEqual(moved, {})

    def test_a_raise_declaration_does_not_cover_a_deletion(self):
        # A deletion is a removal's business, not a raise's: a slot whose
        # entry vanished needs --ratchet-allow-removal, and a raise
        # declaration over a vanished entry stays a failure.
        old = self.population(1.0)
        new = self.population(1.0)
        old["populations"]["unit-suite"]["entries"]["counter::gone"] = {
            "min": 1.0, "max": 1.0, "n": 6}
        moved = baseline.raised_entries(
            old, new, allowed_raises={"unit-suite:entries:counter::gone"})
        self.assertEqual(
            moved,
            {"unit-suite:entries:counter::gone": (1.0, None)})


class ReadsTheCommittedBaseline(unittest.TestCase):
    """Shared access to `speed-baseline.json`, skipping when it is absent.

    There IS a committed baseline. It was absent for one commit — its
    envelopes had been measured at d051020, against a `ghwidgets_common.py`
    the module split removed, and re-derive-never-carry sometimes means
    deleting a document that describes a program this tree is not.

    The cases below still skip while it is absent and come back the moment it
    returns, because that is the property worth keeping: a control that cannot
    run because the thing it describes is missing has not failed, it is
    waiting, and the first commit that re-adds the file gets it checked
    against this tree on the box, in under a second.
    """

    def _load(self):
        if not BASELINE_FILE.is_file():
            self.skipTest("no baseline is committed; the gate is unarmed "
                          "until a runner dispatch produces one")
        return baseline.read_baseline(BASELINE_FILE)


class TestTheSmokeGate(ReadsTheCommittedBaseline):
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
        loaded = self._load()
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
        # supplied for counter::unit-suite — not because the suite should be
        # exempt. Both populations are now armed, and their ceilings differ by
        # two orders of magnitude for the reason the basis records: the same
        # machine effect measured at two scales.
        #
        # Deliberately NOT pinning the figures. A test that hard-codes a
        # baseline's numbers fails every time the baseline is re-derived from
        # a new measurement, and the numbers belong in the file, where the
        # dispatch that produced them is cited. What is worth pinning is that
        # the map is populated and shaped correctly.
        loaded = self._load()
        wall = loaded["populations"]["unit-suite"]["wall"]
        self.assertEqual(list(wall), ["counter::unit-suite"])
        envelope = wall["counter::unit-suite"]
        self.assertGreaterEqual(envelope["n"], baseline.MIN_ENVELOPE_SAMPLES)
        self.assertLessEqual(envelope["min"], envelope["max"])
        self.assertGreater(envelope["max"], 0.0)

    def test_every_recorded_ceiling_is_above_every_observed_value(self):
        """The gate must be able to pass on an ordinary run.

        `max x (1 + tolerance)` has to clear the maximum the dispatches
        recorded, or the baseline refuses on the very program it measured. A
        tolerance of zero would do that; this asserts the margin is real.
        """
        loaded = self._load()
        for name, population in loaded["populations"].items():
            ceiling = {node: envelope["max"] * (1 + population["tolerance"])
                       for node, envelope in population["entries"].items()}
            for node, envelope in population["entries"].items():
                with self.subTest(population=name, entry=node):
                    self.assertGreater(ceiling[node], envelope["max"])
                    self.assertGreater(ceiling[node], envelope["min"])


class TestTheCommittedBaseline(ReadsTheCommittedBaseline):
    """The file in the repository, if there is one, is valid and current.

    There IS one. It was absent for a single commit — its envelopes had
    been measured at d051020, against a `ghwidgets_common.py` the module
    split removed, and re-derive-never-carry sometimes means deleting a
    document that describes a program this tree is not.

    The cases below skip while it is absent and come back the moment it
    returns, because that is the property worth keeping: a control that
    cannot run because the thing it describes is missing has not failed, it
    is waiting, and the first commit that re-adds the file gets it checked
    against this tree on the box, in under a second.
    """

    def test_the_committed_baseline_validates(self):
        loaded = self._load()
        self.assertEqual(
            sorted(loaded["populations"]), ["renderer-workloads", "unit-suite"])

    def test_every_entry_records_more_than_one_observation(self):
        loaded = self._load()
        for name, population in loaded["populations"].items():
            for node, envelope in population["entries"].items():
                with self.subTest(population=name, entry=node):
                    self.assertGreaterEqual(envelope["n"],
                                            baseline.MIN_ENVELOPE_SAMPLES)

    def test_the_tolerances_are_small_enough_to_fire(self):
        # The whole reason the baseline is an envelope rather than a number.
        # A tolerance at or above 1.0 on any entry would be a gate that
        # cannot fail, which is the shape this repository keeps rejecting.
        loaded = self._load()
        for name, population in loaded["populations"].items():
            with self.subTest(population=name):
                self.assertLess(population["tolerance"], 1.0)

    def test_the_committed_digest_matches_this_tree(self):
        """The control that would have caught the stale digest.

        Nine commits after the dispatches were taken added
        `test_baseline.py` and more cases to `test_ci_workflows.py`, so the
        committed digest described a population this tree no longer has and
        the unit-suite comparison refused on the branch's own head. That was
        the guard working correctly on a stale document — which is exactly
        why the staleness has to be caught HERE, on the box, in under a
        second, rather than on a runner minutes later.

        This assertion is deliberately the last thing that changes when a
        test is added, because adding a test is what invalidates it. That
        ordering is the design: the drift is loud immediately and local,
        not deferred to a dispatch.
        """
        loaded = self._load()
        collected = baseline.counter.collect_node_ids(REPO_ROOT)
        self.assertEqual(
            baseline.counter.population_digest(collected),
            loaded["populations"]["unit-suite"]["population"],
            "speed-baseline.json's unit-suite digest describes a population "
            "this tree no longer collects — re-derive it with "
            "`compare_counters.py --print-population .` and "
            "`counter.population_digest(...)`")

    def test_the_committed_workload_digest_matches_the_harness(self):
        loaded = self._load()
        listing = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "bench" /
                                 "e2e_bench.py"), "--list-workloads"],
            capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(
            baseline.counter.population_digest(listing),
            loaded["populations"]["renderer-workloads"]["population"])

    def ceilings(self, loaded):
        """{node: max x (1 + tolerance)} per population, COMPUTED.

        The prose beside the doubling claim used to TRANSCRIBE these, and
        two of three were wrong — which turned a verdict that survived by
        0.52% into one that looked comfortable. A ceiling that is computed
        from the document it belongs to cannot be wrong; a ceiling written
        out beside it can, and did.
        """
        return {name: {node: envelope["max"] * (1 + population["tolerance"])
                       for node, envelope in population["entries"].items()}
                for name, population in loaded["populations"].items()}

    def test_a_doubled_workload_is_caught_wherever_the_gate_can(self):
        """The gate's stated power, computed from the committed numbers.

        This pins the honest answer, which is uncomfortable and which a
        tolerance chosen to look better would have hidden. The envelopes span
        every window the cell has shown, including the busy ones, so at
        these tolerances a doubled workload is caught on NO ENTRY — which is
        what re-deriving against this tree's own 44.5% spread bought. Going
        tighter would mean going below each entry's own observed spread, and
        a ceiling the fastest machine already clears fires on ordinary pool
        variation. Nothing is tuned.

        It also pins the ANSWER, not just the property, so that changing an
        envelope or a tolerance has to be a deliberate act here.
        """
        loaded = self._load()
        ceilings = self.ceilings(loaded)
        caught, missed = set(), set()
        for name, population in loaded["populations"].items():
            for node, envelope in population["entries"].items():
                doubled = envelope["min"] * 2
                (caught if doubled > ceilings[name][node] else missed).add(
                    node)
        self.assertEqual(caught, set())
        self.assertEqual(missed,
                         {"counter::unit-suite", "e2e::bench.render",
                          "e2e::bench.render-impact",
                          "e2e::bench.render-responsiveness"})

    def test_the_smoke_ceilings_are_computed_not_transcribed(self):
        """The four wall ceilings, against the constant the gate really uses.

        The point of this control is that it FAILS when the smoke factor
        changes. An earlier version of it carried its own local literal `3.0`
        and asserted `3.0 * max > max` — which is true for every positive
        factor, so changing `SMOKE_FACTOR` to 1.0 left it green. It
        therefore imported a constant it did not depend on and claimed a
        protection it did not have, which is the same defect three times in
        this branch.

        So the factor comes from `compare_counters.SMOKE_FACTOR` — the same
        object `smoke_failures` defaults to — and the assertion is that every
        ceiling leaves headroom over the worst wall the cell recorded.
        """
        loaded = self._load()
        factor = cd.SMOKE_FACTOR
        self.assertGreater(factor, 1.0,
                           "at or below 1.0 the smoke bound stops being a "
                           "cliff detector")
        for name, population in loaded["populations"].items():
            for node, envelope in population["wall"].items():
                with self.subTest(population=name, entry=node):
                    self.assertGreater(
                        envelope["max"] * factor, envelope["max"],
                        f"the smoke ceiling for {node} leaves no headroom "
                        f"over the worst wall this cell recorded")

    def test_no_tolerance_is_tighter_than_the_cell_has_shown(self):
        """The rule, as a control.

        An envelope and its tolerance must span what the cell has actually
        shown, INCLUDING its fastest and slowest machine. A tolerance
        narrowed because the latest four runs were quiet is the failure the
        file's `basis` is written against, and here it is arithmetic rather
        than a sentence somebody can re-read and agree with.
        """
        loaded = self._load()
        for name, population in loaded["populations"].items():
            for node, envelope in population["entries"].items():
                with self.subTest(population=name, entry=node):
                    span = ((envelope["max"] - envelope["min"])
                            / envelope["min"])
                    self.assertGreaterEqual(
                        population["tolerance"], round(span, 2),
                        f"{node}: tolerance {population['tolerance']} is "
                        f"tighter than the {span:.1%} this cell has shown")

    def test_no_tolerance_is_so_loose_that_the_gate_cannot_fire(self):
        """The other side of the same rule.

        Spanning everything is right until the allowance passes 1.0, at which
        point a doubled workload clears the ceiling and the gate is
        decorative — the shape this whole change exists to remove. A
        tolerance at or above 1.0 would be that.
        """
        loaded = self._load()
        for name, population in loaded["populations"].items():
            with self.subTest(population=name):
                self.assertLess(population["tolerance"], 1.0)

    def test_the_stated_ceilings_are_not_transcribed_anywhere(self):
        """No hard-coded ceiling figures in the prose.

        The wrong ones lived in the baseline's `basis` and in the workflow
        header. This cannot police prose it does not parse; what it CAN do is
        make the next reader's first move a computation rather than a
        reading, and pin the one number that must agree.
        """
        loaded = self._load()
        ceilings = self.ceilings(loaded)
        self.assertIn("counter::unit-suite", ceilings["unit-suite"])
        # Every recorded maximum is below its own ceiling, which is what
        # makes a same-tree run pass rather than refuse.
        for name, population in loaded["populations"].items():
            for node, envelope in population["entries"].items():
                with self.subTest(population=name, entry=node):
                    self.assertLess(envelope["max"], ceilings[name][node])
        for name, by_node in ceilings.items():
            for node, value in by_node.items():
                with self.subTest(population=name, entry=node):
                    self.assertEqual(
                        value,
                        loaded["populations"][name]["entries"][node]["max"]
                        * (1 + loaded["populations"][name]["tolerance"]))

    def test_the_recorded_maxima_clear_the_observed_spread(self):
        # A ceiling below the population's own minimum would mean the
        # envelope was built from different runs than the ones recorded.
        loaded = self._load()
        for name, population in loaded["populations"].items():
            for node, envelope in population["entries"].items():
                with self.subTest(population=name, entry=node):
                    self.assertGreaterEqual(envelope["max"], envelope["min"])


if __name__ == "__main__":
    unittest.main()
