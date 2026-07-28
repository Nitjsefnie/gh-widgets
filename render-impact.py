#!/usr/bin/env python3
"""
gh-widgets — render the self-hosted "External Impact" SVG.

Writes ONE SVG to OUT_DIR:
  impact.svg    three ranked tables (top 5 external repos each) of your
                contribution to repos outside my own account and orgs:
                pull requests (my merged / all merged), issues (my
                maintainer-accepted / all issues) and live code (your
                surviving default-branch lines / all lines, via git blame).
                Each repo is ranked by an impact score:
                WilsonLowerBound(our/total, z=2.58) * our**gamma with
                gamma 1.0 (PRs), 1.75 (issues), 0.5 (live code).

Configuration (env vars or CLI flags, in that order of precedence):
  GH_USER     (required) GitHub username
  GH_TOKEN    (required) Personal access token with `public_repo`. `read:user`
              is NOT needed: identity comes from login + databaseId + orgs,
              never the email field (which that scope gates).
              (can also be read from a file via --token-file or GH_TOKEN_FILE)
  OUT_DIR     where to write the SVG (default: ./widgets)
  CACHE_FILE  JSON cache of immutable data
              (default: /var/lib/gh-widgets/impact-cache.json)
  THEME       tokyonight (default) | catppuccin | gruvbox | github-dark

  GH_EXTRA_INSIDERS  extra owner logins to treat as ours (comma-separated)
  GH_EXTRA_EMAILS    extra commit-author addresses that are ours (comma-sep)
  IMPACT_Z           Wilson lower-bound z score        (default 2.58)
  IMPACT_PR_GAMMA    volume exponent, PR table         (default 1.0)
  IMPACT_ISSUE_GAMMA volume exponent, issue table      (default 1.75)
  IMPACT_LOC_GAMMA   volume exponent, live-code table  (default 0.5)

Who counts as "us" is DERIVED from the token's own account (login, databaseId,
orgs) rather than hardcoded, so joining an org needs no code change. The two
GH_EXTRA_* vars can only ADD to the derived sets — an override would let a
stale value silently reintroduce the drift that derivation removes. Line
ownership is an EXACT match against the account's noreply addresses: in a
third-party repo the commit-author email is attacker-controllable, so a
substring test there was wrong in kind.

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
import importlib.util
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
from datetime import datetime, timezone
from pathlib import Path


def _load_common():
    """Load ghwidgets_common.py from beside this script — see render.py."""
    path = Path(__file__).resolve().with_name("ghwidgets_common.py")
    if not path.exists():
        raise SystemExit(
            f"error: render-impact.py cannot find its ghwidgets_common.py at "
            f"{path} (install.sh copies both; a partial copy is not usable)")
    spec = importlib.util.spec_from_file_location("ghwidgets_common", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"error: cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


common = _load_common()

REQUIRED_COMMON = 1
common.check_version(REQUIRED_COMMON)

CACHE_VERSION = 1
DEFAULT_CACHE_FILE = "/var/lib/gh-widgets/impact-cache.json"

TOP_N = 5

# Re-exported so call sites stay short and patchable, same as render.py.
FONT = common.FONT
THEMES = common.THEMES
gql = common.gql
save_cache = common.save_cache
fmt_short = common.fmt_short
xml_escape = common.xml_escape
base_card = common.base_card
stamp_cache_notice = common.stamp_cache_notice


def metric_knobs():
    """The ranking knobs, read from the environment at call time.

    Defaults are the values validated when the metric was designed; they are
    configurable so a run can be re-scored without editing source, NOT because
    they are expected to change. An unparseable value aborts the run rather
    than silently reverting to the default — see common.env_float.
    """
    return {
        "z":           common.env_float("IMPACT_Z", 2.58),
        "pr_gamma":    common.env_float("IMPACT_PR_GAMMA", 1.0),
        "issue_gamma": common.env_float("IMPACT_ISSUE_GAMMA", 1.75),
        "loc_gamma":   common.env_float("IMPACT_LOC_GAMMA", 0.5),
    }


def load_cache(path):
    """Read the JSON cache, checked against THIS script's schema version."""
    return common.load_cache(path, CACHE_VERSION)


