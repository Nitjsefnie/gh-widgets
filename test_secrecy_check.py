"""scripts/secrecy-check.sh must work from a linked worktree (issue 27).

The literals live in the main checkout (.secrecy-literals and .env are
gitignored, so a linked worktree never checks them out); the script resolved
them from `git rev-parse --show-toplevel`, which is the worktree itself, so
every push from a worktree died with "no literals available".

The fixture commits the repo's LIVE secrecy-check.sh into a throwaway main
checkout, adds a linked worktree (which checks the script out into it), and
each case runs the copy belonging to the checkout it is in -- the same file
the pre-push hook would execute there. On Windows, where CreateProcess
cannot exec a shebang script, the helper runs the same file through `bash`
(git-bash ships with the hosted Windows runners); no case is skipped.
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().with_name("scripts") / "secrecy-check.sh"

LITERAL = "glpat-abc123def456ghi789"
AUTH_DB = "authdb123"
ENV_LINE = ("DATABASE_URL_AUTH="
            "postgres://user:pass@db.example.com:5432/authdb123?sslmode=require\n")


def git(*args, cwd):
    """Run git in *cwd*, raising on failure, output discarded."""
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_script(cwd, script):
    """Run *script* from *cwd* the way the pre-push hook would.

    Direct exec on POSIX; through `bash` on Windows, whose CreateProcess
    cannot exec a shebang script but whose runners ship git-bash.
    """
    argv = [str(script)] if sys.platform != "win32" else ["bash", str(script)]
    return subprocess.run(argv, cwd=cwd, text=True,
                          capture_output=True, check=False)


def commit_all(repo):
    """Stage and commit everything currently in *repo*."""
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "init", cwd=repo)


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


if __name__ == "__main__":
    unittest.main()
