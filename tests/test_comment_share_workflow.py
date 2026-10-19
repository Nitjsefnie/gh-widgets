"""Operational controls for the comment-share workflow and its main-push handoff."""
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


class TestCommentShareWorkflow(unittest.TestCase):
    def test_triggers_permissions_checkout_security_and_staging_conditions(self):
        text = (ROOT / ".github/workflows/comment-share.yml").read_text()
        workflow = yaml.safe_load(text)
        trigger = workflow.get("on", workflow.get(True))
        self.assertEqual(workflow["name"], "comment share")
        self.assertEqual(trigger, {"pull_request": None,
                                   "push": {"branches": ["main"]}})
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(workflow["concurrency"], {
            "group": "comment-share-${{ github.ref }}", "cancel-in-progress": True})
        self.assertEqual(set(workflow["jobs"]), {"comment-share"})
        job = workflow["jobs"]["comment-share"]
        self.assertNotIn("name", job)
        self.assertEqual(job["runs-on"], "ubuntu-latest")
        self.assertEqual(job["timeout-minutes"], 10)
        steps = job["steps"]
        checkouts = [step for step in steps
                     if step.get("uses", "").startswith("actions/checkout@")]
        self.assertTrue(checkouts)
        for checkout in checkouts:
            self.assertIs(checkout.get("with", {}).get("persist-credentials"), False)
        stage = next(step for step in steps if step.get("id") == "stage")
        upload = next(step for step in steps
                      if step.get("name") == "Upload the tightened comment shares")
        self.assertEqual(stage["if"], "${{ success() && github.event_name == 'push' "
                         "&& github.ref == 'refs/heads/main' }}")
        self.assertEqual(upload["if"], "steps.stage.outputs.changed == 'true'")


if __name__ == "__main__":
    unittest.main()
