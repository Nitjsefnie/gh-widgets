#!/usr/bin/env python3
"""Tests for ghwidgets_common.py. Stdlib only, like the thing it tests.

    python3 -m unittest discover -v

No network: the identity test drives a fake gql.
"""
import importlib.util
import os
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "ghwidgets_common", Path(__file__).with_name("ghwidgets_common.py"))
if spec is None or spec.loader is None:
    raise SystemExit("error: cannot load ghwidgets_common.py")
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)


def repo(owner, name="r", private=False):
    return {"repository": {"nameWithOwner": f"{owner}/{name}",
                           "isPrivate": private,
                           "owner": {"login": owner}}}


class InsiderSet(unittest.TestCase):
    def test_login_and_orgs_are_insiders(self):
        s = common.insider_set("Me", ["MyOrg", "Other"], extra=[])
        self.assertEqual(s, {"me", "myorg", "other"})

    def test_casefolded(self):
        s = common.insider_set("ME", ["MYORG"], extra=[])
        self.assertIn("me", s)
        self.assertIn("myorg", s)

    def test_extra_is_additive_not_a_replacement(self):
        s = common.insider_set("Me", ["MyOrg"], extra=["Vendor"])
        self.assertEqual(s, {"me", "myorg", "vendor"})

    def test_extra_cannot_remove_a_fetched_org(self):
        # There is deliberately no way to drop MyOrg via configuration.
        s = common.insider_set("Me", ["MyOrg"], extra=["Vendor"])
        self.assertIn("myorg", s)

    def test_reads_env_when_extra_omitted(self):
        with mock.patch.dict(os.environ, {"GH_EXTRA_INSIDERS": "a, b ,"}):
            self.assertEqual(common.insider_set("Me", []), {"me", "a", "b"})

    def test_empty_names_are_dropped(self):
        self.assertEqual(common.insider_set("Me", ["", None], extra=[]), {"me"})


class IsExternal(unittest.TestCase):
    def setUp(self):
        self.insiders = common.insider_set("Me", ["MyOrg"], extra=[])

    def test_outsider_repo_is_external(self):
        self.assertTrue(common.is_external(repo("stranger"), self.insiders))

    def test_own_repo_is_not(self):
        self.assertFalse(common.is_external(repo("Me"), self.insiders))

    def test_org_repo_is_not(self):
        self.assertFalse(common.is_external(repo("myorg"), self.insiders))

    def test_private_is_never_external(self):
        self.assertFalse(
            common.is_external(repo("stranger", private=True), self.insiders))


class OurEmails(unittest.TestCase):
    def test_both_noreply_forms_are_derived(self):
        e = common.our_emails("Octocat", 12345, extra=[])
        self.assertIn("12345+octocat@users.noreply.github.com", e)
        self.assertIn("octocat@users.noreply.github.com", e)

    def test_missing_database_id_still_yields_the_bare_form(self):
        e = common.our_emails("Octocat", None, extra=[])
        self.assertEqual(e, {"octocat@users.noreply.github.com"})

    def test_extra_addresses_are_lowercased(self):
        e = common.our_emails("Octocat", 1, extra=["Me@Example.COM"])
        self.assertIn("me@example.com", e)

    def test_reads_env_when_extra_omitted(self):
        with mock.patch.dict(os.environ, {"GH_EXTRA_EMAILS": "x@y.z"}):
            self.assertIn("x@y.z", common.our_emails("Octocat", 1))

    def test_lookalike_address_is_NOT_ours(self):
        # The bug this replaced: a substring test counted any address
        # containing the login. Commit-author email is attacker-controllable
        # in a third-party repo, so this must be an exact match.
        e = common.our_emails("Octocat", 12345, extra=[])
        self.assertNotIn("octocat-fan@evil.example", e)
        self.assertNotIn("notoctocat@users.noreply.github.com", e)
        self.assertNotIn("99999+octocat@users.noreply.github.com", e)


