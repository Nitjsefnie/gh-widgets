"""Git clone, blame, and live-line counting for ``render-impact.py``.

The impact renderer keeps the scoring and SVG paths in its entry-point module,
while this module owns the I/O-heavy live-code pass.  ``configure`` receives
the renderer's already-loaded ``ghwidgets_common`` module so the split does
not create a second copy of the shared runtime module.
"""

import contextlib
import itertools
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from concurrent import futures
from pathlib import Path
from typing import Any


common: Any = None
_CLONE_PROCESSES = set()
_CLONE_STARTING = set()
_CLONE_PROCESSES_LOCK = threading.Lock()
_CLONE_SHUTDOWN = False
# next(count) elects one handler in a single GIL-held C operation. A nested
# signal callback returns immediately instead of re-entering cleanup.
_SIGNAL_CLEANUP_CLAIMS = itertools.count()


def configure(common_module: Any) -> None:
    """Bind the shared module used by the renderer's live-code pass."""
    global common
    common = common_module


def _signal_clone_process(proc, signum):
    """Signal one tracked clone process, including its POSIX descendants."""
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signum)
        elif signum == signal.SIGTERM:
            if proc.poll() is None:
                proc.terminate()
        elif proc.poll() is None:
            proc.kill()
    except ProcessLookupError:
        pass


def _stop_clone_processes(processes, grace_s=1.0):
    """Terminate clone writers, escalate, and reap each tracked child."""
    processes = tuple(processes)
    for proc in processes:
        _signal_clone_process(proc, signal.SIGTERM)
    deadline = time.monotonic() + grace_s
    for proc in processes:
        try:
            proc.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
    for proc in processes:
        if os.name == "posix":
            _signal_clone_process(proc, signal.SIGKILL)
        elif proc.poll() is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
    for proc in processes:
        proc.wait()


class _CloneLaunch:
    """Represent a clone spawn that has not reached process registration."""

    def __init__(self):
        self.registered = threading.Event()


def _run_clone_command(cmd, timeout=300):
    """Run and track a clone-path Git child until it has been reaped."""
    # Python dispatches signals on the main thread. Keep Popen and its registry
    # lock off that thread so a signal can wait for an atomic spawn/register
    # operation without blocking the thread that must finish it.
    if threading.current_thread() is threading.main_thread():
        result = []
        failure = []

        def run_in_spawn_thread():
            try:
                result.append(_run_clone_command_worker(cmd, timeout))
            except BaseException as exc:  # propagate the worker's exact error
                failure.append(exc)

        worker = threading.Thread(target=run_in_spawn_thread,
                                  name="impact-clone-spawn")
        worker.start()
        worker.join()
        if failure:
            raise failure[0]
        return result[0]
    return _run_clone_command_worker(cmd, timeout)


def _run_clone_command_worker(cmd, timeout):
    """Run the Popen and registry transition away from the signal thread."""
    launch = _CloneLaunch()
    # Publish the pending launch before checking the shutdown gate. If a signal
    # arrives after publication, its handler waits for this exact transition;
    # if it arrives before publication, the worker sees the closed gate.
    _CLONE_STARTING.add(launch)
    with _CLONE_PROCESSES_LOCK:
        try:
            if _CLONE_SHUTDOWN:
                raise RuntimeError("clone interrupted by renderer shutdown")
            # CI covers Linux, macOS, and Windows. POSIX gets a private process
            # group so signal cleanup also stops Git's helper descendants; on
            # Windows, retain and terminate the direct Popen child portably.
            proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=os.name == "posix")
            _CLONE_PROCESSES.add(proc)
        finally:
            launch.registered.set()
            _CLONE_STARTING.discard(launch)
    try:
        with proc:
            try:
                returncode = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _stop_clone_processes((proc,))
                raise
    finally:
        with _CLONE_PROCESSES_LOCK:
            _CLONE_PROCESSES.discard(proc)
    return subprocess.CompletedProcess(cmd, returncode)


