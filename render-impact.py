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
                PRs/issues decay from acceptance (7-day half-life, 0.05
                floor); live code does not. An undecayed 10.00 anchor exposes
                aging.

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
  BLAME_METHOD       targeted (default) | fame | both   — see CLAUDE.md
  CLONE_LOOKAHEAD    repos cloned ahead of the blame    (default 3)
  DEBUG_TIMING       per-phase timing for the whole run (default off)
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

Deps: Python stdlib + the `git` CLI + git-fame (pip) for the blame pass. Requires Python 3.9+.
"""
import argparse
import importlib.util
import json
import math
import os
import sys
import time
import urllib.error
from collections import namedtuple
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

REQUIRED_COMMON = 3
common.check_version(REQUIRED_COMMON)

CACHE_VERSION = 1
DEFAULT_CACHE_FILE = "/var/lib/gh-widgets/impact-cache.json"
TOP_N = 5
DECAY_GRACE_DAYS = 0.0
DECAY_HALF_LIFE_DAYS = 7.0
DECAY_FLOOR = 0.05
ImpactRow = namedtuple("ImpactRow", "score base share ours total repo")
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


# Validating entry point: the out-of-repo /root/oss-contrib/scripts/impact-picks.py depends on this check; removing it or switching to common.load_cache changes that consumer's behavior.
def load_cache(path):
    """Read the JSON cache, checked against THIS script's schema version."""
    cache = common.load_cache(path, CACHE_VERSION)
    common.validate_cache_shape(cache)
    return cache


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


def _load_loc_module():
    """Load the sibling live-code implementation beside this entry point."""
    path = Path(__file__).resolve().with_name("impact_loc.py")
    if not path.exists():
        raise SystemExit(
            f"error: render-impact.py cannot find its impact_loc.py at {path} "
            f"(install both files together)")
    spec = importlib.util.spec_from_file_location("impact_loc", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"error: cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_LOC_MODULE = _load_loc_module()
_LOC_MODULE.configure(common)

# Compatibility exports: the public renderer module keeps the live-code names
# at their original import path. The small wrappers pass the entry-point
# aliases through so existing tests and callers can patch them as before.
DEBUG_TIMING = _LOC_MODULE.DEBUG_TIMING
_TIMINGS = getattr(_LOC_MODULE, "_TIMINGS")
_PHASES = getattr(_LOC_MODULE, "_PHASES")
_T0 = getattr(_LOC_MODULE, "_T0")
BLAME_METHOD = _LOC_MODULE.BLAME_METHOD
_DISAGREEMENTS = getattr(_LOC_MODULE, "_DISAGREEMENTS")

# Keep the incidental module exports that existed before the split as well;
# callers should not need to know which implementation module owns them.
contextlib = _LOC_MODULE.contextlib
shutil = _LOC_MODULE.shutil
subprocess = _LOC_MODULE.subprocess
tempfile = _LOC_MODULE.tempfile
futures = _LOC_MODULE.futures

check_git_fame = _LOC_MODULE.check_git_fame
clone_repo = _LOC_MODULE.clone_repo
git_out = _LOC_MODULE.git_out
our_touched_files = _LOC_MODULE.our_touched_files
our_first_commit_date = _LOC_MODULE.our_first_commit_date
rename_closure = _LOC_MODULE.rename_closure
targeted_counts = _LOC_MODULE.targeted_counts
blamed_lines_for = _LOC_MODULE.blamed_lines_for
blame_repo = _LOC_MODULE.blame_repo
clone_lookahead = _LOC_MODULE.clone_lookahead


def _sync_loc_state():
    """Keep mutable compatibility globals aligned with the split module."""
    _LOC_MODULE.configure(common)
    setattr(_LOC_MODULE, "DEBUG_TIMING", DEBUG_TIMING)
    setattr(_LOC_MODULE, "BLAME_METHOD", BLAME_METHOD)
    setattr(_LOC_MODULE, "_TIMINGS", _TIMINGS)
    setattr(_LOC_MODULE, "_PHASES", _PHASES)
    setattr(_LOC_MODULE, "_T0", _T0)
    setattr(_LOC_MODULE, "_DISAGREEMENTS", _DISAGREEMENTS)


def timed_phase(name):
    """Keep the original timing context-manager entry point."""
    _sync_loc_state()
    return _LOC_MODULE.timed_phase(name)


def _record_timing(repo, clone_s, fame_s, total, wait_s=0.0):
    """Keep the original timing hook entry point."""
    _sync_loc_state()
    return getattr(_LOC_MODULE, "_record_timing")(
        repo, clone_s, fame_s, total, wait_s)


def print_timing_summary():
    """Keep the original timing-summary entry point."""
    _sync_loc_state()
    return _LOC_MODULE.print_timing_summary()


def counts_for(repo, dest, emails, clone_s=0.0, wait_s=0.0):
    """Keep the original configured-blame entry point."""
    _sync_loc_state()
    return _LOC_MODULE.counts_for(repo, dest, emails, clone_s, wait_s)


def print_method_comparison():
    """Keep the original method-comparison entry point."""
    _sync_loc_state()
    return _LOC_MODULE.print_method_comparison()


def prefetched_clones(moved, depth=None):
    """Keep the original patchable prefetch entry point."""
    _sync_loc_state()
    return _LOC_MODULE.prefetched_clones(
        moved, depth, clone_fn=clone_repo, lookahead_fn=clone_lookahead)


def blame_moved(moved, ourloc, emails):
    """Keep the original patchable blame-loop entry point."""
    _sync_loc_state()
    return _LOC_MODULE.blame_moved(
        moved, ourloc, emails, prefetch_fn=prefetched_clones,
        count_fn=counts_for)


def update_loc(candidate_repos, totals, cached_ourloc, resync, emails):
    """Keep the original live-line refresh entry point."""
    _sync_loc_state()
    return _LOC_MODULE.update_loc(
        candidate_repos, totals, cached_ourloc, resync, emails,
        blame_fn=blame_moved)


def wilson(w, n, z):
    if n == 0:
        return 0.0
    p = w / n
    d = 1 + z * z / n
    return (p + z * z / (2 * n) - z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)) / d


