#!/usr/bin/env python3
"""Tests for the public GitHub data and snapshot contract."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ghwidgets_data as data


def issue_node(**overrides):
    node = {
        "id": "I_1",
        "number": 7,
        "url": "https://github/x/y/issues/7",
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-03T00:00:00Z",
        "closedAt": "2026-01-03T00:00:00Z",
        "state": "CLOSED",
        "stateReason": "NOT_PLANNED",
        "repository": {
            "id": "R_1",
            "nameWithOwner": "x/y",
            "url": "https://github/x/y",
            "isPrivate": False,
            "owner": {"login": "x"},
        },
    }
    node.update(overrides)
    return node


def pull_request_node(**overrides):
    node = issue_node(
        id="PR_1",
        url="https://github/x/y/pull/7",
        mergedAt="2026-01-04T00:00:00Z",
        merged=True,
    )
    node.update(overrides)
    return node


def snapshot(**overrides):
    value = {
        "schema_version": data.SCHEMA_VERSION,
        "generated_at": "2026-01-05T00:00:00+00:00",
        "account": {"login": "me"},
        "insiders": ["me", "org"],
        "repositories": [],
        "issues": [],
        "pull_requests": [],
    }
    value.update(overrides)
    return value


class Normalization(unittest.TestCase):
    def test_issue_keeps_current_final_state(self):
        out = data.normalise_issue({
            "id": "I_1", "number": 7, "url": "https://github/x/y/7",
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-03T00:00:00Z",
            "closedAt": "2026-01-03T00:00:00Z",
            "state": "CLOSED", "stateReason": "NOT_PLANNED",
            "repository": {"id": "R_1", "nameWithOwner": "x/y",
                           "url": "https://github/x/y", "isPrivate": False,
                           "owner": {"login": "x"}},
        })
        self.assertEqual(out["state_reason"], "NOT_PLANNED")
        self.assertEqual(out["repository_id"], "R_1")

    def test_issue_uses_normalized_public_keys(self):
        self.assertEqual(data.normalise_issue(issue_node()), {
            "node_id": "I_1",
            "repository_id": "R_1",
            "repository": "x/y",
            "owner": "x",
            "repository_url": "https://github/x/y",
            "is_private": False,
            "number": 7,
            "url": "https://github/x/y/issues/7",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-03T00:00:00Z",
            "closed_at": "2026-01-03T00:00:00Z",
            "state": "CLOSED",
            "state_reason": "NOT_PLANNED",
        })

    def test_pull_request_adds_merge_fields(self):
        out = data.normalise_pull_request(pull_request_node())
        self.assertEqual(out["node_id"], "PR_1")
        self.assertEqual(out["merged_at"], "2026-01-04T00:00:00Z")
        self.assertTrue(out["merged"])

    def test_private_flag_is_normalized_to_bool(self):
        out = data.normalise_issue(issue_node(
            repository={
                "id": "R_1", "nameWithOwner": "x/y",
                "url": "https://github/x/y", "isPrivate": 1,
                "owner": {"login": "x"},
            }))
        self.assertIs(out["is_private"], True)


class SnapshotBuilding(unittest.TestCase):
    def test_build_snapshot_sets_schema_and_sorts_insiders(self):
        out = data.build_snapshot(
            account={"login": "me"},
            insiders={"org", "me"},
            repositories=[{"id": "R_1"}],
            issues=[{"node_id": "I_1"}],
            pull_requests=[{"node_id": "PR_1"}],
            generated_at="2026-01-05T00:00:00+00:00",
        )
        self.assertEqual(out, {
            "schema_version": 1,
            "generated_at": "2026-01-05T00:00:00+00:00",
            "account": {"login": "me"},
            "insiders": ["me", "org"],
            "repositories": [{"id": "R_1"}],
            "issues": [{"node_id": "I_1"}],
            "pull_requests": [{"node_id": "PR_1"}],
        })

    def test_build_snapshot_defaults_to_utc_iso_seconds(self):
        out = data.build_snapshot(
            account={}, insiders=set(), repositories=[], issues=[],
            pull_requests=[])
        self.assertRegex(
            out["generated_at"],
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$",
        )

    def test_build_snapshot_preserves_explicit_generated_at(self):
        out = data.build_snapshot(
            account={}, insiders=set(), repositories=[], issues=[],
            pull_requests=[], generated_at="")
        self.assertEqual(out["generated_at"], "")


class AuthoredSnapshot(unittest.TestCase):
    def test_fetch_excludes_private_records_and_credentials(self):
        public_repo = {
            "id": "R_public",
            "nameWithOwner": "external/project",
            "url": "https://github.com/external/project",
            "isPrivate": False,
            "owner": {"login": "external"},
            "token": "repository-token-must-not-leak",
        }
        private_repo = {
            "id": "R_private",
            "nameWithOwner": "me/secret",
            "url": "https://github.com/me/secret",
            "isPrivate": True,
            "owner": {"login": "me"},
            "credential": "private-repository-credential",
        }
        public_repo_owned_by_private_member = {
            "id": "R_public_private_member",
            "nameWithOwner": "PrivateOrg/public-project",
            "url": "https://github.com/PrivateOrg/public-project",
            "isPrivate": False,
            "owner": {"login": "PrivateOrg"},
        }
        public_issue = issue_node(id="I_public", repository=public_repo)
        private_issue = issue_node(id="I_private", repository=private_repo)
        public_issue_owned_by_private_member = issue_node(
            id="I_public_private_member",
            repository=public_repo_owned_by_private_member)
        public_pr = pull_request_node(id="PR_public", repository=public_repo)
        private_pr = pull_request_node(id="PR_private", repository=private_repo)
        public_pr_owned_by_private_member = pull_request_node(
            id="PR_public_private_member",
            repository=public_repo_owned_by_private_member)
        calls = []

        def gql_fn(token, query, variables=None, **_kwargs):
            calls.append((token, query, variables))
            if "organizations" in query:
                return {"user": {
                    "login": "Canonical",
                    "databaseId": 42,
                    "organizations": {
                        "pageInfo": {"hasNextPage": False,
                                     "endCursor": None},
                        "nodes": [
                            {"login": "PublicOrg", "isPublic": True},
                            {"login": "PrivateOrg", "isPublic": False},
                        ],
                    },
                }}
            if "pullRequests" in query:
                return {"user": {"pullRequests": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [public_pr, private_pr,
                               public_pr_owned_by_private_member],
                }}}
            if "issues" in query:
                return {"user": {"issues": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [public_issue, private_issue,
                               public_issue_owned_by_private_member],
                }}}
            raise AssertionError("unexpected GraphQL query")

        # The high-level producer receives all inputs explicitly. It must not
        # pull additive insider/email configuration from the environment.
        with mock.patch.object(
                data.ghwidgets_common, "env_list",
                side_effect=AssertionError("environment must not be read")):
            out = data.fetch_authored_snapshot(
                "top-secret-token", "requested-login", gql_fn=gql_fn)

        self.assertEqual(out["account"], {"login": "Canonical"})
        self.assertEqual(out["insiders"], ["canonical", "publicorg"])
        self.assertNotIn("privateorg", out["insiders"])
        self.assertEqual([node["node_id"] for node in out["issues"]],
                         ["I_public", "I_public_private_member"])
        self.assertEqual([node["node_id"] for node in out["pull_requests"]],
                         ["PR_public", "PR_public_private_member"])
        self.assertEqual([repo["id"] for repo in out["repositories"]],
                         ["R_public", "R_public_private_member"])
        encoded = json.dumps(out, sort_keys=True)
        self.assertNotIn("top-secret-token", encoded)
        self.assertNotIn("repository-token-must-not-leak", encoded)
        self.assertNotIn("private-repository-credential", encoded)
        self.assertEqual([call[0] for call in calls],
                         ["top-secret-token"] * len(calls))

    def test_snapshot_producer_opts_into_public_membership_acquisition(self):
        identity = data.ghwidgets_common.Identity(
            "Canonical", 42, ["PrivateOrg"], {"canonical", "privateorg"},
            {"canonical@users.noreply.github.com"}, ["PublicOrg"])
        with mock.patch.object(
                data.ghwidgets_common, "fetch_identity",
                return_value=identity) as fetch_identity, \
                mock.patch.object(
                    data.ghwidgets_common, "fetch_pull_requests",
                    return_value=([], {})), \
                mock.patch.object(
                    data.ghwidgets_common, "fetch_issues", return_value=[]):
            out = data.fetch_authored_snapshot(
                "top-secret-token", "requested-login", gql_fn=object())

        self.assertTrue(fetch_identity.call_args.kwargs["include_public_orgs"])
        self.assertEqual(out["insiders"], ["canonical", "publicorg"])


class SnapshotValidation(unittest.TestCase):
    def test_snapshot_rejects_unknown_schema(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "snapshot.json"
            path.write_text('{"schema_version":999}', encoding="utf-8")
            with self.assertRaises(data.SnapshotVersionError):
                data.load_snapshot(path)

    def test_snapshot_requires_all_top_level_fields(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "snapshot.json"
            path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
            with self.assertRaises(data.SnapshotValidationError):
                data.load_snapshot(path)

    def test_snapshot_requires_collection_fields_to_be_lists(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "snapshot.json"
            value = snapshot(insiders={"members": ["me"]})
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(data.SnapshotValidationError):
                data.load_snapshot(path)

    def test_snapshot_rejects_invalid_json(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "snapshot.json"
            path.write_text("not json", encoding="utf-8")
            with self.assertRaises(data.SnapshotValidationError):
                data.load_snapshot(path)


class SnapshotWriting(unittest.TestCase):
    def test_write_snapshot_round_trips_atomically(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nested" / "snapshot.json"
            value = snapshot()
            data.write_snapshot(path, value)
            self.assertEqual(data.load_snapshot(path), value)

    def test_write_snapshot_validates_before_replacing_existing_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "snapshot.json"
            value = snapshot()
            data.write_snapshot(path, value)
            with self.assertRaises(data.SnapshotValidationError):
                data.write_snapshot(path, {"schema_version": 1})
            self.assertEqual(data.load_snapshot(path), value)

    def test_write_snapshot_raises_when_atomic_write_fails(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "snapshot.json"
            with mock.patch.object(
                    data.ghwidgets_common, "_write_cache",
                    side_effect=OSError("no space left")):
                with self.assertRaises(OSError):
                    data.write_snapshot(path, snapshot())
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
