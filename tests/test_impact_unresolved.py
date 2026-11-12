"""A repository GitHub no longer resolves leaves the impact card; the rest renders fresh.

Stdlib only; the GraphQL client runs for real against a stub transport.
"""
import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests import _ghw_temp
from tests.test_share import CountingAPI, impact, item

common = impact.common
NOT_FOUND = {"type": "NOT_FOUND", "message": "Could not resolve to a Repository."}
LIVE = {"defaultBranchRef": {"name": "main", "target": {"oid": "h1"}},
        "issues": {"totalCount": 4}, "pullRequests": {"totalCount": 3}}


def reply(body):
    return contextlib.closing(io.BytesIO(json.dumps(body).encode()))


class GqlMissingAliases(unittest.TestCase):
    """Only a NOT_FOUND naming one whole null alias is tolerated, and only on request."""

    def run_gql(self, body, **kwargs):
        with mock.patch("urllib.request.urlopen", return_value=reply(body)), \
                contextlib.redirect_stdout(io.StringIO()):
            return common.gql("t", "query", retries=0, **kwargs)

    def test_a_null_alias_answered_not_found_is_tolerated_when_asked(self):
        body = {"data": {"a0": LIVE, "a1": None}, "errors": [{**NOT_FOUND, "path": ["a1"]}]}
        self.assertEqual(self.run_gql(body, allow_missing_aliases=True), body["data"])
        with self.assertRaises(RuntimeError):
            self.run_gql(body)

    def test_every_other_error_shape_still_raises(self):
        shapes = {
            "another type on an alias": ({"a1": None}, [{**NOT_FOUND, "type": "FORBIDDEN", "path": ["a1"]}]),
            "NOT_FOUND on a deeper path": ({"a1": None}, [{**NOT_FOUND, "path": ["a1", "issues"]}]),
            "tolerated mixed with untolerated": ({"a0": None, "a1": None},
                                                 [{**NOT_FOUND, "path": ["a1"]},
                                                  {**NOT_FOUND, "type": "INTERNAL", "path": ["a0"]}]),
            "NOT_FOUND on an alias with data": ({"a1": LIVE}, [{**NOT_FOUND, "path": ["a1"]}]),
            "NOT_FOUND on an alias absent from data": ({"a0": LIVE}, [{**NOT_FOUND, "path": ["a1"]}]),
            "NOT_FOUND without a path": ({"a1": None}, [NOT_FOUND]),
            "data null": (None, [{**NOT_FOUND, "path": ["a1"]}]),
        }
        for name, (data, errors) in shapes.items():
            with self.subTest(name), self.assertRaises(RuntimeError):
                self.run_gql({"data": data, "errors": errors}, allow_missing_aliases=True)

    def test_a_batch_with_an_unresolved_repo_returns_the_others_fresh(self):
        body = {"data": {"a0": LIVE, "a1": None, "a2": LIVE},
                "errors": [{**NOT_FOUND, "path": ["a1"]}]}
        with mock.patch("urllib.request.urlopen", return_value=reply(body)), \
                mock.patch.object(common, "timing_sleep"), \
                contextlib.redirect_stdout(io.StringIO()):
            totals = impact.fetch_repo_totals("t", ["a/live", "b/gone", "c/live"])
        fresh = {"issues": 4, "merged_prs": 3, "branch": "main", "head": "h1"}
        self.assertEqual(totals, {"a/live": fresh, "c/live": fresh})


class UnresolvedLoc(unittest.TestCase):
    def test_an_unresolved_repo_loses_its_count_and_an_empty_one_keeps_it(self):
        cached = {"gone/repo": {"ours": 5, "total": 9, "branch": "main", "head": "old"},
                  "empty/repo": {"ours": 2, "total": 4, "branch": "main", "head": "old"}}
        blamed = []
        ourloc = getattr(impact, "_LOC_MODULE").update_loc(
            ["gone/repo", "empty/repo"], {"empty/repo": {**cached["empty/repo"], "head": ""}},
            cached, False, set(), blame_fn=lambda moved, *_: blamed.extend(moved))
        self.assertEqual(ourloc, {"empty/repo": cached["empty/repo"]})
        self.assertEqual(blamed, [])


