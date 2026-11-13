#!/usr/bin/env python3
"""Print the speed job's unit-suite population digest; --write records it.

`--print-population` exits 0 on a failed import, listing
`unittest.loader._FailedTest.<module>`; any such id is refused (exit 2).
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
    path = HERE / "compare_counters.py"
    spec = importlib.util.spec_from_file_location("ghw_compare_digest", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"error: cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


compare = _load_comparator()


class DigestError(RuntimeError):
    pass


def collect(checkout: Path) -> list:
    # A child, as in the speed job: this process must not import `tests`.
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
    parser = argparse.ArgumentParser(description="Print the unit-suite population digest.")
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
