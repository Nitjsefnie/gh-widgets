"""Cache validation, locking, and atomic writes shared by the renderers.

Plain sibling imports are intentional: the repository root is ``sys.path[0]``
in development and tests, while ``/usr/local/bin`` is ``sys.path[0]`` for each
deployed renderer.
"""
import contextlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

# The cache lock uses each platform's own exclusive-lock API: flock(2) on
# POSIX, msvcrt.locking on Windows. Import conditionally so the module loads
# on both. Where neither exists, the lock is never acquired and cache writers
# fail the run rather than bypassing it.
try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

try:
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None

# --------------------------------------------------------------------- cache


def load_cache(path, version):
    """Read the JSON cache. A missing, unreadable, corrupt, or
    schema-mismatched cache is not an error: it degrades to a full fetch."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != version:
        return {}
    return data


class CacheShapeError(ValueError):
    """Raised when a populated impact-cache map no longer has its schema."""


def _validate_count(name, repo, field, value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CacheShapeError(
            f"impact cache {name}[{repo!r}].{field!r} must be a "
            "non-negative integer")


def _validate_impact_map(cache, name, required_fields):
    """Validate fields only for repositories represented in a cache map.

    A missing or empty map is a legitimate no-contribution result. Once the
    producer has written an entry, however, a missing field is structural
    drift and must not be converted to a ranking zero.
    """
    if name not in cache:
        return
    entries = cache[name]
    if not isinstance(entries, dict):
        raise CacheShapeError(
            f"impact cache field {name!r} must be a map")
    for repo, entry in entries.items():
        if not isinstance(entry, dict):
            raise CacheShapeError(
                f"impact cache {name}[{repo!r}] must be a map")
        for field in required_fields:
            if field not in entry:
                raise CacheShapeError(
                    f"impact cache {name}[{repo!r}] missing field {field!r}")
            _validate_count(name, repo, field, entry[field])


def validate_cache_shape(cache):
    """Reject structural drift in populated ranking inputs.

    Repositories absent from a map remain valid: an empty map means there is
    no contribution in that metric. ``ourloc`` entries without ``ours`` are
    failed-blame records and retain the existing filtering behaviour; entries
    that do contain ``ours`` must also contain ``total``.
    """
    _validate_impact_map(cache, "totals", ("merged_prs", "issues"))
    if "ourloc" not in cache:
        return
    entries = cache["ourloc"]
    if not isinstance(entries, dict):
        raise CacheShapeError("impact cache field 'ourloc' must be a map")
    for repo, entry in entries.items():
        if not isinstance(entry, dict):
            raise CacheShapeError(
                f"impact cache ourloc[{repo!r}] must be a map")
        if "ours" in entry and "total" not in entry:
            raise CacheShapeError(
                f"impact cache ourloc[{repo!r}] missing field 'total'")
        if "ours" in entry:
            _validate_count("ourloc", repo, "ours", entry["ours"])
            _validate_count("ourloc", repo, "total", entry["total"])


CACHE_LOCK_TIMEOUT = 60.0


def _open_lock_file(path):
    """Open (creating) the lock file beside `path`; None if it cannot be
    opened. An unwritable cache directory is already reported by the write
    itself — it must not turn into a second failure mode here."""
    lock = Path(str(path) + ".lock")
    try:
        lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        return os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as e:
        print(f"warning: could not open cache lock {lock}: {e}",
              file=sys.stderr)
        return None


def _lock_once(fd):
    """One non-blocking attempt at an exclusive lock, via the platform's own
    API: flock(2) on POSIX, a one-byte msvcrt.locking region on Windows (a
    byte-range lock needs no existing byte). Returns whether it is held.

    With neither API available the lock is reported as never held, so callers
    see an unavailable lock and fail rather than bypassing it.
    """
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif msvcrt is not None:
            # msvcrt is real on Windows; the POSIX type stubs omit it.
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # pyright: ignore[reportAttributeAccessIssue]
        else:
            return False
        return True
    except OSError:
        return False


def _lock_until(fd, timeout):
    """Take an exclusive lock on `fd`, polling until `timeout` elapses.

    Polled rather than blocking: a blocking lock has no timeout, and a render
    must never park forever behind another writer. Returns whether it is held.
    """
    deadline = time.monotonic() + timeout
    while True:
        if _lock_once(fd):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)


@contextlib.contextmanager
def cache_lock(path, timeout=CACHE_LOCK_TIMEOUT):
    """Serialise the writers of one cache file against each other.

    Two scripts write the impact cache: render-impact.py replaces it whole
    twice a day, render-responsiveness.py reads-modifies-writes its PR half
    every hour. Without a lock spanning that read-modify-write, a whole-file
    save landing between its read and its write is discarded — including
    `ourloc`, which is expensive to rebuild.

    Yields True when the lock is held, False when it is not (timeout, or a
    lock file that could not be created). False is a failure for every caller:
    the cache writers raise rather than bypassing the lock.
    """
    fd = _open_lock_file(path)
    if fd is None:
        yield False
        return
    try:
        yield _lock_until(fd, timeout)
    finally:
        os.close(fd)  # closing the fd releases the flock


def _write_cache(path, payload):
    """Atomic write: temp file beside the target, then os.replace onto it.

    os.replace within one filesystem is atomic, so no reader ever observes a
    half-written cache. The temp file is removed if the write fails, so a
    failed save leaves neither a partial cache nor litter behind. Callers hold
    cache_lock to serialize competing cache updates. The unique mkstemp name
    needs no lock for temp-file safety.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        try:
            stream = os.fdopen(fd, "w", encoding="utf-8")
        except Exception:
            os.close(fd)
            raise
        with stream:
            stream.write(json.dumps(payload))
        os.replace(tmp, path)
        # One-time migration cleanup of the pre-mkstemp temp name.
        try:
            path.with_name(path.name + ".tmp").unlink(missing_ok=True)
        except OSError:
            pass
    finally:
        tmp.unlink(missing_ok=True)  # a no-op once os.replace has moved it


def save_cache(path, payload, timeout=CACHE_LOCK_TIMEOUT):
    """Replace the whole cache atomically under the writers' lock.

    Raises TimeoutError when the lock is unavailable and propagates write
    failures, so a cache is never modified without the lock or silently left
    stale after a failed save.
    """
    with cache_lock(path, timeout) as locked:
        if not locked:
            raise TimeoutError(f"cache lock unavailable for {path}")
        _write_cache(path, payload)


def merge_cache(path, version, updates, timeout=CACHE_LOCK_TIMEOUT):
    """Replace `updates`' keys in the cache at `path`, preserving every other
    key, atomically and under the lock. Returns the payload written.

    For a writer that owns only PART of a shared cache. The load and the write
    happen inside one lock hold, so the whole-file writer cannot slip in
    between and have its expensive sections (`ourloc`) silently reverted to
    what this writer happened to read.

    Raises TimeoutError when the lock is unavailable and propagates write
    failures, so a failed update cannot silently leave the cache stale.
    """
    with cache_lock(path, timeout) as locked:
        if not locked:
            raise TimeoutError(f"cache lock unavailable for {path}")
        payload = {**load_cache(path, version), **updates,
                   "version": version}
        _write_cache(path, payload)
        return payload
