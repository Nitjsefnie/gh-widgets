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
    """Seed and return a runnable previous renderer set."""
    previous = {}
    for source_name, installed_name in RENDERER_SOURCES.items():
        content = (ROOT / source_name).read_bytes()
        if not content.endswith(b"\n"):
            content += b"\n"
        content += b"# previous version\n"
        (destination / installed_name).write_bytes(content)
        previous[installed_name] = content
    return previous


def read_files(directory, names):
    return {name: (directory / name).read_bytes() for name in names}


def source_renderer_bytes(source_dir):
    """Return the source bytes keyed by each installed renderer name."""
    return {
        installed_name: (source_dir / source_name).read_bytes()
        for source_name, installed_name in RENDERER_SOURCES.items()
    }


@unittest.skipIf(
    sys.platform == "win32",
    "install.sh uses POSIX shell syntax and this class executes it directly",
)
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

    def test_happy_path_replaces_old_set_with_all_source_bytes(self):
        old_renderer_set(self.destination)

        proc = run_install(INSTALL, self.destination)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            read_files(self.destination, RENDERER_SOURCES.values()),
            source_renderer_bytes(ROOT),
        )
        self.assertEqual(
            sorted(path.name for path in self.destination.iterdir()),
            sorted(RENDERER_SOURCES.values()),
        )
        self.assertIn(
            "verified: all renderers start and the public data module imports",
            proc.stdout,
        )
        assert_no_transaction_files(self, self.destination)

    def test_happy_path_installs_source_bytes_into_empty_destination(self):
        proc = run_install(INSTALL, self.destination)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            read_files(self.destination, RENDERER_SOURCES.values()),
            source_renderer_bytes(ROOT),
        )
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
        self.assertEqual(
            {path.name: path.read_bytes() for path in self.destination.iterdir()},
            {},
            "failed fresh install must leave no files behind",
        )
        assert_no_transaction_files(self, self.destination)


