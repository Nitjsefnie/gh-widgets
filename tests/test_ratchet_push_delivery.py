"""Execute sibling-artifact reuse, validation, installation and the git boundary."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from scripts.bench.bench_platform import REQUIRES_POSIX_SHELL
from tests import test_coverage_ratchet as coverage_tests
from tests import test_speed_ratchet as speed_tests

ROOT = Path(__file__).resolve().parents[1]
RECORDS = {"gate/coverage-floor.json", "gate/speed-baseline.json",
           ".github/comment-share.json"}
BASE, TIP = "a" * 40, "b" * 40


def push_body():
    document = yaml.safe_load((ROOT / ".github/workflows/ratchet-push.yml").read_text())
    return next(step["run"] for step in document["jobs"]["push"]["steps"]
                if step.get("name") == "Validate and push the ratchet record")


def boundaries(root):
    """Record external gh/git invocations and refuse every unmodeled operation."""
    binary = root / "bin"
    binary.mkdir()
    (binary / "gh").write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        "args = sys.argv[1:]\n"
        "expected = ['api', '-H', 'Cache-Control: no-cache', '--paginate', '--slurp',\n"
        " 'repos/owner/repo/compare/' + os.environ['base'] + '...' + os.environ['fetched'] + '?per_page=100']\n"
        "assert args == expected, args\n"
        "with open(os.environ['GH_CALLS'], 'a', encoding='utf-8') as handle:\n"
        " handle.write(json.dumps(args) + '\\n')\n"
        "if os.environ['API_FAIL'] == 'yes': sys.exit(31)\n"
        "print(os.environ['COMPARISON'])\n", encoding="utf-8")
    (binary / "git").write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        "args = sys.argv[1:]\n"
        "assert args[:2] == ['-C', os.environ['repo']], args\n"
        "args = args[2:]\n"
        "with open(os.environ['GIT_CALLS'], 'a', encoding='utf-8') as handle:\n"
        " handle.write(json.dumps(args) + '\\n')\n"
        "if args == ['diff', '--cached', '--quiet']: sys.exit(1)\n"
        "subject = {'.github/comment-share.json': 'chore(ratchet): tighten comment-share records (automated)',\n"
        " 'gate/coverage-floor.json': 'chore(ratchet): raise the coverage floor (automated)',\n"
        " 'gate/speed-baseline.json': 'chore(ratchet): tighten the speed baseline (automated)'}[os.environ['record_path']]\n"
        "allowed = [['add', '--', os.environ['record_path']],\n"
        " ['config', 'user.name', 'github-actions[bot]'],\n"
        " ['config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com'],\n"
        " ['commit', '-m', subject],\n"
        " ['push', 'origin', 'HEAD:refs/heads/main']]\n"
        "assert args in allowed, args\n", encoding="utf-8")
    for path in binary.iterdir():
        path.chmod(0o755)
    return binary


def records(record_path):
    old = json.loads((ROOT / record_path).read_text())
    new = json.loads(json.dumps(old))
    if record_path == "gate/coverage-floor.json":
        old.update(measured=84.6, floor=84.6)
        new.update(measured=86.2, floor=84.7, statements=1000, missing=138,
                   measured_commit=BASE)
    elif record_path == "gate/speed-baseline.json":
        entry = "counter::unit-suite"
        # The #240 reset leaves `entries` empty for the declared blind
        # window, so the recorded side gains the entry here rather than
        # reading it out of the committed document: what the validator
        # judges is a lowering of a recorded maximum, whatever the
        # committed document currently records.
        envelope = old["populations"]["unit-suite"]["entries"].get(entry)
        if envelope is None:
            envelope = {"min": 9.0, "max": 10.0, "n": 2}
            old["populations"]["unit-suite"].setdefault("slopes", {})[entry] \
                = 2.0
            old["populations"]["unit-suite"]["entries"][entry] = envelope
        envelope["max"] = envelope["min"] + 2
        new = json.loads(json.dumps(old))
        new["populations"]["unit-suite"]["entries"][entry]["max"] -= 1
    else:
        old, new = {"one.py": [1, 2]}, {"one.py": [1, 3]}
    return old, new


class TestRatchetPushDelivery(unittest.TestCase):
    @staticmethod
    def _prepare(root, record_path, documents, coverage):
        old, new = documents or records(record_path)
        (root / "data").mkdir()
        target = root / "main" / record_path
        target.parent.mkdir(parents=True)
        target.write_bytes(json.dumps(old).encode())
        name = Path(record_path).name
        (root / "data" / name).write_bytes(
            (json.dumps(new, sort_keys=True, indent=2) + "\n").encode())
        (root / "data/base.txt").write_bytes((BASE + "\n").encode())
        if coverage is not None:
            target.with_name("coverage-floor.json").write_bytes(coverage)
        return target

    def _drive(self, record_path, response, *, documents=None, body=None,
               api_fail=False, coverage=None):
        with tempfile.TemporaryDirectory(prefix="ratchet delivery ") as tmp:
            root = Path(tmp)
            target = self._prepare(root, record_path, documents, coverage)
            env = dict(os.environ, PATH=str(boundaries(root)) + os.pathsep + os.environ["PATH"],
                       base=BASE, fetched=TIP, record_path=record_path, document_name=Path(record_path).name,
                       data=str(root / "data"), repo=str(root / "main"), REPO="owner/repo",
                       RUN_ID="12345678", RUNNER_TEMP=str(root), GITHUB_STEP_SUMMARY=str(root / "summary"),
                       GH_CALLS=str(root / "gh-calls"), GIT_CALLS=str(root / "git-calls"),
                       COMPARISON=json.dumps(response), API_FAIL="yes" if api_fail else "no")
            run = body or push_body()
            run = run[run.index('if [ "$base" != "$fetched" ]; then'):]
            result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", run],
                                    cwd=root, env=env, capture_output=True, text=True,
                                    check=False, timeout=15)
            calls = [json.loads(line) for line in (root / "git-calls").read_text().splitlines()] \
                if (root / "git-calls").exists() else []
            summary = (root / "summary").read_text() if (root / "summary").exists() else ""
            return result, calls, target.read_bytes(), summary

    def _assert_delivery(self, result, calls, delivered):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(["push", "origin", "HEAD:refs/heads/main"] in calls, delivered)
        if delivered:
            self.assertIn("::notice::Applying", result.stdout)
        else:
            self.assertEqual(calls, [])
            self.assertIn("::notice::Dropped", result.stdout)

    @staticmethod
    def _comparison(paths):
        return [{"status": "ahead", "files": [{"filename": path, "status": "modified"}
                                              for path in paths]}]

    @REQUIRES_POSIX_SHELL
    def test_each_record_reuses_siblings_but_drops_own_and_source_changes(self):
        for own in RECORDS:
            for paths, delivered in ((RECORDS - {own}, True), ({own}, False),
                                     ({"src/render.py"}, False), (set(), True)):
                with self.subTest(record=own, changed=paths):
                    result, calls, written, summary = self._drive(own, self._comparison(paths))
                    self._assert_delivery(result, calls, delivered)
                    old, new = records(own)
                    self.assertEqual(json.loads(written), new if delivered else old)
                    if not delivered:
                        self.assertIn("Dropped ratchet artifact", summary)
                        self.assertIn("own record or non-data paths moved", result.stdout)

    @REQUIRES_POSIX_SHELL
    def test_uncertain_comparisons_and_source_renames_drop_green(self):
        cases = (None, [], [{"status": "diverged", "files": []}],
                 [{"status": "ahead"}], [{"status": "ahead", "files": [{}]}],
                 [{"status": "ahead", "files": [{"filename": "gate/coverage-floor.json",
                                                 "status": "renamed"}]}],
                 [{"status": "ahead", "files": [{"filename": "gate/coverage-floor.json",
                                                 "status": "renamed", "previous_filename": "src/old.py"}]}],
                 self._comparison(["gate/coverage-floor.json"] * 300))
        for response in cases:
            with self.subTest(response=response):
                result, calls, _written, summary = self._drive("gate/speed-baseline.json", response)
                self._assert_delivery(result, calls, False)
                self.assertIn("Dropped ratchet artifact", summary)
        result, calls, _written, _summary = self._drive(
            "gate/speed-baseline.json", None, api_fail=True)
        self._assert_delivery(result, calls, False)

    @REQUIRES_POSIX_SHELL
    def test_excluding_the_own_record_is_required_even_if_its_candidate_validates(self):
        response = self._comparison(["gate/speed-baseline.json"])
        result, calls, _written, _summary = self._drive("gate/speed-baseline.json", response)
        self._assert_delivery(result, calls, False)
        mutant = push_body().replace("allowed = records - {record_path}", "allowed = records")
        self.assertNotEqual(mutant, push_body())
        result, calls, _written, _summary = self._drive(
            "gate/speed-baseline.json", response, body=mutant)
        with self.assertRaisesRegex(AssertionError, "True != False"):
            self._assert_delivery(result, calls, False)

    @REQUIRES_POSIX_SHELL
    def test_real_writer_output_survives_an_intervening_coverage_raise_and_is_delivered(self):
        fixture = speed_tests.TestSpeedRatchetTightening()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        # The actual reports, comparator fold and writer produce the payload at S.
        # pylint: disable=protected-access
        fixture._write_reports()
        written = fixture._run()
        self.assertEqual(written.returncode, 0, written.stdout + written.stderr)
        candidate = json.loads(fixture.output_path.read_bytes())
        with tempfile.TemporaryDirectory() as tmp:
            coverage = Path(tmp) / "coverage-floor.json"
            document = json.loads((ROOT / "gate/coverage-floor.json").read_bytes())
            document.update(measured=84.6, floor=84.6)
            coverage.write_bytes(json.dumps(document).encode())
            measurement = coverage_tests.ratchet.Measurement(86.2, 1000, 138, document["files"])
            self.assertEqual(coverage_tests.ratchet.raise_floor(measurement, coverage, BASE), 0)
            self.assertEqual(json.loads(coverage.read_bytes())["floor"], 84.7)
            result, calls, installed, _summary = self._drive(
                "gate/speed-baseline.json", self._comparison(["gate/coverage-floor.json"]),
                documents=(fixture.baseline, candidate), coverage=coverage.read_bytes())
        self._assert_delivery(result, calls, True)
        self.assertEqual(json.loads(installed), candidate)
        self.assertEqual(candidate["populations"]["unit-suite"]["entries"]
                         ["counter::unit-suite"]["max"], 9.0)
        self.assertIn(["add", "--", "gate/speed-baseline.json"], calls)


if __name__ == "__main__":
    unittest.main()
