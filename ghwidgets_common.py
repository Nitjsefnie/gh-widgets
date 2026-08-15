#!/usr/bin/env python3
"""
gh-widgets — code shared by render.py and render-impact.py.

This module exists because the two renderers were carrying byte-identical
code (theme table, GraphQL client, PR/issue paging, cache helpers, SVG chrome)
that drifted independently. It also owns the identity model both scripts need:
who counts as an insider, and which commit-author addresses count as ours.

Identity is DERIVED, not configured. `fetch_identity` asks the API who the
token's account is and which orgs it belongs to; the environment can only ADD
to that (GH_EXTRA_INSIDERS, GH_EXTRA_EMAILS), never replace it. An override
would let a stale value silently reintroduce the drift this module removes.

Note on scopes: the query deliberately does NOT request `user.email`. That
field requires `read:user`, which the production token does not carry, and a
field-level scope failure rejects the ENTIRE query — so asking for it would
break identity resolution outright. Addresses that GitHub will not tell us
about go in GH_EXTRA_EMAILS.

Loading: both scripts load this file by explicit path relative to their own
__file__ (see `load_common` usage in each), because deployment renames
render.py to render-gh-widgets.py and import-by-name would not survive that.
Each script asserts COMMON_VERSION, so copying one file without the other
fails loudly at startup instead of rendering wrong numbers.

Zero external deps. Pure Python stdlib. Requires Python 3.9+.
"""
import contextlib
import fcntl
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import namedtuple
from pathlib import Path
from typing import Optional

# Bumped whenever this module's interface changes in a way that would make an
# older script misbehave against it. Each script pins the version it expects.
COMMON_VERSION = 3


def check_version(required):
    """Fail loudly if this module is not the version the caller pins.

    A mismatch means one file was copied without the other; aborting here is
    better than rendering wrong numbers from a stale module.
    """
    if COMMON_VERSION != required:
        raise SystemExit(
            f"error: ghwidgets_common.py is version {COMMON_VERSION}, "
            f"this script needs {required} — copy both files together")


FONT = ('font-family="JetBrains Mono, ui-monospace, '
        'SFMono-Regular, Menlo, monospace"')

THEMES = {
    "tokyonight": {
        "bg":     "#1a1b26", "bg2":   "#16161e", "border": "#2a2e42",
        "fg":     "#c0caf5", "dim":   "#565f89",
        "blue":   "#7aa2f7", "cyan":  "#7dcfff", "purple": "#bb9af7",
        "pink":   "#f7768e", "green": "#9ece6a", "gold":   "#e0af68",
    },
    "catppuccin": {
        "bg":     "#1e1e2e", "bg2":   "#181825", "border": "#313244",
        "fg":     "#cdd6f4", "dim":   "#7f849c",
        "blue":   "#89b4fa", "cyan":  "#94e2d5", "purple": "#cba6f7",
        "pink":   "#f5c2e7", "green": "#a6e3a1", "gold":   "#f9e2af",
    },
    "gruvbox": {
        "bg":     "#282828", "bg2":   "#1d2021", "border": "#3c3836",
        "fg":     "#ebdbb2", "dim":   "#928374",
        "blue":   "#83a598", "cyan":  "#8ec07c", "purple": "#d3869b",
        "pink":   "#fb4934", "green": "#b8bb26", "gold":   "#fabd2f",
    },
    "github-dark": {
        "bg":     "#0d1117", "bg2":   "#010409", "border": "#30363d",
        "fg":     "#e6edf3", "dim":   "#7d8590",
        "blue":   "#58a6ff", "cyan":  "#39c5cf", "purple": "#bc8cff",
        "pink":   "#ff7b72", "green": "#3fb950", "gold":   "#d29922",
    },
}


# ---------------------------------------------------------------- environment

def env_list(name, default=""):
    """Comma-separated env var -> list of trimmed non-empty strings."""
    return [s.strip() for s in os.environ.get(name, default).split(",") if s.strip()]


def env_float(name, default):
    """Numeric env var with a default.

    An unparseable value is a hard error, NOT a silent fall back to the
    default: a typo in a tuning knob would otherwise render wrong numbers with
    no signal anywhere, which is exactly the failure mode these knobs were
    moved out of source to avoid.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise SystemExit(f"error: {name}={raw!r} is not a number") from exc


# --------------------------------------------------------------------- cache

def load_cache(path, version):
    """Read the JSON cache. A missing, unreadable, corrupt, or
    schema-mismatched cache is not an error: it degrades to a full fetch."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != version:
        return {}
    return data


