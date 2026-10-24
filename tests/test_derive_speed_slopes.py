"""Offline calibration fits use the same job's clean unit-step readings."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/ci/derive_speed_slopes.py"


def fixture_log():
    lines = []
    for number, calibration, raw in ((1, 1, 12), (2, 3, 16)):
        lines += [f"##[group]unit suite round {number}",
                  f"counter: calibration = {calibration} cpu_time; suite raw {raw} cpu_time"]
    for number, values in ((1, (8, 9, 6)), (2, (10, 15, 10))):
        for entry, raw in zip(("render", "render-impact", "render-responsiveness"), values):
            lines.append(f"bench.{entry} (head round {number}): 0.2 cpu_time_ratio passed; "
                         f"calibration = {100 * number} cpu_time; renderer raw {raw} cpu_time")
    lines.append("Artifact download URL: https://github.com/Nitjsefnie/gh-widgets/actions/runs/12345678/artifacts/1")
    return "\n".join(lines)


class TestDeriveSpeedSlopes(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), "the derivation entry point is missing")
        spec = importlib.util.spec_from_file_location("ghw_slope_tests", SCRIPT)
        assert spec is not None and spec.loader is not None
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_parse_and_fit_pin_clean_calibration_and_per_entry_counts(self):
        rows = self.module.parse_log(fixture_log(), "42", "12345678")
        slopes, counts = self.module.fit(rows)
        self.assertEqual(slopes, {"counter::unit-suite": 2, "e2e::bench.render": 1,
                                  "e2e::bench.render-impact": 3,
                                  "e2e::bench.render-responsiveness": 2})
        self.assertEqual(counts, dict.fromkeys(slopes, 2))
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(row["job"] == "42" and row["run"] == "12345678" for row in rows))
        self.assertEqual({row["calibration"] for row in rows}, {1, 3})

    def test_from_file_prints_json_table_and_counts_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job-42.log"
            path.write_text(fixture_log(), encoding="utf-8")
            output = io.StringIO()
            with mock.patch.object(self.module.subprocess, "run",
                                   side_effect=AssertionError("offline means no child")), \
                    contextlib.redirect_stdout(output):
                self.assertEqual(self.module.main(["--from-file", str(path)]), 0)
        block, _end = json.JSONDecoder().raw_decode(output.getvalue())
        self.assertEqual(block["slopes"]["e2e::bench.render-impact"], 3)
        self.assertIn("12345678", output.getvalue())
        self.assertIn("| Entry | Rounds |", output.getvalue())
        self.assertIn("| e2e::bench.render | 2 |", output.getvalue())

    def test_unparseable_named_job_refuses_with_exit_two(self):
        errors = io.StringIO()
        with mock.patch.object(self.module, "fetch_job", return_value=("100", "garbage")), \
                contextlib.redirect_stderr(errors):
            self.assertEqual(self.module.main(["--job", "42"]), 2)
        self.assertIn("42", errors.getvalue())
        self.assertIn("no parseable rounds", errors.getvalue())

    def test_constant_calibration_refuses_instead_of_inventing_a_slope(self):
        rows = self.module.parse_log(fixture_log().replace("= 3 cpu_time;", "= 1 cpu_time;"),
                                     "42", "100")
        with self.assertRaises(ValueError):
            self.module.fit(rows)


if __name__ == "__main__":
    unittest.main()
