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
timestamp on every card, and exits 0; with no cache to fall back on it
exits non-zero and the existing SVGs keep serving.

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

# The three entry points intentionally carry parallel snapshot adapters so
# each remains independently installable.
# pylint: disable=duplicate-code


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


def _load_data():
    """Load the public snapshot module from beside this renderer."""
    path = Path(__file__).resolve().with_name("ghwidgets_data.py")
    if not path.exists():
        raise SystemExit(
            f"error: {path} is missing — it must sit beside this script "
            f"(install.sh copies all public data modules)")
    spec = importlib.util.spec_from_file_location("ghwidgets_data", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"error: cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


common = _load_common()
data = _load_data()

# The interface version this script was written against. A mismatch means one
# file was copied without the other: fail loudly here rather than render
# wrong numbers from a stale module.
REQUIRED_COMMON = 3
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
base_card = common.base_card
stamp_cache_notice = common.stamp_cache_notice

# v2: the core query gained forkCount; a v1 cache has no fork data, so it
# must be discarded and refetched rather than trusted.
CACHE_VERSION = 2
DEFAULT_CACHE_FILE = "/var/lib/gh-widgets/cache.json"


def load_cache(path):
    """Read the JSON cache, checked against THIS script's schema version.

    (The impact renderer keeps its own cache and its own version, which is why
    the shared loader takes the version as a parameter.)
    """
    return common.load_cache(path, CACHE_VERSION)


def load_snapshot(path):
    """Load a public snapshot with renderer-specific missing-section errors."""
    try:
        return data.load_snapshot(path)
    except data.SnapshotValidationError as exc:
        prefix = "snapshot missing required field(s): "
        message = str(exc)
        if message.startswith(prefix):
            missing = message[len(prefix):].split(", ")
            for section in ("account", "insiders", "repositories", "issues",
                            "pull_requests"):
                if section in missing:
                    raise ValueError(
                        f"snapshot missing required section: {section}") from exc
        raise


def _require_snapshot_sections(snapshot, sections):
    """Fail before any fetch when render-only input is incomplete."""
    for section in sections:
        if section not in snapshot:
            raise ValueError(f"snapshot missing required section: {section}")


def _snapshot_repository(node):
    """Adapt a normalized public repository to the renderer's API shape."""
    name = node.get("nameWithOwner", node.get("repository", ""))
    owner = node.get("owner", {})
    if isinstance(owner, str):
        owner = {"login": owner}
    owner = dict(owner)
    owner.setdefault("login", name.split("/", 1)[0] if "/" in name else name)
    return {
        "id": node.get("id", node.get("repository_id", name)),
        "nameWithOwner": name,
        "url": node.get("url", node.get("repository_url", "")),
        "isPrivate": bool(node.get("isPrivate", node.get("is_private", False))),
        "owner": owner,
        "stargazerCount": int(node.get("stargazerCount", 0)),
        "forkCount": int(node.get("forkCount", 0)),
        "languages": node.get("languages", {"edges": []}),
    }


def _snapshot_item(node, pull_request=False):
    """Adapt one normalized public issue/PR to the legacy renderer shape."""
    repository = node.get("repository")
    if isinstance(repository, dict):
        repo = dict(repository)
    else:
        name = repository or node.get("repository_name", "")
        owner = node.get("owner")
        if isinstance(owner, dict):
            owner = owner.get("login")
        repo = {
            "nameWithOwner": name,
            "isPrivate": bool(node.get("is_private", False)),
            "owner": {"login": owner or (
                name.split("/", 1)[0] if "/" in name else name)},
        }
    out = {
        "id": node.get("id", node.get("node_id")),
        "number": int(node.get("number", 0)),
        "url": node.get("url", ""),
        "createdAt": node.get("createdAt", node.get("created_at")),
        "updatedAt": node.get("updatedAt", node.get("updated_at")),
        "closedAt": node.get("closedAt", node.get("closed_at")),
        "state": node.get("state"),
        "stateReason": node.get("stateReason", node.get("state_reason")),
        "repository": repo,
    }
    if pull_request:
        out["mergedAt"] = node.get("mergedAt", node.get("merged_at"))
        out["merged"] = bool(node.get("merged", False))
    return out


def _snapshot_user(snapshot, requested_login=None):
    """Build the minimum profile payload needed for snapshot-only rendering.

    The public acquisition contract intentionally contains authored items, not
    private profile-cache fields. Missing profile counters therefore render as
    zero in ``--render-only`` mode rather than triggering a network request.
    """
    account = dict(snapshot.get("account") or {})
    nested = account.get("user")
    if isinstance(nested, dict):
        user = dict(nested)
    else:
        user = account
    login = user.get("login") or requested_login or "unknown"
    insiders = list(snapshot.get("insiders") or [])
    organisations = [{"login": value} for value in insiders
                     if str(value).casefold() != str(login).casefold()]
    repos = [_snapshot_repository(repo)
             for repo in snapshot.get("repositories", [])]
    user.setdefault("login", login)
    user.setdefault("name", login)
    user.setdefault("followers", {"totalCount": 0})
    user.setdefault("organizations", {"nodes": organisations})
    user.setdefault("repositories", {
        "totalCount": len(repos), "nodes": repos})
    user.setdefault("contributionsCollection", {
        "contributionCalendar": {"totalContributions": 0, "weeks": []}})
    return user


def _snapshot_records(snapshot):
    """Return raw-shaped authored records from a public snapshot."""
    prs = [_snapshot_item(node, pull_request=True)
           for node in snapshot.get("pull_requests", [])]
    issues = [_snapshot_item(node) for node in snapshot.get("issues", [])]
    return prs, issues


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


def fetch(token, login, cached_days=None):
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
    q_core = """
    query($login: String!) {
      user(login: $login) {
        login
        name
        followers { totalCount }
        organizations(first: 100) { nodes { login } }
        repositories(first: 100, ownerAffiliations: OWNER, isFork: false, privacy: PUBLIC) {
          totalCount
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
    user = gql(token, q_core, {"login": login})["user"]
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


def render_stats(C, user, total_stars, total_forks, year_contribs):
    name = user.get("name") or user["login"]
    body = f"""
  <text x="20" y="34" fill="{C['blue']}" font-size="16" font-weight="600">{xml_escape(name)}</text>
  <text x="20" y="52" fill="{C['dim']}" font-size="11">@{user['login']} · github stats</text>
  <line x1="20" y1="64" x2="400" y2="64" stroke="{C['border']}"/>
  <g font-size="13">
    <text x="20"  y="92"  fill="{C['dim']}">followers</text>
    <text x="200" y="92"  fill="{C['gold']}" font-weight="500">{user['followers']['totalCount']}</text>
    <text x="20"  y="115" fill="{C['dim']}">public repos</text>
    <text x="200" y="115" fill="{C['gold']}" font-weight="500">{user['repositories']['totalCount']}</text>
    <text x="20"  y="138" fill="{C['dim']}">stars received</text>
    <text x="200" y="138" fill="{C['gold']}" font-weight="500">{total_stars}</text>
    <text x="20"  y="161" fill="{C['dim']}">forks received</text>
    <text x="200" y="161" fill="{C['gold']}" font-weight="500">{total_forks}</text>
    <text x="20"  y="184" fill="{C['dim']}">contributions (1y)</text>
    <text x="200" y="184" fill="{C['gold']}" font-weight="500">{year_contribs:,}</text>
  </g>"""
    return base_card(C, 420, 203, body)


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
    return base_card(C, 420, 170, body)


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
    return base_card(C, 420, 298, body)


def language_legend(C, top):
    """One legend row per language: color swatch, name, share percent."""
    legend = []
    ly = 124
    for name, _, pct, color in top:
        legend.append(
            f'<rect x="20" y="{ly-9}" width="9" height="9" fill="{color}"/>'
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
        rects.append(f'<rect x="{rect_x:.1f}" y="{y}" width="{seg:.1f}" height="10" fill="{color}"/>')
        rect_x += seg
    body = f"""
  <text x="20" y="34" fill="{C['cyan']}" font-size="14" font-weight="600">top languages</text>
  <text x="20" y="52" fill="{C['dim']}" font-size="11">by bytes of code, public repos</text>
  <line x1="20" y1="64" x2="400" y2="64" stroke="{C['border']}"/>
  <rect x="{bar_x}" y="{y}" width="{bar_w}" height="10" rx="2" fill="{C['border']}"/>
  {''.join(rects)}
  {language_legend(C, top)}"""
    return base_card(C, 420, 230, body)


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
    p.add_argument("--snapshot-file",
                   help="Read normalized public data from this JSON snapshot")
    p.add_argument("--render-only", action="store_true",
                   help="Render from --snapshot-file without GraphQL")
    args = p.parse_args()

    if args.render_only and not args.snapshot_file:
        p.error("--render-only requires --snapshot-file")
    snapshot_only = args.render_only and args.snapshot_file
    if not args.user and not snapshot_only:
        p.error("--user (or env GH_USER) is required")
    token = args.token
    if not token and args.token_file:
        token = Path(args.token_file).read_text(encoding="utf-8").strip()
    if not token and not snapshot_only:
        p.error("--token, --token-file, GH_TOKEN, or GH_TOKEN_FILE required")
    return args, token


def load_inputs(args, token, cache_file, snapshot=None):
    """Fetch (or, on failure, recover from the cache) every input the cards
    need. Returns (user, prs, issues, stale), where `stale` is the cache's
    fetched_at when rendering from cache and None on a successful fetch."""
    if args.render_only:
        _require_snapshot_sections(
            snapshot, ("account", "insiders", "repositories", "issues",
                       "pull_requests"))
        prs, issues = _snapshot_records(snapshot)
        return (_snapshot_user(snapshot, args.user), prs, issues, None)

    cache = {} if args.resync else load_cache(cache_file)
    try:
        user, days = fetch(token, args.user, cache.get("calendar_days") or None)
        if snapshot is not None and "pull_requests" in snapshot:
            prs, _ = _snapshot_records(snapshot)
            prs_by_id = {node["id"]: node for node in prs}
        else:
            prs, prs_by_id = fetch_pull_requests(
                token, args.user, cache.get("prs") or None)
        if snapshot is not None and "issues" in snapshot:
            _, issues = _snapshot_records(snapshot)
        else:
            issues = fetch_issues(token, args.user)
    except Exception:
        # Durability layer: a failed fetch (after gql's retries) renders from
        # cache and exits 0 — but only with a complete cache. Without one,
        # exiting non-zero is still correct.
        if not cache_complete(cache):
            raise
        user = dict(cache["user"])
        user["contributionsCollection"] = {
            "contributionCalendar": calendar_from_days(cache["calendar_days"])}
        prs = list(cache["prs"].values())
        issues = cache["issues"]
        stale = cache["fetched_at"]
    else:
        save_cache(cache_file, {
            "version": CACHE_VERSION,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "user": {k: v for k, v in user.items() if k != "contributionsCollection"},
            "calendar_days": days,
            "prs": prs_by_id,
            "issues": issues,
        })
        stale = None
    return user, prs, issues, stale


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
    total_stars = sum(r["stargazerCount"] for r in user["repositories"]["nodes"])
    total_forks = sum(r["forkCount"] for r in user["repositories"]["nodes"])
    cal = user["contributionsCollection"]["contributionCalendar"]
    year_contribs = cal["totalContributions"]
    current, longest = compute_streak(cal["weeks"])
    langs = aggregate_languages(user["repositories"]["nodes"])
    ext = external_counts(user, prs, issues)
    svgs = {
        "stats.svg": render_stats(C, user, total_stars, total_forks, year_contribs),
        "streak.svg": render_streak(C, current, longest, year_contribs),
        "languages.svg": render_languages(C, langs),
        "external.svg": render_external(C, *ext),
    }
    return svgs, (total_stars, total_forks, year_contribs, current, longest, ext)


def write_cards(C, out, user, prs, issues, stale):
    """Write the four SVGs (stamping the cache fetch time on each when
    stale) plus last-updated.txt, and print the summary line."""
    svgs, (total_stars, total_forks, year_contribs, current, longest, ext) = \
        build_svgs(C, user, prs, issues)
    for name, svg in svgs.items():
        if stale:
            svg = stamp_cache_notice(C, svg, stale)
        (out / name).write_text(svg)
    (out / "last-updated.txt").write_text(
        stale or datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    if stale:
        print(f"fetch failed; rendered {out}/{{stats,streak,languages,external}}.svg "
              f"from cache (fetched_at={stale})")
    else:
        print(f"wrote {out}/{{stats,streak,languages,external}}.svg "
              f"(stars={total_stars} forks={total_forks} contribs={year_contribs} "
              f"streak={current}/{longest} "
              f"external={ext.pr_merged}/{ext.pr_opened} in {ext.pr_repos} repos "
              f"issues={ext.iss_accepted}/{ext.iss_opened} in {ext.iss_repos} repos)")


def main():
    args, token = parse_args()

    snapshot = load_snapshot(args.snapshot_file) \
        if args.snapshot_file else None

    C = THEMES[args.theme]
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    cache_file = os.environ.get("CACHE_FILE", DEFAULT_CACHE_FILE)
    user, prs, issues, stale = load_inputs(
        args, token, cache_file, snapshot=snapshot)
    write_cards(C, out, user, prs, issues, stale)


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode(errors='replace')}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
