"""install.sh --units checks each unit's interpreter for the pinned git-fame."""
import os
import sys
import unittest
from pathlib import Path

from tests import _ghw_temp, test_install

FAKE_PYTHON = """#!/bin/sh
case "$2" in *sysconfig*) printf '%s\\n' "$GH_WIDGETS_FAKE_SCRIPTS"; exit 0 ;; esac
exec {real} "$@"
"""


@unittest.skipIf(sys.platform == "win32", "install.sh is POSIX shell")
@unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0,
                     "install.sh --units checks for root")
class TestUnitInterpreterGitFame(unittest.TestCase):
    """The warning fires exactly when the interpreter lacks git-fame --jobs."""

    # pylint: disable=protected-access
    _cases = test_install.TestUnitInstallRollback
    make_fixture = _cases.make_fixture
    run_fixture = _cases.run_fixture

    def install(self, help_text):
        temp = Path(self.enterContext(_ghw_temp.tempdir(prefix="ghwidgets-fame-")))
        scripts, stub = temp / "scripts", temp / "stub"
        scripts.mkdir()
        stub.mkdir()
        (stub / "gitfame.py").write_text("", encoding="utf-8")
        fame = scripts / "git-fame"
        fame.write_text(f"#!/bin/sh\nprintf '%s\\n' '{help_text}'\n", encoding="utf-8")
        fame.chmod(0o755)
        python = temp / "python"
        python.write_text(FAKE_PYTHON.format(real=sys.executable), encoding="utf-8")
        python.chmod(0o755)
        fixture = self.make_fixture(temp / "root", {}, corrupt_unit=False,
                                    seed_units=False, python=str(python))
        fixture["env"].update({"GH_WIDGETS_FAKE_SCRIPTS": str(scripts),
                               "PYTHONPATH": str(stub)})
        proc = self.run_fixture(fixture)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("verified: gh-widgets-resync.service starts every renderer", proc.stdout)
        return proc, python

    def test_a_git_fame_with_jobs_raises_no_warning(self):
        proc, python = self.install("usage: git-fame [--jobs N]")
        self.assertNotIn(f"{python} has no git-fame", proc.stderr)

    def test_a_git_fame_without_jobs_names_the_install_command(self):
        proc, python = self.install("usage: git-fame [--format F]")
        self.assertIn(f"WARNING - {python} has no git-fame with --jobs", proc.stderr)
        self.assertIn(f"{python} -m pip install --force-reinstall", proc.stderr)
