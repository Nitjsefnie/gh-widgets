#!/usr/bin/env python3
"""Tests for ghwidgets_common.py. Stdlib only, like the thing it tests.

    python3 -m unittest discover -v

No network: the identity test drives a fake gql.
"""
import importlib.util
import json
import os
import tempfile
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
                                 "pageInfo": {"hasNextPage": False,
                                              "endCursor": None},
                                 "nodes": [{"login": o, "isPublic": True}
                                           for o in orgs]}}}
        return _gql

    def test_derives_insiders_and_emails(self):
        me = common.fetch_identity("t", "octocat", gql_fn=self.fake_gql(),
                                   include_public_orgs=True)
        self.assertEqual(me.login, "Octocat")
        self.assertEqual(me.database_id, 42)
        self.assertEqual(me.orgs, ["OrgOne"])
        self.assertEqual(me.public_orgs, ["OrgOne"])
        self.assertEqual(me.insiders, {"octocat", "orgone"})
        self.assertIn("42+octocat@users.noreply.github.com", me.emails)

    def test_pages_all_organizations_and_keeps_private_membership_transient(self):
        calls = []
        pages = {
            None: {"pageInfo": {"hasNextPage": True, "endCursor": "org-c1"},
                   "nodes": [{"login": "PublicOne", "isPublic": True},
                             {"login": "PrivateOne", "isPublic": False}]},
            "org-c1": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{"login": "PublicTwo", "isPublic": True}],
            },
        }

        def gql_fn(token, query, variables=None, **kw):
            self.assertIsNotNone(variables)
            assert variables is not None
            calls.append(variables["cursor"])
            return {"user": {"login": "Octocat", "databaseId": 42,
                             "organizations": pages[variables["cursor"]]}}

        me = common.fetch_identity("t", "octocat", gql_fn=gql_fn,
                                   include_environment=False,
                                   include_public_orgs=True)
        self.assertEqual(calls, [None, "org-c1"])
        self.assertEqual(me.orgs, ["PublicOne", "PrivateOne", "PublicTwo"])
        self.assertEqual(me.public_orgs, ["PublicOne", "PublicTwo"])
        # Private knowledge remains available to external classification but
        # is a separate field from the serialized public membership list.
        self.assertIn("privateone", me.insiders)

    def test_missing_organization_cursor_fails_explicitly(self):
        def gql_fn(token, query, variables=None, **kw):
            return {"user": {"login": "Octocat", "databaseId": 42,
                             "organizations": {
                                 "pageInfo": {"hasNextPage": True,
                                              "endCursor": None},
                                 "nodes": [],
                             }}}

        with self.assertRaises(common.PaginationLimitError) as raised:
            common.fetch_identity("t", "octocat", gql_fn=gql_fn)
        self.assertIn("organizations", str(raised.exception))
        self.assertIn("None", str(raised.exception))

    def test_repeated_organization_cursor_fails_explicitly(self):
        calls = []

        def gql_fn(token, query, variables=None, **kw):
            self.assertIsNotNone(variables)
            assert variables is not None
            calls.append(variables["cursor"])
            return {"user": {"login": "Octocat", "databaseId": 42,
                             "organizations": {
                                 "pageInfo": {"hasNextPage": True,
                                              "endCursor": "same"},
                                 "nodes": [],
                             }}}

        with self.assertRaises(common.PaginationLimitError) as raised:
            common.fetch_identity("t", "octocat", gql_fn=gql_fn)
        self.assertEqual(calls, [None, "same"])
        self.assertIn("organizations", str(raised.exception))
        self.assertIn("same", str(raised.exception))

    def test_custom_transport_without_visibility_fails_closed_for_public_orgs(self):
        def gql_fn(token, query, variables=None, **kw):
            return {"user": {"login": "Octocat", "databaseId": 42,
                             "organizations": {
                                 "pageInfo": {"hasNextPage": False,
                                              "endCursor": None},
                                 "nodes": [{"login": "Concealed"}],
                             }}}

        me = common.fetch_identity("t", "octocat", gql_fn=gql_fn,
                                   include_environment=False,
                                   include_public_orgs=True)
        self.assertEqual(me.orgs, ["Concealed"])
        self.assertEqual(me.public_orgs, [])

    def test_missing_page_info_fails_even_for_a_short_organization_page(self):
        def gql_fn(token, query, variables=None, **kw):
            return {"user": {"login": "Octocat", "databaseId": 42,
                             "organizations": {
                                 "nodes": [{"login": "OrgOne"}],
                             }}}

        with self.assertRaises(common.PaginationLimitError) as raised:
            common.fetch_identity("t", "octocat", gql_fn=gql_fn)
        self.assertIn("organizations", str(raised.exception))
        self.assertIn("pageInfo", str(raised.exception))

    def test_ordinary_identity_fetch_does_not_acquire_public_memberships(self):
        payload = {"user": {"login": "Octocat", "databaseId": 42,
                            "organizations": {
                                "pageInfo": {"hasNextPage": False,
                                             "endCursor": None},
                                "nodes": [{"login": "PrivateOrg"}],
                            }}}
        with mock.patch.object(common, "gql", return_value=payload), \
                mock.patch.object(
                    common, "fetch_public_organizations",
                    side_effect=AssertionError("public acquisition is opt-in")):
            me = common.fetch_identity("t", "octocat")
        self.assertEqual(me.orgs, ["PrivateOrg"])
        self.assertEqual(me.public_orgs, [])

    def test_legacy_five_argument_identity_defaults_to_empty_public_orgs(self):
        me = common.Identity(
            "Octocat", 42, ["PrivateOrg"], {"octocat", "privateorg"},
            {"octocat@users.noreply.github.com"})
        self.assertEqual(me.public_orgs, [])

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


