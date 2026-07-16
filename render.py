#!/usr/bin/env python3
"""
gh-widgets — render self-hosted GitHub stat SVGs.

Writes four SVGs to OUT_DIR:
  stats.svg      followers, repos, stars, last-year contributions
  streak.svg     current contribution streak, longest streak, year total
  languages.svg  top 5 languages by bytes of code
  external.svg   PRs opened to repos outside your own account and orgs,
                 how many merged, and how many repos you reached

Configuration (env vars or CLI flags, in that order of precedence):
  GH_USER     (required) GitHub username
  GH_TOKEN    (required) Personal access token with `read:user` + `public_repo`
              (can also be read from a file via --token-file or GH_TOKEN_FILE)
  OUT_DIR     where to write the SVGs (default: ./widgets)
  THEME       tokyonight (default) | catppuccin | gruvbox | github-dark

The SVGs are static files — serve them from any web server with
`Cache-Control: must-revalidate` or similar, and embed by URL.

Designed to fail gracefully: if a run fails (API rate limit, network),
existing SVGs keep serving and the next cron run will recover.

Zero external deps. Pure Python stdlib. Requires Python 3.9+.
"""
import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
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


def gql(token, query, variables=None):
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
    with urllib.request.urlopen(req, timeout=20) as r:
        body = json.loads(r.read())
    if "errors" in body:
        raise RuntimeError(f"GraphQL errors: {body['errors']}")
    return body["data"]


def fetch(token, login):
    q = """
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
        contributionsCollection {
          contributionCalendar {
            totalContributions
            weeks { contributionDays { date contributionCount } }
          }
        }
      }
    }
    """
    return gql(token, q, {"login": login})["user"]


def fetch_pull_requests(token, login, max_pages=50):
    """Page through every PR the user has authored.

    There is no server-side "not in these orgs" filter, so we pull the list
    and filter locally. max_pages is a runaway guard, not a real limit.
    """
    q = """
    query($login: String!, $cursor: String) {
      user(login: $login) {
        pullRequests(first: 100, after: $cursor,
                     states: [OPEN, CLOSED, MERGED],
                     orderBy: {field: CREATED_AT, direction: DESC}) {
          pageInfo { hasNextPage endCursor }
          nodes {
            merged
            repository { nameWithOwner isPrivate owner { login } }
          }
        }
      }
    }
    """
    prs = []
    cursor = None
    for _ in range(max_pages):
        page = gql(token, q, {"login": login, "cursor": cursor})["user"]["pullRequests"]
        prs.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    return prs


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


def compute_streak(weeks):
    """Walk newest->oldest; skip leading zeros (today/tz quirks), then
    count consecutive non-zero days. Also returns longest streak."""
    days = []
    for w in weeks:
        for d in w["contributionDays"]:
            days.append((d["date"], d["contributionCount"]))
    days.sort()
    current = 0
    counting = False
    for _, count in reversed(days):
        if count > 0:
            counting = True
            current += 1
        elif counting:
            break
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


def render_external(C, opened, merged, repos):
    col_centers = (84, 210, 336)
    sep_xs = (147, 273)
    rate = f"{merged / opened * 100:.0f}% merged" if opened else "no external PRs yet"

    def big(x_center, label, value, color):
        return f"""
    <text x="{x_center}" y="112" text-anchor="middle" fill="{color}" font-size="36" font-weight="600">{value}</text>
    <text x="{x_center}" y="138" text-anchor="middle" fill="{C['dim']}" font-size="11">{label}</text>"""

    body = f"""
  <text x="20" y="34" fill="{C['gold']}" font-size="14" font-weight="600">external contributions</text>
  <text x="20" y="52" fill="{C['dim']}" font-size="11">pull requests to other people's repos</text>
  <line x1="20" y1="64" x2="400" y2="64" stroke="{C['border']}"/>
  {big(col_centers[0], 'opened', fmt_short(opened), C['blue'])}
  {big(col_centers[1], 'merged', fmt_short(merged), C['green'])}
  {big(col_centers[2], 'repos', fmt_short(repos), C['purple'])}
  <line x1="{sep_xs[0]}" y1="90" x2="{sep_xs[0]}" y2="135" stroke="{C['border']}"/>
  <line x1="{sep_xs[1]}" y1="90" x2="{sep_xs[1]}" y2="135" stroke="{C['border']}"/>
  <text x="210" y="160" text-anchor="middle" fill="{C['dim']}" font-size="11">{rate}</text>"""
    return base_card(C, 420, 178, body)


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

    user = fetch(token, args.user)
    total_stars = sum(r["stargazerCount"] for r in user["repositories"]["nodes"])
    cal = user["contributionsCollection"]["contributionCalendar"]
    year_contribs = cal["totalContributions"]
    current, longest = compute_streak(cal["weeks"])
    langs = aggregate_languages(user["repositories"]["nodes"])

    orgs = [o["login"] for o in user["organizations"]["nodes"]]
    prs = fetch_pull_requests(token, args.user)
    ext_opened, ext_merged, ext_repos = external_contributions(prs, user["login"], orgs)

    (out / "stats.svg").write_text(render_stats(C, user, total_stars, year_contribs))
    (out / "streak.svg").write_text(render_streak(C, current, longest, year_contribs))
    (out / "languages.svg").write_text(render_languages(C, langs))
    (out / "external.svg").write_text(
        render_external(C, ext_opened, ext_merged, ext_repos))
    (out / "last-updated.txt").write_text(
        datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    print(f"wrote {out}/{{stats,streak,languages,external}}.svg "
          f"(stars={total_stars} contribs={year_contribs} streak={current}/{longest} "
          f"external={ext_merged}/{ext_opened} in {ext_repos} repos)")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode(errors='replace')}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
