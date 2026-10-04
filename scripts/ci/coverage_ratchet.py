#!/usr/bin/env python3
"""The coverage ratchet: a committed floor raised by pull request, never lowered.

The gate used to be a hand-edited `--fail-under=81` literal with a comment
telling the next human to raise it by hand, which is a ratchet nobody ratchets:
it rots until it is either removed or quietly ignored. Here the floor is
committed DATA (coverage-floor.json), it only ever moves up, and CI measures
and announces when it can climb. A deliberate pull request moves the floor.

TWO SUBCOMMANDS, ONE MEASUREMENT. Both take the file `python -m coverage json`
writes, so neither this script nor its tests ever shells out to coverage:

    coverage_ratchet.py gate  --coverage-json coverage.json
    coverage_ratchet.py raise --coverage-json coverage.json --commit "$GITHUB_SHA"

`gate` is what tests.yml runs on every push and pull request: it first requires
the measured file count to match the committed record. With matching counts,
measured >= floor is a pass; anything less exits 1 naming the two numbers.

`raise` produces a candidate floor file for a pull request: floor := measured,
but ONLY when measured is strictly above the floor already committed. The main
workflow runs it against a copy of the committed file and uploads the candidate
as `coverage-floor-candidate`; a person reviews and commits the floor change.
There is no branch anywhere in this file that writes a smaller number than it
read, so the one direction a ratchet must never move cannot be reached by
adding a flag.

Both subcommands refuse outright when the measurement covers a different
NUMBER OF FILES than the committed record does. A percentage is a ratio, so adding one
shipped module moves it while every statement count stays comparable and
nothing else announces the change; a ratchet that compares only to itself
cannot see that. That refusal is exit 2, not exit 0-with-no-write: a moved
population is something a human has to record, not something to shrug at.

WHY `basis` IS A REQUIRED KEY. This file can prove that the numbers agree with
each other. It cannot prove that a movement was justified — the reason a floor
moved has to be written where the number is, in repo-visible terms, because the
pull request that explains it is not there when someone reads this file two
months later. So a committed file with no basis is an error, not a default.

Exit codes: 0 in bounds (or nothing to raise), 1 gate below the floor with
matching file counts, 2 invalid or unreadable input, or a file-count mismatch.
2 is separate from 1: input errors and incomparable populations are refused
before the gate compares percentages.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, TypeGuard

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_FLOOR_FILE = REPO_ROOT / "coverage-floor.json"
SUPPORTED_SCHEMA = 1

# The keys a human owns. `raise` copies the committed document and overwrites
# only the numbers and the provenance, so these four survive a raise
# byte-for-byte — a ratchet run must never be able to edit its own rationale.
AUTHORED_KEYS = ("schema", "basis", "population", "cell")
# The keys this script owns: the numbers, and where the measurement came from.
# `files` is here rather than left out because a percentage is a ratio, and a
# ratio cannot announce that its own denominator changed.
SCRIPT_KEYS = ("measured", "floor", "statements", "missing", "files",
               "measured_commit", "measured_at")


class RatchetError(RuntimeError):
    """The measurement or the committed file could not be read as a ratchet."""


class Measurement(NamedTuple):
    """What one coverage report measured, as the gate will see it."""
    percent: float
    statements: int
    missing: int
    files: int


def round_down(value: float) -> float:
    """One decimal, rounded DOWN.

    Rounding to nearest would let a stored floor of 71.94 be written as 71.9
    and then sit above a true measurement of 71.86 — a ratchet that ratchets
    itself down. Rounding down can only ever move the number away from a
    failure, never into one.
    """
    return math.floor(value * 10) / 10


def _is_number(value) -> TypeGuard[float]:
    """A JSON number, excluding booleans — `True` is an int in Python.

    A TypeGuard rather than a plain bool so that a check is also narrowing:
    the refusals below read the value in the message AND convert it after,
    and the gate is not worth a cast at every one of those.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_measurement(path: Path) -> Measurement:
    """The total out of one `coverage json` report.

    The input's file count is printed BEFORE anything is decided, because an
    instrument handed nothing reports 0% and exits 0 — the same shape a clean
    pass has. "The suite ran and measured eleven files" and "this report
    contains no files at all" both yield a number; only one of them is a
    measurement, and the reader cannot tell them apart without the count.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            report = json.load(handle)
    except FileNotFoundError as exc:
        raise RatchetError(
            f"no coverage report at {path} — nothing was measured") from exc
    except (OSError, ValueError) as exc:
        raise RatchetError(f"{path} is not readable coverage JSON: {exc}") from exc
    if not isinstance(report, dict):
        raise RatchetError(f"{path} is not a coverage JSON object")

    raw_files = report.get("files")
    files: dict = raw_files if isinstance(raw_files, dict) else {}
    raw_totals = report.get("totals")
    totals: dict = raw_totals if isinstance(raw_totals, dict) else {}
    statements = totals.get("num_statements")
    percent = totals.get("percent_covered")
    missing = totals.get("missing_lines")
    print(f"coverage report: {len(files)} file(s), "
          f"{statements if _is_number(statements) else 'unknown'} "
          "statement(s) measured")

    if not files:
        raise RatchetError(
            f"{path} measured no files — an empty report is not a measurement, "
            "and 0% of nothing would otherwise read as a total collapse")
    if not _is_number(statements):
        raise RatchetError(
            f"{path} reports {statements!r} statements over "
            f"{len(files)} file(s) — a report with nothing in it is not a "
            "measurement")
    if statements <= 0:
        raise RatchetError(
            f"{path} measured {statements} statements over {len(files)} "
            "file(s) — a report with nothing in it is not a measurement")
    if not _is_number(percent):
        raise RatchetError(f"{path} reports percent_covered={percent!r}")
    if not _is_number(missing):
        missing = 0
    return Measurement(round_down(float(percent)), int(statements),
                       int(missing), len(files))


def read_committed(path: Path) -> dict:
    """The committed floor document, validated before it is used for anything.

    Every declared key is checked, and a wrong one raises rather than taking a
    permissive default. A floor that silently defaults to 0 when its own file
    is malformed is a gate that reports green precisely when its configuration
    is broken.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError as exc:
        raise RatchetError(f"no committed floor at {path}") from exc
    except (OSError, ValueError) as exc:
        raise RatchetError(f"{path} is not readable JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise RatchetError(f"{path} is not a JSON object")

    # Named together first, so a renamed or dropped key is reported as the
    # key it is rather than as whatever the per-key check below made of it.
    absent = [key for key in AUTHORED_KEYS + SCRIPT_KEYS
              if key not in document]
    if absent:
        raise RatchetError(f"{path} is missing required key(s): "
                           + ", ".join(absent))
    if document.get("schema") != SUPPORTED_SCHEMA:
        raise RatchetError(
            f"{path} declares schema={document.get('schema')!r}; this script "
            f"reads schema {SUPPORTED_SCHEMA}")
    basis = document.get("basis")
    if not isinstance(basis, str) or not basis.strip():
        raise RatchetError(
            f"{path} has no basis — the reason a floor sits where it does "
            "belongs at the numbers, not in the pull request that moved them")
    if not _is_number(document.get("floor")):
        raise RatchetError(
            f"{path} floor={document.get('floor')!r} is not a number")
    population = document.get("population")
    if (not isinstance(population, list) or not population
            or not all(isinstance(item, str) and item for item in population)):
        raise RatchetError(
            f"{path} population={population!r} is not a list of strings")
    for key in ("cell", "measured_commit", "measured_at"):
        value = document.get(key)
        if not isinstance(value, str) or not value.strip():
            raise RatchetError(f"{path} {key}={value!r} is not a non-empty "
                               "string")
    _check_counts(path, document)
    return document


def _check_counts(path: Path, document: dict) -> None:
    """The three counts the script writes are numbers, and `files` is positive.

    Its own function because a floor measured over zero files is a different
    failure from a floor whose `missing` is a string, and folding the pair
    into one `if` would have made the message say whichever thing it checked
    first.
    """
    for key in ("statements", "missing", "files"):
        if not _is_number(document.get(key)):
            raise RatchetError(f"{path} {key}={document.get(key)!r} is not a "
                               "number")
    if document["files"] <= 0:
        raise RatchetError(
            f"{path} files={document['files']!r} — a floor measured over no "
            "files is not a measurement")


def _atomic_write(path: Path, text: str) -> None:
    """Replace `path` with `text`, through a fresh temp inode in its directory.

    Never open(path, 'w'): a crash between the truncate and the write leaves a
    gate file that is not JSON, and the next run reads that as a hard error
    rather than as the number it was. mkstemp creates a NEW inode — one an
    earlier crashed run cannot leave behind carrying stale content — and
    os.replace makes the swap atomic within the directory.

    mkstemp creates its inode at 0600 unconditionally — the umask is not a
    factor in it. Left there, a raise would silently publish this git-tracked
    file at 0600, and git does not record the non-executable bit, so the mode
    would not round-trip: the working copy would stay 0600 while a fresh
    checkout elsewhere came back at 0644. The mode is therefore set back to
    what git will hand the next reader.
    """
    directory = path.parent
    handle_fd, temp_name = tempfile.mkstemp(
        dir=directory, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            # Ownership of the descriptor has moved to the stream; leaving the
            # name bound would close it twice if the write below raised.
            handle_fd = None
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o644)
        os.replace(temp_name, path)
    finally:
        if handle_fd is not None:
            os.close(handle_fd)
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _check_population(measurement: Measurement, document: dict,
                      floor_path: Path) -> None:
    """Require comparable file counts before gating or raising the floor."""
    # The population is part of the measurement, not an incidental detail of
    # it. Adding or removing a shipped file moves the percentage while nothing
    # else changes, so comparing the new ratio against a floor the old
    # denominator set is comparing two different programs. Both subcommands
    # refuse that comparison until a human records the move.
    if measurement.files != document["files"]:
        raise RatchetError(
            f"{floor_path} was measured over {document['files']} file(s) and "
            f"this run measured {measurement.files}: the population moved, so "
            "this floor is not comparable until `files` here is re-measured "
            "and committed deliberately")


def gate(measurement: Measurement, floor_path: Path) -> int:
    """Compare matching populations against the floor. 0 in bounds."""
    document = read_committed(floor_path)
    _check_population(measurement, document, floor_path)
    floor = float(document["floor"])
    if measurement.percent >= floor:
        print(f"coverage gate: measured {measurement.percent} against a floor "
              f"of {floor} — at or above it")
        return 0
    print(f"FAIL: measured {measurement.percent} against a floor of {floor}",
          file=sys.stderr)
    return 1


def raise_floor(measurement: Measurement, floor_path: Path,
                commit: str | None = None) -> int:
    """Move the floor up over a matching population, never down. 0 either way.

    With comparable file counts, `measured <= floor` exits without writing.
    Every other key in the document is carried over from what was committed,
    so a raise cannot rewrite the population it measured, the cell it was
    measured on, or the reason the floor exists.
    """
    document = read_committed(floor_path)
    _check_population(measurement, document, floor_path)
    floor = float(document["floor"])
    if measurement.percent <= floor:
        print(f"floor stays at {floor}: measured {measurement.percent} is not "
              "above it, and a ratchet only moves in one direction")
        return 0

    raised = dict(document)
    raised["floor"] = measurement.percent
    raised["measured"] = measurement.percent
    raised["statements"] = measurement.statements
    raised["missing"] = measurement.missing
    raised["files"] = measurement.files
    raised["measured_commit"] = (commit or os.environ.get("GITHUB_SHA")
                                 or "unknown")
    raised["measured_at"] = _utc_now()
    # ensure_ascii=False because the human-authored keys are prose with real
    # punctuation in them; the default would rewrite every em-dash to a —
    # escape on the first raise and make the diff unreadable.
    _atomic_write(floor_path,
                  json.dumps(raised, indent=2, ensure_ascii=False) + "\n")
    print(f"raised the floor {floor} -> {measurement.percent} "
          f"({measurement.statements - measurement.missing}/"
          f"{measurement.statements} statements over "
          f"{measurement.files} file(s), commit {raised['measured_commit']})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("gate", "fail if coverage is below the floor"),
                            ("raise", "move the floor up to the measurement")):
        sub = subcommands.add_parser(name, help=help_text)
        sub.add_argument("--coverage-json", required=True, metavar="PATH",
                         help="the file `coverage json -o` writes")
        sub.add_argument("--floor-file", default=str(DEFAULT_FLOOR_FILE),
                         metavar="PATH",
                         help=f"the committed floor document "
                              f"(default: {DEFAULT_FLOOR_FILE})")
        if name == "raise":
            sub.add_argument("--commit", default=None, metavar="SHA",
                             help="the commit this measurement came from; "
                                  "recorded as measured_commit on a raise "
                                  "(default: $GITHUB_SHA)")
    args = parser.parse_args(argv)

    try:
        measurement = read_measurement(Path(args.coverage_json))
        if args.command == "gate":
            return gate(measurement, Path(args.floor_file))
        return raise_floor(measurement, Path(args.floor_file), args.commit)
    except RatchetError as exc:
        print(f"coverage ratchet: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
