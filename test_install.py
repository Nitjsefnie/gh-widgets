"""Integration tests for install.sh transaction rollback.

All installs run against TemporaryDirectory destinations. The units test
redirects UNIT_DIR and supplies a fake systemctl, so it never touches the
host's service manager or system unit directory.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INSTALL = ROOT / "install.sh"
RENDERER_SOURCES = {
    "render.py": "render-gh-widgets.py",
    "render-impact.py": "render-impact.py",
    "impact_loc.py": "impact_loc.py",
    "render-responsiveness.py": "render-responsiveness.py",
    "ghwidgets_common.py": "ghwidgets_common.py",
    "ghwidgets_data.py": "ghwidgets_data.py",
}
UNIT_NAMES = (
    "gh-widgets.service",
    "gh-widgets.timer",
    "gh-widgets-resync.service",
    "gh-widgets-resync.timer",
)


def copy_installation(source_dir, with_units=False):
    """Copy the installer and its declared source files into a fixture."""
    source_dir.mkdir(parents=True)
    shutil.copy2(INSTALL, source_dir / "install.sh")
    for source_name in RENDERER_SOURCES:
        shutil.copy2(ROOT / source_name, source_dir / source_name)
    if with_units:
        units_dir = source_dir / "units"
        units_dir.mkdir()
        for unit_name in UNIT_NAMES:
            shutil.copy2(ROOT / "units" / unit_name, units_dir / unit_name)
    return source_dir / "install.sh"


def run_install(script, destination, *args, env=None):
    """Run an installer copy with its destination as the working directory."""
    return subprocess.run(
        [str(script), *args, str(destination)],
        cwd=destination,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def assert_no_transaction_files(test_case, destination):
    leftovers = [
        path.name for path in destination.iterdir()
        if path.name.endswith(".new") or path.name.endswith(".old")
    ]
    test_case.assertEqual(leftovers, [], "installer left transaction files")


def old_renderer_set(destination):
    """Seed and return a literal coherent previous renderer set."""
    previous = {}
    for installed_name in RENDERER_SOURCES.values():
        content = f"old renderer version: {installed_name}\n".encode("utf-8")
        (destination / installed_name).write_bytes(content)
        previous[installed_name] = content
    return previous


def read_files(directory, names):
    return {name: (directory / name).read_bytes() for name in names}


class TestRendererInstall(unittest.TestCase):
    """The six renderer files install or roll back as one verified set."""

    def setUp(self):
        # pylint: disable=consider-using-with
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ghwidgets-install-")
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.destination = self.root / "bin"
        self.destination.mkdir()

    def test_happy_path_installs_all_six_and_verifies_them(self):
        proc = run_install(INSTALL, self.destination)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        for installed_name in RENDERER_SOURCES.values():
            self.assertTrue((self.destination / installed_name).is_file())
        self.assertIn(
            "verified: all renderers start and the public data module imports",
            proc.stdout,
        )
        assert_no_transaction_files(self, self.destination)

    def test_failed_renderer_verification_restores_old_six_file_set(self):
        source_dir = self.root / "src"
        script = copy_installation(source_dir)
        broken_renderer = source_dir / "render-responsiveness.py"
        broken_renderer.write_text(
            broken_renderer.read_text(encoding="utf-8") + "\ndef broken(:\n",
            encoding="utf-8",
        )
        previous = old_renderer_set(self.destination)

        proc = run_install(script, self.destination)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(
            read_files(self.destination, previous),
            previous,
            "failed verification must restore the byte-identical old set",
        )
        self.assertEqual(
            sorted(path.name for path in self.destination.iterdir()),
            sorted(previous),
        )
        assert_no_transaction_files(self, self.destination)

    def test_failed_renderer_verification_removes_fresh_install_set(self):
        source_dir = self.root / "src"
        script = copy_installation(source_dir)
        broken_renderer = source_dir / "render-responsiveness.py"
        broken_renderer.write_text(
            broken_renderer.read_text(encoding="utf-8") + "\ndef broken(:\n",
            encoding="utf-8",
        )

        proc = run_install(script, self.destination)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(list(self.destination.iterdir()), [])


@unittest.skipIf(
    sys.platform == "win32",
    "the units fixture runs a POSIX install.sh and shell fake-systemctl script",
)
@unittest.skipUnless(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    "install.sh --units checks for root; run this isolated fake-systemctl test as root",
)
class TestUnitInstallRollback(unittest.TestCase):
    """A failed unit load restores scratch unit files and timer enablement."""

    # pylint: disable=too-many-locals
    def test_failed_unit_verification_restores_old_units_and_disables_timers(self):
        with tempfile.TemporaryDirectory(prefix="ghwidgets-units-") as temp:
            root = Path(temp)
            source_dir = root / "src"
            script = copy_installation(source_dir, with_units=True)
            unit_dir = root / "systemd"
            unit_dir.mkdir()
            renderer_dir = root / "bin"
            renderer_dir.mkdir()
            old_units = {}
            for name in UNIT_NAMES:
                content = f"old unit version: {name}\n".encode("utf-8")
                (unit_dir / name).write_bytes(content)
                old_units[name] = content
            old_renderers = old_renderer_set(renderer_dir)

            bad_unit = source_dir / "units" / "gh-widgets.timer"
            bad_unit.write_text(
                bad_unit.read_text(encoding="utf-8") + "\nINVALID_TEST_UNIT\n",
                encoding="utf-8",
            )

            script_text = script.read_text(encoding="utf-8")
            unit_dir_anchor = "UNIT_DIR=/etc/systemd/system"
            self.assertEqual(script_text.count(unit_dir_anchor), 1)
            self.assertNotIn("|", str(unit_dir))
            script_text = subprocess.run(
                ["sed", f"s|^{unit_dir_anchor}$|UNIT_DIR={unit_dir}|", str(script)],
                text=True,
                capture_output=True,
                check=True,
            ).stdout
            self.assertEqual(script_text.count(f"UNIT_DIR={unit_dir}"), 1)
            script.write_text(script_text, encoding="utf-8")

            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            systemctl = fake_bin / "systemctl"
            systemctl.write_text(
                "#!/bin/sh\n"
                'printf \'%s\\n\' "$*" >> "$SYSTEMCTL_LOG"\n'
                'case "$1" in\n'
                '  is-enabled) exit 1 ;;\n'
                '  daemon-reload) exit 0 ;;\n'
                '  enable) exit 0 ;;\n'
                '  disable) exit 0 ;;\n'
                '  cat)\n'
                '    [ -f "$UNIT_DIR/$2" ] || exit 1\n'
                '    if grep -q \'^INVALID_TEST_UNIT$\' "$UNIT_DIR/$2"; then exit 1; fi\n'
                '    cat "$UNIT_DIR/$2"\n'
                '    ;;\n'
                '  list-timers) printf \'TIMER\\n\'; exit 0 ;;\n'
                '  *) exit 2 ;;\n'
                'esac\n',
                encoding="utf-8",
            )
            systemctl.chmod(0o755)

            env = os.environ.copy()
            env.update({
                "PATH": f"{fake_bin}{os.pathsep}{env.get('PATH', '')}",
                "UNIT_DIR": str(unit_dir),
                "SYSTEMCTL_LOG": str(root / "systemctl.log"),
            })
            proc = run_install(script, renderer_dir, "--units", env=env)

            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertEqual(read_files(unit_dir, UNIT_NAMES), old_units)
            self.assertEqual(
                read_files(renderer_dir, old_renderers), old_renderers,
                "unit verification failure must roll back the renderer phase too",
            )
            assert_no_transaction_files(self, unit_dir)
            assert_no_transaction_files(self, renderer_dir)

            calls = (root / "systemctl.log").read_text(encoding="utf-8").splitlines()
            self.assertIn("disable --now gh-widgets.timer gh-widgets-resync.timer", calls)
            self.assertLess(
                calls.index("is-enabled gh-widgets.timer"),
                calls.index("enable --now gh-widgets.timer gh-widgets-resync.timer"),
                "enabled state must be captured before timers are enabled",
            )


if __name__ == "__main__":
    unittest.main()
