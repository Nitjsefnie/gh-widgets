#!/usr/bin/env python3
"""The committed counter baseline: its shape, and the envelope inside it.

Split out of compare_counters.py because it is a separate question. That
file answers "did this commit get more expensive than the baseline?"; this
one answers "what IS the baseline, and is this document one we are willing
to judge against?". Both change when the baseline changes, and they do not
change for the same reasons: the comparator gains a verdict shape and a
report, this gains a validation rule.

Imported by path rather than as a package, for the same reason counter.py
is: scripts/ci holds standalone CI entry points and deliberately has no
__init__.py.

Modelled on gate/coverage-floor.json: committed data rather than a literal in a
workflow, a REQUIRED `basis` so the reason a number sits where it does lives
at the number, a provenance pair, and a validator that refuses rather than
degrades.
"""
from __future__ import annotations

import importlib.util
import json
import math
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


SUPPORTED_SCHEMA = (3, 4, 5)


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
POPULATION_KEYS = ("metric", "tolerance", "population", "entries", "wall",
                   "slopes", "seeds")
ENVELOPE_KEYS = ("min", "max", "n")
# An envelope from a single sample is not an envelope: it records what one
# machine did once, and the "maximum" is then a measurement, not a worst case.
# It is refused rather than marked, because a file that says PROVISIONAL in a
# key nobody reads is a file that reads as authoritative.
MIN_ENVELOPE_SAMPLES = 2


