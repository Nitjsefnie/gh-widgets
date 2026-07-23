#!/usr/bin/env python3
"""
gh-widgets — render the self-hosted "External Impact" SVG.

Writes ONE SVG to OUT_DIR:
  impact.svg    three ranked tables (top 10 external repos each) of your
                contribution to repos outside your own account and orgs:
                pull requests (your merged / all merged), issues (your
                maintainer-accepted / all issues) and live code (your
                surviving default-branch lines / all lines, via git blame).
                Each repo is ranked by an impact score:
                WilsonLowerBound(our/total, z=2.58) * our**gamma with
                gamma 1.0 (PRs), 1.75 (issues), 0.5 (live code).

Configuration (env vars or CLI flags, in that order of precedence):
  GH_USER     (required) GitHub username
  GH_TOKEN    (required) Personal access token with `read:user` + `public_repo`
              (can also be read from a file via --token-file or GH_TOKEN_FILE)
  OUT_DIR     where to write the SVG (default: ./widgets)
  CACHE_FILE  JSON cache of immutable data
              (default: /var/lib/gh-widgets/impact-cache.json)
  THEME       tokyonight (default) | catppuccin | gruvbox | github-dark

  --resync    discard the cache and refetch/re-blame everything (wired to run
              weekly, so no correctness claim rests permanently on MERGED
              being one-way or on a line count surviving force-pushes)

The SVG is a static file — serve it from any web server with
`Cache-Control: must-revalidate` or similar, and embed by URL.

Designed to fail gracefully, same contract as render.py: MERGED PRs are
immutable, so they are cached keyed by id and only the mutable slice
(OPEN/CLOSED PRs) is refetched each run — a previously live PR now absent
from that slice has merged and moves to the frozen half. Issues can be
REOPENED, so they are never incrementally cached: all authored issues are
re-paged every run (they sit in the cache only as a stale-fallback
snapshot). Live-line counts are cached per repo keyed by the default
branch's HEAD oid; a batched GraphQL query refreshes every external repo's
oid + PR/issue totals each run, and only repos whose oid moved (or that
have no count yet) are re-cloned and re-blamed. A run whose fetches still
fail renders from the cache, stamps the cache timestamp on the card, and
exits 0; with no cache to fall back on it exits non-zero and the existing
SVG keeps serving.

Deps: Python stdlib + the `git` CLI + git-fame (pip) for the blame pass.
Requires Python 3.9+.
"""
import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
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


CACHE_VERSION = 1
DEFAULT_CACHE_FILE = "/var/lib/gh-widgets/impact-cache.json"

# Locked metric knobs (validated in impact_stats.py — do not retune).
Z = 2.58
PR_GAMMA = 1.0
ISSUE_GAMMA = 1.75
LOC_GAMMA = 0.5

# A repo counts only if it is public AND its owner is not one of these.
INSIDERS = {s.casefold() for s in [
    "Nitjsefnie", "BrainByteQuiz", "Consultest-CZ", "West-Scripts",
    "Nitjsefnie-Games", "Nitjsefnie-OSC"]}

# GitHub rewrites the commit author on merge to the noreply email
# (75166987+Nitjsefnie@users.noreply.github.com) and drops the login, so a
# git-fame row (keyed by email) is OURS if it carries the login OR is the
# raw box email. Matching only the box email returns zero for most repos.
OUR_EMAIL = "zmatek.peter@gmail.com"

TOP_N = 10


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
               for k in ("fetched_at", "prs", "issues", "totals", "ourloc"))


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
    # Same retry contract as render.py: transient server-side conditions get
    # a few retries; real errors fail fast.
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
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

    Identical incremental contract to render.py's fetch_pull_requests:
    MERGED is the only one-way PR state, so with a warm cache only
    [OPEN, CLOSED] are queried live and unioned with the cached set keyed by
    PR id, which guarantees a PR appears exactly once. A PR stays in the
    live half until it merges: merging is the only way to leave the
    OPEN/CLOSED result set, so a previously live PR that is now absent is
    recorded as merged and moves to the cached half. On a cold cache (or
    --resync) all three states are queried and the set is rebuilt.
    max_pages is a runaway guard, not a real limit.

    Returns (prs, prs_by_id): the union list and the keyed mapping to
    persist in the cache.
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


