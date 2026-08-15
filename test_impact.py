#!/usr/bin/env python3
"""Tests for render-impact.py's external dependency, git-fame.

    python3 -m unittest discover -v

render-impact.py shells out to `git fame` for its blame pass. Stock git-fame
blames one file per subprocess serially; the pinned
Nitjsefnie-OSC/git-fame build adds `--jobs`.

These tests pin the CAPABILITY, not the timing — a timing assertion would be
flaky on a shared box, and a slow run is not a wrong run. The invariant that
matters is that parallelism does not change the numbers.
"""
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent import futures
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


spec = importlib.util.spec_from_file_location(
    "render_impact", Path(__file__).with_name("render-impact.py"))
if spec is None or spec.loader is None:
    raise SystemExit("error: cannot load render-impact.py")
render_impact = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render_impact)


def git(*args, cwd):
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def fame(cwd, *extra):
    # Deliberately the `git fame` form here, not `git-fame`: this is the same
    # `git fame` dispatch path render-impact.py's blame_repo uses, plus `-s`
    # (--silent-progress) so the test run stays quiet. Only `--help` is
    # special-cased by git's dispatcher, which is why the capability probe
    # below uses the hyphenated binary.
    out = subprocess.run(["git", "fame", "-s", "-e", "-w", "--format", "json", *extra],
                         cwd=str(cwd), capture_output=True, text=True, check=True)
    return out.stdout