def cache_complete(cache):
    """The durability fallback can render only if every input is cached."""
    return all(k in cache
               for k in ("fetched_at", "prs", "issues", "totals", "ourloc"))


PR_QUERY = common.PR_QUERY


def fetch_pull_requests(token, login, cached_prs=None, max_pages=50):
    """Thin wrapper over the shared implementation, passing THIS module's
    `gql` so a test patch of it still intercepts the calls."""
    return common.fetch_pull_requests(token, login, cached_prs, max_pages,
                                      gql_fn=gql)


def fetch_issues(token, login, max_pages=50):
    """Thin wrapper over the shared implementation — see fetch_pull_requests."""
    return common.fetch_issues(token, login, max_pages, gql_fn=gql)


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


def blame_repo(repo, branch, dest, emails):
    """Full-clone the default branch into `dest` (blame needs history, so
    NOT --depth 1), aggregate surviving LOC per author email with git-fame,
    and return (ours, total). The clone is deleted by the caller. Raises on
    any failure.

    `emails` is the derived set of addresses that count as ours, matched
    EXACTLY. This used to be a substring test, which was wrong in kind: in a
    third-party repo the commit-author email is attacker-controllable, so any
    address merely containing our login was counted as ours.
    """
    cmd = ["git", "clone", "--single-branch"]
    if branch:
        cmd += ["--branch", branch]
    cmd += [f"https://github.com/{repo}.git", str(dest)]
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=300, check=False)
    if r.returncode != 0 or not dest.exists():
        raise RuntimeError("clone_failed")
    fm = subprocess.run(["git", "fame", "-e", "-w", "--format", "json"],
                        cwd=str(dest), capture_output=True, text=True,
                        timeout=600, check=False)
    data = json.loads(fm.stdout) if fm.stdout.strip() else {}
    total = data.get("total", {}).get("loc", 0)
    ours = 0
    for row in data.get("data", []):
        if str(row[0]).strip().lower() in emails:
            ours += row[1]
    return ours, total


def update_loc(candidate_repos, totals, cached_ourloc, resync, emails):
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
    blame_moved(moved, ourloc, emails)
    return ourloc


def blame_moved(moved, ourloc, emails):
    """Clone, blame, and record each repo in `moved`, updating `ourloc` in
    place. The failure contract described in update_loc lives here."""
    for i, (repo, t) in enumerate(moved, 1):
        tmp = Path(tempfile.mkdtemp(prefix="impact-fame-"))
        try:
            ours, total = blame_repo(repo, t["branch"], tmp, emails)
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


def wilson(w, n, z):
    if n == 0:
        return 0.0
    p = w / n
    d = 1 + z * z / n
    return (p + z * z / (2 * n) - z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)) / d


def impact_rows(our_counts, total_of, gamma, z):
    """Rank repos by WilsonLowerBound(our/total, z) * our**gamma.
    our_counts: {repo: our}; total_of: repo -> total. Returns rows of
    (score, share_pct, our, total, repo), best first; our==0 rows (no
    contribution) are excluded."""
    rows = []
    for repo, w in our_counts.items():
        n = total_of(repo)
        if w and n:
            rows.append((wilson(w, n, z) * (w ** gamma),
                         w / n * 100, w, n, repo))
    rows.sort(reverse=True)
    return rows


def pr_table(prs, totals, insiders, knobs):
    our_merged = {}
    for pr in prs:
        if pr["merged"] and common.is_external(pr, insiders):
            r = pr["repository"]["nameWithOwner"]
            our_merged[r] = our_merged.get(r, 0) + 1
    return impact_rows(our_merged,
                       lambda r: (totals.get(r) or {}).get("merged_prs", 0),
                       knobs["pr_gamma"], knobs["z"])