class CacheShapeError(ValueError):
    """Raised when a populated impact-cache map no longer has its schema."""


def _validate_count(name, repo, field, value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CacheShapeError(
            f"impact cache {name}[{repo!r}].{field!r} must be a "
            "non-negative integer")


def _validate_impact_map(cache, name, required_fields):
    """Validate fields only for repositories represented in a cache map.

    A missing or empty map is a legitimate no-contribution result. Once the
    producer has written an entry, however, a missing field is structural
    drift and must not be converted to a ranking zero.
    """
    if name not in cache:
        return
    entries = cache[name]
    if not isinstance(entries, dict):
        raise CacheShapeError(
            f"impact cache field {name!r} must be a map")
    for repo, entry in entries.items():
        if not isinstance(entry, dict):
            raise CacheShapeError(
                f"impact cache {name}[{repo!r}] must be a map")
        for field in required_fields:
            if field not in entry:
                raise CacheShapeError(
                    f"impact cache {name}[{repo!r}] missing field {field!r}")
            _validate_count(name, repo, field, entry[field])


def validate_cache_shape(cache):
    """Reject structural drift in populated ranking inputs.

    Repositories absent from a map remain valid: an empty map means there is
    no contribution in that metric. ``ourloc`` entries without ``ours`` are
    failed-blame records and retain the existing filtering behaviour; entries
    that do contain ``ours`` must also contain ``total``.
    """
    _validate_impact_map(cache, "totals", ("merged_prs", "issues"))
    if "ourloc" not in cache:
        return
    entries = cache["ourloc"]
    if not isinstance(entries, dict):
        raise CacheShapeError("impact cache field 'ourloc' must be a map")
    for repo, entry in entries.items():
        if not isinstance(entry, dict):
            raise CacheShapeError(
                f"impact cache ourloc[{repo!r}] must be a map")
        if "ours" in entry and "total" not in entry:
            raise CacheShapeError(
                f"impact cache ourloc[{repo!r}] missing field 'total'")
        if "ours" in entry:
            _validate_count("ourloc", repo, "ours", entry["ours"])
            _validate_count("ourloc", repo, "total", entry["total"])


CACHE_LOCK_TIMEOUT = 60.0


def _open_lock_file(path):
    """Open (creating) the lock file beside `path`; None if it cannot be
    opened. An unwritable cache directory is already reported by the write
    itself — it must not turn into a second failure mode here."""
    lock = Path(str(path) + ".lock")
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        return os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as e:
        print(f"warning: could not open cache lock {lock}: {e}",
              file=sys.stderr)
        return None


def _flock_until(fd, timeout):
    """Take an exclusive flock on `fd`, polling until `timeout` elapses.

    Polled rather than blocking: a blocking flock has no timeout, and a render
    must never park forever behind another writer. Returns whether it is held.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)


@contextlib.contextmanager
def cache_lock(path, timeout=CACHE_LOCK_TIMEOUT):
    """Serialise the writers of one cache file against each other.

    Two scripts write the impact cache: render-impact.py replaces it whole
    twice a day, render-responsiveness.py reads-modifies-writes its PR half
    every hour. Without a lock spanning that read-modify-write, a whole-file
    save landing between its read and its write is discarded — including
    `ourloc`, which is expensive to rebuild.

    Yields True when the lock is held, False when it is not (timeout, or a
    lock file that could not be created). The caller decides what that means:
    a whole-file writer loses nothing by proceeding anyway, a
    read-modify-write writer must not proceed at all.
    """
    fd = _open_lock_file(path)
    if fd is None:
        yield False
        return
    try:
        yield _flock_until(fd, timeout)
    finally:
        os.close(fd)  # closing the fd releases the flock


def _write_cache(path, payload):
    """Atomic write: temp file beside the target, then os.replace onto it.

    os.replace within one filesystem is atomic, so no reader ever observes a
    half-written cache. The temp file is removed if the write fails, so a
    failed save leaves neither a partial cache nor litter behind. Callers hold
    cache_lock, which is also what makes the fixed temp name safe.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)  # a no-op once os.replace has moved it


