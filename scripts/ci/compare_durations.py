#!/usr/bin/env python3
"""Compare per-test durations between two builds of the suite.

Answers one question: did the tests that exist in BOTH builds get slower?

WHY NOT TOTAL WALL TIME. The obvious metric — how long did the suite take
— punishes the wrong thing. Add twenty tests and the total climbs, the
gate goes red, and nothing regressed. Delete a slow test and the total
drops, hiding a genuine regression somewhere else. So this intersects on
test node id and compares only the tests present, and passing, in both.
Adding or removing tests cannot move the number.

WHY MINIMUM ACROSS ROUNDS. A CI runner's speed varies by a factor of two
between jobs, and a test's duration is a floor plus noise: contention,
page-cache state, a neighbouring container. The minimum over repeated
rounds estimates that floor. The mean does not — it tracks the noise.

WHY THIS IS COMPARABLE AT ALL. Both builds run back to back on the SAME
runner, in the same job. That is what makes a percentage meaningful here;
comparing a duration from one runner against a duration recorded on
another would measure the runners.

WHY A CLOSED POPULATION IS OPTIONAL, AND WHY IT EXISTS HERE. The
intersection above is deliberately permissive: adding a test or deleting a
test cannot move the number, and that is right for a suite whose membership
changes by design. It is wrong for a workload set that IS the thing being
gated. If a shipped renderer is renamed or its checkout is incomplete, the
intersection quietly compares one fewer program, the total drops or holds,
and the gate reports a healthy speedup while measuring less than it claims —
a gate that measures fewer programs than it advertises is decorative.
--require-test and --allow-removal turn that off for a named population:
each required name must be present and passing in EVERY head round, and in
that closed-set mode every baseline name that vanished must be accounted
for by --allow-removal. With neither flag the behaviour is exactly what it
has always been, which is why the unit-suite comparison stays flagless.

EXIT CODES. 0 within budget, 1 slower than the budget, 2 could not compare
at all. Those are deliberately different: a workflow that reports a
restructured suite as a performance regression teaches people to read the
red as noise, and a ComparisonError therefore keeps the code it has always
had.

Input is pytest's own --junitxml, which carries an exact per-testcase
time and needs no plugin.

    compare_durations.py --base base1.xml base2.xml \\
                         --head head1.xml head2.xml \\
                         --max-regression 0.30 \\
                         --require-test e2e::bench.render
"""
from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


# A testcase carrying any of these children did not pass, and its duration
# is not comparable: a failure can be fast (an early assert) or slow (a
# timeout), and either way it is measuring the wrong thing.
NOT_PASSED = ("failure", "error", "skipped")


class ComparisonError(RuntimeError):
    """The comparison could not be made at all."""


def node_id(case: ET.Element) -> str:
    """`tests.test_api::test_thing`, stable across runs and machines."""
    classname = case.get("classname", "")
    name = case.get("name", "")
    return f"{classname}::{name}" if classname else name


def scan_junit(path: Path) -> tuple:
    """One report, read three ways: (passing durations, present, not_passed).

    The intersection this comparator works on throws away everything that
    did not pass, which is right for the arithmetic and useless for the
    diagnosis: when a gated workload goes missing, "it is not in the
    intersection" and "it is in the report but failed" are different bugs
    with different fixes, and the reader of a red gate needs to be told
    which one happened. So the scan keeps both raw sets.

    `present` is every node id in the report. `not_passed` is every node id
    that produced no usable passing duration — a failure, an error, a skip,
    or a testcase with no `time` attribute at all. Anything in
    `present - not_passed` is in the first element, so the folded duration
    dict is exactly the first element of every scan intersected.
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
    """{node_id: seconds} for the passing testcases in one report."""
    return scan_junit(path)[0]


def fold_scans(scans: list) -> dict:
    """The minimum duration per test across already-scanned rounds."""
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
    """Per test, the MINIMUM duration across rounds.

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
    failing = [n for n in rounds if n not in absent]
    return (f"- `{name}`: absent from head round(s) {_rounds(absent)}, and "
            f"did not pass in round(s) {_rounds(failing)}")


def _rounds(numbers):
    return ", ".join(str(n) for n in numbers) if numbers else "none"


def verify_population(head_scans, base_folded, require, allow_removal):
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

    Presence is decided on the folded head dict — what would actually be
    compared — while the diagnosis reads the scans, which still know which
    rounds the name was missing from.
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

    head_folded = fold_scans(head_scans)
    problems = [
        _diagnose_required(name, head_scans)
        for name in sorted(require) if name not in head_folded
    ]
    unexplained = sorted(set(base_folded) - set(head_folded)
                         - set(allow_removal))
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
            "declares, so a verdict over the remaining tests would be "
            "decorative:\n" + "\n".join(problems))


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
            "baseline total is zero — the reports carry no usable timings"
        )

    per_test = []
    for nid in shared:
        was, now = base[nid], head[nid]
        # Tests in the millisecond range are dominated by fixture and
        # collection overhead; a 300% "regression" on a 2 ms test is
        # noise and would bury the real entries in the table. They still
        # count in the totals, which is where they belong.
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


