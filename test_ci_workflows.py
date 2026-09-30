"""Invariants over .github/workflows that no workflow run can check itself.

    python3 -m unittest discover -v

Stdlib unittest, matching the rest of this repo's suite.
"""
import re
import unittest
from pathlib import Path


WORKFLOWS = Path(__file__).resolve().parent / ".github" / "workflows"
CODEQL_USE = re.compile(r"uses:\s*github/codeql-action/([\w-]+)@(\S+)")


class TestCodeqlPins(unittest.TestCase):
    """Every github/codeql-action step must run the same release."""

    def test_codeql_action_steps_share_one_ref(self):
        # init writes a config that analyze refuses to load from a different
        # version, so a half-bump fails every CodeQL run. Dependabot treats
        # the two as separate dependencies; dependabot.yml groups them, and
        # this catches a hand edit that splits them again.
        uses = [(path.name, step, ref)
                for path in sorted(WORKFLOWS.glob("*.yml"))
                for step, ref in CODEQL_USE.findall(path.read_text())]
        self.assertTrue(uses, "no github/codeql-action step found")
        self.assertEqual(len({ref for _, _, ref in uses}), 1, uses)


if __name__ == "__main__":
    unittest.main()