def _wait_for_clone_launches():
    """Wait for spawn attempts published before shutdown closed the gate."""
    for launch in tuple(_CLONE_STARTING):
        launch.registered.wait()


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
            _run_clone_command(
                ["git", "-C", str(dest), "fetch", "--quiet", "origin", head],
                timeout=300)
        r = _run_clone_command(
            ["git", "-C", str(dest), "checkout", "--quiet", "--detach",
             head], timeout=300)
        if r.returncode == 0:
            return
    raise SystemExit(f"error: pinned commit {head} is unreachable in {dest}")


def clone_repo(repo, branch, dest, head=None):
    """Full-clone the default branch into ``dest`` and return its duration.

    ``head`` pins the checkout to one commit, so every arm of a comparison
    blames the same tree even when the branch moves between runs."""
    # Clone-size policy: keep complete branch history and blobs because a
    # partial clone would change blame results. A clone has a 300s deadline;
    # pack.threads/pack.windowMemory cap index-pack working memory; lookahead
    # is capped at 16 below (measurement jobs use up to 8). There is no byte
    # quota on a single full clone, so the deadline bounds how long network
    # and scratch-disk growth can continue without truncating repository data.
    # Each later git path/blame command has a 600s deadline and a stdout cap
    # (GIT_OUTPUT_CAP_MB, default 512 MiB), independent of clone size.
    # Peak memory of a --resync is dominated by concurrent clones, not blame,
    # so cap what each clone's index-pack allocates. Its thread count and delta
    # window buy throughput that CLONE_LOOKAHEAD concurrent clones already
    # provide, while each thread holds its own delta window.
    cmd = ["git", "-c", f"pack.threads={common.env_float('CLONE_PACK_THREADS', 1):.0f}",
           "-c", f"pack.windowMemory={int(common.env_float('CLONE_WINDOW_MB', 32))}m",
           "clone", "--single-branch"]
    # CLONE_SOURCE_DIR points at local <owner>__<name> mirrors for offline
    # benchmark runs; without a matching mirror, the normal GitHub clone stays.
    source_dir = os.environ.get("CLONE_SOURCE_DIR")
    local_repo = (Path(source_dir) / repo.replace("/", "__")
                  if source_dir else None)
    if local_repo is not None and local_repo.is_dir():
        # Use the mirror's default branch so its advertised HEAD is honored.
        cmd += [str(local_repo), str(dest)]
    else:
        if branch:
            cmd += ["--branch", branch]
        cmd += [f"https://github.com/{repo}.git", str(dest)]
    t0 = time.monotonic()
    r = _run_clone_command(cmd, timeout=300)
    if r.returncode != 0 or not dest.exists():
        raise RuntimeError("clone_failed")
    if head:
        checkout_pin(dest, head)
    return time.monotonic() - t0


def _git_output_cap_bytes():
    """Return the per-command stdout cap, defaulting to 512 MiB."""
    # 512 MiB is over six times the linear extrapolation of the measured 5.1 MiB
    # incremental blame output on a 107k-loc repo, with headroom for today's
    # largest 1.65M-loc repo while putting a firm ceiling on pathological output.
    cap_mb = common.env_float("GIT_OUTPUT_CAP_MB", 512)
    return max(1, int(cap_mb * 1024 * 1024))


def _kill_process_tree(proc):
    """Kill a command and its children when the platform supports groups."""
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif proc.poll() is None:
        proc.kill()


def _read_bounded_stdout(proc, output, limit, overflow, read_errors):
    """Read one command's stdout and kill its process group on overflow."""
    stream = proc.stdout
    if stream is None:
        return
    try:
        fd = stream.fileno()
        while True:
            remaining = limit + 1 - len(output)
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                return
            output.extend(chunk)
            if len(output) > limit:
                overflow.set()
                _kill_process_tree(proc)
                return
    except OSError as exc:
        read_errors.append(exc)
        _kill_process_tree(proc)


