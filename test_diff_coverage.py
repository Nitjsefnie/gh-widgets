#!/usr/bin/env python3
"""Tests for the informational patch-coverage report.

    python3 -m unittest test_diff_coverage

The suite is stdlib unittest and every test runs the real reporter offline.
"""
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
SCRIPT = REPO_ROOT / "scripts" / "ci" / "diff_coverage.py"
FIXTURES = REPO_ROOT / "fixtures" / "diff_coverage"


class TestDiffCoverageCli(unittest.TestCase):
    """The report counts added executable statements from XML and a diff."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="diff-coverage-"))
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.coverage = self.directory / "coverage.xml"
        self.diff = self.directory / "patch.diff"
        self.coverage.write_bytes((FIXTURES / "coverage.xml").read_bytes())
        self.diff.write_bytes((FIXTURES / "patch.diff").read_bytes())
        (self.directory / "alpha.py").write_text(
            "def alpha():\n    return 1\nvalue = alpha()\n",
            encoding="utf-8")
        (self.directory / "beta.py").write_text(
            "first = 1\nsecond = 2\n", encoding="utf-8")

    def _run(self, coverage=None, diff=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--coverage",
             str(coverage or self.coverage), "--diff", str(diff or self.diff)],
            cwd=self.directory, capture_output=True, text=True, check=False,
            timeout=30)

    def test_report_counts_covered_and_missed_lines_per_file(self):
        result = self._run()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("**40.0%** of added lines covered (2/5).",
                      result.stdout)
        self.assertIn("| `alpha.py` | 2 | 3 | 2 |", result.stdout)
        self.assertIn("| `beta.py` | 0 | 2 | 1-2 |", result.stdout)

    def test_cli_has_no_javascript_coverage_input(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"], cwd=self.directory,
            capture_output=True, text=True, check=False, timeout=30)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--coverage", result.stdout)
        self.assertIn("--diff", result.stdout)
        self.assertNotIn("--js-coverage", result.stdout)

    def test_changes_to_omitted_test_modules_are_not_called_unmeasured_source(self):
        diff = self.directory / "test-module.diff"
        diff.write_text(
            "diff --git a/test_new.py b/test_new.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n+++ b/test_new.py\n"
            "@@ -0,0 +1 @@\n+test_value = 1\n",
            encoding="utf-8")

        result = self._run(diff=diff)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Coverage omits `test_*.py`", result.stdout)
        self.assertNotIn("Unmeasured changed source files", result.stdout)

    def test_report_refuses_an_added_statement_missing_from_xml(self):
        root = ET.parse(self.coverage).getroot()
        beta = root.find(".//class[@filename='beta.py']/lines")
        assert beta is not None
        missing = beta.find("line[@number='2']")
        assert missing is not None
        beta.remove(missing)
        incomplete = self.directory / "incomplete.xml"
        ET.ElementTree(root).write(incomplete, encoding="unicode")

        result = self._run(coverage=incomplete)

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn(
            "missing executable statement records for beta.py: 2",
            result.stderr)

    def test_report_refuses_a_binary_diff_record(self):
        binary = self.directory / "binary.diff"
        binary.write_text(
            "diff --git a/alpha.py b/alpha.py\n"
            "Binary files a/alpha.py and b/alpha.py differ\n",
            encoding="utf-8")

        result = self._run(diff=binary)

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("binary diff record is not measurable", result.stderr)

    def test_report_refuses_a_well_formed_xml_document_with_the_wrong_root(self):
        invalid = self.directory / "not-cobertura.xml"
        invalid.write_text(
            "<html><class filename='alpha.py'><line number='1' hits='1'/>"
            "</class></html>", encoding="utf-8")

        result = self._run(coverage=invalid)

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("root is not <coverage>", result.stderr)

    def test_removed_line_that_looks_like_a_header_does_not_hide_later_hunks(self):
        source = self.directory / "dashes.py"
        source.write_text(
            "a = 1\nb = 2\nc = 3\nd = 4\ne = 5\n", encoding="utf-8")
        coverage = self.directory / "dashes.xml"
        coverage.write_text(
            "<coverage><class filename='dashes.py'><lines>"
            "<line number='4' hits='0'/><line number='5' hits='0'/>"
            "</lines></class></coverage>", encoding="utf-8")
        diff = self.directory / "dashes.diff"
        diff.write_text(
            "diff --git a/dashes.py b/dashes.py\n"
            "--- a/dashes.py\n+++ b/dashes.py\n"
            "@@ -1,4 +1,5 @@\n a = 1\n--- legacy note\n"
            " b = 2\n c = 3\n+d = 4\n+e = 5\n",
            encoding="utf-8")

        result = self._run(coverage=coverage, diff=diff)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("| `dashes.py` | 0 | 2 | 4-5 |", result.stdout)

    def test_bare_crlf_context_line_keeps_added_line_numbers(self):
        (self.directory / "crlf.py").write_text(
            "first = 1\n\nsecond = 2\n", encoding="utf-8")
        coverage = self.directory / "crlf.xml"
        coverage.write_text(
            "<coverage><class filename='crlf.py'><lines>"
            "<line number='1' hits='1'/><line number='3' hits='0'/>"
            "</lines></class></coverage>", encoding="utf-8")
        diff = self.directory / "crlf.diff"
        diff.write_bytes(
            b"diff --git a/crlf.py b/crlf.py\n"
            b"--- a/crlf.py\n+++ b/crlf.py\n"
            b"@@ -1,2 +1,3 @@\n first = 1\n\r\n+second = 2\n")

        result = self._run(coverage=coverage, diff=diff)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("| `crlf.py` | 0 | 1 | 3 |", result.stdout)

    def test_report_decodes_git_quoted_utf8_paths(self):
        (self.directory / "café.py").write_text("value = 1\n", encoding="utf-8")
        coverage = self.directory / "café.xml"
        coverage.write_text(
            "<coverage><class filename='café.py'><lines>"
            "<line number='1' hits='1'/>"
            "</lines></class></coverage>", encoding="utf-8")
        diff = self.directory / "café.diff"
        diff.write_text(
            'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n'
            '--- "a/caf\\303\\251.py"\n'
            '+++ "b/caf\\303\\251.py"\n'
            "@@ -0,0 +1 @@\n+value = 1\n",
            encoding="utf-8")

        result = self._run(coverage=coverage, diff=diff)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("| `café.py` | 1 | 1 | — |", result.stdout)


if __name__ == "__main__":
    unittest.main()
