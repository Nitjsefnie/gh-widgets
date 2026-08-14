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
                PRs/issues decay after 30 days (one-year half-life, 0.5 floor);
                live code does not. An undecayed 10.00 anchor exposes aging.

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

Deps: Python stdlib + the `git` CLI + git-fame (pip) for the blame pass.
Requires Python 3.9+.
"""
import argparse
import contextlib
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
from collections import namedtuple
from concurrent import futures
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

DECAY_GRACE_DAYS = 30.0
DECAY_HALF_LIFE_DAYS = 365.0
DECAY_FLOOR = 0.5

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


def check_git_fame():
    """Verify the installed git-fame is the patched build, and SAY SO.

    This renderer runs unattended on a timer, so a check that is silent on
    success is indistinguishable from a check that never ran — the absence of
    the line has to be the alarm, which only works when success is noisy.
    Stock git-fame is degraded (serial blame, several times slower), not
    wrong, so this warns and continues rather than aborting.
    """
    # NOTE: `git-fame`, not `git fame`. Git's dispatcher rewrites
    # `git <cmd> --help` into `man git-<cmd>`, so `git fame --help` prints
    # "No manual entry for git-fame" and greps as if --jobs were absent —
    # even when the patched build IS installed. Invoke the binary directly.
    try:
        r = subprocess.run(["git-fame", "--help"], capture_output=True,
                           text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"git-fame: CHECK FAILED ({e}) — blame pass may not work", flush=True)
        return False
    if "--jobs" not in r.stdout:
        print("git-fame: WARNING — no --jobs, so this is STOCK git-fame: the "
              "blame pass is serial and several times slower. See CLAUDE.md "
              "for the pin.", flush=True)
        return False
    v = subprocess.run(["git-fame", "--version"], capture_output=True,
                       text=True, timeout=60, check=False).stdout.strip()
    print(f"git-fame: {v} with --jobs (patched build)", flush=True)
    return True


# Per-repo phase timings for the blame pass. Inert unless DEBUG_TIMING is set,
# and read once at import so the flag cannot change mid-run. This exists
# because the blame pass is the only slow part of a render and its cost splits
# across two very different resources -- `git clone` is network-bound and
# `git fame` is CPU-bound -- which a single wall-clock number cannot separate.
DEBUG_TIMING = bool(os.environ.get("DEBUG_TIMING"))
_TIMINGS = []
_PHASES = {}
_T0 = time.monotonic()


@contextlib.contextmanager
def timed_phase(name):
    """Attribute a non-blame phase. Everything outside the blame pass used to
    land in one unattributed remainder -- a stable ~36s of a ~600s run, which
    is too big to leave unnamed: an unmeasured phase cannot be optimised and
    cannot be shown to be irrelevant either."""
    if not DEBUG_TIMING:
        yield
        return
    t0 = time.monotonic()
    try:
        yield
    finally:
        _PHASES[name] = _PHASES.get(name, 0.0) + time.monotonic() - t0


def _record_timing(repo, clone_s, fame_s, total, wait_s=0.0):
    """`clone_s` is how long the clone took; `wait_s` is how much of that the
    blame loop actually blocked for. They diverge once clones are prefetched,
    and the difference IS the saving -- reporting only one of them would hide
    whether the overlap is working."""
    if DEBUG_TIMING:
        _TIMINGS.append((repo, clone_s, fame_s, total, wait_s))
        print(f"    timing {repo}: clone {clone_s:6.1f}s  wait {wait_s:6.1f}s  "
              f"fame {fame_s:6.1f}s  ({total:,} loc)", flush=True)


def print_timing_summary():
    """Report where the run actually went. Prints the phase totals AND the
    measured wall total, so a gap between them is visible rather than
    silently absorbed -- an unaccounted phase is exactly what per-phase timing
    is supposed to expose."""
    if not (DEBUG_TIMING and _TIMINGS):
        return
    clone = sum(t[1] for t in _TIMINGS)
    fame = sum(t[2] for t in _TIMINGS)
    loc = sum(t[3] for t in _TIMINGS)
    wait = sum(t[4] for t in _TIMINGS)
    print(f"\n=== blame pass timing ({len(_TIMINGS)} repos, {loc:,} loc) ===",
          flush=True)
    print(f"  clone total {clone:8.1f}s  (waited {wait:.1f}s, "
          f"{clone - wait:.1f}s hidden behind blame)", flush=True)
    print(f"  fame  total {fame:8.1f}s", flush=True)
    print(f"  phases sum  {wait + fame:8.1f}s  (blocking time, not clone wall)",
          flush=True)
    print("  slowest repos by fame time:", flush=True)
    for repo, c, f, n, w in sorted(_TIMINGS, key=lambda t: -t[2])[:10]:
        print(f"    {f:7.1f}s fame  {c:6.1f}s clone ({w:5.1f}s waited)  "
              f"{n:>10,} loc  {repo}", flush=True)
    for name, secs in sorted(_PHASES.items(), key=lambda kv: -kv[1]):
        print(f"  {name:<12}{secs:8.1f}s", flush=True)
    # The closing line is the point of all of this: named phases against the
    # real elapsed time. A residual that grows is an instrument with a hole
    # in it, not a fast run.
    named = wait + fame + sum(_PHASES.values())
    elapsed = time.monotonic() - _T0
    print(f"  ---\n  named       {named:8.1f}s of {elapsed:8.1f}s elapsed "
          f"({elapsed - named:.1f}s unattributed)", flush=True)


def clone_repo(repo, branch, dest):
    """Full-clone the default branch into `dest` (blame needs history, so NOT
    --depth 1). Returns the clone's own duration. Raises on failure."""
    # Peak memory of a --resync is now dominated by concurrent clones, not by
    # blame, so cap what each clone's `index-pack` allocates. Its thread count
    # and delta window buy throughput that CLONE_LOOKAHEAD concurrent clones
    # already provide, while each thread holds its own delta window -- which
    # is what made lookahead 8 cost 769MB against a 624.6MB budget.
    cmd = ["git", "-c", f"pack.threads={common.env_float('CLONE_PACK_THREADS', 1):.0f}",
           "-c", f"pack.windowMemory={int(common.env_float('CLONE_WINDOW_MB', 32))}m",
           "clone", "--single-branch"]
    if branch:
        cmd += ["--branch", branch]
    cmd += [f"https://github.com/{repo}.git", str(dest)]
    t0 = time.monotonic()
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=300, check=False)
    if r.returncode != 0 or not dest.exists():
        raise RuntimeError("clone_failed")
    return time.monotonic() - t0


