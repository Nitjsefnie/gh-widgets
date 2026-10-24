"""Seed the net instrument through its real readers, writer and workflow steps."""
import contextlib
import io
import json
import unittest
import xml.etree.ElementTree as ET

from scripts.bench.bench_platform import REQUIRES_POSIX_SHELL
from tests import test_speed_ratchet as fixtures
from tests import test_ratchet_push_delivery as delivery

# These tests reuse the existing on-disk report and staging fixtures.
# pylint: disable=protected-access


class TestSpeedSeeding(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TestSpeedRatchetTightening()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture._write_reports(unit_wall=20, renderer_wall=0.085)
        for name, population in self.fixture.baseline["populations"].items():
            population["metric"] = "cpu_time_ratio"
            population["slopes"] = dict.fromkeys(population["entries"], 2)
            population["entries"] = {}
            paths = (self.fixture.unit_reports if name == "unit-suite"
                     else self.fixture.renderer_reports)
            for index, path in enumerate(paths):
                tree = ET.parse(path)
                tree.getroot().set("gh-metric", "cpu_time_ratio")
                for case in tree.iter("testcase"):
                    case.set("time", str((25 + index) / 10))
                    case.set("gh-raw-cpu", str(25 + index))
                    case.set("gh-calibration-cpu", "10")
                tree.write(path)
        self.fixture._write_baseline()

    def test_first_main_measurement_seeds_all_four_entries(self):
        result = self.fixture._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        seeded = json.loads(self.fixture.output_path.read_text())
        for name, population in seeded["populations"].items():
            with self.subTest(population=name):
                self.assertEqual(set(population["entries"]), set(population["slopes"]))
                self.assertTrue(all(value == {"min": 5, "max": 5, "n": 2}
                                    for value in population["entries"].values()))
                self.assertEqual(population["wall"],
                                 self.fixture.baseline["populations"][name]["wall"])
        self.assertEqual(fixtures.speed_ratchet.baseline.raised_entries(
            self.fixture.baseline, seeded), {})

    def test_seed_sample_count_is_the_population_report_count(self):
        path = self.fixture.reports_dir / "third.xml"
        path.write_bytes(self.fixture.unit_reports[0].read_bytes())
        self.fixture.unit_reports.append(path)
        result = self.fixture._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        seeded = json.loads(self.fixture.output_path.read_text())
        self.assertEqual(seeded["populations"]["unit-suite"]["entries"]
                         [self.fixture.UNIT_NODE]["n"], 3)
        self.assertEqual(seeded["populations"]["renderer-workloads"]["entries"]
                         [fixtures.WORKLOAD_NODES[0]]["n"], 2)

    def test_an_absent_entry_without_a_slope_still_refuses(self):
        self.fixture.baseline["populations"]["unit-suite"]["slopes"] = {
            "counter::different": 2}
        self.fixture._write_baseline()
        result = self.fixture._run()
        self.assertEqual(result.returncode, 2)
        self.assertIn("unit-suite reports entry/entries not recorded in the baseline: "
                      "counter::unit-suite", result.stderr)
        self.assertFalse(self.fixture.output_path.exists())

    def test_wall_never_seeds_even_for_a_slope_declared_node(self):
        for path in self.fixture.renderer_reports:
            tree = ET.parse(path)
            tree.getroot().findall("testcase")[1].set("gh-wall", "1")
            tree.write(path)
        result = self.fixture._run()
        self.assertEqual(result.returncode, 2)
        self.assertIn("not recorded in the baseline", result.stderr)
        self.assertFalse(self.fixture.output_path.exists())

    def test_existing_entry_maximum_remains_down_only(self):
        population = self.fixture.baseline["populations"]["unit-suite"]
        population["entries"] = {self.fixture.UNIT_NODE: {"min": 1, "max": 4, "n": 6}}
        self.fixture._write_baseline()
        result = self.fixture._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        seeded = json.loads(self.fixture.output_path.read_text())
        self.assertEqual(seeded["populations"]["unit-suite"]["entries"]
                         [self.fixture.UNIT_NODE], {"min": 1, "max": 4, "n": 6})

    def test_empty_baseline_passes_with_seed_note_but_a_rename_refuses(self):
        compare = fixtures.speed_ratchet.compare
        args = ["--baseline", str(self.fixture.baseline_path), "--population",
                "unit-suite", "--head", *map(str, self.fixture.unit_reports)]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(compare.main(args), 0)
        self.assertIn("within budget", output.getvalue())
        self.assertIn("main's ratchet will seed", output.getvalue())
        self.assertIn("on merge", output.getvalue())
        population = self.fixture.baseline["populations"]["unit-suite"]
        population["entries"] = {"counter::renamed": {"min": 1, "max": 4, "n": 2}}
        population["slopes"]["counter::renamed"] = 2
        self.fixture._write_baseline()
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            self.assertEqual(compare.main(args), 2)
        self.assertIn("share no passing test", errors.getvalue())

    @REQUIRES_POSIX_SHELL
    def test_first_main_push_step_stages_a_seed_the_consumer_accepts(self):
        result, output, _head = self.fixture._run_tighten_step()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("changed=true", output)
        seeded = json.loads((self.fixture.root / "ratchet-data/speed-baseline.json").read_text())
        consumer = delivery.TestRatchetPushDelivery()
        result, calls, installed, _summary = consumer._drive(
            "gate/speed-baseline.json", consumer._comparison([]),
            documents=(self.fixture.baseline, seeded))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(["push", "origin", "HEAD:refs/heads/main"], calls)
        self.assertEqual(json.loads(installed), seeded)


if __name__ == "__main__":
    unittest.main()
