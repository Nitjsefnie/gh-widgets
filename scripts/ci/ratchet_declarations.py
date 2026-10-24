#!/usr/bin/env python3
"""Admit unit-suite raises only from a feature commit in the judged window.

Reads one thing from git: the folded subject (%s) of every commit in
--base..--head. A raised unit-suite maximum is admitted only when some
subject starts `feat:` or `feat(scope):`, case-sensitive; then each raised
unit-suite slot prints, one per line, for compare_counters.py's
--ratchet-allow-raise. A unit-suite entry the base never recorded is refused
with exit 1. Any other unit-suite raise is refused with exit 1 and prints no
slot. Shallow, missing or unreadable history, or an unreadable document,
exits 2.
"""
from __future__ import annotations

import argparse
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

DOCUMENT = "gate/speed-baseline.json"
_FEATURE_SUBJECT = re.compile(r'^feat:|^feat\([^)]*\):')


class DeclarationError(ValueError):
    """A unit-suite maximum rose without a feature commit to admit it."""


def _load_baseline():
    """Load the sibling validator by path, as compare_counters.py does."""
    path = Path(__file__).resolve().with_name("baseline.py")
    spec = importlib.util.spec_from_file_location("ghw_carrier_baseline", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


baseline = _load_baseline()

_TRIM_REMEDY = (
    "A non-feature change answers a speed-budget breach by trimming its "
    "own tests until the suite fits its envelope: drop redundant cases, "
    "merge parametrisations, delete tests that repeat another test's "
    "assertions. Lowering a maximum is always allowed; renderer ceilings "
    "stay strictly down-only.")


def derive_raises(old, new, subjects, window="the judged window"):
    """Return raised unit-suite slots, admitted only by a feat: subject."""
    moved = baseline.raised_entries(old, new)
    # A None `was` is an entry the base never recorded, which no subject
    # admits; the seed shape never reaches here.
    additions = {
        slot: values for slot, values in moved.items()
        if slot.startswith('unit-suite:') and values[0] is None
    }
    if additions:
        raised = '\n'.join(f"- {slot}: {was} -> {now}"
                           for slot, (was, now) in sorted(additions.items()))
        raise DeclarationError(
            f"{DOCUMENT}: unit-suite entry addition refused: the base never "
            f"recorded:\n{raised}\n"
            "No commit subject admits an entry the base never recorded; an "
            "envelope enters the document only through the seeding route "
            "(the declared slope), never written by hand. " + _TRIM_REMEDY)
    unit_raises = {
        slot: values for slot, values in moved.items()
        if slot.startswith('unit-suite:') and values[0] is not None
        and values[1] is not None
    }
    if not unit_raises:
        return []
    # Git separates subjects with LF; other separators can be subject content.
    if not any(_FEATURE_SUBJECT.match(subject) for subject in subjects):
        raised = '\n'.join(f"- {slot}: {was} -> {now}"
                           for slot, (was, now) in sorted(unit_raises.items()))
        raise DeclarationError(
            f"{DOCUMENT}: unit-suite raise refused: no commit subject in "
            f"{window} starts with `feat:` or `feat(scope):`.\n{raised}\n"
            + _TRIM_REMEDY)
    allowed = sorted(unit_raises)
    # The comparator's own contradictory/unknown-slot checks still apply;
    # renderer raises stay undeclared for the ratchet comparison to refuse.
    baseline.raised_entries(old, new, allowed_raises=allowed)
    return allowed


def _git(cwd, args):
    result = subprocess.run(  # pylint: disable=subprocess-run-check
        ['git', *args], cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"cannot read git history ({' '.join(args)}): "
                           f"{result.stderr.strip()}")
    return result.stdout


def collect_window(cwd, base, head):
    """Read the folded subject of every commit in base..head."""
    if _git(cwd, ['rev-parse', '--is-shallow-repository']).strip() != 'false':
        raise RuntimeError("the raise check requires full history; fetch the "
                           "base and head ancestry before judging the window")
    return _git(cwd, ['log', '--format=%s', f'{base}..{head}']).split('\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('old', type=Path)
    parser.add_argument('new', type=Path)
    parser.add_argument('--base', required=True, help='freshly fetched base tip')
    parser.add_argument('--head', default='HEAD', help='PR checkout commit')
    args = parser.parse_args(argv)
    try:
        old = baseline.read_baseline(args.old)
        new = baseline.read_baseline(args.new)
        subjects = collect_window(Path.cwd(), args.base, args.head)
        slots = derive_raises(old, new, subjects,
                              window=f"{args.base}..{args.head}")
    except DeclarationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (baseline.ComparisonError, OSError, RuntimeError) as exc:
        print(f"cannot derive unit-suite raises: {exc}", file=sys.stderr)
        return 2
    for slot in slots:
        print(slot)
    return 0


if __name__ == '__main__':
    sys.exit(main())