def git_out(dest, *args):
    """Run git in `dest` and return stdout, tolerating undecodable bytes."""
    return subprocess.run(["git", "-C", str(dest), *args], capture_output=True,
                          text=True, errors="replace", timeout=600,
                          check=False).stdout


def our_touched_files(dest, emails):
    """Paths touched by any commit WE authored, on the cloned branch.

    A line can only blame to us if we authored the commit that last touched
    its file, so this is a superset of the files that can carry our lines.

    Two flags that are not optional, both of which fail by returning ZERO --
    which is indistinguishable from "we never contributed here":

    * --fixed-strings, because `--author` takes a REGEX and a GitHub noreply
      address like `75166987+user@users.noreply.github.com` contains `+`,
      which quantifies the preceding character and matches nothing.
    * --regexp-ignore-case, because the addresses are lower-cased for exact
      comparison while `--author` matching is case-sensitive.
    """
    files = set()
    for email in emails:
        out = git_out(dest, "log", "HEAD", "--fixed-strings",
                      "--regexp-ignore-case", f"--author={email}",
                      "--name-only", "--pretty=format:", "-M")
        files.update(f for f in out.split("\n") if f)
    return files


def our_first_commit_date(dest, emails):
    """Author date of our EARLIEST commit, or None if we have none here.

    Renames strictly older than that cannot have hidden our lines: at the time
    we committed, the file already had whatever name the rename gave it, so
    our own history records the post-rename path. Bounding the rename scan by
    it is what keeps the scan from walking the entire history of a repo we
    joined last month.
    """
    oldest = None
    for email in emails:
        out = git_out(dest, "log", "HEAD", "--fixed-strings",
                      "--regexp-ignore-case", f"--author={email}",
                      "--format=%at", "--reverse")
        first = out.split("\n", 1)[0].strip()
        if first.isdigit() and (oldest is None or int(first) < oldest):
            oldest = int(first)
    return oldest


