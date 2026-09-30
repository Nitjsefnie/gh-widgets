#!/usr/bin/env python3
"""Build deterministic offline API payloads, caches, and local git mirrors."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path


USER = "bench-user"
ORG = "bench-org"
AUTHOR_EMAIL = "bench-user@users.noreply.github.com"
FUTURE = "9998-01-01T00:00:00Z"
REPOSITORIES = (
    ("outside-owner-a", "project-alpha"),
    ("outside-owner-b", "project-bravo"),
    ("outside-owner-c", "project-charlie"),
    ("outside-owner-d", "project-delta"),
)
COMMIT_DATES = (
    "2001-01-01T00:00:00+00:00",
    "2001-01-02T00:00:00+00:00",
    "2001-01-03T00:00:00+00:00",
)
LINE_TEMPLATES = (
    "VALUE_{file}_{line} = {file} + {line}",
    "def record_{file}_{line}(): return '{repo}-{file}-{line}'",
    "class Entry_{file}_{line}: marker = '{repo}'",
    "label_{file}_{line} = 'fixture {repo} line {line}'",
)


def _run_git(repo, *args, env=None):
    child_env = os.environ.copy()
    if env:
        child_env.update(env)
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                   env=child_env, text=True)


def _commit(repo, message, index):
    author = {
        "GIT_AUTHOR_NAME": USER,
        "GIT_AUTHOR_EMAIL": AUTHOR_EMAIL,
        "GIT_COMMITTER_NAME": USER,
        "GIT_COMMITTER_EMAIL": AUTHOR_EMAIL,
        "GIT_AUTHOR_DATE": COMMIT_DATES[index],
        "GIT_COMMITTER_DATE": COMMIT_DATES[index],
    }
    _run_git(repo, "add", "--all", env=author)
    _run_git(repo, "commit", "--quiet", "--no-gpg-sign", "-m", message,
             env=author)


def _repo_file_text(repo_name, file_index):
    template = LINE_TEMPLATES[file_index % len(LINE_TEMPLATES)]
    return "".join(
        template.format(file=file_index, line=line, repo=repo_name) + "\n"
        for line in range(60))


def _create_mirror(path, owner, name):
    path.mkdir(parents=True)
    _run_git(path, "init", "--quiet", "--initial-branch=main")
    for file_index in range(120):
        content = _repo_file_text(name, file_index)
        (path / f"src-{file_index:03d}.py").write_text(
            content, encoding="utf-8")
    _commit(path, "create deterministic renderer benchmark tree", 0)

    for file_index in range(0, 120, 3):
        target = path / f"src-{file_index:03d}.py"
        with target.open("a", encoding="utf-8") as handle:
            handle.write(f"revision_two_{file_index} = '{owner}-{name}'\n")
    _commit(path, "update first deterministic file slice", 1)

    for file_index in range(1, 120, 3):
        target = path / f"src-{file_index:03d}.py"
        with target.open("a", encoding="utf-8") as handle:
            handle.write(f"revision_three_{file_index} = '{owner}-{name}'\n")
    _commit(path, "update second deterministic file slice", 2)

    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True).stdout.strip()


def _repo_node(owner, name, index):
    full_name = f"{owner}/{name}"
    return {
        "id": f"REPO_{index}",
        "name": name,
        "nameWithOwner": full_name,
        "url": f"https://github.com/{full_name}",
        "isPrivate": False,
        "owner": {"login": owner},
        "stargazerCount": (index + 1) * 23,
        "forkCount": (index + 1) * 4,
        "languages": {
            "edges": [
                {"size": 1400 + index * 100,
                 "node": {"name": "Python", "color": "#3572A5"}},
                {"size": 500 + index * 20,
                 "node": {"name": "Rust", "color": "#dea584"}},
            ],
        },
    }


def _calendar():
    start = date(9998, 1, 1)
    days = {str(start + timedelta(days=index)): index % 5 + 1
            for index in range(21)}
    week = {"contributionDays": [
        {"date": day, "contributionCount": count}
        for day, count in sorted(days.items())]}
    return days, {"totalContributions": sum(days.values()), "weeks": [week]}


def _repository_for(index):
    owner, name = REPOSITORIES[index]
    return _repo_node(owner, name, index)


def _pull_request(index, number, merged):
    owner, name = REPOSITORIES[index]
    full_name = f"{owner}/{name}"
    created = f"9997-01-{index + 1:02d}T12:00:00Z"
    if merged:
        merged_at = f"9998-02-{index * 3 + number:02d}T12:00:00Z"
        closed_at = merged_at
        state = "MERGED"
    else:
        merged_at = None
        closed_at = f"9998-04-{index + number:02d}T12:00:00Z"
        state = "CLOSED"
    return {
        "id": f"PR_{index}_{number}",
        "number": number,
        "url": f"https://github.com/{full_name}/pull/{number}",
        "merged": merged,
        "state": state,
        "createdAt": created,
        "updatedAt": closed_at or created,
        "mergedAt": merged_at,
        "closedAt": closed_at,
        "repository": {
            "id": f"REPO_{index}",
            "nameWithOwner": full_name,
            "url": f"https://github.com/{full_name}",
            "isPrivate": False,
            "owner": {"login": owner},
        },
    }


def _issue(index, number, state):
    owner, name = REPOSITORIES[index]
    full_name = f"{owner}/{name}"
    created = f"9997-05-{index + number:02d}T08:30:00Z"
    closed = f"9998-05-{index + number:02d}T08:30:00Z" if state != "OPEN" else None
    reason = "COMPLETED" if state == "CLOSED" else (
        "REOPENED" if state == "OPEN" else None)
    if number % 3 == 0:
        reason = "NOT_PLANNED"
    return {
        "id": f"ISSUE_{index}_{number}",
        "number": number,
        "url": f"https://github.com/{full_name}/issues/{number}",
        "createdAt": created,
        "updatedAt": closed or created,
        "closedAt": closed,
        "state": state,
        "stateReason": reason,
        "repository": {
            "id": f"REPO_{index}",
            "nameWithOwner": full_name,
            "url": f"https://github.com/{full_name}",
            "isPrivate": False,
            "owner": {"login": owner},
        },
    }


def _connection_page(nodes, has_next, cursor, total):
    return {
        "nodes": nodes,
        "totalCount": total,
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
    }


def _write_json(path, payload):
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


def _write_payloads(payload_dir, repo_heads):
    org = {"id": 9001, "login": ORG, "isPublic": True,
           "url": f"https://github.com/{ORG}"}
    organizations = {
        "totalCount": 1,
        "nodes": [org],
        "pageInfo": {"hasNextPage": False, "endCursor": None},
    }
    identity_user = {
        "login": USER,
        "databaseId": 8675309,
        "id": "MDQ6VXNlcjg2NzUzMDk=",
        "name": "Benchmark User",
        "emails": [AUTHOR_EMAIL],
        "primaryEmail": AUTHOR_EMAIL,
        "organizations": organizations,
    }
    _write_json(payload_dir / "identity.json", {
        "user": identity_user,
        "viewer": identity_user,
    })
    _write_json(payload_dir / "orgs.json", [{
        "id": 9001,
        "login": ORG,
        "url": f"https://api.github.com/orgs/{ORG}",
    }])

    repo_nodes = [_repository_for(index)
                  for index in range(len(REPOSITORIES))]
    profile_repositories = {
        "totalCount": len(repo_nodes),
        "nodes": repo_nodes,
        "pageInfo": {"hasNextPage": False, "endCursor": None},
    }

    merged_prs = [
        _pull_request(index, number, True)
        for index in range(len(REPOSITORIES))
        for number in range(1, 4)
    ]
    closed_prs = [
        _pull_request(index, number + 3, False)
        for index in range(len(REPOSITORIES))
        for number in range(1, 3)
    ]
    cached_prs = {node["id"]: node for node in merged_prs + closed_prs}
    live_pages = [
        _connection_page(closed_prs[:4], True, "pr-live-page-2",
                         len(closed_prs)),
        _connection_page(closed_prs[4:], False, None, len(closed_prs)),
    ]
    all_prs = merged_prs + closed_prs
    all_pages = [
        _connection_page(all_prs[:10], True, "pr-all-page-2", len(all_prs)),
        _connection_page(all_prs[10:], False, None, len(all_prs)),
    ]
    merged_page = _connection_page(merged_prs, False, None, len(merged_prs))
    pr_payload = {
        "live_pages": live_pages,
        "all_pages": all_pages,
        "merged_page": merged_page,
        "all_nodes": all_prs,
    }
    _write_json(payload_dir / "pull-requests.json", pr_payload)

    issue_nodes = []
    for index in range(len(REPOSITORIES)):
        issue_nodes.extend([
            _issue(index, 1, "CLOSED"),
            _issue(index, 2, "CLOSED"),
            _issue(index, 3, "CLOSED"),
            _issue(index, 4, "OPEN"),
        ])
    issue_pages = [
        _connection_page(issue_nodes[:8], True, "issue-page-2",
                         len(issue_nodes)),
        _connection_page(issue_nodes[8:], False, None, len(issue_nodes)),
    ]
    _write_json(payload_dir / "issues.json", {
        "pages": issue_pages,
        "all_nodes": issue_nodes,
    })

    calendar_days, contribution_calendar = _calendar()
    _write_json(payload_dir / "calendar.json", {
        "user": {"contributionsCollection": {
            "contributionCalendar": contribution_calendar}},
    })

    profile_user = {
        **identity_user,
        "followers": {"totalCount": 37},
        "repositories": profile_repositories,
        "contributionsCollection": {
            "contributionCalendar": contribution_calendar},
        "pullRequests": _connection_page(all_prs, False, None,
                                          len(all_prs)),
        "issues": _connection_page(issue_nodes, False, None,
                                   len(issue_nodes)),
    }
    _write_json(payload_dir / "profile-core.json", {
        "user": profile_user,
        "viewer": profile_user,
    })

    totals = {}
    for index, (owner, name) in enumerate(REPOSITORIES):
        repo = f"{owner}/{name}"
        totals[repo] = {
            "issues": 200 + index * 17,
            "merged_prs": 80 + index * 11,
            "branch": "main",
            "head": repo_heads[repo],
        }
    _write_json(payload_dir / "totals.json", totals)

    _write_json(payload_dir / "manifest.json", {
        "login": USER,
        "organizations": [ORG],
        "emails": [AUTHOR_EMAIL],
        "repo_heads": repo_heads,
        "fixture_timestamp": FUTURE,
    })
    return calendar_days, cached_prs, issue_nodes, totals, [ORG, USER]


def _write_caches(cache_dir, user, calendar_days, cached_prs, issue_nodes,
                  totals, insiders):
    fetched_at = FUTURE
    _write_json(cache_dir / "profile-cache.json", {
        "version": 2,
        "fetched_at": fetched_at,
        "user": user,
        "calendar_days": calendar_days,
        "prs": cached_prs,
        "issues": issue_nodes,
    })

    ourloc = {}
    for repo, total in totals.items():
        stale = hashlib.sha1(("stale-head:" + repo).encode("utf-8"))
        stale_head = stale.hexdigest()
        if stale_head == total["head"]:
            raise AssertionError("stale fixture head unexpectedly matches")
        ourloc[repo] = {
            "head": stale_head,
            "ours": 7280,
            "total": 7280,
        }
    _write_json(cache_dir / "impact-cache.json", {
        "version": 1,
        "fetched_at": fetched_at,
        "prs_fetched_at": fetched_at,
        "insiders": insiders,
        "prs": cached_prs,
        "issues": issue_nodes,
        "totals": totals,
        "ourloc": ourloc,
    })


def build(root):
    """Create a fresh fixture root and return its repository head map."""
    root = Path(root)
    repo_root = Path(__file__).resolve().parents[2]
    root_resolved = root.resolve()
    try:
        inside_repo = (os.path.commonpath(
            [str(root_resolved), str(repo_root)]) == str(repo_root))
    except ValueError:
        inside_repo = False
    if inside_repo:
        raise ValueError("fixture root must be outside the repository")
    root.mkdir(parents=True, exist_ok=True)
    if next(root.iterdir(), None) is not None:
        raise FileExistsError(f"fixture root is not empty: {root}")

    mirror_dir = root / "mirror"
    payload_dir = root / "payloads"
    cache_dir = root / "caches"
    mirror_dir.mkdir()
    payload_dir.mkdir()
    cache_dir.mkdir()

    repo_heads = {}
    for owner, name in REPOSITORIES:
        repo_name = f"{owner}/{name}"
        repo_heads[repo_name] = _create_mirror(
            mirror_dir / f"{owner}__{name}", owner, name)

    (calendar_days, cached_prs, issue_nodes, totals,
     insiders) = _write_payloads(payload_dir, repo_heads)
    identity = json.loads((payload_dir / "profile-core.json").read_text(
        encoding="utf-8"))["user"]
    identity.pop("contributionsCollection", None)
    identity.pop("pullRequests", None)
    identity.pop("issues", None)
    _write_caches(cache_dir, identity, calendar_days, cached_prs,
                  issue_nodes, totals, insiders)
    return repo_heads


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path,
                        help="new fixture root outside the repository")
    args = parser.parse_args(argv)
    try:
        heads = build(args.root)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"fixture setup failed: {exc}", file=sys.stderr)
        return 1
    print(f"fixture root: {args.root.resolve()}")
    print(f"mirror repositories: {len(heads)}")
    for repo, head in sorted(heads.items()):
        print(f"{repo} {head}")
    print("payloads: 8 JSON files")
    print("caches: complete profile and impact caches")
    return 0


if __name__ == "__main__":
    sys.exit(main())
