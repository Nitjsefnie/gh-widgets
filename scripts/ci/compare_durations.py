#!/usr/bin/env python3
"""Compare a counter against the committed baseline, per test and per workload.

THE NAME IS HISTORICAL. `compare_durations` compared wall-clock durations;
it now compares load-invariant COUNTERS — instruction counts, or CPU seconds
where the kernel refuses to count instructions. The filename is kept because
it is on every command line, in a workflow and in another test module's
import, and renaming it would break that seat's work mid-flight. A rename to
something like `compare_counters.py` is owed; it is not done here because
doing it here is not free.

Answers one question: did the work that exists in BOTH the baseline and this
commit get more expensive?

WHY NOT WALL TIME. The obvious metric — how long did it take — punishes the
wrong thing and, worse, measures the machine rather than the code. A
GitHub-hosted runner is a multi-tenant VM: steal time, a neighbour, a
different CPU model and a different core all move an elapsed duration. Two
runs of IDENTICAL code on the same runner differ by a factor of two on a
single test. The maintainer ruling behind issue #81 is that paired wall-clock
A/B is not a valid magnitude ANYWHERE — not on a shared box, and not in the
same job on a CI runner. So the quantity compared here is one runner load
cannot move, and elapsed time survives only as a gross smoke check that is
reported as a verdict with no number attached.

WHY MINIMUM ACROSS ROUNDS. Each round is a whole run of the same pinned
offline inputs; the minimum across them is the least-noisy observation of the
same quantity. Under the syscall instrument the count is deterministic and
this costs nothing but the runs it already needed. Under the CPU-seconds
fallback it is what separates a real change from one unlucky run, because
those seconds are not deterministic — which is also why the tolerance lives
in the baseline next to its metric and is re-derived whenever the metric
changes, rather than being one number that would have to be right for
whichever instrument is not in use.

WHY THE BASELINE IS COMMITTED DATA. The alternative — checking out the last
release and running both sides in this job — bought "the same runner", which
the ruling above says is not enough to make a wall-clock ratio meaningful. A
committed baseline removes the pairing entirely, and it is only sound because
the counter is stable enough to travel between jobs. It also means the
comparison point is reviewable: a baseline change shows up in the diff.

WHY THE CLOSED POPULATION IS OPTIONAL, AND WHY IT EXISTS HERE. The
intersection is deliberately permissive: adding a test or deleting a test
cannot move the number, and that is right for a suite whose membership
changes by design. It is wrong for a workload set that IS the thing being
gated. If a shipped renderer is renamed or its checkout is incomplete, the
intersection quietly compares one fewer program and the gate reports a
healthy improvement while measuring less than it claims — a gate that
measures fewer programs than it advertises is decorative. --require-test and
--allow-removal turn that off for a named population: each required name must
be present and passing in EVERY head round, and in that closed-set mode every
baseline name that vanished must be accounted for by --allow-removal.

A METRIC MISMATCH IS EXIT 2, NEVER A COMPARISON. An instruction count and a
CPU-second count are different quantities. Dividing one by the other produces
either a catastrophic regression or a spectacular speedup, depending on
which way the numbers fall, and neither reading would be true. So the metric
recorded in the baseline is checked against the metric the head run measured,
and a difference is a refusal naming both.

EXIT CODES. 0 within budget, 1 over budget, 2 could not compare at all. Those
are deliberately different: a workflow that reports a restructured suite as a
performance regression teaches people to read the red as noise, and a
ComparisonError therefore keeps the code it has always had.

    compare_durations.py --baseline speed-baseline.json \\
                         --head head1.xml head2.xml \\
                         --require-test e2e::bench.render
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_BASELINE = REPO_ROOT / "speed-baseline.json"
SUPPORTED_SCHEMA = 1

# The smoke gate's factor, deliberately loose. It is a net for "something is
# catastrophically wrong now" — an hour where the baseline says a minute — and
# deliberately NOT for "this got slower". It is reported as a verdict with no
# number attached, because the number is precisely what is not trustworthy
# here.
SMOKE_FACTOR = 3.0

# The instrument names, and what may be said about each one in a report.
METRIC_NOTES = {
    "syscalls": (
        "syscalls retired by the kernel across the whole traced tree, "
        "including the git subprocesses the renderers spawn; the count is "
        "deterministic"),
    "cpu_time": (
        "CPU seconds, so steal time is excluded — but NOT deterministic: "
        "5.7% min-to-max on a quiet runner, which is why its tolerance is "
        "the loose one"),
}

# A testcase carrying any of these children did not pass, and its value is not
# comparable: a failure can be cheap (an early assert) or dear (a timeout), and
# either way it is measuring the wrong thing.
NOT_PASSED = ("failure", "error", "skipped")


class ComparisonError(RuntimeError):
    """The comparison could not be made at all."""


class MissingBaseline(ComparisonError):
    """The baseline file is not there. Exit 0: no data is not a regression."""


def _load_counter():
    """Import scripts/ci/counter.py by path.

    Same reason this file itself is imported by path elsewhere: scripts/ci is
    not a package and deliberately has no __init__.py. The population digest
    lives in counter.py because a digest computed two different ways by two
    tools would compare unequal forever, and the resulting refusal would be
    indistinguishable from a real population change.
    """
    path = Path(__file__).resolve().with_name("counter.py")
    spec = importlib.util.spec_from_file_location("ghw_counter", path)
    if spec is None or spec.loader is None:
        raise ComparisonError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


counter = _load_counter()


def node_id(case: ET.Element) -> str:
    """`tests.test_api::test_thing`, stable across runs and machines."""
    classname = case.get("classname", "")
    name = case.get("name", "")
    return f"{classname}::{name}" if classname else name


def scan_junit(path: Path) -> tuple:
    """One report, read three ways: (passing values, present, not_passed).

    The intersection this comparator works on throws away everything that did
    not pass, which is right for the arithmetic and useless for the
    diagnosis: when a gated workload goes missing, "it is not in the
    intersection" and "it is in the report but failed" are different bugs
    with different fixes, and the reader of a red gate needs to be told
    which one happened. So the scan keeps both raw sets.

    `present` is every node id in the report. `not_passed` is every node id
    that produced no usable passing value — a failure, an error, a skip, or a
    testcase with no `time` attribute at all. Anything in `present -
    not_passed` is in the first element, so the folded dict is exactly the
    first element of every scan intersected.

    The `time` attribute read here is whatever the producer wrote into it.
    For counter.py and e2e_bench.py that is the COUNTER, not seconds, and
    the producer says which in the suite's gh-metric attribute; this file does
    not care, because a counter and a duration are both numbers and the one
    thing that must never happen — comparing across two different metrics —
    is checked explicitly in `_resolve_metric`.
    """
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ComparisonError(f"{path} is not parseable JUnit XML: {exc}") from exc

    times = {}
    present = set()
    for case in root.iter("testcase"):
        nid = node_id(case)
        present.add(nid)
        if any(case.find(tag) is not None for tag in NOT_PASSED):
            continue
        raw = case.get("time")
        if raw is None:
            continue
        try:
            times[nid] = float(raw)
        except ValueError:
            continue
    if not times:
        raise ComparisonError(f"{path} contains no passing testcases")
    return times, present, present - set(times)


def parse_junit(path: Path) -> dict:
    """{node_id: counter} for the passing testcases in one report."""
    return scan_junit(path)[0]


def scan_instrument(path: Path) -> tuple:
    """One report's (gh-metric, {node_id: gh-wall}), read once.

    The metric is a property of the whole run, not of a testcase, so it is
    taken from the testsuite element that carries it; a report that declares
    none reports None, which is what a pre-counter report does and which
    disables the mismatch check rather than inventing a metric.
    """
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ComparisonError(f"{path} is not parseable JUnit XML: {exc}") from exc
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    metric = suite.get("gh-metric") if suite is not None else None
    wall = {}
    for case in root.iter("testcase"):
        raw = case.get("gh-wall")
        if raw is None:
            continue
        try:
            wall[node_id(case)] = float(raw)
        except ValueError:
            continue
    return metric, wall


def fold_scans(scans: list) -> dict:
    """The minimum value per test across already-scanned rounds.

    The empty-list guard matches fold_rounds': an IndexError out of main is
    exit 1, which this file reserves for "over budget", so a programming
    error here would be reported to whoever reads the job summary as a
    performance regression.
    """
    if not scans:
        raise ComparisonError("no JUnit reports given")
    rounds = [scan[0] for scan in scans]
    common = set(rounds[0])
    for other in rounds[1:]:
        common &= set(other)
    if not common:
        raise ComparisonError(
            "no test passed in every round — nothing comparable"
        )
    return {nid: min(r[nid] for r in rounds) for nid in common}


def fold_rounds(paths: list) -> dict:
    """Per test, the MINIMUM across rounds.

    A test must have passed in EVERY round to be included. One that passed
    twice and was skipped once is not the same test in all three, and
    letting it through would compare two different populations.
    """
    if not paths:
        raise ComparisonError("no JUnit reports given")
    return fold_scans([scan_junit(Path(p)) for p in paths])


def _diagnose_required(name, head_scans):
    """One line per failing required name, naming the rounds and the why."""
    rounds = list(range(1, len(head_scans) + 1))
    absent = [n for n in rounds if name not in head_scans[n - 1][1]]
    if not absent:
        # Present everywhere, so the fault is a verdict, not an omission.
        failing = [n for n in rounds if name in head_scans[n - 1][2]]
        return (f"- `{name}`: present in all {len(rounds)} head report(s) "
                f"but did not pass in round(s) {_rounds(failing)}")
    if len(absent) == len(rounds):
        return (f"- `{name}`: absent from all {len(rounds)} head report(s) "
                "entirely — no testcase with that node id exists at all")
    failing = [n for n in rounds
               if n not in absent and name in head_scans[n - 1][2]]
    clause = (f", and did not pass in round(s) {_rounds(failing)}"
              if failing else "")
    return f"- `{name}`: absent from head round(s) {_rounds(absent)}{clause}"


def _rounds(numbers):
    return ", ".join(str(n) for n in numbers) if numbers else "none"


def verify_population(head_scans, base_folded, head_folded, require,
                      allow_removal):
    """Refuse to compare a population smaller than the one that was declared.

    Two distinct checks, because a gated set can be broken in two distinct
    ways and one of them is invisible to the other:

    * A name in `require` must exist and pass in EVERY head round. This
      catches a workload that is absent altogether, and one that is present
      but skipped or failed — the two are reported differently, since a
      silent skip is a renamed renderer while a failure is a real one.
    * In closed-set mode (either flag given), every name that passed in the
      baseline must still pass at the head, unless it was named in
      `allow_removal`. This catches the variant that leaves no trace at all
      in the report: a deleted testcase, rather than a skipped one.

    Presence is decided on `head_folded` — what would actually be compared,
    and folded by the caller so the value the verdict rests on is derived
    once rather than twice — while the diagnosis reads the scans, which still
    know which rounds the name was missing from.

    The "gone from the baseline" check is scoped to the JUNIT CLASSNAMES the
    head reports actually carry. One committed baseline covers every family
    the job measures, and each half of the gate sees only its own reports, so
    an unscoped check would report the unit-suite entry as a retired
    renderer every single run. A name whose classname appears nowhere in the
    head reports belongs to a comparison that is not this one.
    """
    require = list(require)
    allow_removal = list(allow_removal)
    conflict = sorted(set(require) & set(allow_removal))
    if conflict:
        raise ComparisonError(
            "these names are both required and allowed to be removed: "
            + ", ".join(f"`{n}`" for n in conflict)
            + " — that asks for a gate and an exemption at once; pick one"
        )
    if not require and not allow_removal:
        # Permissive mode, which is what this tool has always done. The unit
        # suite's membership changes by design and must not trip the gate.
        return

    problems = [
        _diagnose_required(name, head_scans)
        for name in sorted(require) if name not in head_folded
    ]
    head_classes = {identifier.split("::", 1)[0]
                    for _times, present, _not in head_scans
                    for identifier in present}
    candidates = {name for name in base_folded
                  if name.split("::", 1)[0] in head_classes}
    unexplained = sorted(candidates - set(head_folded) - set(allow_removal))
    if unexplained:
        problems.append(
            "workload(s) that passed at the baseline are gone from the head "
            "reports and were not declared as allowed removals:\n"
            + "\n".join(f"- `{n}`" for n in unexplained)
            + "\n  If this retirement is deliberate, name it with "
              "--allow-removal; otherwise a shipped renderer has stopped "
              "being measured."
        )
    if problems:
        raise ComparisonError(
            "the head build is not measuring the population this gate "
            "declares, so a verdict over the remaining entries would be "
            "decorative:\n" + "\n".join(problems))


def verify_unit_population(expected_digest, population_file):
    """The unit suite's collected population must be the baseline's.

    A counter total is a ratio over whatever tests happened to run, so a
    suite that grew by twenty tests moves the total with no product change —
    the field guide's own counter-ratchet lesson. Refusing is the honest form
    of that: it does not silently compare two different populations, and it
    does not exit green either. Re-deriving the baseline is a deliberate act.

    No `--population-file` means the caller is gating a population whose
    membership this file cannot enumerate — the renderer workload set, which
    is guarded instead by --require-test. The guard is therefore opt-in and
    applies to the one family that needs it.
    """
    if not population_file:
        return
    try:
        node_ids = [line.strip() for line in
                    Path(population_file).read_text(encoding="utf-8").splitlines()
                    if line.strip()]
    except OSError as exc:
        raise ComparisonError(
            f"{population_file} is not readable: {exc}") from exc
    if not node_ids:
        raise ComparisonError(
            f"{population_file} names no collected tests, so the population "
            "digest cannot be computed — an empty collection is not a "
            "population")
    actual = counter.population_digest(node_ids)
    if actual != expected_digest:
        raise ComparisonError(
            "this run collected a different unit-test population than the "
            "baseline was measured over, so the two counters are not "
            f"comparable:\n  baseline: {expected_digest}\n"
            f"  this run: {actual}\n  Re-derive the baseline "
            "(see `basis` in it) if the suite's membership genuinely "
            "changed. Note the two sides were not asked the same question."
        )


def compare(base: dict, head: dict) -> dict:
    shared = sorted(set(base) & set(head))
    if not shared:
        raise ComparisonError(
            "the two builds share no passing test — the suite was renamed "
            "or restructured wholesale, so there is nothing to compare"
        )

    base_total = sum(base[n] for n in shared)
    head_total = sum(head[n] for n in shared)
    if base_total <= 0:
        raise ComparisonError(
            "baseline total is zero — the reports carry no usable values"
        )

    per_test = []
    for nid in shared:
        was, now = base[nid], head[nid]
        # The floor is 0.05 because that number means two different things
        # and is right for both. Under cpu_time it is the 50 ms line this
        # file has always used, where the value is overhead rather than the
        # work. Under a syscall count — a number that is thousands, hundreds
        # of thousands — it excludes only entries that measured essentially
        # nothing, and counts them in the totals regardless, which is where
        # they belong.
        if was >= 0.05:
            per_test.append((now - was, (now / was) - 1.0, nid, was, now))
    per_test.sort(reverse=True)

    return {
        "shared": len(shared),
        "base_only": sorted(set(base) - set(head)),
        "head_only": sorted(set(head) - set(base)),
        "base_total": base_total,
        "head_total": head_total,
        "ratio": head_total / base_total,
        "per_test": per_test,
    }


def over_tolerance(base: dict, head: dict, tolerance: float) -> dict:
    """Every entry whose head value exceeds `baseline * (1 + tolerance)`.

    The down-only ratchet, per entry rather than on the total: a total can
    hide one expensive entry behind a hundred cheap ones, and the issue asks
    for a per-workload counter baseline, not a suite-wide one.
    """
    return {nid: (base[nid], head[nid]) for nid in sorted(set(base) & set(head))
            if head[nid] > base[nid] * (1.0 + tolerance)}


def smoke_failures(head_wall: dict, baseline_wall: dict,
                   factor: float = SMOKE_FACTOR) -> list:
    """Node ids whose wall time is more than `factor` times the recorded one.

    Wall time is not a magnitude and never appears as one; this is the only
    place elapsed seconds are consulted, and all it asks is whether the run
    fell off a cliff.
    """
    return sorted(nid for nid, was in baseline_wall.items()
                  if nid in head_wall and was > 0
                  and head_wall[nid] > was * factor)


def _format_counter(value) -> str:
    """A counter for a report: grouped when integral, 6 significant otherwise."""
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:.6g}"


def render(result: dict, threshold: float, base_label: str) -> str:
    delta = result["ratio"] - 1.0
    metric = result.get("metric") or "counter"
    verdict = "🔴 REGRESSION" if delta > threshold else "🟢 within budget"
    lines = [
        f"### Counter cost vs `{base_label}`",
        "",
        f"**{verdict}** — {delta:+.1%} "
        f"(budget {threshold:+.0%})",
        "",
        f"- Metric: **{metric}** — {METRIC_NOTES.get(metric, 'unrecognised')}",
        f"- Compared on **{result['shared']}** entries passing in both",
        f"- Baseline `{base_label}`: **{_format_counter(result['base_total'])}**",
        f"- This commit: **{_format_counter(result['head_total'])}**",
        "",
        "_No elapsed time appears in this report, deliberately. A "
        "GitHub-hosted runner is a multi-tenant VM, so a wall-clock duration "
        "measured here is not a magnitude of anything; it survives only as "
        "the gross smoke check below, which reports a verdict and no "
        "number._",
    ]
    if result.get("head_only"):
        lines.append(
            f"- {len(result['head_only'])} entry/entries in this report "
            "that the baseline does not record, excluded from the comparison"
        )
    if result.get("base_only"):
        lines.append(
            f"- {len(result['base_only'])} baseline entry/entries not "
            "present in these head reports, excluded from the comparison "
            "(the baseline covers every family this job measures, and each "
            "half of the gate sees only its own reports)"
        )

    over = result.get("over") or {}
    if over:
        lines += [
            "",
            f"**🔴 {len(over)} entry/entries past the down-only tolerance** "
            f"(baseline × {1 + result['tolerance']:.2f})",
            "",
            "| Entry | Baseline | This commit |",
            "| --- | ---: | ---: |",
        ]
        for nid, (was, now) in over.items():
            lines.append(
                f"| `{nid}` | {_format_counter(was)} | {_format_counter(now)} |"
            )

    movers = [row for row in result["per_test"] if abs(row[1]) >= 0.10][:15]
    if movers:
        lines += [
            "",
            "<details><summary>Largest per-entry movements</summary>",
            "",
            "| Entry | Was | Now | Change |",
            "| --- | ---: | ---: | ---: |",
        ]
        for _abs_delta, rel, nid, was, now in movers:
            lines.append(
                f"| `{nid}` | {_format_counter(was)} | {_format_counter(now)} "
                f"| {rel:+.0%} |"
            )
        lines += [
            "",
            "_**A row here is not a regression by itself.** The verdict is "
            "the total and the down-only ratchet above; a row is where to "
            "start looking once one of those has gone red. A CPU-second "
            "counter is only approximately stable — cache and frequency "
            "effects move it — so a single entry's percentage is a "
            "starting point, not a finding._",
            "",
            "_Entries whose baseline value is under 0.05 are omitted — at "
            "that scale the number is overhead, not the work. They still "
            "count in the totals above._",
            "",
            "</details>",
        ]

    smoke = result.get("smoke")
    if smoke is not None:
        if smoke:
            lines += [
                "",
                f"**🔴 SMOKE FAIL** — {len(smoke)} entry/entries took "
                f"grossly longer than the baseline recorded. Elapsed time is "
                "not reported: see the disclaimer above. Failing entries: "
                + ", ".join(f"`{n}`" for n in smoke),
            ]
        else:
            lines += [
                "",
                "**🟢 SMOKE PASS** — no workload took grossly longer than "
                "the baseline recorded. No elapsed time is reported.",
            ]
    return "\n".join(lines) + "\n"


def render_failure(base_label: str, message: str) -> str:
    """A job summary for the ComparisonError path.

    The full detail is already on stderr and in the step log; this is
    deliberately much terser, but it names the baseline and says what went
    wrong, which is the difference between a failure somebody can act on and
    one they have to go and reconstruct from a log. A green gate with an
    empty summary is the same defect pointed the other way, which is why the
    no-baseline path below writes one too.
    """
    first = message.splitlines()[0] if message else "no reason given"
    return "\n".join([
        f"### Counter cost vs `{base_label}`",
        "",
        f"**🔴 COULD NOT COMPARE** — {first}",
        "",
        "```",
        message,
        "```",
        "",
    ])


def render_no_baseline(metric: str, head_folded: dict,
                       base_label: str) -> str:
    """The report for a first run with nothing committed to compare against.

    Same shape as the "no release exists yet" path this file has always had:
    it states plainly that no comparison happened, and it exits 0 rather than
    failing a commit for the absence of data. What it must NOT do is print
    nothing — a green gate with an empty summary is the thing this
    repository keeps failing reviews over — so it carries the measured
    counters in a form a person can paste straight into the baseline.
    """
    block = json.dumps({"metric": metric, "entries": head_folded},
                       indent=2, sort_keys=True)
    return "\n".join([
        f"### Counter cost vs `{base_label}`",
        "",
        f"**No baseline to compare against** — `{base_label}` does not "
        "exist, so nothing was compared and nothing failed.",
        "",
        f"This run measured **{len(head_folded)}** entries on `{metric}`. To "
        "adopt them as the baseline, fill `entries` in the baseline "
        "document with this block, set `metric`, `cell`, `basis`, "
        "`measured_commit` and `measured_at`, and re-run.",
        "",
        "```json",
        block,
        "```",
        "",
    ])


# ---------------------------------------------------------------------------
# The committed baseline document.
#
# Modelled on coverage-floor.json, deliberately: committed data rather than a
# literal in a workflow, a REQUIRED `basis` so the reason a number sits where
# it does lives at the number rather than in a pull request that will not be
# there when someone reads the file, a provenance pair, and a validator that
# refuses rather than degrades. A baseline that silently defaulted every
# missing key would be a gate that reports green precisely when its own
# configuration is broken.
# ---------------------------------------------------------------------------

REQUIRED_KEYS = ("schema", "basis", "metric", "cell", "tolerance",
                 "measured_commit", "measured_at", "population", "entries",
                 "wall")


def _is_number(value) -> bool:
    """A JSON number, excluding booleans — `True` is an int in Python."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def read_baseline(path: Path) -> dict:
    """The committed baseline, validated before it is used for anything."""
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError as exc:
        raise MissingBaseline(path) from exc
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
    if document["metric"] not in counter.METRICS:
        raise ComparisonError(
            f"{path} metric={document['metric']!r} is not an instrument "
            "counter.py defines")
    for key in ("cell", "measured_commit", "measured_at"):
        value = document[key]
        if not isinstance(value, str) or not value.strip():
            raise ComparisonError(
                f"{path} {key}={value!r} is not a non-empty string")
    tolerance = document["tolerance"]
    if not _is_number(tolerance) or tolerance <= 0:
        raise ComparisonError(
            f"{path} tolerance={tolerance!r} is not a positive fraction")
    population = document["population"]
    if (not isinstance(population, str)
            or len(population) != 64
            or any(character not in "0123456789abcdef" for character in population)):
        raise ComparisonError(
            f"{path} population={population!r} is not a sha256 digest of the "
            "suite's collected node ids")
    _check_entry_map(path, document["entries"], "entries", required=True)
    _check_entry_map(path, document["wall"], "wall", required=False)
    return document


