#!/usr/bin/env python3
"""Choose which consolidated CI gate jobs apply to this event."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote


class DetectionError(RuntimeError):
    """Changed paths could not be classified safely."""


class Transport(Protocol):
    def api(self, path: str, *, paginate: bool = False,
            no_cache: bool = True) -> Any:
        """Read a GitHub REST resource."""


CODE_IGNORES = (
    "**/*.md", "PRESENTATION.txt", "docs/**", "examples/**", "LICENSE",
    ".gitignore",
)
CODEQL_IGNORES = (
    "**/*.md", "docs/**", "examples/**", ".claude/**", "LICENSE",
    "NOTICE", ".gitignore",
)
GATES = {
    "tests": ("deny", CODE_IGNORES),
    "lint": ("deny", CODE_IGNORES),
    "types": ("deny", CODE_IGNORES),
    "audit": ("deny", CODE_IGNORES),
    "speed": ("deny", CODE_IGNORES),
    "codeql": ("deny", CODEQL_IGNORES),
    "actionlint": (
        "allow", (".github/workflows/**", ".github/dependabot.yml",
                  "requirements-zizmor.txt")),
}
ALL_GATES = tuple(GATES)
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
AUDIT_CRON = "12 4 * * *"
CODEQL_CRON = "47 3 * * 3"


def github_path_matches(pattern: str, path: str) -> bool:
    """Match the case-sensitive Actions glob subset used by these gates."""
    pieces = []
    index = 0
    while index < len(pattern):
        if pattern[index:index + 2] == "**":
            index += 2
            if pattern[index:index + 1] == "/":
                pieces.append("(?:.*/)?")
                index += 1
            else:
                pieces.append(".*")
        elif pattern[index] == "*":
            pieces.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            pieces.append("[^/]")
            index += 1
        else:
            pieces.append(re.escape(pattern[index]))
            index += 1
    return re.compile("".join(pieces), re.DOTALL).fullmatch(path) is not None


def classify(changed: set[str], *, capped: bool = False) -> dict[str, str]:
    """Return run/skip for each gate, conservatively handling a capped diff."""
    decisions = {}
    for name, (kind, patterns) in GATES.items():
        if capped:
            required = True
        elif kind == "deny":
            required = not all(
                any(github_path_matches(pattern, path) for pattern in patterns)
                for path in changed)
        elif kind == "allow":
            required = any(
                github_path_matches(pattern, path)
                for pattern in patterns for path in changed)
        else:
            raise DetectionError(f"unknown filter kind for {name}: {kind}")
        decisions[name] = "run" if required else "skip"
    return decisions


def _list_items(data: Any) -> list[dict]:
    """Flatten gh's slurped pages and validate the returned item shape."""
    if isinstance(data, dict):
        data = data.get("workflow_runs")
    if isinstance(data, list):
        if all(isinstance(page, list) for page in data):
            data = [item for page in data for item in page]
        elif all(isinstance(page, dict) and "workflow_runs" in page
                 for page in data):
            data = [item for page in data
                    for item in page.get("workflow_runs", [])]
    if (not isinstance(data, list)
            or not all(isinstance(item, dict) for item in data)):
        raise DetectionError("API list response is missing or malformed")
    return data


class GhTransport:
    """Use gh with explicit no-cache headers and complete list pagination."""

    def __init__(self, environment: dict[str, str] | None = None):
        self.environment = environment

    def api(self, path: str, *, paginate: bool = False,
            no_cache: bool = True) -> Any:
        command = [
            "gh", "api", "--method", "GET",
            "-H", "Accept: application/vnd.github+json",
        ]
        if no_cache:
            command += ["-H", "Cache-Control: no-cache"]
        if paginate:
            command += ["--paginate", "--slurp"]
        command.append(path)
        try:
            response = subprocess.run(
                command, check=True, capture_output=True, text=True,
                timeout=60, env=self.environment)
            data = json.loads(response.stdout)
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or str(exc)).strip()
            raise DetectionError(f"gh api {path}: {detail}") from exc
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise DetectionError(f"gh api {path}: {exc}") from exc
        if not paginate:
            return data
        if not isinstance(data, list):
            raise DetectionError(f"gh api {path}: missing paginated JSON pages")
        return _list_items(data)


