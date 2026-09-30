"""scripts/secrecy-check.sh must work from a linked worktree (issue 27).

The literals live in the main checkout (.secrecy-literals and .env are
gitignored, so a linked worktree never checks them out); the script resolved
them from `git rev-parse --show-toplevel`, which is the worktree itself, so
every push from a worktree died with "no literals available".

The fixture commits the repo's LIVE secrecy-check.sh into a throwaway main
checkout, adds a linked worktree (which checks the script out into it), and
the worktree cases run the copy belonging to the worktree -- the same file
the pre-push hook would execute there; the no-literals case runs the live
source script directly. All classes skip on Windows: the
fixture drives the POSIX hook path, which that runner cannot execute (same
policy as the git-fame shebang fixture).
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().with_name("scripts") / "secrecy-check.sh"

LITERAL = "glpat-abc123def456ghi789"
# LITERAL as the history fixture spells it on disk. Derived from the needle,
# so the two can never drift apart.
MIXED_CASE = LITERAL.upper()
AUTH_DB = "authdb123"
ENV_LINE = ("DATABASE_URL_AUTH="
            "postgres://user:pass@db.example.com:5432/authdb123?sslmode=require\n")


def git(*args, cwd):
    """Run git in *cwd*, raising on failure, output discarded."""
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def git_output(*args, cwd):
    """Run git in *cwd* and return its stdout, leaving the status to the
    caller -- for the queries whose answer IS the empty string."""
    return subprocess.run(["git", *args], cwd=cwd, text=True,
                          capture_output=True, check=False).stdout


def run_script(cwd, script, *args):
    """Run *script* from *cwd* the way the pre-push hook would: direct exec,
    like CreateProcess-free POSIX execution of the shebang script. *args* are
    passed through for the modes the script takes (e.g. --tree)."""
    return subprocess.run([str(script), *args], cwd=cwd, text=True,
                          capture_output=True, check=False)


def commit_all(repo):
    """Stage and commit everything currently in *repo*."""
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "init", cwd=repo)


@unittest.skipIf(
    sys.platform == "win32",
    "the fixture drives the POSIX pre-push path -- a shebang script plus "
    "sed -- which CreateProcess cannot exec and which the runner's bash "
    "invocations fail silently on; the script had no Windows coverage "
    "before this change either")
class TestWorktreeLiterals(unittest.TestCase):
    """A worktree run must find the main checkout's .secrecy-literals and
    still scan its OWN tree, not the main checkout's."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="ghw-secrecy-"))
        cls.main = cls.tmp / "main"
        cls.main.mkdir()
        git("init", "-q", "-b", "main", cwd=cls.main)
        git("config", "user.name", "T", cwd=cls.main)
        git("config", "user.email", "t@example.com", cwd=cls.main)
        (cls.main / "tracked.txt").write_text("clean\n", encoding="utf-8")
        (cls.main / "scripts").mkdir()
        shutil.copy2(SCRIPT, cls.main / "scripts" / "secrecy-check.sh")
        commit_all(cls.main)
        (cls.main / ".secrecy-literals").write_text(
            LITERAL + "\n", encoding="utf-8")
        cls.wt = cls.tmp / "wt"
        git("worktree", "add", "-q", str(cls.wt), cwd=cls.main)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_worktree_finds_main_checkout_literals(self):
        """A worktree run passes: literals resolved from the main checkout."""
        proc = run_script(self.wt, self.wt / "scripts" / "secrecy-check.sh")
        self.assertEqual(proc.returncode, 0,
                         f"worktree run failed: {proc.stderr}")
        self.assertIn("clean", proc.stdout)

    def test_worktree_tree_scan_still_covers_the_worktree(self):
        """The tree check scans the calling worktree, not the main checkout.

        git grep covers tracked files only, which is the right scope here:
        an untracked file is never pushed, and a committed one is caught by
        the history scan. Stage the leak the way a real near-miss would be.
        """
        (self.wt / "leak.txt").write_text(
            f"token = {LITERAL}\n", encoding="utf-8")
        git("add", "leak.txt", cwd=self.wt)
        try:
            proc = run_script(self.wt,
                              self.wt / "scripts" / "secrecy-check.sh")
        finally:
            git("rm", "-q", "--cached", "leak.txt", cwd=self.wt)
            (self.wt / "leak.txt").unlink()
        self.assertEqual(proc.returncode, 1, "a literal in the worktree "
                         "tree must fail the check")
        self.assertIn("working tree", proc.stderr)

    def test_main_checkout_run_unchanged(self):
        """The main checkout's own run behaves as before."""
        proc = run_script(self.main,
                          self.main / "scripts" / "secrecy-check.sh")
        self.assertEqual(proc.returncode, 0,
                         f"main-checkout run failed: {proc.stderr}")
        self.assertIn("clean", proc.stdout)


@unittest.skipIf(
    sys.platform == "win32",
    "the fixture drives the POSIX pre-push path -- a shebang script plus "
    "sed -- which CreateProcess cannot exec and which the runner's bash "
    "invocations fail silently on; the script had no Windows coverage "
    "before this change either")
