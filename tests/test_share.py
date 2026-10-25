"""Unit-order acquisition sharing, through the real GraphQL client and cards."""
import contextlib
import copy
import io
import json
import os
import re
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

import ghwidgets_share as share
from tests import test_render, test_impact_fallback, test_responsiveness

profile = test_render.render
impact = test_impact_fallback.render_impact
resp = test_responsiveness.resp


def item(node_id, owner="other", *, issue=False, merged=True):
    """Complete authored node, including private and insider filter inputs."""
    node = {
        "id": node_id, "number": 1, "url": f"https://github.com/{owner}/repo/1",
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-02T00:00:00Z",
        "closedAt": "2026-01-02T00:00:00Z" if merged else None,
        "state": "CLOSED" if issue else "MERGED" if merged else "OPEN",
        "repository": {"id": f"R_{owner}", "nameWithOwner": f"{owner}/repo",
                       "url": f"https://github.com/{owner}/repo",
                       "isPrivate": owner == "private",
                       "owner": {"login": owner}},
    }
    if issue:
        node["stateReason"] = "COMPLETED"
    else:
        node.update(merged=merged, mergedAt=node["closedAt"])
    return node


class CountingAPI(test_render.FakeAPI):
    """Strict transport: repeated reads return the same pages, never an empty default."""

    def __init__(self):
        super().__init__()
        self.renderer = ""
        self.counts = Counter()
        self.nodes = [item("live"), item("org", "MyOrg"),
                      item("private", "private"), item("extra", "Added")]
        self.archived = []
        self.issues = [item("I1", issue=True), item("I2", issue=True)]
        self.issues[1].update(state="OPEN", stateReason="REOPENED", closedAt=None)

    def __call__(self, request, **_kwargs):
        payload = json.loads(request.data)
        query, variables = payload["query"], payload.get("variables") or {}
        cursor = variables.get("cursor")
        if "databaseId" in query:
            kind = "identity"
            data = {"user": {"login": "me", "databaseId": 42,
                             "organizations": self.page(
                                 [{"login": "MyOrg" if cursor is None else "SecondOrg"}],
                                 "org-next" if cursor is None else None)}}
        elif "user(login:" in query and "pullRequests" in query:
            kind = "sweep" if "states: [MERGED]" in query else "prs"
            nodes = [] if "states: [OPEN, CLOSED]" in query else self.nodes
            if "states: [OPEN, CLOSED, MERGED]" in query:
                nodes = nodes[:2] if cursor is None else nodes[2:] + self.archived
                next_cursor = "pr-next" if cursor is None else None
            else:
                next_cursor = None
            data = {"user": {"pullRequests": self.page(nodes, next_cursor)}}
        elif "user(login:" in query and "issues" in query:
            kind = "issues"
            data = {"user": {"issues": self.page(
                [self.issues[0 if cursor is None else 1]],
                "issue-next" if cursor is None else None)}}
        elif "author { __typename }" in query:
            kind = "bots"
            data = {"repository": {"pullRequests": self.page([])}}
        elif "a0: repository" in query:
            kind = "totals"
            data = {alias: {"defaultBranchRef": None,
                            "issues": {"totalCount": 10},
                            "pullRequests": {"totalCount": 10}}
                    for alias in re.findall(r"(a\d+): repository", query)}
        else:
            kinds = ("organizations", "repositories", "contributionCalendar")
            kind = next((name for name in kinds if name in query), None)
            if kind is None:
                raise AssertionError(f"unmodelled query: {query}")
            data = super().__call__("t", query, variables)
        self.counts[self.renderer, kind, cursor] += 1
        return contextlib.closing(io.BytesIO(json.dumps({"data": data}).encode()))

    @staticmethod
    def page(nodes, cursor=None):
        return {"nodes": nodes, "pageInfo": {
            "hasNextPage": cursor is not None, "endCursor": cursor}}