def issue_table(issues, totals, insiders, knobs):
    our_completed = {}
    for it in issues:
        if (it["state"] == "CLOSED" and it["stateReason"] == "COMPLETED"
                and common.is_external(it, insiders)):
            r = it["repository"]["nameWithOwner"]
            our_completed[r] = our_completed.get(r, 0) + 1
    return impact_rows(our_completed,
                       lambda r: (totals.get(r) or {}).get("issues", 0),
                       knobs["issue_gamma"], knobs["z"])


def loc_table(ourloc, knobs):
    our_lines = {r: v["ours"] for r, v in ourloc.items() if "ours" in v}
    return impact_rows(our_lines,
                       lambda r: (ourloc.get(r) or {}).get("total", 0),
                       knobs["loc_gamma"], knobs["z"])


CARD_W = 520
COL_MINE = 290   # mine, right-anchored
COL_TOTAL = 340  # total, right-anchored
COL_SHARE = 390  # share, right-anchored
COL_SCORE = 500  # impact score, right-anchored (long header clears share)
BAR_X = 20
BAR_MAX_W = CARD_W - 2 * BAR_X  # rating bar at 100% = top score in section
REPO_CHARS = 34  # owner/name truncation budget (~225px at font-size 11 mono)


def truncate(s, n):
    return s if len(s) <= n else s[:n - 1] + "…"


def render_section(C, y, title, rows, accent):
    parts = [f'<text x="20" y="{y}" fill="{accent}" font-size="11" '
             f'font-weight="600">{title}</text>']
    y += 16
    parts.append(
        f'<text x="20" y="{y}" fill="{C["dim"]}" font-size="10">repo</text>'
        f'<text x="{COL_MINE}" y="{y}" text-anchor="end" fill="{C["dim"]}" font-size="10">mine</text>'
        f'<text x="{COL_TOTAL}" y="{y}" text-anchor="end" fill="{C["dim"]}" font-size="10">total</text>'
        f'<text x="{COL_SHARE}" y="{y}" text-anchor="end" fill="{C["dim"]}" font-size="10">share</text>'
        f'<text x="{COL_SCORE}" y="{y}" text-anchor="end" fill="{C["dim"]}" font-size="10">impact score</text>')
    y += 17
    top = rows[:TOP_N]
    if not top:
        parts.append(f'<text x="20" y="{y}" fill="{C["dim"]}" font-size="11">'
                     f'no external contributions yet</text>')
        y += 16
        return parts, y
    best = top[0][0] or 1
    for score, share, w, n, repo in top:
        bar_w = max(score / best * BAR_MAX_W, 2)
        parts.append(
            f'<text x="20" y="{y}" fill="{C["fg"]}" font-size="11">'
            f'{xml_escape(truncate(repo, REPO_CHARS))}</text>'
            f'<text x="{COL_MINE}" y="{y}" text-anchor="end" fill="{C["cyan"]}" font-size="11">{w}</text>'
            f'<text x="{COL_TOTAL}" y="{y}" text-anchor="end" fill="{C["dim"]}" font-size="11">{fmt_short(n)}</text>'
            f'<text x="{COL_SHARE}" y="{y}" text-anchor="end" fill="{C["gold"]}" font-size="11">{share:.1f}%</text>'
            f'<text x="{COL_SCORE}" y="{y}" text-anchor="end" fill="{accent}" font-size="11" font-weight="500">{score:.2f}</text>'
            # rating bar: length = score normalized to the section's top
            # score (row #1 = full width), Dota-hero-stats style
            f'<rect x="{BAR_X}" y="{y + 5}" width="{BAR_MAX_W}" height="3" rx="1.5" fill="{C["border"]}" fill-opacity="0.35"/>'
            f'<rect x="{BAR_X}" y="{y + 5}" width="{bar_w:.1f}" height="3" rx="1.5" fill="{accent}"/>')
        y += 22
    return parts, y


