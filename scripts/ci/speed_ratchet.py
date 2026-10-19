#!/usr/bin/env python3
"""Tighten recorded speed maxima from the values the comparison judged.

This operation only lowers the maxima in ``entries`` and ``wall``. It
reuses ``compare_counters``' report reader, round folding and population
digest check, so the new maxima are the same head values the speed gate
compared. Every other recorded field is carried unchanged. A candidate is
validated by ``baseline.read_baseline`` before it replaces the source or is
written to the requested output.

There is deliberately no hysteresis. CPU time is noisy, and any positive
hysteresis below the pool's observed spread either dead-letters the tighten
or selects a noise-sensitive threshold. Reports serialize six decimal
places, so strict improvements move each recorded maximum down a finite
grid: after a bounded number of pushes it reaches a fixed point, which
future runs rarely beat because the ceiling has reached the low tail already
observed. The gate stays green throughout because its ceiling is
``max * (1 + tolerance)`` and every tighten sets ``max`` to a passing head
value.

If a head value falls below the recorded ``min``, lowering ``max`` alone
would make an invalid envelope. Since ``min`` is carried unchanged, the
candidate fails baseline validation and nothing is written.

The authored explanation and measurement identity are not re-stamped by this
tool. Provenance for an automated move is the ratchet commit message, which
names the triggering run id.

    speed_ratchet.py --baseline gate/speed-baseline.json \\
      --unit-report reports/unit-1.xml --unit-report reports/unit-2.xml \\
      --unit-population-file reports/unit-population.txt \\
      --renderer-report reports/bench-head-1.xml \\
      --renderer-report reports/bench-head-2.xml \\
      --renderer-population-file reports/renderer-population.txt
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASELINE = REPO_ROOT / "gate/speed-baseline.json"


def _load_comparator():
    """Import the comparator by path; scripts/ci is deliberately not a package."""
    path = Path(__file__).resolve().with_name("compare_counters.py")
    spec = importlib.util.spec_from_file_location("ghw_speed_ratchet_compare",
                                                  path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


compare = _load_comparator()
baseline = compare.baseline


class RatchetError(ValueError):
    """The candidate could not be compared or kept as a valid baseline."""


def _check_population(population: dict, path: Path, name: str) -> None:
    """Use the comparator's digest contract before accepting any measurement."""
    compare.verify_unit_population(population["population"], path, name)


def _read_head(reports: list[Path], population: dict, name: str) -> tuple:
    """Fold passing reports exactly as the comparator does for this population."""
    if not reports:
        raise RatchetError(f"no {name.replace('-', ' ')} reports were given")
    try:
        scans = [compare.scan_junit(path) for path in reports]
        head = compare.fold_scans(scans)
        metric = compare._resolve_metric(reports, name)
        compare._check_metric(population["metric"], metric)
        wall = compare._fold_wall(reports)
    except (compare.ComparisonError, OSError, ValueError) as exc:
        raise RatchetError(f"cannot read {name.replace('-', ' ')} reports: "
                           f"{exc}") from exc
    for node, value in head.items():
        if not math.isfinite(value) or value < 0:
            raise RatchetError(
                f"{name} report value for {node} is not a finite "
                "non-negative number")
    for node, value in wall.items():
        if not math.isfinite(value) or value < 0:
            raise RatchetError(
                f"{name} wall report value for {node} is not a finite "
                "non-negative number")
    return head, wall


def _check_recorded_entries(head: dict, recorded: dict, name: str) -> None:
    """A report entry outside the record is not a candidate threshold."""
    unknown = sorted(set(head) - set(recorded))
    if unknown:
        raise RatchetError(
            f"{name} reports entry/entries not recorded in the baseline: "
            + ", ".join(unknown))


def tighten(document: dict, measurements: dict) -> tuple[dict, list[str]]:
    """Return a valid down-only candidate and the maxima it tightened.

    ``measurements`` maps population names to ``(head entries, head wall)``.
    Missing entries remain unchanged, exactly as the comparator's shared-set
    comparison does. A report-only entry is refused instead of being
    intersected away.
    """
    candidate = json.loads(json.dumps(document))
    changes = []
    for name, (head_entries, head_wall) in measurements.items():
        recorded_population = baseline.read_population(document, name)
        candidate_population = candidate["populations"][name]
        for map_name, head_values in (("entries", head_entries),
                                      ("wall", head_wall)):
            recorded = recorded_population[map_name]
            _check_recorded_entries(head_values, recorded, name)
            for node, head_value in head_values.items():
                envelope = recorded.get(node)
                if envelope is None or head_value >= envelope["max"]:
                    continue
                candidate_envelope = candidate_population[map_name].get(node)
                if candidate_envelope is None:
                    # This is the comparator's shared-set case: no recorded
                    # wall envelope means there is no wall threshold to tighten.
                    continue
                candidate_envelope["max"] = head_value
                changes.append(
                    f"{name}:{map_name}:{node} "
                    f"{envelope['max']} -> {head_value}")
    return candidate, changes


def _write_validated(path: Path, candidate: dict) -> None:
    """Validate a temporary JSON file before atomically replacing the target."""
    text = json.dumps(candidate, indent=2, ensure_ascii=False) + "\n"
    directory = path.parent
    handle_fd, temp_name = tempfile.mkstemp(
        dir=directory, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle_fd = None
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        baseline.read_baseline(Path(temp_name))
        os.chmod(temp_name, 0o644)
        os.replace(temp_name, path)
    finally:
        if handle_fd is not None:
            os.close(handle_fd)
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def update(path: Path, output: Path | None, unit_reports: list[Path],
           unit_population: Path, renderer_reports: list[Path],
           renderer_population: Path) -> list[str]:
    """Compute and optionally write one tighten; no-op leaves every file alone."""
    document = baseline.read_baseline(path)
    measurements = {}
    for name, reports, population_file in (
            ("unit-suite", unit_reports, unit_population),
            ("renderer-workloads", renderer_reports, renderer_population)):
        recorded_population = baseline.read_population(document, name)
        _check_population(recorded_population, population_file, name)
        measurements[name] = _read_head(reports, recorded_population, name)

    candidate, changes = tighten(document, measurements)
    if not changes:
        print("no speed maximum beat its recorded value: nothing to tighten")
        return []

    target = output or path
    _write_validated(target, candidate)
    for change in changes:
        print(f"tightened {change}")
    return changes


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE,
                        help=f"recorded baseline (default: {DEFAULT_BASELINE})")
    parser.add_argument("--output", type=Path,
                        help="write a tightened copy here; no file is created "
                             "when nothing tightens")
    parser.add_argument("--unit-report", type=Path, action="append",
                        required=True, metavar="XML")
    parser.add_argument("--unit-population-file", type=Path, required=True)
    parser.add_argument("--renderer-report", type=Path, action="append",
                        required=True, metavar="XML")
    parser.add_argument("--renderer-population-file", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        update(args.baseline, args.output, args.unit_report,
               args.unit_population_file, args.renderer_report,
               args.renderer_population_file)
    except (baseline.ComparisonError, OSError, RuntimeError, ValueError) as exc:
        print(f"speed ratchet: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