class FetchIdentity(unittest.TestCase):
    def fake_gql(self, database_id=42, orgs=("OrgOne",), login="Octocat"):
        def _gql(token, query, variables=None, **kw):
            return {"user": {"login": login, "databaseId": database_id,
                             "organizations": {
                                 "nodes": [{"login": o} for o in orgs]}}}
        return _gql

    def test_derives_insiders_and_emails(self):
        me = common.fetch_identity("t", "octocat", gql_fn=self.fake_gql())
        self.assertEqual(me.login, "Octocat")
        self.assertEqual(me.database_id, 42)
        self.assertEqual(me.orgs, ["OrgOne"])
        self.assertEqual(me.insiders, {"octocat", "orgone"})
        self.assertIn("42+octocat@users.noreply.github.com", me.emails)

    def test_uses_the_canonical_login_not_the_argument(self):
        # Queried as "OCTOCAT", GitHub answers "Octocat"; the noreply address
        # must be built from GitHub's spelling.
        me = common.fetch_identity("t", "OCTOCAT", gql_fn=self.fake_gql())
        self.assertIn("42+octocat@users.noreply.github.com", me.emails)

    def test_unknown_user_raises(self):
        def _gql(token, query, variables=None, **kw):
            return {"user": None}
        with self.assertRaises(RuntimeError):
            common.fetch_identity("t", "nobody", gql_fn=_gql)

    def test_query_does_not_request_email(self):
        # The production token lacks read:user. A field-level scope failure
        # rejects the ENTIRE query, so asking for email breaks identity
        # resolution outright.
        self.assertNotIn("email", common.IDENTITY_QUERY)


