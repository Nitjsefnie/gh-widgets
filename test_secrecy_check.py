"""scripts/secrecy-check.sh must work from a linked worktree (issue 27).

The literals live in the main checkout (.secrecy-literals and .env are
gitignored, so a linked worktree never has them); the script resolved them
from `git rev-parse --show-toplevel`, which is the worktree itself, so every
push from a worktree died with "no literals available".

Each case builds its own throwaway repo: a main checkout with literals, a
linked worktree, and the repo's real secrecy-check.sh copied in, then runs
it exactly as the pre-push hook does -- from inside the worktree.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().with_name("scripts") / "secrecy-check.sh"

LITERAL = "glpat-abc123def456ghi789"


def git(*args, cwd):
    """Run git in *cwd*, raising on failure, output discarded."""
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_script(cwd):
    """Run the real secrecy-check.sh from *cwd* like the hook does."""
    return subprocess.run([str(SCRIPT)], cwd=cwd, text=True,
                          capture_output=True)


@unittest.skipIf(
    sys.platform == "win32",
    "the script is a shebang script, which CreateProcess cannot exec; the "
    "worktree path it guards does not exist on Windows CI anyway")
class TestWorktreeLiterals(unittest.TestCase):
    """A worktree run must find the main checkout's literals and still scan
    its OWN tree, not the main checkout's."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="ghw-secrecy-"))
        cls.main = cls.tmp / "main"
        cls.main.mkdir()
        git("init", "-q", "-b", "main", cwd=cls.main)
        git("config", "user.name", "T", cwd=cls.main)
        git("config", "user.email", "t@example.com", cwd=cls.main)
        (cls.main / "tracked.txt").write_text("clean\n", encoding="utf-8")
        git("add", "-A", cwd=cls.main)
        git("commit", "-qm", "init", cwd=cls.main)
        (cls.main / ".secrecy-literals").write_text(
            LITERAL + "\n", encoding="utf-8")
        cls.wt = cls.tmp / "wt"
        git("worktree", "add", "-q", str(cls.wt), cwd=cls.main)
        # The hook runs the calling checkout's own copy; give the worktree
        # the script under test exactly as a real worktree would have it.
        (cls.wt / "scripts").mkdir()
        shutil.copy2(SCRIPT, cls.wt / "scripts" / "secrecy-check.sh")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_worktree_finds_main_checkout_literals(self):
        """A worktree run passes: literals resolved from the main checkout."""
        proc = run_script(self.wt)
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
            proc = run_script(self.wt)
        finally:
            git("rm", "-q", "--cached", "leak.txt", cwd=self.wt)
            (self.wt / "leak.txt").unlink()
        self.assertEqual(proc.returncode, 1, "a literal in the worktree "
                         "tree must fail the check")
        self.assertIn("working tree", proc.stderr)

    def test_main_checkout_run_unchanged(self):
        """The main checkout's own run behaves as before."""
        proc = run_script(self.main)
        self.assertEqual(proc.returncode, 0,
                         f"main-checkout run failed: {proc.stderr}")
        self.assertIn("clean", proc.stdout)


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
            proc = run_script(repo)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no literals available", proc.stderr)


if __name__ == "__main__":
    unittest.main()
