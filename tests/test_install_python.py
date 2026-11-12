"""install.sh --units checks each unit's interpreter for the pinned git-fame."""
import os
import sys
import unittest
from pathlib import Path

from tests import _ghw_temp, test_install

FAKE_PYTHON = """#!/bin/sh
printf '%s\\n' "$*" >> "$GH_WIDGETS_FAKE_LOG"
case "$2" in *ghwidgets_data*) [ -z "$GH_WIDGETS_FAKE_IMPORT_FAILS" ] || exit 1 ;; esac
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

    def install(self, help_text, status=0, **env):
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
        log = temp / "calls.log"
        fixture["env"].update({"GH_WIDGETS_FAKE_SCRIPTS": str(scripts),
                               "GH_WIDGETS_FAKE_LOG": str(log), "PYTHONPATH": str(stub), **env})
        proc = self.run_fixture(fixture)
        self.assertEqual(proc.returncode, status, proc.stdout + proc.stderr)
        if status:
            self.assertEqual(list(fixture["unit_dir"].iterdir()), [], "a failed install keeps no unit")
            return proc, python
        calls = log.read_text(encoding="utf-8").splitlines()
        for name in ("render-gh-widgets.py", "render-impact.py", "render-responsiveness.py",
                     "render-stars.py", "render-popularity.py"):
            self.assertEqual(calls.count(f"{fixture['renderer_dir'] / name} --help"), 2, calls)
        self.assertEqual(sum("import ghwidgets_data" in call for call in calls), 2, calls)
        self.assertIn("verified: gh-widgets-resync.service starts every renderer", proc.stdout)
        return proc, python

    def test_a_git_fame_with_jobs_raises_no_warning(self):
        proc, python = self.install("usage: git-fame [--jobs N]")
        self.assertNotIn(f"{python} has no git-fame", proc.stderr)

    def test_a_git_fame_without_jobs_names_the_install_command(self):
        proc, python = self.install("usage: git-fame [--format F]")
        self.assertIn(f"WARNING - {python} has no git-fame with --jobs", proc.stderr)
        self.assertIn(f"{python} -m pip install --force-reinstall", proc.stderr)

    def test_a_module_that_fails_to_import_on_the_unit_interpreter_rolls_back(self):
        proc, python = self.install("usage: git-fame [--jobs N]", status=1,
                                    GH_WIDGETS_FAKE_IMPORT_FAILS="1")
        self.assertIn(f"a shared module failed to import on gh-widgets.service's {python}", proc.stderr)
