"""Load scripts/ci/counter.py by path, once, for the test suite.

    python3 -m unittest discover -v

`scripts/ci` is not a package and deliberately has no `__init__.py`, so
every module here loads a sibling by path. This is the one place that
happens for the instrument itself, so that `bench_platform.py` and the
modules that guard on it do not each grow their own loader.
"""
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent


def load_counter():
    """Import scripts/ci/counter.py, once, and return it."""
    cached = sys.modules.get("ghw_counter_shared")
    if cached is not None:
        return cached
    path = REPO_ROOT / "scripts" / "ci" / "counter.py"
    spec = importlib.util.spec_from_file_location("ghw_counter_shared", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["ghw_counter_shared"] = module
    spec.loader.exec_module(module)
    return module
