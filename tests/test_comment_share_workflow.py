"""The pinned comment-ratchet reference shape and its main-push handoff."""
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PIN = "721426819be9e05deb76d56364f671b87fa05fbf"


class TestCommentShareWorkflow(unittest.TestCase):
    def test_reference_triggers_permissions_inputs_and_checkout(self):
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
        checkout, action, stage, upload = job["steps"]
        self.assertEqual(checkout["uses"],
                         "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1")
        self.assertEqual(checkout["with"], {
            "ref": "${{ github.event.pull_request.head.sha || github.sha }}",
            "fetch-depth": 0, "persist-credentials": False})
        self.assertEqual(action["uses"], f"Nitjsefnie-Actions/comment-ratchet@{PIN}")
        self.assertIn(f"Nitjsefnie-Actions/comment-ratchet@{PIN} # v1.0.0", text)
        self.assertEqual(action["with"], {
            "target": "15", "records": ".github/comment-share.json", "mode": "auto",
            "base-ref": "${{ github.event.pull_request.base.sha }}"})
        self.assertEqual(stage["if"], "${{ success() && github.event_name == 'push' "
                         "&& github.ref == 'refs/heads/main' }}")
        self.assertEqual(upload["if"], "steps.stage.outputs.changed == 'true'")
        self.assertEqual(upload["uses"].split("@")[0], "actions/upload-artifact")
        self.assertEqual(upload["with"], {
            "name": "ratchet-push", "retention-days": 1,
            "if-no-files-found": "error", "path":
            "${{ runner.temp }}/ratchet-data/base.txt\n"
            "${{ runner.temp }}/ratchet-data/comment-share.json\n"})


if __name__ == "__main__":
    unittest.main()