def rename_closure(dest, paths, since=None):
    """Extend `paths` with everything they were renamed INTO, transitively.

    A rename made by somebody else after our commit moves our lines to a path
    our own history never mentions, so the path set our commits name is not
    enough. Following the chain forward recovers the current name.

    Over-approximating is FREE here and under-approximating is the whole bug:
    a file we never touched contributes zero lines, because only lines whose
    author matches ours are counted. So this deliberately follows every chain
    that starts at one of our paths, without trying to establish that the
    rename happened after our commit.
    """
    # --diff-merges=first-parent: `git log --name-status` shows NOTHING for a
    # merge commit by default, and a rename performed during a merge is
    # therefore invisible. One real chain needed exactly that hop
    # (.../tags/blocks/... -> .../tags/block/...), and without it 13 lines
    # stayed lost after every other hop resolved.
    scan = ["log", "HEAD", "--diff-filter=R", "--name-status", "-M",
            "--diff-merges=first-parent", "--format="]
    if since:
        scan.append(f"--since={since}")
    out = git_out(dest, *scan)
    events = []
    for line in out.split("\n"):
        if line.startswith("R"):
            parts = line.split("\t")
            if len(parts) == 3:
                events.append((parts[1], parts[2]))
    reachable = set(paths)
    # a chain can be discovered out of order, so iterate to a fixpoint rather
    # than assuming one pass down the log catches every hop
    changed = True
    while changed:
        changed = False
        for old, new in events:
            if old in reachable and new not in reachable:
                reachable.add(new)
                changed = True
    return reachable


def targeted_counts(dest, emails):
    """(ours, total) without blaming every file.

    `total` needs no blame at all: it is the line count of exactly the files
    git-fame would blame, which `git grep -I` already identifies. `ours` needs
    blame only for the files our own commits touched -- 4 of 349 on the repo
    this was first measured against.

    A rename performed by SOMEONE ELSE after our commit moves our lines to a
    path our own history never mentions, so this can under-count. That is why
    it is gated on agreeing with git-fame rather than trusted on its own.
    """
    # TWO greps, with DIFFERENT patterns, because git-fame uses `.` to decide
    # which files are text: a file containing only blank lines matches the
    # empty pattern but not `.`, so git-fame skips it entirely while a naive
    # count includes its lines. That was 5 lines of 320,722 on one repo and 1
    # of 1,651,048 on another -- small, and still a disagreement.
    texts = {f[len("HEAD:"):] if f.startswith("HEAD:") else f
             for f in git_out(dest, "grep", "-I", "--name-only", ".",
                              "HEAD").split("\n") if f}
    total = 0
    for line in git_out(dest, "grep", "-I", "-c", "", "HEAD").split("\n"):
        if line:
            path, _, count = line.rpartition(":")
            path = path[len("HEAD:"):] if path.startswith("HEAD:") else path
            # every line of a text file counts, blank ones included -- the `.`
            # pattern picks the FILES, not the lines
            if path in texts:
                total += int(count)
    touched = our_touched_files(dest, emails)
    # The recovery scans walk the whole history, so only pay for them when a
    # path we touched has actually vanished from the tree -- the only way a
    # rename can have hidden our lines. Most repos skip this entirely.
    gone = touched - texts
    if gone:
        touched = rename_closure(dest, touched,
                                 since=our_first_commit_date(dest, emails))
        # `git log` does not record every link that `git blame` follows: blame
        # re-detects renames during its own walk, and one real case moved
        # resources/.../pickaxe.json to generated/.../pickaxe.json with no R
        # record joining them. Same basename, so add every current file
        # sharing a vanished path's name. Blaming a file we never touched
        # yields zero, so this can only cost time, never accuracy -- and it is
        # scoped to the paths that actually went missing.
        by_base = {}
        for path in texts:
            by_base.setdefault(path.rsplit("/", 1)[-1], []).append(path)
        for path in gone:
            touched.update(by_base.get(path.rsplit("/", 1)[-1], ()))
    ours = 0
    for fname in touched & texts:
        out = git_out(dest, "blame", "--incremental", "-w", "HEAD", "--", fname)
        ours += blamed_lines_for(out, emails)
    return ours, total