def _is_number(value) -> bool:
    """A JSON number, excluding booleans — `True` is an int in Python."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_schema_keys(path: Path, name: str, population, schema: int) -> None:
    """The keys one schema's population may carry, and the eras it refuses.

    Schema 3 never carried slopes; schema 5 retires them for `seeds`. A
    document carrying an era's keys it does not declare is refused rather
    than read loosely — a key this file does not define is the drift the
    schema bump exists to catch.
    """
    if not isinstance(population, dict):
        return
    if schema == 3 and "slopes" in population:
        raise ComparisonError(
            f"{path} populations[{name!r}] slopes requires schema 4")
    if schema == 5 and "slopes" in population:
        raise ComparisonError(
            f"{path} populations[{name!r}] slopes is retired by schema 5 "
            "(issue #269): the statement instrument records no slopes; "
            "declare the instrument reset's removals through `seeds`")
    if schema != 5 and "seeds" in population:
        raise ComparisonError(
            f"{path} populations[{name!r}] seeds requires schema 5")


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
    if document["schema"] not in SUPPORTED_SCHEMA:
        raise ComparisonError(
            f"{path} declares schema={document['schema']!r}; this script "
            "reads schema 3, 4 and 5")
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
        _check_schema_keys(path, name, populations[name], document["schema"])
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
    """One population's metric, tolerance, digest and entry maps.

    Schema 5 (the statement instrument) declares `seeds` instead of
    `slopes`: the list of entry ids awaiting their seed, which is both the
    validator's authorization and the ratchet writer's. An empty `entries`
    map is the declared blind window and is lawful only beside a `seeds`
    list — the mirror of the schema-3/4 rule that an empty `entries` needs
    slopes.
    """
    where = f"{path} populations[{name!r}]"
    if not isinstance(population, dict):
        raise ComparisonError(f"{where} is not an object")
    absent = [key for key in POPULATION_KEYS
              if key not in ("slopes", "seeds") and key not in population]
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
    _check_seeds(where, population.get("seeds"))
    # Either declared route licenses the blind window: slopes on schema 3/4,
    # seeds on schema 5. The schema rules above already refuse carrying both
    # eras in one document.
    _check_entry_map(
        path, name, population["entries"], "entries",
        required=not ("seeds" in population or "slopes" in population))
    _check_entry_map(path, name, population["wall"], "wall", required=False)
    if "slopes" in population:
        slopes = population["slopes"]
        if not isinstance(slopes, dict) or not slopes:
            raise ComparisonError(
                f"{where} slopes must be a non-empty object")
        for node, slope in slopes.items():
            if not isinstance(node, str) or not node:
                raise ComparisonError(f"{where} slopes has an empty or non-string node id")
            if not _is_number(slope) or not math.isfinite(slope) or slope <= 0:
                raise ComparisonError(
                    f"{where} slopes[{node!r}]={slope!r} is not a finite "
                    "positive number")
        for node in population["entries"]:
            if node not in slopes:
                raise ComparisonError(f"{where} slopes is missing entry {node!r}")


def _check_seeds(where: str, seeds) -> None:
    """`seeds`: a list of unique, non-empty entry ids, or absent entirely."""
    if seeds is None:
        return
    if not isinstance(seeds, list):
        raise ComparisonError(
            f"{where} seeds={seeds!r} is not a list of entry ids")
    seen = set()
    for node in seeds:
        if not isinstance(node, str) or not node:
            raise ComparisonError(
                f"{where} seeds has an empty or non-string entry id")
        if node in seen:
            raise ComparisonError(
                f"{where} seeds names {node!r} more than once")
        seen.add(node)


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


def _refuse_contradictory_declarations(allowed, raises):
    """A slot declared both removed and raised asks for two opposite
    verdicts; refuse it before anything is judged."""
    conflict = set(allowed) & set(raises)
    if conflict:
        raise ComparisonError(
            "these slots are both declared removed and declared raised: "
            + ", ".join(sorted(conflict))
            + " — that asks for two opposite verdicts at once; pick one")


def _is_seed_addition(old: dict, name: str, key: str, node: str,
                      after: dict, now) -> bool:
    """Whether `node`'s absence from `old` is the seeding path, not a raise.

    Three properties, all read from the documents: the base population
    declares the node's id in its `seeds` list (the authorisation that the
    entry's record awaits its first measurement, not that one was lost),
    the map is `entries` (wall never seeds), and the candidate envelope is
    seed-shaped — the observed value as both bounds, from at least the
    minimum sample count. Anything else about an entry the base never
    recorded stays exactly the raise it was.
    """
    if now is None or key != "entries" or now < 0 or not math.isfinite(now):
        return False
    declared = node in old.get("populations", {}).get(name, {}).get(
        "seeds", ())
    shape = (after[node]["min"] == now
             and isinstance(after[node]["n"], int)
             and after[node]["n"] >= MIN_ENVELOPE_SAMPLES)
    return declared and shape


def raised_entries(old: dict, new: dict,
                   allowed_removals=frozenset(),
                   allowed_raises=frozenset()) -> dict:
    """Entries that went UP between two baseline documents, or went AWAY.

    The comparator refuses to let a head counter exceed its baseline, but
    nothing there stops a commit from EDITING the baseline upwards in the same
    push — which is the gate switched off from the inside. So the workflow
    diffs the committed baseline against the base branch tip's copy and fails
    on any entry that moved up, in EITHER map: `entries`, which carries the
    counter ceilings, and `wall`, which carries the gross smoke ceilings.
    Governing only the first left half the document ungoverned, so a commit
    could raise the smoke bound from 0.085 s to 99 s in the same push and the
    step whose whole reason for existing would report success.

    A DELETED entry is a raise too. Iterating only the new document never
    visits an entry that was removed, so dropping one passed this check
    silently while `--require-test` went on demanding the workload ran — it
    simply stopped being compared.

    But refusing every deletion blocked the DOCUMENTED retirement path: a
    renderer workload is retired by dropping it from `WORKLOADS` and removing
    its entry in the same commit, and a shipped test asserted that route
    works. So a removal is allowed when it is DECLARED, and a declaration that
    contradicts the document is refused rather than honoured: an id declared
    removed but still recorded in `new` is a stale entry left behind by a
    half-finished retirement, and `compare()` intersects, so it would be
    silently dropped from the comparison. The caller derives the declaration
    from what `--list-workloads` no longer emits, so declaring a removal is
    how a commit says "the harness dropped this", and it is checked against
    the document rather than trusted.

    Lowering a recorded maximum, or raising a minimum alone, is an improvement
    and stays allowed: the ceiling is the maximum.

    An entry the base never recorded is a raise too — unless it is the
    seeding path (issues #240 and #269): the base population declares the
    node's id in its `seeds` list, the map is `entries`, and the candidate
    is seed-shaped (`_is_seed_addition`). That addition is the automated
    ratchet commit landing after an instrument reset; every other
    absent-entry addition keeps its original raise.

    A RAISE is permitted only for a slot the caller names in
    `allowed_raises`. The workflow derives unit-suite slots only when the
    judged base..head window carries a commit subject starting `feat:` or
    `feat(scope):`, and never names a renderer slot, so renderer ceilings
    stay strictly down-only. A named slot is checked against the documents
    rather than trusted: one that has NOT gone up, or names no slot either
    document has, is refused.
    """
    _refuse_contradictory_declarations(allowed_removals, allowed_raises)
    moved = {}
    declared_seen = set()
    for name in sorted(set(old.get("populations", {}))
                       | set(new.get("populations", {}))):
        for key in ("entries", "wall"):
            before = old.get("populations", {}).get(name, {}).get(key, {})
            after = new.get("populations", {}).get(name, {}).get(key, {})
            for node in sorted(set(before) | set(after)):
                slot = f"{name}:{key}:{node}"
                if slot in allowed_removals:
                    if node in after:
                        raise ComparisonError(
                            f"{slot} was declared removed but is still "
                            "recorded in the head document — a workload "
                            "retired from WORKLOADS still leaves its entry "
                            "behind here, and compare() intersects, so the "
                            "entry would be silently dropped from the "
                            "comparison rather than compared")
                    continue
                was = before.get(node, {}).get("max")
                now = after.get(node, {}).get("max")
                if slot in allowed_raises:
                    declared_seen.add(slot)
                if now is None:
                    moved[slot] = (was, None)
                    continue
                if (was is None and _is_seed_addition(old, name, key, node,
                                                      after, now)):
                    continue
                if was is None or now > was:
                    if slot in allowed_raises:
                        continue
                    moved[slot] = (was, now)
                elif slot in allowed_raises:
                    raise ComparisonError(
                        f"{slot} was declared raised but is not a raise "
                        f"({was} -> {now}): a declaration that contradicts "
                        "the documents is refused rather than honoured")
    if set(allowed_raises) - declared_seen:
        raise ComparisonError(
            "these slots were declared raised but name no slot that exists "
            "in either document: "
            + ", ".join(sorted(set(allowed_raises) - declared_seen))
            + " — a declaration is checked against the documents, and a "
              "declaration nothing resolves to is refused rather than "
              "silently ignored")
    return moved