def _run_bounded(cmd, *, cwd=None, timeout=600):
    """Capture at most the configured stdout bytes before killing the child."""
    limit = _git_output_cap_bytes()
    with subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL,
                          start_new_session=os.name == "posix") as proc:
        output = bytearray()
        overflow = threading.Event()
        read_errors = []
        reader = threading.Thread(
            target=_read_bounded_stdout,
            args=(proc, output, limit, overflow, read_errors),
            name="git-output-reader", daemon=True)
        deadline = time.monotonic() + timeout
        reader.start()
        timed_out = False
        try:
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
            if not timed_out:
                reader.join(max(0, deadline - time.monotonic()))
                timed_out = reader.is_alive()
            if timed_out:
                _kill_process_tree(proc)
                proc.wait()
                reader.join()
        except BaseException:
            _kill_process_tree(proc)
            proc.wait()
            reader.join()
            raise
        if timed_out:
            raise subprocess.TimeoutExpired(cmd, timeout)
        if read_errors:
            raise read_errors[0]
        if overflow.is_set():
            raise RuntimeError(f"{cmd[0]} output exceeded {limit}-byte cap")
        return subprocess.CompletedProcess(cmd, proc.returncode, bytes(output))


def git_out(dest, *args):
    """Run git in ``dest`` and return stdout, tolerating undecodable bytes."""
    cmd = ["git", "-C", str(dest), "-c", "core.quotePath=false", *args]
    result = _run_bounded(cmd, timeout=600)
    stdout = result.stdout.decode("utf-8", errors="replace")
    result.stdout = b""
    grep_no_matches = args and args[0] == "grep" and result.returncode == 1
    if result.returncode and not grep_no_matches:
        raise subprocess.CalledProcessError(result.returncode, cmd, stdout)
    return stdout


def _countable_path(path):
    """Whether line-based consumers can safely identify this Git path."""
    # NUL-delimited Git output preserves control bytes until this shared check;
    # exclude such paths from both the text and touched sets so counts agree.
    return not any(ord(char) < 32 or ord(char) == 127 for char in path)


def _without_head_prefix(path):
    """Remove git-grep's tree prefix from a path, when present."""
    return path[len("HEAD:"):] if path.startswith("HEAD:") else path


def _text_line_total(dest, texts):
    """Sum grep counts for the text paths retained by the shared path filter."""
    counts = git_out(dest, "grep", "-I", "-c", "-z", "", "HEAD")
    total = 0
    cursor = 0
    while cursor < len(counts):
        path_end = counts.find("\0", cursor)
        if path_end < 0:
            break
        count_end = counts.find("\n", path_end + 1)
        if count_end < 0:
            break
        path = _without_head_prefix(counts[cursor:path_end])
        count = counts[path_end + 1:count_end]
        if path in texts and _countable_path(path) and count:
            total += int(count)
        cursor = count_end + 1
    return total


