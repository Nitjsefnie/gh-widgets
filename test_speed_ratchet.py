"""The down-only ratchet: what it must refuse, and what it must let through.

    python3 -m unittest discover -v

The ratchet governs the committed baseline document itself rather than a run
of it, so a mistake in it either lets the ceiling be raised in the same push
that is measured against it, or blocks the legitimate work of retiring a
workload.

Every case here builds two documents and asks the real comparator, or runs
the real step's own derivation fragment sliced out of `speed.yml`. The
fragment is sliced rather than reimplemented because a reimplementation is
exactly what let a prefix bug survive two review rounds: `e2e::bench.render`
is a prefix of the other two ids, and a reimplementation that tested the
wrong thing would have gone on passing.
"""
import contextlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from speed_workflow_steps import StepRunner

REPO_ROOT = Path(__file__).resolve().parent
COMPARATOR = REPO_ROOT / "scripts" / "ci" / "compare_durations.py"
HARNESS = REPO_ROOT / "scripts" / "bench" / "e2e_bench.py"

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

    def test_nothing_is_declared_when_the_harness_still_lists_everything(self):
        """The other direction, so the check cannot simply always declare."""
        self.assertEqual(self._declared_removals(list(WORKLOAD_NODES)), [])

    def _declared_removals(self, listing):
        """The STEP's own derivation fragment, run against a fixed listing.

        Sliced out of `speed.yml` between the line that reads
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
        self.assertIn("grep -qxF", fragment,
                      "the step matches a listed workload with a glob, so a "
                      "retired e2e::bench.render reads as present — it "
                      "prefixes the other two ids")

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


if __name__ == "__main__":
    unittest.main()
