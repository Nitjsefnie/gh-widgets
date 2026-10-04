"""release.yml's gates, run as the shell they ship as.

    python3 -m unittest discover -v

Both bugs these cover were invisible to a green run: the wait step
accepted a subset of the gates it was supposed to require, and the tag
guard never looked at the tag. The only way to hold either one is to
execute the `run:` block itself, against a stubbed `gh`.

Its own module rather than classes inside `test_ci_workflows.py`, which
owns the cross-workflow invariants; the split is mechanical — same cases,
same names, same assertions. Twenty-six is a lot of cases, and it is
that many because every guard branch needs an entry that only that
branch refuses: a table-driven merge would put two different assertions
in one case and say less.

Stdlib unittest, matching the rest of this repo's suite.
"""
# Issue #79's cases took this module past pylint's 1000-line ceiling. The
# checks they buy are the ones that need the `gh` stub below to exist, and
# the stub cannot be shared with another module without inventing the
# cross-module helper convention this file deliberately has none of. Split
# when the module next grows; do not split to satisfy a counter.
# pylint: disable=too-many-lines
import json
import re
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


# Deliberately duplicated from test_ci_workflows.py rather than imported.
# This repo has no shared-helper convention between its test modules, and
# adding one for two constants would be a larger change than the split.
REPO_ROOT = Path(__file__).resolve().parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


# The release job only ever runs on ubuntu runners in production, so the
# block-text assertions in TestReleaseWorkflowGates and
# TestReleaseWorkflowTagGuards carry the Windows half of the contract.
requires_bash = unittest.skipIf(
    sys.platform == "win32",
    "runs bash, which resolves to WSL bash.exe without an installed "
    "distribution on Windows; the release job only ever runs on ubuntu "
    "runners in production, and the block-text assertions in "
    "test_release_workflow.py — "
    "test_manifest_names_the_workflows_a_version_push_schedules, "
    "test_the_self_exclusion_names_the_job_instead_of_a_prefix, "
    "test_tag_guards_are_identical_in_both_steps, "
    "test_the_shipped_wait_budget_is_the_documented_one and "
    "test_no_dispatch_input_is_inlined_into_a_run_block — run on every OS",
)