def _api(transport: Transport, path: str, *, paginate: bool = False) -> Any:
    try:
        return transport.api(path, paginate=paginate, no_cache=True)
    except DetectionError:
        raise
    except Exception as exc:  # pylint: disable=broad-exception-caught
        raise DetectionError(f"API request failed for {path}: {exc}") from exc


def _filenames(data: Any) -> set[str]:
    """Validate changed-file items, retaining both paths for a rename."""
    if not isinstance(data, list):
        raise DetectionError("changed-file response has no files list")
    names = set()
    for item in data:
        if (not isinstance(item, dict)
                or not isinstance(item.get("filename"), str)
                or not item["filename"]):
            raise DetectionError("changed-file response has a missing filename")
        names.add(item["filename"])
        if item.get("status") == "renamed":
            previous = item.get("previous_filename")
            if not isinstance(previous, str) or not previous:
                raise DetectionError("renamed file has no previous_filename")
            names.add(previous)
    return names


def _sha(value: Any) -> str:
    if not isinstance(value, str) or SHA.fullmatch(value) is None:
        raise DetectionError(f"missing or invalid commit SHA: {value!r}")
    return value


def changed_files(transport: Transport, repository: str, *, event: str,
                  sha: str, pr_number: str = "", default_branch: str = "",
                  payload: dict | None = None) -> tuple[set[str], bool]:
    """Read the event's changed paths and report whether the diff hit its cap."""
    if REPOSITORY.fullmatch(repository) is None:
        raise DetectionError("missing or invalid REPOSITORY")
    if event == "pull_request":
        number = pr_number
        if not number:
            pull_request = (payload or {}).get("pull_request")
            number = (str(pull_request.get("number", ""))
                      if isinstance(pull_request, dict) else "")
        if not number.isascii() or not number.isdigit() or int(number) < 1:
            raise DetectionError("pull_request has no valid PR_NUMBER")
        files = _list_items(_api(
            transport,
            f"repos/{repository}/pulls/{number}/files?per_page=100",
            paginate=True))
        return _filenames(files), len(files) >= 300
    if event != "push":
        raise DetectionError(f"cannot acquire changed files for event {event!r}")
    payload = payload or {}
    before, after = _sha(payload.get("before")), _sha(payload.get("after"))
    if after != _sha(sha):
        raise DetectionError("push event after does not match HEAD_SHA")
    return _compare_files(transport, repository, before, after, default_branch)


def _compare_files(transport: Transport, repository: str, before: str,
                   after: str, default_branch: str) -> tuple[set[str], bool]:
    if before == "0" * 40:
        if not default_branch:
            raise DetectionError("new-branch push has no default branch to compare")
        before = quote(default_branch, safe="")
    comparison = _api(
        transport, f"repos/{repository}/compare/{before}...{after}")
    if isinstance(comparison, dict) and comparison.get("status") == "diverged":
        if not default_branch:
            raise DetectionError("divergent push has no default branch to compare")
        baseline = quote(default_branch, safe="")
        if before != baseline:
            comparison = _api(
                transport, f"repos/{repository}/compare/{baseline}...{after}")
    if (not isinstance(comparison, dict)
            or comparison.get("status") not in ("ahead", "identical")):
        raise DetectionError(
            "push comparison is divergent, behind, or has an unknown status")
    files = comparison.get("files")
    if not isinstance(files, list):
        raise DetectionError("push comparison has no changed-file list")
    return _filenames(files), len(files) >= 300


def _tag_ref(payload: dict) -> str | None:
    ref = payload.get("ref")
    return (ref if isinstance(ref, str) and ref.startswith("refs/tags/")
            else None)


def classify_event(event: str, *, transport: Transport, repository: str,
                   payload: dict, schedule: str = "", sha: str = "",
                   pr_number: str = "", default_branch: str = "") -> dict[str, str]:
    """Classify one Actions event using its changed paths or schedule."""
    if event == "push" and _tag_ref(payload):
        return {name: "run" for name in GATES}
    if event == "workflow_dispatch":
        return {name: "run" for name in GATES}
    if event == "schedule":
        active = {
            "audit": schedule == AUDIT_CRON,
            "codeql": schedule == CODEQL_CRON,
        }
        return {name: "run" if active.get(name, False) else "skip"
                for name in GATES}
    changed, capped = changed_files(
        transport, repository, event=event, sha=sha,
        pr_number=pr_number, default_branch=default_branch, payload=payload)
    return classify(changed, capped=capped)


