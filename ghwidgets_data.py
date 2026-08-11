#!/usr/bin/env python3
"""Public, normalized GitHub data and versioned snapshot contract.

The renderers' private caches are implementation details.  This module is the
small, dependency-free boundary for consumers that need to exchange public
GitHub data with the renderers.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

import ghwidgets_common


SCHEMA_VERSION: int = 1

_REQUIRED_FIELDS = (
    "schema_version",
    "generated_at",
    "account",
    "insiders",
    "repositories",
    "issues",
    "pull_requests",
)
_COLLECTION_FIELDS = ("insiders", "repositories", "issues", "pull_requests")
SnapshotPath = Union[str, Path]


class SnapshotValidationError(ValueError):
    """Raised when a snapshot does not have the public contract shape."""


class SnapshotVersionError(SnapshotValidationError):
    """Raised when a snapshot uses a schema version this module cannot read."""


def _normalise_item(node):
    repo = node["repository"]
    return {
        "node_id": node["id"],
        "repository_id": repo["id"],
        "repository": repo["nameWithOwner"],
        "owner": repo["owner"]["login"],
        "repository_url": repo["url"],
        "is_private": bool(repo["isPrivate"]),
        "number": int(node["number"]),
        "url": node["url"],
        "created_at": node["createdAt"],
        "updated_at": node["updatedAt"],
        "closed_at": node.get("closedAt"),
        "state": node["state"],
        "state_reason": node.get("stateReason"),
    }


def normalise_issue(node: dict) -> dict:
    """Convert a GitHub GraphQL issue node to its public record shape."""
    return _normalise_item(node)


def normalise_pull_request(node: dict) -> dict:
    """Convert a GitHub GraphQL pull-request node to its public record shape."""
    out = _normalise_item(node)
    out.update({
        "merged_at": node.get("mergedAt"),
        "merged": bool(node.get("merged")),
    })
    return out


def _public_repository(repo: dict) -> dict:
    """Keep only the public repository fields used by snapshot consumers."""
    return {
        "id": repo["id"],
        "nameWithOwner": repo["nameWithOwner"],
        "url": repo["url"],
        "isPrivate": bool(repo["isPrivate"]),
        "owner": {"login": repo["owner"]["login"]},
    }


def _validate_snapshot(snapshot: dict) -> dict:
    if not isinstance(snapshot, dict):
        raise SnapshotValidationError("snapshot must be a JSON object")

    if "schema_version" not in snapshot:
        raise SnapshotValidationError(
            "snapshot missing required field(s): schema_version")
    version = snapshot["schema_version"]
    if not isinstance(version, int) or isinstance(version, bool):
        raise SnapshotValidationError("snapshot schema_version must be an integer")
    if version != SCHEMA_VERSION:
        raise SnapshotVersionError(
            f"unsupported snapshot schema_version: {version}")

    missing = [key for key in _REQUIRED_FIELDS if key not in snapshot]
    if missing:
        raise SnapshotValidationError(
            "snapshot missing required field(s): " + ", ".join(missing))

    if not isinstance(snapshot["generated_at"], str):
        raise SnapshotValidationError("snapshot generated_at must be a string")
    if not isinstance(snapshot["account"], dict):
        raise SnapshotValidationError("snapshot account must be an object")
    for field in _COLLECTION_FIELDS:
        if not isinstance(snapshot[field], list):
            raise SnapshotValidationError(
                f"snapshot {field} must be a list")
    return snapshot


def build_snapshot(*, account: dict, insiders: set[str],
                   repositories: list[dict], issues: list[dict],
                   pull_requests: list[dict],
                   generated_at: Optional[str] = None) -> dict:
    """Build and validate a versioned public snapshot.

    ``insiders`` is a set at the acquisition boundary, but snapshots use a
    sorted list so their JSON representation is deterministic.
    """
    timestamp = (generated_at if generated_at is not None else datetime.now(
        timezone.utc).isoformat(timespec="seconds"))
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": timestamp,
        "account": account,
        "insiders": sorted(insiders),
        "repositories": list(repositories),
        "issues": list(issues),
        "pull_requests": list(pull_requests),
    }
    return _validate_snapshot(snapshot)


def fetch_authored_snapshot(token: str, login: str, *, gql_fn=None,
                            max_pages: Optional[int] = None) -> dict:
    """Fetch all public issues and pull requests authored by an account.

    The token is used only for API calls and is deliberately absent from the
    returned public snapshot. Environment-based insider and email additions
    are also disabled so this boundary is deterministic and cannot publish
    configuration or credentials.
    """
    identity = ghwidgets_common.fetch_identity(
        token, login, gql_fn=gql_fn, include_environment=False)
    pull_requests, _ = ghwidgets_common.fetch_pull_requests(
        token, identity.login, cached_prs=None, max_pages=max_pages,
        gql_fn=gql_fn)
    issues = ghwidgets_common.fetch_issues(
        token, identity.login, max_pages=max_pages, gql_fn=gql_fn)

    public_pull_requests = [
        node for node in pull_requests if not node["repository"]["isPrivate"]]
    public_issues = [
        node for node in issues if not node["repository"]["isPrivate"]]

    repositories = {}
    for node in [*public_pull_requests, *public_issues]:
        repo = node["repository"]
        repositories.setdefault(repo["id"], _public_repository(repo))

    return build_snapshot(
        account={"login": identity.login},
        insiders=ghwidgets_common.insider_set(
            identity.login, identity.orgs, extra=()),
        repositories=list(repositories.values()),
        issues=[normalise_issue(node) for node in public_issues],
        pull_requests=[normalise_pull_request(node)
                       for node in public_pull_requests],
    )


def load_snapshot(path: SnapshotPath) -> dict:
    """Read and validate a public snapshot from JSON."""
    path = Path(path)
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SnapshotValidationError(
            f"could not decode snapshot {path}") from exc
    return _validate_snapshot(snapshot)


def write_snapshot(path: SnapshotPath, snapshot: dict) -> None:
    """Validate and atomically write a public snapshot under the common lock."""
    _validate_snapshot(snapshot)
    if not ghwidgets_common.save_cache(path, snapshot, strict=True):
        raise OSError(f"could not write snapshot {path}")
