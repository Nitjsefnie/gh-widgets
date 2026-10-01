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

import impact_clone


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
from bench_platform import REQUIRES_BENCH  # noqa: E402
render_impact = load_module("bench_render_impact",
                            REPO_ROOT / "render-impact.py")


def file_bytes(root, subdir):
    base = root / subdir
    return {path.relative_to(base).as_posix(): path.read_bytes()
            for path in sorted(base.rglob("*")) if path.is_file()}


def _number(element, attribute):
    """One XML attribute as a float, refusing to read a missing one.

    `Element.get` is Optional[str]; a test that read an absent attribute as
    zero would pass for the wrong reason, which is the defect the counter
    change is about, applied to the test that pins it.
    """
    raw = element.get(attribute)
    if raw is None:
        raise AssertionError(f"{attribute} is absent from {element.tag}")
    return float(raw)


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

    def test_seeded_ourloc_count_differs_from_live_fixture_count(self):
        with tempfile.TemporaryDirectory(prefix="ghw-bench-stale-count-") as td:
            root = Path(td) / "fixture"
            fixture_setup.build(root)
            cache = json.loads((root / "caches" /
                                "impact-cache.json").read_text(
                                    encoding="utf-8"))
            expected_lines = 120 * 60 + 80
            for repo, entry in cache["ourloc"].items():
                self.assertNotEqual(entry["ours"], expected_lines, repo)


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

    # The renderer's profile query, abbreviated but with the selections that
    # identify it: its own fields, and an organizations connection that pages.
    PROFILE_ORG_QUERY = (
        "query($login: String!, $cursor: String) { user(login: $login) { "
        "login name followers { totalCount } "
        "organizations(first: 100, after: $cursor) { "
        "pageInfo { hasNextPage endCursor } nodes { login } } } }")
    # What the base release still sends: one query carrying BOTH connections,
    # so the org branch must not steal it.
    COMBINED_PROFILE_QUERY = (
        "query($login: String!) { user(login: $login) { login name "
        "followers { totalCount } organizations(first: 100) { nodes { login } } "
        "repositories(first: 100, ownerAffiliations: OWNER, isFork: false) { "
        "totalCount nodes { stargazerCount } } } }")

    def test_dispatches_the_profile_org_query_with_its_own_fields(self):
        # The identity payload models the identity query and carries no
        # followers; serving it here made render.py exit 2 on 'followers'.
        response = self.graphql(self.PROFILE_ORG_QUERY, {"cursor": None})
        user = response["data"]["user"]
        self.assertIn("followers", user)
        self.assertIn("name", user)
        self.assertIn("pageInfo", user["organizations"])

    def test_dispatches_both_profile_org_pages(self):
        # A fixture that only ever answers hasNextPage: false cannot tell a
        # renderer that walks the connection from one that stops at page one.
        first_page = self.graphql(self.PROFILE_ORG_QUERY, {"cursor": None})
        connection = first_page["data"]["user"]["organizations"]
        self.assertTrue(connection["pageInfo"]["hasNextPage"])
        second_page = self.graphql(
            self.PROFILE_ORG_QUERY,
            {"cursor": connection["pageInfo"]["endCursor"]})
        self.assertFalse(second_page["data"]["user"]["organizations"]
                         ["pageInfo"]["hasNextPage"])
        self.assertNotEqual(
            [node["login"] for node in connection["nodes"]],
            [node["login"] for node
             in second_page["data"]["user"]["organizations"]["nodes"]])

    def test_the_org_branch_does_not_steal_identity_or_the_combined_query(self):
        # Two neighbours the org branch could swallow by accident: the
        # identity query, which contains organizations too, and the base
        # release's single combined profile query, which contains BOTH
        # connections and also selects followers.
        identity = self.graphql("query { user { login databaseId "
                                "organizations { nodes { login } } } }")
        self.assertIn("databaseId", identity["data"]["user"])
        self.assertIn("emails", identity["data"]["user"])

        combined = self.graphql(self.COMBINED_PROFILE_QUERY, {"cursor": None})
        self.assertIn("repositories", combined["data"]["user"])
        self.assertIn("followers", combined["data"]["user"])
        self.assertIn("pullRequests", combined["data"]["user"])

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
                        impact_clone, "_run_clone_command",
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
    # Every case below drives e2e_bench.py, which measures through
    # scripts/ci/counter.py; see bench_platform for why that is one
    # decision in one place rather than a guard per file.
    @REQUIRES_BENCH
    @classmethod
    def setUpClass(cls):
        cls.root = class_temp_path(cls, "ghw-bench-harness-")
        cls.fixture_root = cls.root / "fixture"
        fixture_setup.build(cls.fixture_root)
        cls.repo_root = cls.root / "renderer-stubs"
        cls.repo_root.mkdir()

    def write_stubs(self, degraded=(), refresh_impact=True,
                    wrote_summary=True):
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
            if script == "render-impact.py" and refresh_impact:
                body += (
                    "import json\n"
                    "payloads = Path(os.environ['GH_BENCH_PAYLOADS'])\n"
                    "manifest = json.loads((payloads / 'manifest.json')."
                    "read_text(encoding='utf-8'))\n"
                    "cache_path = Path(os.environ['CACHE_FILE'])\n"
                    "cache = json.loads(cache_path.read_text("
                    "encoding='utf-8'))\n"
                    "for repo, head in manifest['repo_heads'].items():\n"
                    "    entry = cache['ourloc'][repo]\n"
                    "    entry['head'] = head\n"
                    "    entry['ours'] = manifest['expected_ourloc_lines'][repo]\n"
                    "    entry['total'] = manifest['expected_ourloc_lines'][repo]\n"
                    "cache_path.write_text(json.dumps(cache), encoding='utf-8')\n"
                )
            if script in degraded:
                if script == "render-impact.py":
                    body += "print('stub failure')\nsys.exit(7)\n"
                else:
                    body += "print('fetch failed; rendered from cache')\n"
            elif wrote_summary:
                body += "print('wrote stub output')\n"
            else:
                body += "print('renderer finished without summary')\n"
            (self.repo_root / script).write_text(body, encoding="utf-8")

    def invoke(self, work_root, round_number, junit_path, selfcheck=False,
               repo_root=None, side="head"):
        command = [
            sys.executable,
            str(BENCH_DIR / "e2e_bench.py"),
            "--side", side,
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
        self.assertTrue(all(case.find("failure") is None
                            for case in cases.values()))
        # `time` is the COUNTER now, not a duration, so what is pinned is
        # that it is present and numeric — and that the suite says which
        # instrument it is. Its magnitude is not a test-harness property.
        self.assertTrue(all(_number(case, "time") > 0
                            for case in cases.values()))
        # Which instrument depends on what this host's kernel permits, so
        # what is pinned is that the suite NAMES it and that it is the one
        # the counter would pick here — not a particular instrument.
        self.assertEqual(suite.get("gh-metric"),
                         e2e_bench.counter.choose_metric())
        self.assertIn(suite.get("gh-metric"), e2e_bench.counter.METRICS)
        self.assertTrue(all(_number(case, "gh-wall") > 0
                            for case in cases.values()))
        self.assertTrue((work / "outputs/head-1/render/stats.svg").is_file())
        self.assertTrue((work / "outputs/head-1/render/last-updated.txt").is_file())

    def test_harness_rejects_impact_run_that_keeps_stale_ourloc(self):
        self.write_stubs(refresh_impact=False)
        report = self.root / "stale-impact.xml"
        result = self.invoke(self.root / "stale-impact-work", 1, report)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        cases = {case.get("name"): case for case in
                 ET.parse(report).getroot().findall("testcase")}
        failure = cases["bench.render-impact"].find("failure")
        if failure is None:
            self.fail("impact refresh validation did not emit a failure")
        self.assertIn("ourloc", failure.text or "")

    def test_harness_rejects_renderer_without_live_summary(self):
        self.write_stubs(wrote_summary=False)
        report = self.root / "missing-summary.xml"
        result = self.invoke(self.root / "missing-summary-work", 1, report)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        cases = {case.get("name"): case for case in
                 ET.parse(report).getroot().findall("testcase")}
        failure = cases["bench.render"].find("failure")
        if failure is None:
            self.fail("missing live summary did not emit a failure")
        self.assertIn("wrote summary", failure.text or "")

    def test_renderer_environment_rewrites_clone_urls_to_local_mirrors(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            env = e2e_bench._renderer_environment(  # pylint: disable=protected-access
                self.fixture_root, BENCH_DIR, self.root / "cache.json",
                self.root / "out")
        manifest = json.loads((self.fixture_root / "payloads" /
                               "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(env["GIT_CONFIG_COUNT"],
                         str(len(manifest["repo_heads"])))
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
        repo = next(iter(manifest["repo_heads"]))
        mirror = self.fixture_root / "mirror" / repo.replace("/", "__")
        self.assertIn("file://", env["GIT_CONFIG_KEY_0"])
        self.assertEqual(env["GIT_CONFIG_VALUE_0"],
                         f"https://github.com/{repo}.git")
        completed = subprocess.run(
            ["git", "ls-remote", f"https://github.com/{repo}.git", "HEAD"],
            env=env, capture_output=True, text=True, check=False, timeout=10)
        self.assertEqual(completed.returncode, 0,
                         completed.stdout + completed.stderr)
        self.assertIn(manifest["repo_heads"][repo], completed.stdout)
        self.assertTrue(mirror.is_dir())

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

    def test_missing_renderer_fails_on_the_head_side(self):
        """A shipped renderer that is not in the head checkout is a failure.

        It used to be a skip, and a skip is dropped from the comparator's
        intersection — so the speed gate went on measuring one fewer program
        and reported the result as a pass (issue #36).
        """
        self.write_stubs()
        old_checkout = self.root / "old-checkout"
        old_checkout.mkdir()
        (old_checkout / "render.py").write_bytes(
            (self.repo_root / "render.py").read_bytes())
        report = self.root / "missing-renderers.xml"
        result = self.invoke(self.root / "missing-work", 1, report,
                             repo_root=old_checkout)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        cases = {case.get("name"): case
                 for case in ET.parse(report).getroot().findall("testcase")}
        for name in ("bench.render-impact", "bench.render-responsiveness"):
            case = cases[name]
            self.assertIsNone(
                case.find("skipped"),
                f"{name} was skipped on the head side")
            failure = case.find("failure")
            if failure is None:
                self.fail(f"{name} did not emit a JUnit failure")
            self.assertIn("missing from the head checkout", failure.text or "")
        # The one renderer that IS present still ran and passed.
        self.assertIsNone(cases["bench.render"].find("failure"))
        # ...and the reason reaches the step log, not only the uploaded XML:
        # a red step whose detail lives in an artifact is a step nobody can
        # act on without downloading the artifact first.
        self.assertIn("render-impact.py is missing from the head checkout",
                      result.stderr)
        # The status line carries the counter and the instrument that
        # measured it. The `s` that used to sit there is gone: the number is
        # not seconds, and printing a counter with a seconds suffix is the
        # exact confusion this change exists to remove.
        self.assertIn("bench.render-impact (head round 1): 0.000 unmeasured "
                      "failed", result.stdout)

    def test_missing_renderer_is_still_skipped_on_the_base_side(self):
        """The asymmetry is deliberate, and this is what pins it.

        speed.yml runs HEAD's harness against the baseline release too, so a
        renderer added after that release has no base script — a
        new-since-baseline workload, not a missing one. Failing here too
        would redden the gate on every commit that adds a renderer.
        """
        self.write_stubs()
        # The class root is shared, so the checkout name has to differ from
        # the head-side test's.
        old_checkout = self.root / "old-checkout-base-side"
        old_checkout.mkdir()
        (old_checkout / "render.py").write_bytes(
            (self.repo_root / "render.py").read_bytes())
        report = self.root / "base-side-missing.xml"
        result = self.invoke(self.root / "base-side-missing-work", 1, report,
                             repo_root=old_checkout, side="base")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        cases = {case.get("name"): case
                 for case in ET.parse(report).getroot().findall("testcase")}
        self.assertIsNotNone(cases["bench.render-impact"].find("skipped"))
        self.assertIsNotNone(
            cases["bench.render-responsiveness"].find("skipped"))
        self.assertIsNone(cases["bench.render"].find("failure"))

    def test_list_workloads_needs_no_checkout_or_fixtures(self):
        env = dict(os.environ)
        env.pop("GH_BENCH_FIXTURE_ROOT", None)
        result = subprocess.run(
            [sys.executable, str(BENCH_DIR / "e2e_bench.py"),
             "--list-workloads"],
            env=env, capture_output=True, text=True, check=False)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.split(),
                         ["e2e::bench.render", "e2e::bench.render-impact",
                          "e2e::bench.render-responsiveness"])

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