def save_cache(path, payload, timeout=CACHE_LOCK_TIMEOUT, *, strict=False):
    """Replace the whole cache, atomically and under the writers' lock.
    A failed save must not fail the run by default. With ``strict=True``,
    lock or write failures are raised instead, and an unavailable lock is not
    bypassed.

    For the default best-effort caller, proceeding without the lock can at
    worst drop the other writer's PR refresh — which its next hourly run
    redoes.
    """
    try:
        with cache_lock(path, timeout) as locked:
            if not locked:
                if strict:
                    raise TimeoutError(f"cache lock unavailable for {path}")
                print(f"warning: writing {path} without the cache lock",
                      file=sys.stderr)
            _write_cache(path, payload)
        return True
    except Exception as e:
        print(f"warning: could not write cache {path}: {e}", file=sys.stderr)
        if strict:
            raise
        return False


def merge_cache(path, version, updates, timeout=CACHE_LOCK_TIMEOUT):
    """Replace `updates`' keys in the cache at `path`, preserving every other
    key, atomically and under the lock. Returns the payload written, or None
    if nothing was written.

    For a writer that owns only PART of a shared cache. The load and the write
    happen inside one lock hold, so the whole-file writer cannot slip in
    between and have its expensive sections (`ourloc`) silently reverted to
    what this writer happened to read.

    Failing to update is the safe outcome and is reported, not raised: a cache
    that missed one refresh is recovered by the next run; one that lost
    `ourloc` must wait for the next `--resync` to rebuild it.
    """
    try:
        with cache_lock(path, timeout) as locked:
            if not locked:
                print(f"warning: cache {path} not updated: lock unavailable",
                      file=sys.stderr)
                return None
            payload = {**load_cache(path, version), **updates,
                       "version": version}
            _write_cache(path, payload)
            return payload
    except Exception as e:
        print(f"warning: could not update cache {path}: {e}", file=sys.stderr)
        return None


# ------------------------------------------------------------------- GraphQL

def gql(token, query, variables=None, retries=3, timeout=20):
    """POST a GraphQL query, retrying transient server-side conditions.

    GitHub intermittently answers with RESOURCE_LIMITS_EXCEEDED under load.
    Transient conditions get a few retries; real errors fail fast.
    """
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        method="POST",
        data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "gh-widgets/1.0 (+https://github.com/Nitjsefnie/gh-widgets)",
        },
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = json.loads(r.read())
        except urllib.error.URLError:
            if attempt == retries:
                raise
            time.sleep(5 * (attempt + 1))
            continue
        if "errors" in body:
            transient = all(
                e.get("type") in ("RESOURCE_LIMITS_EXCEEDED", "SERVICE_UNAVAILABLE")
                for e in body["errors"]
            )
            if transient and attempt < retries:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f"GraphQL errors: {body['errors']}")
        return body["data"]
    raise RuntimeError("unreachable: gql retry loop exhausted")


# ------------------------------------------------------------------ identity

class Identity(namedtuple(
        "Identity",
        "login database_id orgs insiders emails public_orgs")):
    """Resolved account identity and its transient/public org views.

    ``orgs`` and ``insiders`` intentionally retain every membership visible to
    the authenticated token: render-time ownership classification needs that
    information.  ``public_orgs`` is the only membership collection suitable
    for a public snapshot.  The optional default keeps five-field callers
    compatible without treating authenticated organizations as public.
    """

    __slots__ = ()

    def __new__(cls, login, database_id, orgs, insiders, emails,
                public_orgs=None):
        if public_orgs is None:
            public_orgs = []
        return super().__new__(
            cls, login, database_id, orgs, insiders, emails, public_orgs)


class PaginationLimitError(RuntimeError):
    """Raised when a bounded pagination run cannot reach its final page."""


IDENTITY_QUERY = """
query($login: String!, $cursor: String) {
  user(login: $login) {
    login
    databaseId
    organizations(first: 100, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes { login }
    }
  }
}
"""


def insider_set(login, orgs, extra=None):
    """Casefolded set of owner logins that are NOT external: the account
    itself, every org it belongs to, plus anything in GH_EXTRA_INSIDERS.

    Additive by construction — there is no way to remove a fetched org, so a
    stale configuration cannot make an owned repo look external.
    """
    if extra is None:
        extra = env_list("GH_EXTRA_INSIDERS")
    return frozenset(n.casefold() for n in [login, *orgs, *extra] if n)


