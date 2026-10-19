"""The down-only ratchet: what it must refuse, and what it must let through.

    python3 -m unittest discover -v

The ratchet governs the committed baseline document itself rather than a run
of it, so a mistake in it either lets the ceiling be raised in the same push
that is measured against it, or blocks the legitimate work of retiring a
workload.

Every case here builds two documents and asks the real comparator, or runs
the real step's own derivation fragment sliced out of the `speed` job in
`tests.yml`. The
fragment is sliced rather than reimplemented because a reimplementation is
exactly what let a prefix bug survive two review rounds: `e2e::bench.render`
is a prefix of the other two ids, and a reimplementation that tested the
wrong thing would have gone on passing.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import xml.etree.ElementTree as ET

import yaml

from scripts.bench.bench_platform import REQUIRES_POSIX_SHELL
from scripts.ci.speed_workflow_steps import StepRunner
from tests.test_speed_workflow import CarrierFixture

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPARATOR = REPO_ROOT / "scripts" / "ci" / "compare_counters.py"
HARNESS = REPO_ROOT / "scripts" / "bench" / "e2e_bench.py"
SPEED_RATCHET = REPO_ROOT / "scripts" / "ci" / "speed_ratchet.py"


def load_speed_ratchet():
    """Import the new CLI by path so coverage exercises it in this process."""
    spec = importlib.util.spec_from_file_location(
        "ghw_speed_ratchet_test", SPEED_RATCHET)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SPEED_RATCHET}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["ghw_speed_ratchet_test"] = module
    spec.loader.exec_module(module)
    return module


speed_ratchet = load_speed_ratchet()

WORKLOAD_NODES = ("e2e::bench.render", "e2e::bench.render-impact",
                  "e2e::bench.render-responsiveness")


def envelope(value, samples=6):
    """A baseline entry as an OBSERVED RANGE: min, max and sample count."""
    return {"min": round(value * 0.8, 6), "max": value, "n": samples}


def document(*, impact=1.3514, wall=0.085, drop_impact=False):
    """A baseline document shaped like the committed one, for refusal tests.

    Only the fields the ratchet reads are present; the validator is not under
    test here, `test_baseline.py` owns it.
    """
    entries = {node: envelope(impact if "impact" in node else 1.0)
               for node in WORKLOAD_NODES}
    if drop_impact:
        entries.pop("e2e::bench.render-impact")
    return {
        "schema": 3,
        "basis": "fixture document for the ratchet tests",
        "cell": "fixture",
        "measured_commit": "0" * 40,
        "measured_at": "2026-10-01T00:00:00Z",
        "populations": {
            "unit-suite": {
                "metric": "cpu_time", "tolerance": 0.4, "population": "a" * 64,
                "entries": {"counter::unit-suite": envelope(10.0)},
                "wall": {},
            },
            "renderer-workloads": {
                "metric": "cpu_time", "tolerance": 0.6, "population": "b" * 64,
                "entries": entries,
                "wall": {"e2e::bench.render": envelope(wall)},
            },
        },
    }


class TestTheDownOnlyRatchet(unittest.TestCase):
    steps = StepRunner()

    def temp_root(self, prefix):
        """A directory that outlives this test and not the next."""
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        return Path(stack.enter_context(
            tempfile.TemporaryDirectory(prefix=prefix)))

    def run_ratchet(self, base, head, allow_removals=()):
        """The real comparator, in its ratchet mode, over two documents."""
        root = self.temp_root("ghw-ratchet-")
        base_path = root / "base.json"
        head_path = root / "head.json"
        base_path.write_text(json.dumps(base, indent=2), encoding="utf-8")
        head_path.write_text(json.dumps(head, indent=2), encoding="utf-8")
        command = [sys.executable, str(COMPARATOR), "--ratchet-baselines",
                   str(base_path), str(head_path)]
        for slot in allow_removals:
            command += ["--ratchet-allow-removal", slot]
        return subprocess.run(command, capture_output=True, text=True,
                              check=False)

    # -- a removal nobody declared is a raise -------------------------------

    def test_an_undeclared_removal_is_refused(self):
        refused = self.run_ratchet(document(),
                                   document(drop_impact=True))
        self.assertEqual(refused.returncode, 1,
                         refused.stdout + refused.stderr)
        self.assertIn("renderer-workloads:entries:e2e::bench.render-impact",
                      refused.stderr)

    def test_a_declared_removal_is_the_green_route_out(self):
        """The documented retirement, once the step declares it.

        Retiring a renderer workload means dropping it from WORKLOADS and
        removing its entry. A ratchet that refuses every deletion blocks that
        while a shipped test asserts the route works.
        """
        retired = self.run_ratchet(
            document(), document(drop_impact=True),
            allow_removals=["renderer-workloads:entries:"
                            "e2e::bench.render-impact"])
        self.assertEqual(retired.returncode, 0,
                         retired.stdout + retired.stderr)
        self.assertIn("no entry raised", retired.stdout)

    def test_a_declared_removal_that_is_still_recorded_is_refused(self):
        """The other half of the trade, and it is the half that bites.

        A declared removal that is STILL in the document is an entry deleted
        while its workload stayed: `compare()` would intersect it away
        silently, which is the decorative outcome the ratchet exists to stop.
        """
        refused = self.run_ratchet(
            document(), document(),
            allow_removals=["renderer-workloads:entries:e2e::bench.render"])
        self.assertNotEqual(refused.returncode, 0,
                            refused.stdout + refused.stderr)
        self.assertIn("declared removed but is still recorded",
                      refused.stderr)

    # -- both maps are governed ---------------------------------------------

    def test_the_wall_ceilings_are_governed_too(self):
        """Half the document used to be ungoverned.

        `raised_entries` iterated `entries` and never `wall`, so the smoke
        ceiling could go from 0.085 s to 99 s in the same push and the step
        whose whole reason for existing reported success.
        """
        raised = self.run_ratchet(document(wall=0.085), document(wall=99.0))
        self.assertEqual(raised.returncode, 1, raised.stdout + raised.stderr)
        self.assertIn("renderer-workloads:wall:e2e::bench.render",
                      raised.stderr)

    def test_a_lowered_maximum_is_an_improvement_not_a_raise(self):
        lowered = self.run_ratchet(document(impact=1.4), document(impact=1.2))
        self.assertEqual(lowered.returncode, 0,
                         lowered.stdout + lowered.stderr)
        self.assertIn("no entry raised", lowered.stdout)

    # -- the STEP's derivation, executed ------------------------------------

    @REQUIRES_POSIX_SHELL
    def test_the_ratchet_step_declares_every_retired_workload(self):
        """N3's workflow half, which was broken for a whole review round.

        The step used `case "$listing" in *"$node"*`, and
        `e2e::bench.render` is a PREFIX of the other two, so a retired
        bench.render read as still present and was never declared — leaving
        one of three workloads with no green route to retirement while a
        shipped test asserted the route worked. All three cases, each with
        the other two still listed.
        """
        for retired, still_listed in (
                ("e2e::bench.render",
                 ["e2e::bench.render-impact",
                  "e2e::bench.render-responsiveness"]),
                ("e2e::bench.render-impact",
                 ["e2e::bench.render",
                  "e2e::bench.render-responsiveness"]),
                ("e2e::bench.render-responsiveness",
                 ["e2e::bench.render", "e2e::bench.render-impact"])):
            with self.subTest(retired=retired):
                declared = self._declared_removals(still_listed)
                self.assertEqual(
                    declared, ["--ratchet-allow-removal",
                               f"renderer-workloads:entries:{retired}"],
                    f"retiring {retired} must declare exactly that slot, and "
                    "nothing else: the id prefixes the other two, so a glob "
                    "reports it still present and leaves the workload with "
                    "no route to retirement")

    @REQUIRES_POSIX_SHELL
    def test_listed_workload_is_not_retired_when_more_listing_output_is_delayed(
            self):
        """A match must not close the producer pipe before its later writes."""
        _declared, _working_dir, block = self.steps.step_run(
            "The committed baseline only ratchets down")
        lines = block.splitlines()
        start = next(index for index, line in enumerate(lines)
                     if line.lstrip().startswith(
                         "if ! printf '%s\\n' \"$listing\" | grep"))
        end = next(index for index in range(start + 1, len(lines))
                   if lines[index].strip() == "fi")
        membership = "\n".join(lines[start:end + 1]).replace(
            "printf '%s\\n' \"$listing\"", '"$LISTING_PRODUCER"', 1)

        root = self.temp_root("ghw-retire-delayed-")
        producer = root / "list-workloads.sh"
        producer.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' 'e2e::bench.render-impact'\n"
            "\"$PYTHON\" -c 'import time; time.sleep(0.075)'\n"
            "printf '%s\\n' 'e2e::bench.render-responsiveness'\n",
            encoding="utf-8")
        producer.chmod(0o755)

        script = "\n".join((
            "set -euo pipefail",
            "node='e2e::bench.render-impact'",
            "removals=()",
            membership,
            'printf "%s\\n" "${removals[@]+${removals[@]}}"',
        ))
        env = self.steps.env_for(
            {"LISTING_PRODUCER": str(producer), "PYTHON": sys.executable},
            root)
        # Bound the regression itself as well as its fixture delay so a broken
        # workflow fragment cannot hang the suite indefinitely.
        result = subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", script],
            cwd=root, env=env, capture_output=True, text=True, check=False,
            timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            result.stdout.split(), [],
            "a workload listed before delayed producer output must not be "
            "declared retired")

    @REQUIRES_POSIX_SHELL
    def test_nothing_is_declared_when_the_harness_still_lists_everything(self):
        """The other direction, so the check cannot simply always declare."""
        self.assertEqual(self._declared_removals(list(WORKLOAD_NODES)), [])

    def _declared_removals(self, listing):
        """The STEP's own derivation fragment, run against a fixed listing.

        Sliced out of the `speed` job in `tests.yml` between the line that reads
        `--list-workloads` and the step's announcement of what it derived,
        with only the harness's answer substituted. `$(...)` emits one id per
        line and `grep -x` matches whole lines, so the substitution is
        newline-separated too.
        """
        _declared, _working_dir, block = self.steps.step_run(
            "The committed baseline only ratchets down")
        lines = block.splitlines(True)
        start = next(i for i, line in enumerate(lines)
                     if line.strip().startswith("listing="))
        end = next(i for i, line in enumerate(lines)
                   if "${#removals[@]}" in line)
        fragment = "".join(lines[start:end]).replace(
            'listing="$(python3 scripts/bench/e2e_bench.py --list-workloads)"',
            "listing=$'" + "\n".join(listing) + "'")
        self.assertIn('grep -xF "$node" >/dev/null', fragment,
                      "the step must drain the listing; grep -q can SIGPIPE "
                      "the producer under pipefail")

        root = self.temp_root("ghw-retire-")
        (root / "head" / "scripts" / "ci").mkdir(parents=True)
        (root / "head" / "scripts" / "bench").mkdir(parents=True)
        shutil.copyfile(HARNESS,
                        root / "head" / "scripts" / "bench" / "e2e_bench.py")
        base = root / "base-baseline.json"
        base.write_text(json.dumps(document()), encoding="utf-8")
        script = ("set -euo pipefail\n"
                  f'RUNNER_TEMP="{root}"\n'
                  f'BASELINE="{base}"\n'
                  + fragment
                  + 'printf "%s\\n" "${removals[@]+${removals[@]}}"\n')
        done = self.steps.bash(script, root, "head",
                               self.steps.env_for({"BASELINE": "x"}, root))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done.stdout.split()

    # -- the RAISE derivation, executed --------------------------------------

    def _raise_step(self, old_doc, new_doc, message="", extra=None, shallow=False):
        """Run the STEP's own declaration block against a carrier repository.

        Sliced out of the `speed` job in `tests.yml` from the declaration
        block's header through the loop that builds the comparator args.
        The fragment reads the base copy from $RUNNER_TEMP and the head from
        $BASELINE, exactly as the step does.
        """
        block = self.steps.step_run(
            "The committed baseline only ratchets down")[2]
        lines = block.splitlines(True)
        start = next(i for i, line in enumerate(lines)
                     if line.lstrip().startswith('# DECLARED RAISES'))
        end = next(i for i, line in enumerate(lines)
                   if line.lstrip().startswith('done <<< "$raise_slots"'))
        fragment = "".join(lines[start:end + 1])

        root = self.temp_root("ghw-raise-")
        fixture = CarrierFixture(root, old_doc)
        fixture.commit(message or "Change baseline", new_doc, extra)
        if shallow:
            remote = root / "remote.git"
            fixture.git("clone", "--bare", str(fixture.head), str(remote))
            fixture.git("remote", "add", "origin", remote.as_uri())
            (fixture.head / ".git" / "shallow").write_text(
                fixture.git("rev-parse", "HEAD").stdout + fixture.base + "\n",
                encoding="utf-8")
        script = ("set -euo pipefail\n"
                  f'RUNNER_TEMP="{root}"\n'
                  'BASELINE="gate/speed-baseline.json"\n'
                  f'base_tip="{fixture.base}"\n'
                  + fragment
                  + 'printf "%s\\n" "${raises_args[@]+${raises_args[@]}}"\n')
        return self.steps.bash(script, root, "head",
                               self.steps.env_for({"BASELINE": "x"}, root)), fixture

    def _derived_raises(self, old_doc, new_doc, message=""):
        done, _fixture = self._raise_step(old_doc, new_doc, message)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done.stdout.split()

    @REQUIRES_POSIX_SHELL
    def test_the_ratchet_step_declares_both_unit_raises_separately(self):
        """The raise derivation, executed against two documents.

        The first live run of this derivation printed the slots
        space-joined on one line and the step's `while IFS= read -r` loop
        splits on newlines, so both declarations arrived as ONE undeclared
        slot name and the ratchet refused its own re-derivation — while the
        whole unit suite stayed green, because nothing in it executes this
        fragment. This control does.
        """
        old_doc = document()
        new_doc = document()
        old_doc["populations"]["unit-suite"]["wall"] = {
            "counter::unit-suite": envelope(40.0)}
        new_doc["populations"]["unit-suite"]["entries"][
            "counter::unit-suite"] = envelope(14.0)
        new_doc["populations"]["unit-suite"]["wall"] = {
            "counter::unit-suite": envelope(60.0)}
        message = (CarrierFixture.MESSAGE + "\nBudget-Raise: gate/speed-baseline.json "
                   "unit-suite:wall:counter::unit-suite 40 -> 60")
        derived = self._derived_raises(old_doc, new_doc, message)
        self.assertEqual(derived, [
            "--ratchet-allow-raise",
            "unit-suite:entries:counter::unit-suite",
            "--ratchet-allow-raise",
            "unit-suite:wall:counter::unit-suite",
        ], "a CPU and a wall raise at once must arrive as TWO separate "
           "declarations; one merged argument names no slot and the "
           "ratchet refuses its own re-derivation")

    @REQUIRES_POSIX_SHELL
    def test_the_ratchet_step_declares_nothing_when_nothing_rises(self):
        self.assertEqual(self._derived_raises(document(), document()), [])

    @REQUIRES_POSIX_SHELL
    def test_derived_unit_raises_satisfy_the_real_comparator(self):
        """The workflow boundary: the step's derivation feeds the tool.

        The derived declarations are the arguments the ratchet invocation
        receives, so the pair must compose: unit raises pass with them,
        and a renderer raise — which the step never declares — still
        refuses. This is the accepted-exception's scope, checked at the
        boundary rather than taken from the tool's unit tests.
        """
        root = self.temp_root("ghw-raise-compose-")
        (root / "head" / "scripts" / "ci").mkdir(parents=True)
        for name in ("compare_counters.py", "baseline.py", "counter.py", "changes_detect.py"):
            shutil.copyfile(REPO_ROOT / "scripts" / "ci" / name,
                            root / "head" / "scripts" / "ci" / name)
        comparator = root / "head" / "scripts" / "ci" / "compare_counters.py"

        def ratchet(old_doc, new_doc, message=""):
            base = root / "base-baseline.json"
            base.write_text(json.dumps(old_doc), encoding="utf-8")
            head = root / "head-baseline.json"
            head.write_text(json.dumps(new_doc), encoding="utf-8")
            derived = self._derived_raises(old_doc, new_doc, message)
            return subprocess.run(
                [sys.executable, str(comparator),
                 "--ratchet-baselines", str(base), str(head), *derived],
                capture_output=True, text=True, check=False)

        # Both unit slots rise: the step declares both, the tool accepts.
        old_doc = document()
        new_doc = document()
        old_doc["populations"]["unit-suite"]["wall"] = {
            "counter::unit-suite": envelope(40.0)}
        new_doc["populations"]["unit-suite"]["entries"][
            "counter::unit-suite"] = envelope(14.0)
        new_doc["populations"]["unit-suite"]["wall"] = {
            "counter::unit-suite": envelope(60.0)}
        message = (CarrierFixture.MESSAGE + "\nBudget-Raise: gate/speed-baseline.json "
                   "unit-suite:wall:counter::unit-suite 40 -> 60")
        unit = ratchet(old_doc, new_doc, message)
        self.assertEqual(unit.returncode, 0, unit.stdout + unit.stderr)
        self.assertIn("no entry raised", unit.stdout)

        # A renderer maximum rises: the step derives nothing for it, and
        # the tool refuses — the renderer half stays strictly down-only.
        renderer = ratchet(document(impact=1.3514),
                           document(impact=1.9))
        self.assertEqual(renderer.returncode, 1,
                         renderer.stdout + renderer.stderr)
        self.assertIn("renderer-workloads:entries:e2e::bench.render-impact",
                      renderer.stderr)

    @REQUIRES_POSIX_SHELL
    def test_raise_step_refuses_a_mixed_pr_with_the_split_route(self):
        old, new = document(), document()
        new["populations"]["unit-suite"]["entries"][
            "counter::unit-suite"] = envelope(14)
        result, _fixture = self._raise_step(
            old, new, CarrierFixture.MESSAGE,
            {"test_new.py": "# added control\n"})
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("code PR first (red on speed)", result.stderr)

    @REQUIRES_POSIX_SHELL
    def test_raise_step_refuses_an_undeclared_raise(self):
        old, new = document(), document()
        new["populations"]["unit-suite"]["entries"][
            "counter::unit-suite"] = envelope(14)
        result, _fixture = self._raise_step(old, new)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("has no declaration", result.stderr)

    @REQUIRES_POSIX_SHELL
    def test_raise_step_fetches_the_full_shallow_checkout_message_window(self):
        old, new = document(), document()
        new["populations"]["unit-suite"]["entries"][
            "counter::unit-suite"] = envelope(14)
        result, fixture = self._raise_step(old, new, CarrierFixture.MESSAGE,
                                           shallow=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.split(), ["--ratchet-allow-raise",
                                                 CarrierFixture.SLOT])
        self.assertEqual(fixture.git("rev-parse", "--is-shallow-repository")
                         .stdout.strip(), "false")

    @REQUIRES_POSIX_SHELL
    def test_digest_move_with_test_changes_passes_the_step_and_ratchet(self):
        old, new = document(), document()
        new["populations"]["unit-suite"]["population"] = "c" * 64
        result, fixture = self._raise_step(
            old, new, extra={"test_new.py": "# added control\n"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.split(), [])
        ratchet = subprocess.run(
            [sys.executable, str(COMPARATOR), "--ratchet-baselines",
             str(fixture.base_path), str(fixture.head / "gate/speed-baseline.json")],
            capture_output=True, text=True, check=False)
        self.assertEqual(ratchet.returncode, 0, ratchet.stdout + ratchet.stderr)


class TestMainPushRatchet(unittest.TestCase):
    """Judge the landed window with the real step, including its fetches."""

    steps = StepRunner()

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(
            tempfile.TemporaryDirectory(prefix="ghw-push-ratchet-")))
        self.fixture = CarrierFixture(self.root, document())
        self.fixture.base_path.unlink()
        bench = self.fixture.head / "scripts" / "bench"
        bench.mkdir()
        shutil.copyfile(HARNESS, bench / "e2e_bench.py")
        self.fixture.git("add", "scripts/bench/e2e_bench.py")
        self.fixture.git("commit", "-qm", "Seed workload listing")
        self.fixture.base = self.fixture.git("rev-parse", "HEAD").stdout.strip()

    def _push_step(self, before=None):
        """Start shallow at the landed tip, with a bare origin holding history."""
        fixture = self.fixture
        remote = self.root / "remote.git"
        fixture.git("clone", "--bare", str(fixture.head), str(remote))
        # Keep the seed separately; file:// makes --depth use a real fetch.
        fixture.head.rename(self.root / "source")
        subprocess.run(
            ["git", "clone", "--depth", "1", remote.as_uri(), str(fixture.head)],
            cwd=self.root, capture_output=True, text=True, check=True)
        self.assertEqual(fixture.git("rev-parse", "--is-shallow-repository")
                         .stdout.strip(), "true")
        previous = subprocess.run(
            ["git", "cat-file", "-e", f"{fixture.base}^{{commit}}"],
            cwd=fixture.head, capture_output=True, check=False)
        self.assertNotEqual(previous.returncode, 0,
                            "the shallow checkout must lack the before tip")
        declared, working_dir, block = self.steps.step_run(
            "The committed baseline only ratchets down")
        env = self.steps.env_for(
            declared, self.root, EVENT_NAME="push",
            BEFORE_SHA=fixture.base if before is None else before)
        self.assertFalse(fixture.base_path.exists(),
                         "the step must extract the previous tip's baseline")
        return self.steps.bash(block, self.root, working_dir, env)

    def test_step_schedules_prs_and_main_pushes_only(self):
        step = self.steps.step_text("The committed baseline only ratchets down")
        condition = next(line.strip() for line in step.splitlines()
                         if line.strip().startswith("if:"))
        self.assertEqual(
            condition,
            "if: ${{ github.event_name == 'pull_request' || "
            "(github.event_name == 'push' && github.ref == 'refs/heads/main') }}")
        declared = self.steps.step_run(
            "The committed baseline only ratchets down")[0]
        self.assertEqual(declared["EVENT_NAME"], "${{ github.event_name }}")
        self.assertEqual(declared["BEFORE_SHA"], "${{ github.event.before }}")

    @REQUIRES_POSIX_SHELL
    def test_squash_landing_without_declarations_is_refused_on_push_route(self):
        self.fixture.commit("Squash baseline carrier (#140)", self.fixture.new)
        result = self._push_step()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("has no declaration", result.stderr)
        self.assertNotIn("Declared unit-suite raises:", result.stdout)

    @REQUIRES_POSIX_SHELL
    def test_rebase_landing_keeps_declarations_on_push_route(self):
        self.fixture.commit(self.fixture.MESSAGE, self.fixture.new)
        result = self._push_step()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Declared unit-suite raises: --ratchet-allow-raise "
                      "unit-suite:entries:counter::unit-suite", result.stdout)
        self.assertIn("no entry raised", result.stdout)
        self.assertEqual(self.fixture.git("rev-parse", "--is-shallow-repository")
                         .stdout.strip(), "false")
        self.assertEqual(json.loads(self.fixture.base_path.read_text(
            encoding="utf-8")), self.fixture.old)

    @REQUIRES_POSIX_SHELL
    def test_new_main_ref_fails_closed_on_push_route(self):
        self.fixture.commit(self.fixture.MESSAGE, self.fixture.new)
        result = self._push_step(before="0" * 40)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("newly created main ref has no previous tip", result.stderr)
        self.assertIn("failing closed", result.stderr)

    @REQUIRES_POSIX_SHELL
    def test_missing_before_sha_fails_closed_on_push_route(self):
        self.fixture.commit(self.fixture.MESSAGE, self.fixture.new)
        result = self._push_step(before="")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("BEFORE_SHA is empty", result.stderr)

    @REQUIRES_POSIX_SHELL
    def test_force_push_landing_fails_closed_on_push_route(self):
        fixture = self.fixture
        fixture.commit(fixture.MESSAGE, fixture.new)
        landed = fixture.git("rev-parse", "HEAD").stdout.strip()
        fixture.git("checkout", "-q", "--orphan", "unrelated")
        fixture.git("commit", "-qm", "Unrelated previous main tip")
        unrelated = fixture.git("rev-parse", "HEAD").stdout.strip()
        fixture.git("checkout", "-q", "--detach", landed)
        result = self._push_step(before=unrelated)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("force-push", result.stderr)
        self.assertIn("failing closed", result.stderr)

    @REQUIRES_POSIX_SHELL
    def test_layout_move_compares_against_the_previous_root_baseline(self):
        fixture = self.fixture
        fixture.git("mv", "gate/speed-baseline.json", "speed-baseline.json")
        fixture.git("commit", "-qm", "Seed the previous flat layout")
        fixture.base = fixture.git("rev-parse", "HEAD").stdout.strip()
        (fixture.head / "gate").mkdir(exist_ok=True)
        fixture.git("mv", "speed-baseline.json", "gate/speed-baseline.json")
        fixture.git("commit", "-qm", "Move the unchanged baseline into gate")
        result = self._push_step()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("no entry raised", result.stdout)

    @REQUIRES_POSIX_SHELL
    def test_missing_before_baseline_fails_closed_on_push_route(self):
        fixture = self.fixture
        fixture.git("rm", "gate/speed-baseline.json")
        fixture.git("commit", "-qm", "Previous main has no baseline")
        fixture.base = fixture.git("rev-parse", "HEAD").stdout.strip()
        fixture.commit(fixture.MESSAGE, fixture.new)
        result = self._push_step()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("No gate/speed-baseline.json at the previous main tip",
                      result.stderr)


class TestSpeedRatchetTightening(unittest.TestCase):
    """Lower only maxima justified by the comparator's passing head values."""

    steps = StepRunner()
    UNIT_NODE = "counter::unit-suite"
    UNIT_POPULATION = ["tests.test_one.TestOne.test_a"]

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(
            tempfile.TemporaryDirectory(prefix="ghw-speed-tighten-")))
        self.reports_dir = self.root / "reports"
        self.reports_dir.mkdir()
        self.baseline_path = self.root / "speed-baseline.json"
        self.output_path = self.root / "output" / "speed-baseline.json"
        self.output_path.parent.mkdir()
        self.unit_population_path = self.reports_dir / "unit-population.txt"
        self.renderer_population_path = self.reports_dir / "renderer-population.txt"
        self.unit_reports = [self.reports_dir / f"unit-{number}.xml"
                             for number in (1, 2)]
        self.renderer_reports = [self.reports_dir / f"bench-head-{number}.xml"
                                 for number in (1, 2)]
        self.unit_population_path.write_text(
            "\n".join(self.UNIT_POPULATION) + "\n", encoding="utf-8")
        self.renderer_population_path.write_text(
            "\n".join(WORKLOAD_NODES) + "\n", encoding="utf-8")
        self.baseline = document()
        self.baseline["populations"]["unit-suite"]["population"] = \
            self._population_digest(self.UNIT_POPULATION)
        self.baseline["populations"]["renderer-workloads"]["population"] = \
            self._population_digest(WORKLOAD_NODES)
        self.baseline["populations"]["unit-suite"]["entries"][
            self.UNIT_NODE] = {"min": 5.0, "max": 10.0, "n": 6}
        self.baseline["populations"]["unit-suite"]["wall"] = {
            self.UNIT_NODE: {"min": 10.0, "max": 20.0, "n": 6}}
        self.baseline["populations"]["renderer-workloads"]["wall"] = {
            WORKLOAD_NODES[0]: {"min": 0.04, "max": 0.085, "n": 6}}
        self._write_baseline()

    @staticmethod
    def _population_digest(node_ids):
        joined = "\n".join(sorted(node_ids)) + "\n"
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()

    @staticmethod
    def _write_report(path, entries, walls=None, extra=()):
        suite = ET.Element("testsuite", {
            "name": "fixture", "tests": str(len(entries) + len(extra)),
            "failures": "0", "errors": "0", "skipped": "0",
            "gh-metric": "cpu_time",
        })
        for node, value in [*entries.items(), *extra]:
            classname, name = node.split("::", maxsplit=1)
            attributes = {"classname": classname, "name": name,
                          "time": str(value)}
            if walls and node in walls:
                attributes["gh-wall"] = str(walls[node])
            ET.SubElement(suite, "testcase", attributes)
        ET.ElementTree(suite).write(path, encoding="utf-8",
                                    xml_declaration=True)

    def _write_baseline(self):
        self.baseline_path.write_text(
            json.dumps(self.baseline, indent=2), encoding="utf-8")

    def _write_reports(self, *, unit=9.0, unit_wall=18.0, renderer=None,
                       renderer_wall=0.07, extra=(), malformed=False):
        if renderer is None:
            renderer = {node: 0.9 for node in WORKLOAD_NODES}
            renderer[WORKLOAD_NODES[0]] = 1.0
            renderer[WORKLOAD_NODES[1]] = 1.2
        renderer_walls = {WORKLOAD_NODES[0]: renderer_wall}
        for path in self.unit_reports:
            self._write_report(path, {self.UNIT_NODE: unit},
                               {self.UNIT_NODE: unit_wall})
        for path in self.renderer_reports:
            self._write_report(path, renderer, renderer_walls, extra=extra)
        if malformed:
            self.unit_reports[0].write_text("<testsuite", encoding="utf-8")

    def _run(self):
        command = [
            sys.executable, str(SPEED_RATCHET),
            "--baseline", str(self.baseline_path),
            "--output", str(self.output_path),
            "--unit-population-file", str(self.unit_population_path),
            "--renderer-population-file", str(self.renderer_population_path),
        ]
        for path in self.unit_reports:
            command.extend(("--unit-report", str(path)))
        for path in self.renderer_reports:
            command.extend(("--renderer-report", str(path)))
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
                stderr):
            status = speed_ratchet.main(command[2:])
        return subprocess.CompletedProcess(command, status, stdout.getvalue(),
                                           stderr.getvalue())

    def test_command_line_entrypoint_runs_the_same_tighten(self):
        self._write_reports()
        command = [
            sys.executable, str(SPEED_RATCHET),
            "--baseline", str(self.baseline_path),
            "--output", str(self.output_path),
            "--unit-population-file", str(self.unit_population_path),
            "--renderer-population-file", str(self.renderer_population_path),
        ]
        for path in self.unit_reports:
            command.extend(("--unit-report", str(path)))
        for path in self.renderer_reports:
            command.extend(("--renderer-report", str(path)))

        result = subprocess.run(
            command, capture_output=True, text=True, check=False)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.output_path.is_file())

    def test_workflow_helper_derives_the_main_push_condition(self):
        self.assertEqual(
            self.steps.step_condition("Stage a tightened speed baseline"),
            "${{ success() && github.event_name == 'push' && "
            "github.ref == 'refs/heads/main' }}")

    def test_only_a_tightened_main_push_uploads_the_ratchet_artifact(self):
        workflow = yaml.safe_load(
            (REPO_ROOT / ".github" / "workflows" / "tests.yml")
            .read_text(encoding="utf-8"))
        upload = next(step for step in workflow["jobs"]["speed"]["steps"]
                      if step.get("name") ==
                      "Upload the tightened speed baseline")
        self.assertEqual(
            " ".join(upload["if"].split()),
            "success() && github.event_name == 'push' && "
            "github.ref == 'refs/heads/main' && "
            "steps.tighten.outputs.changed == 'true'")

    def _prepare_step_checkout(self):
        head = self.root / "head"
        scripts = head / "scripts" / "ci"
        scripts.mkdir(parents=True)
        for name in ("speed_ratchet.py", "compare_counters.py", "baseline.py",
                     "counter.py", "changes_detect.py"):
            shutil.copyfile(REPO_ROOT / "scripts" / "ci" / name,
                            scripts / name)
        (head / "gate").mkdir()
        (head / "gate/speed-baseline.json").write_bytes(
            self.baseline_path.read_bytes())
        return head

    def _run_tighten_step(self):
        head = self._prepare_step_checkout()
        declared, working_dir, block = self.steps.step_run(
            "Stage a tightened speed baseline")
        self.assertEqual(working_dir, "head")
        env = self.steps.env_for(declared, self.root,
                                 GITHUB_SHA="a" * 40)
        result = self.steps.bash(block, self.root, working_dir, env)
        output_path = Path(env["GITHUB_OUTPUT"])
        output = (output_path.read_text(encoding="utf-8")
                  if output_path.is_file() else "")
        return result, output, head

    @REQUIRES_POSIX_SHELL
    def test_main_push_step_stages_the_tightened_record_and_measured_base(self):
        self._write_reports()

        result, output, head = self._run_tighten_step()

        data = self.root / "ratchet-data"
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("changed=true", output)
        self.assertEqual((data / "base.txt").read_text(encoding="utf-8"),
                         "a" * 40 + "\n")
        staged = json.loads((data / "speed-baseline.json").read_text(
            encoding="utf-8"))
        self.assertEqual(staged["populations"]["unit-suite"]["entries"]
                         [self.UNIT_NODE]["max"], 9.0)
        self.assertEqual(json.loads((head / "gate/speed-baseline.json")
                                    .read_text(encoding="utf-8")),
                         self.baseline)

    @REQUIRES_POSIX_SHELL
    def test_main_push_step_stages_nothing_when_no_maximum_tightens(self):
        renderer = {node: self.baseline["populations"]["renderer-workloads"]
                    ["entries"][node]["max"] for node in WORKLOAD_NODES}
        self._write_reports(unit=10.0, unit_wall=20.0, renderer=renderer,
                            renderer_wall=0.085)

        result, output, _head = self._run_tighten_step()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("changed=false", output)
        self.assertFalse((self.root / "ratchet-data" / "base.txt").exists())
        self.assertFalse((self.root / "ratchet-data" / "speed-baseline.json")
                         .exists())

    def test_lower_head_values_write_only_the_corresponding_maxima(self):
        self._write_reports()

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        updated = json.loads(self.output_path.read_text(encoding="utf-8"))
        self.assertEqual(
            updated["populations"]["unit-suite"]["entries"][self.UNIT_NODE],
            {"min": 5.0, "max": 9.0, "n": 6})
        self.assertEqual(
            updated["populations"]["unit-suite"]["wall"][self.UNIT_NODE],
            {"min": 10.0, "max": 18.0, "n": 6})
        self.assertEqual(
            updated["populations"]["renderer-workloads"]["entries"]
            [WORKLOAD_NODES[1]], {"min": 1.08112, "max": 1.2, "n": 6})
        self.assertEqual(
            updated["populations"]["renderer-workloads"]["wall"]
            [WORKLOAD_NODES[0]], {"min": 0.04, "max": 0.07, "n": 6})
        self.assertEqual(updated["populations"]["renderer-workloads"]
                         ["entries"][WORKLOAD_NODES[0]],
                         self.baseline["populations"]["renderer-workloads"]
                         ["entries"][WORKLOAD_NODES[0]])

    def test_equal_or_higher_head_values_write_nothing(self):
        before = self.baseline_path.read_bytes()
        self._write_reports(
            unit=10.0, unit_wall=21.0,
            renderer={node: self.baseline["populations"]
                      ["renderer-workloads"]["entries"][node]["max"]
                      for node in WORKLOAD_NODES},
            renderer_wall=0.09)

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.output_path.exists())
        self.assertEqual(self.baseline_path.read_bytes(), before)

    @unittest.skipIf(sys.platform == "win32", "requires POSIX file modes")
    def test_tightening_preserves_the_existing_records_permission_bits(self):
        self.output_path = self.baseline_path
        self._write_reports()
        for mode in (0o600, 0o664):
            with self.subTest(mode=oct(mode)):
                self._write_baseline()
                self.baseline_path.chmod(mode)

                result = self._run()

                self.assertEqual(result.returncode, 0,
                                 result.stdout + result.stderr)
                self.assertEqual(stat.S_IMODE(self.baseline_path.stat().st_mode),
                                 mode)
                updated = json.loads(self.baseline_path.read_text())
                self.assertEqual(updated["populations"]["unit-suite"]["entries"]
                                 [self.UNIT_NODE]["max"], 9.0)

    @unittest.skipIf(sys.platform == "win32", "requires POSIX file modes")
    def test_new_output_uses_the_process_creation_mode_and_restores_umask(self):
        self._write_reports()
        for mask, expected in ((0o077, 0o600), (0o027, 0o640)):
            with self.subTest(umask=oct(mask)):
                self.output_path.unlink(missing_ok=True)
                previous = os.umask(mask)
                try:
                    result = self._run()
                    probe = self.root / "creation-probe"
                    probe.unlink(missing_ok=True)
                    probe.write_bytes(b"probe")
                finally:
                    os.umask(previous)

                self.assertEqual(result.returncode, 0,
                                 result.stdout + result.stderr)
                self.assertEqual(stat.S_IMODE(self.output_path.stat().st_mode),
                                 expected)
                self.assertEqual(stat.S_IMODE(probe.stat().st_mode), expected)

    def test_any_positive_improvement_tightens_without_hysteresis(self):
        self._write_reports(unit=9.999999)

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        updated = json.loads(self.output_path.read_text(encoding="utf-8"))
        self.assertEqual(updated["populations"]["unit-suite"]["entries"]
                         [self.UNIT_NODE]["max"], 9.999999)

    def test_authored_metadata_is_carried_unchanged(self):
        self._write_reports()
        authored = ("schema", "basis", "cell", "measured_commit",
                    "measured_at")

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        updated = json.loads(self.output_path.read_text(encoding="utf-8"))
        for key in authored:
            with self.subTest(key=key):
                self.assertEqual(updated[key], self.baseline[key])
        for name in self.baseline["populations"]:
            for key in ("metric", "tolerance", "population"):
                with self.subTest(population=name, key=key):
                    self.assertEqual(updated["populations"][name][key],
                                     self.baseline["populations"][name][key])

    def test_population_digest_mismatch_refuses_without_writing(self):
        self._write_reports()
        self.unit_population_path.write_text(
            "tests.test_other.TestOne.test_a\n", encoding="utf-8")

        result = self._run()

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("different unit suite", result.stderr)
        self.assertFalse(self.output_path.exists())

    def test_malformed_report_refuses_without_writing(self):
        self._write_reports(malformed=True)

        result = self._run()

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("not parseable JUnit XML", result.stderr)
        self.assertFalse(self.output_path.exists())

    def test_an_unrecorded_report_entry_is_refused(self):
        self._write_reports(extra=[("e2e::bench.unrecorded", 0.1)])

        result = self._run()

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("not recorded", result.stderr)
        self.assertFalse(self.output_path.exists())

    def test_a_tightened_result_must_still_validate_as_a_baseline(self):
        self._write_reports()

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        baseline_path = REPO_ROOT / "scripts" / "ci" / "baseline.py"
        spec = importlib.util.spec_from_file_location(
            "ghw_speed_ratchet_test_baseline", baseline_path)
        self.assertIsNotNone(spec)
        assert spec is not None and spec.loader is not None
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        self.assertEqual(loaded.read_baseline(self.output_path)["schema"], 3)

    def test_a_head_below_the_recorded_minimum_explains_a_successful_noop(self):
        before = self.baseline_path.read_bytes()
        self._write_reports(unit=4.0)

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("below the recorded minimum", result.stdout)
        self.assertFalse(self.output_path.exists())
        self.assertEqual(self.baseline_path.read_bytes(), before)

    @REQUIRES_POSIX_SHELL
    def test_main_push_below_minimum_leaves_the_document_and_artifact_alone(self):
        self._write_reports(unit=4.0)

        result, output, head = self._run_tighten_step()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("below the recorded minimum", result.stdout)
        self.assertIn("changed=false", output)
        self.assertEqual((head / "gate/speed-baseline.json").read_bytes(),
                         self.baseline_path.read_bytes())
        self.assertFalse((self.root / "ratchet-data/base.txt").exists())
        self.assertFalse((self.root / "ratchet-data/speed-baseline.json")
                         .exists())


if __name__ == "__main__":
    unittest.main()
