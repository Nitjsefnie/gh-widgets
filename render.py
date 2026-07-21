#!/usr/bin/env python3
"""
gh-widgets — render self-hosted GitHub stat SVGs.

Writes four SVGs to OUT_DIR:
  stats.svg      followers, repos, stars, last-year contributions
  streak.svg     current contribution streak, longest streak, year total
  languages.svg  top 5 languages by bytes of code
  external.svg   PRs opened to repos outside your own account and orgs
                 (how many merged, how many repos) and issues filed there
                 (how many the maintainers accepted, how many repos)

Configuration (env vars or CLI flags, in that order of precedence):
  GH_USER     (required) GitHub username
  GH_TOKEN    (required) Personal access token with `read:user` + `public_repo`
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
calendar query was getting killed with RESOURCE_LIMITS_EXCEEDED. A run
whose fetches still fail renders from the cache, stamps the cache
timestamp on every card, and exits 0; with no cache to fall back on it
exits non-zero and the existing SVGs keep serving.

Zero external deps. Pure Python stdlib. Requires Python 3.9+.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

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


CACHE_VERSION = 1
DEFAULT_CACHE_FILE = "/var/lib/gh-widgets/cache.json"


def load_cache(path):
    """Read the JSON cache. A missing, unreadable, corrupt, or
    schema-mismatched cache is not an error: it degrades to a full fetch."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
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


def cache_complete(cache):
    """The durability fallback can render only if every input is cached."""
    return all(k in cache
               for k in ("fetched_at", "user", "calendar_days", "prs", "issues"))


def prune_days(days, now):
    """Drop days older than the trailing year so the merged history matches
    what a full calendar fetch (GitHub's default one-year window) returns."""
    try:
        cutoff = now.date().replace(year=now.year - 1)
    except ValueError:  # Feb 29 -> Feb 28
        cutoff = now.date().replace(year=now.year - 1, day=28)
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


def gql(token, query, variables=None, retries=3):
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
    # GitHub intermittently answers the contribution-calendar query with
    # RESOURCE_LIMITS_EXCEEDED under load (observed 2026-07-18: four hourly
    # runs red, then the identical query succeeded minutes later). Transient
    # server-side conditions get a few retries; real errors fail fast.
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
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


def fetch(token, login, cached_days=None):
    # Two requests, not one: GitHub's GraphQL node-limit estimator started
    # rejecting the combined repos×languages + full-calendar query with
    # RESOURCE_LIMITS_EXCEEDED (first seen 2026-07-18, every hourly run red).
    # Splitting the calendar into its own request was not enough — the
    # full-year calendar query alone now trips the limit — so with a warm
    # cache the calendar is fetched as a trailing 7-day window (the only days
    # that can still move) and merged onto the cached day history keyed by
    # date. The merged structure keeps the exact shape the renderers already
    # consume. Returns (user, days); `days` is the merged date -> count map
    # to persist in the cache.
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
    if cached_days is None:
        # Cold cache (or --resync): full one-year calendar, the default
        # contributionsCollection window.
        q_calendar = """
        query($login: String!) {
          user(login: $login) {
            contributionsCollection {
              contributionCalendar {
                totalContributions
                weeks { contributionDays { date contributionCount } }
              }
            }
          }
        }
        """
        cal = gql(token, q_calendar, {"login": login})[
            "user"]["contributionsCollection"]["contributionCalendar"]
        days = {d["date"]: d["contributionCount"]
                for w in cal["weeks"] for d in w["contributionDays"]}
    else:
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


def fetch_pull_requests(token, login, cached_prs=None, max_pages=50):
    """Fetch the user's authored PRs and merge with the cached set.

    There is no server-side "not in these orgs" filter, so we pull the list
    and filter locally. max_pages is a runaway guard, not a real limit.

    MERGED is the only one-way PR state, so with a warm cache only
    [OPEN, CLOSED] are queried live and unioned with the cached set keyed by
    PR id, which guarantees a PR appears exactly once. A PR stays in the
    live half until it merges: merging is the only way to leave the
    OPEN/CLOSED result set, so a previously live PR that is now absent is
    recorded as merged and moves to the cached half. On a cold cache (or
    --resync) all three states are queried and the set is rebuilt.

    Returns (prs, prs_by_id): the union list for external_contributions and
    the keyed mapping to persist in the cache.
    """
    states = "[OPEN, CLOSED]" if cached_prs is not None else "[OPEN, CLOSED, MERGED]"
    q = PR_QUERY % states
    live = []
    cursor = None
    for _ in range(max_pages):
        page = gql(token, q, {"login": login, "cursor": cursor})["user"]["pullRequests"]
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
        for pid, node in known.items():
            if not node["merged"] and pid not in live_ids:
                known[pid] = {**node, "merged": True}
    return list(known.values()), known


def external_contributions(prs, login, orgs):
    """Count PRs to repos owned by neither the user nor any org they belong to.

    Private repos are excluded: they can't be shown off, and a viewer of the
    SVG can't verify them.
    """
    insiders = {login.casefold()} | {o.casefold() for o in orgs}
    opened = merged = 0
    repos = set()
    for pr in prs:
        repo = pr["repository"]
        if repo["isPrivate"] or repo["owner"]["login"].casefold() in insiders:
            continue
        opened += 1
        merged += bool(pr["merged"])
        repos.add(repo["nameWithOwner"])
    return opened, merged, len(repos)