def contribution_freshness(stamps, now):
    """Mean bounded weight; missing or malformed dates fail loudly."""
    def weight(stamp):
        if not stamp:
            raise ValueError("impact timestamp is missing")
        try:
            accepted = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError(
                f"impact timestamp is invalid: {stamp!r}") from exc
        if accepted.tzinfo is None:
            raise ValueError(f"impact timestamp has no timezone: {stamp!r}")
        age_days = max(0.0, (now - accepted).total_seconds() / (24 * 60 * 60))
        if age_days <= DECAY_GRACE_DAYS:
            return 1.0
        remainder = 2 ** (-(age_days - DECAY_GRACE_DAYS) / DECAY_HALF_LIFE_DAYS)
        return DECAY_FLOOR + (1 - DECAY_FLOOR) * remainder

    return sum(weight(stamp) for stamp in stamps) / len(stamps) if stamps else 1.0


def impact_rows(our_counts, total_of, gamma, z, freshness_of=None):
    """Rank repos by decayed WilsonLowerBound(our/total, z) * our**gamma.
    Rows retain their undecayed base so normalisation cannot hide decay."""
    rows = []
    for repo, w in our_counts.items():
        n = total_of(repo)
        if w and n:
            base_score = wilson(w, n, z) * (w ** gamma)
            freshness = freshness_of(repo) if freshness_of else 1.0
            rows.append(ImpactRow(base_score * freshness, base_score,
                                  w / n * 100, w, n, repo))
    rows.sort(reverse=True)
    return rows


