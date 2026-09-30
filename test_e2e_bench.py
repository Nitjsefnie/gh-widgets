"""Offline tests for the deterministic renderer benchmark fixtures."""
import importlib.util
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock


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
render_impact = load_module("bench_render_impact",
                            REPO_ROOT / "render-impact.py")


def file_bytes(root, subdir):
    base = root / subdir
    return {path.relative_to(base).as_posix(): path.read_bytes()
            for path in sorted(base.rglob("*")) if path.is_file()}


def class_temp_path(test_case, prefix):
    stack = contextlib.ExitStack()
    test_case.addClassCleanup(stack.close)
    return Path(stack.enter_context(tempfile.TemporaryDirectory(
        prefix=prefix)))


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
        cls.fixture_root = class_temp_path(
            cls, "ghw-bench-dispatch-") / "fixture"
        fixture_setup.build(cls.fixture_root)
        cls.payloads = cls.fixture_root / "payloads"
        cls.shim = load_module("bench_sitecustomize_test",
                               BENCH_DIR / "sitecustomize.py")

    def graphql(self, query, variables=None):
        request = urllib.request.Request(
            "https://api.github.com/graphql",
            data=json.dumps({"query": query,
                             "variables": variables or {}}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST")
        return json.loads(self.shim.dispatch(request, self.payloads))

    def test_dispatches_identity_viewer_and_rest_orgs(self):
        identity = self.graphql("query { user { login databaseId "
                                "organizations { nodes { login } } } }")
        self.assertEqual(identity["data"]["user"]["login"], "bench-user")
        self.assertEqual(identity["data"]["user"]["emails"],
                         ["bench-user@users.noreply.github.com"])
        viewer = self.graphql("query { viewer { login } }")
        self.assertEqual(viewer["data"]["viewer"]["login"], "bench-user")

        orgs = urllib.request.Request(
            "https://api.github.com/users/bench-user/orgs?per_page=100&page=1")
        self.assertEqual(json.loads(self.shim.dispatch(orgs, self.payloads)),
                         [{"id": 9001, "login": "bench-org",
                           "url": "https://api.github.com/orgs/bench-org"}])

    def test_dispatches_calendar_and_both_pr_pages(self):
        calendar = self.graphql("query { user { contributionsCollection { "
                                "contributionCalendar { weeks } } } }")
        self.assertIn("weeks", calendar["data"]["user"]
                      ["contributionsCollection"]["contributionCalendar"])

        query = "query { user { pullRequests(states: [OPEN, CLOSED]) "
        first_page = self.graphql(query, {"cursor": None})
        connection = first_page["data"]["user"]["pullRequests"]
        self.assertTrue(connection["pageInfo"]["hasNextPage"])
        second_page = self.graphql(
            query, {"cursor": connection["pageInfo"]["endCursor"]})
        self.assertFalse(second_page["data"]["user"]["pullRequests"]
                         ["pageInfo"]["hasNextPage"])

    def test_dispatches_both_issue_pages_and_totals_alias(self):
        query = "query { user { issues(states: [OPEN, CLOSED]) "
        first_page = self.graphql(query, {"cursor": None})
        connection = first_page["data"]["user"]["issues"]
        self.assertTrue(connection["pageInfo"]["hasNextPage"])
        second_page = self.graphql(
            query, {"cursor": connection["pageInfo"]["endCursor"]})
        self.assertFalse(second_page["data"]["user"]["issues"]
                         ["pageInfo"]["hasNextPage"])

        totals = self.graphql(
            'query { a0: repository(owner:"outside-owner-a",'
            'name:"project-alpha") { defaultBranchRef { target { oid } } } }')
        self.assertRegex(totals["data"]["a0"]["defaultBranchRef"]
                         ["target"]["oid"], r"^[0-9a-f]{40}$")

    def test_foreign_urls_pass_through_without_installing_the_shim(self):
        original_urlopen = urllib.request.urlopen
        request = urllib.request.Request("https://example.invalid/api")
        self.assertIsNone(self.shim.dispatch(request, self.payloads))
        self.assertIs(urllib.request.urlopen, original_urlopen)


class TestCloneSourceMirror(unittest.TestCase):
    """Use offline mirror paths only when a matching local repo exists."""

    loc = getattr(render_impact, "_LOC_MODULE")

    def clone_command(self, mirror_state):
        with tempfile.TemporaryDirectory(prefix="ghw-local-mirror-") as td:
            root = Path(td)
            mirror = root / "outside__project"
            if mirror_state == "present":
                mirror.mkdir()
            dest = root / "clone"
            dest.mkdir()
            with mock.patch.dict(os.environ, {}, clear=True):
                if mirror_state != "unset":
                    os.environ["CLONE_SOURCE_DIR"] = str(root)
                with mock.patch.object(
                        self.loc.subprocess, "run",
                        return_value=subprocess.CompletedProcess([], 0)) as run:
                    self.loc.clone_repo("outside/project", "main", dest)
                    command = run.call_args.args[0]
            return command, str(mirror), str(dest)

    def test_clone_uses_local_mirror_when_present(self):
        command, mirror, dest = self.clone_command("present")
        self.assertEqual(command[-2:], [mirror, dest])
        self.assertNotIn("--branch", command)

    def test_clone_uses_network_url_when_mirror_is_unset(self):
        command, _mirror, dest = self.clone_command("unset")
        self.assertEqual(command[-4:], ["--branch", "main",
                                        "https://github.com/outside/project.git",
                                        dest])

    def test_clone_falls_back_when_mirror_repo_is_missing(self):
        command, _mirror, dest = self.clone_command("missing")
        self.assertIn("https://github.com/outside/project.git", command)
        self.assertEqual(command[-1], dest)


class TestHarness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = class_temp_path(cls, "ghw-bench-harness-")
        cls.fixture_root = cls.root / "fixture"
        fixture_setup.build(cls.fixture_root)
        cls.repo_root = cls.root / "renderer-stubs"
        cls.repo_root.mkdir()

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
        render_failure = cases["bench.render"].find("failure")
        if render_failure is None:
            self.fail("degraded renderer did not emit a JUnit failure")
        self.assertIn("fetch failed", render_failure.text or "")
        impact_failure = cases["bench.render-impact"].find("failure")
        if impact_failure is None:
            self.fail("nonzero renderer did not emit a JUnit failure")
        self.assertIn("exited with code 7", impact_failure.text or "")

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