def fetch_issues(token, login, max_pages=50):
    """Page through every ISSUE the user has authored.

    Mirrors fetch_pull_requests: no server-side "not in these orgs" filter,
    so we pull the list and filter locally. The GraphQL `issues` connection
    returns issues ONLY (pull requests live under `pullRequests`), so there
    is no PR double-counting to guard against here. max_pages is a runaway
    guard, not a real limit.
    """
    q = """
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
    issues = []
    cursor = None
    for _ in range(max_pages):
        page = gql(token, q, {"login": login, "cursor": cursor})["user"]["issues"]
        issues.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    return issues


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
    insiders = {login.casefold()} | {o.casefold() for o in orgs}
    opened = accepted = 0
    repos = set()
    for issue in issues:
        repo = issue["repository"]
        if repo["isPrivate"] or repo["owner"]["login"].casefold() in insiders:
            continue
        opened += 1
        repos.add(repo["nameWithOwner"])
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


def render_stats(C, user, total_stars, year_contribs):
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
    <text x="20"  y="161" fill="{C['dim']}">contributions (1y)</text>
    <text x="200" y="161" fill="{C['gold']}" font-weight="500">{year_contribs:,}</text>
  </g>"""
    return base_card(C, 420, 180, body)


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
        return (f"""
  <line x1="{sep_xs[0]}" y1="{y1}" x2="{sep_xs[0]}" y2="{y2}" stroke="{C['border']}"/>
  <line x1="{sep_xs[1]}" y1="{y1}" x2="{sep_xs[1]}" y2="{y2}" stroke="{C['border']}"/>""")

    body = f"""
  <text x="20" y="34" fill="{C['gold']}" font-size="14" font-weight="600">external contributions</text>
  <text x="20" y="52" fill="{C['dim']}" font-size="11">to repos outside your own account and orgs</text>
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
    legend = []
    ly = 124
    for name, _, pct, color in top:
        legend.append(
            f'<rect x="20" y="{ly-9}" width="9" height="9" fill="{color}"/>'
            f'<text x="34" y="{ly}" fill="{C["fg"]}" font-size="12">{xml_escape(name)}</text>'
            f'<text x="400" y="{ly}" text-anchor="end" fill="{C["dim"]}" font-size="11">{pct:.1f}%</text>'
        )
        ly += 18
    body = f"""
  <text x="20" y="34" fill="{C['cyan']}" font-size="14" font-weight="600">top languages</text>
  <text x="20" y="52" fill="{C['dim']}" font-size="11">by bytes of code, public repos</text>
  <line x1="20" y1="64" x2="400" y2="64" stroke="{C['border']}"/>
  <rect x="{bar_x}" y="{y}" width="{bar_w}" height="10" rx="2" fill="{C['border']}"/>
  {''.join(rects)}
  {''.join(legend)}"""
    return base_card(C, 420, 230, body)


def stamp_cache_notice(C, svg, fetched_at):
    """A card rendered from cache must show it: tag the cache's own fetch
    time bottom-left so staleness is visible rather than silent. Done as a
    post-pass so the renderer signatures stay unchanged."""
    m = re.search(r'height="(\d+)"', svg)
    y = int(m.group(1)) - 8 if m else 12
    note = (f'<text x="10" y="{y}" fill="{C["dim"]}" font-size="10">'
            f'cached data from {xml_escape(fetched_at)}</text>')
    return svg.replace("</svg>", f"  {note}\n</svg>")


def main():
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
        token = Path(args.token_file).read_text().strip()
    if not token:
        p.error("--token, --token-file, GH_TOKEN, or GH_TOKEN_FILE required")

    C = THEMES[args.theme]
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    cache_file = os.environ.get("CACHE_FILE", DEFAULT_CACHE_FILE)
    cache = {} if args.resync else load_cache(cache_file)

    stale = None
    try:
        user, days = fetch(token, args.user, cache.get("calendar_days") or None)
        prs, prs_by_id = fetch_pull_requests(token, args.user, cache.get("prs") or None)
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

    total_stars = sum(r["stargazerCount"] for r in user["repositories"]["nodes"])
    cal = user["contributionsCollection"]["contributionCalendar"]
    year_contribs = cal["totalContributions"]
    current, longest = compute_streak(cal["weeks"])
    langs = aggregate_languages(user["repositories"]["nodes"])

    orgs = [o["login"] for o in user["organizations"]["nodes"]]
    ext_opened, ext_merged, ext_repos = external_contributions(prs, user["login"], orgs)
    ext_iss_opened, ext_iss_accepted, ext_iss_repos = external_issues(
        issues, user["login"], orgs)

    svgs = {
        "stats.svg": render_stats(C, user, total_stars, year_contribs),
        "streak.svg": render_streak(C, current, longest, year_contribs),
        "languages.svg": render_languages(C, langs),
        "external.svg": render_external(
            C, ext_opened, ext_merged, ext_repos,
            ext_iss_opened, ext_iss_accepted, ext_iss_repos),
    }
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
              f"(stars={total_stars} contribs={year_contribs} streak={current}/{longest} "
              f"external={ext_merged}/{ext_opened} in {ext_repos} repos "
              f"issues={ext_iss_accepted}/{ext_iss_opened} in {ext_iss_repos} repos)")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode(errors='replace')}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