def render_impact(C, pr_rows, issue_rows, loc_rows):
    y = 88
    body = f"""
  <text x="20" y="34" fill="{C['gold']}" font-size="14" font-weight="600">external impact</text>
  <text x="20" y="52" fill="{C['dim']}" font-size="11">contribution to repos outside my own account and orgs</text>
  <line x1="20" y1="64" x2="{CARD_W - 20}" y2="64" stroke="{C['border']}"/>"""
    sections = (("Pull Requests", pr_rows, C["blue"]),
                ("Issues", issue_rows, C["green"]),
                ("Live Code", loc_rows, C["purple"]))
    for i, (title, rows, accent) in enumerate(sections):
        parts, y = render_section(C, y, title, rows, accent)
        body += "\n  " + "\n  ".join(parts)
        if i < len(sections) - 1:
            y += 12
            body += (f'\n  <line x1="20" y1="{y}" x2="{CARD_W - 20}" y2="{y}" '
                     f'stroke="{C["border"]}"/>')
            y += 24
    return base_card(C, CARD_W, y + 10, body)


def parse_args():
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
        token = Path(args.token_file).read_text(encoding="utf-8").strip()
    if not token:
        p.error("--token, --token-file, GH_TOKEN, or GH_TOKEN_FILE required")
    return args, token


def fetch_all(token, user, cache, resync):
    """Fetch every input the impact card needs. Returns
    (insiders, prs, prs_by_id, issues, totals, ourloc)."""
    # Identity first: everything downstream depends on knowing who we are
    # and which owners are insiders. Derived from the token's own account,
    # so joining an org needs no code or config change.
    me = common.fetch_identity(token, user, gql_fn=gql)
    insiders = me.insiders
    prs, prs_by_id = fetch_pull_requests(
        token, me.login, None if resync else cache.get("prs") or None)
    issues = fetch_issues(token, me.login)
    repos = sorted({n["repository"]["nameWithOwner"]
                    for n in prs + issues
                    if common.is_external(n, insiders)})
    totals = fetch_repo_totals(token, repos)
    merged_repos = {n["repository"]["nameWithOwner"] for n in prs
                    if n["merged"] and common.is_external(n, insiders)}
    ourloc = update_loc(merged_repos, totals,
                        {} if resync else cache.get("ourloc") or {},
                        resync, me.emails)
    return insiders, prs, prs_by_id, issues, totals, ourloc


def write_card(C, out, prs, issues, totals, ourloc, insiders, stale):
    """Render impact.svg from the inputs and write it, stamping the cache
    fetch time when stale. Returns the three section row lists."""
    knobs = metric_knobs()
    pr_rows = pr_table(prs, totals, insiders, knobs)
    issue_rows = issue_table(issues, totals, insiders, knobs)
    loc_rows = loc_table(ourloc, knobs)

    svg = render_impact(C, pr_rows, issue_rows, loc_rows)
    if stale:
        svg = stamp_cache_notice(C, svg, stale)
    (out / "impact.svg").write_text(svg)
    return pr_rows, issue_rows, loc_rows


def main():
    args, token = parse_args()

    C = THEMES[args.theme]
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    cache = {} if args.resync else load_cache(args.cache_file)

    stale = None
    try:
        insiders, prs, prs_by_id, issues, totals, ourloc = fetch_all(
            token, args.user, cache, args.resync)
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
        # Identity is cached alongside the data precisely so this path does
        # not need the network. A cache written before identity was cached
        # degrades to "the account itself", which can over-report externals
        # for one render; the next successful run repairs it.
        insiders = (frozenset(cache["insiders"]) if cache.get("insiders")
                    else common.insider_set(args.user, []))
    else:
        save_cache(args.cache_file, {
            "version": CACHE_VERSION,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "insiders": sorted(insiders),
            "prs": prs_by_id,
            "issues": issues,
            "totals": totals,
            "ourloc": ourloc,
        })

    pr_rows, issue_rows, loc_rows = write_card(
        C, out, prs, issues, totals, ourloc, insiders, stale)

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