def our_touched_files(dest, emails):
    """Return paths touched by any commit authored by one of ``emails``."""
    files = set()
    for email in emails:
        out = git_out(dest, "log", "HEAD", "--fixed-strings",
                      "--regexp-ignore-case", f"--author={email}",
                      "--name-only", "-z", "--pretty=format:", "-M")
        files.update(f for f in out.split("\0")
                     if f and _countable_path(f))
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
            "-z", "--diff-merges=first-parent", "--format="]
    if since:
        scan.append(f"--since={since}")
    out = git_out(dest, *scan)
    events = []
    fields = out.split("\0")
    index = 0
    while index + 2 < len(fields):
        status, old, new = fields[index:index + 3]
        if status.startswith("R") and _countable_path(old) \
                and _countable_path(new):
            events.append((old, new))
        index += 3
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
    texts = {_without_head_prefix(path)
             for path in git_out(dest, "grep", "-I", "--name-only", "-z",
                                 ".", "HEAD").split("\0")
             if path and _countable_path(_without_head_prefix(path))}
    total = _text_line_total(dest, texts)
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
    cmd = ["git", "fame", "-e", "-w", "--format", "json"]
    fm = _run_bounded(cmd, cwd=str(dest), timeout=600)
    fame_s = time.monotonic() - t1
    if fm.returncode:
        stdout = fm.stdout.decode("utf-8", errors="replace")
        fm.stdout = b""
        raise subprocess.CalledProcessError(fm.returncode, fm.args, stdout)
    fame_stdout = fm.stdout.decode("utf-8", errors="replace")
    fm.stdout = b""
    if not fame_stdout.strip():
        raise ValueError("empty git-fame output")
    data = json.loads(fame_stdout)
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
    """Report the fast path's agreement under ``BLAME_METHOD=both``.

    Returns the number of disagreeing repos so a caller can fail on it; the
    count is zero under any other method, where nothing was compared.
    """
    if BLAME_METHOD != "both":
        return 0
    if _DISAGREEMENTS:
        print(f"\n=== targeted DISAGREES on {len(_DISAGREEMENTS)} repo(s) ===",
              flush=True)
        for repo, o, t, to, tt in _DISAGREEMENTS:
            print(f"  {repo}: ours {o:,} -> {to:,}   total {t:,} -> {tt:,}",
                  flush=True)
    else:
        print("\n=== targeted agreed with git-fame on every repo ===", flush=True)
    return len(_DISAGREEMENTS)


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
        # Only a real count at an unchanged head suppresses a re-clone. An error
        # entry records a failure, not a result: skipping it would freeze the
        # repo out of the card until its HEAD moved or the weekly --resync
        # re-blamed everything. The retry needs no cap of its own --
        # update_loc runs once per render, so a hard-down repo costs one clone
        # attempt per run, and that attempt is a subprocess carrying
        # clone_repo's own 300s timeout, not an unbounded wait.
        if not resync and entry.get("head") == t["head"] and "ours" in entry:
            continue
        moved.append((repo, t))
    blame_fn(moved, ourloc, emails)
    return ourloc


def clone_lookahead():
    """Return clone prefetch depth, clamped to the resource policy ceiling."""
    # Keep room above the measured depth-8 workflow while bounding transfers
    # and scratch space when an environment value is accidentally huge.
    return min(16, max(1, int(common.env_float("CLONE_LOOKAHEAD", 3))))


_SCRATCH_DIRS = set()
_SCRATCH_LOCKS = {}
_SIGNAL_HANDLERS_INSTALLED = False
_SCRATCH_OWNER_LOCK_SUFFIX = ".owner.lock"


def _scratch_owner_lock_path(scratch):
    """Return the adjacent lock path, keeping the Git destination empty."""
    path = Path(scratch)
    return path.with_name(path.name + _SCRATCH_OWNER_LOCK_SUFFIX)


def _acquire_scratch_owner_lock(scratch, create):
    """Acquire an exclusive owner lock, or return None when it is absent."""
    lock_path = _scratch_owner_lock_path(scratch)
    try:
        # The returned handle deliberately stays open while this process owns
        # the scratch, so the context manager ends in its caller's cleanup.
        stream = lock_path.open("a+b" if create else "r+b")  # pylint: disable=consider-using-with
    except FileNotFoundError:
        if create:
            raise
        return None
    try:
        if os.name == "nt":
            import msvcrt  # pylint: disable=import-outside-toplevel
            if lock_path.stat().st_size == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl  # pylint: disable=import-outside-toplevel
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        stream.close()
        raise
    return stream


def _release_scratch_owner_lock(stream):
    """Release a scratch ownership lock and close its descriptor."""
    if stream is None:
        return
    try:
        if os.name == "nt":
            import msvcrt  # pylint: disable=import-outside-toplevel
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl  # pylint: disable=import-outside-toplevel
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        stream.close()


