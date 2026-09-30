"""Offline tests for the deterministic renderer benchmark fixtures."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
BENCH_DIR = REPO_ROOT / "scripts" / "bench"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fixture_setup = load_module("bench_fixture_setup", BENCH_DIR / "fixture_setup.py")
e2e_bench = load_module("bench_e2e_bench", BENCH_DIR / "e2e_bench.py")


def file_bytes(root, subdir):
    base = root / subdir
    return {path.relative_to(base).as_posix(): path.read_bytes()
            for path in sorted(base.rglob("*")) if path.is_file()}


class TestFixtureSetup(unittest.TestCase):
    def test_two_builds_have_identical_heads_payloads_and_caches(self):
        with tempfile.TemporaryDirectory(prefix="ghw-bench-determinism-") as td:
            root = Path(td)
            first = root / "first"
            second = root / "second"
            heads_first = fixture_setup.build(first)
            heads_second = fixture_setup.build(second)

            self.assertEqual(heads_first, heads_second)
            self.assertEqual(file_bytes(first, "payloads"),
                             file_bytes(second, "payloads"))
            self.assertEqual(file_bytes(first, "caches"),
                             file_bytes(second, "caches"))


class TestPayloadDispatcher(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="ghw-bench-dispatch-")
        cls.fixture_root = Path(cls.temp.name) / "fixture"
        fixture_setup.build(cls.fixture_root)
        cls.payloads = cls.fixture_root / "payloads"
        cls.shim = load_module("bench_sitecustomize_test",
                               BENCH_DIR / "sitecustomize.py")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def graphql(self, query, variables=None):
        request = urllib.request.Request(
            "https://api.github.com/graphql",
            data=json.dumps({"query": query,
                             "variables": variables or {}}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST")
        return json.loads(self.shim.dispatch(request, self.payloads))

    def test_dispatches_every_renderer_request_shape_and_passes_foreign_urls(self):
        original_urlopen = urllib.request.urlopen
        identity = self.graphql("query { user { login databaseId "
                                "organizations { nodes { login } } } }")
        self.assertEqual(identity["data"]["user"]["login"], "bench-user")
        self.assertEqual(identity["data"]["user"]["emails"],
                         ["bench-user@users.noreply.github.com"])
        viewer = self.graphql("query { viewer { login } }")
        self.assertEqual(viewer["data"]["viewer"]["login"], "bench-user")

        calendar = self.graphql("query { user { contributionsCollection { "
                                "contributionCalendar { weeks } } } }")
        self.assertIn("weeks", calendar["data"]["user"]
                      ["contributionsCollection"]["contributionCalendar"])

        pr_query = "query { user { pullRequests(states: [OPEN, CLOSED]) "
        first_prs = self.graphql(pr_query, {"cursor": None})
        first_connection = first_prs["data"]["user"]["pullRequests"]
        self.assertTrue(first_connection["pageInfo"]["hasNextPage"])
        second_prs = self.graphql(
            pr_query, {"cursor": first_connection["pageInfo"]["endCursor"]})
        self.assertFalse(second_prs["data"]["user"]["pullRequests"]
                         ["pageInfo"]["hasNextPage"])

        issue_query = "query { user { issues(states: [OPEN, CLOSED]) "
        first_issues = self.graphql(issue_query, {"cursor": None})
        issue_connection = first_issues["data"]["user"]["issues"]
        self.assertTrue(issue_connection["pageInfo"]["hasNextPage"])
        second_issues = self.graphql(
            issue_query,
            {"cursor": issue_connection["pageInfo"]["endCursor"]})
        self.assertFalse(second_issues["data"]["user"]["issues"]
                         ["pageInfo"]["hasNextPage"])

        totals = self.graphql(
            'query { a0: repository(owner:"outside-owner-a",'
            'name:"project-alpha") { defaultBranchRef { target { oid } } } }')
        total_node = totals["data"]["a0"]
        self.assertRegex(total_node["defaultBranchRef"]["target"]["oid"],
                         r"^[0-9a-f]{40}$")

        orgs = urllib.request.Request(
            "https://api.github.com/users/bench-user/orgs?per_page=100&page=1")
        self.assertEqual(json.loads(self.shim.dispatch(orgs, self.payloads)),
                         [{"id": 9001, "login": "bench-org",
                           "url": "https://api.github.com/orgs/bench-org"}])
        self.assertIsNone(self.shim.dispatch(
            urllib.request.Request("https://example.invalid/api"),
            self.payloads))
        self.assertIs(urllib.request.urlopen, original_urlopen)


class TestHarness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="ghw-bench-harness-")
        cls.root = Path(cls.temp.name)
        cls.fixture_root = cls.root / "fixture"
        fixture_setup.build(cls.fixture_root)
        cls.repo_root = cls.root / "renderer-stubs"
        cls.repo_root.mkdir()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def write_stubs(self, degraded=()):
        scripts = {
            "render.py": ("stats.svg", "streak.svg", "languages.svg",
                          "external.svg"),
            "render-impact.py": ("impact.svg",),
            "render-responsiveness.py": ("responsiveness.svg",),
        }
        degraded = set(degraded)
        for script, svgs in scripts.items():
            body = (
                "import os, sys\n"
                "from pathlib import Path\n"
                "out = Path(os.environ['OUT_DIR'])\n"
                "out.mkdir(parents=True, exist_ok=True)\n"
                f"for name in {svgs!r}:\n"
                "    (out / name).write_text('<svg>stable</svg>', "
                "encoding='utf-8')\n"
                "(out / 'last-updated.txt').write_text('fixed', "
                "encoding='utf-8')\n"
            )
            if script in degraded:
                if script == "render-impact.py":
                    body += "print('stub failure')\nsys.exit(7)\n"
                else:
                    body += "print('fetch failed; rendered from cache')\n"
            else:
                body += "print('wrote stub output')\n"
            (self.repo_root / script).write_text(body, encoding="utf-8")

    def invoke(self, work_root, round_number, junit_path, selfcheck=False,
               repo_root=None):
        command = [
            sys.executable,
            str(BENCH_DIR / "e2e_bench.py"),
            "--side", "head",
            "--repo-root", str(repo_root or self.repo_root),
            "--round", str(round_number),
            "--junit-file", str(junit_path),
            "--work-root", str(work_root),
        ]
        if selfcheck:
            command.append("--selfcheck")
        env = os.environ.copy()
        env["GH_BENCH_FIXTURE_ROOT"] = str(self.fixture_root)
        return subprocess.run(command, env=env, capture_output=True,
                              text=True, check=False)

    def test_harness_emits_live_junit_and_copies_outputs(self):
        self.write_stubs()
        work = self.root / "success-work"
        report = self.root / "success.xml"
        result = self.invoke(work, 1, report)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        suite = ET.parse(report).getroot()
        self.assertEqual(suite.tag, "testsuite")
        cases = {case.get("name"): case for case in suite.findall("testcase")}
        self.assertEqual(set(cases), {
            "bench.render", "bench.render-impact",
            "bench.render-responsiveness"})
        self.assertTrue(all(case.get("classname") == "e2e"
                            for case in cases.values()))
        self.assertTrue(all(case.get("time") is not None
                            and case.find("failure") is None
                            for case in cases.values()))
        self.assertTrue((work / "outputs/head-1/render/stats.svg").is_file())
        self.assertTrue((work / "outputs/head-1/render/last-updated.txt").is_file())

    def test_nonzero_and_degraded_renderers_are_junit_failures(self):
        self.write_stubs(degraded=("render.py", "render-impact.py"))
        work = self.root / "failure-work"
        report = self.root / "failure.xml"
        result = self.invoke(work, 1, report)

        self.assertEqual(result.returncode, 1)
        cases = {case.get("name"): case
                 for case in ET.parse(report).getroot().findall("testcase")}
        self.assertIsNotNone(cases["bench.render"].find("failure"))
        self.assertIn("fetch failed", cases["bench.render"].find("failure").text)
        self.assertIsNotNone(cases["bench.render-impact"].find("failure"))
        self.assertIn("exited with code 7",
                      cases["bench.render-impact"].find("failure").text)

    def test_missing_renderer_is_skipped(self):
        self.write_stubs()
        old_checkout = self.root / "old-checkout"
        old_checkout.mkdir()
        (old_checkout / "render.py").write_bytes(
            (self.repo_root / "render.py").read_bytes())
        report = self.root / "missing-renderers.xml"
        result = self.invoke(self.root / "missing-work", 1, report,
                             repo_root=old_checkout)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        cases = {case.get("name"): case
                 for case in ET.parse(report).getroot().findall("testcase")}
        self.assertIsNotNone(cases["bench.render-impact"].find("skipped"))
        self.assertIsNotNone(
            cases["bench.render-responsiveness"].find("skipped"))

    def test_selfcheck_compares_svg_bytes_across_head_rounds(self):
        self.write_stubs()
        work = self.root / "selfcheck-work"
        first = self.invoke(work, 1, self.root / "selfcheck-1.xml")
        second = self.invoke(work, 2, self.root / "selfcheck-2.xml",
                             selfcheck=True)

        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

    def test_selfcheck_reports_changed_svg_bytes(self):
        self.write_stubs()
        work = self.root / "changed-selfcheck-work"
        first = self.invoke(work, 1, self.root / "changed-selfcheck-1.xml")
        changed = work / "outputs/head-1/render/stats.svg"
        changed.write_text("<svg>changed</svg>", encoding="utf-8")
        second = self.invoke(work, 2, self.root / "changed-selfcheck-2.xml",
                             selfcheck=True)

        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(second.returncode, 1)
        self.assertIn("stats.svg: bytes differ", second.stderr)


if __name__ == "__main__":
    unittest.main()