@unittest.skipIf(
    sys.platform == "win32",
    "the units fixture runs a POSIX install.sh and shell fake-systemctl script",
)
@unittest.skipUnless(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    "install.sh --units checks for root; run this isolated fake-systemctl test as root",
)
class TestUnitInstallRollback(unittest.TestCase):
    """Unit installs verify scratch files and preserve timer enablement."""

    # pylint: disable=too-many-locals
    def make_fixture(self, root, timer_states, corrupt_unit=True, seed_units=True):
        """Make an isolated unit installation and a stateful systemctl fake."""
        source_dir = root / "src"
        script = copy_installation(source_dir, with_units=True)
        unit_dir = root / "systemd"
        unit_dir.mkdir()
        renderer_dir = root / "bin"
        renderer_dir.mkdir()
        old_units = {}
        if seed_units:
            for name in UNIT_NAMES:
                content = f"old unit version: {name}\n".encode("utf-8")
                (unit_dir / name).write_bytes(content)
                old_units[name] = content
        old_renderers = old_renderer_set(renderer_dir)

        if corrupt_unit:
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

        timer_state_dir = root / "timer-states"
        timer_state_dir.mkdir()
        for timer, state in timer_states.items():
            (timer_state_dir / timer).write_text(state, encoding="utf-8")

        fake_bin = root / "fake-bin"
        fake_bin.mkdir()
        systemctl = fake_bin / "systemctl"
        systemctl.write_text(
            "#!/bin/sh\n"
            'printf \'%s\\n\' "$*" >> "$SYSTEMCTL_LOG"\n'
            'case "$1" in\n'
            '  is-enabled)\n'
            '    state_file="$TIMER_STATE_DIR/$2"\n'
            '    if [ ! -f "$state_file" ]; then printf \'not-found\\n\'; exit 4; fi\n'
            '    state=$(cat "$state_file") || exit 2\n'
            '    case "$state" in\n'
            '      enabled) printf \'enabled\\n\'; exit 0 ;;\n'
            '      disabled) printf \'disabled\\n\'; exit 1 ;;\n'
            '      unknown-response) printf \'mystery-state\\n\'; exit 4 ;;\n'
            '      *) printf \'query failed\\n\' >&2; exit 2 ;;\n'
            '    esac\n'
            '    ;;\n'
            '  daemon-reload) exit 0 ;;\n'
            '  enable)\n'
            '    [ "$2" = --now ] || exit 2\n'
            '    shift 2\n'
            '    for timer do printf \'enabled\\n\' > "$TIMER_STATE_DIR/$timer"; done\n'
            '    exit 0\n'
            '    ;;\n'
            '  disable)\n'
            '    [ "$2" = --now ] || exit 2\n'
            '    shift 2\n'
            '    for timer do printf \'disabled\\n\' > "$TIMER_STATE_DIR/$timer"; done\n'
            '    exit 0\n'
            '    ;;\n'
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
            "TIMER_STATE_DIR": str(timer_state_dir),
            "SYSTEMCTL_LOG": str(root / "systemctl.log"),
        })
        return {
            "script": script,
            "source_dir": source_dir,
            "unit_dir": unit_dir,
            "renderer_dir": renderer_dir,
            "old_units": old_units,
            "old_renderers": old_renderers,
            "timer_state_dir": timer_state_dir,
            "env": env,
            "log": root / "systemctl.log",
        }

    def run_fixture(self, fixture):
        """Run the fixture installer and return its output and fake state."""
        proc = run_install(
            fixture["script"], fixture["renderer_dir"], "--units",
            env=fixture["env"],
        )
        return proc

    def timer_states(self, fixture):
        """Read timer state left by fake systemctl."""
        return {
            timer: (fixture["timer_state_dir"] / timer).read_text(
                encoding="utf-8")
            for timer in ("gh-widgets.timer", "gh-widgets-resync.timer")
        }

    def systemctl_calls(self, fixture):
        """Read fake systemctl calls, or return an empty log."""
        log = fixture["log"]
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    def assert_timer_snapshot_fails_closed(self, response):
        """Check an invalid is-enabled result aborts before unit deployment."""
        with tempfile.TemporaryDirectory(prefix="ghwidgets-units-query-error-") as temp:
            fixture = self.make_fixture(
                Path(temp),
                {"gh-widgets.timer": "enabled", "gh-widgets-resync.timer": response},
                corrupt_unit=False,
            )

            proc = self.run_fixture(fixture)

            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertEqual(read_files(fixture["unit_dir"], UNIT_NAMES), fixture["old_units"])
            self.assertEqual(
                read_files(fixture["renderer_dir"], fixture["old_renderers"]),
                fixture["old_renderers"],
            )
            self.assertEqual(
                self.timer_states(fixture),
                {"gh-widgets.timer": "enabled", "gh-widgets-resync.timer": response},
            )
            calls = self.systemctl_calls(fixture)
            self.assertIn("is-enabled gh-widgets.timer", calls)
            self.assertIn("is-enabled gh-widgets-resync.timer", calls)
            self.assertFalse(
                any(call.startswith("enable --now") for call in calls),
                "timer snapshot errors must abort before enabling either timer",
            )
            self.assertFalse(
                any(call.startswith("cat ") for call in calls),
                "timer snapshot errors must abort before unit verification",
            )
            assert_no_transaction_files(self, fixture["unit_dir"])
            assert_no_transaction_files(self, fixture["renderer_dir"])

    def test_failed_unit_verification_keeps_previously_enabled_timers_enabled(self):
        with tempfile.TemporaryDirectory(prefix="ghwidgets-units-enabled-") as temp:
            fixture = self.make_fixture(
                Path(temp),
                {"gh-widgets.timer": "enabled", "gh-widgets-resync.timer": "enabled"},
            )

            proc = self.run_fixture(fixture)

            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertEqual(read_files(fixture["unit_dir"], UNIT_NAMES), fixture["old_units"])
            self.assertEqual(
                read_files(fixture["renderer_dir"], fixture["old_renderers"]),
                fixture["old_renderers"],
            )
            self.assertEqual(
                self.timer_states(fixture),
                {"gh-widgets.timer": "enabled\n", "gh-widgets-resync.timer": "enabled\n"},
            )
            calls = self.systemctl_calls(fixture)
            self.assertIn("enable --now gh-widgets.timer gh-widgets-resync.timer", calls)
            self.assertFalse(any(call.startswith("disable ") for call in calls))
            assert_no_transaction_files(self, fixture["unit_dir"])
            assert_no_transaction_files(self, fixture["renderer_dir"])

    def test_failed_unit_verification_restores_mixed_timer_states(self):
        with tempfile.TemporaryDirectory(prefix="ghwidgets-units-mixed-") as temp:
            fixture = self.make_fixture(
                Path(temp),
                {"gh-widgets.timer": "disabled", "gh-widgets-resync.timer": "enabled"},
            )

            proc = self.run_fixture(fixture)

            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertEqual(read_files(fixture["unit_dir"], UNIT_NAMES), fixture["old_units"])
            self.assertEqual(
                read_files(fixture["renderer_dir"], fixture["old_renderers"]),
                fixture["old_renderers"],
            )
            self.assertEqual(
                self.timer_states(fixture),
                {"gh-widgets.timer": "disabled\n", "gh-widgets-resync.timer": "enabled\n"},
            )
            calls = self.systemctl_calls(fixture)
            self.assertIn("disable --now gh-widgets.timer", calls)
            self.assertNotIn("disable --now gh-widgets-resync.timer", calls)
            self.assertLess(
                calls.index("is-enabled gh-widgets.timer"),
                calls.index("enable --now gh-widgets.timer gh-widgets-resync.timer"),
            )
            assert_no_transaction_files(self, fixture["unit_dir"])
            assert_no_transaction_files(self, fixture["renderer_dir"])

    def test_timer_state_query_error_aborts_before_unit_commit_and_enable(self):
        self.assert_timer_snapshot_fails_closed("query-error")

    def test_unknown_timer_state_aborts_before_unit_commit_and_enable(self):
        self.assert_timer_snapshot_fails_closed("unknown-response")

    def test_fresh_unit_install_accepts_confirmed_missing_timers(self):
        with tempfile.TemporaryDirectory(prefix="ghwidgets-units-fresh-") as temp:
            fixture = self.make_fixture(
                Path(temp),
                {},
                corrupt_unit=False,
                seed_units=False,
            )
            self.assertEqual(list(fixture["unit_dir"].iterdir()), [])

            proc = self.run_fixture(fixture)

            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(
                read_files(fixture["renderer_dir"], RENDERER_SOURCES.values()),
                source_renderer_bytes(fixture["source_dir"]),
            )
            expected_units = read_files(
                fixture["source_dir"] / "units", UNIT_NAMES)
            self.assertEqual(read_files(fixture["unit_dir"], UNIT_NAMES), expected_units)
            self.assertEqual(
                self.timer_states(fixture),
                {"gh-widgets.timer": "enabled\n", "gh-widgets-resync.timer": "enabled\n"},
            )
            self.assertIn("verified: units loaded; timers enabled", proc.stdout)
            assert_no_transaction_files(self, fixture["unit_dir"])
            assert_no_transaction_files(self, fixture["renderer_dir"])


if __name__ == "__main__":
    unittest.main()