def register_scratch_dir(path):
    """Register a new scratch path and hold its cross-process owner lock."""
    scratch = Path(path)
    _SCRATCH_DIRS.add(scratch)
    try:
        _SCRATCH_LOCKS[scratch] = _acquire_scratch_owner_lock(
            scratch, create=True)
    except OSError:
        _SCRATCH_DIRS.discard(scratch)
        raise


def remove_scratch_dir(path):
    """Remove one scratch directory and forget it from signal cleanup."""
    scratch = Path(path)
    _release_scratch_owner_lock(_SCRATCH_LOCKS.pop(scratch, None))
    shutil.rmtree(scratch, ignore_errors=True)
    try:
        _scratch_owner_lock_path(scratch).unlink()
    except FileNotFoundError:
        pass
    _SCRATCH_DIRS.discard(scratch)


def _handle_scratch_signal(signum, _frame):
    """Remove every in-flight clone before exiting with signal status."""
    global _CLONE_SHUTDOWN
    if next(_SIGNAL_CLEANUP_CLAIMS):
        return
    # Do not acquire _CLONE_PROCESSES_LOCK here: the interrupted main thread
    # may have been inside Popen while holding it. Closing the gate first and
    # waiting on pre-published launch records preserves registration without
    # taking that lock from the signal handler.
    _CLONE_SHUTDOWN = True
    _wait_for_clone_launches()
    processes = tuple(_CLONE_PROCESSES)
    _stop_clone_processes(processes)
    for scratch in tuple(_SCRATCH_DIRS):
        remove_scratch_dir(scratch)
    os._exit(128 + signum)


def install_scratch_signal_handlers():
    """Install clone cleanup handlers once, from the main thread only."""
    global _SIGNAL_HANDLERS_INSTALLED
    if _SIGNAL_HANDLERS_INSTALLED \
            or threading.current_thread() is not threading.main_thread():
        return
    signal.signal(signal.SIGTERM, _handle_scratch_signal)
    signal.signal(signal.SIGINT, _handle_scratch_signal)
    _SIGNAL_HANDLERS_INSTALLED = True


def scavenge_scratch_dirs():
    """Remove abandoned impact clones older than the configured age."""
    # Age is the cheap stale-candidate gate. A cross-process owner lock is
    # required before deletion so a live sibling render remains untouched.
    max_age = max(0, common.env_float("IMPACT_SCRATCH_MAX_AGE_HOURS", 24))
    cutoff = time.time() - max_age * 60 * 60
    active = set(_SCRATCH_DIRS)
    for scratch in Path(tempfile.gettempdir()).glob("impact-fame-*"):
        if scratch in active or scratch.is_symlink() or not scratch.is_dir():
            continue
        try:
            if scratch.stat().st_mtime >= cutoff:
                continue
            owner_lock = _acquire_scratch_owner_lock(scratch, create=False)
        except OSError:
            continue
        _release_scratch_owner_lock(owner_lock)
        remove_scratch_dir(scratch)


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
            scratch = Path(tempfile.mkdtemp(prefix="impact-fame-"))
            try:
                register_scratch_dir(scratch)
            except OSError:
                shutil.rmtree(scratch, ignore_errors=True)
                raise
            dirs[idx] = scratch
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
        for scratch in dirs.values():
            remove_scratch_dir(scratch)


def blame_moved(moved, ourloc, emails, *, prefetch_fn=None, count_fn=None):  # pylint: disable=too-many-locals
    """Clone, blame, and record each entry in ``moved``."""
    if prefetch_fn is None:
        prefetch_fn = prefetched_clones
    if count_fn is None:
        count_fn = counts_for
    install_scratch_signal_handlers()
    scavenge_scratch_dirs()
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
            remove_scratch_dir(tmp)