def pr_table(prs, totals, insiders, knobs):
    our_merged = {}
    merged_at = {}
    for pr in prs:
        if pr["merged"] and common.is_external(pr, insiders):
            r = pr["repository"]["nameWithOwner"]
            our_merged[r] = our_merged.get(r, 0) + 1
            merged_at.setdefault(r, []).append(pr.get("mergedAt") or pr.get("closedAt"))
    now = datetime.now(timezone.utc)
    return impact_rows(our_merged,
                       lambda r: (totals.get(r) or {}).get("merged_prs", 0),
                       knobs["pr_gamma"], knobs["z"],
                       lambda r: contribution_freshness(merged_at[r], now))


def issue_table(issues, totals, insiders, knobs):
    our_completed = {}
    completed_at = {}
    for it in issues:
        if (it["state"] == "CLOSED" and it["stateReason"] == "COMPLETED"
                and common.is_external(it, insiders)):
            r = it["repository"]["nameWithOwner"]
            our_completed[r] = our_completed.get(r, 0) + 1
            completed_at.setdefault(r, []).append(it.get("closedAt"))
    now = datetime.now(timezone.utc)
    return impact_rows(our_completed,
                       lambda r: (totals.get(r) or {}).get("issues", 0),
                       knobs["issue_gamma"], knobs["z"],
                       lambda r: contribution_freshness(completed_at[r], now))


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
BAR_MAX_W = CARD_W - 2 * BAR_X  # full width = strongest undecayed raw score
SCORE_MAX = 10.0  # display anchor for the strongest undecayed raw score
REPO_CHARS = 34  # owner/name truncation budget


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
    # Every undecayed row anchors this; a top-five-only anchor would jump.
    best = max(row.base for row in rows) or 1
    for row in top:
        bar_w = max(row.score / best * BAR_MAX_W, 2)
        scaled = row.score / best * SCORE_MAX
        parts.append(
            f'<text x="20" y="{y}" fill="{C["fg"]}" font-size="11">'
            f'{xml_escape(truncate(row.repo, REPO_CHARS))}</text>'
            f'<text x="{COL_MINE}" y="{y}" text-anchor="end" fill="{C["cyan"]}" font-size="11">{row.ours}</text>'
            f'<text x="{COL_TOTAL}" y="{y}" text-anchor="end" fill="{C["dim"]}" font-size="11">{fmt_short(row.total)}</text>'
            f'<text x="{COL_SHARE}" y="{y}" text-anchor="end" fill="{C["gold"]}" font-size="11">{row.share:.1f}%</text>'
            f'<text x="{COL_SCORE}" y="{y}" text-anchor="end" fill="{accent}" font-size="11" font-weight="500">{scaled:.2f}</text>'
            # Rating bar uses the same undecayed 10-point anchor as the label,
            # so a stale leader's bar can visibly stop short of full width.
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
    with timed_phase("identity"):
        me = common.fetch_identity(token, user, gql_fn=gql)
    insiders = me.insiders
    with timed_phase("fetch_prs"):
        prs, prs_by_id = fetch_pull_requests(
            token, me.login, None if resync else cache.get("prs") or None)
    with timed_phase("fetch_issues"):
        issues = fetch_issues(token, me.login)
    repos = sorted({n["repository"]["nameWithOwner"]
                    for n in prs + issues
                    if common.is_external(n, insiders)})
    with timed_phase("fetch_totals"):
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

    with timed_phase("render_svg"):
        svg = render_impact(C, pr_rows, issue_rows, loc_rows)
        if stale:
            svg = stamp_cache_notice(C, svg, stale)
        (out / "impact.svg").write_text(svg)
    return pr_rows, issue_rows, loc_rows


def main():
    args, token = parse_args()
    # Say which method ran, every run. The git-fame guard line was the health
    # check while git-fame did the counting; under `targeted` it never runs,
    # so its absence has to mean "not used" rather than "guard broken".
    print(f"blame-method: {BLAME_METHOD}", flush=True)
    if BLAME_METHOD in ("fame", "both"):
        check_git_fame()

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
        with timed_phase("save_cache"):
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
    print_method_comparison()
    print_timing_summary()


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode(errors='replace')}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