def _all_run(reason: str) -> tuple[dict[str, str], str]:
    return {name: "run" for name in GATES}, reason


def _reason(event: str, gate: str, decision: str, *, changed: set[str] | None,
            capped: bool, fallback: str) -> str:
    if fallback:
        reason = fallback
    elif event == "workflow_dispatch":
        reason = "manual dispatch runs every gate"
    elif event == "schedule" and decision == "run":
        cron = AUDIT_CRON if gate == "audit" else CODEQL_CRON
        reason = f"schedule matches {cron}"
    elif event == "schedule":
        reason = "gate is not scheduled for this event"
    elif capped:
        reason = "changed-file response reached the 300-file cap; running all gates"
    elif decision == "skip" and gate == "actionlint":
        reason = ("no changed path matches .github/workflows/**, "
                  ".github/dependabot.yml, or requirements-zizmor.txt")
    elif decision == "skip":
        reason = "every changed path is denied by this gate's path rules"
    else:
        paths = sorted(changed or ())
        reason = (f"changed path requires this gate: {paths[0]}" if paths
                  else "changed paths could not justify a skip; running this gate")
    return reason


def _payload(event: str, path: str) -> dict:
    if event not in {"push", "pull_request"}:
        return {}
    if not path:
        raise DetectionError(f"{event} has no EVENT_PATH")
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DetectionError(f"cannot read event payload: {exc}") from exc
    if not isinstance(payload, dict):
        raise DetectionError("event payload is not a JSON object")
    return payload


def _detect(environment: dict[str, str],
            transport: Transport | None) -> tuple[str, dict[str, str], set[str] | None,
                                                  bool, str]:
    event = environment.get("EVENT_NAME", "")
    changed = None
    capped = False
    fallback = ""
    try:
        repository = environment.get("REPOSITORY", "")
        payload = _payload(event, environment.get("EVENT_PATH", ""))
        api = transport or GhTransport(environment)
        if event in {"push", "pull_request"}:
            tag_ref = _tag_ref(payload) if event == "push" else None
            if tag_ref:
                decisions = classify_event(
                    event, transport=api, repository=repository, payload=payload)
                fallback = f"tag push {tag_ref} runs all seven gates"
            else:
                changed, capped = changed_files(
                    api, repository, event=event,
                    sha=environment.get("HEAD_SHA", ""),
                    pr_number=environment.get("PR_NUMBER", ""),
                    default_branch=environment.get("DEFAULT_BRANCH", ""),
                    payload=payload)
                decisions = classify(changed, capped=capped)
        else:
            decisions = classify_event(
                event, transport=api, repository=repository, payload=payload,
                schedule=environment.get("SCHEDULE", ""),
                sha=environment.get("HEAD_SHA", ""),
                pr_number=environment.get("PR_NUMBER", ""),
                default_branch=environment.get("DEFAULT_BRANCH", ""))
    except Exception as exc:  # pylint: disable=broad-exception-caught
        decisions, fallback = _all_run(
            f"changed-path detection failed ({exc}); running every gate")
    return event, decisions, changed, capped, fallback


def _write_outputs(decisions: dict[str, str], output_path: str) -> int:
    lines = [f"{name}={decision}" for name, decision in decisions.items()]
    if not output_path:
        print("\n".join(lines))
        return 0
    try:
        with open(output_path, "a", encoding="utf-8") as output:
            output.write("\n".join(lines) + "\n")
    except OSError as exc:
        print(f"cannot write GITHUB_OUTPUT: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None, *, transport: Transport | None = None,
         environ: dict[str, str] | None = None) -> int:
    del argv
    environment = dict(os.environ if environ is None else environ)
    event, decisions, changed, capped, fallback = _detect(environment, transport)
    for name, decision in decisions.items():
        reason = _reason(event, name, decision, changed=changed,
                         capped=capped, fallback=fallback)
        print(f"{name}: {decision} — {reason}")
    return _write_outputs(decisions, environment.get("GITHUB_OUTPUT", ""))


if __name__ == "__main__":
    sys.exit(main())
