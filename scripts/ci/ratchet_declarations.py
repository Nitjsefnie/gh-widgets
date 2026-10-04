#!/usr/bin/env python3
"""Authorize unit-suite raises from pure carrier PRs' commit messages.

Print one slot per line for compare_counters.py's unchanged
--ratchet-allow-raise flag. No output is emitted until every declaration has
been checked. A refusal exits 1; unavailable history or documents exit 2.
Commit messages bind declarations and measuring run ids to the tested SHA;
editing a PR body does not re-run the speed job.
"""
from __future__ import annotations

import argparse
import importlib.util
import re
import subprocess
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

DOCUMENT = "speed-baseline.json"
_DECLARATION_PREFIX = 'Budget-Raise: '
_DECLARATION_LINE = re.compile(
    r'^(?P<document>\S+) +(?P<key>.+) +(?P<was>\S+) -> (?P<to>\S+)$')
_RUN_ID = re.compile(
    r'(?<!\S)https://github\.com/[^/\s]+/[^/\s]+/actions/runs/'
    r'[0-9]+(?=[/?#\s]|$)|(?<!\S)[0-9]{8,}(?!\S)')
_FORMAT = 'Budget-Raise: <document> <key> <from> -> <to>'


class DeclarationError(ValueError):
    """The carrier or its declarations do not describe a permitted raise."""


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


def parse_declarations(lines):
    """Port the greedy-key, finite-Decimal grammar from check_ratchets.py."""
    declarations = []
    errors = []
    for line in lines:
        match = _DECLARATION_LINE.match(line[len(_DECLARATION_PREFIX):])
        try:
            if match is None:
                raise InvalidOperation
            was = Decimal(match.group('was'))
            to = Decimal(match.group('to'))
            if not (was.is_finite() and to.is_finite()):
                raise InvalidOperation
        except InvalidOperation:
            errors.append(f"Budget-Raise declaration {line!r} does not parse "
                          f"— the format is '{_FORMAT}'")
            continue
        declarations.append((match.group('document'), match.group('key'),
                             was, to))
    return declarations, errors


def _collect_declarations(commits):
    """Scan every message line, with provenance required on each carrier."""
    declarations = []
    errors = []
    for sha, body in commits:
        lines = [line.strip() for line in body.splitlines()]
        raw = [line for line in lines
               if line.startswith(_DECLARATION_PREFIX)]
        if not raw:
            continue
        # A large from/to value is not measuring provenance. Require the run
        # id on a non-declaration line of this same carrying message.
        provenance = '\n'.join(line for line in lines
                               if not line.startswith(_DECLARATION_PREFIX))
        if _RUN_ID.search(provenance) is None:
            errors.append(f"Budget-Raise: commit {sha[:12]} must record the "
                          "dispatch run ids the re-derivation was measured on")
        parsed, malformed = parse_declarations(raw)
        errors.extend(malformed)
        for document, key, was, to in parsed:
            if document != DOCUMENT:
                errors.append(f"{document}: {key}: document is not declarable "
                              f"— only {DOCUMENT} may carry raises")
            elif not key.startswith('unit-suite:'):
                errors.append(f"{document}: {key}: only unit-suite raises are "
                              "declarable; renderer ceilings stay strictly "
                              "down-only")
            else:
                declarations.append((document, key, was, to))
    return declarations, errors


def _consume_declaration(declarations, key, was, now):
    """Consume one exact line; duplicates remain as refused leftovers."""
    for index, (_document, decl_key, decl_was, decl_to) in enumerate(
            declarations):
        if (decl_key == key and was is not None
                and decl_was == Decimal(str(was))
                and decl_to == Decimal(str(now))):
            del declarations[index]
            return True
    return False


def derive_raises(old, new, commits, changed_paths):
    """Return validated slot names; only a baseline-only PR may raise."""
    unit_raises = {
        slot: values for slot, values in baseline.raised_entries(old, new).items()
        if slot.startswith('unit-suite:') and values[1] is not None
    }
    declarations, errors = _collect_declarations(commits)
    if unit_raises and set(changed_paths) != {DOCUMENT}:
        errors.append(
            "Unit-suite raise refused: the PR must touch only "
            f"{DOCUMENT}; changed paths: {', '.join(sorted(changed_paths))}. "
            "Split the change: land the code PR first (red on speed), then "
            "open the raise carrier re-derived from runs on that head.")
    allowed = []
    for slot, (was, now) in unit_raises.items():
        if _consume_declaration(declarations, slot, was, now):
            allowed.append(slot)
        else:
            errors.append(f"{DOCUMENT}: {slot}: raise {was} -> {now} has no "
                          f"declaration — record one '{_FORMAT}' line per "
                          "raised key in the carrying commit")
    for document, key, was, to in declarations:
        errors.append(f"{document}: {key}: declaration {was} -> {to} does not "
                      "match a raise in this diff — a declaration must name "
                      "the exact from and to the diff carries")
    if errors:
        raise DeclarationError('\n'.join(errors))
    # Keep the comparator's own contradictory/unknown-slot checks in charge
    # of the unchanged flag semantics too. Renderer changes remain undeclared
    # and the workflow's subsequent ratchet comparison judges them.
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
    """Read the full base..head message window and the PR-level path set."""
    if _git(cwd, ['rev-parse', '--is-shallow-repository']).strip() != 'false':
        raise RuntimeError("declarations require full history; fetch the base "
                           "and head ancestry before judging the carrier")
    records = _git(cwd, ['log', '--format=%x00%H%n%B', f'{base}..{head}'])
    commits = []
    for record in records.split('\0'):
        if record:
            sha, _newline, body = record.partition('\n')
            commits.append((sha, body))
    # Base can advance after Actions creates its checkout merge. A PR's
    # paths start at its merge base; base-only commits are not PR edits.
    paths = _git(cwd, ['diff', '--no-renames', '--name-only', '-z',
                       f'{base}...{head}'])
    return commits, set(filter(None, paths.split('\0')))


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
        commits, paths = collect_window(Path.cwd(), args.base, args.head)
        slots = derive_raises(old, new, commits, paths)
    except DeclarationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (baseline.ComparisonError, OSError, RuntimeError) as exc:
        print(f"cannot derive Budget-Raise declarations: {exc}", file=sys.stderr)
        return 2
    for slot in slots:
        print(slot)
    return 0


if __name__ == '__main__':
    sys.exit(main())
