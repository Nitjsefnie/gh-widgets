"""Private unit-run sharing with a process-wide request cap of 2.

``limited_request`` accepts GraphQL/REST callables. Two slots limit exposure to
GitHub's secondary CPU budget; nested pools share slots, cursor pages stay serial.
"""
import functools
import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path

import ghwidgets_cache


MAX_IN_FLIGHT = 2
_REQUEST_SLOTS = threading.BoundedSemaphore(MAX_IN_FLIGHT)


def limited_request(request):
    """Decorate a request callable with the process-wide in-flight limit."""
    @functools.wraps(request)
    def run(*args, **kwargs):
        with _REQUEST_SLOTS:
            return request(*args, **kwargs)
    return run


def _keys(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() \
            or value.keys() - set(required) - set(optional):
        raise ValueError("unexpected or missing shared fields")


def _string(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("shared string must be non-empty")


def _strings(value):
    if not isinstance(value, list):
        raise ValueError("shared strings must be a list")
    for entry in value:
        _string(entry)


def _positive(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("shared integer must be positive")


def _timestamp(value, *, nullable):
    if nullable and value is None:
        return
    _string(value)
    if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)", value):
        raise ValueError("shared timestamp must be RFC 3339")
    datetime.fromisoformat(value.replace("Z", "+00:00"))


def _repository(repo):
    _keys(repo, ("nameWithOwner", "isPrivate", "owner"), ("id", "url"))
    for field in ("nameWithOwner", "id", "url"):
        if field in repo:
            _string(repo[field])
    if not isinstance(repo["isPrivate"], bool):
        raise ValueError("shared isPrivate must be boolean")
    _keys(repo["owner"], ("login",))
    _string(repo["owner"]["login"])


def _items(items, *, issues=False):
    if not isinstance(items, list):
        raise ValueError("shared items must be a list")
    seen = set()
    for node in items:
        # Legacy cached merges can lack timestamps and retain their pre-merge state.
        _keys(node, ("id", "repository", "state", "stateReason" if issues else "merged"),
              ("number", "url", "createdAt", "updatedAt", "closedAt")
              + (() if issues else ("mergedAt",)))
        _string(node["id"])
        if node["id"] in seen:
            raise ValueError("duplicate shared item id")
        seen.add(node["id"])
        if node["state"] not in ("OPEN", "CLOSED") + (() if issues else ("MERGED",)):
            raise ValueError("invalid shared item state")
        if issues:
            if node["stateReason"] is not None:
                _string(node["stateReason"])
        elif not isinstance(node["merged"], bool):
            raise ValueError("shared merged must be boolean")
        for field in ("createdAt", "updatedAt", "closedAt", "mergedAt"):
            if field in node:
                _timestamp(node[field], nullable=field in ("closedAt", "mergedAt"))
        if "number" in node:
            _positive(node["number"])
        if "url" in node:
            _string(node["url"])
        _repository(node["repository"])


def validate(payload):
    """Validate consumed identity and complete lists before writing or reading."""
    _keys(payload, ("version", "invocation_id", "identity", "issues", "prs"))
    if type(payload["version"]) is not int or payload["version"] != 1:  # pylint: disable=unidiomatic-typecheck
        raise ValueError("unsupported shared version")
    if not isinstance(payload["invocation_id"], str):
        raise ValueError("shared invocation_id must be a string")
    me = payload["identity"]
    _keys(me, ("login", "databaseId", "orgs", "insiders", "emails"))
    _string(me["login"])
    _positive(me["databaseId"])
    for field in ("orgs", "insiders", "emails"):
        _strings(me[field])
    _items(payload["issues"], issues=True)
    _items(payload["prs"])


def share_path():
    """Return the opt-in private artifact location, or None for standalone runs."""
    directory = os.environ.get("GH_WIDGETS_SHARE_DIR")
    return Path(directory) / "authored.json" if directory else None


def begin():
    """Invalidate the prior acquisition before the producer can fall back."""
    path = share_path()
    if path is not None:
        with ghwidgets_cache.cache_lock(path) as locked:
            if not locked:
                raise TimeoutError(f"share lock unavailable for {path}")
            path.unlink(missing_ok=True)
    return path


def write(path, identity, issues, prs):
    """Publish only a successful acquisition, atomically under the cache lock."""
    payload = {
        "version": 1, "invocation_id": os.environ.get("INVOCATION_ID", ""),
        "identity": {"login": identity.login, "databaseId": identity.database_id,
                     "orgs": identity.orgs, "insiders": sorted(identity.insiders),
                     "emails": sorted(identity.emails)},
        "issues": issues, "prs": prs,
    }
    validate(payload)
    ghwidgets_cache.save_cache(path, payload)


def load(login):
    """Use only this account's current unit acquisition; explain live fallbacks."""
    path = share_path()
    reason = "GH_WIDGETS_SHARE_DIR unset"
    if path is not None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            reason = "share artifact absent"
        except OSError as exc:
            reason = f"share artifact unreadable: {exc}"
        except (ValueError, UnicodeError):
            reason = "bad share JSON"
        else:
            try:
                validate(payload)
                if payload["identity"]["login"].casefold() != login.casefold():
                    raise ValueError("share account mismatch")
                if payload["invocation_id"] != os.environ.get("INVOCATION_ID", ""):
                    raise ValueError("share unit invocation mismatch")
            except ValueError as exc:
                reason = f"share validation failure: {exc}"
            else:
                return payload
    print(f"share: live fetch ({reason})", flush=True)
    return None