def _check_entry_map(path: Path, mapping, key: str, required: bool) -> None:
    """Node id -> a positive number. Named for `entries` and `wall`."""
    if not isinstance(mapping, dict):
        raise ComparisonError(f"{path} {key}={mapping!r} is not an object")
    if required and not mapping:
        raise ComparisonError(
            f"{path} {key} is empty — a baseline with no entries in it "
            "compares nothing and reports that as a pass")
    for node, value in mapping.items():
        if not isinstance(node, str) or not node:
            raise ComparisonError(f"{path} {key} has a non-string node id")
        if not _is_number(value) or value < 0:
            raise ComparisonError(
                f"{path} {key}[{node!r}]={value!r} is not a non-negative "
                "number")


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
    for node, now in new.get("entries", {}).items():
        was = old.get("entries", {}).get(node)
        if was is None or now > was:
            moved[node] = (was, now)
    return moved


def collect_node_ids(directory: Path) -> list:
    """The test node ids a discovery pass collects under `directory`.

    A COLLECTION pass, not a test run: `unittest.TestLoader().discover`
    imports every test module and builds the suite, but executes nothing.
    That is the cheap half of what the baseline's population digest is about
    — the expensive half is running the tests, which the caller has already
    done and reported.
    """
    loader = unittest.TestLoader()
    try:
        discovered = loader.discover(str(directory), top_level_dir=str(directory))
    except Exception as exc:  # pylint: disable=broad-exception-caught
        raise ComparisonError(
            f"{directory} could not be collected, so the population digest "
            f"cannot be computed: {exc}") from exc
    found = []

    def walk(item):
        for thing in item:
            if isinstance(thing, unittest.TestSuite):
                walk(thing)
            else:
                found.append(thing.id())

    walk(discovered)
    if not found:
        raise ComparisonError(
            f"{directory} collected no tests, which is not a population")
    return sorted(set(found))


