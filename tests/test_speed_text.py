"""The tests.yml speed job's shape, asserted as text rather than executed.

    python3 -m unittest discover -v

These read the workflow and check its DECLARATIONS: the block text of a
step, the events a step is gated to, the inventory it hard-codes. None of
them runs anything, so none of them is POSIX-only, and none can fail for a
reason other than the thing it names. The controls that execute a step live
in `test_speed_workflow.py`; `test_speed_ratchet.py` holds the ones that
drive the ratchet.
"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.ci.speed_workflow_steps import StepRunner
from tests.test_baseline import document
from tests.test_compare_counters import cd, write_junit


class TestTheWorkflowText(unittest.TestCase):
    def _run_block(self, step_name):
        """One named step's `run:` body, as the file spells it."""
        lines = StepRunner.job_lines()
        start = lines.index(f"      - name: {step_name}")
        run_line = next(index for index in range(start + 1, len(lines))
                        if lines[index].startswith("        run: |"))
        body = []
        for line in lines[run_line + 1:]:
            if line.startswith("          "):
                body.append(line[10:])
            elif not line.strip():
                body.append("")
            else:
                break
        return "\n".join(body) + "\n"

    def _step_text(self, step_name):
        """One named step's YAML, from its name to the next step."""
        text = StepRunner.job_text()
        start = text.index(f"      - name: {step_name}")
        return text[start:text.index("\n      - ", start + 10)]

    def test_renderer_rounds_keep_the_head_side_and_the_selfcheck(self):
        block = self._run_block("Run renderer workloads")
        self.assertIn('--work-root "$RUNNER_TEMP/gh7-bench"', block)
        self.assertIn('--junit-file "$GH_WIDGETS_REPORTS/bench-head-$round.xml"',
                      block)
        self.assertIn("--repo-root .", block)
        self.assertIn('if [ "$round" -eq "$GH_WIDGETS_ROUNDS" ]; then', block)
        self.assertIn("selfcheck=(--selfcheck)", block)
        # Head only: there is no base checkout any more. A leftover --side
        # loop would measure nothing, because there is nothing to measure it
        # against.
        self.assertIn("--side head", block)
        self.assertNotIn("--side base", block)

    def test_the_ratchet_step_compares_from_the_merge_base_parent(self):
        text = StepRunner.job_text()
        step = self._step_text("The committed baseline only ratchets down")
        self.assertNotIn("github.event.pull_request.base.ref", step)
        self.assertNotIn("github.event.pull_request.base.sha", step)
        self.assertIn("--ratchet-baselines", step)
        self.assertIn("$base_tip:$baseline_at_base", step)
        self.assertIn("working-directory: head", step)
        self.assertIn("id: probe", text)

    def test_the_ratchet_step_declares_seed_redefined_removals(self):
        """The instrument reset's removals must stay declared by the step.

        Issue #269 removes the four retired-instrument envelopes while the
        same document declares each node's id in `seeds`; without the
        derivation the ratchet refuses the reset commit, and once main
        records the seeded entries the loop stays silent again. Both
        conditions live in the step text alone.
        """
        block = self._run_block("The committed baseline only ratchets down")
        self.assertIn("# SEED-DECLARED REMOVALS", block)
        self.assertIn(
            '"$RUNNER_TEMP/base-baseline.json" "$GH_WIDGETS_BASELINE"',
            block)
        self.assertIn('--ratchet-allow-removal "$population:entries:$node"',
                      block)
        # The declaration reads both documents, and the comparator still
        # refuses a declared removal that is still recorded — the loop may
        # only name entries the head document no longer records.
        self.assertIn('if node in new["populations"][name]["entries"]:', block)
        listing_start = block.index("listing=")
        seeds_start = block.index("# SEED-DECLARED REMOVALS")
        self.assertGreater(seeds_start, listing_start,
                           "the seeds loop reads the base copy the listing "
                           "loop's window wrote; it must run after it")

    def test_no_workflow_still_looks_up_a_release_for_the_speed_gate(self):
        # The whole point of issue #81: the comparison point is committed
        # data now, so a tag lookup here would be a leftover argument for a
        # method this gate no longer uses.
        text = StepRunner.job_text()
        self.assertNotIn("releases/latest", text)
        self.assertNotIn("--base-label \"$BASE_TAG\"", text)

        result = cd.compare({"m::a": 10}, {"m::a": 12})
        result.update(metric="cpu_time_ratio", slopes={"m::a": 2})
        report = cd.render(result, 0.30, "baseline")
        # The retired ratio's note is a legacy one-liner now; the net line
        # survives because schema 3/4 documents still fold that way.
        self.assertIn("retired by #269; recorded here so schema 3/4 "
                      "documents keep rendering", report)
        self.assertIn("Compared value: **net of the calibration slope**", report)

    def test_the_statement_metric_note_names_the_instrument(self):
        result = cd.compare({"m::a": 10}, {"m::a": 12})
        result.update(metric="statement_count")
        report = cd.render(result, 0.30, "baseline")
        self.assertIn("deterministic count of sys.monitoring LINE events",
                      report)
        self.assertIn("runner-invariant by construction", report)
        self.assertIn("non-Python subprocess work", report)


class TestTheComparatorBlindWindow(unittest.TestCase):
    """Schema-5 read through the comparator with a seeds-declared window.

    Hosted here because tests/test_compare_counters.py sits at its recorded
    size ceiling; the control drives the same comparator entry point and
    reuses its fixtures.
    """

    UNIT_NODE = "counter::unit-suite"

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.tmp_path = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.baseline_path = self.tmp_path / "baseline.json"
        self.summary_path = self.tmp_path / "summary.md"

    def test_a_schema_five_blind_window_passes_with_the_seed_note(self):
        # The committed shape after the #269 reset: `entries` is empty and
        # the population's seeds declare what awaits its first measurement.
        # The compare passes and reports the seed note, and the metric note
        # names the statement instrument and its blind spot.
        report_path = write_junit(self.tmp_path / "a.xml",
                                  {self.UNIT_NODE: 99_000_000})
        payload = document()
        payload["schema"] = 5
        population = payload["populations"]["unit-suite"]
        population["metric"] = "statement_count"
        population["entries"] = {}
        population["seeds"] = [self.UNIT_NODE]
        self.baseline_path.write_text(json.dumps(payload), encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cd.main([
                "--baseline", str(self.baseline_path), "--population",
                "unit-suite", "--head", str(report_path),
                "--summary-file", str(self.summary_path)]), 0)
        report = output.getvalue()
        self.assertIn("within budget", report)
        self.assertIn("main's ratchet will seed the seed-declared entries",
                      report)
        self.assertIn("runner-invariant by construction", report)
        self.assertIn("non-Python subprocess work", report)
        self.assertTrue(self.summary_path.read_text(encoding="utf-8")
                        .endswith(report.rstrip() + "\n"))


if __name__ == "__main__":
    unittest.main()
