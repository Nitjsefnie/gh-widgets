"""Execute sibling-artifact reuse, validation, installation and the git boundary."""
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
from tests import test_coverage_ratchet as coverage_tests
from tests import test_speed_ratchet as speed_tests

ROOT = Path(__file__).resolve().parents[1]
RECORDS = {"gate/coverage-floor.json", "gate/speed-baseline.json",
           ".github/comment-share.json", ".github/size-baseline.json"}
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
        "if args[:1] == ['merge-file']:\n"
        " os.execv(os.environ['REAL_GIT'], ['git'] + args)\n"
        "assert args[:2] == ['-C', os.environ['repo']], args\n"
        "args = args[2:]\n"
        "if args == ['diff', '--cached', '--quiet']: sys.exit(1)\n"
        "if args[:1] == ['show']:\n"
        " if os.environ.get('SHOW_FAIL') == 'yes': sys.exit(1)\n"
        " sys.stdout.buffer.write(open(os.environ['BASE_RECORD'], 'rb').read())\n"
        " sys.exit(0)\n"
        "with open(os.environ['GIT_CALLS'], 'a', encoding='utf-8') as handle:\n"
        " handle.write(json.dumps(args) + '\\n')\n"
        "subject = {'.github/comment-share.json': 'chore(ratchet): tighten comment-share records (automated)',\n"
        " '.github/size-baseline.json': 'chore(ratchet): tighten size records (automated)',\n"
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
    if record_path == ".github/size-baseline.json":
        # Built from literals, not from the committed seed: the validator
        # must hold for any shape it accepts, and the seed lands after the
        # suite that judges the landing.
        return ({"schema_version": 1, "baseline": {"one.py": 42}},
                {"schema_version": 1, "baseline": {"one.py": 41}})
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
    elif record_path == ".github/size-baseline.json":
        # A count moves DOWN only: never-raised against fetched main is
        # what the validator judges, and a lowered candidate must stay
        # deliverable.
        old = {"schema_version": 1, "baseline": {"one.py": 42}}
        new = {"schema_version": 1, "baseline": {"one.py": 41}}
    else:
        old, new = {"one.py": [1, 2]}, {"one.py": [1, 3]}
    return old, new


def size_document(baseline):
    """The action writer's exact bytes for one baseline map."""
    return ('{\n  "schema_version": 1,\n  "baseline": '
            + json.dumps(baseline, indent=2, sort_keys=True)
            .replace("\n", "\n  ")
            + "\n}\n").encode("utf-8")