class FetchPublicOrganizations(unittest.TestCase):
    class Response:
        def __init__(self, body, link=""):
            self.body = body
            self.headers = {"Link": link}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(self.body).encode()

    def test_public_endpoint_is_paginated_without_authentication(self):
        calls = []

        def request_fn(request, timeout=None):
            calls.append(request)
            if len(calls) == 1:
                return self.Response(
                    [{"login": "PublicOne"}],
                    '<https://api.github.com/users/Octocat/orgs?page=2>; '
                    'rel="next"')
            return self.Response([{"login": "PublicTwo"}])

        orgs = common.fetch_public_organizations(
            "Octocat", request_fn=request_fn)
        self.assertEqual(orgs, ["PublicOne", "PublicTwo"])
        self.assertEqual(len(calls), 2)
        self.assertFalse(calls[0].has_header("Authorization"))


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

    def test_invalid_inferred_merge_triggers_full_resync(self):
        """A stale OPEN snapshot must be replaced by the full PR rebuild."""
        cached = {
            "PR_open": {
                "id": "PR_open", "merged": False, "state": "OPEN",
                "mergedAt": None, "closedAt": None,
            }
        }
        authoritative = {
            **cached["PR_open"], "merged": True, "state": "MERGED",
            "mergedAt": "2026-08-18T19:27:50Z",
            "closedAt": "2026-08-18T19:27:50Z",
        }
        calls = []

        def gql_fn(_token, query, variables=None, **_kwargs):
            calls.append(query)
            if "states: [OPEN, CLOSED, MERGED]" in query:
                nodes = [authoritative]
            else:
                nodes = []
            return {"user": {"pullRequests": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": nodes,
            }}}

        _prs, by_id = common.fetch_pull_requests(
            "t", "me", cached_prs=cached, gql_fn=gql_fn)

        self.assertEqual(
            sum("states: [OPEN, CLOSED, MERGED]" in q for q in calls), 1,
            "invalid inferred merge must invoke the full-resync query")
        self.assertEqual(by_id["PR_open"], authoritative)

    def test_persisted_incomplete_merge_resyncs_after_recent_sweep(self):
        """A sweep hit cannot substitute for the required full rebuild."""
        cached = {
            "PR_open": {
                "id": "PR_open", "merged": True, "state": "OPEN",
                "mergedAt": None, "closedAt": None,
            }
        }
        sweep_node = {
            **cached["PR_open"], "state": "MERGED",
            "mergedAt": "2026-08-02T00:00:00Z",
            "closedAt": "2026-08-02T00:00:00Z",
        }
        authoritative = {
            **sweep_node,
            "mergedAt": "2026-08-03T00:00:00Z",
            "closedAt": "2026-08-03T00:00:00Z",
        }
        calls = []

        def gql_fn(_token, query, variables=None, **_kwargs):
            calls.append(query)
            if "states: [OPEN, CLOSED, MERGED]" in query:
                nodes = [authoritative]
            elif "states: [MERGED]" in query:
                nodes = [sweep_node]
            else:
                nodes = []
            return {"user": {"pullRequests": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": nodes,
            }}}

        _prs, by_id = common.fetch_pull_requests(
            "t", "me", cached_prs=cached, gql_fn=gql_fn)

        self.assertEqual(
            sum("states: [OPEN, CLOSED, MERGED]" in q for q in calls), 1,
            "persisted incomplete merge must force the full-resync query")
        self.assertEqual(by_id["PR_open"], authoritative)

    def test_cold_cache_runs_no_merged_sweep(self):
        # On a cold cache / --resync the [OPEN, CLOSED, MERGED] rebuild
        # already covers everything; sweeping again would double-fetch.
        g = self.fake_gql(live_nodes=[], merged_nodes=[{"id": "PR_m"}])
        common.fetch_pull_requests("t", "me", cached_prs=None, gql_fn=g)
        self.assertEqual(self.merged_calls, 0)

    def test_no_limit_pages_until_has_next_page(self):
        pages = iter([
            {"pageInfo": {"hasNextPage": True, "endCursor": "c1"},
             "nodes": [{"id": "PR_1", "merged": False}]},
            {"pageInfo": {"hasNextPage": False, "endCursor": None},
             "nodes": [{"id": "PR_2", "merged": True}]},
        ])

        def gql_fn(*_args, **_kwargs):
            return {"user": {"pullRequests": next(pages)}}

        prs, by_id = common.fetch_pull_requests(
            "t", "me", max_pages=None, gql_fn=gql_fn)
        self.assertEqual([node["id"] for node in prs], ["PR_1", "PR_2"])
        self.assertEqual(set(by_id), {"PR_1", "PR_2"})

    def test_explicit_limit_raises_instead_of_returning_partial(self):
        def gql_fn(*_args, **_kwargs):
            return {"user": {"pullRequests": {
                "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                "nodes": [{"id": "PR_1", "merged": False}],
            }}}

        with self.assertRaises(common.PaginationLimitError) as raised:
            common.fetch_pull_requests("t", "me", max_pages=1,
                                       gql_fn=gql_fn)
        self.assertIn("pullRequests", str(raised.exception))
        self.assertIn("c1", str(raised.exception))

    def test_missing_next_cursor_raises_without_limit(self):
        def gql_fn(*_args, **_kwargs):
            return {"user": {"pullRequests": {
                "pageInfo": {"hasNextPage": True, "endCursor": None},
                "nodes": [],
            }}}

        with self.assertRaises(common.PaginationLimitError) as raised:
            common.fetch_pull_requests("t", "me", max_pages=None,
                                       gql_fn=gql_fn)
        self.assertIn("pullRequests", str(raised.exception))
        self.assertIn("None", str(raised.exception))

    def test_repeated_next_cursor_raises_without_limit(self):
        calls = []

        def gql_fn(token, query, variables=None, **_kwargs):
            self.assertIsNotNone(variables)
            assert variables is not None
            calls.append(variables["cursor"])
            return {"user": {"pullRequests": {
                "pageInfo": {"hasNextPage": True, "endCursor": "same"},
                "nodes": [],
            }}}

        with self.assertRaises(common.PaginationLimitError) as raised:
            common.fetch_pull_requests("t", "me", max_pages=None,
                                       gql_fn=gql_fn)
        self.assertEqual(calls, [None, "same"])
        self.assertIn("pullRequests", str(raised.exception))
        self.assertIn("same", str(raised.exception))


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


