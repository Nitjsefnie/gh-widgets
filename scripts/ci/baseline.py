#!/usr/bin/env python3
"""The committed counter baseline: its shape, and the envelope inside it.

Split out of compare_durations.py because it is a separate question. That
file answers "did this commit get more expensive than the baseline?"; this
one answers "what IS the baseline, and is this document one we are willing
to judge against?". Both change when the baseline changes, and they do not
change for the same reasons: the comparator gains a verdict shape and a
report, this gains a validation rule.

Imported by path rather than as a package, for the same reason counter.py
is: scripts/ci holds standalone CI entry points and deliberately has no
__init__.py.

Modelled on coverage-floor.json: committed data rather than a literal in a
workflow, a REQUIRED `basis` so the reason a number sits where it does lives
at the number, a provenance pair, and a validator that refuses rather than
degrades.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_counter():
    """Import scripts/ci/counter.py by path — scripts/ci is not a package."""
    path = Path(__file__).resolve().with_name("counter.py")
    spec = importlib.util.spec_from_file_location("ghw_counter_baseline",
                                                  path)
    if spec is None or spec.loader is None:
        raise ComparisonError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


counter = _load_counter()


SUPPORTED_SCHEMA = 3


class ComparisonError(RuntimeError):
    """The comparison could not be made at all."""


class MissingBaseline(ComparisonError):
    """The baseline file is not there. Exit 0: no data is not a regression.

    An EXPECTED outcome, not an error, and deliberately not chained to the
    FileNotFoundError underneath it. Two tracebacks ahead of every green
    first run trains readers to scroll past the red ones. Raised with
    `from None` so that even an unhandled print is one readable line.
    """


REQUIRED_KEYS = ("schema", "basis", "cell", "measured_commit", "measured_at",
                 "populations")
POPULATION_KEYS = ("metric", "tolerance", "population", "entries", "wall")
ENVELOPE_KEYS = ("min", "max", "n")
# An envelope from a single sample is not an envelope: it records what one
# machine did once, and the "maximum" is then a measurement, not a worst case.
# It is refused rather than marked, because a file that says PROVISIONAL in a
# key nobody reads is a file that reads as authoritative.
MIN_ENVELOPE_SAMPLES = 2


def _is_number(value) -> bool:
    """A JSON number, excluding booleans — `True` is an int in Python."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def read_baseline(path: Path) -> dict:
    """The committed baseline document, validated before it is used."""
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        raise MissingBaseline(
            f"no baseline at {path} — a first push, or the file was deleted") \
            from None
    except (OSError, ValueError) as exc:
        raise ComparisonError(f"{path} is not readable JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise ComparisonError(f"{path} is not a JSON object")

    absent = [key for key in REQUIRED_KEYS if key not in document]
    if absent:
        raise ComparisonError(
            f"{path} is missing required key(s): " + ", ".join(absent))
    if document["schema"] != SUPPORTED_SCHEMA:
        raise ComparisonError(
            f"{path} declares schema={document['schema']!r}; this script "
            f"reads schema {SUPPORTED_SCHEMA}")
    if not isinstance(document["basis"], str) or not document["basis"].strip():
        raise ComparisonError(
            f"{path} has no basis — the reason a counter baseline sits "
            "where it does belongs at the numbers, not in the pull request "
            "that moved them")
    for key in ("cell", "measured_commit", "measured_at"):
        value = document[key]
        if not isinstance(value, str) or not value.strip():
            raise ComparisonError(
                f"{path} {key}={value!r} is not a non-empty string")
    populations = document["populations"]
    if not isinstance(populations, dict) or not populations:
        raise ComparisonError(
            f"{path} populations={populations!r} is not a non-empty object; "
            "one metric and one tolerance for both populations is the shape "
            "this document was changed to remove")
    for name in sorted(populations):
        _check_population(path, name, populations[name])
    return document


def read_population(document: dict, name: str) -> dict:
    """One population's sub-document, named or refused. There is no
    default: the caller says which contract it is being held to.
    """
    populations = document["populations"]
    if name not in populations:
        raise ComparisonError(
            f"this baseline has no population named {name!r}; it has "
            + ", ".join(repr(key) for key in sorted(populations))
            + ". Naming the population is how the right metric and the "
              "right tolerance are chosen.")
    return populations[name]


def _check_population(path: Path, name: str, population) -> None:
    """One population's metric, tolerance, digest and entry maps."""
    where = f"{path} populations[{name!r}]"
    if not isinstance(population, dict):
        raise ComparisonError(f"{where} is not an object")
    absent = [key for key in POPULATION_KEYS if key not in population]
    if absent:
        raise ComparisonError(
            f"{where} is missing required key(s): " + ", ".join(absent))
    if population["metric"] not in counter.METRICS:
        raise ComparisonError(
            f"{where} metric={population['metric']!r} is not an instrument "
            "counter.py defines")
    tolerance = population["tolerance"]
    if not _is_number(tolerance) or tolerance <= 0:
        raise ComparisonError(
            f"{where} tolerance={tolerance!r} is not a positive fraction")
    digest = population["population"]
    if (not isinstance(digest, str) or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)):
        raise ComparisonError(
            f"{where} population={digest!r} is not a sha256 digest of that "
            "population's node ids")
    _check_entry_map(path, name, population["entries"], "entries",
                     required=True)
    _check_entry_map(path, name, population["wall"], "wall", required=False)


