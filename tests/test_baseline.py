"""Tests for the committed baseline's shape and its refusals.

`scripts/ci/baseline.py` answers one question — is this document one we are
willing to judge against? — and every line of it is a way of saying no; a
baseline that validates when it should not is a gate comparing against a
number nobody can account for. `scripts/ci` is not a package and deliberately
has no `__init__.py`, so the module is loaded by path.
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


REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_FILE = REPO_ROOT / "gate/speed-baseline.json"


def load_baseline():
    """baseline.py by path — scripts/ci is not a package."""
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
        "schema": 4,
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
        self.assertEqual(loaded["schema"], 4)
        self.assertEqual(cd.SUPPORTED_SCHEMA, baseline.SUPPORTED_SCHEMA)
        for schema in (3, 4, 5):
            payload = document()
            payload["schema"] = schema
            with self.subTest(schema=schema):
                self.assertEqual(baseline.read_baseline(self.write(payload)),
                                 payload)
        payload = document()
        population = payload["populations"]["unit-suite"]
        population["slopes"] = {"counter::unit-suite": 2.0}
        self.assertEqual(baseline.read_baseline(self.write(payload)), payload)
        population["slopes"]["other"] = 1.0
        self.assertEqual(baseline.read_baseline(self.write(payload)), payload)
        population["entries"] = {}
        self.assertEqual(baseline.read_baseline(self.write(payload)), payload)
        population["entries"] = document()["populations"]["unit-suite"]["entries"]
        invalid = [None, [], {}, {"other": 2.0},
                   {"counter::unit-suite": 2.0, "": 1.0}]
        invalid += [{"counter::unit-suite": value} for value in
                    (0, -1, True, "2", None, float("nan"), float("inf"),
                     float("-inf"))]
        for slopes in invalid:
            with self.subTest(slopes=slopes):
                population["slopes"] = slopes
                path = self.write(payload)
                with self.assertRaises(baseline.ComparisonError) as caught:
                    baseline.read_baseline(path)
                for fragment in (str(path), "unit-suite", "slopes"):
                    self.assertIn(fragment, str(caught.exception))
        for name in ("unit-suite", "renderer-workloads"):
            for slopes in ({"counter::unit-suite": 2}, {}, None):
                payload = document()
                payload["schema"] = 3
                payload["populations"][name] = payload["populations"].pop("unit-suite")
                payload["populations"][name]["slopes"] = slopes
                path = self.write(payload)
                with self.subTest(population=name, slopes=slopes):
                    with self.assertRaises(baseline.ComparisonError) as caught:
                        baseline.read_baseline(path)
                    for fragment in (str(path), name, "slopes", "schema 4"):
                        self.assertIn(fragment, str(caught.exception))

    def test_a_missing_file_is_the_first_push_not_an_error(self):
        # Exit 0 and a report, never a traceback ahead of a green first run.
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
        for schema in (2, 6):
            with self.subTest(schema=schema):
                payload["schema"] = schema
                self.refusal(payload, "reads schema 3, 4 and 5")

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
        # No default: picking one by inference is the accommodation this gate refuses.
        loaded = baseline.read_baseline(self.write(document()))
        with self.assertRaises(baseline.ComparisonError) as caught:
            baseline.read_population(loaded, "renderer-workloads")
        self.assertIn("unit-suite", str(caught.exception))


class TestSchemaFive(TestValidation):
    """Schema 5: slopes refused, `seeds` the entry ids awaiting measurement."""

    @staticmethod
    def seeded(entries=None, seeds=None):
        """A schema-5 document, seeded or not."""
        payload = document()
        payload["schema"] = 5
        population = payload["populations"]["unit-suite"]
        if seeds is not None:
            population["seeds"] = seeds
        if entries is not None:
            population["entries"] = entries
        return payload

    def test_a_schema_five_document_reads_with_entries_or_with_seeds(self):
        with_entries = self.seeded()
        self.assertEqual(baseline.read_baseline(self.write(with_entries)),
                         with_entries)
        seeded = self.seeded(entries={}, seeds=["counter::unit-suite"])
        self.assertEqual(baseline.read_baseline(self.write(seeded)), seeded)

    def test_schema_five_refuses_slopes(self):
        payload = self.seeded(seeds=["counter::unit-suite"])
        payload["populations"]["unit-suite"]["slopes"] = {
            "counter::unit-suite": 2.0}
        self.refusal(payload, "slopes is retired by schema 5")

    def test_seeds_are_refused_before_schema_five(self):
        # No earlier schema carries the seed route.
        for schema in (3, 4):
            with self.subTest(schema=schema):
                payload = document()
                payload["schema"] = schema
                payload["populations"]["unit-suite"]["seeds"] = [
                    "counter::unit-suite"]
                self.refusal(payload, "seeds requires schema 5")

    def test_a_seed_list_must_be_unique_nonempty_strings(self):
        for seeds in ([],
                      ["counter::unit-suite", "counter::unit-suite"],
                      ["counter::unit-suite", ""],
                      ["counter::unit-suite", 4],
                      "counter::unit-suite",
                      {"counter::unit-suite": 2}):
            with self.subTest(seeds=seeds):
                self.refusal(self.seeded(entries={}, seeds=seeds), "seeds")

    def test_an_empty_entries_map_needs_seeds_under_schema_five(self):
        self.refusal(self.seeded(entries={}), "no entries")


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
        # This is what schema 2 looked like, and the message has to say so.
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
    """A ceiling may go down; nothing goes up, except a seed-declared seeding."""

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

    def seeds_population(self):
        payload = document()
        payload["schema"] = 5
        for name in payload["populations"]:
            payload["populations"][name] = {
                "metric": "statement_count", "tolerance": 0.05,
                "population": DIGEST, "entries": {}, "wall": {},
                "seeds": ["counter::unit-suite", "e2e::bench.render"]}
        return payload

    @staticmethod
    def seed(old, node="counter::unit-suite", envelope=None):
        new = copy.deepcopy(old)
        new["populations"]["unit-suite"]["entries"][node] = (
            envelope or {"min": 14.0, "max": 14.0, "n": 2})
        return new

    def test_a_raised_maximum_is_a_raise(self):
        moved = baseline.raised_entries(self.population(0.09),
                                        self.population(0.12))
        self.assertEqual(list(moved),
                         ["renderer-workloads:entries:e2e::bench.render"])
        self.assertEqual(moved["renderer-workloads:entries:e2e::bench.render"],
                         (0.09, 0.12))

    def test_a_lowered_maximum_is_not(self):
        self.assertEqual(baseline.raised_entries(self.population(0.12),
                                                 self.population(0.09)), {})

    def test_a_minimum_moving_up_alone_is_not_a_raise(self):
        # The ceiling is the recorded maximum; a faster minimum is an improvement.
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
        # Iterating only the new document never visits a removed entry.
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

    def test_a_seed_declared_addition_is_not_a_raise(self):
        # The base declares the node in `seeds`, the map is `entries`, and
        # the candidate is seed-shaped; all read from the documents.
        old = self.seeds_population()
        self.assertEqual(baseline.raised_entries(old, self.seed(old)), {})

    def test_an_addition_without_a_seed_declaration_stays_a_raise(self):
        old = self.seeds_population()
        old["populations"]["unit-suite"]["seeds"] = ["counter::other"]
        self.assertEqual(
            baseline.raised_entries(old, self.seed(old)),
            {"unit-suite:entries:counter::unit-suite": (None, 14.0)})

    def test_a_non_seed_shaped_addition_stays_a_raise(self):
        old = self.seeds_population()
        self.assertEqual(
            baseline.raised_entries(
                old, self.seed(old, envelope={"min": 10.0, "max": 14.0,
                                              "n": 2})),
            {"unit-suite:entries:counter::unit-suite": (None, 14.0)})

    def test_a_seed_shaped_wall_addition_stays_a_raise(self):
        # Wall never seeds: the authorization is `entries`-only.
        old = self.seeds_population()
        new = self.seed(old)
        new["populations"]["unit-suite"]["wall"]["counter::unit-suite"] = {
            "min": 65.0, "max": 65.0, "n": 2}
        self.assertEqual(
            baseline.raised_entries(old, new),
            {"unit-suite:wall:counter::unit-suite": (None, 65.0)})

    def test_the_old_slope_seeding_is_no_authorization_under_schema_five(self):
        """A schema-4 base's slopes do NOT authorize a schema-5 seeding."""
        old = self.population(0.09)
        old["populations"]["renderer-workloads"]["slopes"] = {
            "e2e::bench.render": 0.5}
        del old["populations"]["renderer-workloads"]["entries"][
            "e2e::bench.render"]
        new = self.population(0.09)
        new["populations"]["renderer-workloads"]["seeds"] = [
            "e2e::bench.render"]
        new["populations"]["renderer-workloads"]["entries"][
            "e2e::bench.render"] = {"min": 0.12, "max": 0.12, "n": 2}
        moved = baseline.raised_entries(old, new)
        self.assertEqual(
            moved,
            {"renderer-workloads:entries:e2e::bench.render": (None, 0.12)})