class CacheWriting(unittest.TestCase):
    """Two scripts write the impact cache; the lock is what keeps the cheap
    hourly writer from reverting the expensive twice-daily one."""

    def setUp(self):
        # pylint: disable=consider-using-with
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.path = Path(self.td.name) / "impact-cache.json"

    def write(self, payload):
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    def read(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_merge_replaces_only_the_listed_keys(self):
        self.write({"version": 1, "prs": {"old": 1},
                    "ourloc": {"a/b": {"ours": 7}}})
        common.merge_cache(self.path, 1, {"prs": {"new": 2}})
        after = self.read()
        self.assertEqual(after["prs"], {"new": 2})
        self.assertEqual(after["ourloc"], {"a/b": {"ours": 7}})

    def test_merge_stamps_the_schema_version(self):
        common.merge_cache(self.path, 1, {"prs": {}})
        self.assertEqual(self.read()["version"], 1)

    def test_merge_of_a_version_mismatch_keeps_nothing(self):
        # load_cache already refuses a foreign schema; carrying its keys into
        # the new payload would mix two layouts in one file.
        self.write({"version": 99, "ourloc": {"a/b": {"ours": 7}}})
        common.merge_cache(self.path, 1, {"prs": {}})
        self.assertNotIn("ourloc", self.read())

    def test_a_held_lock_stops_a_merge_rather_than_racing_it(self):
        # The read-modify-write writer must never proceed unlocked: a
        # whole-file save landing between its read and its write would be
        # silently reverted, ourloc included.
        self.write({"version": 1, "ourloc": {"a/b": {"ours": 7}}})
        with common.cache_lock(self.path) as held:
            self.assertTrue(held)
            self.assertIsNone(
                common.merge_cache(self.path, 1, {"prs": {}}, timeout=0.1))
        self.assertNotIn("prs", self.read())

    def test_a_held_lock_does_not_stop_a_whole_file_save(self):
        # Deliberate asymmetry: save_cache's caller owns every key it writes,
        # so the worst it can drop is a PR refresh the next hourly run redoes.
        with common.cache_lock(self.path):
            common.save_cache(self.path, {"version": 1, "prs": {}},
                              timeout=0.1)
        self.assertEqual(self.read()["prs"], {})

    def test_the_lock_is_released_when_the_block_ends(self):
        with common.cache_lock(self.path) as held:
            self.assertTrue(held)
        with common.cache_lock(self.path, timeout=0.1) as held:
            self.assertTrue(held, "lock must not survive its context manager")

    def test_a_failed_write_leaves_neither_a_partial_cache_nor_a_temp_file(self):
        self.write({"version": 1, "prs": {"old": 1}})
        before = self.path.read_bytes()
        with mock.patch.object(common.os, "replace",
                               side_effect=OSError("no space left")):
            common.save_cache(self.path, {"version": 1, "prs": {"new": 2}})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(
            [p.name for p in Path(self.td.name).glob("*.tmp")], [])

    def test_strict_save_raises_when_write_fails(self):
        with mock.patch.object(common.os, "replace",
                               side_effect=OSError("no space left")):
            with self.assertRaises(OSError):
                common.save_cache(self.path, {"version": 1}, strict=True)

    def test_strict_save_raises_when_lock_is_unavailable(self):
        with common.cache_lock(self.path):
            with self.assertRaises(TimeoutError):
                common.save_cache(self.path, {"version": 1}, timeout=0.1,
                                  strict=True)


class CacheShape(unittest.TestCase):
    """Impact cache maps reject structural field drift without rejecting
    empty maps."""

    @staticmethod
    def total_entry(**overrides):
        entry = {"issues": 4, "merged_prs": 2,
                 "branch": "main", "head": "abc123"}
        entry.update(overrides)
        return entry

    @staticmethod
    def loc_entry(**overrides):
        entry = {"ours": 2, "total": 10,
                 "branch": "main", "head": "abc123"}
        entry.update(overrides)
        return entry

    @classmethod
    def cache(cls, **overrides):
        payload = {
            "version": 1,
            "totals": {},
            "ourloc": {},
        }
        payload.update(overrides)
        return payload

    @staticmethod
    def load(payload):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "impact-cache.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            loaded = common.load_cache(path, 1)
            common.validate_cache_shape(loaded)
            return loaded

    def test_populated_totals_missing_merged_prs_fails_loudly(self):
        payload = self.cache(
            totals={"outside/project": self.total_entry(
                merged_prs=None)})
        del payload["totals"]["outside/project"]["merged_prs"]

        with self.assertRaisesRegex(ValueError, r"totals.*merged_prs"):
            self.load(payload)

    def test_repository_absent_from_empty_totals_still_works(self):
        loaded = self.load(self.cache())

        self.assertEqual(loaded["totals"], {})

    def test_ourloc_entry_with_ours_missing_total_fails_loudly(self):
        payload = self.cache(
            ourloc={"outside/project": self.loc_entry()})
        del payload["ourloc"]["outside/project"]["total"]

        with self.assertRaisesRegex(ValueError, r"ourloc.*total"):
            self.load(payload)

    def test_ourloc_entry_missing_ours_keeps_filtering(self):
        payload = self.cache(
            ourloc={"outside/project": self.loc_entry(ours=None)})
        del payload["ourloc"]["outside/project"]["ours"]

        loaded = self.load(payload)

        self.assertNotIn("ours", loaded["ourloc"]["outside/project"])


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
    # data-loss bug (#3) in production while the suite stayed green. Same paging
    # shape, same injectable gql_fn.

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

    def test_explicit_limit_raises_instead_of_returning_partial(self):
        # A server that never says hasNextPage: false must not return a
        # partial snapshot when an explicit safety limit is reached.
        endless = [
            {"pageInfo": {"hasNextPage": True, "endCursor": "c1"},
             "nodes": [{"id": "I"}]},
            {"pageInfo": {"hasNextPage": True, "endCursor": "c2"},
             "nodes": [{"id": "I"}]},
            {"pageInfo": {"hasNextPage": True, "endCursor": "c3"},
             "nodes": [{"id": "I"}]},
        ]
        with self.assertRaises(common.PaginationLimitError) as raised:
            common.fetch_issues("t", "me", max_pages=3,
                                gql_fn=self.paged_gql(endless))
        self.assertEqual(len(self.calls), 3)
        self.assertIn("issues", str(raised.exception))
        self.assertIn("c3", str(raised.exception))

    def test_no_limit_pages_until_has_next_page(self):
        pages = iter([
            {"pageInfo": {"hasNextPage": True, "endCursor": "c1"},
             "nodes": [{"id": "I1"}]},
            {"pageInfo": {"hasNextPage": False, "endCursor": None},
             "nodes": [{"id": "I2"}]},
        ])
        nodes = common.fetch_issues(
            "t", "me", max_pages=None,
            gql_fn=lambda *_args, **_kwargs: {
                "user": {"issues": next(pages)}})
        self.assertEqual([node["id"] for node in nodes], ["I1", "I2"])

    def test_missing_next_cursor_raises_without_limit(self):
        def gql_fn(*_args, **_kwargs):
            return {"user": {"issues": {
                "pageInfo": {"hasNextPage": True, "endCursor": None},
                "nodes": [],
            }}}

        with self.assertRaises(common.PaginationLimitError) as raised:
            common.fetch_issues("t", "me", max_pages=None, gql_fn=gql_fn)
        self.assertIn("issues", str(raised.exception))
        self.assertIn("None", str(raised.exception))

    def test_repeated_next_cursor_raises_without_limit(self):
        calls = []

        def gql_fn(token, query, variables=None, **_kwargs):
            self.assertIsNotNone(variables)
            assert variables is not None
            calls.append(variables["cursor"])
            return {"user": {"issues": {
                "pageInfo": {"hasNextPage": True, "endCursor": "same"},
                "nodes": [],
            }}}

        with self.assertRaises(common.PaginationLimitError) as raised:
            common.fetch_issues("t", "me", max_pages=None, gql_fn=gql_fn)
        self.assertEqual(calls, [None, "same"])
        self.assertIn("issues", str(raised.exception))
        self.assertIn("same", str(raised.exception))


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
