"""The speed gate's interpreter-move route for renderer ceilings."""
import contextlib
import copy
import importlib.util
import io
import json
import unittest
from pathlib import Path

import yaml

from tests import _ghw_temp, test_ci_workflows

REPO_ROOT = Path(__file__).resolve().parents[1]
UNIT = "unit-suite:entries:counter::unit-suite"
RENDERER = "renderer-workloads:entries:e2e::bench.render-impact"


def _load(name):
    path = REPO_ROOT / "scripts" / "ci" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"ghw_move_{name}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load("ratchet_declarations")


def envelope(value):
    return {"min": value, "max": value, "n": 2}


def document(interpreter=None, unit=10.0, renderer=100.0, wall=1.0):
    population = {"metric": "statement_count", "tolerance": 0.05,
                  "population": "a" * 64, "seeds": ["counter::unit-suite"],
                  "entries": {"counter::unit-suite": envelope(unit)},
                  "wall": {"counter::unit-suite": envelope(1.0)}}
    renderers = copy.deepcopy(population)
    renderers["seeds"] = ["e2e::bench.render-impact"]
    renderers["entries"] = {"e2e::bench.render-impact": envelope(renderer)}
    renderers["wall"] = {"e2e::bench.render-impact": envelope(wall)}
    doc = {"schema": 5, "basis": "fixture", "cell": "fixture",
           "measured_commit": "0" * 40, "measured_at": "2026-11-12",
           "populations": {"unit-suite": population,
                           "renderer-workloads": renderers}}
    if interpreter is not None:
        doc["interpreter"] = interpreter
    return doc


class TestInterpreterMoveRoute(unittest.TestCase):
    """A renderer raise is admitted once per move, never by a subject."""

    def setUp(self):
        self.log = io.StringIO()

    def derive(self, old, new, *subjects, running="3.15"):
        self.log = io.StringIO()
        with contextlib.redirect_stderr(self.log):
            return guard.derive_raises(old, new, list(subjects) or ["fix: x"],
                                       running=running)

    def test_a_move_admits_the_raised_renderer_entry(self):
        for before in (None, "3.13"):
            with self.subTest(before=before):
                self.assertEqual(self.derive(document(before),
                                             document("3.15", renderer=120.0)),
                                 [RENDERER])
                self.assertEqual(self.log.getvalue(), f"{RENDERER}: 100.0 on 3.13 -> "
                                 "120.0 on 3.15, admitted by the interpreter move\n")

    def test_without_a_move_a_renderer_raise_stays_undeclared(self):
        for before, after, running in ((None, None, "3.15"),
                                       ("3.15", "3.15", "3.15"),
                                       (None, "3.13", "3.13")):
            with self.subTest(before=before, after=after):
                self.assertEqual(self.derive(
                    document(before), document(after, renderer=120.0),
                    "feat: grow", running=running), [])

    def test_a_move_admits_neither_renderer_wall_nor_a_featureless_unit_raise(self):
        self.assertEqual(self.derive(document(), document("3.15", wall=2.0)), [])
        with self.assertRaises(guard.DeclarationError):
            self.derive(document(), document("3.15", unit=20.0))
        self.assertEqual(self.derive(document(), document("3.15", unit=20.0,
                                                          renderer=120.0),
                                     "feat: move to 3.15"), [UNIT, RENDERER])

    def test_a_move_admits_no_renderer_entry_the_base_never_recorded(self):
        new = document("3.15")
        new["populations"]["renderer-workloads"]["entries"]["e2e::bench.new"] = envelope(5.0)
        self.assertEqual(self.derive(document(), new), [])

    def test_a_lowered_renderer_entry_needs_no_declaration(self):
        self.assertEqual(self.derive(document(), document("3.15", renderer=90.0)), [])

    def test_the_head_must_record_the_running_interpreter(self):
        cases = ((document(), document("3.15"), "3.14", "job measures on 3.14"),
                 (document("3.15"), document(), "3.15", "drops the base's"))
        for old, new, running, fragment in cases:
            with self.subTest(fragment=fragment):
                with self.assertRaises(RuntimeError) as caught:
                    self.derive(old, new, running=running)
                self.assertIn(fragment, str(caught.exception))

    def test_the_validator_refuses_a_malformed_interpreter(self):
        with _ghw_temp.tempdir(prefix="ghw-move-") as root:
            path = Path(root) / "speed-baseline.json"
            for value in ("3.15", "3.10"):
                path.write_text(json.dumps(document(value)), encoding="utf-8")
                guard.baseline.read_baseline(path)
            for value in (3.15, "3", "3.15.1", "py3.15", ""):
                with self.subTest(value=value):
                    path.write_text(json.dumps(document(value)), encoding="utf-8")
                    with self.assertRaises(guard.baseline.ComparisonError):
                        guard.baseline.read_baseline(path)


class TestRatchetPushCarriesTheInterpreter(unittest.TestCase):
    """The automated push keeps `interpreter` an authored, required key."""

    # pylint: disable=protected-access
    _cases = test_ci_workflows.TestRatchetPushWorkflow
    _step = _cases._step
    _validator = _cases._validator
    _run_validator = staticmethod(_cases._run_validator)
    _speed_validator_tree = staticmethod(_cases._speed_validator_tree)
    _speed_validator_case = _cases._speed_validator_case
    _speed_documents = staticmethod(_cases._speed_documents)

    @classmethod
    def setUpClass(cls):
        workflow = yaml.safe_load((test_ci_workflows.WORKFLOWS / "ratchet-push.yml")
                                  .read_text(encoding="utf-8"))
        cls.steps = workflow["jobs"]["push"]["steps"]

    def test_the_artifact_may_neither_change_nor_drop_the_interpreter(self):
        script = self._validator()
        data, repo = self._speed_validator_tree("ghw-speed-interpreter-")
        self.addCleanup(_ghw_temp.rmtree, data.parent)
        recorded, candidate = self._speed_documents()
        self._speed_validator_case(script, data, repo, recorded, candidate, 0, "")
        candidate["interpreter"] = "3.16"
        self._speed_validator_case(script, data, repo, recorded, candidate, 1,
                                   "changed authored key interpreter")
        del candidate["interpreter"]
        self._speed_validator_case(script, data, repo, recorded, candidate, 1,
                                   "unexpected key set")