class TestDeclaredRaises(unittest.TestCase):
    """A raise is allowed only when declared, and the declaration is checked.

    A declared raise must name a slot that actually went up, a slot declared
    both removed and raised is a contradiction, and every undeclared raise
    stays a failure.
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

    def test_a_declaration_naming_no_existing_slot_is_refused(self):
        # A typo'd declaration names no slot either document has; honouring
        # the silence would make "checked against the documents" true only
        # for the declarations that happen to resolve.
        for typo in ("unit-suite:entries:counter::typo",
                     "typo:entries:counter::unit-suite",
                     "unit-suite:typo:counter::unit-suite"):
            with self.subTest(typo=typo):
                with self.assertRaises(baseline.ComparisonError) as caught:
                    baseline.raised_entries(
                        self.population(1.0), self.population(1.0),
                        allowed_raises={typo})
                self.assertIn("name no slot that exists",
                              str(caught.exception))


class TestFeatureAdmittedRaises(unittest.TestCase):
    """Only a feat: subject in the window admits a unit-suite raise."""

    SLOT = "unit-suite:entries:counter::unit-suite"
    WALL_SLOT = "unit-suite:wall:counter::unit-suite"

    def setUp(self):
        path = REPO_ROOT / "scripts" / "ci" / "ratchet_declarations.py"
        spec = importlib.util.spec_from_file_location("ghw_declarations", path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load {path}")
        self.guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.guard)
        self.old = document()
        self.new = document()
        self.new["populations"]["unit-suite"]["entries"][
            "counter::unit-suite"]["max"] = 20

    def derive(self, *messages, new=None, old=None, **kwargs):
        return self.guard.derive_raises(
            self.old if old is None else old,
            self.new if new is None else new,
            list(messages), **kwargs)

    def test_subjects_the_anchors_admit(self):
        for subject in ("feat: grow the suite", "feat(speed): grow",
                        "feat(): grow", "feat: grow\n\nfix: body text"):
            with self.subTest(subject=subject):
                self.assertEqual(self.derive(subject), [self.SLOT])

    def test_near_miss_subjects_refuse(self):
        for subject in ("feature: grow", "defeat: grow", "Feat: grow",
                        " feat: grow", "feat(speed) grow", "feat(speed: grow",
                        "fix: grow\nfeat: second line",
                        "fix: grow feat: second line", ""):
            with self.subTest(subject=subject):
                with self.assertRaises(self.guard.DeclarationError):
                    self.derive(subject)

    def test_any_window_commit_may_carry_the_feature(self):
        self.assertEqual(self.derive("fix: raise", "feat: the feature"),
                         [self.SLOT])

    def test_every_raised_unit_slot_is_admitted_sorted(self):
        self.old["populations"]["unit-suite"]["wall"] = {
            "counter::unit-suite": {"min": 10, "max": 16, "n": 2}}
        self.new["populations"]["unit-suite"]["wall"] = {
            "counter::unit-suite": {"min": 10, "max": 20, "n": 2}}
        self.assertEqual(self.derive("feat: grow"),
                         [self.SLOT, self.WALL_SLOT])

    def test_the_refusal_states_the_window_slots_and_remedy(self):
        with self.assertRaises(self.guard.DeclarationError) as caught:
            self.derive("fix: grow", window="abc123..HEAD")
        message = str(caught.exception)
        self.assertIn("no commit subject in abc123..HEAD starts with", message)
        self.assertIn(f"- {self.SLOT}: 16.0 -> 20", message)
        self.assertIn("trimming its own tests", message)
        self.assertIn("renderer ceilings stay strictly down-only", message)

    def test_no_raise_derives_nothing_whatever_the_subjects(self):
        for subject in ("fix: tests", "feat: tests"):
            with self.subTest(subject=subject):
                self.assertEqual(self.derive(subject, new=self.old), [])

    def test_renderer_raises_are_never_derived(self):
        old = document()
        old["populations"]["renderer-workloads"] = copy.deepcopy(
            old["populations"]["unit-suite"])
        renderer_up = copy.deepcopy(old)
        renderer_up["populations"]["renderer-workloads"]["entries"][
            "counter::unit-suite"]["max"] = 20
        self.assertEqual(self.derive("feat: x", old=old, new=renderer_up), [])
        renderer_up["populations"]["unit-suite"]["entries"][
            "counter::unit-suite"]["max"] = 20
        self.assertEqual(self.derive("feat: x", old=old, new=renderer_up),
                         [self.SLOT])

    def test_a_seed_shaped_addition_needs_no_feature(self):
        old = document()
        old["schema"] = 5
        old["populations"]["unit-suite"]["entries"] = {}
        old["populations"]["unit-suite"]["seeds"] = ["counter::unit-suite"]
        seeded = copy.deepcopy(old)
        seeded["populations"]["unit-suite"]["entries"] = {
            "counter::unit-suite": {"min": 14.0, "max": 14.0, "n": 2}}
        self.assertEqual(self.derive("chore: seed", old=old, new=seeded), [])

    def test_a_non_seed_shaped_addition_is_refused_whatever_the_subject(self):
        old = document()
        old["populations"]["unit-suite"]["entries"] = {}
        added = copy.deepcopy(old)
        added["populations"]["unit-suite"]["entries"]["counter::added"] = {
            "min": 10.0, "max": 50.0, "n": 2}
        for subject in ("feat: add the entry", "chore: add the entry"):
            with self.subTest(subject=subject):
                with self.assertRaises(self.guard.DeclarationError) as caught:
                    self.derive(subject, old=old, new=added)
                message = str(caught.exception)
                self.assertIn("counter::added", message)
                self.assertIn("None -> 50.0", message)
                self.assertIn("trimming its own tests", message)

    def test_a_slope_declared_removal_derives_no_raise(self):
        old = document()
        removed = copy.deepcopy(old)
        del removed["populations"]["unit-suite"]["entries"][
            "counter::unit-suite"]
        removed["populations"]["unit-suite"]["slopes"] = {
            "counter::unit-suite": 2.0}
        self.assertEqual(self.derive("chore: reset", old=old, new=removed),
                         [])

    def test_a_slope_declared_removal_stays_no_raise_with_a_feature(self):
        old = document()
        removed = copy.deepcopy(old)
        del removed["populations"]["unit-suite"]["entries"][
            "counter::unit-suite"]
        removed["populations"]["unit-suite"]["slopes"] = {
            "counter::unit-suite": 2.0}
        self.assertEqual(self.derive("feat: reset", old=old, new=removed),
                         [])


class ReadsTheCommittedBaseline(unittest.TestCase):
    """Shared access to the committed `speed-baseline.json`, skipping when absent.

    A control that cannot run because the thing it describes is missing has
    not failed, it is waiting; the commit that re-adds the file gets it
    checked against this tree on the box, in under a second.
    """

    def _load(self):
        if not BASELINE_FILE.is_file():
            self.skipTest("no baseline is committed; the gate is unarmed "
                          "until a runner dispatch produces one")
        return baseline.read_baseline(BASELINE_FILE)


class TestTheSmokeGate(ReadsTheCommittedBaseline):
    """`wall` is an envelope, and it is armed for the renderer workloads."""

    def test_a_wall_value_that_is_not_an_envelope_is_refused(self):
        # Same refusal as `entries`: a single stored wall figure trips the
        # gate on noise or leaves it deaf.
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
        loaded = self._load()  # three workloads, every envelope n >= 2
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
        # Deliberately NOT pinning the figures: a test that hard-codes a
        # baseline's numbers fails every re-derivation. Pin that the map is
        # populated and shaped correctly.
        loaded = self._load()
        wall = loaded["populations"]["unit-suite"]["wall"]
        self.assertEqual(list(wall), ["counter::unit-suite"])
        envelope = wall["counter::unit-suite"]
        self.assertGreaterEqual(envelope["n"], baseline.MIN_ENVELOPE_SAMPLES)
        self.assertLessEqual(envelope["min"], envelope["max"])
        self.assertGreater(envelope["max"], 0.0)

    def test_every_recorded_ceiling_is_above_every_observed_value(self):
        """`max x (1 + tolerance)` must clear the recorded maximum, or the
        baseline refuses on the very program it measured."""
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
        self.assertEqual(sorted(loaded["populations"]),
                         ["renderer-workloads", "unit-suite"])

    def test_every_entry_records_more_than_one_observation(self):
        loaded = self._load()
        for name, population in loaded["populations"].items():
            for node, envelope in population["entries"].items():
                with self.subTest(population=name, entry=node):
                    self.assertGreaterEqual(envelope["n"],
                                            baseline.MIN_ENVELOPE_SAMPLES)

    def test_the_tolerances_are_small_enough_to_fire(self):
        # A tolerance at or above 1.0 on any entry is a gate that cannot fail.
        loaded = self._load()
        for name, population in loaded["populations"].items():
            with self.subTest(population=name):
                self.assertLess(population["tolerance"], 1.0)

    def test_the_committed_digest_matches_this_tree(self):
        """The control that catches a stale digest.

        Adding a test invalidates the committed digest, so this assertion is
        deliberately the last thing that changes when a test is added: the
        drift fails HERE on the box, in under a second, not on a runner.
        """
        loaded = self._load()
        collected = baseline.counter.collect_node_ids(REPO_ROOT / "tests")
        self.assertEqual(
            baseline.counter.population_digest(collected),
            loaded["populations"]["unit-suite"]["population"],
            "speed-baseline.json's unit-suite digest describes a population "
            "this tree no longer collects — re-derive it with "
            "`compare_counters.py --print-population tests` and "
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
        """Derive doubling detection from the current tightened record.

        While `entries` is {} (the declared blind window) this greens with
        zero assertions exercised; it starts judging when main's ratchet
        seeds, the same waiting shape as the absent-baseline skip.
        """
        loaded = self._load()
        for name, population in loaded["populations"].items():
            entries = population["entries"]
            doubled = {node: envelope["min"] * 2
                       for node, envelope in entries.items()}
            maxima = {node: envelope["max"]
                      for node, envelope in entries.items()}
            expected = {node for node, envelope in entries.items()
                        if 2 * envelope["min"] > envelope["max"]
                        * (1 + population["tolerance"])}
            reported = cd.over_tolerance(maxima, doubled,
                                         population["tolerance"])
            with self.subTest(population=name, caught=sorted(expected)):
                self.assertEqual(set(reported), expected)
                for node, values in reported.items():
                    self.assertEqual(values, (maxima[node], doubled[node]))

    def test_the_smoke_ceilings_are_computed_not_transcribed(self):
        """The wall ceilings, against the constant the gate really uses.

        An earlier version carried its own literal `3.0` and stayed green when
        `SMOKE_FACTOR` changed to 1.0 — it claimed a protection it did not
        have. So the factor comes from `compare_counters.SMOKE_FACTOR`, and
        every ceiling must leave headroom over the worst wall recorded.
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
        """An envelope's tolerance must span what the cell has actually shown,
        INCLUDING its fastest and slowest machine — here as arithmetic, not a
        sentence somebody can re-read and agree with. Greens vacuously while
        `entries` is {} (the blind window); it judges once main seeds.
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
        """The other side: past 1.0 a doubled workload clears the ceiling and
        the gate is decorative — the shape this whole change exists to remove.
        Vacuous while `entries` is {}; the tolerance loop needs a seeded entry.
        """
        loaded = self._load()
        for name, population in loaded["populations"].items():
            with self.subTest(population=name):
                self.assertLess(population["tolerance"], 1.0)

    def test_the_stated_ceilings_are_not_transcribed_anywhere(self):
        """No hard-coded ceiling figures in the prose (the wrong ones lived in
        the baseline's `basis` and in the workflow header). The suite's own
        entry is named only while the population records entries at all: the
        #269 reset empties `entries` for the declared blind window and the
        automated ratchet re-seeds the node after the merge.
        """
        loaded = self._load()
        ceilings = self.ceilings(loaded)
        if loaded["populations"]["unit-suite"]["entries"]:
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