class TestEnvOnlyLiterals(unittest.TestCase):
    """Same scenario with the literals coming from .env's DATABASE_URL_AUTH
    alone -- the other source the fix moved to the main checkout."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="ghw-secrecy-env-"))
        cls.main = cls.tmp / "main"
        cls.main.mkdir()
        git("init", "-q", "-b", "main", cwd=cls.main)
        git("config", "user.name", "T", cwd=cls.main)
        git("config", "user.email", "t@example.com", cwd=cls.main)
        (cls.main / "tracked.txt").write_text("clean\n", encoding="utf-8")
        (cls.main / "scripts").mkdir()
        shutil.copy2(SCRIPT, cls.main / "scripts" / "secrecy-check.sh")
        commit_all(cls.main)
        (cls.main / ".env").write_text(ENV_LINE, encoding="utf-8")
        cls.wt = cls.tmp / "wt"
        git("worktree", "add", "-q", str(cls.wt), cwd=cls.main)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_worktree_finds_env_auth_db(self):
        """A worktree run passes on .env literals from the main checkout."""
        proc = run_script(self.wt, self.wt / "scripts" / "secrecy-check.sh")
        self.assertEqual(proc.returncode, 0,
                         f"worktree run failed: {proc.stderr}")
        self.assertIn("clean", proc.stdout)

    def test_env_auth_db_leak_in_worktree_fails(self):
        """The extracted auth-DB name is a needle like any other."""
        (self.wt / "leak.txt").write_text(
            f"db = {AUTH_DB}\n", encoding="utf-8")
        git("add", "leak.txt", cwd=self.wt)
        try:
            proc = run_script(self.wt,
                              self.wt / "scripts" / "secrecy-check.sh")
        finally:
            git("rm", "-q", "--cached", "leak.txt", cwd=self.wt)
            (self.wt / "leak.txt").unlink()
        self.assertEqual(proc.returncode, 1, "the auth-DB name in the "
                         "worktree tree must fail the check")
        self.assertIn("working tree", proc.stderr)


@unittest.skipIf(
    sys.platform == "win32",
    "the fixture drives the POSIX pre-push path -- a shebang script plus "
    "sed -- which CreateProcess cannot exec and which the runner's bash "
    "invocations fail silently on; the script had no Windows coverage "
    "before this change either")
class TestNoLiteralsFails(unittest.TestCase):
    """No literals anywhere must keep failing loudly, never look clean."""

    def test_no_literals_errors(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "main"
            repo.mkdir()
            git("init", "-q", "-b", "main", cwd=repo)
            git("config", "user.name", "T", cwd=repo)
            git("config", "user.email", "t@example.com", cwd=repo)
            (repo / "tracked.txt").write_text("clean\n", encoding="utf-8")
            git("add", "-A", cwd=repo)
            git("commit", "-qm", "init", cwd=repo)
            proc = run_script(repo, SCRIPT)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no literals available", proc.stderr)


@unittest.skipIf(
    sys.platform == "win32",
    "the fixture drives the POSIX pre-push path -- a shebang script plus "
    "sed -- which CreateProcess cannot exec and which the runner's bash "
    "invocations fail silently on; the script had no Windows coverage "
    "before this change either")
class TestHistoryScanIsCaseInsensitive(unittest.TestCase):
    """The history pass must stay case-insensitive, and must be the arm
    that catches a mixed-case literal (issue 42).

    scripts/secrecy-check.sh's own comment calls --regexp-ignore-case
    load-bearing, and deleting that flag from the git log invocation left
    the whole suite green: nothing pinned the property, so the guard could
    lose case-insensitivity silently.

    The fixture is shaped so ONLY the history arm can fire, which is what
    gives the catch its credit: MIXED_CASE is committed and then removed,
    so the checked-out tree is clean and the tree pass's own -i has
    nothing to find; and LITERAL -- the lowercase spelling, the actual
    needle -- appears in no commit at all. A case-sensitive pickaxe finds
    nothing and the script exits clean; a case-insensitive one finds the
    two commits.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="ghw-secrecy-hist-"))
        cls.main = cls.tmp / "main"
        cls.main.mkdir()
        git("init", "-q", "-b", "main", cwd=cls.main)
        git("config", "user.name", "T", cwd=cls.main)
        git("config", "user.email", "t@example.com", cwd=cls.main)
        (cls.main / "tracked.txt").write_text("clean\n", encoding="utf-8")
        (cls.main / "scripts").mkdir()
        shutil.copy2(SCRIPT, cls.main / "scripts" / "secrecy-check.sh")
        commit_all(cls.main)
        # The leak, then its removal: the value stays in the earlier commit
        # and is gone from the tree the script scans.
        (cls.main / "leak.txt").write_text(
            f"token = {MIXED_CASE}\n", encoding="utf-8")
        commit_all(cls.main)
        git("rm", "-q", "leak.txt", cwd=cls.main)
        commit_all(cls.main)
        # Untracked, so it never enters a commit -- the needle itself must
        # stay out of the history.
        (cls.main / ".secrecy-literals").write_text(
            LITERAL + "\n", encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_history_scan_finds_the_mixed_case_spelling(self):
        """A mixed-case literal no longer in the tree must still fail, and
        the message must name the HISTORY arm -- asserting the exit code
        alone would let the tree pass satisfy it."""
        proc = run_script(self.main,
                          self.main / "scripts" / "secrecy-check.sh")
        self.assertEqual(proc.returncode, 1,
                         "a mixed-case literal in committed history must "
                         f"fail the check; stdout={proc.stdout!r} "
                         f"stderr={proc.stderr!r}")
        self.assertIn("is in committed history:", proc.stderr)
        self.assertNotIn("working tree", proc.stderr,
                         "the catch must come from the history arm alone")

    def test_fixture_tree_is_clean(self):
        """The premise of the catch: at run time the tree arm has nothing
        to find, so it cannot be what credits the test above."""
        proc = run_script(self.main,
                          self.main / "scripts" / "secrecy-check.sh", "--tree")
        self.assertEqual(proc.returncode, 0,
                         f"the fixture's tree must be clean: {proc.stderr}")

    def test_lowercase_needle_appears_in_no_commit(self):
        """The other premise: the needle's own spelling is nowhere in
        history, so case-insensitivity is the only thing that finds it."""
        found = git_output("log", "--all", "--oneline",
                           f"-S{LITERAL}", "--format=%H", cwd=self.main)
        self.assertEqual(found.strip(), "",
                         "the lowercase needle must not appear in any "
                         f"commit, or the case-sensitive pickaxe would "
                         f"find it too: {found.strip()}")


@unittest.skipIf(
    sys.platform == "win32",
    "the fixture drives the POSIX pre-push path -- a shebang script plus "
    "sed -- which CreateProcess cannot exec and which the runner's bash "
    "invocations fail silently on; the script had no Windows coverage "
    "before this change either")
class TestTreeScanIsCaseInsensitive(unittest.TestCase):
    """The tree pass's own -i must stay, and must be the arm that catches
    a mixed-case literal.

    The counterpart to the history pass's --regexp-ignore-case, and the same
    gap: no fixture put a mixed-case literal in the TREE, so deleting the -i
    at scripts/secrecy-check.sh:55 left the suite green. Every tree test
    here stages the needle's own lowercase spelling, which a case-sensitive
    git grep finds anyway -- so none of them exercised the flag.

    The fixture is shaped so ONLY the tree arm can fire: MIXED_CASE sits in
    a staged-but-uncommitted leak.txt, and no commit in this repository
    contains either spelling, so the history pass has nothing to report and
    the catch cannot be credited to it.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="ghw-secrecy-tree-"))
        cls.main = cls.tmp / "main"
        cls.main.mkdir()
        git("init", "-q", "-b", "main", cwd=cls.main)
        git("config", "user.name", "T", cwd=cls.main)
        git("config", "user.email", "t@example.com", cwd=cls.main)
        (cls.main / "tracked.txt").write_text("clean\n", encoding="utf-8")
        (cls.main / "scripts").mkdir()
        shutil.copy2(SCRIPT, cls.main / "scripts" / "secrecy-check.sh")
        # Nothing but the clean file and the script: no commit here holds
        # either spelling of the needle.
        commit_all(cls.main)
        (cls.main / ".secrecy-literals").write_text(
            LITERAL + "\n", encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_tree_scan_finds_the_mixed_case_spelling(self):
        """A mixed-case literal in the tree must fail, and the message must
        name the TREE arm -- asserting the exit code alone would let the
        history pass satisfy it."""
        (self.main / "leak.txt").write_text(
            f"token = {MIXED_CASE}\n", encoding="utf-8")
        git("add", "leak.txt", cwd=self.main)
        try:
            proc = run_script(self.main,
                              self.main / "scripts" / "secrecy-check.sh")
        finally:
            git("rm", "-q", "--cached", "leak.txt", cwd=self.main)
            (self.main / "leak.txt").unlink()
        self.assertEqual(proc.returncode, 1,
                         "a mixed-case literal in the tree must fail the "
                         f"check; stdout={proc.stdout!r} "
                         f"stderr={proc.stderr!r}")
        self.assertIn("is in the working tree:", proc.stderr)
        self.assertNotIn("committed history", proc.stderr,
                         "the catch must come from the tree arm alone")

    def test_fixture_history_is_clean(self):
        """The premise of the catch: the history pass has nothing to find,
        so it cannot be what credits the test above.

        Asked of git directly, on the same pickaxe shape the script runs,
        because --tree mode skips the history pass by design and a full-mode
        run can only be judged by the very stderr the catch already reads.
        """
        found = git_output("log", "--branches", "--tags", "--oneline",
                           "--regexp-ignore-case", f"-S{LITERAL}",
                           "--format=%H", cwd=self.main)
        self.assertEqual(found.strip(), "",
                         "no commit in this fixture may hold the needle, or "
                         "a case-sensitive history pickaxe would find it too "
                         f"and the catch would not be attributable: "
                         f"{found.strip()}")


if __name__ == "__main__":
    unittest.main()
