"""speed.yml's shape, asserted as text rather than executed.

    python3 -m unittest discover -v

These read the workflow and check its DECLARATIONS: the block text of a
step, the events a step is gated to, the inventory it hard-codes. None of
them runs anything, so none of them is POSIX-only, and none can fail for a
reason other than the thing it names. The controls that execute a step live
in `test_speed_workflow.py`; `test_speed_ratchet.py` holds the ones that
drive the ratchet.
"""
import unittest
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent / ".github" / "workflows"
SPEED_YML = WORKFLOWS / "speed.yml"


class TestTheWorkflowText(unittest.TestCase):
    def _run_block(self, step_name):
        """One named step's `run:` body, as the file spells it."""
        lines = SPEED_YML.read_text().splitlines()
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
        text = SPEED_YML.read_text(encoding="utf-8")
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

    def test_the_ratchet_step_is_declared_against_the_base_ref(self):
        text = SPEED_YML.read_text(encoding="utf-8")
        step = self._step_text("The committed baseline only ratchets down")
        # `base.sha` trails the base tip and does not refresh on synchronize,
        # so the comparison would be made against an OLDER, looser baseline —
        # the one direction that is wrong here.
        self.assertIn("github.event.pull_request.base.ref", step)
        self.assertNotIn("github.event.pull_request.base.sha", step)
        self.assertIn("FETCH_HEAD", step)
        self.assertIn("--ratchet-baselines", step)
        self.assertIn("$base_tip:$BASELINE", step)
        self.assertIn("working-directory: head", step)
        self.assertIn("id: probe", text)

    def test_no_workflow_still_looks_up_a_release_for_the_speed_gate(self):
        # The whole point of issue #81: the comparison point is committed
        # data now, so a tag lookup here would be a leftover argument for a
        # method this gate no longer uses.
        text = SPEED_YML.read_text(encoding="utf-8")
        self.assertNotIn("releases/latest", text)
        self.assertNotIn("--base-label \"$BASE_TAG\"", text)


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