def blamed_lines_for(blame_out, emails):
    """Lines in `git blame --incremental` output authored by `emails`.

    Only the totals are wanted, so adjacent chunks of one commit need no
    re-joining: summing is order- and grouping-independent. Commit identity
    appears only the FIRST time a commit shows up, so it is remembered per
    sha rather than re-read per chunk.
    """
    seen = {}
    ours = 0
    sha = None
    nlines = 0
    for line in blame_out.split("\n"):
        head = line.split(" ")
        if len(head) == 4 and len(head[0]) >= 40 and head[3].isdigit():
            sha, nlines = head[0], int(head[3])
        elif sha is None:
            continue
        elif line.startswith("author-mail <") and line.endswith(">"):
            seen[sha] = line[13:-1].strip().lower()
        elif line.startswith("filename "):
            if seen.get(sha) in emails:
                ours += nlines
            sha = None
    return ours


def blame_repo(repo, dest, emails, clone_s=0.0, wait_s=0.0):
    """Aggregate surviving LOC per author email with git-fame over the clone
    already at `dest`, and return (ours, total). The clone is deleted by the
    caller. Raises on any failure.

    `emails` is the derived set of addresses that count as ours, matched
    EXACTLY. This used to be a substring test, which was wrong in kind: in a
    third-party repo the commit-author email is attacker-controllable, so any
    address merely containing our login was counted as ours.
    """
    t1 = time.monotonic()
    fm = subprocess.run(["git", "fame", "-e", "-w", "--format", "json"],
                        cwd=str(dest), capture_output=True, text=True,
                        timeout=600, check=False)
    fame_s = time.monotonic() - t1
    data = json.loads(fm.stdout) if fm.stdout.strip() else {}
    total = data.get("total", {}).get("loc", 0)
    ours = 0
    for row in data.get("data", []):
        if str(row[0]).strip().lower() in emails:
            ours += row[1]
    _record_timing(repo, clone_s, fame_s, total, wait_s)
    return ours, total


# How the per-repo line counts are produced:
#   targeted - blame only the files our own commits touched (the default)
#   fame     - git-fame over every file (the reference)
#   both     - run BOTH and fail loudly on any disagreement
# `targeted` is the default again, on 59/59 agreement measured WITH
# production's GH_EXTRA_EMAILS -- the address set that renders the real card.
# An earlier 59/59 used only the derived noreply addresses and was worthless:
# re-run with the real set it was 54/59, under-counting by up to 99% on one
# repo. Every future measurement of this must carry the production addresses.
#
# It is a verified superset, not a proof: the ground truth is blame's own
# rename detection, which is not reconstructible from `git log`. What makes it
# safe to ship is the weekly `both` audit in gitfame-resync-memory.yml, which
# re-checks the whole fleet and fails on any disagreement.
BLAME_METHOD = os.environ.get("BLAME_METHOD", "targeted").strip().lower()
_DISAGREEMENTS = []


def counts_for(repo, dest, emails, clone_s=0.0, wait_s=0.0):
    """Return (ours, total) by the configured method, checking the fast path
    against the reference when asked to."""
    if BLAME_METHOD == "targeted":
        t1 = time.monotonic()
        ours, total = targeted_counts(dest, emails)
        _record_timing(repo, clone_s, time.monotonic() - t1, total, wait_s)
        return ours, total

    ours, total = blame_repo(repo, dest, emails, clone_s=clone_s, wait_s=wait_s)
    if BLAME_METHOD == "both":
        t1 = time.monotonic()
        t_ours, t_total = targeted_counts(dest, emails)
        secs = time.monotonic() - t1
        agree = (t_ours, t_total) == (ours, total)
        if not agree:
            _DISAGREEMENTS.append((repo, ours, total, t_ours, t_total))
        print(f"    compare {repo}: fame ({ours:,}, {total:,}) vs targeted "
              f"({t_ours:,}, {t_total:,}) {'ok' if agree else 'MISMATCH'} "
              f"in {secs:.1f}s", flush=True)
    return ours, total


def print_method_comparison():
    """Report the fast path's agreement. Silence would be indistinguishable
    from the comparison never having run, so this always prints under `both`."""
    if BLAME_METHOD != "both":
        return
    if _DISAGREEMENTS:
        print(f"\n=== targeted DISAGREES on {len(_DISAGREEMENTS)} repo(s) ===",
              flush=True)
        for repo, o, t, to, tt in _DISAGREEMENTS:
            print(f"  {repo}: ours {o:,} -> {to:,}   total {t:,} -> {tt:,}",
                  flush=True)
    else:
        print("\n=== targeted agreed with git-fame on every repo ===",
              flush=True)


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