def noreply_addresses(login, database_id):
    """Both forms of GitHub's noreply address for an account.

    GitHub rewrites the commit author on merge to the ID-prefixed form
    (e.g. 75166987+octocat@users.noreply.github.com); the bare form predates
    per-user IDs and still appears in older history. Constructing them from
    databaseId is why ownership can be an exact match instead of a substring
    test on an attacker-controllable field.
    """
    out = {f"{login}@users.noreply.github.com".lower()}
    if database_id:
        out.add(f"{database_id}+{login}@users.noreply.github.com".lower())
    return out


def our_emails(login, database_id, extra=None):
    """Lowercased set of commit-author addresses that count as ours."""
    if extra is None:
        extra = env_list("GH_EXTRA_EMAILS")
    return frozenset(noreply_addresses(login, database_id)
                     | {e.strip().lower() for e in extra if e.strip()})


def fetch_public_organizations(login, request_fn=None):
    """Fetch only memberships GitHub exposes publicly for ``login``.

    This deliberately uses the unauthenticated ``/users/:login/orgs`` REST
    endpoint.  The authenticated GraphQL organization connection can include
    concealed memberships, which are useful transiently for ownership
    classification but must never enter a public snapshot.
    """
    request_fn = request_fn or urllib.request.urlopen
    page = 1
    public_orgs = []
    while True:
        encoded_login = urllib.parse.quote(login, safe="")
        url = (f"https://api.github.com/users/{encoded_login}/orgs"
               f"?per_page=100&page={page}")
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": (
                    "gh-widgets/1.0 (+https://github.com/Nitjsefnie/gh-widgets)"
                ),
            },
        )
        try:
            with request_fn(req, timeout=20) as response:
                body = json.loads(response.read())
                link = response.headers.get("Link", "")
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"could not retrieve public organization memberships for {login}"
            ) from exc
        if not isinstance(body, list):
            raise RuntimeError(
                f"invalid public organization response for {login}")
        public_orgs.extend(
            org["login"] for org in body
            if isinstance(org, dict) and org.get("login"))
        if 'rel="next"' not in link:
            break
        page += 1
    return public_orgs


# The identity query deliberately keeps pagination, visibility, and derived
# sets together so the transient membership data cannot leak into snapshots.
# pylint: disable=too-many-locals
def fetch_identity(token, login, gql_fn=None, *, include_environment=True,
                   include_public_orgs=False):
    """Resolve who we are from the token's own account.

    Returns an Identity carrying the canonical login (as GitHub spells it),
    the numeric databaseId, the org list, and the two derived sets.
    ``include_environment=False`` omits additive environment configuration.
    Public-membership acquisition is opt-in for the snapshot producer via
    ``include_public_orgs``; ordinary renderer callers remain GraphQL-only.
    """
    g = gql_fn or gql
    # The GraphQL connection is the authoritative transient membership set;
    # page it fully rather than silently accepting the old first-100 cap.
    org_nodes = []
    cursor = None
    seen_cursors = {None}
    canonical = None
    database_id = None
    while True:
        data = g(token, IDENTITY_QUERY,
                 {"login": login, "cursor": cursor})
        user = data["user"]
        if not user:
            raise RuntimeError(f"no such GitHub user: {login}")
        if canonical is None:
            canonical = user["login"]
            database_id = user.get("databaseId")
        connection = user.get("organizations") or {}
        org_nodes.extend(connection.get("nodes") or [])
        page_info = connection.get("pageInfo")
        if page_info is None or "hasNextPage" not in page_info:
            raise PaginationLimitError(
                "organizations pagination missing pageInfo or hasNextPage "
                "after cursor "
                f"{cursor!r}")
        if not page_info.get("hasNextPage"):
            break
        next_cursor = page_info.get("endCursor")
        if not next_cursor:
            raise PaginationLimitError(
                "organizations pagination stalled after cursor "
                f"{cursor!r}: hasNextPage=true but endCursor is missing")
        if next_cursor in seen_cursors:
            raise PaginationLimitError(
                "organizations pagination stalled after cursor "
                f"{cursor!r}: endCursor {next_cursor!r} repeats an earlier "
                "cursor")
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    orgs = [o["login"] for o in org_nodes]
    public_orgs = []
    if include_public_orgs:
        explicit_visibility = all("isPublic" in node for node in org_nodes)
        if explicit_visibility and org_nodes:
            public_orgs = [node["login"] for node in org_nodes
                           if node.get("isPublic") is True]
        elif g is gql:
            # Only the snapshot producer opts into this unauthenticated view;
            # ordinary identity consumers retain their GraphQL-only behavior.
            public_orgs = fetch_public_organizations(canonical)
        else:
            # A custom injected transport cannot establish visibility without
            # an explicit marker. Fail closed for the serialized view rather
            # than treating authenticated memberships as public.
            public_orgs = [node["login"] for node in org_nodes
                           if node.get("isPublic") is True]
    extras = None if include_environment else ()
    return Identity(
        login=canonical,
        database_id=database_id,
        orgs=orgs,
        insiders=insider_set(canonical, orgs, extra=extras),
        emails=our_emails(canonical, database_id, extra=extras),
        public_orgs=public_orgs,
    )
