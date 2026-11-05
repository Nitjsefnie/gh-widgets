"""Repository contracts of the comment-share workflow."""
import unittest
from pathlib import Path

import yaml

from tests.test_ratchet_concurrency import RATCHET_SUFFIX

ROOT = Path(__file__).resolve().parents[1]


class TestCommentShareWorkflow(unittest.TestCase):
    def test_concurrency_check_name_and_permissions(self):
        text = (ROOT / ".github/workflows/comment-share.yml").read_text()
        workflow = yaml.safe_load(text)
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(workflow["concurrency"], {
            "group": "comment-share-${{ github.ref }}" + RATCHET_SUFFIX,
            "cancel-in-progress": True})
        self.assertEqual(set(workflow["jobs"]), {"comment-share"})
        job = workflow["jobs"]["comment-share"]
        self.assertNotIn("name", job)


if __name__ == "__main__":
    unittest.main()
