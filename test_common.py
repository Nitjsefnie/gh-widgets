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
        self.merged_calls = 0

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
    def test_both_scripts_pin_the_current_version(self):
        # A partial copy to /usr/local/bin must fail loudly, which only works
        # if the scripts' REQUIRED_COMMON tracks COMMON_VERSION.
        here = Path(__file__).parent
        for name in ("render.py", "render-impact.py"):
            src = (here / name).read_text()
            self.assertIn(f"REQUIRED_COMMON = {common.COMMON_VERSION}", src,
                          f"{name} does not pin COMMON_VERSION")


if __name__ == "__main__":
    unittest.main()
