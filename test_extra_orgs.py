#!/usr/bin/env python3
"""Offline tests for GH_EXTRA_ORGS in render.py."""
import hashlib
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

import test_render as fixtures

render = fixtures.render
FakeAPI = fixtures.FakeAPI
complete_cache = fixtures.complete_cache
recent_days = fixtures.recent_days
repo_node = fixtures.repo_node
run_main_capturing_output = fixtures.run_main_capturing_output


class ExtraOrganizationStats(unittest.TestCase):
    C = render.THEMES["tokyonight"]
    SVG_NS = "http://www.w3.org/2000/svg"

    def fetched_user(self, api, orgs="", max_pages=render.PROFILE_MAX_PAGES):
        with mock.patch.object(render, "gql", api), \
                mock.patch.dict(os.environ, {"GH_EXTRA_ORGS": orgs}):
            user, _days = render.fetch("token", "me", {}, max_pages=max_pages)
        return user

    def stats_desc(self, svg):
        root = ET.fromstring(svg)
        desc = root.find(f"{{{self.SVG_NS}}}desc")
        if desc is None or desc.text is None:
            raise AssertionError("stats card is missing its accessible description")
        return desc.text

    def visible_stat_values(self, svg):
        root = ET.fromstring(svg)
        rows = {"115": "", "138": "", "161": ""}
        for text in root.iter(f"{{{self.SVG_NS}}}text"):
            y = text.get("y")
            if text.get("x") == "200" and y in rows:
                rows[y] = text.text or ""
        return rows["115"], rows["138"], rows["161"]

    def test_empty_default_preserves_stats_svg_bytes_and_skips_org_queries(self):
        with tempfile.TemporaryDirectory() as td:
            api = FakeAPI()
            api.full_calendar = recent_days(3)
            out = Path(td) / "out"
            stdout, stderr = run_main_capturing_output(
                api, out, Path(td) / "cache.json")
            svg = (out / "stats.svg").read_text(encoding="utf-8")

        self.assertIn("wrote ", stdout)
        self.assertEqual(stderr, "")
        self.assertFalse(any("organization(login:" in query
                             for query, _variables in api.calls))
        self.assertEqual(self.visible_stat_values(svg), ("1", "3", "2"))
        self.assertEqual(
            hashlib.sha256(svg.encode("utf-8")).hexdigest(),
            "2b03f88dc52a4c89a50c7f8964620c7156478befdf918fb1773918eb72547256")
        self.assertNotIn("organization followers", svg)

    def test_org_totals_add_to_stats_without_changing_languages(self):
        api = FakeAPI()
        api.full_calendar = recent_days(3)
        api.extra_org_pages["Example"] = [
            [repo_node(7, 3), repo_node(2, 1)], [repo_node(4, 2)]]
        api.extra_org_totals["Example"] = 3

        user = self.fetched_user(api, " Example ")
        svgs, summary = render.build_svgs(self.C, user, [], [])
        desc = self.stats_desc(svgs["stats.svg"])

        self.assertEqual(self.visible_stat_values(svgs["stats.svg"]),
                         ("4", "16", "8"))
        self.assertIn("public repositories: 4", desc)
        self.assertIn("stars received: 16", desc)
        self.assertIn("forks received: 8", desc)
        self.assertIn("Followers: 5", desc)
        self.assertIn("organization followers are not included", desc)
        self.assertEqual(summary[0:2], (16, 8))
        self.assertEqual(user["repositories"]["totalCount"], 1)
        self.assertEqual(len(user["repositories"]["nodes"]), 1)
        self.assertEqual(
            render.aggregate_languages(user["repositories"]["nodes"])[0][0],
            "Python")
        org_calls = [(query, variables) for query, variables in api.calls
                     if "organization(login:" in query]
        self.assertEqual([variables["cursor"] for _query, variables in org_calls],
                         [None, "cursor-1"])

    def test_org_query_is_public_nonfork_and_has_no_language_edge(self):
        api = FakeAPI()
        self.fetched_user(api, "Example")
        query, variables = next(
            (query, variables) for query, variables in api.calls
            if "organization(login:" in query)

        self.assertEqual(variables["login"], "Example")
        self.assertIn("isFork: false", query)
        self.assertIn("privacy: PUBLIC", query)
        self.assertIn("stargazerCount", query)
        self.assertIn("forkCount", query)
        self.assertNotIn("languages", query)

    def test_org_logins_are_trimmed_deduplicated_and_ordered(self):
        api = FakeAPI()
        user = self.fetched_user(api, " Acme, , acme, Beta ,BETA,  ")
        org_calls = [(query, variables) for query, variables in api.calls
                     if "organization(login:" in query]

        self.assertEqual([variables["login"] for _query, variables in org_calls],
                         ["Acme", "Beta"])
        self.assertEqual(user["extraOrgStats"]["orgs"], ["Acme", "Beta"])

    def test_page_limit_is_applied_separately_to_each_org(self):
        api = FakeAPI()
        api.extra_org_pages = {"First": [[repo_node(1, 1)]],
                               "Second": [[repo_node(2, 2)]]}

        user = self.fetched_user(api, "First,Second", max_pages=1)

        self.assertEqual(user["extraOrgStats"]["totalCount"], 2)

    def test_org_page_limit_raises_instead_of_truncating(self):
        api = FakeAPI()
        api.extra_org_pages["Example"] = [[repo_node()], [repo_node()]]
        with self.assertRaises(render.common.PaginationLimitError) as raised:
            self.fetched_user(api, "Example", max_pages=1)

        self.assertIn("Example", str(raised.exception))
        self.assertIn("pagination limit 1", str(raised.exception))

    def test_missing_page_info_or_has_next_page_raises(self):
        malformed = [
            {"nodes": [repo_node()]},
            {"pageInfo": {}, "nodes": [repo_node()]},
        ]
        for connection in malformed:
            with self.subTest(connection=connection):
                api = FakeAPI()
                api.extra_org_pages["Example"] = [connection]
                with self.assertRaises(render.common.PaginationLimitError):
                    self.fetched_user(api, "Example")

    def test_has_next_page_without_end_cursor_raises(self):
        api = FakeAPI()
        api.extra_org_pages["Example"] = [{
            "pageInfo": {"hasNextPage": True, "endCursor": None},
            "nodes": [repo_node()],
        }]
        with self.assertRaises(render.common.PaginationLimitError) as raised:
            self.fetched_user(api, "Example")

        self.assertIn("endCursor is missing", str(raised.exception))

    def test_repeated_end_cursor_raises(self):
        repeated_page = {
            "pageInfo": {"hasNextPage": True, "endCursor": "LOOP"},
            "nodes": [repo_node()],
        }
        api = FakeAPI()
        api.extra_org_pages["Example"] = [repeated_page, repeated_page]
        with self.assertRaises(render.common.PaginationLimitError) as raised:
            self.fetched_user(api, "Example")

        self.assertIn("LOOP", str(raised.exception))

    def test_missing_or_private_named_org_raises(self):
        api = FakeAPI()
        api.extra_org_errors.add("PrivateOrg")
        with self.assertRaisesRegex(RuntimeError, "PrivateOrg"):
            self.fetched_user(api, "PrivateOrg")

    def test_durability_fallback_uses_cached_org_stats(self):
        with tempfile.TemporaryDirectory() as td:
            cache_file = Path(td) / "cache.json"
            out = Path(td) / "out"
            cache = complete_cache()
            cache["user"]["extraOrgStats"] = {
                "orgs": ["Example"], "totalCount": 2,
                "totalStars": 7, "totalForks": 3,
            }
            render.save_cache(cache_file, cache)
            api = FakeAPI()
            api.fail = True

            stdout, stderr = run_main_capturing_output(
                api, out, cache_file, extra_orgs="Example")
            desc = self.stats_desc(
                (out / "stats.svg").read_text(encoding="utf-8"))

            self.assertIn("fetch failed at fetch: simulated fetch failure",
                          stdout)
            self.assertEqual(stderr, "")
            self.assertIn("public repositories: 3", desc)
            self.assertIn("stars received: 10", desc)
            self.assertIn("forks received: 5", desc)
            self.assertIn("organization followers are not included", desc)
