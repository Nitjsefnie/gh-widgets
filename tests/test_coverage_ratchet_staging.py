"""Execute the coverage floor's data-staging step with synthetic inputs."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from scripts.bench.bench_platform import REQUIRES_POSIX_SHELL

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "coverage-ratchet.yml"


class TestCoverageRatchetStaging(unittest.TestCase):
    """Only a strict climb stages a floor artifact and its measured SHA."""

    @classmethod
    def setUpClass(cls):
        document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        cls.stage_step = next(
            step for step in document["jobs"]["measure"]["steps"]
            if step.get("name") == "Stage a climbable floor")
        cls.floor = json.loads((REPO_ROOT / "gate/coverage-floor.json")
                               .read_text(encoding="utf-8"))

    @REQUIRES_POSIX_SHELL
    @unittest.skipUnless(shutil.which("jq"), "the staging step uses jq")
    def test_climb_stages_the_candidate_floor_and_measured_sha(self):
        with tempfile.TemporaryDirectory(prefix="ghw-coverage-stage-") as tmp:
            root = Path(tmp)
            scripts = root / "scripts/ci"
            scripts.mkdir(parents=True)
            shutil.copyfile(REPO_ROOT / "scripts/ci/changes_detect.py",
                            scripts / "changes_detect.py")
            candidate = json.loads(json.dumps(self.floor))
            candidate["floor"] = candidate["measured"] = \
                self.floor["floor"] + 0.1
            (root / "candidate-coverage-floor.json").write_text(
                json.dumps(candidate), encoding="utf-8")
            (root / "gate").mkdir()
            (root / "gate/coverage-floor.json").write_text(
                json.dumps(self.floor), encoding="utf-8")
            runner_temp = root / "runner-temp"
            runner_temp.mkdir()
            summary = root / "summary.md"
            output = root / "output.txt"
            environment = dict(
                os.environ,
                GITHUB_SHA="a" * 40,
                GITHUB_STEP_SUMMARY=str(summary),
                GITHUB_OUTPUT=str(output),
                RUNNER_TEMP=str(runner_temp))
            result = subprocess.run(
                ["bash", "-e", "-o", "pipefail", "-c",
                 self.stage_step["run"]],
                cwd=root, env=environment, capture_output=True, text=True,
                check=False, timeout=10)
            staged = runner_temp / "ratchet-data"
            self.assertEqual(result.returncode, 0,
                             result.stdout + result.stderr)
            self.assertIn("changed=true", output.read_text(encoding="utf-8"))
            self.assertEqual((staged / "base.txt").read_text(encoding="utf-8"),
                             "a" * 40 + "\n")
            self.assertEqual(json.loads((staged / "coverage-floor.json")
                                        .read_text(encoding="utf-8")),
                             candidate)
            self.assertTrue(summary.is_file())

    @REQUIRES_POSIX_SHELL
    @unittest.skipUnless(shutil.which("jq"), "the staging step uses jq")
    def test_equal_floor_stages_nothing(self):
        with tempfile.TemporaryDirectory(prefix="ghw-coverage-no-stage-") as tmp:
            root = Path(tmp)
            scripts = root / "scripts/ci"
            scripts.mkdir(parents=True)
            shutil.copyfile(REPO_ROOT / "scripts/ci/changes_detect.py",
                            scripts / "changes_detect.py")
            (root / "candidate-coverage-floor.json").write_text(
                json.dumps(self.floor), encoding="utf-8")
            (root / "gate").mkdir()
            (root / "gate/coverage-floor.json").write_text(
                json.dumps(self.floor), encoding="utf-8")
            runner_temp = root / "runner-temp"
            runner_temp.mkdir()
            environment = dict(
                os.environ,
                GITHUB_SHA="a" * 40,
                GITHUB_STEP_SUMMARY=str(root / "summary.md"),
                GITHUB_OUTPUT=str(root / "output.txt"),
                RUNNER_TEMP=str(runner_temp))
            result = subprocess.run(
                ["bash", "-e", "-o", "pipefail", "-c",
                 self.stage_step["run"]],
                cwd=root, env=environment, capture_output=True, text=True,
                check=False, timeout=10)
            self.assertEqual(result.returncode, 0,
                             result.stdout + result.stderr)
            self.assertIn("changed=false", (root / "output.txt").read_text(
                encoding="utf-8"))
            self.assertFalse((runner_temp / "ratchet-data").exists())


if __name__ == "__main__":
    unittest.main()
