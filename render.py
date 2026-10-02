#!/usr/bin/env python3
"""
gh-widgets — render self-hosted GitHub stat SVGs.

Writes four SVGs to OUT_DIR:
  stats.svg      followers, repos, stars, forks, last-year contributions
  streak.svg     current contribution streak, longest streak, year total
  languages.svg  top 5 languages by bytes of code
  external.svg   PRs opened to repos outside my own account and orgs
                 (how many merged, how many repos) and issues filed there
                 (how many the maintainers accepted, how many repos)

Configuration (env vars or CLI flags, in that order of precedence):
  GH_USER     (required) GitHub username
  GH_TOKEN    (required) Personal access token with `public_repo`. `read:user`
              is NOT needed: nothing here reads the account's email.
              (can also be read from a file via --token-file or GH_TOKEN_FILE)
  GH_EXTRA_ORGS optional, comma-separated organization logins whose public
               repos add to stats; empty by default
  OUT_DIR     where to write the SVGs (default: ./widgets)
  CACHE_FILE  JSON cache of immutable data (default: /var/lib/gh-widgets/cache.json)
  THEME       tokyonight (default) | catppuccin | gruvbox | github-dark

  --resync    discard the cache and refetch everything (wired to run weekly,
              so no correctness claim rests permanently on MERGED being one-way)

The SVGs are static files — serve them from any web server with
`Cache-Control: must-revalidate` or similar, and embed by URL.

Designed to fail gracefully. Settled calendar days and MERGED PRs are
immutable, so they are cached and only the mutable slice (the trailing 7
days, OPEN/CLOSED PRs, issues) is refetched each run — the full-year
calendar query was getting killed with RESOURCE_LIMITS_EXCEEDED, so a
cold cache backfills the year in 12 monthly windowed queries instead.
A run whose fetches still fail renders from the cache, stamps the cache
timestamp on every card, names the acquisition that failed, and exits 0;
with no cache to fall back on it exits non-zero and the existing SVGs keep
serving.

Zero external deps. Pure Python stdlib. Requires Python 3.9+.
"""
import argparse
import importlib.util
import os
import sys
import urllib.error
from calendar import monthrange
from collections import namedtuple
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


