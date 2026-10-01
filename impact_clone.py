"""Git clone prefetch and scratch ownership for the impact live-code pass.

``impact_loc`` loads this sibling by path and registers it in ``sys.modules``
so path-loaded consumers share the same state as normal imports. Normal
imports work when this directory is on the import path. ``configure`` receives
the renderer's already-loaded shared module at runtime.
"""

import itertools
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
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


def _retry_readonly_scratch_removal(func, path, error):
    """Make read-only Git files writable and retry their removal."""
    if isinstance(error, tuple):
        error = error[1]
    is_permission_error = isinstance(error, PermissionError)
    is_removal = func in (os.unlink, os.rmdir)
    if not is_permission_error or not is_removal:
        raise error
    try:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
        func(path)
    except FileNotFoundError:
        pass


def remove_scratch_dir(path):
    """Remove one scratch directory and forget it from signal cleanup."""
    scratch = Path(path)
    _release_scratch_owner_lock(_SCRATCH_LOCKS.pop(scratch, None))
    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(scratch, onexc=_retry_readonly_scratch_removal)
        else:
            # Python 3.10 and 3.11 require the legacy callback argument.
            shutil.rmtree(  # pylint: disable=deprecated-argument
                scratch, onerror=_retry_readonly_scratch_removal)
    except FileNotFoundError:
        pass
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
