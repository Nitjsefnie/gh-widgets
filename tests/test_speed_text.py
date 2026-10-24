"""The tests.yml speed job's shape, asserted as text rather than executed.

    python3 -m unittest discover -v

These read the workflow and check its DECLARATIONS: the block text of a
step, the events a step is gated to, the inventory it hard-codes. None of
them runs anything, so none of them is POSIX-only, and none can fail for a
reason other than the thing it names. The controls that execute a step live
in `test_speed_workflow.py`; `test_speed_ratchet.py` holds the ones that
drive the ratchet.
"""
import unittest
from scripts.ci.speed_workflow_steps import StepRunner
from tests.test_compare_counters import cd


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
        self.assertIn('--junit-file "$REPORTS/bench-head-$round.xml"', block)
        self.assertIn("--repo-root .", block)
        self.assertIn('if [ "$round" -eq "$ROUNDS" ]; then', block)
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

    def test_the_ratchet_step_declares_slope_redefined_removals(self):
        """The instrument reset's removals must stay declared by the step.

        Issue #240 removes the four redefined ratio envelopes while the
        same document declares each node's slope; without the derivation
        the ratchet refuses the reset commit, and once main records the
        seeds the loop stays silent again. Both conditions live in the
        step text alone.
        """
        block = self._run_block("The committed baseline only ratchets down")
        self.assertIn("# SLOPE-DECLARED REMOVALS", block)
        self.assertIn('"$RUNNER_TEMP/base-baseline.json" "$BASELINE"', block)
        self.assertIn('--ratchet-allow-removal "$population:entries:$node"',
                      block)
        # The declaration reads both documents, and the comparator still
        # refuses a declared removal that is still recorded — the loop may
        # only name entries the head document no longer records.
        self.assertIn('if node in new["populations"][name]["entries"]:', block)
        listing_start = block.index("listing=")
        slope_start = block.index("# SLOPE-DECLARED REMOVALS")
        self.assertGreater(slope_start, listing_start,
                           "the slope loop reads the base copy the listing "
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
        self.assertIn(
            "The gate judges the NET value (raw − slope·calibration, "
            "per-entry slopes in the baseline) when the population declares "
            "slopes; the ratio remains what the JUnit records.", report)
        self.assertIn("Compared value: **net of the calibration slope**", report)


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