def _load_common():
    """Load ghwidgets_common.py from beside this script.

    By path rather than by name: deployment renames this file to
    render-gh-widgets.py, so an `import ghwidgets_common` would depend on the
    script's own filename and on sys.path. This does not.
    """
    path = Path(__file__).resolve().with_name("ghwidgets_common.py")
    if not path.exists():
        raise SystemExit(
            f"error: {path} is missing — it must sit beside this script "
            f"(install.sh copies both; a partial copy is not usable)")
    spec = importlib.util.spec_from_file_location("ghwidgets_common", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"error: cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


common = _load_common()

# The interface version this script was written against. A mismatch means one
# file was copied without the other: fail loudly here rather than render
# wrong numbers from a stale module.
REQUIRED_COMMON = 10
common.check_version(REQUIRED_COMMON)

# Re-exported so this module's surface is unchanged for callers and tests.
# Internal call sites resolve these through module globals, which keeps them
# patchable (the test suite swaps out `gql`).
FONT = common.FONT
THEMES = common.THEMES
gql = common.gql
save_cache = common.save_cache
fmt_short = common.fmt_short
xml_escape = common.xml_escape
xml_color = common.xml_color
atomic_write_text = common.atomic_write_text
base_card = common.base_card
stamp_cache_notice = common.stamp_cache_notice
CacheFallback = common.CacheFallback

# v3: the cached user payload gains extra-organization stats; v2 caches must
# be discarded and refetched rather than trusted.
CACHE_VERSION = 3
DEFAULT_CACHE_FILE = "/var/lib/gh-widgets/cache.json"


def load_cache(path):
    """Read the JSON cache, checked against THIS script's schema version.

    (The impact renderer keeps its own cache and its own version, which is why
    the shared loader takes the version as a parameter.)
    """
    return common.load_cache(path, CACHE_VERSION)


def cache_complete(cache):
    """The durability fallback can render only if every input is cached."""
    return all(k in cache
               for k in ("fetched_at", "user", "calendar_days", "prs", "issues"))


def one_year_ago(now):
    """The trailing-year boundary date: this date one year back
    (Feb 29 -> Feb 28). Shared by prune_days and the cold backfill so the
    fetched period and the kept period are the same by construction."""
    try:
        return now.date().replace(year=now.year - 1)
    except ValueError:  # Feb 29 -> Feb 28
        return now.date().replace(year=now.year - 1, day=28)


def prune_days(days, now):
    """Drop days older than the trailing year so the merged history matches
    what a full calendar fetch (GitHub's default one-year window) returns."""
    cutoff = one_year_ago(now)
    return {d: c for d, c in days.items() if date.fromisoformat(d) >= cutoff}


def calendar_from_days(days):
    """Rebuild the contributionCalendar structure the renderers consume
    ({totalContributions, weeks}) from a date -> count map. Weeks start on
    Sunday, matching GitHub's payload; compute_streak only reads the days."""
    weeks = []
    week = None
    for d in sorted(days):
        if week is None or date.fromisoformat(d).weekday() == 6:
            week = {"contributionDays": []}
            weeks.append(week)
        week["contributionDays"].append(
            {"date": d, "contributionCount": days[d]})
    return {"totalContributions": sum(days.values()), "weeks": weeks}


def monthly_windows(now):
    """Split the trailing year into 12 adjacent monthly (from, to) windows
    for the cold-cache backfill. The first window starts at the same
    boundary prune_days keeps from (this date one year back, at midnight)
    and the last ends at `now`; interior boundaries fall on the same
    day-of-month as the start, clamped to the month's length. Consecutive
    windows share the boundary instant, so under GitHub's to-exclusive
    counting ("only contributions made before `to`") every contribution
    lands in exactly one window and no date is skipped or duplicated."""
    start = one_year_ago(now)
    bounds = []
    for i in range(13):
        m = start.month - 1 + i
        y, m = start.year + m // 12, m % 12 + 1
        bounds.append(datetime(y, m, min(start.day, monthrange(y, m)[1]),
                               tzinfo=now.tzinfo))
    bounds[-1] = now  # the old full-year query also counted up to "now"
    return list(zip(bounds, bounds[1:]))


# The account's own profile: who it is, how many followers, and every org
# it belongs to. `pageInfo` is requested so the membership can be walked to
# its end — the first 100 memberships are not all of them, and an org left
# out of this list classifies the account's own work in that org as external.
ORG_QUERY = """
query($login: String!, $cursor: String) {
  user(login: $login) {
    login
    name
    followers { totalCount }
    organizations(first: 100, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes { login }
    }
  }
}
"""

# The repos×languages half, paged for the same reason. The filters are
# unchanged and load-bearing: owned-only, non-fork, public — they keep
# private repository names out of a public SVG.
REPO_QUERY = """
query($login: String!, $cursor: String) {
  user(login: $login) {
    repositories(first: 100, after: $cursor, ownerAffiliations: OWNER,
                 isFork: false, privacy: PUBLIC) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        stargazerCount
        forkCount
        languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name color } }
        }
      }
    }
  }
}
"""

# Extra organization repositories contribute to stats only. They use the same
# public, non-fork filter as the user's own repositories and omit languages so
# they cannot change the languages card.
ORG_REPO_QUERY = """
query($login: String!, $cursor: String) {
  organization(login: $login) {
    repositories(first: 100, after: $cursor, isFork: false, privacy: PUBLIC) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes { stargazerCount forkCount }
    }
  }
}
"""

# A runaway guard, the same bound fetch_pull_requests and fetch_issues use:
# 50 pages of 100 is 5000 repositories (or memberships), past any real
# account or named organization. Reaching it raises rather than returning a
# partial profile, because truncation silently under-reports the account.
PROFILE_MAX_PAGES = 50


def page_profile_connection(token, query, field, login, max_pages):
    """Walk one paginated ``user`` connection to its last page.

    Returns ``(user, connection)``: the ``user`` payload of the first page
    (login, name, followers) and the connection with every page's nodes
    concatenated and ``pageInfo`` dropped, so the merged result has exactly
    the shape the renderers and the cache already consume.

    A missing ``pageInfo``, a ``hasNextPage`` without an ``endCursor``, and
    a cursor that repeats an earlier one all raise ``PaginationLimitError``:
    each means the walk cannot prove it reached the end, and returning what
    did arrive is precisely the silent truncation this replaces.
    """
    nodes = []
    head = None
    cursor = None
    seen_cursors = {None}
    pages = 0
    while True:
        if max_pages is not None and pages >= max_pages:
            raise common.PaginationLimitError(
                f"{field} pagination limit {max_pages} reached after cursor "
                f"{cursor!r}")
        user = gql(token, query, {"login": login, "cursor": cursor})["user"]
        pages += 1
        connection = user.get(field) or {}
        if head is None:
            head = user
            head[field] = {k: v for k, v in connection.items()
                           if k != "pageInfo"}
        nodes.extend(connection.get("nodes") or [])
        page_info = connection.get("pageInfo")
        if page_info is None or "hasNextPage" not in page_info:
            raise common.PaginationLimitError(
                f"{field} pagination missing pageInfo or hasNextPage after "
                f"cursor {cursor!r}")
        if not page_info.get("hasNextPage"):
            break
        next_cursor = page_info.get("endCursor")
        if not next_cursor:
            raise common.PaginationLimitError(
                f"{field} pagination stalled after cursor {cursor!r}: "
                "hasNextPage=true but endCursor is missing")
        if next_cursor in seen_cursors:
            raise common.PaginationLimitError(
                f"{field} pagination stalled after cursor {cursor!r}: "
                f"endCursor {next_cursor!r} repeats an earlier cursor")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    head[field]["nodes"] = nodes
    return head, head[field]


def page_organization_repositories(
        token, login, max_pages=PROFILE_MAX_PAGES):
    """Return every public, non-fork repository in one named organization.

    The organization connection uses the same pagination safety checks as the
    user profile connections. A missing organization is an error so an
    inaccessible configured login cannot silently undercount the card.
    """
    nodes = []
    head = None
    cursor = None
    seen_cursors = {None}
    pages = 0
    while True:
        if max_pages is not None and pages >= max_pages:
            raise common.PaginationLimitError(
                f"organization {login!r} repositories pagination limit "
                f"{max_pages} reached after cursor {cursor!r}")
        response = gql(token, ORG_REPO_QUERY,
                       {"login": login, "cursor": cursor})
        organization = response.get("organization")
        if organization is None:
            raise RuntimeError(
                f"organization {login!r} was not returned by GitHub")
        connection = organization.get("repositories") or {}
        pages += 1
        if head is None:
            head = {k: v for k, v in connection.items() if k != "pageInfo"}
        nodes.extend(connection.get("nodes") or [])
        page_info = connection.get("pageInfo")
        if page_info is None or "hasNextPage" not in page_info:
            raise common.PaginationLimitError(
                f"organization {login!r} repositories pagination missing "
                f"pageInfo or hasNextPage after cursor {cursor!r}")
        if not page_info.get("hasNextPage"):
            break
        next_cursor = page_info.get("endCursor")
        if not next_cursor:
            raise common.PaginationLimitError(
                f"organization {login!r} repositories pagination stalled "
                f"after cursor {cursor!r}: hasNextPage=true but endCursor "
                "is missing")
        if next_cursor in seen_cursors:
            raise common.PaginationLimitError(
                f"organization {login!r} repositories pagination stalled "
                f"after cursor {cursor!r}: endCursor {next_cursor!r} "
                "repeats an earlier cursor")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    head["nodes"] = nodes
    return head


def extra_org_logins():
    """Read GH_EXTRA_ORGS, preserving first spelling and order on dedupe."""
    logins = []
    seen = set()
    for login in common.env_list("GH_EXTRA_ORGS"):
        key = login.casefold()
        if key not in seen:
            seen.add(key)
            logins.append(login)
    return logins


def fetch_extra_org_stats(token, org_logins,
                          max_pages=PROFILE_MAX_PAGES):
    """Fetch the public repository count, stars, and forks for each org."""
    total_count = 0
    total_stars = 0
    total_forks = 0
    for org_login in org_logins:
        repositories = page_organization_repositories(
            token, org_login, max_pages)
        total_count += repositories["totalCount"]
        total_stars += sum(repo["stargazerCount"]
                           for repo in repositories["nodes"])
        total_forks += sum(repo["forkCount"]
                           for repo in repositories["nodes"])
    return {
        "orgs": org_logins,
        "totalCount": total_count,
        "totalStars": total_stars,
        "totalForks": total_forks,
    }


def fetch(token, login, cached_days=None, max_pages=PROFILE_MAX_PAGES):
    # The repos×languages core and the contribution calendar are fetched
    # separately: GitHub's GraphQL node-limit estimator started rejecting
    # the combined query with RESOURCE_LIMITS_EXCEEDED. Splitting the calendar into its
    # own request was not enough — the full-year calendar query alone now
    # trips the limit — so the calendar is always fetched windowed and
    # merged into a date -> count map: with a warm cache, a single
    # trailing 7-day window (the only days that can still move) merged
    # onto the cached history; on a cold cache (or --resync), 12
    # sequential monthly windows backfilling the whole year, each far
    # below the node limit, so bootstrap is deterministic instead of
    # lucky. The merged structure keeps the exact shape the renderers
    # already consume. Returns (user, days); `days` is the merged
    # date -> count map to persist in the cache.
    #
    # The profile is two paginated connections rather than one query, because
    # each is walked to its own end (issue #51) and a combined query would
    # re-send the whole of one connection on every page of the other.
    # The helper returns the `user` payload with the connection it walked
    # already merged into it, so organizations is in place; ORG_QUERY's payload
    # carries no repositories key at all, which is why the second walk's
    # result is grafted on rather than merely read.
    user, _orgs = page_profile_connection(
        token, ORG_QUERY, "organizations", login, max_pages)
    _, repos = page_profile_connection(
        token, REPO_QUERY, "repositories", login, max_pages)
    user["repositories"] = repos
    user["extraOrgStats"] = fetch_extra_org_stats(
        token, extra_org_logins(), max_pages)
    now = datetime.now(timezone.utc)
    q_window = """
    query($login: String!, $from: DateTime!, $to: DateTime!) {
      user(login: $login) {
        contributionsCollection(from: $from, to: $to) {
          contributionCalendar {
            totalContributions
            weeks { contributionDays { date contributionCount } }
          }
        }
      }
    }
    """
    if cached_days is None:
        # Cold cache (or --resync): backfill the trailing year in 12
        # monthly windows (spec change 3b) instead of one full-year query.
        # Windows share midnight boundaries and are merged oldest-first,
        # so a boundary day reported by two windows keeps the newer
        # window's count and no day is skipped.
        days = {}
        for from_, to in monthly_windows(now):
            cal = gql(token, q_window, {
                "login": login,
                "from": from_.isoformat(timespec="seconds"),
                "to": to.isoformat(timespec="seconds"),
            })["user"]["contributionsCollection"]["contributionCalendar"]
            for w in cal["weeks"]:
                for d in w["contributionDays"]:
                    days[d["date"]] = d["contributionCount"]
    else:
        cal = gql(token, q_window, {
            "login": login,
            "from": (now - timedelta(days=7)).isoformat(timespec="seconds"),
            "to": now.isoformat(timespec="seconds"),
        })["user"]["contributionsCollection"]["contributionCalendar"]
        days = dict(cached_days)
        for w in cal["weeks"]:
            for d in w["contributionDays"]:
                days[d["date"]] = d["contributionCount"]
    days = prune_days(days, now)
    user["contributionsCollection"] = {
        "contributionCalendar": calendar_from_days(days)}
    return user, days


PR_QUERY = common.PR_QUERY


def fetch_pull_requests(token, login, cached_prs=None, max_pages=50):
    """Thin wrapper over the shared implementation, passing THIS module's
    `gql` so the test suite's patch of it still intercepts the calls."""
    return common.fetch_pull_requests(token, login, cached_prs, max_pages,
                                      gql_fn=gql)


def fetch_issues(token, login, max_pages=50):
    """Thin wrapper over the shared implementation — see fetch_pull_requests."""
    return common.fetch_issues(token, login, max_pages, gql_fn=gql)


def external_contributions(prs, login, orgs):
    """Count PRs to repos owned by neither the user nor any org they belong to.

    Private repos are excluded: they can't be shown off, and a viewer of the
    SVG can't verify them. The insider predicate is shared with the impact
    renderer so the two cards cannot disagree about what "external" means.
    """
    insiders = common.insider_set(login, orgs)
    opened = merged = 0
    repos = set()
    for pr in prs:
        if not common.is_external(pr, insiders):
            continue
        opened += 1
        merged += bool(pr["merged"])
        repos.add(pr["repository"]["nameWithOwner"])
    return opened, merged, len(repos)


def external_issues(issues, login, orgs):
    """Count issues filed in repos owned by neither the user nor any org they
    belong to. Same external predicate as external_contributions: private
    repos excluded, insiders (self + orgs) excluded.

    Returns (opened, accepted, repos), mirroring external_contributions:
    total external issues authored, how many the MAINTAINER closed as
    completed (state CLOSED + stateReason COMPLETED — the issue analog of a
    merged PR, as opposed to NOT_PLANNED closures), and how many distinct
    external repos they touched. "maintainer-accepted" names the maintainer
    closing the report as done; it does not claim the author did the fixing.
    """
    insiders = common.insider_set(login, orgs)
    opened = accepted = 0
    repos = set()
    for issue in issues:
        if not common.is_external(issue, insiders):
            continue
        opened += 1
        repos.add(issue["repository"]["nameWithOwner"])
        if issue["state"] == "CLOSED" and issue["stateReason"] == "COMPLETED":
            accepted += 1
    return opened, accepted, len(repos)


def compute_streak(weeks):
    """Walk newest->oldest; skip at most ONE leading zero (today may not be
    logged yet, or the calendar's UTC day is ahead of yours), then count
    consecutive non-zero days. Also returns longest streak."""
    days = []
    for w in weeks:
        for d in w["contributionDays"]:
            days.append((d["date"], d["contributionCount"]))
    days.sort()
    counts = [c for _, c in reversed(days)]
    # Exactly one zero is forgiven, and only at the newest end. A second zero
    # is a real gap: the streak ended, so the current streak is 0.
    if counts and counts[0] == 0:
        counts = counts[1:]
    current = 0
    for count in counts:
        if count == 0:
            break
        current += 1
    longest = 0
    run = 0
    for _, count in days:
        if count > 0:
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return current, longest


def aggregate_languages(repos):
    totals = {}
    colors = {}
    for r in repos:
        for e in r["languages"]["edges"]:
            name = e["node"]["name"]
            totals[name] = totals.get(name, 0) + e["size"]
            colors[name] = e["node"]["color"] or "#888888"
    grand = sum(totals.values()) or 1
    return sorted(((n, s, s / grand * 100, colors[n])
                   for n, s in totals.items()),
                  key=lambda x: -x[1])


def render_stats(C, user, total_repos, total_stars, total_forks,
                 year_contribs, orgs_counted=False):
    name = user.get("name") or user["login"]
    body = f"""
  <text x="20" y="34" fill="{C['blue']}" font-size="16" font-weight="600">{xml_escape(name)}</text>
  <text x="20" y="52" fill="{C['dim']}" font-size="11">@{xml_escape(user['login'])} · github stats</text>
  <line x1="20" y1="64" x2="400" y2="64" stroke="{C['border']}"/>
  <g font-size="13">
    <text x="20"  y="92"  fill="{C['dim']}">followers</text>
    <text x="200" y="92"  fill="{C['gold']}" font-weight="500">{user['followers']['totalCount']}</text>
    <text x="20"  y="115" fill="{C['dim']}">public repos</text>
    <text x="200" y="115" fill="{C['gold']}" font-weight="500">{total_repos}</text>
    <text x="20"  y="138" fill="{C['dim']}">stars received</text>
    <text x="200" y="138" fill="{C['gold']}" font-weight="500">{total_stars}</text>
    <text x="20"  y="161" fill="{C['dim']}">forks received</text>
    <text x="200" y="161" fill="{C['gold']}" font-weight="500">{total_forks}</text>
    <text x="20"  y="184" fill="{C['dim']}">contributions (1y)</text>
    <text x="200" y="184" fill="{C['gold']}" font-weight="500">{year_contribs:,}</text>
  </g>"""
    desc = (f"Followers: {user['followers']['totalCount']}; public repositories: "
            f"{total_repos}; stars received: "
            f"{total_stars}; forks received: {total_forks}; contributions "
            f"in the last year: {year_contribs:,}.")
    if orgs_counted:
        desc += (" Followers count the account only; organization followers "
                 "are not included.")
    return base_card(C, 420, 203, body, card="stats", title="GitHub stats",
                     desc=desc)


def render_streak(C, current, longest, total):
    col_centers = (84, 210, 336)
    sep_xs = (147, 273)

    def big(x_center, label, value, color):
        return f"""
    <text x="{x_center}" y="112" text-anchor="middle" fill="{color}" font-size="36" font-weight="600">{value}</text>
    <text x="{x_center}" y="138" text-anchor="middle" fill="{C['dim']}" font-size="11">{label}</text>"""

    body = f"""
  <text x="20" y="34" fill="{C['purple']}" font-size="14" font-weight="600">contribution streak</text>
  <text x="20" y="52" fill="{C['dim']}" font-size="11">rolling 365 days</text>
  <line x1="20" y1="64" x2="400" y2="64" stroke="{C['border']}"/>
  {big(col_centers[0], 'current', fmt_short(current), C['green'])}
  {big(col_centers[1], 'longest', fmt_short(longest), C['pink'])}
  {big(col_centers[2], 'total',   fmt_short(total),   C['cyan'])}
  <line x1="{sep_xs[0]}" y1="90" x2="{sep_xs[0]}" y2="135" stroke="{C['border']}"/>
  <line x1="{sep_xs[1]}" y1="90" x2="{sep_xs[1]}" y2="135" stroke="{C['border']}"/>"""
    desc = (f"Current streak: {current} days; longest streak: {longest} days; "
            f"total contributions: {total:,}.")
    return base_card(C, 420, 170, body, card="streak",
                     title="Contribution streak", desc=desc)


def render_external(C, pr_opened, pr_merged, pr_repos,
                    iss_opened, iss_accepted, iss_repos):
    col_centers = (84, 210, 336)
    sep_xs = (147, 273)
    merge_rate = f"{pr_merged / pr_opened * 100:.0f}% merged" if pr_opened else "no external PRs yet"
    accept_rate = (f"{iss_accepted / iss_opened * 100:.0f}% maintainer-accepted"
                   if iss_opened else "no external issues yet")
    footer = f"{merge_rate}  ·  {accept_rate}"

    def big(x_center, label, value, color, vy):
        return f"""
    <text x="{x_center}" y="{vy}" text-anchor="middle" fill="{color}" font-size="36" font-weight="600">{value}</text>
    <text x="{x_center}" y="{vy + 26}" text-anchor="middle" fill="{C['dim']}" font-size="11">{label}</text>"""

    def seps(y1, y2):
        # Bracket only the big numbers, ending well above the label row so a
        # wide label (e.g. "maintainer-accepted") never crosses a divider.
        return f"""
  <line x1="{sep_xs[0]}" y1="{y1}" x2="{sep_xs[0]}" y2="{y2}" stroke="{C['border']}"/>
  <line x1="{sep_xs[1]}" y1="{y1}" x2="{sep_xs[1]}" y2="{y2}" stroke="{C['border']}"/>"""

    body = f"""
  <text x="20" y="34" fill="{C['gold']}" font-size="14" font-weight="600">external contributions</text>
  <text x="20" y="52" fill="{C['dim']}" font-size="11">to repos outside my own account and orgs</text>
  <line x1="20" y1="64" x2="400" y2="64" stroke="{C['border']}"/>
  <text x="20" y="86" fill="{C['dim']}" font-size="11" font-weight="600">pull requests</text>
  {big(col_centers[0], 'opened', fmt_short(pr_opened), C['blue'], 124)}
  {big(col_centers[1], 'merged', fmt_short(pr_merged), C['green'], 124)}
  {big(col_centers[2], 'repos', fmt_short(pr_repos), C['purple'], 124)}
  {seps(98, 136)}
  <text x="20" y="182" fill="{C['dim']}" font-size="11" font-weight="600">issues</text>
  {big(col_centers[0], 'opened', fmt_short(iss_opened), C['blue'], 220)}
  {big(col_centers[1], 'maintainer-accepted', fmt_short(iss_accepted), C['green'], 220)}
  {big(col_centers[2], 'repos', fmt_short(iss_repos), C['purple'], 220)}
  {seps(194, 232)}
  <line x1="20" y1="262" x2="400" y2="262" stroke="{C['border']}"/>
  <text x="210" y="282" text-anchor="middle" fill="{C['dim']}" font-size="11">{footer}</text>"""
    return base_card(C, 420, 298, body, card="external",
                     title="External contributions",
                     desc=(f"Pull requests: {pr_opened} opened, "
                           f"{pr_merged} merged, {pr_repos} repos "
                           f"({merge_rate}); issues: {iss_opened} opened, "
                           f"{iss_accepted} maintainer-accepted, "
                           f"{iss_repos} repos ({accept_rate})."))


def language_legend(C, top):
    """One legend row per language: color swatch, name, share percent."""
    legend = []
    ly = 124
    for name, _, pct, color in top:
        legend.append(
            f'<rect x="20" y="{ly-9}" width="9" height="9" fill="{xml_color(color)}"/>'
            f'<text x="34" y="{ly}" fill="{C["fg"]}" font-size="12">{xml_escape(name)}</text>'
            f'<text x="400" y="{ly}" text-anchor="end" fill="{C["dim"]}" font-size="11">{pct:.1f}%</text>'
        )
        ly += 18
    return "".join(legend)


def render_languages(C, langs):
    top = langs[:5]
    pct_sum = sum(p for _, _, p, _ in top) or 1
    bar_x, bar_w, y = 20, 380, 88
    rects = []
    rect_x = bar_x
    for _, _, pct, color in top:
        seg = (pct / pct_sum) * bar_w
        rects.append(f'<rect x="{rect_x:.1f}" y="{y}" width="{seg:.1f}" height="10" fill="{xml_color(color)}"/>')
        rect_x += seg
    body = f"""
  <text x="20" y="34" fill="{C['cyan']}" font-size="14" font-weight="600">top languages</text>
  <text x="20" y="52" fill="{C['dim']}" font-size="11">by bytes of code, public repos</text>
  <line x1="20" y1="64" x2="400" y2="64" stroke="{C['border']}"/>
  <rect x="{bar_x}" y="{y}" width="{bar_w}" height="10" rx="2" fill="{C['border']}"/>
  {''.join(rects)}
  {language_legend(C, top)}"""
    return base_card(C, 420, 230, body, card="languages",
                     title="Top languages",
                     desc=(f"Top language: {top[0][0]} "
                           f"({top[0][2]:.1f}% of code bytes)."
                           if top else "No language data available."))


def parse_args():
    p = argparse.ArgumentParser(description="Render self-hosted GitHub stat SVGs.")
    p.add_argument("--user", default=os.environ.get("GH_USER"),
                   help="GitHub username (env: GH_USER)")
    p.add_argument("--token", default=os.environ.get("GH_TOKEN"),
                   help="GitHub PAT (env: GH_TOKEN)")
    p.add_argument("--token-file", default=os.environ.get("GH_TOKEN_FILE"),
                   help="Read token from a file (env: GH_TOKEN_FILE)")
    p.add_argument("--out-dir", default=os.environ.get("OUT_DIR", "./widgets"),
                   help="Where to write SVGs (env: OUT_DIR)")
    p.add_argument("--theme", default=os.environ.get("THEME", "tokyonight"),
                   choices=list(THEMES),
                   help="Color theme (env: THEME)")
    p.add_argument("--resync", action="store_true",
                   help="Discard the cache and refetch everything")
    args = p.parse_args()

    if not args.user:
        p.error("--user (or env GH_USER) is required")
    token = args.token
    if not token and args.token_file:
        token = Path(args.token_file).read_text(encoding="utf-8").strip()
    if not token:
        p.error("--token, --token-file, GH_TOKEN, or GH_TOKEN_FILE required")
    return args, token


def load_inputs(args, token, cache_file):
    """Fetch (or, on failure, recover from the cache) every input the cards
    need. Returns (user, prs, issues, fallback), where `fallback` is None
    after a successful fetch and otherwise carries the cache timestamp the
    cards are drawn from plus the acquisition that failed."""
    cache = {} if args.resync else load_cache(cache_file)
    # `phase` is set to the acquisition about to run, so the except clause
    # knows which one raised without having to infer it after the fact.
    phase = "fetch"
    try:
        user, days = fetch(token, args.user, cache.get("calendar_days") or None)
        phase = "fetch_pull_requests"
        prs, prs_by_id = fetch_pull_requests(token, args.user, cache.get("prs") or None)
        phase = "fetch_issues"
        issues = fetch_issues(token, args.user)
    except Exception as exc:
        # Durability layer: a failed fetch (after gql's retries) renders from
        # cache and exits 0 — but only with a complete cache. Without one,
        # exiting non-zero is still correct.
        if not cache_complete(cache):
            raise
        fallback = CacheFallback(cache["fetched_at"], phase, exc)
        user = dict(cache["user"])
        user["contributionsCollection"] = {
            "contributionCalendar": calendar_from_days(cache["calendar_days"])}
        prs = list(cache["prs"].values())
        issues = cache["issues"]
    else:
        save_cache(cache_file, {
            "version": CACHE_VERSION,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "user": {k: v for k, v in user.items() if k != "contributionsCollection"},
            "calendar_days": days,
            "prs": prs_by_id,
            "issues": issues,
        })
        fallback = None
    return user, prs, issues, fallback


ExternalCounts = namedtuple(
    "ExternalCounts",
    "pr_opened pr_merged pr_repos iss_opened iss_accepted iss_repos")


def external_counts(user, prs, issues):
    """The six external-contribution figures: PR opened/merged/repos, then
    issue opened/accepted/repos."""
    orgs = [o["login"] for o in user["organizations"]["nodes"]]
    opened, merged, repos = external_contributions(prs, user["login"], orgs)
    iss_opened, iss_accepted, iss_repos = external_issues(
        issues, user["login"], orgs)
    return ExternalCounts(opened, merged, repos,
                          iss_opened, iss_accepted, iss_repos)


def build_svgs(C, user, prs, issues):
    """Render the four cards. Returns (svgs, summary), where summary carries
    the figures the final log line reports."""
    extra_org_stats = user.get("extraOrgStats") or {}
    total_repos = (user["repositories"]["totalCount"]
                   + extra_org_stats.get("totalCount", 0))
    total_stars = (sum(r["stargazerCount"]
                       for r in user["repositories"]["nodes"])
                   + extra_org_stats.get("totalStars", 0))
    total_forks = (sum(r["forkCount"] for r in user["repositories"]["nodes"])
                   + extra_org_stats.get("totalForks", 0))
    cal = user["contributionsCollection"]["contributionCalendar"]
    year_contribs = cal["totalContributions"]
    current, longest = compute_streak(cal["weeks"])
    langs = aggregate_languages(user["repositories"]["nodes"])
    ext = external_counts(user, prs, issues)
    svgs = {
        "stats.svg": render_stats(
            C, user, total_repos, total_stars, total_forks, year_contribs,
            bool(extra_org_stats.get("orgs"))),
        "streak.svg": render_streak(C, current, longest, year_contribs),
        "languages.svg": render_languages(C, langs),
        "external.svg": render_external(C, *ext),
    }
    return svgs, (total_stars, total_forks, year_contribs, current, longest, ext)


def write_cards(C, out, user, prs, issues, fallback):
    """Write the four SVGs (stamping the cache fetch time on each when
    rendering from cache) plus last-updated.txt, and print the summary line."""
    svgs, (total_stars, total_forks, year_contribs, current, longest, ext) = \
        build_svgs(C, user, prs, issues)
    for name, svg in svgs.items():
        if fallback:
            svg = stamp_cache_notice(C, svg, fallback.fetched_at)
        atomic_write_text(out / name, svg)
    atomic_write_text(
        out / "last-updated.txt",
        (fallback.fetched_at if fallback
         else datetime.now(timezone.utc).isoformat(timespec="seconds")))

    if fallback:
        print(f"fetch failed at {fallback.phase}: {fallback.message}; "
              f"rendered {out}/{{stats,streak,languages,external}}.svg from "
              f"cache (fetched_at={fallback.fetched_at})")
    else:
        print(f"wrote {out}/{{stats,streak,languages,external}}.svg "
              f"(stars={total_stars} forks={total_forks} contribs={year_contribs} "
              f"streak={current}/{longest} "
              f"external={ext.pr_merged}/{ext.pr_opened} in {ext.pr_repos} repos "
              f"issues={ext.iss_accepted}/{ext.iss_opened} in {ext.iss_repos} repos)")


def main():
    args, token = parse_args()

    C = THEMES[args.theme]
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    cache_file = os.environ.get("CACHE_FILE", DEFAULT_CACHE_FILE)
    user, prs, issues, fallback = load_inputs(args, token, cache_file)
    write_cards(C, out, user, prs, issues, fallback)


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode(errors='replace')}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