def fetch_issues(token, login, max_pages=50):
    """Page through every ISSUE the user has authored, every run.

    Issues can be REOPENED, so unlike PRs there is no immutable slice to
    freeze: no incremental caching here, the full [OPEN, CLOSED] list is
    re-paged each run and the metrics recomputed. (The list is persisted in
    the cache only as a stale-fallback snapshot for the durability layer.)
    The GraphQL `issues` connection returns issues ONLY, so there is no PR
    double-counting to guard against. max_pages is a runaway guard.
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


def is_external(node):
    """Public repo whose owner is not in INSIDERS (casefold)."""
    r = node["repository"]
    return not r["isPrivate"] and r["owner"]["login"].casefold() not in INSIDERS


def fetch_repo_totals(token, repos):
    """Batched per-repo live totals + default-branch HEAD oid, one query per
    15 repos. The issue/merged-PR totals are live counts and are refetched
    every run; the defaultBranchRef oid rides along in the same query so
    the LOC cache check is free. Returns
    {repo: {issues, merged_prs, branch, head}}; missing/renamed repos are
    simply absent."""
    totals = {}
    for i in range(0, len(repos), 15):
        chunk = repos[i:i + 15]
        parts = [
            f'a{j}: repository(owner:{json.dumps(r.split("/")[0])},'
            f'name:{json.dumps(r.split("/", 1)[1])}){{ '
            f'defaultBranchRef{{ name target{{ oid }} }} '
            f'issues{{totalCount}} '
            f'pullRequests(states:MERGED){{totalCount}} }}'
            for j, r in enumerate(chunk)]
        d = gql(token, "query{ " + " ".join(parts) + " }")
        for j, r in enumerate(chunk):
            node = d.get(f"a{j}")
            if not node:
                continue
            ref = node.get("defaultBranchRef") or {}
            totals[r] = {
                "issues": node["issues"]["totalCount"],
                "merged_prs": node["pullRequests"]["totalCount"],
                "branch": ref.get("name", ""),
                "head": (ref.get("target") or {}).get("oid", ""),
            }
        time.sleep(0.3)
    return totals


def blame_repo(repo, branch, dest):
    """Full-clone the default branch into `dest` (blame needs history, so
    NOT --depth 1), aggregate surviving LOC per author email with git-fame,
    and return (ours, total). The clone is deleted by the caller. Raises on
    any failure."""
    cmd = ["git", "clone", "--single-branch"]
    if branch:
        cmd += ["--branch", branch]
    cmd += [f"https://github.com/{repo}.git", str(dest)]
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=300)
    if r.returncode != 0 or not dest.exists():
        raise RuntimeError("clone_failed")
    fm = subprocess.run(["git", "fame", "-e", "-w", "--format", "json"],
                        cwd=str(dest), capture_output=True, text=True, timeout=600)
    data = json.loads(fm.stdout) if fm.stdout.strip() else {}
    total = data.get("total", {}).get("loc", 0)
    ours = 0
    for row in data.get("data", []):
        em = str(row[0]).strip().lower()
        if "nitjsefnie" in em or em == OUR_EMAIL:
            ours += row[1]
    return ours, total


def update_loc(candidate_repos, totals, cached_ourloc, resync):
    """Refresh the per-repo live-line counts. Only repos we have a merged PR
    in can carry our lines (external lines land only via merges), so only
    those are cloned — matching the validated clone_fame.py. A repo is
    re-blamed only when its current default-branch HEAD oid differs from
    the oid its cached count was taken at (or it has no count yet, or
    --resync); otherwise the cached count is reused. Clone -> count ->
    delete immediately; clones are never kept.

    A failed clone/blame keeps any prior good count untouched (its stale
    oid makes it retry next run); with no prior count it records
    {"error", head} so an always-failing repo is not re-cloned every run
    until its HEAD actually moves.
    """
    ourloc = dict(cached_ourloc)
    moved = []
    for repo in sorted(candidate_repos):
        t = totals.get(repo)
        if not t or not t["head"]:
            continue  # repo gone/renamed: keep any cached entry, skip
        entry = ourloc.get(repo) or {}
        if (not resync and entry.get("head") == t["head"]
                and ("ours" in entry or "error" in entry)):
            continue
        moved.append((repo, t))
    for i, (repo, t) in enumerate(moved, 1):
        tmp = Path(tempfile.mkdtemp(prefix="impact-fame-"))
        try:
            ours, total = blame_repo(repo, t["branch"], tmp)
            ourloc[repo] = {"ours": ours, "total": total,
                            "branch": t["branch"], "head": t["head"]}
            sh = ours / total * 100 if total else 0
            print(f"loc [{i}/{len(moved)}] ours {ours:>7,} / {total:>8,} "
                  f"({sh:4.1f}%)  {repo}", flush=True)
        except Exception as e:
            old = ourloc.get(repo) or {}
            if "ours" in old:
                print(f"loc [{i}/{len(moved)}] FAIL (kept old count) "
                      f"{repo}: {str(e)[:60]}", flush=True)
            else:
                ourloc[repo] = {"error": str(e)[:80], "head": t["head"]}
                print(f"loc [{i}/{len(moved)}] FAIL {repo}: {str(e)[:60]}",
                      flush=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return ourloc


def wilson(w, n, z):
    if n == 0:
        return 0.0
    p = w / n
    d = 1 + z * z / n
    return (p + z * z / (2 * n) - z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)) / d


def impact_rows(our_counts, total_of, gamma):
    """Rank repos by WilsonLowerBound(our/total, Z) * our**gamma.
    our_counts: {repo: our}; total_of: repo -> total. Returns rows of
    (score, share_pct, our, total, repo), best first; our==0 rows (no
    contribution) are excluded."""
    rows = []
    for repo, w in our_counts.items():
        n = total_of(repo)
        if w and n:
            rows.append((wilson(w, n, Z) * (w ** gamma),
                         w / n * 100, w, n, repo))
    rows.sort(reverse=True)
    return rows


def pr_table(prs, totals):
    our_merged = {}
    for pr in prs:
        if pr["merged"] and is_external(pr):
            r = pr["repository"]["nameWithOwner"]
            our_merged[r] = our_merged.get(r, 0) + 1
    return impact_rows(our_merged,
                       lambda r: (totals.get(r) or {}).get("merged_prs", 0),
                       PR_GAMMA)


def issue_table(issues, totals):
    our_completed = {}
    for it in issues:
        if (it["state"] == "CLOSED" and it["stateReason"] == "COMPLETED"
                and is_external(it)):
            r = it["repository"]["nameWithOwner"]
            our_completed[r] = our_completed.get(r, 0) + 1
    return impact_rows(our_completed,
                       lambda r: (totals.get(r) or {}).get("issues", 0),
                       ISSUE_GAMMA)


def loc_table(ourloc):
    our_lines = {r: v["ours"] for r, v in ourloc.items() if "ours" in v}
    return impact_rows(our_lines,
                       lambda r: (ourloc.get(r) or {}).get("total", 0),
                       LOC_GAMMA)


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


CARD_W = 480
COL_MT = 330   # mine/total, right-anchored
COL_SHARE = 395  # share, right-anchored
COL_SCORE = 460  # impact score, right-anchored
REPO_CHARS = 42  # owner/name truncation budget (~280px at font-size 11 mono)


def truncate(s, n):
    return s if len(s) <= n else s[:n - 1] + "…"


def render_section(C, y, title, rows, accent):
    parts = [f'<text x="20" y="{y}" fill="{C["dim"]}" font-size="11" '
             f'font-weight="600">{title}</text>']
    y += 16
    parts.append(
        f'<text x="20" y="{y}" fill="{C["dim"]}" font-size="10">repo</text>'
        f'<text x="{COL_MT}" y="{y}" text-anchor="end" fill="{C["dim"]}" font-size="10">mine/total</text>'
        f'<text x="{COL_SHARE}" y="{y}" text-anchor="end" fill="{C["dim"]}" font-size="10">share</text>'
        f'<text x="{COL_SCORE}" y="{y}" text-anchor="end" fill="{C["dim"]}" font-size="10">impact score</text>')
    y += 17
    if not rows:
        parts.append(f'<text x="20" y="{y}" fill="{C["dim"]}" font-size="11">'
                     f'no external contributions yet</text>')
        y += 16
    for score, share, w, n, repo in rows[:TOP_N]:
        parts.append(
            f'<text x="20" y="{y}" fill="{C["fg"]}" font-size="11">'
            f'{xml_escape(truncate(repo, REPO_CHARS))}</text>'
            f'<text x="{COL_MT}" y="{y}" text-anchor="end" fill="{C["dim"]}" font-size="11">{w}/{fmt_short(n)}</text>'
            f'<text x="{COL_SHARE}" y="{y}" text-anchor="end" fill="{C["dim"]}" font-size="11">{share:.1f}%</text>'
            f'<text x="{COL_SCORE}" y="{y}" text-anchor="end" fill="{accent}" font-size="11" font-weight="500">{score:.2f}</text>')
        y += 16
    return parts, y


def render_impact(C, pr_rows, issue_rows, loc_rows):
    y = 88
    body = f"""
  <text x="20" y="34" fill="{C['gold']}" font-size="14" font-weight="600">external impact</text>
  <text x="20" y="52" fill="{C['dim']}" font-size="11">contribution to repos outside your own account and orgs</text>
  <line x1="20" y1="64" x2="{CARD_W - 20}" y2="64" stroke="{C['border']}"/>"""
    sections = (("Pull Requests", pr_rows, C["blue"]),
                ("Issues", issue_rows, C["green"]),
                ("Live Code", loc_rows, C["purple"]))
    for i, (title, rows, accent) in enumerate(sections):
        parts, y = render_section(C, y, title, rows, accent)
        body += "\n  " + "\n  ".join(parts)
        if i < len(sections) - 1:
            y += 10
            body += (f'\n  <line x1="20" y1="{y}" x2="{CARD_W - 20}" y2="{y}" '
                     f'stroke="{C["border"]}"/>')
            y += 24
    return base_card(C, CARD_W, y + 12, body)


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
    p = argparse.ArgumentParser(
        description="Render the self-hosted External Impact SVG.")
    p.add_argument("--user", default=os.environ.get("GH_USER"),
                   help="GitHub username (env: GH_USER)")
    p.add_argument("--token", default=os.environ.get("GH_TOKEN"),
                   help="GitHub PAT (env: GH_TOKEN)")
    p.add_argument("--token-file", default=os.environ.get("GH_TOKEN_FILE"),
                   help="Read token from a file (env: GH_TOKEN_FILE)")
    p.add_argument("--out-dir", default=os.environ.get("OUT_DIR", "./widgets"),
                   help="Where to write the SVG (env: OUT_DIR)")
    p.add_argument("--theme", default=os.environ.get("THEME", "tokyonight"),
                   choices=list(THEMES),
                   help="Color theme (env: THEME)")
    p.add_argument("--resync", action="store_true",
                   help="Discard the cache and refetch/re-blame everything")
    p.add_argument("--cache-file",
                   default=os.environ.get("CACHE_FILE", DEFAULT_CACHE_FILE),
                   help="JSON cache path (env: CACHE_FILE)")
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

    cache = {} if args.resync else load_cache(args.cache_file)

    stale = None
    try:
        prs, prs_by_id = fetch_pull_requests(
            token, args.user, None if args.resync else cache.get("prs") or None)
        issues = fetch_issues(token, args.user)
        repos = sorted({n["repository"]["nameWithOwner"]
                        for n in prs + issues if is_external(n)})
        totals = fetch_repo_totals(token, repos)
        merged_repos = {n["repository"]["nameWithOwner"] for n in prs
                        if n["merged"] and is_external(n)}
        ourloc = update_loc(merged_repos, totals,
                            {} if args.resync else cache.get("ourloc") or {},
                            args.resync)
    except Exception:
        # Durability layer: a failed fetch (after gql's retries) renders from
        # cache and exits 0 — but only with a complete cache. Without one,
        # exiting non-zero is still correct.
        if not cache_complete(cache):
            raise
        prs = list(cache["prs"].values())
        issues = cache["issues"]
        totals = cache["totals"]
        ourloc = cache["ourloc"]
        stale = cache["fetched_at"]
    else:
        save_cache(args.cache_file, {
            "version": CACHE_VERSION,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "prs": prs_by_id,
            "issues": issues,
            "totals": totals,
            "ourloc": ourloc,
        })

    pr_rows = pr_table(prs, totals)
    issue_rows = issue_table(issues, totals)
    loc_rows = loc_table(ourloc)

    svg = render_impact(C, pr_rows, issue_rows, loc_rows)
    if stale:
        svg = stamp_cache_notice(C, svg, stale)
    (out / "impact.svg").write_text(svg)

    if stale:
        print(f"fetch failed; rendered {out}/impact.svg from cache "
              f"(fetched_at={stale})")
    else:
        print(f"wrote {out}/impact.svg "
              f"(pr repos={len(pr_rows)} issue repos={len(issue_rows)} "
              f"loc repos={len(loc_rows)})")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode(errors='replace')}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