class TestRatchetPushDelivery(unittest.TestCase):
    @staticmethod
    def _prepare(root, record_path, documents, coverage, *, base_bytes=None,
                 main_bytes=None):
        old, new = documents or records(record_path)
        (root / "data").mkdir()
        target = root / "main" / record_path
        target.parent.mkdir(parents=True)
        if main_bytes is None:
            # All three merge inputs share one canonical shape, exactly as
            # the real writers produce them; divergent fixture formatting
            # would read as a whole-file rewrite on both sides.
            main_bytes = (size_document(old["baseline"])
                          if record_path == ".github/size-baseline.json"
                          else (json.dumps(old, sort_keys=True, indent=2)
                                + "\n").encode())
        if base_bytes is None:
            base_bytes = main_bytes
        target.write_bytes(main_bytes)
        name = Path(record_path).name
        if record_path == ".github/size-baseline.json":
            (root / "data" / name).write_bytes(size_document(new["baseline"]))
        else:
            (root / "data" / name).write_bytes(
                (json.dumps(new, sort_keys=True, indent=2) + "\n").encode())
        (root / "data/base.txt").write_bytes((BASE + "\n").encode())
        (root / "base-record").write_bytes(base_bytes)
        if coverage is not None:
            target.with_name("coverage-floor.json").write_bytes(coverage)
        return target

    def _drive(self, record_path, response, *, documents=None, body=None,
               api_fail=False, coverage=None, base_bytes=None,
               main_bytes=None, show_fail=False):
        with tempfile.TemporaryDirectory(prefix="ratchet delivery ") as tmp:
            root = Path(tmp)
            target = self._prepare(root, record_path, documents, coverage,
                                   base_bytes=base_bytes,
                                   main_bytes=main_bytes)
            env = dict(os.environ, PATH=str(boundaries(root)) + os.pathsep + os.environ["PATH"],
                       base=BASE, fetched=TIP, record_path=record_path, document_name=Path(record_path).name,
                       data=str(root / "data"), repo=str(root / "main"),
                       GH_WIDGETS_REPO="owner/repo",
                       GH_WIDGETS_RUN_ID="12345678", RUNNER_TEMP=str(root),
                       GITHUB_STEP_SUMMARY=str(root / "summary"),
                       GH_CALLS=str(root / "gh-calls"), GIT_CALLS=str(root / "git-calls"),
                       COMPARISON=json.dumps(response), API_FAIL="yes" if api_fail else "no",
                       BASE_RECORD=str(root / "base-record"),
                       REAL_GIT=shutil.which("git"),
                       SHOW_FAIL="yes" if show_fail else "no")
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
    def test_each_record_delivers_and_the_policy_scenarios_hold(self):
        """Per record, its delivered drive; the policy scenarios once.

        The policy arms are record-independent; a record's delivered drive
        is what differs. Own-record movement is pinned module-wide by
        test_every_record_is_data... with its subtraction mutant.
        """
        for own in RECORDS:
            paths = RECORDS - {own}
            with self.subTest(record=own, changed=paths):
                result, calls, written, summary = self._drive(
                    own, self._comparison(paths))
                self._assert_delivery(result, calls, True)
                _old, new = records(own)
                self.assertEqual(json.loads(written), new)
        for paths, delivered in (({"src/render.py"}, False), (set(), True)):
            with self.subTest(record="gate/speed-baseline.json",
                              changed=paths):
                result, calls, _written, summary = self._drive(
                    "gate/speed-baseline.json", self._comparison(paths))
                self._assert_delivery(result, calls, delivered)
                if not delivered:
                    self.assertIn("Dropped ratchet artifact", summary)
                    self.assertIn("non-data paths moved", result.stdout)

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
    def test_every_record_is_data_but_a_source_change_still_drops(self):
        """Every records file may move under a reusable artifact.

        The dropped exception the subtraction used to carve (an artifact's
        own record moving in the window) is gone: apply-time validation
        still judges the candidate against fetched main, so the own record
        moving is exactly as reusable as a sibling's. The mutant restores
        the old subtraction and must drop green again.
        """
        response = self._comparison([".github/size-baseline.json"])
        result, calls, _written, _summary = self._drive(
            ".github/size-baseline.json", response)
        self._assert_delivery(result, calls, True)
        # Asserting the old verdict against the correct body fails loudly.
        with self.assertRaisesRegex(AssertionError, "True != False"):
            self._assert_delivery(result, calls, False)
        mutant = push_body().replace(
            "blocked = changed - records",
            "blocked = changed - (records - {record_path})")
        self.assertNotEqual(mutant, push_body())
        result, calls, _written, _summary = self._drive(
            ".github/size-baseline.json", response, body=mutant)
        # The restored subtraction drops the same movement green.
        self._assert_delivery(result, calls, False)

    @REQUIRES_POSIX_SHELL
    def test_the_merge_preserves_main_edit_and_delivers_both(self):
        """Own-record movement plus a non-conflicting in-between edit.

        Main added an entry to the size record after the artifact was
        measured; the artifact lowered a different entry. The delivered
        document must carry both sides merged against the base copy —
        the whole-file copy the old apply used would lose main's entry,
        which is exactly the mutant's tell.
        """
        base_bytes = size_document({"a1.py": 300, "one.py": 42})
        main_bytes = size_document({"a0.py": 100, "a1.py": 300,
                                    "one.py": 42})
        # The two edits sit apart: main inserts a0.py above an entry the
        # artifact never touches, and the artifact lowers one.py below it —
        # same map on both sides, so neither side deletes what the other
        # needs. Adjacent hunks or a deleted entry would conflict, which is
        # the other branch's own drop case.
        merged_bytes = size_document({"a0.py": 100, "a1.py": 300,
                                      "one.py": 41})
        documents = (
            {"schema_version": 1,
             "baseline": {"a1.py": 300, "one.py": 42}},
            {"schema_version": 1,
             "baseline": {"a1.py": 300, "one.py": 41}},
        )
        result, calls, written, _summary = self._drive(
            ".github/size-baseline.json",
            self._comparison({".github/size-baseline.json"}),
            documents=documents,
            base_bytes=base_bytes, main_bytes=main_bytes)
        self._assert_delivery(result, calls, True)
        self.assertEqual(written, merged_bytes)
        self.assertIn("merged and preserved", result.stdout)

    @REQUIRES_POSIX_SHELL
    def test_a_conflicting_records_edit_drops_green(self):
        """Main and the artifact touched the same entry: drop, don't lose.

        The conflict notice is asserted by name: a mutant that delivers on
        conflict would otherwise fall through to the parse check (conflict
        markers do not parse) and drop with a different notice, which the
        verdict alone cannot distinguish.
        """
        base_bytes = size_document({"one.py": 42})
        main_bytes = size_document({"one.py": 50})
        result, calls, _written, summary = self._drive(
            ".github/size-baseline.json",
            self._comparison({".github/size-baseline.json"}),
            base_bytes=base_bytes, main_bytes=main_bytes)
        self._assert_delivery(result, calls, False)
        self.assertIn("the merge conflicts", result.stdout)
        self.assertIn("Dropped ratchet artifact", summary)
        self.assertIn("records merge conflicts", summary)

    @REQUIRES_POSIX_SHELL
    def test_an_unparseable_merged_record_drops_green(self):
        """A clean line-merge that still produces unparseable JSON drops.

        Main appended a broken line where the artifact did not touch, so
        the textual merge is clean but the result is not JSON; the parse
        check must drop the run green rather than installing it.
        """
        base_bytes = size_document({"one.py": 42})
        main_bytes = size_document({"one.py": 42}).replace(
            b'{\n', b'{\n  broken\n', 1)
        result, calls, _written, summary = self._drive(
            ".github/size-baseline.json",
            self._comparison({".github/size-baseline.json"}),
            base_bytes=base_bytes, main_bytes=main_bytes)
        self._assert_delivery(result, calls, False)
        self.assertIn("does not parse as JSON", result.stdout)

    @REQUIRES_POSIX_SHELL
    def test_an_unreachable_base_record_drops_green(self):
        """No base copy of the record: drop green, name the reason.

        The record first landed on main after the artifact's measured
        base, so there is nothing to merge against; installing the
        artifact whole would discard whatever main carries now.
        """
        result, calls, _written, summary = self._drive(
            ".github/size-baseline.json",
            self._comparison({".github/size-baseline.json"}),
            show_fail=True)
        self._assert_delivery(result, calls, False)
        self.assertIn("the measured base carries no copy", result.stdout)
        self.assertIn("no record to merge against", summary)

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