def render(result: dict, threshold: float, base_label: str) -> str:
    delta = result["ratio"] - 1.0
    verdict = "🔴 REGRESSION" if delta > threshold else "🟢 within budget"
    lines = [
        "### Test speed vs "
        f"`{base_label}`",
        "",
        f"**{verdict}** — {delta:+.1%} "
        f"(budget {threshold:+.0%})",
        "",
        f"- Compared on **{result['shared']}** tests passing in both builds",
        f"- Baseline `{base_label}`: **{result['base_total']:.2f}s**",
        f"- This commit: **{result['head_total']:.2f}s**",
    ]
    if result["head_only"]:
        lines.append(
            f"- {len(result['head_only'])} test(s) new since `{base_label}`, "
            "excluded from the comparison"
        )
    if result["base_only"]:
        lines.append(
            f"- {len(result['base_only'])} test(s) removed since "
            f"`{base_label}`, excluded from the comparison"
        )

    movers = [row for row in result["per_test"] if abs(row[1]) >= 0.10][:15]
    if movers:
        lines += [
            "",
            "<details><summary>Largest per-test movements "
            "(individually noisy — read the total, not these)</summary>",
            "",
            "| Test | Was | Now | Change |",
            "| --- | ---: | ---: | ---: |",
        ]
        for _abs_delta, rel, nid, was, now in movers:
            lines.append(
                f"| `{nid}` | {was:.3f}s | {now:.3f}s | {rel:+.0%} |"
            )
        lines += [
            "",
            "_**A row here is not a regression.** Two runs of identical "
            "code on one machine routinely differ by 100% or more on a "
            "single test, while the total moves by under 2% — that is why "
            "the gate is the total across all shared tests and not any "
            "individual row. These are ordered by absolute seconds gained "
            "and are useful only as a starting point once the TOTAL has "
            "already gone red._",
            "",
            "_Tests under 50 ms are omitted — at that scale the number is "
            "fixture overhead, not the test. They still count in the "
            "totals above._",
            "",
            "</details>",
        ]
    return "\n".join(lines) + "\n"


def render_failure(base_label: str, message: str) -> str:
    """A job summary for the ComparisonError path.

    Today the error goes to stderr and the step summary stays empty, so a
    red gate is a red box with nothing in it. This is deliberately much
    terser than render()'s report — the full detail is already on stderr and
    in the step log — but it names the baseline and says what went wrong,
    which is the difference between a failure somebody can act on and one
    they have to go and reconstruct from a log.
    """
    first = message.splitlines()[0] if message else "no reason given"
    return "\n".join([
        f"### Test speed vs `{base_label}`",
        "",
        f"**🔴 COULD NOT COMPARE** — {first}",
        "",
        "```",
        message,
        "```",
        "",
    ])


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--base", nargs="+", required=True,
                        metavar="XML", help="baseline JUnit report(s)")
    parser.add_argument("--head", nargs="+", required=True,
                        metavar="XML", help="this commit's JUnit report(s)")
    parser.add_argument("--max-regression", type=float, default=0.30,
                        help="fail above this fractional slowdown "
                             "(default: 0.30 = 30%%)")
    parser.add_argument("--base-label", default="baseline",
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
    args = parser.parse_args()

    try:
        base_folded = fold_rounds(args.base)
        head_scans = [scan_junit(Path(p)) for p in args.head]
        head_folded = fold_scans(head_scans)
        verify_population(head_scans, base_folded, args.require_test,
                          args.allow_removal)
        result = compare(base_folded, head_folded)
    except ComparisonError as exc:
        print(f"cannot compare: {exc}", file=sys.stderr)
        if args.summary_file:
            failure = render_failure(args.base_label, str(exc))
            with open(args.summary_file, "a", encoding="utf-8") as handle:
                handle.write(failure)
        return 2

    report = render(result, args.max_regression, args.base_label)
    print(report)
    if args.summary_file:
        with open(args.summary_file, "a", encoding="utf-8") as handle:
            handle.write(report)

    delta = result["ratio"] - 1.0
    if delta > args.max_regression:
        print(
            f"FAIL: the shared tests are {delta:+.1%} slower than "
            f"{args.base_label}, over the {args.max_regression:+.0%} budget.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
