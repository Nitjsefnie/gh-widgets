#!/usr/bin/env python3
"""Print, or rewrite, the unit-suite population digest of a checkout.

`gate/speed-baseline.json` pins `populations.unit-suite.population`: the
digest the speed job takes over `compare_counters.py --print-population
tests`. A change that adds or removes a unit test moves it in the same change,
and this command re-derives it through that same collection and digest.

`--print-population` exits 0 when a test module fails to import, listing
`unittest.loader._FailedTest.<module>` in place of its tests. A digest over
that listing describes a suite nobody runs, so any such id is a refusal
(exit 2) that names the modules and writes nothing.

    population_digest.py [--write] [CHECKOUT]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASELINE = Path("gate/speed-baseline.json")
POPULATION = "unit-suite"
FAILED_IMPORT = "unittest.loader._FailedTest."


def _load_comparator():
    """Import compare_counters.py by path: scripts/ci is not a package."""
    path = HERE / "compare_counters.py"
    spec = importlib.util.spec_from_file_location("ghw_compare_digest", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"error: cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


compare = _load_comparator()


class DigestError(RuntimeError):
    """No digest this command is willing to print or write."""


def collect(checkout: Path) -> list:
    # A child process, as the speed job runs it: discovery imports every
    # test module, and this process must not keep the checkout's `tests`.
    result = subprocess.run(
        [sys.executable, str(HERE / "compare_counters.py"),
         "--print-population", str(checkout / "tests")],
        cwd=checkout, capture_output=True, text=True, check=False)
    if result.returncode:
        raise DigestError(result.stderr.strip()
                          or f"collection exited {result.returncode}")
    node_ids = [line.strip() for line in result.stdout.splitlines()
                if line.strip()]
    failed = sorted(node[len(FAILED_IMPORT):] for node in node_ids
                    if node.startswith(FAILED_IMPORT))
    if failed:
        raise DigestError(
            "these test modules failed to import, so the collected "
            "population is not the suite: " + ", ".join(failed)
            + ". Run `python3 -m unittest <module>` to see why.")
    return node_ids


def rewrite(path: Path, digest: str) -> str:
    """Replace the unit-suite digest in place; return the old one."""
    old = compare.read_population(compare.read_baseline(path),
                                  POPULATION)["population"]
    raw = path.read_bytes()
    needle = json.dumps(old).encode("utf-8")
    if raw.count(needle) != 1:
        raise DigestError(
            f"{path} holds {needle.decode()} {raw.count(needle)} times; "
            "only a single occurrence can be rewritten alone")
    candidate = raw.replace(needle, json.dumps(digest).encode("utf-8"))
    expected = json.loads(raw)
    expected["populations"][POPULATION]["population"] = digest
    if json.loads(candidate) != expected:
        raise DigestError(f"{path}: the rewrite would change another value")
    if candidate == raw:
        return old
    handle_fd, temp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle_fd, "wb") as handle:
            handle.write(candidate)
        compare.read_baseline(Path(temp_name))
        os.chmod(temp_name, stat.S_IMODE(os.stat(path).st_mode))
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return old


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("checkout", nargs="?", type=Path,
                        default=HERE.parent.parent,
                        help="the checkout to collect (default: this one)")
    parser.add_argument("--write", action="store_true",
                        help=f"rewrite populations.{POPULATION}.population "
                             f"in {BASELINE} and no other byte")
    args = parser.parse_args(argv)
    checkout = args.checkout.resolve()
    try:
        digest = compare.counter.population_digest(collect(checkout))
        if args.write:
            old = rewrite(checkout / BASELINE, digest)
            print(f"{BASELINE}: {old} -> {digest}", file=sys.stderr)
    except (DigestError, compare.ComparisonError) as exc:
        print(f"population_digest: {exc}", file=sys.stderr)
        return 2
    print(digest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