def clone_lookahead():
    """How many repos to clone ahead of the blame consuming them.

    One-deep hid a measured ~60s of clone per run but still left 115-131s of
    WAITING, because a clone only overlaps the single blame next to it and
    the blames are not all long. Going deeper overlaps a clone with several
    blames. The cost is linear in disk (that many extra checkouts) and in
    concurrent network transfers, so this is bounded and configurable rather
    than "as deep as possible".
    """
    # env_float, not a bespoke int reader: it already hard-errors on a typo
    # instead of silently reverting to the default, which is the property
    # that matters for a tuning knob.
    return max(1, int(common.env_float("CLONE_LOOKAHEAD", 3)))


def prefetched_clones(moved, depth=None):
    """Yield `(repo, t, dest, clone_s, wait_s, error)` per entry of `moved`,
    with the next `depth` repos' clones already running in the background.

    Clone and blame contend for nothing -- `git clone` waits on the network,
    `git fame` saturates the CPUs -- so running clones ahead hides them behind
    blame time the loop was otherwise spending idle.

    A clone failure is handed to the consumer as `error` rather than raised,
    so the caller's per-repo failure contract still applies and, crucially,
    the prefetch chain keeps running instead of stopping at the first bad
    repo. The consumer owns deleting each `dest` it is given.
    """
    depth = clone_lookahead() if depth is None else depth
    pool = futures.ThreadPoolExecutor(max_workers=depth, thread_name_prefix="prefetch")
    pending = {}
    dirs = {}

    def start(idx):
        if idx < len(moved) and idx not in pending:
            repo, t = moved[idx]
            dirs[idx] = Path(tempfile.mkdtemp(prefix="impact-fame-"))
            pending[idx] = pool.submit(clone_repo, repo, t["branch"], dirs[idx])

    try:
        for i, entry in enumerate(moved):
            # top the queue back up BEFORE blocking, so the wait for repo i
            # is also clone time for i+1..i+depth
            for ahead in range(i, i + depth + 1):
                start(ahead)
            t0 = time.monotonic()
            try:
                clone_s = pending.pop(i).result()
                err = None
            except Exception as e:  # pylint: disable=broad-except
                clone_s, err = 0.0, e
            yield (entry[0], entry[1], dirs.pop(i), clone_s,
                   time.monotonic() - t0, err)
    finally:
        for fut in pending.values():
            fut.cancel()
        pool.shutdown(wait=True)
        for unused in dirs.values():
            shutil.rmtree(unused, ignore_errors=True)


def blame_moved(moved, ourloc, emails):
    """Clone, blame, and record each repo in `moved`, updating `ourloc` in
    place. The failure contract described in update_loc lives here."""
    n = len(moved)
    for i, (repo, t, tmp, clone_s, wait_s, err) in enumerate(prefetched_clones(moved), 1):
        try:
            if err is not None:
                raise err
            ours, total = counts_for(repo, tmp, emails,
                                     clone_s=clone_s, wait_s=wait_s)
            ourloc[repo] = {"ours": ours, "total": total,
                            "branch": t["branch"], "head": t["head"]}
            print(f"loc [{i}/{n}] ours {ours:>7,} / {total:>8,} "
                  f"({ours / total * 100 if total else 0:4.1f}%)  {repo}",
                  flush=True)
        except Exception as e:  # pylint: disable=broad-except
            old = ourloc.get(repo) or {}
            if "ours" in old:
                print(f"loc [{i}/{n}] FAIL (kept old count) "
                      f"{repo}: {str(e)[:60]}", flush=True)
            else:
                ourloc[repo] = {"error": str(e)[:80], "head": t["head"]}
                print(f"loc [{i}/{n}] FAIL {repo}: {str(e)[:60]}", flush=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def wilson(w, n, z):
    if n == 0:
        return 0.0
    p = w / n
    d = 1 + z * z / n
    return (p + z * z / (2 * n) - z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)) / d


def contribution_freshness(stamps, now):
    """Mean bounded weight; unknown dates retain full credit."""
    def weight(stamp):
        if not stamp:
            return 1.0
        accepted = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
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
