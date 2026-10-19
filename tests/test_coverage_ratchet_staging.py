"""Execute the coverage floor's data-staging step with synthetic inputs."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from scripts.bench.bench_platform import REQUIRES_POSIX_SHELL
from tests.test_coverage_ratchet import _report

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
        cls.compute_step = next(
            step for step in document["jobs"]["measure"]["steps"]
            if step.get("name") == "Compute the candidate floor")
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
            candidate["measured"] = (round(self.floor["measured"] * 10) + 16) / 10
            candidate["floor"] = (round(candidate["measured"] * 10) - 15) / 10
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

    def _compute_and_stage(self, percent):
        with tempfile.TemporaryDirectory(prefix="ghw-coverage-pipeline-") as tmp:
            root = Path(tmp)
            scripts = root / "scripts/ci"
            scripts.mkdir(parents=True)
            for name in ("changes_detect.py", "coverage_ratchet.py"):
                shutil.copyfile(REPO_ROOT / "scripts/ci" / name, scripts / name)
            binary = root / "bin"
            binary.mkdir()
            (binary / "python").symlink_to(sys.executable)
            document = {**self.floor, "floor": 84.6, "measured": 84.6}
            (root / "gate").mkdir()
            original = json.dumps(document, indent=2) + "\n"
            (root / "gate/coverage-floor.json").write_text(original)
            (root / "coverage.json").write_text(json.dumps(_report(
                percent=percent, statements=1000,
                missing=1000 - round(percent * 10), files=document["files"])))
            environment = dict(os.environ, GITHUB_SHA="a" * 40, RATCHET_BEFORE="",
                               PATH=f"{binary}{os.pathsep}{os.environ['PATH']}",
                               GITHUB_STEP_SUMMARY=str(root / "summary.md"),
                               GITHUB_OUTPUT=str(root / "output.txt"),
                               RUNNER_TEMP=str(root))
            for step in (self.compute_step, self.stage_step):
                result = subprocess.run(
                    ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
                    cwd=root, env=environment, capture_output=True, text=True,
                    check=False, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((root / "gate/coverage-floor.json").read_text(), original)
            candidate = (root / "candidate-coverage-floor.json").read_text()
            staged = root / "ratchet-data"
            files = {path.name: path.read_text() for path in staged.iterdir()} \
                if staged.exists() else {}
            return original, candidate, files, (root / "output.txt").read_text()

    @REQUIRES_POSIX_SHELL
    @unittest.skipUnless(shutil.which("jq"), "the staging step uses jq")
    def test_actual_raise_and_staging_honor_the_hysteresis_boundary(self):
        for percent in (84.7, 85.9, 86.1):
            with self.subTest(no_raise=percent):
                original, candidate, files, output = self._compute_and_stage(percent)
                self.assertEqual(candidate, original)
                self.assertEqual(files, {})
                self.assertIn("changed=false", output)
        _original, candidate, files, output = self._compute_and_stage(86.2)
        self.assertEqual(json.loads(candidate)["floor"], 84.7)
        self.assertEqual(files, {"coverage-floor.json": candidate,
                                 "base.txt": "a" * 40 + "\n"})
        self.assertIn("changed=true", output)


if __name__ == "__main__":
    unittest.main()