def _check_entry_map(path: Path, name: str, mapping, key: str,
                     required: bool) -> None:
    """Both maps hold envelopes: node id -> {min, max, n}.

    `wall` is a second envelope rather than one indicative number because of
    the same spread that made `entries` one. Wall time on this cell spreads
    52-63% min-to-max across dispatches, so a single stored wall figure
    would be an arbitrary pick among min, max and median: store the min and
    the smoke gate fires on noise, store the median and it fires on half the
    pool. Storing the maximum makes the choice explicit and makes
    SMOKE_FACTOR mean what it says — a multiple of the worst wall actually
    observed — which is the right base for a cliff detector. It is also one
    shape rather than two, so the validator has one rule and not two.
    """
    where = f"{path} populations[{name!r}]"
    if not isinstance(mapping, dict):
        raise ComparisonError(f"{where} {key}={mapping!r} is not an object")
    if required and not mapping:
        raise ComparisonError(
            f"{where} {key} is empty — a baseline with no entries in it "
            "compares nothing and reports that as a pass")
    for node, value in mapping.items():
        if not isinstance(node, str) or not node:
            raise ComparisonError(f"{where} {key} has a non-string node id")
        _check_envelope(where, node, value, key=key)


def _check_envelope(where: str, node: str, envelope, key: str = "entries") -> None:
    """{min, max, n}, with min <= max and n at least MIN_ENVELOPE_SAMPLES."""
    slot = f"{where} {key}[{node!r}]"
    if not isinstance(envelope, dict):
        raise ComparisonError(
            f"{slot}={envelope!r} is not an envelope; this schema records "
            "the observed RANGE of an entry ("
            f"{', '.join(ENVELOPE_KEYS)}), not a single value")
    absent = [name for name in ENVELOPE_KEYS if name not in envelope]
    if absent:
        raise ComparisonError(f"{slot} is missing " + ", ".join(absent))
    for bound in ("min", "max"):
        if not _is_number(envelope[bound]) or envelope[bound] < 0:
            raise ComparisonError(
                f"{slot}.{bound}={envelope[bound]!r} is not a non-negative "
                "number")
    count = envelope["n"]
    if not isinstance(count, int) or isinstance(count, bool):
        raise ComparisonError(f"{slot}.n={count!r} is not an integer")
    if envelope["min"] > envelope["max"]:
        raise ComparisonError(
            f"{slot} has min {envelope['min']} above max "
            f"{envelope['max']}, which is not a range")
    if count < MIN_ENVELOPE_SAMPLES:
        raise ComparisonError(
            f"{slot} was recorded from {count} observation(s); an envelope "
            f"needs at least {MIN_ENVELOPE_SAMPLES}, because one sample's "
            "maximum is a measurement rather than a worst case. Re-measure, "
            "do not widen the number by hand")


def envelope_maxima(entries: dict) -> dict:
    """{node id: observed maximum} — the number the head is judged against."""
    return {node: float(envelope["max"]) for node, envelope in entries.items()}


def raised_entries(old: dict, new: dict) -> dict:
    """Entries that went UP between two baseline documents.

    The comparator refuses to let a head counter exceed its baseline, but
    nothing there stops a commit from EDITING the baseline upwards in the
    same push — which is the gate switched off from the inside. So the
    workflow diffs the committed baseline against the merge base's copy and
    fails on any entry that moved up, and the tolerance lives here so the
    question is answered once.
    """
    moved = {}
    for name, population in sorted(new.get("populations", {}).items()):
        before = old.get("populations", {}).get(name, {}).get("entries", {})
        for node, envelope in population.get("entries", {}).items():
            was = before.get(node, {}).get("max")
            now = envelope.get("max")
            if was is None or now > was:
                moved[f"{name}:{node}"] = (was, now)
    return moved