class UnitSharing(unittest.TestCase):
    """The producer publishes one acquisition for both subsequent renderers."""

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.api = CountingAPI()
        self.output = io.StringIO()
        self.profile_cache = self.root / "cache.json"
        self.impact_cache = self.root / "impact-cache.json"
        self.environment = {
            "GH_WIDGETS_SHARE_DIR": str(self.root / "share"),
            "GH_WIDGETS_CACHE_FILE": str(self.profile_cache),
            "GH_WIDGETS_GH_EXTRA_ORGS": "",
            "GH_WIDGETS_GH_EXTRA_INSIDERS": "Added",
            "GH_WIDGETS_GH_EXTRA_EMAILS": "extra@example.org",
            "GH_WIDGETS_RESP_MIN_PRS": "1", "INVOCATION_ID": "unit-run-one",
        }

    def run_renderer(self, module, *, resync=False):
        self.api.renderer = module.__name__
        argv = [module.__file__, "--user", "me", "--token", "t",
                "--out-dir", str(self.root / "out")]
        if module is not profile:
            argv += ["--cache-file", str(self.impact_cache)]
        if resync and module is not resp:
            argv.append("--resync")
        with mock.patch.dict(os.environ, self.environment), \
                mock.patch.object(sys, "argv", argv), \
                mock.patch("urllib.request.urlopen", self.api), \
                mock.patch.object(impact, "update_loc", return_value={}), \
                mock.patch.object(module.common, "DEBUG_TIMING", True), \
                contextlib.redirect_stdout(self.output):
            module.main()

    def test_three_renderers_share_pages_and_preserve_both_pr_caches(self):
        for resync in (False, True):
            with self.subTest(resync=resync):
                self.api.counts.clear()
                cached = test_render.complete_cache()
                cached["prs"] = {"profile-only": item("profile-only")}
                profile.save_cache(self.profile_cache, cached)
                impact.save_cache(self.impact_cache, {
                    "version": 1, "prs": {"impact-only": item("impact-only")}})
                for module in (profile, impact, resp):
                    self.run_renderer(module, resync=resync)
                counts = self.api.counts
                self.assertEqual(sum(n for (_, kind, _), n in counts.items()
                                     if kind == "issues"), 2)
                self.assertEqual(counts[profile.__name__, "issues", None], 1)
                self.assertEqual(counts[profile.__name__, "issues", "issue-next"], 1)
                self.assertEqual(sum(n for (_, kind, cursor), n in counts.items()
                                     if kind == "prs" and cursor is None), 1)
                self.assertEqual(counts[profile.__name__, "sweep", None], 0 if resync else 1)
                self.assertEqual(sum(n for (renderer, _, _), n in counts.items()
                                     if renderer == resp.__name__), 0)
                self.assertEqual(sum(n for (_, kind, _), n in counts.items()
                                     if kind == "identity"), 2)
                for path in (self.profile_cache, self.impact_cache):
                    self.assertEqual(set(json.loads(path.read_text())["prs"]),
                                     {"profile-only", "impact-only", "live",
                                      "org", "private", "extra"})
                    self.assertEqual(json.loads(path.read_text())["issues"][1]["state"], "OPEN")
                svg = (self.root / "out" / "responsiveness.svg").read_text()
                self.assertIn("other/repo", svg)
                self.assertNotIn("MyOrg/repo", svg)
                self.assertNotIn("Added/repo", svg)
                self.assertNotIn("private/repo", svg)
                text = self.output.getvalue().split("=== render-responsiveness timing ===")[-1]
                self.assertIn("graphql: count=0 blocked=0.000s", text)

    def test_standalone_renderers_still_fetch_live(self):
        self.environment["GH_WIDGETS_SHARE_DIR"] = ""
        for module in (profile, impact, resp):
            self.run_renderer(module)
            self.assertGreater(sum(n for (renderer, _, _), n in self.api.counts.items()
                                   if renderer == module.__name__), 0)
        self.assertIn((resp.__name__, "identity", None), self.api.counts)
        self.assertIn((resp.__name__, "prs", None), self.api.counts)

    def test_cold_unit_pages_prs_once_and_shares_the_complete_set(self):
        for module in (profile, impact, resp):
            self.run_renderer(module)
        self.assertEqual(self.api.counts[profile.__name__, "prs", None], 1)
        self.assertEqual(self.api.counts[profile.__name__, "prs", "pr-next"], 1)
        self.assertEqual(sum(n for (renderer, kind, _), n in self.api.counts.items()
                             if renderer in (impact.__name__, resp.__name__)
                             and kind in ("prs", "sweep", "identity", "issues")), 0)
        for path in (self.profile_cache, self.impact_cache):
            self.assertEqual(set(json.loads(path.read_text())["prs"]),
                             {"live", "org", "private", "extra"})

    def test_timestamp_repair_is_shared_without_another_acquisition(self):
        cached = test_render.complete_cache()
        cached["prs"] = {"repair": item("repair", merged=False)}
        profile.save_cache(self.profile_cache, cached)
        self.api.archived = [item("repair")]
        for module in (profile, impact, resp):
            self.run_renderer(module)
        self.assertEqual(self.api.counts[profile.__name__, "sweep", None], 1)
        self.assertEqual(self.api.counts[profile.__name__, "prs", None], 2)
        for path in (self.profile_cache, self.impact_cache):
            node = json.loads(path.read_text())["prs"]["repair"]
            self.assertTrue(node["merged"])
            self.assertEqual(node["mergedAt"], "2026-01-02T00:00:00Z")

    def test_invalid_shares_fetch_live_and_name_the_failed_acquisition(self):
        self.run_renderer(profile)
        path = self.root / "share" / "authored.json"
        valid = path.read_text()
        invalid = json.loads(valid)
        invalid["identity"]["databaseId"] = True
        for contents, reason in ((None, "artifact absent"),
                                 ('{"identity":', "bad share JSON"),
                                 (json.dumps(invalid), "validation failure")):
            for module in (impact, resp):
                with self.subTest(reason=reason, renderer=module.__name__):
                    if contents is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_text(contents)
                    self.api.counts.clear()
                    self.output.truncate(0)
                    self.output.seek(0)
                    self.run_renderer(module)
                    self.assertIn(reason, self.output.getvalue())
                    self.assertIn((module.__name__, "identity", None), self.api.counts)
                    self.assertIn("other/repo", (self.root / "out" / (
                        "impact.svg" if module is impact else "responsiveness.svg")).read_text())
                    # The share rejection must not mask the actual network failure.
                    with mock.patch.object(module, "fetch_pull_requests",
                                           side_effect=RuntimeError("offline")):
                        self.run_renderer(module)
                    self.assertIn("fetch failed at fetch_pull_requests: offline",
                                  self.output.getvalue())

    def test_failed_producer_invalidates_previous_share_and_keeps_timestamp(self):
        self.run_renderer(profile)
        cache_before = self.profile_cache.read_bytes()
        timestamp = json.loads(cache_before)["fetched_at"]
        with mock.patch.object(profile, "fetch_issues", side_effect=RuntimeError("offline")):
            self.run_renderer(profile)
        self.assertFalse((self.root / "share" / "authored.json").exists())
        self.assertEqual(self.profile_cache.read_bytes(), cache_before)
        self.assertIn("fetch failed at fetch_issues: offline", self.output.getvalue())
        self.assertEqual((self.root / "out" / "last-updated.txt").read_text(), timestamp)

    def test_shared_identity_uses_writer_environment_without_rederiving(self):
        self.run_renderer(profile)
        self.environment.update(GH_WIDGETS_GH_EXTRA_INSIDERS="",
                                GH_WIDGETS_GH_EXTRA_EMAILS="")
        with mock.patch.object(impact, "update_loc", return_value={}) as update:
            with mock.patch.dict(os.environ, self.environment), \
                    mock.patch("urllib.request.urlopen", self.api):
                insiders, *_rest = impact.fetch_all("t", "me", {}, False)
            self.assertIn("extra@example.org", update.call_args.args[-1])
        self.assertIn("added", insiders)
        self.assertIn("secondorg", insiders)
        self.run_renderer(resp)
        self.assertNotIn("Added/repo", (self.root / "out" / "responsiveness.svg").read_text())

    def test_both_units_opt_in_to_separate_share_directories(self):
        root = Path(__file__).resolve().parents[1]
        for unit in ("gh-widgets.service", "gh-widgets-resync.service"):
            self.assertIn("Environment=GH_WIDGETS_SHARE_DIR=/var/lib/gh-widgets/share/%n",
                          (root / "units" / unit).read_text().splitlines())


