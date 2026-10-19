"""Derived upload namespaces and executable suppression of ratchet cascades."""
import contextlib
import copy
import io
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
from scripts.ci import changes_detect as cd
from tests.test_changes_detect import FakeTransport

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
PRODUCERS = {
    ("tests.yml", "speed", "Upload the tightened speed baseline"),
    ("coverage-ratchet.yml", "measure", "Upload the tightened coverage floor"),
    ("comment-share.yml", "comment-share", "Upload the tightened comment shares"),
}


def workflows():
    """Decode every workflow, including jobs outside today's producer set."""
    return {path.name: yaml.safe_load(path.read_text(encoding="utf-8"))
            for path in WORKFLOWS.glob("*.yml")}


def upload_namespace(documents):
    """Each name has one declaration per run; return all ratchet producers."""
    producers = set()
    for filename, document in documents.items():
        names = {}
        for job_id, job in document["jobs"].items():
            for step in job.get("steps", []):
                if step.get("uses", "").split("@")[0] != "actions/upload-artifact":
                    continue
                name = step.get("with", {}).get("name", "artifact")
                location = (filename, job_id, step.get("name"))
                if name in names:
                    raise AssertionError(f"duplicate artifact {name}: "
                                         f"{names[name]} and {location}")
                names[name] = location
                if name == "ratchet-push":
                    producers.add(location)
    return producers


class TestUploadNamespaces(unittest.TestCase):
    def test_every_upload_name_has_one_producer_per_workflow(self):
        self.assertEqual(upload_namespace(workflows()), PRODUCERS)

    def test_copying_an_upload_under_another_step_name_fails_the_control(self):
        documents = workflows()
        steps = documents["tests.yml"]["jobs"]["speed"]["steps"]
        original = next(step for step in steps
                        if step.get("with", {}).get("name") == "ratchet-push")
        mutant = copy.deepcopy(original)
        mutant["name"] = "Copy the tightened speed baseline"
        steps.append(mutant)
        with self.assertRaisesRegex(AssertionError, "duplicate artifact ratchet-push"):
            upload_namespace(documents)


class TestRatchetDataWindow(unittest.TestCase):
    def test_only_complete_data_windows_skip_and_name_the_compared_shas(self):
        records = {"gate/coverage-floor.json", "gate/speed-baseline.json",
                   ".github/comment-share.json"}
        self.assertEqual(cd.RATCHET_DATA, records)
        environment = {"GITHUB_REPOSITORY": "owner/repo",
                       "GITHUB_SHA": "b" * 40, "RATCHET_BEFORE": "a" * 40}
        endpoint = f"repos/owner/repo/compare/{'a' * 40}...{'b' * 40}"
        cases = [(list(records), 0), (["src/render.py"], 1), ([], 1)]
        cases.extend(([path], 0) for path in records)
        for paths, expected in cases:
            transport = FakeTransport({endpoint: {
                "status": "ahead", "files": [{"filename": p} for p in paths]}})
            stdout = io.StringIO()
            with self.subTest(paths=paths), contextlib.redirect_stdout(stdout):
                self.assertEqual(cd.main(["--ratchet-data-only"],
                                         transport=transport,
                                         environ=environment), expected)
                self.assertEqual(transport.calls, [(endpoint, False, True)])
                if expected == 0:
                    self.assertIn("::notice::", stdout.getvalue())
                    self.assertIn(f"{'a' * 40}...{'b' * 40}", stdout.getvalue())

    def test_unknown_or_incomplete_windows_stage_normally(self):
        environment = {"GITHUB_REPOSITORY": "owner/repo",
                       "GITHUB_SHA": "b" * 40, "RATCHET_BEFORE": "a" * 40}
        endpoint = f"repos/owner/repo/compare/{'a' * 40}...{'b' * 40}"
        cases = (None, {}, {"status": "behind", "files": []},
                 {"status": "ahead", "files": [{}]},
                 {"status": "ahead", "files": [{"filename": p}
                                               for p in cd.RATCHET_DATA] * 100},
                 {"status": "ahead", "files": [{
                     "filename": "gate/speed-baseline.json", "status": "renamed",
                     "previous_filename": "src/retired.py"}]})
        for response in cases:
            with self.subTest(response=response), contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(cd.ratchet_data_only(
                    environment, FakeTransport({endpoint: response})))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(cd.ratchet_data_only(
                environment, FakeTransport(error=OSError("API unavailable"))))
        for before in ("", "0" * 40):
            transport = FakeTransport()
            self.assertFalse(cd.ratchet_data_only(
                {**environment, "RATCHET_BEFORE": before}, transport))
            self.assertEqual(transport.calls, [])