def _resolve_metric(paths, label) -> str:
    """The one instrument every head report agrees on, or a refusal."""
    seen = []
    for path in paths:
        metric, _wall = scan_instrument(Path(path))
        if metric and metric not in seen:
            seen.append(metric)
    if len(seen) > 1:
        raise ComparisonError(
            "the head rounds disagree about what they measured ("
            + ", ".join(seen) + f") across the {label} reports, so there is "
            "no single number to compare")
    return seen[0] if seen else ""


def _check_metric(baseline_metric, head_metric) -> None:
    if baseline_metric and head_metric and baseline_metric != head_metric:
        raise ComparisonError(
            f"metric mismatch: the baseline records `{baseline_metric}` and "
            f"this run measured `{head_metric}`. Those are different "
            "quantities — one is not faster than the other — so nothing was "
            "compared. Re-derive the baseline on the instrument this job "
            "actually has.")


def ratchet_check(old_path: Path, new_path: Path) -> int:
    """The down-only half of the gate: no baseline entry may go UP.

    Run against the MERGE BASE's copy of the baseline, not the parent's last
    successful run and not the previous commit on this branch — a gate that
    judges the wrong base is worse than no gate, because it reads as one.
    """
    old = read_baseline(old_path)
    new = read_baseline(new_path)
    moved = raised_entries(old, new)
    if not moved:
        print(f"baseline ratchet: no entry raised in {new_path}")
        return 0
    lines = ["baseline ratchet: these entries went UP, and this baseline "
             "only ever ratchets down:"]
    for node, (was, now) in moved.items():
        lines.append(f"- `{node}`: {was} -> {now}")
    lines.append("  Lowering is always allowed. If a rise is real, it is "
                 "because the work got dearer — say so in the pull request.")
    print("\n".join(lines), file=sys.stderr)
    return 1


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--base", nargs="+", metavar="XML",
                        help="baseline JUnit report(s), for the historical "
                             "same-job comparison. Mutually exclusive with "
                             "--baseline")
    parser.add_argument("--baseline", default=None, metavar="PATH",
                        help="the committed baseline document to compare "
                             f"against (e.g. {DEFAULT_BASELINE})")
    parser.add_argument("--head", nargs="+", metavar="XML",
                        help="this commit's JUnit report(s)")
    parser.add_argument("--max-regression", type=float, default=0.30,
                        help="fail above this fractional increase in the "
                             "total (default: 0.30 = 30%%)")
    parser.add_argument("--tolerance", type=float, default=None,
                        help="per-entry down-only tolerance, as a fraction. "
                             "Defaults to the baseline's own `tolerance`")
    parser.add_argument("--base-label", default="the committed baseline",
                        help="name of the baseline, for the report")
    parser.add_argument("--summary-file", default=None,
                        help="append the markdown report here as well as "
                             "printing it (e.g. $GITHUB_STEP_SUMMARY)")
    parser.add_argument("--require-test", action="append", default=[],
                        metavar="NAME",
                        help="node id that must be present and passing in "
                             "EVERY head report, e.g. e2e::bench.render. "
                             "Repeatable. A name absent from the head "
                             "reports, or skipped/failed in any round, is a "
                             "ComparisonError (exit 2).")
    parser.add_argument("--allow-removal", action="append", default=[],
                        metavar="NAME",
                        help="node id whose disappearance from the head "
                             "reports is deliberate. Repeatable. Passing "
                             "either this or --require-test switches the "
                             "gate to CLOSED-SET mode, where every name that "
                             "passed at the baseline must still pass at the "
                             "head unless it is named here. With neither "
                             "flag, removals are reported and excluded, "
                             "which is this tool's historical behaviour.")
    parser.add_argument("--population-file", default=None, metavar="PATH",
                        help="the node ids this run COLLECTED, one per line. "
                             "Their digest is checked against the "
                             "baseline's, and a difference is a refusal "
                             "(exit 2). Produced by --print-population")
    parser.add_argument("--print-population", default=None, metavar="DIR",
                        help="collect the test node ids under DIR, print "
                             "them one per line, and exit. A collection "
                             "pass, not a test run")
    parser.add_argument("--ratchet-baselines", nargs=2,
                        metavar=("OLD", "NEW"),
                        help="compare two baseline documents and fail if any "
                             "entry in NEW is above its value in OLD; the "
                             "down-only half of the gate. Needs nothing else")
    args = parser.parse_args(argv)

    if args.print_population:
        print("\n".join(collect_node_ids(Path(args.print_population))))
        return 0

    if args.ratchet_baselines:
        return ratchet_check(Path(args.ratchet_baselines[0]),
                             Path(args.ratchet_baselines[1]))

    if args.base and args.baseline:
        parser.error("--base and --baseline are two sources for the same "
                     "side; pick one")
    if not args.base and not args.baseline:
        parser.error("either --base or --baseline is required; without a "
                     "baseline there is nothing to compare against")
    if not args.head:
        parser.error("--head is required")

    try:
        return _compare(args)
    except MissingBaseline:
        return _no_baseline(args)
    except ComparisonError as exc:
        return _cannot_compare(args, str(exc))


