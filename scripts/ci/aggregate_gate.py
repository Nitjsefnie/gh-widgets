#!/usr/bin/env python3
"""Evaluate the needs graph for the consolidated CI workflow."""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.ci import changes_detect as cd


WORKFLOW = ".github/workflows/tests.yml"
WORKFLOW_FILE = Path(WORKFLOW).name
STRICT = frozenset({"changes"})
ALLOWED = frozenset({"success", "skipped"})
CANCELLED = "cancelled"
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
RUN_ID = re.compile(r"[0-9]+\Z")

PASSED = "PASSED"
FAILED = "FAILED"
SKIPPED = "SKIPPED"
SUPERSEDED = "SUPERSEDED"
GREEN = frozenset({PASSED, SKIPPED, SUPERSEDED})

GATE_JOBS = {
    "tests": "unittest",
    "lint": "lint",
    "types": "pyright",
    "audit": "pip-audit",
    "speed": "speed",
    "codeql": "analyze",
    "actionlint": "actionlint",
}
EXPECTED_NEEDS = frozenset({"changes", *GATE_JOBS.values()})


class AggregationError(RuntimeError):
    """The aggregate cannot safely determine a verdict."""


class QueryError(AggregationError):
    """The runs API could not prove a cancelled need was superseded."""


@dataclass(frozen=True)
class Result:
    """One gate's terminal summary row."""

    verdict: str
    detail: str


def allowed_results(name: str) -> frozenset[str]:
    """The changes selector must succeed; gates may be skipped by design."""
    return frozenset({"success"}) if name in STRICT else ALLOWED