class FetchPullRequests(unittest.TestCase):
    def fake_gql(self, live_nodes, merged_nodes):
        # Serves the [OPEN, CLOSED] live query and the [MERGED] sweep from
        # two canned single pages, counting sweep invocations. The cold-cache
        # [OPEN, CLOSED, MERGED] query matches neither branch on purpose:
        # "[MERGED]" is not a substring of "[OPEN, CLOSED, MERGED]".
        # Deliberately set outside __init__: fixture state owned by the fake
        # factory, mirroring the FetchPullRequests style (triaged, see #5).
        self.merged_calls = 0  # pylint: disable=attribute-defined-outside-init

        def _gql(token, query, variables=None, **kw):
            if "[MERGED]" in query:
                self.merged_calls += 1
                nodes = merged_nodes
            else:
                nodes = live_nodes
            return {"user": {"pullRequests": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": nodes}}}
        return _gql

    def test_warm_cache_discovers_pr_seen_only_in_merged_sweep(self):
        # Issue #3: a PR opened and merged between two renders never enters
        # the [OPEN, CLOSED] live set, so without the sweep it is lost.
        sweep_only = {"id": "PR_sweep", "merged": True,
                      **repo("stranger", "blazedb")}
        g = self.fake_gql(live_nodes=[], merged_nodes=[sweep_only])
        prs, by_id = common.fetch_pull_requests("t", "me", cached_prs={},
                                                gql_fn=g)
        self.assertIn("PR_sweep", by_id)
        self.assertTrue(by_id["PR_sweep"]["merged"])
        self.assertIn(sweep_only, prs)
        self.assertEqual(self.merged_calls, 1,
                         "merged sweep must be a single page, not paginated")

    def test_cold_cache_runs_no_merged_sweep(self):
        # On a cold cache / --resync the [OPEN, CLOSED, MERGED] rebuild
        # already covers everything; sweeping again would double-fetch.
        g = self.fake_gql(live_nodes=[], merged_nodes=[{"id": "PR_m"}])
        common.fetch_pull_requests("t", "me", cached_prs=None, gql_fn=g)
        self.assertEqual(self.merged_calls, 0)


class EnvKnobs(unittest.TestCase):
    def test_default_when_unset(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(common.env_float("IMPACT_Z", 2.58), 2.58)

    def test_blank_is_treated_as_unset(self):
        with mock.patch.dict(os.environ, {"IMPACT_Z": "   "}):
            self.assertEqual(common.env_float("IMPACT_Z", 2.58), 2.58)

    def test_override_is_read(self):
        with mock.patch.dict(os.environ, {"IMPACT_Z": "1.96"}):
            self.assertEqual(common.env_float("IMPACT_Z", 2.58), 1.96)

    def test_unparseable_exits_rather_than_silently_defaulting(self):
        with mock.patch.dict(os.environ, {"IMPACT_Z": "2,58"}):
            with self.assertRaises(SystemExit):
                common.env_float("IMPACT_Z", 2.58)

    def test_env_list_trims_and_drops_empties(self):
        with mock.patch.dict(os.environ, {"X": " a , ,b,"}):
            self.assertEqual(common.env_list("X"), ["a", "b"])


class VersionContract(unittest.TestCase):
    def test_every_script_pins_the_current_version(self):
        # A partial copy to /usr/local/bin must fail loudly, which only works
        # if the scripts' REQUIRED_COMMON tracks COMMON_VERSION.
        here = Path(__file__).parent
        for name in ("render.py", "render-impact.py",
                     "render-responsiveness.py"):
            src = (here / name).read_text()
            self.assertIn(f"REQUIRED_COMMON = {common.COMMON_VERSION}", src,
                          f"{name} does not pin COMMON_VERSION")


class FetchIssues(unittest.TestCase):
    # The untested twin of fetch_pull_requests, which harboured a permanent
    # data-loss bug (#3) while 58 tests passed. Same paging shape, same
    # injectable gql_fn.

    def paged_gql(self, pages):
        """Serve canned issue pages in order, recording every call."""
        # Deliberately set outside __init__: fixture state owned by the fake
        # factory, mirroring the FetchPullRequests style (triaged, see #5).
        self.calls = []  # pylint: disable=attribute-defined-outside-init

        def _gql(token, query, variables=None, **kw):
            self.calls.append(variables)
            page = pages[min(len(self.calls) - 1, len(pages) - 1)]
            return {"user": {"issues": page}}
        return _gql

    def test_paging_terminates_on_has_next_page_false(self):
        pages = [
            {"pageInfo": {"hasNextPage": True, "endCursor": "c1"},
             "nodes": [{"id": "I_1"}, {"id": "I_2"}]},
            {"pageInfo": {"hasNextPage": False, "endCursor": None},
             "nodes": [{"id": "I_3"}]},
        ]
        issues = common.fetch_issues("t", "me", gql_fn=self.paged_gql(pages))
        self.assertEqual([i["id"] for i in issues], ["I_1", "I_2", "I_3"])
        self.assertEqual(len(self.calls), 2,
                         "must stop at hasNextPage: false, not max_pages")

    def test_cursor_from_one_page_feeds_the_next(self):
        pages = [
            {"pageInfo": {"hasNextPage": True, "endCursor": "c1"},
             "nodes": []},
            {"pageInfo": {"hasNextPage": False, "endCursor": None},
             "nodes": []},
        ]
        common.fetch_issues("t", "me", gql_fn=self.paged_gql(pages))
        self.assertIsNone(self.calls[0]["cursor"],
                          "first page must start from no cursor")
        self.assertEqual(self.calls[1]["cursor"], "c1")

    def test_repages_every_run_rather_than_caching(self):
        # The docstring says issues are re-paged each run because they can be
        # REOPENED — there is no cached_* parameter and no module state that
        # lets a second run skip the fetch. Nothing checked that until now.
        page = [{"pageInfo": {"hasNextPage": False, "endCursor": None},
                 "nodes": [{"id": "I_1"}]}]
        g = self.paged_gql(page)
        first = common.fetch_issues("t", "me", gql_fn=g)
        second = common.fetch_issues("t", "me", gql_fn=g)
        self.assertEqual(len(self.calls), 2,
                         "second run must hit the API again, not a cache")
        self.assertEqual(first, second)

    def test_max_pages_is_a_runaway_guard(self):
        # A server that never says hasNextPage: false must not loop forever.
        endless = [{"pageInfo": {"hasNextPage": True, "endCursor": "c"},
                    "nodes": [{"id": "I"}]}]
        common.fetch_issues("t", "me", max_pages=3,
                            gql_fn=self.paged_gql(endless))
        self.assertEqual(len(self.calls), 3)


class XmlEscape(unittest.TestCase):
    # These values are interpolated into SVG; a repo name or title with
    # markup characters must not produce malformed XML.

    def test_all_five_specials_are_escaped(self):
        self.assertEqual(common.xml_escape('a&b<c>d"e\'f'),
                         "a&amp;b&lt;c&gt;d&quot;e&apos;f")

    def test_ampersand_is_escaped_first_not_doubled(self):
        # If & were escaped after the others, their entities would be
        # double-escaped ("&lt;" -> "&amp;lt;" applied twice).
        self.assertEqual(common.xml_escape("&lt;"), "&amp;lt;")

    def test_clean_text_is_unchanged(self):
        self.assertEqual(common.xml_escape("repo-name_1.2"), "repo-name_1.2")

    def test_non_string_input_is_stringified(self):
        self.assertEqual(common.xml_escape(42), "42")


class FmtShort(unittest.TestCase):
    # Shown on every card; a wrong magnitude renders a plausible-looking
    # wrong number that nobody notices.

    def test_below_a_thousand_is_plain(self):
        self.assertEqual(common.fmt_short(0), "0")
        self.assertEqual(common.fmt_short(999), "999")
        self.assertEqual(common.fmt_short(-999), "-999")

    def test_thousands_get_a_k_suffix(self):
        self.assertEqual(common.fmt_short(1234), "1.2k")
        self.assertEqual(common.fmt_short(1500), "1.5k")
        self.assertEqual(common.fmt_short(-1234), "-1.2k")

    def test_millions_get_an_m_suffix(self):
        self.assertEqual(common.fmt_short(2_500_000), "2.5M")

    def test_trailing_zero_is_dropped(self):
        self.assertEqual(common.fmt_short(1000), "1k")
        self.assertEqual(common.fmt_short(1_000_000), "1M")

    def test_just_under_a_million_rounds_up_to_1000k(self):
        # Rounding artifact locked in as documentation: 999_999 renders as
        # "1000k", not "1M" — the k/M boundary is checked before rounding.
        self.assertEqual(common.fmt_short(999_999), "1000k")

    def test_non_int_input_is_coerced(self):
        self.assertEqual(common.fmt_short("42"), "42")
        self.assertEqual(common.fmt_short(1234.0), "1.2k")


class BaseCard(unittest.TestCase):
    # The shared SVG chrome every card is built on; a breakage here breaks
    # every widget at once.
    C = common.THEMES["tokyonight"]

    def test_wraps_the_body_in_a_sized_svg(self):
        svg = common.base_card(self.C, 420, 180, "<text>hi</text>")
        self.assertTrue(svg.startswith(
            '<svg xmlns="http://www.w3.org/2000/svg" '
            'width="420" height="180" viewBox="0 0 420 180"'))
        self.assertIn("<text>hi</text>", svg)
        self.assertTrue(svg.rstrip().endswith("</svg>"))

    def test_theme_colors_are_applied(self):
        svg = common.base_card(self.C, 420, 180, "")
        self.assertIn(f'stop-color="{self.C["bg"]}"', svg)
        self.assertIn(f'stop-color="{self.C["bg2"]}"', svg)
        self.assertIn(f'stroke="{self.C["border"]}"', svg)

    def test_font_is_declared(self):
        self.assertIn(common.FONT, common.base_card(self.C, 420, 180, ""))


class NoreplyAddresses(unittest.TestCase):
    # A miss here silently under-counts contributions: the commit author is
    # matched exactly against this set.

    def test_both_forms_when_database_id_known(self):
        self.assertEqual(
            common.noreply_addresses("octocat", 75166987),
            {"octocat@users.noreply.github.com",
             "75166987+octocat@users.noreply.github.com"})

    def test_bare_form_only_when_database_id_missing(self):
        self.assertEqual(common.noreply_addresses("octocat", None),
                         {"octocat@users.noreply.github.com"})

    def test_addresses_are_lowercased(self):
        # GitHub logins are case-insensitive; email comparison here is exact,
        # so the set must be lowercase for the .lower() match to work.
        self.assertEqual(
            common.noreply_addresses("OctoCat", 75166987),
            {"octocat@users.noreply.github.com",
             "75166987+octocat@users.noreply.github.com"})


class StampCacheNotice(unittest.TestCase):
    # The stale-cache banner is the only signal a rendered card is not
    # fresh; a silent failure here means a card looks fresh when it is not.
    C = {"dim": "#8b949e"}

    def test_stale_card_gets_the_banner(self):
        svg = '<svg xmlns="x" width="400" height="120"></svg>'
        out = common.stamp_cache_notice(self.C, svg, "2026-07-28 09:00 UTC")
        self.assertIn("cached data from 2026-07-28 09:00 UTC", out)

    def test_banner_sits_above_the_bottom_edge(self):
        svg = '<svg xmlns="x" width="400" height="120"></svg>'
        out = common.stamp_cache_notice(self.C, svg, "now")
        self.assertIn('y="112"', out)  # height - 8

    def test_banner_lands_inside_the_svg_element(self):
        svg = '<svg xmlns="x" width="400" height="120"></svg>'
        out = common.stamp_cache_notice(self.C, svg, "now")
        self.assertLess(out.index("cached data from"), out.index("</svg>"),
                        "notice must render inside the closing tag")

    def test_fetched_at_is_escaped_for_svg(self):
        svg = '<svg height="60"></svg>'
        out = common.stamp_cache_notice(self.C, svg, 'a<b&"c')
        self.assertIn("a&lt;b&amp;&quot;c", out)
        self.assertNotIn('a<b&"c', out)

    def test_missing_height_still_stamps_visibly(self):
        out = common.stamp_cache_notice(self.C, "<svg></svg>", "now")
        self.assertIn('y="12"', out)
        self.assertIn("cached data from now", out)


if __name__ == "__main__":
    unittest.main()
