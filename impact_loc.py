"""Git clone, blame, and live-line counting for ``render-impact.py``.

The impact renderer keeps the scoring and SVG paths in its entry-point module,
while this module owns the I/O-heavy live-code pass.  ``configure`` receives
the renderer's already-loaded ``ghwidgets_common`` module so the split does
not create a second copy of the shared runtime module.
"""

import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from concurrent import futures
from pathlib import Path
from typing import Any


common: Any = None


def configure(common_module: Any) -> None:
    """Bind the shared module used by the renderer's live-code pass."""
    global common
    common = common_module


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
    """Attribute a non-blame phase.

    Everything outside the blame pass used to land in one unattributed
    remainder -- a stable ~36s of a ~600s run, which is too big to leave
    unnamed: an unmeasured phase cannot be optimised and cannot be shown to be
    irrelevant either.
    """
    if not DEBUG_TIMING:
        yield
        return
    t0 = time.monotonic()
    try:
        yield
    finally:
        _PHASES[name] = _PHASES.get(name, 0.0) + time.monotonic() - t0


def _record_timing(repo, clone_s, fame_s, total, wait_s=0.0):
    """Record clone/blame timings when the optional instrumentation is on."""
    if DEBUG_TIMING:
        _TIMINGS.append((repo, clone_s, fame_s, total, wait_s))
        print(f"    timing {repo}: clone {clone_s:6.1f}s  wait {wait_s:6.1f}s  "
              f"fame {fame_s:6.1f}s  ({total:,} loc)", flush=True)


def print_timing_summary():
    """Report the measured blame and named non-blame phase totals."""
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


def load_repo_pins():
    """Read the commit manifest named by ``IMPACT_REPO_PINS``.

    An empty result means "blame whatever the default branch tips are", which
    is what production does. When a manifest IS named, every failure below is
    fatal: a pin that quietly degrades to live HEAD would leave two arms of a
    comparison blaming different trees while still reporting a clean run,
    which is the exact outcome pinning exists to prevent.
    """
    path = os.environ.get("IMPACT_REPO_PINS", "").strip()
    if not path:
        return {}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"error: IMPACT_REPO_PINS={path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SystemExit(f"error: IMPACT_REPO_PINS={path}: not an object")
    pins = {}
    for repo, entry in raw.items():
        head = entry.get("head") if isinstance(entry, dict) else None
        branch = entry.get("branch") or "" if isinstance(entry, dict) else None
        if not isinstance(head, str) or not head:
            raise SystemExit(
                f"error: IMPACT_REPO_PINS={path}: {repo} has no head commit")
        if not isinstance(branch, str):
            raise SystemExit(
                f"error: IMPACT_REPO_PINS={path}: {repo} has a non-string branch")
        pins[repo] = {"branch": branch, "head": head}
    return pins


def checkout_pin(dest, head):
    """Detach ``dest`` to ``head``, fetching the commit if the clone lacks it.

    A single-branch clone carries the pinned commit whenever it is an ancestor
    of the branch tip, which is the normal case for a manifest resolved before
    the run. A force-push can leave it absent, so ask the remote for it once
    before giving up.
    """
    for fetch_first in (False, True):
        if fetch_first:
            subprocess.run(["git", "-C", str(dest), "fetch", "--quiet",
                            "origin", head],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=300, check=False)
        r = subprocess.run(["git", "-C", str(dest), "checkout", "--quiet",
                            "--detach", head],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=300, check=False)
        if r.returncode == 0:
            return
    raise SystemExit(f"error: pinned commit {head} is unreachable in {dest}")


def clone_repo(repo, branch, dest, head=None):
    """Full-clone the default branch into ``dest`` and return its duration.

    ``head`` pins the checkout to one commit, so every arm of a comparison
    blames the same tree even when the branch moves between runs."""
    # Peak memory of a --resync is dominated by concurrent clones, not blame,
    # so cap what each clone's index-pack allocates. Its thread count and delta
    # window buy throughput that CLONE_LOOKAHEAD concurrent clones already
    # provide, while each thread holds its own delta window.
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
    if head:
        checkout_pin(dest, head)
    return time.monotonic() - t0


def git_out(dest, *args):
    """Run git in ``dest`` and return stdout, tolerating undecodable bytes."""
    return subprocess.run(["git", "-C", str(dest), *args], capture_output=True,
                          text=True, errors="replace", timeout=600,
                          check=False).stdout


def our_touched_files(dest, emails):
    """Return paths touched by any commit authored by one of ``emails``."""
    files = set()
    for email in emails:
        out = git_out(dest, "log", "HEAD", "--fixed-strings",
                      "--regexp-ignore-case", f"--author={email}",
                      "--name-only", "--pretty=format:", "-M")
        files.update(f for f in out.split("\n") if f)
    return files


def our_first_commit_date(dest, emails):
    """Return the author date of our earliest commit, or ``None``."""
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
    """Extend ``paths`` with everything they were renamed into."""
    # --diff-merges=first-parent: `git log --name-status` shows NOTHING for a
    # merge commit by default, and a rename performed during a merge is
    # therefore invisible. One real chain needed exactly that hop.
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
    # A chain can be discovered out of order, so iterate to a fixpoint rather
    # than assuming one pass down the log catches every hop.
    changed = True
    while changed:
        changed = False
        for old, new in events:
            if old in reachable and new not in reachable:
                reachable.add(new)
                changed = True
    return reachable