class _ReleaseWorkflowFixture(unittest.TestCase):
    """The world both release-workflow classes run against: the constants,
    including the `gh` stub, the runner, and the fixture helpers.

    A TestCase so `addCleanup` and `fail` are really there — the helpers
    below use both, and a plain mixin would leave pyright unable to type
    `_execute` and pylint unable to see them. It carries no `test_` method,
    so unittest collects nothing from it.
    """

    REPO = "Nitjsefnie/gh-widgets"
    SHA = "9c1f2d3e4a5b6c7d8e9f0a1b2c3d4e5f60718293a"
    OTHER_SHA = "1111111111111111111111111111111111111111"
    TAG = "v9.9.9"
    MANIFEST = ["lint", "pyright", "speed", "pip-audit", "analyze",
                "unittest", "coverage-ratchet", "aggregate", "gitleaks"]

    # The whole manifest, matrix legs and all, as the check-runs API
    # reports it: "status<TAB>conclusion<TAB>name". Four jobs report their
    # bare name; `analyze` and `unittest` have matrix names, while `gitleaks`
    # reports its job name. The "name (" form is load-bearing for the matrix
    # cases here.
    ALL_GREEN = [
        ("completed", "success", "lint"),
        ("completed", "success", "pyright"),
        ("completed", "success", "speed"),
        ("completed", "success", "pip-audit"),
        ("completed", "success", "analyze (python)"),
        ("completed", "success", "unittest (ubuntu-latest, 3.10)"),
        ("completed", "success", "unittest (ubuntu-latest, 3.13)"),
        ("completed", "success", "unittest (macos-latest, 3.10)"),
        ("completed", "success", "unittest (macos-latest, 3.13)"),
        ("completed", "success", "unittest (windows-latest, 3.10)"),
        ("completed", "success", "unittest (windows-latest, 3.13)"),
        # One leg, because the job renames ITSELF to `coverage-ratchet`; the
        # manifest entry is written against that string, not the job key.
        ("completed", "success", "coverage-ratchet"),
        ("completed", "success", "aggregate"),
        ("completed", "success", "gitleaks"),
    ]

    # A stand-in for `gh` whose whole world is the files beside it in the
    # directory the step runs in, so one stub serves every case:
    #
    #   check-runs           JSON the workflow's own --jq program is run over
    #   polls-remaining      how many more polls still answer from check-runs
    #   check-runs-after     what to answer once that counter runs out
    #   tag-sha              the commit a release tag resolves to
    #   tag-wrong            on create, resolve the tag to this instead
    #   resolve-error        answer the commits read with this, and exit 1
    #   resolve-error-after  how many reads still answer normally first
    #   release-exists       its presence means a release is already published
    #   create.log           each `gh release create` invocation, verbatim
    #
    # Handing the --jq program to a real jq rather than pre-filtering the
    # fixture is deliberate: what the workflow ships has to decide what the
    # stub answers, not this file's guess at it.
    #
    # The stub FAILS on anything it does not model, flags included: an
    # unknown invocation falls through to a non-zero exit, the check-runs
    # read without --paginate is refused (a dropped --paginate would
    # silently truncate to one page of 30), and the commits read without
    # `--jq .sha` is refused.
    #
    # It also models the one behaviour #35 is about — `gh release create`
    # reuses a tag that already exists and never moves it to --target.
    #
    # The "no such tag" answer is a 422, not a 404, and the message is
    # the real one, read off the live API:
    #
    #   $ gh api "repos/Nitjsefnie/gh-widgets/commits/v99.99.99"
    #   gh: No commit found for SHA: v99.99.99 (HTTP 422)
    GH_STUB = r"""#!/bin/sh
jq_prog=""
paginate=no
prev=""
for arg in "$@"; do
  if [ "$prev" = "--jq" ]; then jq_prog="$arg"; fi
  if [ "$arg" = "--paginate" ]; then paginate=yes; fi
  prev="$arg"
done

if [ "$1" = "release" ]; then
  case "$2" in
    view)
      if [ -f release-exists ]; then exit 0; fi
      echo "release not found" >&2
      exit 1
      ;;
    create)
      echo "$*" >> create.log
      if [ ! -f tag-sha ]; then
        target=""
        while [ "$#" -gt 0 ]; do
          if [ "$1" = "--target" ]; then target="$2"; fi
          shift
        done
        if [ -f tag-wrong ]; then
          cat tag-wrong
        else
          printf '%s\n' "$target"
        fi > tag-sha
      fi
      exit 0
      ;;
  esac
  echo "unstubbed gh release $2" >&2
  exit 1
fi

if [ "$1" = "api" ]; then
  for arg in "$@"; do
    case "$arg" in
      */check-runs)
        if [ "$paginate" != yes ]; then
          echo "unstubbed gh check-runs read: --paginate is missing" >&2
          exit 1
        fi
        if [ -f polls-remaining ]; then
          left="$(cat polls-remaining)"
          if [ "$left" -gt 0 ]; then
            left=$(( left - 1 ))
            printf '%s\n' "$left" > polls-remaining
            if [ "$left" -eq 0 ] && [ -f check-runs-after ]; then
              exec jq -r "$jq_prog" < check-runs-after
            fi
          fi
        fi
        if [ -f check-runs ]; then
          exec jq -r "$jq_prog" < check-runs
        fi
        exit 0
        ;;
      repos/*/commits/*)
        if [ "$jq_prog" != ".sha" ]; then
          echo "unstubbed gh commits read: --jq is '$jq_prog', wanted .sha" >&2
          exit 1
        fi
        # "Could not ask" and "asked, and there is no such ref" are two
        # different answers, and this is the distinction the whole
        # fail-open finding was about — so the stub can produce both.
        if [ -f resolve-error ]; then
          left=0
          if [ -f resolve-error-after ]; then
            left="$(cat resolve-error-after)"
          fi
          if [ "$left" -gt 0 ]; then
            printf '%s\n' "$(( left - 1 ))" > resolve-error-after
          else
            cat resolve-error >&2
            exit 1
          fi
        fi
        if [ -f tag-sha ]; then cat tag-sha; exit 0; fi
        echo "gh: No commit found for SHA: not-a-real-ref (HTTP 422)" >&2
        exit 1
        ;;
    esac
  done
fi
echo "unstubbed gh invocation: $*" >&2
exit 1
"""

    def setUp(self):
        # Not a `with`: subTest cases re-enter setUp to get a clean stub
        # world, and a cleanup stack is the only thing that survives that.
        # pylint: disable=consider-using-with
        self.tmp = tempfile.TemporaryDirectory(prefix="ghw-release-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        stub = self.bin / "gh"
        stub.write_text(self.GH_STUB, encoding="utf-8")
        stub.chmod(0o755)

    @staticmethod
    def _job_block(text, job):
        """The lines of one `jobs:` entry, up to the next sibling job."""
        lines = text.splitlines()
        start = next(index for index, line in enumerate(lines)
                     if line.rstrip() == f"  {job}:")
        body = []
        for line in lines[start + 1:]:
            if line.startswith("  ") and not line.startswith("   "):
                break
            body.append(line)
        return body

    @classmethod
    def _own_check_name(cls):
        """The check-run name GitHub gives this workflow's own job.

        Read out of the workflow rather than written here, so a rename of the
        job follows the tests instead of silently leaving them asserting a
        name nothing reports. The check-runs API reports a job's own
        `name:` when it has one and its job id when it does not — verified
        live against this repository, where `measure` arrives as
        `coverage-ratchet`, `analyze` as `analyze (python)`, and the jobs with
        no override as themselves. There is no `workflow / job` form; the
        fixture that used to use one was wrong about the API, not the code
        under test.

        The job is found by the step that runs inside it rather than by being
        the only one, so a second job added here is a detail rather than
        thirty-odd unrelated failures. The two shapes that would break an
        exact match — a `name:` override and a matrix `strategy:` — are
        refused rather than passed over, because every caller that matches on
        this name needs it to be exact.
        """
        text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
        step = "      - name: Wait for the other gates on this commit"
        # Actions job ids: a letter or underscore, then letters, digits,
        # underscores and dashes.
        owners = [job for job in re.findall(
            r"(?m)^  ([A-Za-z_][A-Za-z0-9_-]*):$",
            text.split("\njobs:\n", 1)[-1])
            if step in "\n".join(cls._job_block(text, job))]
        if len(owners) != 1:
            raise AssertionError(
                f"{len(owners)} jobs in release.yml contain the wait step "
                f"({owners}); its own check run is not a single name")
        job = owners[0]
        job_block = "\n".join(cls._job_block(text, job))
        override = re.search(r"(?m)^ {4}name:\s*(\S.*)$", job_block)
        if override:
            raise AssertionError(
                f"release.yml's job {job} renames itself to "
                f"{override.group(1)!r}; ${{{{ github.job }}}} would no "
                "longer be the name its check runs report")
        if re.search(r"(?m)^ {4,6}strategy:", job_block):
            raise AssertionError(
                f"release.yml's job {job} became a matrix, so its check runs "
                f"report as {job!r} (…) rather than {job!r} and the exact "
                "match in the wait step needs the decorated form too")
        return job

    def _env(self, **overrides):
        env = dict(os.environ)
        for name in ("POLL_INTERVAL_SECONDS", "WAIT_DEADLINE_SECONDS",
                     "WAIVE", "GITHUB_STEP_SUMMARY", "SELF"):
            env.pop(name, None)
        env.update({
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "GH_TOKEN": "stub-token",
            "REPO": self.REPO,
            "SHA": self.SHA,
            "TAG": self.TAG,
            "WAIVE": "",
            "GITHUB_STEP_SUMMARY": str(self.root / "summary.md"),
            # What the workflow's `${{ github.job }}` renders to, read out
            # of the workflow rather than typed here — see where SELF is
            # pinned, in the gates class.
            "SELF": self._own_check_name(),
        })
        env.update(overrides)
        return env

    @classmethod
    def _run_block(cls, step_name, workflow=None):
        """The `run:` body of one release.yml step, with the workflow's own
        expressions substituted, since bash cannot parse `${{ }}`."""
        lines = ((workflow or (WORKFLOWS / "release.yml")).read_text(
            encoding="utf-8").splitlines())
        start = lines.index(f"      - name: {step_name}")
        run_line = next(index for index in range(start + 1, len(lines))
                        if lines[index].startswith("        run: |"))
        body = []
        for line in lines[run_line + 1:]:
            if line.startswith("      - "):
                break
            if line.startswith("          "):
                body.append(line[10:])
            elif not line.strip():
                body.append("")
            else:
                break
        block = "\n".join(body) + "\n"
        return block.replace("${{ github.repository }}", cls.REPO)

    def _execute(self, step_name, **overrides):
        # A wait step whose loop does not terminate is 45 minutes of CI
        # and, here, an apparently hung suite. Fail it loudly instead —
        # and suppress the chained TimeoutExpired, whose argv is the whole
        # workflow block and would bury the one line that says what went
        # wrong.
        try:
            return subprocess.run(
                ["bash", "-e", "-o", "pipefail", "-c",
                 self._run_block(step_name)],
                cwd=self.root, env=self._env(**overrides),
                capture_output=True, text=True, check=False, timeout=120)
        except subprocess.TimeoutExpired:
            raise AssertionError(
                f"the {step_name!r} block never exited") from None

    @staticmethod
    def _check_runs(runs):
        return json.dumps({"check_runs": [
            {"name": name, "status": status, "conclusion": conclusion}
            for status, conclusion, name in runs]})

    def _write_runs(self, runs, **extra):
        (self.root / "check-runs").write_text(
            self._check_runs(runs), encoding="utf-8")
        for name, value in extra.items():
            (self.root / name).write_text(value, encoding="utf-8")

    def _without(self, *names):
        return [run for run in self.ALL_GREEN if run[2] not in names]

    def _summary(self):
        return (self.root / "summary.md").read_text(encoding="utf-8")

    def _created(self):
        log = self.root / "create.log"
        return log.read_text(encoding="utf-8") if log.exists() else None

    # Long enough that a step which should never wait does not, short
    # enough that a step which should give up does.
    NO_WAIT = {"POLL_INTERVAL_SECONDS": "1", "WAIT_DEADLINE_SECONDS": "3"}
    GIVE_UP = {"POLL_INTERVAL_SECONDS": "1", "WAIT_DEADLINE_SECONDS": "1"}


class TestReleaseWorkflowGates(_ReleaseWorkflowFixture):
    """
Issue #34 — the wait step must not release on a subset of the gates.

Every case runs the shipped `run:` block against a stubbed `gh`; the
    ones that only read the workflow as text run on every OS.
    """
    # pylint: disable=too-many-public-methods

    # --- #34: the manifest wait -----------------------------------------

    @requires_bash
    @unittest.skipUnless(shutil.which("jq"),
                         "the gh stub runs the workflow's own --jq program, "
                         "which is how the workflow's own --jq program is "
                         "proven to be what reads these check runs")
    def test_every_manifest_gate_present_and_passing_releases(self):
        self._write_runs(self.ALL_GREEN)
        done = self._execute("Wait for the other gates on this commit")

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("expected gates: lint pyright speed pip-audit analyze "
                      "unittest", done.stdout)
        self.assertIn("required gates: lint pyright speed pip-audit analyze "
                      "unittest", done.stdout)

    @requires_bash
    @unittest.skipUnless(shutil.which("jq"), "see above")
    def test_a_decorated_check_run_name_still_matches_its_entry(self):
        # The manifest says `unittest`; the check runs say
        # `unittest (ubuntu-latest, 3.10)`. Matching has to bridge that,
        # and codeql renames its job to `analyze (python)` the same way.
        # A job that reports its bare id must match too.
        for entry, name in (("unittest", "unittest (ubuntu-latest, 3.10)"),
                            ("unittest", "unittest"),
                            ("analyze", "analyze (python)"),
                            ("analyze", "analyze")):
            with self.subTest(entry=entry, check_run=name):
                self.setUp()
                # Collapse every leg of this entry down to the one name
                # under test, so what differs is only that name.
                runs, replaced = [], False
                for run in self.ALL_GREEN:
                    if run[2].split(" ", maxsplit=1)[0] != entry:
                        runs.append(run)
                    elif not replaced:
                        runs.append(("completed", "success", name))
                        replaced = True
                self._write_runs(runs)
                done = self._execute(
                    "Wait for the other gates on this commit", **self.NO_WAIT)

                self.assertEqual(done.returncode, 0,
                                 done.stdout + done.stderr)

    @requires_bash
    @unittest.skipUnless(shutil.which("jq"), "see above")
    def test_a_gate_that_never_reported_stops_the_release(self):
        # The regression for #34: five green gates and one that has not
        # registered is not six green gates. The old poll saw only the
        # five and released.
        self._write_runs(self._without("speed"))
        done = self._execute("Wait for the other gates on this commit",
                             **self.GIVE_UP)

        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("speed", done.stdout + done.stderr)
        self.assertIn("never reported", done.stderr)

    @requires_bash
    @unittest.skipUnless(shutil.which("jq"), "see above")
    def test_a_still_running_gate_is_waited_for_until_it_passes(self):
        self._write_runs(
            [("in_progress", "", "unittest (ubuntu-latest, 3.10)")]
            + self._without("unittest (ubuntu-latest, 3.10)"),
            **{"polls-remaining": "2",
               "check-runs-after": self._check_runs(self.ALL_GREEN)})
        done = self._execute("Wait for the other gates on this commit",
                             **self.NO_WAIT)

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("still running", done.stdout)
        self.assertIn("unittest (ubuntu-latest, 3.10)", done.stdout)

    @requires_bash
    @unittest.skipUnless(shutil.which("jq"), "see above")
    def test_a_failed_or_cancelled_gate_stops_the_release(self):
        # Every one of these is a COMPLETED run with a conclusion outside
        # the acceptance set. An in-flight run has no conclusion at all
        # and must be waited for instead, which is its own case below.
        # A required gate and an incidental check are refused by different
        # code and say so differently, so both are driven.
        for conclusion in ("failure", "cancelled", "timed_out",
                           "action_required", "startup_failure"):
            for check_run, message in (
                    ("speed", "did not reach"),
                    ("some-other-gate", "did not pass")):
                with self.subTest(check_run=check_run,
                                  conclusion=conclusion):
                    self.setUp()
                    self._write_runs(
                        [("completed", conclusion, check_run)]
                        + self._without("speed"))
                    done = self._execute(
                        "Wait for the other gates on this commit")
                    out = done.stdout + done.stderr

                    self.assertEqual(done.returncode, 1, out)
                    self.assertIn(message, done.stderr)
                    self.assertIn(conclusion, done.stderr)

    @requires_bash
    @unittest.skipUnless(shutil.which("jq"), "see above")
    def test_a_required_gate_must_reach_success_and_an_incidental_check_may_not(self):
        # `skipped` on a required gate means the gate did not run, which is
        # issue #34's own hole one level down. The wider set is kept for
        # checks the manifest does not name, so nothing else changes.
        for check_run, conclusion, expected in (
                ("speed", "success", 0),
                ("speed", "skipped", 1),
                ("speed", "neutral", 1),
                ("other-gate", "skipped", 0),
                ("other-gate", "neutral", 0)):
            with self.subTest(check_run=check_run, conclusion=conclusion):
                self.setUp()
                base = (self._without("speed")
                        if check_run in self.MANIFEST else self.ALL_GREEN)
                self._write_runs([("completed", conclusion, check_run)]
                                 + base)
                done = self._execute(
                    "Wait for the other gates on this commit")

                self.assertEqual(done.returncode, expected,
                                 done.stdout + done.stderr)
                if expected == 0:
                    self.assertIn("Every expected gate reported", done.stdout)
                else:
                    self.assertIn("did not reach", done.stderr)

    @requires_bash
    @unittest.skipUnless(shutil.which("jq"), "see above")
    def test_a_waived_gate_is_not_judged_at_all(self):
        # "Removed from the required set" has to mean removed, or the
        # escape hatch is no escape hatch: waiving a broken gate would
        # still refuse. It also has to mean removed from the PENDING count,
        # or a waived gate that is slow or stuck holds the release for the
        # full deadline and then fails it — one screen under a log line
        # saying the opposite. A completed conclusion and a running one are
        # the same property reached by different code, so both are driven.
        for status, conclusion in (("completed", "failure"),
                                   ("completed", "skipped"),
                                   ("completed", "cancelled"),
                                   ("in_progress", ""),
                                   ("queued", ""),
                                   ("waiting", "")):
            with self.subTest(status=status, conclusion=conclusion):
                self.setUp()
                self._write_runs([(status, conclusion, "speed")]
                                 + self._without("speed"))
                done = self._execute(
                    "Wait for the other gates on this commit", WAIVE="speed")

                self.assertEqual(done.returncode, 0,
                                 done.stdout + done.stderr)
                self.assertNotIn("Timed out", done.stdout + done.stderr)
                self.assertIn("WAIVED: speed", done.stderr)

    @requires_bash
    @unittest.skipUnless(shutil.which("jq"), "see above")
    def test_a_red_matrix_leg_refuses_and_every_red_leg_is_named(self):
        # `unittest` is six legs and the manifest entry is one name, so
        # "every leg has to land" is a claim about six checks rather than
        # one. Judging only the first leg leaves the other cases green,
        # which is what the naming fixtures above would not catch: they
        # deliberately collapse an entry to a single name.
        #
        # Two red legs, not one, so a scan that reads only the first bad
        # leg is caught too — and it should be: an operator looking at a
        # six-leg matrix needs to know which of them to look at.
        red = ("unittest (ubuntu-latest, 3.10)", "unittest (windows-latest, 3.13)")
        runs = [("completed", "failure", name) for name in red] + [
            (status, conclusion, name)
            for status, conclusion, name in self.ALL_GREEN
            if name not in red]
        self._write_runs(runs)
        done = self._execute("Wait for the other gates on this commit")

        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        for name in red:
            self.assertIn(name, done.stderr)
        # A green leg is never named as a problem.
        self.assertNotIn("unittest (macos-latest, 3.10)", done.stderr)

    @requires_bash
    @unittest.skipUnless(shutil.which("jq"), "see above")
    def test_a_gate_that_has_not_started_is_waited_for_not_refused(self):
        # queued/waiting/pending/requested carry no conclusion at all.
        # Reading that empty conclusion as a failure would turn every
        # ordinary wait into a red release — which is precisely what
        # mutating either `$1 == "completed"` guard to
        # `$1 != "in_progress"` would do.
        #
        # Both a REQUIRED gate and an INCIDENTAL check are driven, because
        # after the success-only tightening they are judged by different
        # code and only the incidental one goes through the bad-scan.
        for status in ("queued", "waiting", "pending", "requested",
                       "waiting_for_deployment"):
            for queued, runs in (
                    ("speed", [(status, "", run[2]) if run[2] == "speed"
                               else run for run in self.ALL_GREEN]),
                    ("other-gate", [(status, "", "other-gate")]
                     + self.ALL_GREEN)):
                with self.subTest(status=status, check_run=queued):
                    self.setUp()
                    self._write_runs(runs)
                    done = self._execute(
                        "Wait for the other gates on this commit",
                        **self.GIVE_UP)
                    out = done.stdout + done.stderr

                    self.assertEqual(done.returncode, 1, out)
                    self.assertIn("Timed out", out)
                    self.assertNotIn("did not pass", out)
                    self.assertNotIn("did not reach", out)

    @requires_bash
    @unittest.skipUnless(shutil.which("jq"), "see above")
    def test_this_jobs_own_check_run_is_never_waited_on(self):
        # Excluded by the workflow's own exclusion, matched against the name
        # GitHub really reports for this job — which `_own_check_name` reads
        # out of the workflow rather than this file guessing it.
        own = self._own_check_name()
        self._write_runs([("in_progress", "", own)] + self.ALL_GREEN)
        done = self._execute("Wait for the other gates on this commit",
                             **self.NO_WAIT)

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertNotIn(own, done.stdout)

    @requires_bash
    @unittest.skipUnless(shutil.which("jq"), "see above")
    def test_a_check_sharing_this_jobs_name_prefix_is_not_excluded(self):
        # Issue #79, and the regression for it: the exclusion used to be
        # `startswith("release")`, so a gate named `release-notes-lint` left
        # the evidence entirely — the release waited on nothing, saw nothing
        # wrong, and shipped. Only this job's own check run may be excluded,
        # and it is matched exactly. Both directions are driven: a red one is
        # refused, and a running one is waited for rather than ignored.
        own = self._own_check_name()
        # The bare prefix, the decorated one, and the matrix form of the job's
        # OWN name — that last is the shape a wrong fix reaches for when it
        # tries to be future-proof, and it is the same dropped-gate bug one
        # delimiter down. This job is not a matrix and its name is exact.
        for other in (f"{own}-notes-lint", f"{own}-notes-lint (lint)",
                      f"{own} (waived)"):
            with self.subTest(check_run=other):
                self.setUp()
                self._write_runs([("completed", "failure", other)]
                                 + self.ALL_GREEN)
                done = self._execute("Wait for the other gates on this commit")

                self.assertEqual(done.returncode, 1,
                                 done.stdout + done.stderr)
                self.assertIn("did not pass", done.stderr)
                self.assertIn(other, done.stderr)

                self.setUp()
                self._write_runs([("in_progress", "", other)]
                                 + self.ALL_GREEN)
                done = self._execute("Wait for the other gates on this commit",
                                     **self.GIVE_UP)
                out = done.stdout + done.stderr

                self.assertEqual(done.returncode, 1, out)
                self.assertIn("still running", done.stdout)
                self.assertNotIn("did not pass", out)
                self.assertNotIn("did not reach", out)

    @requires_bash
    @unittest.skipUnless(shutil.which("jq"), "see above")
    def test_waive_removes_exactly_the_named_gate(self):
        cases = [
            # (waive input, gates still missing a check run, returncode)
            ("speed", (), 0),
            ("speed,unittest", (), 0),
            ("speed, unittest", (), 0),
            # A name that is not in the manifest waives nothing at all.
            ("speed, lint-docs", (), 0),
            # Waiving speed must not also waive lint.
            ("speed", ("lint",), 1),
            ("speed, lint-docs", ("lint",), 1),
            ("lint-docs", ("speed",), 1),
        ]
        for waive, missing, expected in cases:
            with self.subTest(waive=waive, missing=missing):
                self.setUp()
                self._write_runs(self._without("speed", *missing))
                done = self._execute(
                    "Wait for the other gates on this commit",
                    WAIVE=waive, **self.NO_WAIT)
                out = done.stdout + done.stderr

                self.assertEqual(done.returncode, expected, out)
                for name in missing:
                    self.assertIn(name, out)
                    self.assertNotIn(f"WAIVED: {name}", out)
                # The required set is the manifest minus exactly the names
                # in the input — a name outside it waives nothing.
                requested = [word.strip() for word in waive.split(",")]
                required_line = next(
                    line for line in done.stdout.splitlines()
                    if line.startswith("required gates: "))
                self.assertEqual(
                    required_line.removeprefix("required gates: ").split(),
                    [name for name in self.MANIFEST
                     if name not in requested])
                if not missing:
                    self.assertIn("Waived gates", self._summary())
                    for name in self.MANIFEST:
                        if name in requested:
                            self.assertIn(name, self._summary())
                    self.assertNotIn("lint-docs", self._summary())

    @requires_bash
    @unittest.skipUnless(shutil.which("jq"), "see above")
    def test_giving_up_names_the_gate_that_never_reported(self):
        self._write_runs(
            [("in_progress", "", "unittest (ubuntu-latest, 3.10)")]
            + self._without("speed", "unittest (ubuntu-latest, 3.10)"),
            **{"polls-remaining": "1"})
        done = self._execute("Wait for the other gates on this commit",
                             **self.GIVE_UP)

        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("Timed out", done.stderr)
        self.assertIn("speed", done.stderr)

    @requires_bash
    @unittest.skipUnless(shutil.which("jq"), "see above")
    def test_a_prefix_collision_does_not_satisfy_a_manifest_entry(self):
        # `lint` must not be satisfied by a `lint-docs` check, which is
        # the same bug as the missing gate one delimiter down.
        self._write_runs(
            [("completed", "success", "lint-docs")]
            + self._without("lint"))
        done = self._execute("Wait for the other gates on this commit",
                             **self.GIVE_UP)

        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("never reported a check run: lint", done.stderr)

    # --- text assertions, so they run on every OS ------------------------

    def test_the_repository_slug_is_the_real_remote(self):
        # Pinned as a literal, because nothing else in this suite can see a
        # remote: a nonexistent org in this string is completely inert (the
        # stub matches `repos/*/commits/*`), so drifting back to one would
        # be caught by nothing but a reader. `git remote -v` reports the
        # same slug, and it is the one string here someone would copy.
        self.assertEqual(self.REPO, "Nitjsefnie/gh-widgets")

    def test_waive_is_a_dispatch_input_a_push_cannot_set(self):
        text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
        self.assertRegex(
            text,
            r"workflow_dispatch:\n    inputs:\n(?:.*\n)*?      waive:\n"
            r"        description: [^\n]*\n        required: false\n"
            r"        type: string\n        default: ''\n")
        self.assertRegex(
            text,
            r"  push:\n    branches: \[main\]\n    paths:\n"
            r"      - 'VERSION'\n  workflow_dispatch:")
        self.assertIn("WAIVE: ${{ inputs.waive }}", text)

    def test_manifest_names_the_workflows_a_version_push_schedules(self):
        text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
        entries = re.findall(r"^ {12}([a-z][a-z-]*)\s+# (\S+\.yml), job "
                             r"`([a-z][a-z-]*)`(?: \([^)]*\))?$", text,
                             re.MULTILINE)
        self.assertEqual([name for name, _, _ in entries], self.MANIFEST)

        # The workflows deliberately NOT in the manifest: the ones a
        # VERSION push does not schedule, and this one, which cannot wait
        # on itself. Hoisted so the completeness check below and the
        # per-file checks agree on one literal rather than two copies.
        excluded = {"claim.yml", "pr-gate.yml", "audit.yml", "codeql.yml",
                    "targeted-blame-audit.yml", "gitfame-pool-probe.yml",
                    "gitfame-resync-memory.yml", "release.yml",
                    "scorecard.yml", "coverage-comment.yml"}

        for name, workflow_file, job in entries:
            with self.subTest(gate=name):
                text_wf = (WORKFLOWS / workflow_file).read_text(
                    encoding="utf-8")
                self.assertIn(f"\n  {job}:\n", text_wf,
                              f"{workflow_file} has no job {job}")
                # A job that renames itself reports its check runs under
                # the new name, so the manifest entry has to be a prefix of
                # that — otherwise the gate is never seen and the release
                # waits out the deadline.
                job_block = self._job_block(text_wf, job)
                # The job's OWN `name:` key, at four spaces — a step's
                # `- name:` and an artifact's `name:` are both deeper.
                override = next((match.group(1) for line in job_block
                                 if (match := re.match(r"^ {4}name:\s*(\S.*)$",
                                                       line))), None)
                if override:
                    # `${{ matrix.x }}` renders to a value, so the part
                    # before the first expression is what must match.
                    rendered = override.split("${{")[0].strip()
                    self.assertTrue(
                        rendered == name or rendered.startswith(f"{name} ("),
                        f"{workflow_file}: job {job} renames itself to "
                        f"{rendered!r}, which manifest entry {name!r} "
                        f"would never match")
                push = self._push_block(text_wf)
                self.assertRegex(text_wf, r"(?m)^  push:\s*$",
                                 f"{workflow_file} has no push trigger")
                # A paths-ignore list that does not mention VERSION is what
                # makes this workflow fire on a VERSION push. An allow-list
                # would need VERSION named before it could be a manifest
                # entry at all. An unfiltered push always fires, even when
                # its block is empty; either form must have no allow-list.
                self.assertNotIn("paths:", "\n".join(push))
                self.assertNotIn("VERSION", "\n".join(push))

        for workflow_file in excluded - {"release.yml"}:
            with self.subTest(excluded=workflow_file):
                path = WORKFLOWS / workflow_file
                self.assertTrue(path.is_file(), f"missing excluded {workflow_file}")
                text_wf = path.read_text(encoding="utf-8")
                self.assertEqual(self._push_block(text_wf), [],
                                 f"{workflow_file} gained a push trigger")
                self.assertEqual(self._pull_request_block(text_wf), [],
                                 f"{workflow_file} gained a pull_request trigger")
        # COMPLETENESS. Every workflow in the directory is either a manifest
        # entry or a named exclusion — nothing else. Without this, a new
        # push-triggered gate is invisible to the manifest AND to this
        # test, which is issue #34's own failure mode re-entering through
        # the fix's own control. A new workflow is not a new release gate
        # by default, but it must be a decision rather than an oversight.
        self.assertEqual(
            {path.name for path in WORKFLOWS.glob("*.yml")},
            {workflow_file for _, workflow_file, _ in entries} | excluded,
            "a workflow is neither a manifest entry nor a named exclusion; "
            "add it to release.yml's manifest or to `excluded` here, "
            "deliberately")

    def test_the_shipped_wait_budget_is_the_documented_one(self):
        # Every executing test overrides both, so nothing else pins what
        # production actually waits. A 5-minute deadline fails every real
        # release the moment `speed` is slow, and no local run would say so.
        block = self._run_block("Wait for the other gates on this commit")
        self.assertIn(": \"${POLL_INTERVAL_SECONDS:=20}\"", block)
        self.assertIn(": \"${WAIT_DEADLINE_SECONDS:=$(( 45 * 60 ))}\"", block)

    def test_no_dispatch_input_is_inlined_into_a_run_block(self):
        # `${{ inputs.waive }}` inside a run: is a script-injection vector:
        # the input is attacker-supplied on any dispatch, and the shell
        # would splice it into the script. Every input reaches the shell
        # through `env:` instead.
        for step in ("Refuse to re-release an existing tag",
                     "Wait for the other gates on this commit",
                     "Create the release"):
            with self.subTest(step=step):
                self.assertNotIn("inputs.", self._run_block(step))

    def test_the_self_exclusion_names_the_job_instead_of_a_prefix(self):
        # Issue #79's actual fix, as text: the excluded name is this job's
        # own, taken from the workflow context, so there is no second
        # literal anywhere to drift and no other gate can be dropped by
        # sharing a prefix with it.
        text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
        own = self._own_check_name()
        block = self._run_block("Wait for the other gates on this commit")

        self.assertIn("SELF: ${{ github.job }}", text)
        self.assertIn('-v self="$SELF"', block)
        self.assertNotIn(f'self="{own}"', block)
        # A second exclusion is the shape a drifted copy takes, and a
        # hard-coded literal left alongside the real one would satisfy every
        # other assertion here while pinning the name all over again.
        self.assertNotIn("select(", block)
        # `startswith("release")` was the bug, not the general shape of the
        # word, so it is spelled out rather than derived from `own`.
        self.assertNotIn("startswith", block)

    @staticmethod
    def _push_block(text):
        """The lines under `on: push:` — empty when there is no push."""
        lines = text.splitlines()
        start = next(index for index, line in enumerate(lines)
                     if line.startswith("on:"))
        body = []
        for line in lines[start + 1:]:
            if line.strip() and not line[0].isspace():
                break
            body.append(line)
        block, inside = [], False
        for line in body:
            if line.startswith("  ") and not line.startswith("   "):
                inside = line.rstrip() == "  push:"
                continue
            if inside and line.strip():
                block.append(line)
        return block

    @staticmethod
    def _pull_request_block(text):
        """The lines under `on: pull_request:` — empty when absent."""
        lines = text.splitlines()
        start = next(index for index, line in enumerate(lines)
                     if line.startswith("on:"))
        body = []
        for line in lines[start + 1:]:
            if line.strip() and not line[0].isspace():
                break
            body.append(line)
        block, inside = [], False
        for line in body:
            if line.startswith("  ") and not line.startswith("   "):
                inside = line.rstrip() == "  pull_request:"
                continue
            if inside and line.strip():
                block.append(line)
        return block


class TestReleaseWorkflowTagGuards(_ReleaseWorkflowFixture):
    """
Issue #35 — a tag that already exists must never be published over.

The guard step, the re-check immediately before the create, and the
read after it, each driven against a stubbed `gh`.
    """

    # --- #35: the tag guard ---------------------------------------------

    @requires_bash
    def test_a_free_tag_passes(self):
        done = self._execute("Refuse to re-release an existing tag")

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn(f"{self.TAG} is free", done.stdout)

    @requires_bash
    def test_an_existing_release_is_refused(self):
        (self.root / "release-exists").write_text("", encoding="utf-8")
        done = self._execute("Refuse to re-release an existing tag")

        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("already exists", done.stderr)

    @requires_bash
    def test_a_tag_pointing_elsewhere_is_refused(self):
        (self.root / "tag-sha").write_text(self.OTHER_SHA + "\n",
                                           encoding="utf-8")
        done = self._execute("Refuse to re-release an existing tag")

        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn(self.OTHER_SHA, done.stderr)
        self.assertIn(self.SHA, done.stderr)
        self.assertIn("tag-protection", done.stderr)

    @requires_bash
    def test_a_tag_already_at_this_sha_is_the_rerun_case(self):
        (self.root / "tag-sha").write_text(self.SHA + "\n", encoding="utf-8")
        done = self._execute("Refuse to re-release an existing tag")

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("rerun after a partial failure", done.stdout)

    @requires_bash
    def test_publish_refuses_a_tag_that_moved_while_it_waited(self):
        # The guard step passed an hour ago; the tag is elsewhere now.
        (self.root / "tag-sha").write_text(self.OTHER_SHA + "\n",
                                           encoding="utf-8")
        done = self._execute("Create the release")

        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn(self.OTHER_SHA, done.stderr)
        self.assertIsNone(self._created(), "the release was cut anyway")

    @requires_bash
    def test_publish_cuts_the_release_at_this_sha(self):
        done = self._execute("Create the release")

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self._created(),
                         f"release create {self.TAG} --target {self.SHA} "
                         f"--title {self.TAG} --generate-notes\n")
        self.assertEqual((self.root / "tag-sha").read_text(encoding="utf-8"),
                         self.SHA + "\n")
        self.assertIn("Released", done.stdout)

    @requires_bash
    def test_publish_fails_loudly_when_the_tag_resolved_elsewhere(self):
        # The race the post-create assertion exists to catch: gh ignored
        # --target because the tag was already there.
        (self.root / "tag-wrong").write_text(self.OTHER_SHA + "\n",
                                             encoding="utf-8")
        done = self._execute("Create the release")

        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn(self.OTHER_SHA, done.stderr)
        self.assertIn("wrong commit", done.stderr)

    @requires_bash
    def test_the_guard_refuses_when_it_cannot_ask_about_the_tag(self):
        # A stale tag IS on the remote, and the API is failing. Reading
        # that as "the tag is free" is issue #35 live on any day gh has a
        # bad one, so the guard has to refuse rather than guess.
        (self.root / "tag-sha").write_text(self.OTHER_SHA + "\n",
                                           encoding="utf-8")
        (self.root / "resolve-error").write_text(
            "gh: HTTP 500 (https://api.github.com/repos/x/y/commits/z)\n",
            encoding="utf-8")
        done = self._execute("Refuse to re-release an existing tag")

        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("Could not resolve", done.stderr)
        self.assertIn("500", done.stderr)
        self.assertNotIn("is free", done.stdout)

    @requires_bash
    def test_publish_refuses_when_the_tag_read_fails_outright(self):
        # "Could not ask" is not "asked, and it is wrong". The pre-create
        # read succeeds, the release is cut correctly, and only the
        # re-read fails — so this run has no evidence the release is bad
        # and must not tell anyone to delete it.
        self._write_runs([], **{"resolve-error": "gh: HTTP 500 (oops)\n",
                                "resolve-error-after": "1\n"})
        done = self._execute("Create the release")

        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("Could not re-resolve", done.stderr)
        self.assertNotIn("delete it by hand", done.stderr)
        self.assertNotIn("wrong commit", done.stderr)
        # ...and the release it just cut really is at this SHA.
        self.assertIsNotNone(self._created(), "the release was never cut")
        self.assertIn(f"--target {self.SHA}", self._created() or "")
        self.assertEqual((self.root / "tag-sha").read_text(encoding="utf-8"),
                         self.SHA + "\n")

    # --- text assertion, so it runs on every OS ---

    def test_tag_guards_are_identical_in_both_steps(self):
        # The publish step repeats the guard verbatim, and a drifted copy
        # of a guard is worse than no second copy. The two steps' refusal
        # MESSAGES differ on purpose — one is a stale version, the other a
        # race — so the comparison stops at the `if` they share.
        def guard(step_name):
            lines = self._run_block(step_name).splitlines()
            start = next(index for index, line in enumerate(lines)
                         if line.startswith('existing="$(gh api'))
            end = next(index for index, line in enumerate(lines)
                       if line.startswith('if [ -n "$existing" ]'))
            return lines[start:end + 1]

        self.assertEqual(guard("Refuse to re-release an existing tag"),
                         guard("Create the release"))
        self.assertIn('gh api "repos/$REPO/commits/$TAG" --jq .sha',
                      guard("Create the release")[0])


if __name__ == "__main__":
    unittest.main()