def _compare(args) -> int:
    """The whole comparison, once the arguments have been vetted."""
    baseline = None
    if args.baseline:
        baseline = read_baseline(Path(args.baseline))
        base_folded = {nid: float(value)
                       for nid, value in baseline["entries"].items()}
    else:
        base_folded = fold_rounds(args.base)

    head_scans = [scan_junit(Path(p)) for p in args.head]
    head_folded = fold_scans(head_scans)
    head_metric = _resolve_metric(args.head, "head")
    if baseline is not None:
        _check_metric(baseline["metric"], head_metric)
        verify_unit_population(baseline["population"], args.population_file)
    verify_population(head_scans, base_folded, head_folded,
                      args.require_test, args.allow_removal)

    tolerance = (args.tolerance if args.tolerance is not None
                 else (baseline["tolerance"] if baseline is not None
                       else args.max_regression))
    result = compare(base_folded, head_folded)
    result["metric"] = (baseline["metric"] if baseline is not None
                        else (head_metric or "counter"))
    result["tolerance"] = tolerance
    result["over"] = over_tolerance(base_folded, head_folded, tolerance)
    if baseline is not None and baseline.get("wall"):
        result["smoke"] = smoke_failures(_fold_wall(args.head),
                                         baseline["wall"])
    return _verdict(args, result)


