#!/usr/bin/env python3
"""Public, normalized GitHub data and versioned snapshot contract.

The renderers' private caches are implementation details.  This module is the
small, dependency-free boundary for consumers that need to exchange public
GitHub data with the renderers.  Only ``fetch_authored_snapshot`` constructs a
supported snapshot; ``_build_snapshot`` is an internal producer helper whose
input is always validated before it crosses the boundary.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Optional, Union

import ghwidgets_common


SCHEMA_VERSION: int = 1

_TOP_LEVEL_FIELDS = (
    "schema_version",
    "generated_at",
    "account",
    "repositories",
    "issues",
    "pull_requests",
)
_ACCOUNT_FIELDS = ("login",)
_REPOSITORY_FIELDS = ("id", "nameWithOwner", "url", "isPrivate", "owner")
_REPOSITORY_OWNER_FIELDS = ("login",)
_ITEM_FIELDS = (
    "node_id",
    "repository_id",
    "repository",
    "owner",
    "repository_url",
    "is_private",
    "number",
    "url",
    "created_at",
    "updated_at",
    "closed_at",
    "state",
    "state_reason",
)
_PULL_REQUEST_FIELDS = tuple(
    field for field in _ITEM_FIELDS if field != "state_reason") + (
        "merged_at", "merged")
_COLLECTION_FIELDS = ("repositories", "issues", "pull_requests")
_ISSUE_STATES = frozenset(("OPEN", "CLOSED"))
_PULL_REQUEST_STATES = frozenset(("OPEN", "CLOSED", "MERGED"))
# GitHub may add a new enum member before this consumer is updated. Keep the
# exact source spelling for reconciliation while bounding the value that can
# cross the public snapshot boundary.
MAX_STATE_REASON_LENGTH = 64
_RFC3339 = re.compile(
    r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z"
)
SnapshotPath = Union[str, Path]


class SnapshotValidationError(ValueError):
    """Raised when a snapshot does not have the public contract shape."""


class SnapshotVersionError(SnapshotValidationError):
    """Raised when a snapshot uses a schema version this module cannot read."""


def _normalise_item(node, *, include_state_reason):
    repo = node["repository"]
    out = {
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
    }
    if include_state_reason:
        out["state_reason"] = node.get("stateReason")
    return out


def normalise_issue(node: dict) -> dict:
    """Convert a GitHub GraphQL issue node to its public record shape."""
    return _normalise_item(node, include_state_reason=True)


def normalise_pull_request(node: dict) -> dict:
    """Convert a GitHub GraphQL pull-request node to its public record shape."""
    out = _normalise_item(node, include_state_reason=False)
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


# These validators deliberately reject dict/list subclasses.  The snapshot
# boundary must not execute attacker-controlled container behavior.
# pylint: disable=unidiomatic-typecheck
def _exact_keys(value, expected, context):
    """Require a JSON object to have exactly the documented fields."""
    if type(value) is not dict:
        raise SnapshotValidationError(f"{context} must be an object")
    expected = set(expected)
    actual = set(value)
    unknown = sorted(actual - expected, key=repr)
    missing = sorted(expected - actual, key=repr)
    if unknown:
        raise SnapshotValidationError(
            f"{context} has unknown field(s): {', '.join(map(str, unknown))}")
    if missing:
        raise SnapshotValidationError(
            f"{context} missing required field(s): {', '.join(map(str, missing))}")


def _string(value, context):
    if type(value) is not str or not value:
        raise SnapshotValidationError(f"{context} must be a non-empty string")


def _boolean(value, context):
    if type(value) is not bool:
        raise SnapshotValidationError(f"{context} must be a boolean")


def _integer(value, context):
    if type(value) is not int:
        raise SnapshotValidationError(f"{context} must be an integer")


def _timestamp(value, context, *, nullable=False):
    if nullable and value is None:
        return
    if type(value) is not str or not value or not _RFC3339.fullmatch(value):
        raise SnapshotValidationError(
            f"{context} must be a non-empty RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SnapshotValidationError(
            f"{context} must be a non-empty RFC 3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SnapshotValidationError(
            f"{context} must be a non-empty RFC 3339 timestamp")


def _nullable_string(value, context, allowed=None, max_length=None):
    if value is None:
        return
    if type(value) is not str or not value or not value.strip():
        raise SnapshotValidationError(f"{context} must be a string or null")
    if max_length is not None and len(value) > max_length:
        raise SnapshotValidationError(
            f"{context} exceeds the maximum length of {max_length}")
    if allowed is not None and value not in allowed:
        raise SnapshotValidationError(f"{context} has an invalid value")


def _validate_account(account):
    _exact_keys(account, _ACCOUNT_FIELDS, "snapshot account")
    _string(account["login"], "snapshot account.login")


def _validate_repository(repository, index):
    context = f"snapshot repositories[{index}]"
    _exact_keys(repository, _REPOSITORY_FIELDS, context)
    _string(repository["id"], f"{context}.id")
    _string(repository["nameWithOwner"], f"{context}.nameWithOwner")
    _string(repository["url"], f"{context}.url")
    if repository["isPrivate"] is not False:
        raise SnapshotValidationError(
            f"{context}.isPrivate must be false for public snapshots")
    _exact_keys(repository["owner"], _REPOSITORY_OWNER_FIELDS,
                f"{context}.owner")
    _string(repository["owner"]["login"], f"{context}.owner.login")


def _validate_item(item, index, kind, repository_by_id):  # pylint: disable=too-many-branches,too-many-statements
    context = f"snapshot {kind}[{index}]"
    fields = _PULL_REQUEST_FIELDS if kind == "pull_requests" else _ITEM_FIELDS
    _exact_keys(item, fields, context)
    for field in ("node_id", "repository_id", "repository", "owner",
                  "repository_url"):
        _string(item[field], f"{context}.{field}")
    if item["is_private"] is not False:
        raise SnapshotValidationError(
            f"{context}.is_private must be false for public snapshots")
    _integer(item["number"], f"{context}.number")
    if item["number"] < 1:
        raise SnapshotValidationError(f"{context}.number must be positive")
    _string(item["url"], f"{context}.url")
    _timestamp(item["created_at"], f"{context}.created_at")
    _timestamp(item["updated_at"], f"{context}.updated_at")
    _timestamp(item["closed_at"], f"{context}.closed_at", nullable=True)
    _string(item["state"], f"{context}.state")
    states = (_PULL_REQUEST_STATES if kind == "pull_requests"
              else _ISSUE_STATES)
    if item["state"] not in states:
        raise SnapshotValidationError(f"{context}.state has an invalid value")
    if kind == "issues":
        _nullable_string(
            item["state_reason"], f"{context}.state_reason",
            max_length=MAX_STATE_REASON_LENGTH,
        )

    repository = repository_by_id.get(item["repository_id"])
    if repository is None:
        raise SnapshotValidationError(
            f"{context}.repository_id references an absent repository")
    if item["repository"] != repository["nameWithOwner"]:
        raise SnapshotValidationError(
            f"{context}.repository does not match its repository record")
    if item["owner"] != repository["owner"]["login"]:
        raise SnapshotValidationError(
            f"{context}.owner does not match its repository record")
    if item["repository_url"] != repository["url"]:
        raise SnapshotValidationError(
            f"{context}.repository_url does not match its repository record")

    if kind == "issues":
        if item["state"] == "OPEN":
            if item["closed_at"] is not None:
                raise SnapshotValidationError(
                    f"{context} open state cannot have closed_at")
            if item["state_reason"] in ("COMPLETED", "NOT_PLANNED"):
                raise SnapshotValidationError(
                    f"{context} open state has an inconsistent state_reason")
        else:
            if item["closed_at"] is None:
                raise SnapshotValidationError(
                    f"{context} closed state requires closed_at")
            if item["state_reason"] in (None, "REOPENED"):
                raise SnapshotValidationError(
                    f"{context} closed issue has an inconsistent state_reason")
    else:
        _boolean(item["merged"], f"{context}.merged")
        _timestamp(item["merged_at"], f"{context}.merged_at", nullable=True)
        if item["state"] == "OPEN":
            if item["merged"] is not False or item["closed_at"] is not None \
                    or item["merged_at"] is not None:
                raise SnapshotValidationError(
                    f"{context} open PR has inconsistent merge fields")
        elif item["state"] == "CLOSED":
            if item["merged"] is not False or item["closed_at"] is None \
                    or item["merged_at"] is not None:
                raise SnapshotValidationError(
                    f"{context} closed PR has inconsistent merge fields")
        else:
            if item["merged"] is not True or item["closed_at"] is None \
                    or item["merged_at"] is None:
                raise SnapshotValidationError(
                    f"{context} merged PR has inconsistent merge fields")


def _validate_snapshot(snapshot: dict) -> dict:
    if type(snapshot) is not dict:
        raise SnapshotValidationError("snapshot must be a JSON object")

    if "schema_version" not in snapshot:
        raise SnapshotValidationError(
            "snapshot missing required field(s): schema_version")
    version = snapshot["schema_version"]
    if type(version) is not int:
        raise SnapshotValidationError("snapshot schema_version must be an integer")
    if version != SCHEMA_VERSION:
        raise SnapshotVersionError(
            f"unsupported snapshot schema_version: {version}")

    _exact_keys(snapshot, _TOP_LEVEL_FIELDS, "snapshot")
    _timestamp(snapshot["generated_at"], "snapshot generated_at")
    _validate_account(snapshot["account"])
    for field in _COLLECTION_FIELDS:
        if type(snapshot[field]) is not list:
            raise SnapshotValidationError(f"snapshot {field} must be a list")

    repository_by_id = {}
    for index, repository in enumerate(snapshot["repositories"]):
        _validate_repository(repository, index)
        repository_id = repository["id"]
        if repository_id in repository_by_id:
            raise SnapshotValidationError(
                f"duplicate repository node_id: {repository_id}")
        repository_by_id[repository_id] = repository
        if repository["owner"]["login"].casefold() == \
                snapshot["account"]["login"].casefold():
            raise SnapshotValidationError(
                f"snapshot repositories[{index}] is owned by the account")

    item_ids = set(repository_by_id)
    for kind in ("issues", "pull_requests"):
        for index, item in enumerate(snapshot[kind]):
            _validate_item(item, index, kind, repository_by_id)
            node_id = item["node_id"]
            if node_id in item_ids:
                raise SnapshotValidationError(f"duplicate item node_id: {node_id}")
            item_ids.add(node_id)
    return snapshot


# pylint: enable=unidiomatic-typecheck


def _build_snapshot(*, account: dict, repositories: list[dict],
                    issues: list[dict], pull_requests: list[dict],
                    generated_at: Optional[str] = None) -> dict:
    """Build the only supported v1 snapshot shape, for internal producers."""
    timestamp = (generated_at if generated_at is not None else datetime.now(
        timezone.utc).isoformat(timespec="seconds"))
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": timestamp,
        "account": account,
        "repositories": repositories,
        "issues": issues,
        "pull_requests": pull_requests,
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
        token, login, gql_fn=gql_fn, include_environment=False,
        include_public_orgs=True)
    pull_requests, _ = ghwidgets_common.fetch_pull_requests(
        token, identity.login, cached_prs=None, max_pages=max_pages,
        gql_fn=gql_fn)
    issues = ghwidgets_common.fetch_issues(
        token, identity.login, max_pages=max_pages, gql_fn=gql_fn)

    # Public organization memberships are the only membership relationship
    # used for the public classification.  The set is transient and never
    # crosses the snapshot boundary; concealed/private memberships are not
    # serialized or used to make a public ownership claim.
    public_insiders = ghwidgets_common.insider_set(
        identity.login, identity.public_orgs, extra=())
    public_pull_requests = [
        node for node in pull_requests
        if ghwidgets_common.is_external(node, public_insiders)]
    public_issues = [
        node for node in issues
        if ghwidgets_common.is_external(node, public_insiders)]

    repositories = {}
    for node in [*public_pull_requests, *public_issues]:
        repo = node["repository"]
        repositories.setdefault(repo["id"], _public_repository(repo))

    return _build_snapshot(
        account={"login": identity.login},
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