class VanishedRepo(CountingAPI):
    """Answers the totals batch NOT_FOUND for gone/repo."""

    def __call__(self, request, **kwargs):
        query = json.loads(request.data)["query"]
        if "a0: repository" not in query:
            return super().__call__(request, **kwargs)
        aliases = {f"a{j}": repo for j, repo in enumerate(sorted({"gone/repo", "live/repo"}))}
        self.counts["totals"] += 1
        return reply({"data": {a: None if r == "gone/repo" else LIVE for a, r in aliases.items()},
                      "errors": [{**NOT_FOUND, "path": [a]} for a, r in aliases.items() if r == "gone/repo"]})


class UnresolvedRepoRun(unittest.TestCase):
    """main(): the vanished repo is in no section, named once, and out of the written cache."""

    def test_the_card_renders_fresh_without_the_unresolved_repo(self):
        for resync in (False, True):
            with self.subTest(resync=resync):
                self.check_run(resync)

    def check_run(self, resync):
        api = VanishedRepo()
        api.nodes = [item("Pgone", "gone"), item("Plive", "live")]
        api.issues = [item("Igone", "gone", issue=True), item("Ilive", "live", issue=True)]
        loc = {"ours": 10, "total": 100, "branch": "main", "head": "h1"}
        with _ghw_temp.tempdir() as td:
            cache_file = Path(td) / "impact-cache.json"
            cache_file.write_text(json.dumps({
                "version": impact.CACHE_VERSION, "fetched_at": "2026-07-20T06:00:00+00:00",
                "insiders": ["me"], "prs": {n["id"]: n for n in api.nodes}, "issues": api.issues,
                "totals": {r: {"issues": 4, "merged_prs": 3, "branch": "main", "head": "h1"}
                           for r in ("gone/repo", "live/repo")},
                "ourloc": {"gone/repo": {**loc, "ours": 90}, "live/repo": loc}}), encoding="utf-8")
            argv = ["render-impact.py", "--user", "me", "--token", "t", "--out-dir", td,
                    "--cache-file", str(cache_file)] + (["--resync"] if resync else [])
            out = io.StringIO()
            blame = mock.Mock(side_effect=lambda moved, ourloc, _emails: ourloc.update({r: loc for r, _t in moved}))
            with mock.patch.dict(os.environ, {"GH_WIDGETS_SHARE_DIR": "", "GH_WIDGETS_IMPACT_COUNT_BOT_PRS": "1"}), \
                    mock.patch.object(sys, "argv", argv), mock.patch("urllib.request.urlopen", api), \
                    mock.patch.object(common, "timing_sleep"), mock.patch.object(impact, "blame_moved", blame), \
                    contextlib.redirect_stdout(out):
                impact.main()
            svg = (Path(td) / "impact.svg").read_text(encoding="utf-8")
            written = json.loads(cache_file.read_text(encoding="utf-8"))
        lines = out.getvalue().splitlines()
        self.assertEqual([line for line in lines if "did not resolve" in line],
                         ["repository gone/repo did not resolve; dropped from the PR, issue and LOC sections"])
        self.assertIn("(pr repos=1 issue repos=1 loc repos=1)", out.getvalue())
        self.assertFalse([line for line in lines if line.startswith("fetch failed")], lines)
        self.assertNotIn("gone/repo", svg)
        # One row per section plus the description naming the top live-code repo.
        self.assertEqual(svg.count("live/repo"), 4, svg)
        self.assertNotEqual(written["fetched_at"], "2026-07-20T06:00:00+00:00")
        self.assertEqual((set(written["totals"]), set(written["ourloc"])), ({"live/repo"}, {"live/repo"}))
        if resync:
            blame.assert_called_once()
            self.assertEqual([repo for repo, _t in blame.call_args.args[0]], ["live/repo"])


if __name__ == "__main__":
    unittest.main()