class TestGitFameParallel(unittest.TestCase):
    """The blame pass depends on a git-fame that supports --jobs."""

    @classmethod
    def setUpClass(cls):
        if shutil.which("git-fame") is None:
            raise unittest.SkipTest("git-fame is not installed")
        cls.tmp = Path(tempfile.mkdtemp(prefix="ghw-fame-"))
        git("init", "-q", "-b", "main", ".", cwd=cls.tmp)
        git("config", "user.email", "t@example.com", cwd=cls.tmp)
        git("config", "user.name", "T", cwd=cls.tmp)
        for i in range(12):
            (cls.tmp / f"f{i}.txt").write_text(f"line one {i}\nline two {i}\n")
        git("add", "-A", cwd=cls.tmp)
        git("commit", "-qm", "initial", cwd=cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_jobs_option_supported(self):
        """The pinned build must accept --jobs.

        Fails on stock git-fame from PyPI, which is the point: without the
        patch the blame pass is serial and render-impact runs take minutes.
        """
        # `git-fame`, NOT `git fame` — git rewrites `git <cmd> --help` into
        # `man git-<cmd>`, which reports no manual entry and would make this
        # assertion fail against a correctly patched build.
        help_text = subprocess.run(["git-fame", "--help"],
                                   capture_output=True, text=True, check=True).stdout
        self.assertIn("--jobs", help_text,
                      "installed git-fame lacks --jobs; expected the pinned "
                      "Nitjsefnie-OSC build (see CLAUDE.md)")

    def test_parallel_matches_serial(self):
        """Parallelism must not change the counts render-impact reads."""
        serial = json.loads(fame(self.tmp, "-j", "1"))
        parallel = json.loads(fame(self.tmp, "-j", "4"))
        self.assertEqual(serial, parallel)
        self.assertEqual(serial["total"]["loc"], 24)


class TestGitFameRuntimeCheck(unittest.TestCase):
    """The runtime check must be self-proving: loud on success, not silent."""

    @classmethod
    def setUpClass(cls):
        if shutil.which("git-fame") is None:
            raise unittest.SkipTest("git-fame is not installed")

    def test_check_is_noisy_on_success(self):
        """Success must print — a silent success cannot be distinguished from
        the check never having run, which is the whole point of it."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            ok = render_impact.check_git_fame()
        out = buf.getvalue()
        self.assertTrue(ok, f"check reported stock git-fame: {out!r}")
        self.assertIn("git-fame:", out)
        self.assertIn("--jobs", out)
        self.assertTrue(out.strip(), "check was silent on success")


class TestGitFameRuntimeCheckAlarms(unittest.TestCase):
    """The alarm branches must also be self-proving; a silent regression is
    the worst kind. These tests deliberately manipulate PATH so they run even
    on boxes where the real git-fame is absent or already patched."""

    def _check_with_path(self, path):
        """Run check_git_fame() with PATH replaced by *path*, capturing stdout."""
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"PATH": path}):
            with redirect_stdout(buf):
                ok = render_impact.check_git_fame()
        return ok, buf.getvalue()

    def test_stock_git_fame_warns_instead_of_lieing(self):
        """A git-fame without --jobs is detected and warned about."""
        with tempfile.TemporaryDirectory() as td:
            fake_bin = Path(td)
            script = fake_bin / "git-fame"
            script.write_text(
                "#!/bin/sh\n"
                "echo 'usage: git-fame [--format FORMAT] [--help]'\n",
                encoding="utf-8")
            script.chmod(0o755)
            path = f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"
            ok, out = self._check_with_path(path)
        self.assertFalse(ok, "stock git-fame must be rejected")
        self.assertIn("WARNING", out)

    def test_missing_git_fame_fails_loudly(self):
        """git-fame missing from PATH must not be treated as success."""
        with tempfile.TemporaryDirectory() as td:
            ok, out = self._check_with_path(td)
        self.assertFalse(ok, "missing git-fame must be rejected")
        self.assertIn("CHECK FAILED", out)


def fake_moved(n):
    """`moved` entries as update_loc builds them, for tests that never clone."""
    return [(f"o/r{i}", {"branch": "main", "head": f"h{i}"}) for i in range(n)]


class TestPrefetchedClones(unittest.TestCase):
    """The clone prefetch must not change WHAT is blamed, only when it starts.

    The failure contract is the delicate part: a repo whose clone fails still
    has to hand its error to the consumer AND leave the prefetch chain
    running, or one dead repo silently serialises every repo after it.
    """

    def test_yields_every_repo_in_order(self):
        seen = []
        with mock.patch.object(render_impact, "clone_repo",
                               side_effect=lambda *a: 0.5) as cl:
            for repo, _t, dest, clone_s, _wait, err in \
                    render_impact.prefetched_clones(fake_moved(5)):
                seen.append(repo)
                self.assertIsNone(err)
                self.assertEqual(clone_s, 0.5)
                shutil.rmtree(dest, ignore_errors=True)
        self.assertEqual(seen, [f"o/r{i}" for i in range(5)])
        self.assertEqual(cl.call_count, 5)

    def test_clone_runs_ahead_of_the_consumer(self):
        """By the time repo i is handed over, repo i+1's clone is under way.

        The generator SUBMITS the next clone before yielding, so the worker
        thread may not have entered clone_repo at the instant the consumer
        looks. Waiting for the count (rather than asserting it immediately)
        tests that the prefetch happens without depending on thread timing.
        """
        started = []
        lock = threading.Lock()

        def record(repo, *_a):
            with lock:
                started.append(repo)
            return 0.0

        def count():
            with lock:
                return len(started)

        with mock.patch.object(render_impact, "clone_repo", side_effect=record):
            for i, (_repo, _t, dest, _c, _w, _e) in enumerate(
                    render_impact.prefetched_clones(fake_moved(4), depth=1)):
                want = min(i + 2, 4)
                deadline = time.monotonic() + 5
                while count() < want and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(count(), want,
                                 f"prefetch did not run ahead at {i}")
                shutil.rmtree(dest, ignore_errors=True)

    def test_clone_failure_is_handed_over_not_raised(self):
        def flaky(repo, *_a):
            if repo == "o/r1":
                raise RuntimeError("clone_failed")
            return 0.1

        errs, seen = {}, []
        with mock.patch.object(render_impact, "clone_repo", side_effect=flaky):
            for repo, _t, dest, _c, _w, err in \
                    render_impact.prefetched_clones(fake_moved(4)):
                seen.append(repo)
                if err is not None:
                    errs[repo] = str(err)
                shutil.rmtree(dest, ignore_errors=True)
        self.assertEqual(seen, [f"o/r{i}" for i in range(4)],
                         "a failed clone must not stop the chain")
        self.assertEqual(list(errs), ["o/r1"])

    def test_abandoning_the_generator_leaves_no_directories(self):
        made = []

        def record(_repo, _branch, dest):
            made.append(Path(dest))
            return 0.0

        with mock.patch.object(render_impact, "clone_repo", side_effect=record):
            gen = render_impact.prefetched_clones(fake_moved(6))
            _repo, _t, dest, _c, _w, _e = next(gen)
            shutil.rmtree(dest, ignore_errors=True)
            gen.close()
        leftover = [p for p in made if p.exists()]
        self.assertEqual(leftover, [], f"prefetch leaked {leftover}")


class TestTargetedCounts(unittest.TestCase):
    """The fast path's failure mode is a silent ZERO, so its author matching
    gets regression tests rather than trust. Both bugs below were real: each
    made a repo we HAD contributed to report no lines at all."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="ghw-targeted-"))
        # a GitHub noreply address: the `+` is a regex quantifier, and the
        # capital letters matter once the address is lower-cased for matching
        cls.email = "75166987+MixedCase@users.noreply.github.com"
        git("init", "-q", "-b", "main", ".", cwd=cls.tmp)
        git("config", "user.name", "Other", cwd=cls.tmp)
        git("config", "user.email", "other@example.com", cwd=cls.tmp)
        (cls.tmp / "theirs.txt").write_text("a\nb\nc\n")
        git("add", "-A", cwd=cls.tmp)
        git("commit", "-qm", "theirs", cwd=cls.tmp)
        (cls.tmp / "ours.txt").write_text("x\ny\n")
        git("add", "-A", cwd=cls.tmp)
        git("-c", "user.name=Us", "-c", f"user.email={cls.email}",
            "commit", "-qm", "ours", cwd=cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_plus_in_address_is_not_a_regex(self):
        """`75166987+x@...` as a regex matches nothing -- and looks like zero."""
        found = render_impact.our_touched_files(self.tmp, {self.email.lower()})
        self.assertIn("ours.txt", found)
        self.assertNotIn("theirs.txt", found)

    def test_matching_is_case_insensitive(self):
        """Addresses are lower-cased for comparison; --author is not."""
        found = render_impact.our_touched_files(self.tmp, {self.email.lower()})
        self.assertTrue(found, "lower-cased address matched no commit")

    def test_counts_match_git_fame(self):
        if shutil.which("git-fame") is None:
            raise unittest.SkipTest("git-fame is not installed")
        emails = {self.email.lower()}
        ours, total = render_impact.targeted_counts(self.tmp, emails)
        reference = json.loads(fame(self.tmp))
        ref_total = reference["total"]["loc"]
        ref_ours = sum(r[1] for r in reference["data"]
                       if str(r[0]).strip().lower() in emails)
        self.assertEqual((ours, total), (ref_ours, ref_total))

    def test_blank_line_only_file_is_excluded(self):
        """git-fame picks text files with `git grep -I .`, so a file of only
        blank lines is not a text file to it. Counting its lines anyway was a
        real +5 on one repo and +1 on another."""
        blank = self.tmp / "blank.txt"
        blank.write_text("\n\n\n")
        git("add", "-A", cwd=self.tmp)
        git("commit", "-qm", "blank", cwd=self.tmp)
        try:
            _ours, total = render_impact.targeted_counts(self.tmp,
                                                         {self.email.lower()})
            self.assertEqual(total, 5, "blank-only file must not add lines")
        finally:
            git("rm", "-q", "-f", "blank.txt", cwd=self.tmp)
            git("commit", "-qm", "drop blank", cwd=self.tmp)

    def test_unrelated_address_counts_nothing(self):
        ours, total = render_impact.targeted_counts(self.tmp,
                                                    {"nobody@example.com"})
        self.assertEqual(ours, 0)
        self.assertEqual(total, 5)


class TestTargetedRenames(unittest.TestCase):
    """Our lines survive under paths our own history never mentions.

    This is what took `targeted` back out of production once: a rename made
    by somebody ELSE after our commit moved the lines, and selecting candidate
    files by the paths our commits name missed them entirely -- silently, as a
    smaller number.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ghw-rename-"))
        self.email = "75166987+MixedCase@users.noreply.github.com"
        git("init", "-q", "-b", "main", ".", cwd=self.tmp)
        git("config", "user.name", "Other", cwd=self.tmp)
        git("config", "user.email", "other@example.com", cwd=self.tmp)
        (self.tmp / "seed.txt").write_text("seed\n")
        git("add", "-A", cwd=self.tmp)
        git("commit", "-qm", "seed", cwd=self.tmp)
        (self.tmp / "ours.txt").write_text("our line 1\nour line 2\nour line 3\n")
        git("add", "-A", cwd=self.tmp)
        git("-c", "user.name=Us", "-c", f"user.email={self.email}",
            "commit", "-qm", "ours", cwd=self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ours(self):
        return render_impact.targeted_counts(self.tmp, {self.email.lower()})[0]

    def test_rename_by_another_author_is_followed(self):
        git("mv", "ours.txt", "moved.txt", cwd=self.tmp)
        git("commit", "-qm", "someone else moves it", cwd=self.tmp)
        self.assertEqual(self._ours(), 3, "lines lost through a rename")

    def test_rename_chain_is_followed(self):
        git("mv", "ours.txt", "one.txt", cwd=self.tmp)
        git("commit", "-qm", "move 1", cwd=self.tmp)
        git("mv", "one.txt", "two.txt", cwd=self.tmp)
        git("commit", "-qm", "move 2", cwd=self.tmp)
        self.assertEqual(self._ours(), 3, "lines lost through a rename CHAIN")

    def test_move_into_a_directory_keeping_the_basename(self):
        """The case `git log` recorded no rename edge for: same basename, new
        directory. Recovered by matching basenames of vanished paths."""
        (self.tmp / "sub").mkdir()
        git("mv", "ours.txt", "sub/ours.txt", cwd=self.tmp)
        git("commit", "-qm", "relocate", cwd=self.tmp)
        self.assertEqual(self._ours(), 3, "lines lost through a directory move")

    def test_rename_before_our_commit_does_not_break_a_later_one(self):
        """The scan is bounded to renames newer than our earliest commit.

        That is sound because a rename older than our commit is already
        reflected in the path our own history records — but only if a LATER
        rename is still followed, which is what this pins.
        """
        git("mv", "seed.txt", "seed-renamed.txt", cwd=self.tmp)
        git("commit", "-qm", "rename before we arrive", cwd=self.tmp)
        (self.tmp / "later.txt").write_text("a\nb\n")
        git("add", "-A", cwd=self.tmp)
        git("-c", "user.name=Us", "-c", f"user.email={self.email}",
            "commit", "-qm", "ours later", cwd=self.tmp)
        git("mv", "later.txt", "later-moved.txt", cwd=self.tmp)
        git("commit", "-qm", "rename after us", cwd=self.tmp)
        self.assertEqual(self._ours(), 5, "post-commit rename was not followed")

    def test_untouched_repo_still_counts_nothing(self):
        """The recovery paths must not invent lines for a repo we never touched."""
        ours, _total = render_impact.targeted_counts(self.tmp,
                                                     {"nobody@example.com"})
        self.assertEqual(ours, 0)


if __name__ == "__main__":
    unittest.main()


class TestCloneLookahead(unittest.TestCase):
    """Depth is a tuning knob, so its bounds are part of the contract."""

    def test_depth_caps_concurrent_clones(self):
        """At most `depth` clones RUN at once, however many are queued.

        The generator submits indices i..i+depth, but the pool has `depth`
        workers, so the surplus waits its turn. That cap is the contract worth
        pinning: it bounds the disk and the concurrent transfers.
        """
        gate = threading.Event()
        started, lock = [], threading.Lock()

        def blocking(repo, *_a):
            with lock:
                started.append(repo)
            gate.wait(10)
            return 0.0

        moved = fake_moved(8)
        with mock.patch.object(render_impact, "clone_repo", side_effect=blocking):
            gen = render_impact.prefetched_clones(moved, depth=3)
            # the generator submits before blocking, so the window is filled
            # even though nothing has completed yet
            fut = futures.ThreadPoolExecutor(max_workers=1).submit(next, gen)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with lock:
                    if len(started) >= 3:
                        break
                time.sleep(0.01)
            time.sleep(0.3)  # give a 4th a chance to start, if the cap leaks
            with lock:
                inflight = len(started)
            gate.set()
            shutil.rmtree(fut.result(timeout=10)[2], ignore_errors=True)
            gen.close()
        self.assertEqual(inflight, 3,
                         "depth=3 must run exactly 3 clones concurrently")

    def test_lookahead_is_at_least_one(self):
        for raw in ("0", "-5"):
            with mock.patch.dict(os.environ, {"CLONE_LOOKAHEAD": raw}):
                self.assertEqual(render_impact.clone_lookahead(), 1)

    def test_lookahead_rejects_garbage(self):
        with mock.patch.dict(os.environ, {"CLONE_LOOKAHEAD": "lots"}):
            with self.assertRaises(SystemExit):
                render_impact.clone_lookahead()


class TestImpactTimeDecay(unittest.TestCase):
    """Recent work keeps full credit; older accepted work fades gently."""

    NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = TestImpactTimeDecay.NOW
            return value if tz is not None else value.replace(tzinfo=None)

    @classmethod
    def stamp(cls, age_days):
        return (cls.NOW - timedelta(days=age_days)).isoformat().replace(
            "+00:00", "Z")

    @staticmethod
    def repository(name):
        owner = name.split("/", 1)[0]
        return {"nameWithOwner": name, "isPrivate": False,
                "owner": {"login": owner}}

    @classmethod
    def pull_request(cls, repo, age_days=None):
        stamp = None if age_days is None else cls.stamp(age_days)
        return {"merged": True, "mergedAt": stamp, "closedAt": stamp,
                "repository": cls.repository(repo)}

    @classmethod
    def issue(cls, repo, age_days):
        stamp = cls.stamp(age_days)
        return {"state": "CLOSED", "stateReason": "COMPLETED",
                "closedAt": stamp, "repository": cls.repository(repo)}

    def test_pr_decay_averages_merges_instead_of_refreshing_the_repo(self):
        """One fresh merge must not make an old merge fresh again."""
        repo = "outside/project"
        prs = [self.pull_request(repo, 30), self.pull_request(repo, 395)]
        totals = {repo: {"merged_prs": 4}}
        knobs = {"z": 0.0, "pr_gamma": 1.0}

        with mock.patch.object(render_impact, "datetime", self.FixedDateTime):
            rows = render_impact.pr_table(prs, totals, frozenset(), knobs)

        # Base score: (2/4) * 2 = 1.  The two merge weights are 1 and
        # 0.75, so their mean freshness is 0.875.
        self.assertAlmostEqual(rows[0][0], 0.875)

    def test_unknown_merge_date_fails_loudly(self):
        """A merged contribution without a timestamp must not get full credit."""
        repo = "outside/project"
        prs = [self.pull_request(repo, None), self.pull_request(repo, 395)]
        totals = {repo: {"merged_prs": 4}}
        knobs = {"z": 0.0, "pr_gamma": 1.0}

        with mock.patch.object(render_impact, "datetime", self.FixedDateTime):
            with self.assertRaisesRegex(ValueError, "impact timestamp"):
                render_impact.pr_table(prs, totals, frozenset(), knobs)

    def test_closed_date_backs_up_missing_merge_date(self):
        """An inferred merge must age from its known closure date."""
        repo = "outside/project"
        pr = self.pull_request(repo, None)
        pr["closedAt"] = self.stamp(395)
        totals = {repo: {"merged_prs": 2}}
        knobs = {"z": 0.0, "pr_gamma": 1.0}

        with mock.patch.object(render_impact, "datetime", self.FixedDateTime):
            rows = render_impact.pr_table([pr], totals, frozenset(), knobs)

        self.assertAlmostEqual(rows[0][0], 0.375)

    def test_issue_decay_uses_completion_date(self):
        """Issue impact begins aging from maintainer acceptance."""
        repo = "outside/project"
        issues = [self.issue(repo, 395)]
        totals = {repo: {"issues": 2}}
        knobs = {"z": 0.0, "issue_gamma": 1.0}

        with mock.patch.object(render_impact, "datetime", self.FixedDateTime):
            rows = render_impact.issue_table(
                issues, totals, frozenset(), knobs)

        # Base score 1/2, multiplied by the 0.75 bounded-decay weight.
        self.assertAlmostEqual(rows[0][0], 0.375)

    def test_decay_changes_ranking_instead_of_only_the_label(self):
        """A smaller fresh contribution should pass a larger stale one."""
        old = "outside/old"
        fresh = "outside/fresh"
        prs = ([self.pull_request(old, 395) for _ in range(2)]
               + [self.pull_request(fresh, 30) for _ in range(3)])
        totals = {old: {"merged_prs": 4}, fresh: {"merged_prs": 10}}
        knobs = {"z": 0.0, "pr_gamma": 1.0}

        with mock.patch.object(render_impact, "datetime", self.FixedDateTime):
            rows = render_impact.pr_table(prs, totals, frozenset(), knobs)

        # Undecayed: old=1.0 and fresh=0.9. Decayed: old=0.75.
        self.assertEqual(rows[0][-1], fresh)

    def test_display_anchor_does_not_hide_decay(self):
        """A stale section leader must be allowed to display below 10."""
        repo = "outside/project"
        prs = [self.pull_request(repo, 395)]
        totals = {repo: {"merged_prs": 2}}
        knobs = {"z": 0.0, "pr_gamma": 1.0}

        with mock.patch.object(render_impact, "datetime", self.FixedDateTime):
            rows = render_impact.pr_table(prs, totals, frozenset(), knobs)
        parts, _ = render_impact.render_section(
            render_impact.THEMES["tokyonight"], 88, "Pull Requests",
            rows, "#fff")

        self.assertIn('>7.50</text>', "".join(parts))

    def test_undecayed_anchor_survives_outside_the_top_five(self):
        """Dropping a stale raw leader must not move the 10-point scale."""
        anchor = "outside/stale-anchor"
        prs = [self.pull_request(anchor, 395) for _ in range(2)]
        totals = {anchor: {"merged_prs": 4}}
        for i in range(5):
            repo = f"outside/fresh-{i}"
            prs.extend(self.pull_request(repo, 30) for _ in range(3))
            totals[repo] = {"merged_prs": 10}
        knobs = {"z": 0.0, "pr_gamma": 1.0}

        with mock.patch.object(render_impact, "datetime", self.FixedDateTime):
            rows = render_impact.pr_table(prs, totals, frozenset(), knobs)
        parts, _ = render_impact.render_section(
            render_impact.THEMES["tokyonight"], 88, "Pull Requests",
            rows, "#fff")
        svg = "".join(parts)

        self.assertNotIn(anchor, svg)
        self.assertEqual(svg.count('>9.00</text>'), 5)
        self.assertEqual(svg.count('width="432.0" height="3"'), 5)


class TestImpactCacheShape(unittest.TestCase):
    """Cache maps reject structural field drift without rejecting empty maps."""

    @staticmethod
    def total_entry(**overrides):
        entry = {"issues": 4, "merged_prs": 2,
                 "branch": "main", "head": "abc123"}
        entry.update(overrides)
        return entry

    @staticmethod
    def loc_entry(**overrides):
        entry = {"ours": 2, "total": 10,
                 "branch": "main", "head": "abc123"}
        entry.update(overrides)
        return entry

    @classmethod
    def cache(cls, **overrides):
        payload = {
            "version": render_impact.CACHE_VERSION,
            "totals": {},
            "ourloc": {},
        }
        payload.update(overrides)
        return payload

    @staticmethod
    def load(payload):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "impact-cache.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return render_impact.load_cache(path)

    def test_populated_totals_missing_merged_prs_fails_loudly(self):
        payload = self.cache(
            totals={"outside/project": self.total_entry(
                merged_prs=None)})
        del payload["totals"]["outside/project"]["merged_prs"]

        with self.assertRaisesRegex(ValueError, r"totals.*merged_prs"):
            self.load(payload)

    def test_repository_absent_from_empty_totals_still_works(self):
        loaded = self.load(self.cache())

        rows = render_impact.pr_table(
            [], loaded["totals"], frozenset(),
            {"z": 2.58, "pr_gamma": 1.0})

        self.assertEqual(rows, [])

    def test_ourloc_entry_with_ours_missing_total_fails_loudly(self):
        payload = self.cache(
            ourloc={"outside/project": self.loc_entry()})
        del payload["ourloc"]["outside/project"]["total"]

        with self.assertRaisesRegex(ValueError, r"ourloc.*total"):
            self.load(payload)

    def test_ourloc_entry_missing_ours_keeps_filtering(self):
        payload = self.cache(
            ourloc={"outside/project": self.loc_entry(ours=None)})
        del payload["ourloc"]["outside/project"]["ours"]

        loaded = self.load(payload)
        rows = render_impact.loc_table(
            loaded["ourloc"], {"z": 2.58, "loc_gamma": 0.5})

        self.assertEqual(rows, [])

    def test_unparseable_timestamp_fails_loudly(self):
        repo = "outside/project"
        pr = TestImpactTimeDecay.pull_request(repo, 0)
        pr["mergedAt"] = "not-a-timestamp"

        with self.assertRaisesRegex(ValueError, "impact timestamp"):
            render_impact.pr_table(
                [pr], {repo: {"merged_prs": 2}}, frozenset(),
                {"z": 0.0, "pr_gamma": 1.0})
