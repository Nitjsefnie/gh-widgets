"""Operational controls for the size-baseline workflow."""
import unittest
from pathlib import Path

import yaml

from tests.test_ratchet_concurrency import RATCHET_SUFFIX

ROOT = Path(__file__).resolve().parents[1]
GATED = ("github.event_name == 'pull_request' || (github.event_name == 'push' "
         "&& github.ref_type == 'branch' "
         "&& github.ref_name == github.event.repository.default_branch)")


class TestSizeBaselineWorkflow(unittest.TestCase):
    def test_triggers_permissions_concurrency_and_gated_steps(self):
        text = (ROOT / ".github/workflows/size-baseline.yml").read_text()
        workflow = yaml.safe_load(text)
        trigger = workflow.get("on", workflow.get(True))
        self.assertEqual(workflow["name"], "size baseline")
        self.assertEqual(trigger, {"pull_request": None,
                                   "push": {"branches": ["main", "master"]},
                                   "workflow_call": None})
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(workflow["concurrency"], {
            "group": "size-baseline-${{ github.ref }}" + RATCHET_SUFFIX,
            "cancel-in-progress": True})
        self.assertEqual(set(workflow["jobs"]), {"size-baseline"})
        job = workflow["jobs"]["size-baseline"]
        self.assertNotIn("name", job)
        self.assertEqual(job["environment"], (
            "${{ github.event_name == 'push' && github.ref_type == 'branch' "
            "&& github.ref_name == github.event.repository.default_branch "
            "&& format('{0}-push', github.event.repository.default_branch) "
            "|| '' }}"))
        self.assertEqual(job["runs-on"], "ubuntu-latest")
        self.assertEqual(job["timeout-minutes"], 10)
        steps = job["steps"]
        self.assertEqual([step["uses"].split("@")[0] for step in steps],
                         ["actions/checkout", "Nitjsefnie-Actions/size-ratchet"])
        for step in steps:
            self.assertEqual(step["if"], GATED)


if __name__ == "__main__":
    unittest.main()
