"""Operational controls for the size-baseline workflow and its handoff."""
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


class TestSizeBaselineWorkflow(unittest.TestCase):
    def test_triggers_permissions_checkout_security_and_staging_conditions(self):
        text = (ROOT / ".github/workflows/size-baseline.yml").read_text()
        workflow = yaml.safe_load(text)
        trigger = workflow.get("on", workflow.get(True))
        self.assertEqual(workflow["name"], "size baseline")
        self.assertEqual(trigger, {"pull_request": None,
                                   "push": {"branches": ["main"]}})
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(workflow["concurrency"], {
            "group": "size-baseline-${{ github.ref }}",
            "cancel-in-progress": True})
        self.assertEqual(set(workflow["jobs"]), {"size-baseline"})
        job = workflow["jobs"]["size-baseline"]
        self.assertNotIn("name", job)
        self.assertEqual(job["runs-on"], "ubuntu-latest")
        self.assertEqual(job["timeout-minutes"], 10)
        steps = job["steps"]
        stage = next(step for step in steps if step.get("id") == "stage")
        upload = next(step for step in steps
                      if step.get("name") == "Upload the tightened size records")
        self.assertEqual(stage["if"], "${{ success() && github.event_name == 'push' "
                         "&& github.ref == 'refs/heads/main' }}")
        self.assertEqual(upload["if"], "steps.stage.outputs.changed == 'true'")

    def test_staging_diffs_the_size_record_and_stages_both_files(self):
        workflow = yaml.safe_load(
            (ROOT / ".github/workflows/size-baseline.yml").read_text())
        steps = workflow["jobs"]["size-baseline"]["steps"]
        stage = next(step for step in steps if step.get("id") == "stage")
        self.assertEqual(stage["env"]["GH_WIDGETS_RATCHET_BEFORE"],
                         "${{ github.event.before }}")
        self.assertEqual(stage["env"]["GH_TOKEN"], "${{ github.token }}")
        run = stage["run"]
        self.assertIn("python3 scripts/ci/changes_detect.py --ratchet-data-only",
                      run)
        self.assertIn("git diff --quiet HEAD -- .github/size-baseline.json",
                      run)


if __name__ == "__main__":
    unittest.main()