# pylint: enable=too-many-locals


def is_external(node, insiders):
    """A repo counts as external iff it is public AND its owner is not an
    insider. Private repos are excluded everywhere: they cannot be shown off,
    and a viewer of the SVG cannot verify them.
    """
    r = node["repository"]
    return not r["isPrivate"] and r["owner"]["login"].casefold() not in insiders


# ------------------------------------------------------- PR / issue fetching

# The three timestamps are for render-responsiveness.py, which measures how
# long an external PR sits before it merges. They are selected here rather
# than in a query of its own so that a PR node means the same thing whichever
# renderer last wrote it to the shared cache. All three are immutable once a
# PR is merged, so caching them alongside `merged` is safe; closedAt is
# selected because a node cached while the PR was still open, and later
# inferred merged, has no mergedAt.
PR_QUERY = """
query($login: String!, $cursor: String) {
  user(login: $login) {
    pullRequests(first: 100, after: $cursor,
                 states: %s,
                 orderBy: {field: CREATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id
        number
        url
        merged
        state
        updatedAt
        createdAt
        mergedAt
        closedAt
        repository {
          id
          nameWithOwner
          url
          isPrivate
          owner { login }
        }
      }
    }
  }
}
"""

ISSUE_QUERY = """
query($login: String!, $cursor: String) {
  user(login: $login) {
    issues(first: 100, after: $cursor,
           states: [OPEN, CLOSED],
           orderBy: {field: CREATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id
        number
        url
        createdAt
        updatedAt
        closedAt
        state
        stateReason
        repository {
          id
          nameWithOwner
          url
          isPrivate
          owner { login }
        }
      }
    }
  }
}
"""


# The live/cached/sweep reconciliation is intentionally one function so its
# state transitions remain atomic and easy to audit.
# pylint: disable=too-many-locals
def fetch_pull_requests(token, login, cached_prs=None,
                        max_pages: Optional[int] = 50, gql_fn=None):
    """Fetch the user's authored PRs and merge with the cached set.

    MERGED is the only one-way PR state, so with a warm cache the live query
    covers [OPEN, CLOSED] only and is unioned with the cached set keyed by
    PR id, which guarantees a PR appears exactly once. A PR stays in the live
    half until it merges: merging is the only way to leave the OPEN/CLOSED
    result set, so a previously live PR that is now absent is recorded as
    merged and moves to the cached half. On a cold cache (or --resync) all
    three states are queried and the set is rebuilt.

    The live set alone permanently misses a PR that is opened AND merged
    between two renders: it never appears in [OPEN, CLOSED], so nothing ever
    discovers it (issue #3). A warm run therefore also fetches ONE page of
    the most recent MERGED PRs and unions it into the same keyed map. The
    sweep is deliberately a single page, never paginated: PR_QUERY orders by
    CREATED_AT DESC, so 100 PRs covers everything authored since the last
    render unless more than 100 were authored in between, and paging further
    would walk the entire merged history every run — the cost the cache
    exists to avoid. Known bound: PRs authored beyond that first page since
    the last run are still missed until a --resync.

    There is no server-side "not in these orgs" filter, so callers pull the
    list and filter locally with is_external. ``max_pages`` is an optional
    runaway guard. When set, reaching the limit before the connection ends
    raises ``PaginationLimitError`` rather than returning a partial result.

    Returns (prs, prs_by_id): the union list and the keyed mapping to persist.
    """
    g = gql_fn or gql
    q = PR_QUERY % ("[OPEN, CLOSED]" if cached_prs is not None
                    else "[OPEN, CLOSED, MERGED]")
    live = []
    cursor = None
    seen_cursors = {None}
    pages = 0
    while True:
        if max_pages is not None and pages >= max_pages:
            raise PaginationLimitError(
                f"pullRequests pagination limit {max_pages} reached after "
                f"cursor {cursor!r}")
        page = g(token, q, {"login": login, "cursor": cursor})["user"][
            "pullRequests"]
        pages += 1
        live.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        next_cursor = page["pageInfo"].get("endCursor")
        if not next_cursor:
            raise PaginationLimitError(
                "pullRequests pagination stalled after cursor "
                f"{cursor!r}: hasNextPage=true but endCursor is missing")
        if next_cursor in seen_cursors:
            raise PaginationLimitError(
                "pullRequests pagination stalled after cursor "
                f"{cursor!r}: endCursor {next_cursor!r} repeats an earlier "
                "cursor")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    known = dict(cached_prs or {})
    live_ids = set()
    for node in live:
        live_ids.add(node["id"])
        known[node["id"]] = node
    if cached_prs is not None:
        sweep = g(token, PR_QUERY % "[MERGED]",
                  {"login": login, "cursor": None})["user"]["pullRequests"]
        for node in sweep["nodes"]:
            known[node["id"]] = node
        for pid, node in known.items():
            if not node["merged"] and pid not in live_ids:
                known[pid] = {**node, "merged": True}
    return list(known.values()), known