class TestStagingCallers(unittest.TestCase):
    """Execute all three decoded callers with their external boundaries recorded."""

    def _assert_skipped(self, root, result, output):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("changed=false", output)
        self.assertFalse((root / "ratchet-data/base.txt").exists())
        self.assertIn("::notice::", result.stdout)

    @staticmethod
    def _run(root, step, *, data_only=False, unchanged=False, delete_skip=False):
        scripts = root / "scripts/ci"
        scripts.mkdir(parents=True)
        shutil.copyfile(ROOT / "scripts/ci/changes_detect.py", scripts / "changes_detect.py")
        binary = root / "bin"
        binary.mkdir()
        boundary = binary / "gh"
        boundary.write_text("#!/bin/sh\nprintf '%s' \"$COMPARE_RESPONSE\"\n", encoding="utf-8")
        boundary.chmod(0o755)
        boundary = binary / "git"
        boundary.write_text("#!/bin/sh\n"
                            "test \"$*\" = 'diff --quiet HEAD -- .github/comment-share.json' "
                            "|| exit 97\nexit \"$DIFF_STATUS\"\n", encoding="utf-8")
        boundary.chmod(0o755)
        # The speed writer is covered through its real CLI in test_speed_ratchet.
        # Here its process boundary returns a candidate so deleting the skip
        # stages data, rather than merely failing on an incomplete fixture.
        boundary = binary / "python3"
        boundary.write_text(
            f"#!{sys.executable}\nimport os, pathlib, sys\n"
            "if sys.argv[1] == 'scripts/ci/changes_detect.py':\n"
            "    os.execv(sys.executable, [sys.executable] + sys.argv[1:])\n"
            "assert sys.argv[1] == 'scripts/ci/speed_ratchet.py', sys.argv\n"
            "target = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n"
            "if os.environ['DIFF_STATUS'] == '1':\n"
            "    target.write_text('{}\\n')\n", encoding="utf-8")
        boundary.chmod(0o755)
        (root / "gate").mkdir()
        (root / "gate/coverage-floor.json").write_text('{"floor": 80}')
        (root / "candidate-coverage-floor.json").write_text(
            json.dumps({"floor": 80 if unchanged else 81}))
        (root / ".github").mkdir()
        (root / ".github/comment-share.json").write_text('{"x.py": [1, 5]}\n')
        reports = root / "reports"
        reports.mkdir()
        (reports / "bench-head-1.xml").touch()
        output = root / "output"
        environment = dict(os.environ, PATH=f"{binary}{os.pathsep}{os.environ['PATH']}",
                           GITHUB_SHA="b" * 40, RATCHET_BEFORE="a" * 40,
                           GITHUB_REPOSITORY="owner/repo", RUNNER_TEMP=str(root),
                           GITHUB_OUTPUT=str(output), BASELINE="gate/speed-baseline.json",
                           GITHUB_STEP_SUMMARY=str(root / "summary"), REPORTS=str(reports),
                           DIFF_STATUS="0" if unchanged else "1",
                           COMPARE_RESPONSE=json.dumps({"status": "ahead", "files": [
                               {"filename": "gate/speed-baseline.json" if data_only
                                else "src/render.py"}]}))
        block = step["run"]
        if delete_skip:
            start = block.index("if python3 scripts/ci/changes_detect.py --ratchet-data-only;")
            end = block.index("\nfi", start) + len("\nfi")
            block = block[:start] + block[end:]
        result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", block],
                                cwd=root, env=environment, capture_output=True,
                                text=True, check=False, timeout=15)
        return result, output.read_text() if output.exists() else ""

    @REQUIRES_POSIX_SHELL
    @unittest.skipUnless(shutil.which("jq"), "coverage staging uses jq")
    def test_each_caller_stages_changes_skips_data_windows_and_ends_the_cascade(self):
        for filename, job_id, _ in PRODUCERS:
            steps = workflows()[filename]["jobs"][job_id]["steps"]
            step = next(s for s in steps if s.get("id") in {"stage", "tighten"})
            self.assertEqual(step["env"]["RATCHET_BEFORE"], "${{ github.event.before }}")
            self.assertEqual(step["env"]["GH_TOKEN"], "${{ github.token }}")
            for data_only, unchanged in ((False, False), (False, True), (True, False)):
                with self.subTest(workflow=filename, data_only=data_only, unchanged=unchanged):
                    with tempfile.TemporaryDirectory() as tmp:
                        result, output = self._run(Path(tmp), step,
                                                   data_only=data_only, unchanged=unchanged)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        changed = not data_only and not unchanged
                        self.assertIn(f"changed={str(changed).lower()}", output)
                        self.assertEqual((Path(tmp) / "ratchet-data/base.txt").exists(), changed)
                        if changed:
                            self.assertEqual((Path(tmp) / "ratchet-data/base.txt").read_text(),
                                             "b" * 40 + "\n")
                        if data_only:
                            self._assert_skipped(Path(tmp), result, output)
            with self.subTest(mutant=filename), tempfile.TemporaryDirectory() as tmp:
                result, output = self._run(Path(tmp), step, data_only=True, delete_skip=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("changed=true", output)
                self.assertTrue((Path(tmp) / "ratchet-data/base.txt").exists())
                with self.assertRaisesRegex(AssertionError, "changed=false"):
                    self._assert_skipped(Path(tmp), result, output)


if __name__ == "__main__":
    unittest.main()
