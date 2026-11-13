"""The unit-suite population digest command: scripts/ci/population_digest.py."""
import contextlib
import hashlib
import importlib.util
import io
import os
import shutil
import unittest
from pathlib import Path

from tests import _ghw_temp

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "population_digest.py"
RECORDED = REPO_ROOT / "gate" / "speed-baseline.json"
IDS = ("tests.test_alpha.Alpha.test_one", "tests.test_alpha.Alpha.test_two")
ALPHA = ("import unittest\n\n\nclass Alpha(unittest.TestCase):\n"
         "    def test_two(self):\n        pass\n\n"
         "    def test_one(self):\n        pass\n")


def _load():
    spec = importlib.util.spec_from_file_location("ghw_population_digest",
                                                  SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


population_digest = _load()


def _digest(ids):
    joined = "\n".join(sorted(ids)) + "\n"
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


class PopulationDigestTest(unittest.TestCase):

    def setUp(self):
        tmp = _ghw_temp.tempdir(prefix="ghw-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "tests").mkdir()
        (self.root / "tests" / "__init__.py").write_text("", "utf-8")
        (self.root / "tests" / "test_alpha.py").write_text(ALPHA, "utf-8")
        (self.root / "gate").mkdir()
        self.baseline = self.root / "gate" / "speed-baseline.json"
        shutil.copyfile(RECORDED, self.baseline)
        self.recorded = population_digest.compare.read_population(
            population_digest.compare.read_baseline(RECORDED),
            "unit-suite")["population"]

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = population_digest.main([*argv, str(self.root)])
        return code, out.getvalue(), err.getvalue()

    def test_prints_the_digest_of_the_collected_ids_and_writes_nothing(self):
        before = self.baseline.read_bytes()
        code, out, _ = self.run_main()
        self.assertEqual((code, out), (0, _digest(IDS) + "\n"))
        self.assertEqual(self.baseline.read_bytes(), before)

    def test_write_replaces_only_the_unit_suite_digest(self):
        before = self.baseline.read_bytes()
        os.chmod(self.baseline, 0o640)
        code, out, err = self.run_main("--write")
        self.assertEqual((code, out), (0, _digest(IDS) + "\n"))
        self.assertIn(f"{self.recorded} -> {_digest(IDS)}", err)
        self.assertEqual(self.baseline.read_bytes(), before.replace(
            self.recorded.encode(), _digest(IDS).encode()))
        self.assertEqual(self.baseline.stat().st_mode & 0o777, 0o640)
        inode = self.baseline.stat().st_ino
        self.assertEqual(self.run_main("--write")[0], 0)
        self.assertEqual(self.baseline.stat().st_ino, inode)

    def test_a_failed_import_is_refused_by_name_and_writes_nothing(self):
        self.assertEqual(self.run_main("--write")[0], 0)
        written = self.baseline.read_bytes()
        (self.root / "tests" / "test_broken.py").write_text(
            "import ghw_no_such_module\n", "utf-8")
        code, out, err = self.run_main("--write")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("failed to import", err)
        self.assertIn(": tests.test_broken.", err)
        self.assertEqual(self.baseline.read_bytes(), written)

    def test_a_collection_failure_is_refused(self):
        (self.root / "tests" / "test_alpha.py").unlink()
        code, out, err = self.run_main()
        self.assertEqual((code, out), (2, ""))
        self.assertIn("collected no tests", err)

    def refuse_rewrite(self, text, message):
        self.baseline.write_text(text, "utf-8")
        before = self.baseline.read_bytes()
        code, out, err = self.run_main("--write")
        self.assertEqual((code, out), (2, ""))
        self.assertIn(message, err)
        self.assertEqual(self.baseline.read_bytes(), before)

    def test_a_digest_written_twice_is_refused(self):
        text = self.baseline.read_text("utf-8")
        renderer = population_digest.compare.read_population(
            population_digest.compare.read_baseline(RECORDED),
            "renderer-workloads")["population"]
        self.refuse_rewrite(text.replace(renderer, self.recorded),
                            "2 times")

    def test_a_rewrite_that_would_land_on_another_field_is_refused(self):
        # The unit-suite value is JSON-escaped, so the one raw occurrence
        # left is the renderer field, which must not be the one rewritten.
        text = self.baseline.read_text("utf-8")
        renderer = population_digest.compare.read_population(
            population_digest.compare.read_baseline(RECORDED),
            "renderer-workloads")["population"]
        escaped = f"\\u{ord(self.recorded[0]):04x}{self.recorded[1:]}"
        text = text.replace(self.recorded, escaped).replace(
            renderer, self.recorded)
        self.refuse_rewrite(text, "would change another value")


if __name__ == "__main__":
    unittest.main()
