#!/usr/bin/env python3
"""Compare a counter against the committed baseline, per test and per workload.

THE NAME IS HISTORICAL. `compare_durations` compared wall-clock durations; it
now compares load-invariant COUNTERS — syscalls for the renderer workloads,
CPU seconds for the unit suite. The filename is kept because it is on every
command line and in another test module's import, and renaming it would break
that seat's work mid-flight. A rename to `compare_counters.py` is owed.

WHY NOT WALL TIME. A GitHub-hosted runner is a multi-tenant VM: steal time, a
neighbour, a different core and a different CPU model all move an elapsed
duration, and none of that is the code being measured. Two runs of IDENTICAL
code in one job differ by a factor of two on a single test. The maintainer
ruling behind issue #81 is stronger: paired wall-clock A/B is not a valid
magnitude ANYWHERE — not on a shared box, and not in one job on a CI runner.
So the quantity compared here is one runner load cannot move, and elapsed
time survives only as a gross smoke check reported as a verdict with no
number attached. That ruling is also why there is no base checkout any more:
the committed baseline IS the comparison point.

WHY ONE METRIC AND TOLERANCE PER POPULATION, NOT PER DOCUMENT. The renderer
workloads are counted with `strace -f -c`: they are the shipped product,
they are cheap to trace, and a syscall count over them is stable. The unit
suite is measured with CPU seconds instead, because tracing it measured at
about 3.2x its untraced wall time (this box; the runner is faster but the
ratio is the point). One global `metric` can therefore only ever be right
for one of the two, so the baseline carries a sub-document per population,
each with the tolerance that belongs to its own instrument, and --population
says which contract this run is being held to.

WHY MINIMUM ACROSS ROUNDS. Each round is a whole run of the same pinned
offline inputs, and the minimum across them is the least-noisy observation of
the same quantity. Under the syscall count that costs nothing extra. Under
CPU seconds — which are not deterministic, 15.9% min-to-max here — it is what
separates a real change from one unlucky run.

WHY THE CLOSED POPULATION IS OPTIONAL, AND WHY IT EXISTS HERE. The
intersection is deliberately permissive: adding or removing a test cannot move
the number, which is right for a suite whose membership changes by design. It
is wrong for a workload set that IS the thing being gated — a renamed renderer
would leave the gate comparing one fewer program and still calling it healthy,
and a gate that measures fewer programs than it advertises is decorative.
--require-test and --allow-removal turn that off for a named population.

A METRIC MISMATCH IS EXIT 2, NEVER A COMPARISON. A syscall count and a
CPU-second count are different quantities; dividing one by the other produces
either a catastrophic regression or a spectacular speedup, and neither reading
would be true.

EXIT CODES. 0 within budget, 1 over budget, 2 could not compare at all. Those
are deliberately different: a workflow that reports a restructured suite as a
performance regression teaches people to read the red as noise.

    compare_durations.py --baseline speed-baseline.json --population unit-suite \
                         --head head1.xml head2.xml \
                         --require-test e2e::bench.render
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_BASELINE = REPO_ROOT / "speed-baseline.json"
SUPPORTED_SCHEMA = 2

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
    with different fixes. So the scan keeps both raw sets. `present` is every
    node id in the report; `not_passed` is every one that produced no usable
    passing value — a failure, an error, a skip, or a testcase with no `time`
    at all.

    The `time` attribute read here is whatever the producer wrote into it:
    for counter.py and e2e_bench.py that is the COUNTER, not seconds, and
    the producer says which in the suite's gh-metric. This file does not
    care, because comparing across two different metrics is checked
    explicitly in `_resolve_metric` and `_check_metric`.
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

    * A name in `require` must exist and pass in EVERY head round, which
      catches a workload that is absent altogether and one that is present
      but skipped or failed — reported differently, since a silent skip is a
      renamed renderer while a failure is a real one.
    * In closed-set mode (either flag given), every name that passed in the
      baseline must still pass at the head unless it was named in
      `allow_removal`. This catches the variant that leaves no trace at all:
      a deleted testcase rather than a skipped one.

    Presence is decided on `head_folded` — what would actually be compared —
    while the diagnosis reads the scans, which know which rounds a name was
    missing from.

    The "gone from the baseline" check is scoped to the JUNIT CLASSNAMES the
    head reports carry. One baseline covers every population the job
    measures and each half of the gate sees only its own reports, so an
    unscoped check would report the unit-suite entry as a retired renderer on
    every run. A name whose classname appears nowhere in the head reports
    belongs to a comparison that is not this one.
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
    does not exit green either. This matters MORE under the tight syscall
    budget than it would under a loose CPU one, because a tight budget makes
    an unrelated population change look exactly like a regression.

    No `--population-file` means the caller is gating a population whose
    membership this file cannot enumerate — the renderer workload set, which
    is guarded instead by --require-test. The guard is therefore opt-in.
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
        "_No elapsed time appears in this report, deliberately: a "
        "multi-tenant runner makes a duration a measure of the machine. It "
        "survives only as the gross smoke check below, which reports a "
        "verdict and no number._",
    ]
    if result.get("head_only"):
        lines.append(
            f"- {len(result['head_only'])} entry/entries in this report the "
            "baseline does not record, excluded from the comparison")
    if result.get("base_only"):
        lines.append(
            f"- {len(result['base_only'])} baseline entry/entries not in "
            "these head reports, excluded from the comparison")

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
            "effects move it — so one entry's percentage is a starting "
            "point, not a finding._",
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

    The full detail is already on stderr and in the step log; this is much
    terser, but it names the baseline and says what went wrong, which is the
    difference between a failure somebody can act on and one they have to go
    and reconstruct from a log.
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
    no comparison happened, and it exits 0 rather than failing a commit for
    the absence of data. It must NOT print nothing — a green gate with an
    empty summary is what this repository keeps failing reviews over — so it
    carries the counters in a form a person can paste straight in.
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


# The committed baseline document, modelled on coverage-floor.json:
# committed data rather than a literal in a workflow, a REQUIRED `basis` so
# the reason a number sits where it does lives at the number rather than in a
# pull request that will not be there when someone reads the file, a
# provenance pair, and a validator that refuses rather than degrades. A
# baseline that defaulted every missing key would be a gate reporting green
# precisely when its own configuration is broken.

# One `metric` and one `tolerance` for the whole document is what made the
# two populations collide: the unit suite wants CPU seconds because tracing
# it costs three times its untraced wall time, the renderer workloads want
# syscalls because they are the shipped product and cheap to trace, and a
# single pair of keys can only ever be right for one of them. So the
# baseline carries one sub-document per POPULATION, and the comparator is
# told which one it is judging.
REQUIRED_KEYS = ("schema", "basis", "cell", "measured_commit", "measured_at",
                 "populations")
POPULATION_KEYS = ("metric", "tolerance", "population", "entries", "wall")


def _is_number(value) -> bool:
    """A JSON number, excluding booleans — `True` is an int in Python."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def read_baseline(path: Path) -> dict:
    """The committed baseline document, validated before it is used."""
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
    """One population's sub-document, named or refused.

    There is no default. Picking a population by inference would be exactly
    the kind of quiet accommodation this gate exists to avoid: the caller
    says which contract it is being held to.
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
    """Node id -> a non-negative number, under `entries` and under `wall`."""
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
        if not _is_number(value) or value < 0:
            raise ComparisonError(
                f"{where} {key}[{node!r}]={value!r} is not a non-negative "
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
    for name, population in sorted(new.get("populations", {}).items()):
        before = old.get("populations", {}).get(name, {}).get("entries", {})
        for node, now in population.get("entries", {}).items():
            was = before.get(node)
            if was is None or now > was:
                moved[f"{name}:{node}"] = (was, now)
    return moved


def _resolve_metric(paths, label) -> str:
    """The one instrument every head report agrees on, or a refusal.

    Two rounds disagreeing means there is no single number to compare, and
    picking one of them silently is the failure this whole file is about.
    """
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

    Every population, in one pass: the workflow runs this against the merge
    base's copy of the document, and a population added by the head commit
    has no baseline there, which is a raise by definition and is reported as
    one.

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
                             "same-job comparison; mutually exclusive with "
                             "--baseline")
    parser.add_argument("--baseline", default=None, metavar="PATH",
                        help=f"the committed baseline document (e.g. "
                             f"{DEFAULT_BASELINE})")
    parser.add_argument("--head", nargs="+", metavar="XML",
                        help="this commit's JUnit report(s)")
    parser.add_argument("--max-regression", type=float, default=0.30,
                        help="fail above this fractional increase in the "
                             "total (default: 0.30 = 30%%)")
    parser.add_argument("--population", default=None, metavar="NAME",
                        help="which population of --baseline this run is "
                             "judging, e.g. unit-suite. Required with "
                             "--baseline: it is what selects that "
                             "population's own metric and tolerance")
    parser.add_argument("--tolerance", type=float, default=None,
                        help="one fractional allowance, applied to EVERY "
                             "entry in the population being compared; "
                             "defaults to that population's own "
                             "`tolerance`. The check is per entry, the "
                             "allowance is one value — that is what this "
                             "flag is, and the wording says so")
    parser.add_argument("--base-label", default="the committed baseline",
                        help="name of the baseline, for the report")
    parser.add_argument("--summary-file", default=None,
                        help="append the markdown report here as well as "
                             "printing it (e.g. $GITHUB_STEP_SUMMARY)")
    parser.add_argument("--require-test", action="append", default=[],
                        metavar="NAME",
                        help="node id that must be present and passing in "
                             "EVERY head report, e.g. e2e::bench.render. "
                             "Repeatable. Absent, or skipped/failed in any "
                             "round, is a ComparisonError (exit 2).")
    parser.add_argument("--allow-removal", action="append", default=[],
                        metavar="NAME",
                        help="node id whose disappearance from the head "
                             "reports is deliberate. Repeatable. Either flag "
                             "switches the gate to CLOSED-SET mode, where "
                             "every name that passed at the baseline must "
                             "still pass at the head unless named here.")
    parser.add_argument("--population-file", default=None, metavar="PATH",
                        help="the node ids this run COLLECTED, one per line; "
                             "their digest is checked against that "
                             "population's, and a difference is a refusal "
                             "(exit 2). Produced by --print-population")
    parser.add_argument("--print-population", default=None, metavar="DIR",
                        help="collect the node ids under DIR, print them one "
                             "per line and exit; a collection pass, not a "
                             "test run")
    parser.add_argument("--ratchet-baselines", nargs=2,
                        metavar=("OLD", "NEW"),
                        help="fail if any entry of NEW is above its value in "
                             "OLD; the down-only half of the gate, over "
                             "every population. Needs nothing else")
    args = parser.parse_args(argv)

    if args.print_population:
        try:
            collected = counter.collect_node_ids(Path(args.print_population))
        except counter.CounterError as exc:
            # The collection pass is cheap, but a tree that cannot even be
            # IMPORTED is a setup failure, and a traceback in a CI log is
            # the least actionable form of saying so.
            print(f"cannot collect the test population: {exc}",
                  file=sys.stderr)
            return 2
        print("\n".join(collected))
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
    if args.baseline and not args.population:
        parser.error("--baseline needs --population: the document carries "
                     "one metric and one tolerance per population, and "
                     "guessing which contract this run is held to is the "
                     "accommodation this gate refuses to make")

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
        baseline = read_population(read_baseline(Path(args.baseline)),
                                   args.population)
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