# pylint: enable=too-many-locals


def fetch_issues(token, login, max_pages: Optional[int] = 50, gql_fn=None):
    """Page through every ISSUE the user has authored, every run.

    Issues can be REOPENED, so unlike PRs there is no immutable slice to
    freeze: the full [OPEN, CLOSED] list is re-paged each run. The GraphQL
    `issues` connection returns issues ONLY (pull requests live under
    `pullRequests`), so there is no PR double-counting to guard against.
    ``max_pages`` is an optional runaway guard. When set, reaching the limit
    before the connection ends raises ``PaginationLimitError`` rather than
    returning a partial result.
    """
    g = gql_fn or gql
    issues = []
    cursor = None
    seen_cursors = {None}
    pages = 0
    while True:
        if max_pages is not None and pages >= max_pages:
            raise PaginationLimitError(
                f"issues pagination limit {max_pages} reached after "
                f"cursor {cursor!r}")
        page = g(token, ISSUE_QUERY, {"login": login, "cursor": cursor})["user"][
            "issues"]
        pages += 1
        issues.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        next_cursor = page["pageInfo"].get("endCursor")
        if not next_cursor:
            raise PaginationLimitError(
                "issues pagination stalled after cursor "
                f"{cursor!r}: hasNextPage=true but endCursor is missing")
        if next_cursor in seen_cursors:
            raise PaginationLimitError(
                "issues pagination stalled after cursor "
                f"{cursor!r}: endCursor {next_cursor!r} repeats an earlier "
                "cursor")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return issues


# ----------------------------------------------------------------- SVG chrome

def fmt_short(n):
    n = int(n)
    if abs(n) < 1000:
        return str(n)
    if abs(n) < 1_000_000:
        return f"{n/1000:.1f}".rstrip("0").rstrip(".") + "k"
    return f"{n/1_000_000:.1f}".rstrip("0").rstrip(".") + "M"


def xml_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;")
            .replace("'", "&apos;"))


def base_card(C, w, h, body):
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" {FONT}>
  <defs>
    <linearGradient id="bgGrad" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{C['bg']}"/>
      <stop offset="1" stop-color="{C['bg2']}"/>
    </linearGradient>
    <pattern id="grain" patternUnits="userSpaceOnUse" width="3" height="3">
      <rect width="3" height="1" fill="white" fill-opacity="0.012"/>
    </pattern>
  </defs>
  <rect width="{w}" height="{h}" rx="8" fill="url(#bgGrad)"/>
  <rect width="{w}" height="{h}" rx="8" fill="url(#grain)"/>
  <rect width="{w}" height="{h}" rx="8" fill="none" stroke="{C['border']}" stroke-width="1"/>
  {body}
</svg>"""


def stamp_cache_notice(C, svg, fetched_at):
    """A card rendered from cache must show it: tag the cache's own fetch
    time bottom-left so staleness is visible rather than silent. Done as a
    post-pass so the renderer signatures stay unchanged."""
    m = re.search(r'height="(\d+)"', svg)
    y = int(m.group(1)) - 8 if m else 12
    note = (f'<text x="10" y="{y}" fill="{C["dim"]}" font-size="10">'
            f'cached data from {xml_escape(fetched_at)}</text>')
    return svg.replace("</svg>", f"  {note}\n</svg>")
