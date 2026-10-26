"""Owner-locked temp directories for tests, plus the run-start scavenger.

Every ``ghw-*`` directory a test creates through this helper holds an
adjacent ``<dir>.owner.lock`` file whose flock the owning run keeps for the
directory's lifetime, reusing src/impact_clone.py's scratch owner-lock
primitives. Importing the ``tests`` package — the start of every unittest
run, whole-suite or single-module — calls ``scavenge()`` once: it removes
``ghw-*`` directories in the system temp dir whose owner lock it can take,
which the kernel releases when the owning run dies, SIGKILL included, and
leaves alone the directories a live run still holds and the ``ghw-*``
directories no test run created (they carry no lock file).

    python3 -m unittest discover
"""
import atexit
import tempfile
from pathlib import Path

import impact_clone

_PREFIX = "ghw-"

# Lock streams this process holds, keyed by the directory each one owns.
# A scavenge skips these outright: between a directory's creation and the
# flock inside _register the lock file is absent or unheld, and no observer
# can tell that window from a dead owner.
_OWNED = {}


class _LockedTemporaryDirectory(tempfile.TemporaryDirectory):
    """A TemporaryDirectory whose owner lock is dropped at cleanup."""

    def cleanup(self):
        try:
            super().cleanup()
        finally:
            _discard(Path(self.name))


def _register(path):
    """Hold the adjacent owner lock for a freshly created directory."""
    _OWNED[Path(path)] = impact_clone._acquire_scratch_owner_lock(
        path, create=True)  # pylint: disable=protected-access


def _discard(path):
    """Release one lock this process holds and unlink its lock file.

    Unlinking is tied to holding: removing a foreign lock file while its
    owner still holds the flock would let a third run create a fresh lock
    file and believe it owns the same directory.
    """
    stream = _OWNED.pop(Path(path), None)
    if stream is None:
        return
    impact_clone._release_scratch_owner_lock(  # pylint: disable=protected-access
        stream)
    try:
        impact_clone._scratch_owner_lock_path(path).unlink()  # pylint: disable=protected-access
    except FileNotFoundError:
        pass


def tempdir(prefix=None, dir=None):
    """Create a TemporaryDirectory; lock it when its basename starts ghw-."""
    tmp = _LockedTemporaryDirectory(prefix=prefix, dir=dir)
    if Path(tmp.name).name.startswith(_PREFIX):
        # shortcut: a fresh directory is unheld between its creation and the
        # flock inside _register, and a sibling scavenge in that window can
        # reclaim it; the window is one scheduler slice wide — upgrade when
        # a test observes it, the tolerance src/impact_clone.py also accepts.
        _register(tmp.name)
    return tmp


def mkdtemp(prefix=None, dir=None):
    """Create a directory via tempfile.mkdtemp; lock it when ghw-*."""
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=dir))
    if path.name.startswith(_PREFIX):
        _register(path)
    return path


def scavenge():
    """Remove ghw-* temp dirs whose owning test run is no longer alive.

    The lock is the only liveness oracle: no PID or age check can prove an
    owner is dead, and a live sibling run's directory is spared exactly
    while its lock is held (issue 273; the scratch-age incident).
    """
    active = set(_OWNED)
    for scratch in Path(tempfile.gettempdir()).glob(f"{_PREFIX}*"):
        if scratch in active or scratch.is_symlink() or not scratch.is_dir():
            continue
        try:
            owner_lock = impact_clone._acquire_scratch_owner_lock(  # pylint: disable=protected-access
                scratch, create=False)
        except BlockingIOError:
            continue  # the lock is held: the owning run is still alive
        except OSError:
            continue  # an unreadable lock file spares the directory
        if owner_lock is None:
            continue  # no lock file: no test run created this directory
        impact_clone._release_scratch_owner_lock(  # pylint: disable=protected-access
            owner_lock)
        impact_clone.remove_scratch_dir(scratch)


def _release_owned():
    """Release every still-held lock and unlink its file at interpreter exit.

    A normal exit leaves no orphan lock files; a SIGKILL leaves directory
    and lock file behind for the next run's scavenge().
    """
    for path in list(_OWNED):
        _discard(path)


atexit.register(_release_owned)