def _fold_wall(paths) -> dict:
    """The minimum gh-wall per node id across rounds, for the smoke gate."""
    wall = {}
    for path in paths:
        for nid, seconds in scan_instrument(Path(path))[1].items():
            wall[nid] = min(seconds, wall.get(nid, seconds))
    return wall


def _no_baseline(args) -> int:
    """No committed baseline: report what was measured, and exit 0."""
    head_folded = fold_scans([scan_junit(Path(p)) for p in args.head])
    report = render_no_baseline(_resolve_metric(args.head, "head"),
                                head_folded, args.base_label)
    print(report)
    if args.summary_file:
        _append(args.summary_file, report)
    return 0


def _cannot_compare(args, message: str) -> int:
    print(f"cannot compare: {message}", file=sys.stderr)
    if args.summary_file:
        _append(args.summary_file, render_failure(args.base_label, message))
    return 2


def _verdict(args, result: dict) -> int:
    """Print the report, then decide the exit code. Every red names its why."""
    report = render(result, args.max_regression, args.base_label)
    print(report)
    if args.summary_file:
        _append(args.summary_file, report)

    if result.get("smoke"):
        print("FAIL: the gross wall-clock smoke check tripped on "
              + ", ".join(result["smoke"]) + ". No elapsed time is reported: "
              "it is not a magnitude. This usually means the run was "
              "starved, not that the code got slower.", file=sys.stderr)
        return 1
    delta = result["ratio"] - 1.0
    if delta > args.max_regression:
        print(
            f"FAIL: the shared entries cost {delta:+.1%} more than "
            f"{args.base_label}, over the {args.max_regression:+.0%} budget.",
            file=sys.stderr,
        )
        return 1
    if result["over"]:
        tolerance = result["tolerance"]
        print(
            "FAIL: past the down-only tolerance: "
            + ", ".join(result["over"])
            + f" (baseline x {1 + tolerance:.2f})", file=sys.stderr)
        return 1
    return 0


def _append(path, text) -> None:
    """Append to the summary file; never open the file the caller read."""
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(text)


if __name__ == "__main__":
    sys.exit(main())
