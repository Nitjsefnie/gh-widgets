"""Owner-locked ghw-* dirs, run-start scavenged."""
# pylint: disable=protected-access
import atexit
import tempfile
from pathlib import Path

import impact_clone

_PREFIX = "ghw-"

_OWNED = {}


class _LockedTemporaryDirectory(tempfile.TemporaryDirectory):

    def cleanup(self):
        try:
            super().cleanup()
        finally:
            _discard(Path(self.name))


def _register(path):
    _OWNED[Path(path)] = impact_clone._acquire_scratch_owner_lock(
        path, create=True)


def _discard(path):
    stream = _OWNED.pop(Path(path), None)
    if stream is None:
        return
    impact_clone._release_scratch_owner_lock(
        stream)
    try:
        impact_clone._scratch_owner_lock_path(path).unlink()
    except FileNotFoundError:
        pass


def tempdir(prefix=None, directory=None):
    tmp = _LockedTemporaryDirectory(prefix=prefix, dir=directory)
    if Path(tmp.name).name.startswith(_PREFIX):
        # shortcut: unheld until the flock below; a sibling scavenge can win it (upgrade: seen)
        _register(tmp.name)
    return tmp


def mkdtemp(prefix=None, directory=None):
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=directory))
    if path.name.startswith(_PREFIX):
        # shortcut: same window as tempdir().
        _register(path)
    return path


def scavenge():
    active = set(_OWNED)
    for scratch in Path(tempfile.gettempdir()).glob(f"{_PREFIX}*"):
        if scratch in active or scratch.is_symlink() or not scratch.is_dir():
            continue
        try:
            owner_lock = impact_clone._acquire_scratch_owner_lock(
                scratch, create=False)
        except BlockingIOError:
            continue
        except OSError:
            continue
        if owner_lock is None:
            continue
        impact_clone._release_scratch_owner_lock(
            owner_lock)
        try:
            impact_clone.remove_scratch_dir(scratch)
        except OSError:
            # a stuck dir must not red every run (cross-user filtered above)
            pass


def _release_owned():
    for path in list(_OWNED):
        _discard(path)


atexit.register(_release_owned)