def classify_needs(needs: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Separate hard need failures from cancellations requiring evidence."""
    if not isinstance(needs, dict):
        raise AggregationError("NEEDS_JSON must be an object")
    hard = []
    cancelled = []
    for name, details in needs.items():
        result = details.get("result") if isinstance(details, dict) else None
        if result in allowed_results(name):
            continue
        if result == CANCELLED:
            cancelled.append(name)
        else:
            hard.append(name)
    return hard, cancelled


def _workflow_of(run: dict[str, Any]) -> Any:
    return run.get("workflow_id") or run.get("path")


def _started_key(run: dict[str, Any]) -> tuple[datetime, int] | None:
    """Use a valid start/create time, with run id breaking equal-time ties."""
    text = run.get("run_started_at") or run.get("created_at")
    if not isinstance(text, str) or not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp, _run_id(run)


def _run_id(run: dict[str, Any]) -> int:
    identifier = run.get("id")
    if (not isinstance(identifier, int) or isinstance(identifier, bool)
            or identifier < 1):
        raise QueryError("workflow run has no valid id")
    return identifier


def _pull_request_numbers(run: dict[str, Any]) -> set[int] | None:
    """Read the run API's PR associations, or return None when omitted."""
    associations = run.get("pull_requests", [])
    if not isinstance(associations, list):
        raise QueryError("workflow run pull_requests is not a list")
    numbers: set[int] = set()
    for association in associations:
        if not isinstance(association, dict):
            raise QueryError("workflow run has a malformed pull_requests entry")
        number = association.get("number")
        if (not isinstance(number, int) or isinstance(number, bool)
                or number < 1):
            raise QueryError(
                "workflow run pull_requests entry has no valid integer number")
        numbers.add(number)
    return numbers if numbers else None


def _attributable(run: dict[str, Any], pr_number: int) -> bool:
    """Port the former aggregate's association-number membership check."""
    numbers = _pull_request_numbers(run)
    if numbers is None:
        return True
    known_numbers: set[int] = set(numbers)
    return pr_number in known_numbers


def _head_repository_identity(run: dict[str, Any]) -> tuple[str, int | str] | None:
    """Return a stable identity for the run's source repository when present."""
    repository = run.get("head_repository")
    if not isinstance(repository, dict):
        return None
    identifier = repository.get("id")
    if (isinstance(identifier, int) and not isinstance(identifier, bool)
            and identifier > 0):
        return "id", identifier
    full_name = repository.get("full_name")
    if isinstance(full_name, str) and full_name.strip():
        return "full_name", full_name.casefold()
    return None


def _same_pr_domain(run: dict[str, Any], *, workflow: str, branch: str,
                    repository: tuple[str, int | str],
                    pr_numbers: set[int] | None) -> bool:
    """Check whether a candidate shares the current PR's concurrency key."""
    if (run.get("event") != "pull_request"
            or _workflow_of(run) != workflow
            or _head_repository_identity(run) != repository):
        return False
    candidate_numbers = _pull_request_numbers(run)
    if pr_numbers is None:
        return candidate_numbers is None and run.get("head_branch") == branch
    if candidate_numbers is None:
        return False
    return any(_attributable(run, number) for number in pr_numbers)


def superseding_run(mine: dict[str, Any], runs: list[dict[str, Any]],
                    branch: str) -> dict[str, Any] | None:
    """Find a newer run that shares this PR's actual concurrency domain."""
    event = mine.get("event")
    if not isinstance(event, str) or not event:
        raise QueryError("current run has no event identity")
    if event != "pull_request":
        return None
    if not isinstance(branch, str) or not branch:
        raise QueryError("supersession query has no head branch")
    if mine.get("head_branch") != branch:
        raise QueryError("current run does not match the requested head branch")
    workflow = _workflow_of(mine)
    if workflow is None:
        raise QueryError("current run has no workflow identity")
    repository = _head_repository_identity(mine)
    if repository is None:
        return None
    pr_numbers = _pull_request_numbers(mine)
    mine_key = _started_key(mine)
    if mine_key is None:
        raise QueryError("current workflow run has no valid start or creation time")
    newer: list[tuple[tuple[datetime, int], dict[str, Any]]] = []
    for run in runs:
        if not isinstance(run, dict):
            raise QueryError("workflow run list has a malformed item")
        if not _same_pr_domain(
                run, workflow=workflow, branch=branch,
                repository=repository, pr_numbers=pr_numbers):
            continue
        run_key = _started_key(run)
        if run_key is not None and run_key > mine_key:
            newer.append((run_key, run))
    if not newer:
        return None
    return max(newer, key=lambda item: item[0])[1]


def _gate_result(gate: str, job: str, result: Any, required: str, *,
                 hard_jobs: set[str], cancelled_jobs: set[str],
                 superseded_by: dict[str, Any] | None) -> Result:
    if job in cancelled_jobs:
        if superseded_by is None:
            row = Result(
                FAILED,
                f"no qualifying same-PR successor proves auto-cancellation; "
                f"deliberate cancel: {job}=cancelled")
        else:
            detail = (
                f"superseded by run {_run_id(superseded_by)} "
                f"({superseded_by.get('html_url') or '?'})")
            if not superseded_by.get("pull_requests"):
                detail += (
                    "; proof degraded: pull_requests association absent; "
                    "same head repository and head branch matched; two PRs "
                    "from one fork branch are indistinguishable in the runs API")
            row = Result(
                SUPERSEDED,
                detail)
    elif job in hard_jobs:
        row = Result(FAILED, f"{job} has unknown or failing result {result!r}")
    elif result == "skipped" and required == "run":
        row = Result(
            FAILED, f"{job} was skipped although changed paths require {gate}")
    elif result == "success" and required == "skip":
        row = Result(
            FAILED, f"{job} succeeded although changed paths skip {gate}")
    elif result == "skipped":
        row = Result(SKIPPED, "changed paths classify this gate as not required")
    else:
        row = Result(PASSED, "job succeeded and changed paths require this gate")
    return row


def _api(transport: cd.Transport, path: str, *,
         paginate: bool = False) -> Any:
    try:
        return transport.api(path, paginate=paginate, no_cache=True)
    except AggregationError:
        raise
    except Exception as exc:  # pylint: disable=broad-exception-caught
        raise QueryError(f"API request failed for {path}: {exc}") from exc


def _prove_superseded(transport: cd.Transport, repository: str, run_id: str,
                      branch: str) -> dict[str, Any] | None:
    if REPOSITORY.fullmatch(repository) is None:
        raise QueryError("missing or invalid REPOSITORY for supersession query")
    if not RUN_ID.fullmatch(run_id) or int(run_id) < 1:
        raise QueryError("missing or invalid RUN_ID for supersession query")
    own_path = f"repos/{repository}/actions/runs/{run_id}"
    mine = _api(transport, own_path)
    if not isinstance(mine, dict) or _run_id(mine) != int(run_id):
        raise QueryError("current workflow run response is missing or malformed")
    event = mine.get("event")
    if event in {"push", "workflow_dispatch"}:
        return None
    if event != "pull_request":
        raise QueryError("current workflow run has no supported event identity")
    list_path = (
        f"repos/{repository}/actions/workflows/{WORKFLOW_FILE}/runs"
        f"?branch={quote(branch, safe='')}&per_page=100")
    runs = _api(transport, list_path, paginate=True)
    if not isinstance(runs, list):
        raise QueryError("workflow runs response is missing or malformed")
    return superseding_run(mine, runs, branch)


def decide(needs: dict[str, Any], applicability: dict[str, str], *,
           superseded_by: dict[str, Any] | None = None) -> dict[str, Result]:
    """Classify needs and cross-check their terminal state against paths."""
    if not isinstance(needs, dict):
        raise AggregationError("NEEDS_JSON must be an object")
    missing = EXPECTED_NEEDS - set(needs)
    extra = set(needs) - EXPECTED_NEEDS
    if missing or extra:
        raise AggregationError(
            f"needs inventory mismatch; missing={sorted(missing)}, "
            f"unexpected={sorted(extra)}")
    if not isinstance(applicability, dict) or set(applicability) != set(GATE_JOBS):
        raise AggregationError("gate applicability is incomplete or unexpected")
    if any(value not in {"run", "skip"} for value in applicability.values()):
        raise AggregationError("gate applicability contains an unknown value")

    changes = needs["changes"].get("result")
    if changes != "success":
        raise AggregationError(f"strict dependency changes={changes!r}; must succeed")

    hard, cancelled = classify_needs(needs)
    hard_jobs = set(hard)
    cancelled_jobs = set(cancelled)
    return {
        gate: _gate_result(
            gate, job, needs[job].get("result"), applicability[gate],
            hard_jobs=hard_jobs, cancelled_jobs=cancelled_jobs,
            superseded_by=superseded_by)
        for gate, job in GATE_JOBS.items()
    }


def evaluate(needs: dict[str, Any], applicability: dict[str, str], *,
             repository: str, run_id: str, branch: str,
             transport: cd.Transport) -> dict[str, Result]:
    """Prove any cancelled gates superseded, then return the gate rows."""
    _, cancelled = classify_needs(needs)
    if needs.get("changes", {}).get("result") != "success":
        return decide(needs, applicability)
    gate_cancellations = [name for name in cancelled if name != "changes"]
    if not gate_cancellations:
        return decide(needs, applicability)
    newer = _prove_superseded(transport, repository, run_id, branch)
    return decide(needs, applicability, superseded_by=newer)


def exit_code(results: dict[str, Result]) -> int:
    if set(results) != set(GATE_JOBS):
        raise AggregationError("aggregate results are incomplete")
    if any(row.verdict not in GREEN | {FAILED} for row in results.values()):
        raise AggregationError("aggregate results contain an unknown verdict")
    return int(any(row.verdict not in GREEN for row in results.values()))


def render_summary(results: dict[str, Result]) -> str:
    def cell(value: str) -> str:
        return value.replace("|", "\\|").replace("\r", " ").replace("\n", " ")

    lines = [
        "### Aggregate CI gates", "", "| Gate | Result | Detail |",
        "| --- | --- | --- |",
    ]
    lines.extend(
        f"| {cell(name)} | {row.verdict} | {cell(row.detail)} |"
        for name, row in results.items())
    return "\n".join(lines) + "\n"


def _payload(event: str, path: str) -> dict:
    if event not in {"push", "pull_request"}:
        return {}
    if not path:
        raise AggregationError(f"{event} has no EVENT_PATH")
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AggregationError(f"cannot read event payload: {exc}") from exc
    if not isinstance(payload, dict):
        raise AggregationError("event payload is not a JSON object")
    return payload


def _context_results(environment: dict[str, str],
                     transport: cd.Transport) -> dict[str, Result]:
    try:
        needs = json.loads(environment["NEEDS_JSON"])
    except (KeyError, ValueError) as exc:
        raise AggregationError("NEEDS_JSON is missing or not JSON") from exc
    event = environment.get("EVENT_NAME", "")
    payload = _payload(event, environment.get("EVENT_PATH", ""))
    applicability = cd.classify_event(
        event, transport=transport, repository=environment.get("REPOSITORY", ""),
        payload=payload, sha=environment.get("HEAD_SHA", ""),
        pr_number=environment.get("PR_NUMBER", ""),
        default_branch=environment.get("DEFAULT_BRANCH", ""))
    return evaluate(
        needs, applicability, repository=environment.get("REPOSITORY", ""),
        run_id=environment.get("RUN_ID", ""),
        branch=environment.get("HEAD_BRANCH", ""), transport=transport)


def _failed_rows(reason: str) -> dict[str, Result]:
    return {name: Result(FAILED, f"aggregate could not determine verdict: {reason}")
            for name in GATE_JOBS}


def _write_summary(report: str, path: str) -> bool:
    if not path:
        return True
    try:
        with open(path, "a", encoding="utf-8") as summary:
            summary.write(report)
    except OSError as exc:
        print(f"cannot write GITHUB_STEP_SUMMARY: {exc}", file=sys.stderr)
        return False
    return True


def main(argv: list[str] | None = None, *, transport: cd.Transport | None = None,
         environ: dict[str, str] | None = None) -> int:
    del argv
    environment = dict(os.environ if environ is None else environ)
    api = transport or cd.GhTransport(environment)
    try:
        results = _context_results(environment, api)
        status = exit_code(results)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        print(f"cannot aggregate: {exc}", file=sys.stderr)
        results = _failed_rows(str(exc))
        status = 1
    report = render_summary(results)
    print(report, end="")
    if not _write_summary(report, environment.get("GITHUB_STEP_SUMMARY", "")):
        return 1
    return status


if __name__ == "__main__":
    sys.exit(main())