def targeted_counts(dest, emails):
    """Return ``(ours, total)`` without blaming every file."""
    # TWO greps, with DIFFERENT patterns, because git-fame uses `.` to decide
    # which files are text: a file containing only blank lines matches the
    # empty pattern but not `.`, so git-fame skips it entirely while a naive
    # count includes its lines.
    texts = {f[len("HEAD:"):] if f.startswith("HEAD:") else f
             for f in git_out(dest, "grep", "-I", "--name-only", ".",
                              "HEAD").split("\n") if f}
    total = 0
    for line in git_out(dest, "grep", "-I", "-c", "", "HEAD").split("\n"):
        if line:
            path, _, count = line.rpartition(":")
            path = path[len("HEAD:"):] if path.startswith("HEAD:") else path
            if path in texts:
                total += int(count)
    touched = our_touched_files(dest, emails)
    # Only pay for recovery scans when a path we touched vanished from the
    # tree -- the only way a rename can have hidden our lines.
    gone = touched - texts
    if gone:
        touched = rename_closure(dest, touched,
                                 since=our_first_commit_date(dest, emails))
        # `git log` does not record every link that `git blame` follows. A
        # same-basename current file is a safe, bounded fallback: blaming a
        # file we never touched yields zero.
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
    """Return lines in incremental blame output authored by ``emails``."""
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
    """Aggregate surviving LOC per author email with git-fame."""
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
BLAME_METHOD = os.environ.get("BLAME_METHOD", "targeted").strip().lower()
_DISAGREEMENTS = []


def counts_for(repo, dest, emails, clone_s=0.0, wait_s=0.0):
    """Return ``(ours, total)`` by the configured method."""
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
    """Report the fast path's agreement under ``BLAME_METHOD=both``."""
    if BLAME_METHOD != "both":
        return
    if _DISAGREEMENTS:
        print(f"\n=== targeted DISAGREES on {len(_DISAGREEMENTS)} repo(s) ===",
              flush=True)
        for repo, o, t, to, tt in _DISAGREEMENTS:
            print(f"  {repo}: ours {o:,} -> {to:,}   total {t:,} -> {tt:,}",
                  flush=True)
    else:
        print("\n=== targeted agreed with git-fame on every repo ===", flush=True)


def update_loc(candidate_repos, totals, cached_ourloc, resync, emails,
               *, blame_fn=None):
    """Refresh per-repo live-line counts, retrying failed or moved heads."""
    if blame_fn is None:
        blame_fn = blame_moved
    ourloc = dict(cached_ourloc)
    pins = load_repo_pins()
    moved = []
    for repo in sorted(candidate_repos):
        t = totals.get(repo)
        if not t or not t["head"]:
            continue  # repo gone/renamed: keep any cached entry, skip
        if pins:
            # Blaming an unpinned repo would put one repo's counts on a moving
            # target while every other repo is fixed, so refuse the whole run
            # rather than quietly produce a half-pinned comparison.
            pin = pins.get(repo)
            if not pin:
                raise SystemExit(f"error: {repo} is blamed but absent from "
                                 f"IMPACT_REPO_PINS")
            t = {**t, "head": pin["head"],
                 "branch": pin["branch"] or t["branch"]}
        entry = ourloc.get(repo) or {}
        if (not resync and entry.get("head") == t["head"]
                and ("ours" in entry or "error" in entry)):
            continue
        moved.append((repo, t))
    blame_fn(moved, ourloc, emails)
    return ourloc


def clone_lookahead():
    """Return the configured minimum number of clones to prefetch."""
    return max(1, int(common.env_float("CLONE_LOOKAHEAD", 3)))


def prefetched_clones(moved, depth=None, *, clone_fn=None, lookahead_fn=None):  # pylint: disable=too-many-locals
    """Yield clone results in order while running the next clones ahead."""
    if clone_fn is None:
        clone_fn = clone_repo
    if lookahead_fn is None:
        lookahead_fn = clone_lookahead
    depth = lookahead_fn() if depth is None else depth
    pool = futures.ThreadPoolExecutor(max_workers=depth,
                                      thread_name_prefix="prefetch")
    pending = {}
    dirs = {}

    def start(idx):
        if idx < len(moved) and idx not in pending:
            repo, t = moved[idx]
            dirs[idx] = Path(tempfile.mkdtemp(prefix="impact-fame-"))
            pending[idx] = pool.submit(clone_fn, repo, t["branch"], dirs[idx],
                                       t.get("head"))

    try:
        for i, entry in enumerate(moved):
            # Top the queue up BEFORE blocking, so the wait for repo i is also
            # clone time for i+1..i+depth.
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


def blame_moved(moved, ourloc, emails, *, prefetch_fn=None, count_fn=None):  # pylint: disable=too-many-locals
    """Clone, blame, and record each entry in ``moved``."""
    if prefetch_fn is None:
        prefetch_fn = prefetched_clones
    if count_fn is None:
        count_fn = counts_for
    n = len(moved)
    for i, (repo, t, tmp, clone_s, wait_s, err) in enumerate(
            prefetch_fn(moved), 1):
        try:
            if err is not None:
                raise err
            ours, total = count_fn(repo, tmp, emails,
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
