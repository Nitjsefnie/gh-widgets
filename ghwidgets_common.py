#!/usr/bin/env python3
"""
gh-widgets — code shared by render.py and render-impact.py.

This module exists because the two renderers were carrying ~200 lines of
byte-identical code (theme table, GraphQL client, PR/issue paging, cache
helpers, SVG chrome) that drifted independently. It also owns the identity
model both scripts need: who counts as an insider, and which commit-author
addresses count as ours.

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
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import namedtuple
from pathlib import Path

# Bumped whenever this module's interface changes in a way that would make an
# older script misbehave against it. Each script pins the version it expects.
COMMON_VERSION = 1


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
    except ValueError:
        raise SystemExit(f"error: {name}={raw!r} is not a number")


# --------------------------------------------------------------------- cache

def load_cache(path, version):
    """Read the JSON cache. A missing, unreadable, corrupt, or
    schema-mismatched cache is not an error: it degrades to a full fetch."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != version:
        return {}
    return data


def save_cache(path, payload):
    """Best-effort atomic write: temp file in the same directory, then
    os.replace onto the target. A failed save must not fail the run."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, path)
    except Exception as e:
        print(f"warning: could not write cache {path}: {e}", file=sys.stderr)


# ------------------------------------------------------------------- GraphQL

def gql(token, query, variables=None, retries=3, timeout=20):
    """POST a GraphQL query, retrying transient server-side conditions.

    GitHub intermittently answers with RESOURCE_LIMITS_EXCEEDED under load
    (observed 2026-07-18: four hourly runs red, then the identical query
    succeeded minutes later). Transient conditions get a few retries; real
    errors fail fast.
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

Identity = namedtuple("Identity", "login database_id orgs insiders emails")

IDENTITY_QUERY = """
query($login: String!) {
  user(login: $login) {
    login
    databaseId
    organizations(first: 100) { nodes { login } }
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


def fetch_identity(token, login, gql_fn=None):
    """Resolve who we are from the token's own account.

    Returns an Identity carrying the canonical login (as GitHub spells it),
    the numeric databaseId, the org list, and the two derived sets.
    """
    data = (gql_fn or gql)(token, IDENTITY_QUERY, {"login": login})
    user = data["user"]
    if not user:
        raise RuntimeError(f"no such GitHub user: {login}")
    orgs = [o["login"] for o in user["organizations"]["nodes"]]
    canonical = user["login"]
    return Identity(
        login=canonical,
        database_id=user.get("databaseId"),
        orgs=orgs,
        insiders=insider_set(canonical, orgs),
        emails=our_emails(canonical, user.get("databaseId")),
    )


def is_external(node, insiders):
    """A repo counts as external iff it is public AND its owner is not an
    insider. Private repos are excluded everywhere: they cannot be shown off,
    and a viewer of the SVG cannot verify them.
    """
    r = node["repository"]
    return not r["isPrivate"] and r["owner"]["login"].casefold() not in insiders


# ------------------------------------------------------- PR / issue fetching

PR_QUERY = """
query($login: String!, $cursor: String) {
  user(login: $login) {
    pullRequests(first: 100, after: $cursor,
                 states: %s,
                 orderBy: {field: CREATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id
        merged
        repository { nameWithOwner isPrivate owner { login } }
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
        state
        stateReason
        repository { nameWithOwner isPrivate owner { login } }
      }
    }
  }
}
"""


def fetch_pull_requests(token, login, cached_prs=None, max_pages=50, gql_fn=None):
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
    list and filter locally with is_external. max_pages is a runaway guard,
    not a real limit.

    Returns (prs, prs_by_id): the union list and the keyed mapping to persist.
    """
    g = gql_fn or gql
    states = "[OPEN, CLOSED]" if cached_prs is not None else "[OPEN, CLOSED, MERGED]"
    q = PR_QUERY % states
    live = []
    cursor = None
    for _ in range(max_pages):
        page = g(token, q, {"login": login, "cursor": cursor})["user"]["pullRequests"]
        live.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
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


def fetch_issues(token, login, max_pages=50, gql_fn=None):
    """Page through every ISSUE the user has authored, every run.

    Issues can be REOPENED, so unlike PRs there is no immutable slice to
    freeze: the full [OPEN, CLOSED] list is re-paged each run. The GraphQL
    `issues` connection returns issues ONLY (pull requests live under
    `pullRequests`), so there is no PR double-counting to guard against.
    max_pages is a runaway guard, not a real limit.
    """
    g = gql_fn or gql
    issues = []
    cursor = None
    for _ in range(max_pages):
        page = g(token, ISSUE_QUERY, {"login": login, "cursor": cursor})["user"]["issues"]
        issues.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
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
