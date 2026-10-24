#!/usr/bin/env python3
"""Fit raw CPU on calibration from speed-job logs (issue #240).

Unit rounds use their own calibration; renderer rounds use the SAME JOB's
unit-step calibration, because pre-#242 renderer calibrators inherited the
sitecustomize PYTHONPATH and are contaminated. Post-merge re-derivations may
use the renderer step's own reading after a clean population exists.
`--job <id>` (repeatable) fetches via `gh api`; `--from-file <log>` reads one
saved log offline, labelled file:<name> without a numeric job id. A single
job's two rounds are noise-dominated and can fit a negative slope, which is
refused; pool jobs instead. Nothing is written by this script.
Job-log API: https://docs.github.com/en/rest/actions/workflow-jobs
"""
import argparse
import json
import math
from pathlib import Path
import re
import statistics
import subprocess
import sys

REPOSITORY = "Nitjsefnie/gh-widgets"
NUMBER = r"([0-9.eE+-]+)"
UNIT = re.compile(r"counter: calibration = " + NUMBER
                  + r" cpu_time; suite raw " + NUMBER + r" cpu_time")
RENDERER = re.compile(r"(bench\.[\w-]+) \(head round (\d+)\): .*?cpu_time_ratio passed; "
                      r"calibration = " + NUMBER + r" cpu_time; renderer raw "
                      + NUMBER + r" cpu_time")
ROUND = re.compile(r"##\[group\]unit suite round (\d+)")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _api(endpoint):
    return subprocess.run(
        ["gh", "api", "--allow-escape-sequences", "-H", "Cache-Control: no-cache", endpoint],
        capture_output=True, text=True, encoding="utf-8", check=True, timeout=60).stdout


def fetch_job(job):
    """The job's run id and plain-text log, from the same API job id."""
    endpoint = f"repos/{REPOSITORY}/actions/jobs/{job}"
    metadata = json.loads(_api(endpoint))
    return str(metadata["run_id"]), _api(endpoint + "/logs")


def parse_log(text, job, run):
    """Rows with matched clean calibration, retaining their provenance."""
    units, renderers = {}, []
    current = None
    for line in ANSI.sub("", text).splitlines():
        if match := ROUND.search(line):
            current = int(match[1])
        if (match := UNIT.search(line)) and current is not None:
            units[current] = tuple(map(float, match.groups()))
        if match := RENDERER.search(line):
            renderers.append((int(match[2]), "e2e::" + match[1], float(match[4])))
    if not units:
        raise ValueError(f"job {job} has no parseable rounds")
    values = [(number, "counter::unit-suite", raw)
              for number, (_calibration, raw) in sorted(units.items())] + renderers
    rows = []
    for number, entry, raw in values:
        if number not in units:
            raise ValueError(f"job {job} round {number} has no unit-step calibration")
        calibration = units[number][0]
        if not math.isfinite(raw) or raw < 0 or not math.isfinite(calibration) or calibration <= 0:
            raise ValueError(f"job {job} round {number} has invalid CPU parts")
        rows.append({"job": str(job), "run": str(run), "round": number,
                     "entry": entry, "raw": raw, "calibration": calibration})
    return rows


def fit(rows):
    """Ordinary least-squares slopes, with an intercept, and observed counts."""
    groups = {}
    for row in rows:
        groups.setdefault(row["entry"], []).append(row)
    slopes, counts = {}, {}
    for entry, group in sorted(groups.items()):
        try:
            slope, _intercept = statistics.linear_regression(
                [row["calibration"] for row in group], [row["raw"] for row in group])
        except statistics.StatisticsError as exc:
            raise ValueError(f"{entry}: cannot fit calibration slope: {exc}") from exc
        if not math.isfinite(slope) or slope <= 0:
            raise ValueError(f"{entry}: fitted slope is not finite and positive: {slope}")
        slopes[entry], counts[entry] = slope, len(group)
    return slopes, counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--job", type=int, action="append")
    inputs.add_argument("--from-file", type=Path)
    args = parser.parse_args(argv)
    try:
        rows = []
        if args.from_file:
            text = args.from_file.read_text(encoding="utf-8")
            run = re.search(r"actions/runs/(\d+)", text)
            job = re.fullmatch(r"job-(\d+)\.log", args.from_file.name)
            rows = parse_log(text, job[1] if job else f"file:{args.from_file.name}",
                             run[1] if run else "unknown")
        else:
            for job in args.job:
                run, text = fetch_job(job)
                rows.extend(parse_log(text, job, run))
        slopes, counts = fit(rows)
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        print(f"derive speed slopes: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"slopes": slopes}, indent=2, sort_keys=True))
    print("\n| Run | Job | Round | Entry | Raw CPU | Calibration CPU |")
    print("| --- | --- | --- | --- | ---: | ---: |")
    for row in rows:
        print(f"| {row['run']} | {row['job']} | {row['round']} | {row['entry']} | "
              f"{row['raw']:.6f} | {row['calibration']:.6f} |")
    print("\n| Entry | Rounds |")
    print("| --- | ---: |")
    for entry, count in counts.items():
        print(f"| {entry} | {count} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