class ShareValidation(unittest.TestCase):
    """Reject corrupt identity and nested item inputs before a renderer sees them."""

    def setUp(self):
        self.payload = {
            "version": 1, "invocation_id": "run",
            "identity": {"login": "me", "databaseId": 42, "orgs": ["org"],
                         "insiders": ["me", "org"], "emails": ["me@example.org"]},
            "issues": [item("I1", issue=True)], "prs": [item("P1")],
        }

    def test_each_identity_field_is_validated(self):
        for field, values in {
                "login": ("", " ", None, 42), "databaseId": (True, 0, -1, "42", None),
                "orgs": ({}, [None], [""]), "insiders": (None, [1], [" "]),
                "emails": ("me@example.org", [False], [""])}.items():
            for value in values:
                payload = copy.deepcopy(self.payload)
                payload["identity"][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    share.validate(payload)
            payload = copy.deepcopy(self.payload)
            del payload["identity"][field]
            with self.subTest(missing=field), self.assertRaises(ValueError):
                share.validate(payload)

    def test_nested_inputs_and_schema_are_validated(self):
        corruptions = (
            (("version",), True), (("invocation_id",), None),
            (("identity", "unknown"), "extra"), (("issues",), {}), (("prs",), None),
            (("prs", 0, "id"), ""), (("prs", 0, "merged"), 1),
            (("issues", 0, "state"), "MERGED"), (("issues", 0, "stateReason"), 1),
            (("prs", 0, "createdAt"), "yesterday"), (("prs", 0, "number"), True),
            (("prs", 0, "repository", "isPrivate"), 0),
            (("prs", 0, "repository", "owner", "login"), None),
            (("prs", 0, "repository", "nameWithOwner"), ""),
        )
        for path, value in corruptions:
            payload = copy.deepcopy(self.payload)
            parent: dict = payload
            for key in path[:-1]:
                parent = parent[key]
            parent[path[-1]] = value
            with self.subTest(path=path), self.assertRaises(ValueError):
                share.validate(payload)
        self.payload["prs"] *= 2
        with self.assertRaises(ValueError):
            share.validate(self.payload)

    def test_account_and_unit_mismatches_fetch_live(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {"GH_WIDGETS_SHARE_DIR": directory,
                                             "INVOCATION_ID": "run"}), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            Path(directory, "authored.json").write_text(json.dumps(self.payload), encoding="utf-8")
            self.assertIsNone(share.load("someone-else"))
            self.payload["invocation_id"] = "previous-run"
            Path(directory, "authored.json").write_text(json.dumps(self.payload), encoding="utf-8")
            self.assertIsNone(share.load("me"))
            with mock.patch.object(share.Path, "read_text", side_effect=PermissionError("denied")):
                self.assertIsNone(share.load("me"))
            Path(directory, "authored.json").write_bytes(b"\xff")
            self.assertIsNone(share.load("me"))
        self.assertIn("account mismatch", output.getvalue())
        self.assertIn("unit invocation mismatch", output.getvalue())
        self.assertIn("artifact unreadable", output.getvalue())
        self.assertIn("bad share JSON", output.getvalue())

    def test_locked_atomic_write_and_invalid_input_preserve_previous_artifact(self):
        identity = profile.common.Identity("me", 42, ["org"], {"me", "org"}, {"email"})
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {"GH_WIDGETS_SHARE_DIR": directory,
                                             "INVOCATION_ID": "run"}):
            path = share.begin()
            assert path is not None
            share.write(path, identity, self.payload["issues"], self.payload["prs"])
            before = path.read_bytes()
            with mock.patch.object(share.ghwidgets_cache, "cache_lock") as lock:
                lock.return_value.__enter__.return_value = False
                with self.assertRaises(TimeoutError):
                    share.write(path, identity, [], [])
                with self.assertRaises(TimeoutError):
                    share.begin()
            with mock.patch.object(share.ghwidgets_cache.os, "replace",
                                   side_effect=OSError("disk full")), self.assertRaises(OSError):
                share.write(path, identity, [], [])
            with self.assertRaises(ValueError):
                share.write(path, identity._replace(database_id=0), [], [])
            self.assertEqual(path.read_bytes(), before)
            loaded = share.load("ME")
            assert loaded is not None
            self.assertEqual(loaded["prs"][0]["id"], "P1")


if __name__ == "__main__":
    unittest.main()
