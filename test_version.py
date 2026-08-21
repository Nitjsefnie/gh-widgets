"""The root VERSION file and the REPO_VERSION constant that reads it.

VERSION is the input to the release machinery: .github/workflows/release.yml
tags when it changes, and speed.yml benchmarks HEAD against the release it
names. Both read the file with `cat`, so its SHAPE matters as much as its
content — a stray second line or a leading `v` would produce a malformed tag.

Stdlib unittest, matching the rest of this repo's suite.
"""
import importlib.util
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
VERSION_PATH = REPO_ROOT / "VERSION"

# Bare semver, no leading `v`: release.yml prefixes the tag itself, so a
# `v` here would produce `vv0.1.0`.
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def load_common(path=REPO_ROOT / "ghwidgets_common.py"):
    """Load the shared module by explicit path.

    By path, not by name, for the same reason the renderers do it: the
    deploy renames files, so import-by-name would not survive.
    """
    spec = importlib.util.spec_from_file_location("ghwidgets_common_v", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class VersionFileTests(unittest.TestCase):
    def test_version_file_exists(self):
        self.assertTrue(VERSION_PATH.is_file(),
                        "VERSION is the release machinery's input")

    def test_version_file_is_one_bare_semver_line(self):
        lines = [ln for ln in
                 VERSION_PATH.read_text(encoding="utf-8").splitlines()
                 if ln.strip()]
        self.assertEqual(len(lines), 1,
                         f"VERSION must hold exactly one line, got {lines!r}")
        self.assertRegex(lines[0], SEMVER,
                         "VERSION must be bare semver with no leading 'v'")

    def test_version_file_ends_with_a_newline(self):
        # Harmless to $(cat), but it makes the file annoying to edit and
        # shows up as "\ No newline at end of file" in every release diff.
        self.assertTrue(VERSION_PATH.read_bytes().endswith(b"\n"))

    def test_git_can_see_it(self):
        # The deny-by-default .gitignore names files back one at a time. A
        # VERSION git cannot see would let release.yml tag a version the
        # repo never records.
        proc = subprocess.run(["git", "check-ignore", "-q", "VERSION"],
                              cwd=REPO_ROOT, check=False)
        self.assertNotEqual(proc.returncode, 0,
                            ".gitignore hides VERSION from git")


class RepoVersionConstantTests(unittest.TestCase):
    def test_matches_the_file(self):
        common = load_common()
        self.assertEqual(
            common.REPO_VERSION,
            VERSION_PATH.read_text(encoding="utf-8").strip(),
        )

    def test_is_distinct_from_common_version(self):
        """They answer different questions and must not be conflated.

        COMMON_VERSION is an interface-compatibility marker between this
        module and the renderers that load it; REPO_VERSION names a
        release. Bumping one must never be taken to mean the other moved.
        """
        common = load_common()
        self.assertIsInstance(common.COMMON_VERSION, int)
        self.assertIsInstance(common.REPO_VERSION, str)

    def test_missing_file_degrades_to_unknown(self):
        """The NORMAL case in a deploy, not an edge case.

        install.sh copies the renderers to /usr/local/bin/ without the repo
        around them, so a deployed module has no VERSION beside it.
        Reporting "unknown" is correct there; raising would break the
        deploy over a cosmetic field.
        """
        with tempfile.TemporaryDirectory() as tmp:
            copied = Path(tmp) / "ghwidgets_common.py"
            copied.write_text(
                (REPO_ROOT / "ghwidgets_common.py").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            common = load_common(copied)
            self.assertEqual(common.REPO_VERSION, "unknown")

    def test_blank_file_degrades_to_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            copied = Path(tmp) / "ghwidgets_common.py"
            copied.write_text(
                (REPO_ROOT / "ghwidgets_common.py").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (Path(tmp) / "VERSION").write_text("   \n", encoding="utf-8")
            common = load_common(copied)
            self.assertEqual(common.REPO_VERSION, "unknown")


if __name__ == "__main__":
    unittest.main()
