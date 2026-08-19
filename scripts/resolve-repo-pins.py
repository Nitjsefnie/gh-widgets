#!/usr/bin/env python3
"""Resolve the commit each repo of an impact run will be blamed at.

An audit compares two counting methods, and a memory comparison compares two
git-fame builds; both do it across runs that clone minutes apart. Cloning the
default branch means each of those clones takes whatever the tip happens to be
at that moment, so a repo that moves mid-comparison quietly makes the arms
incomparable while every report still reads clean.

This writes the manifest that `IMPACT_REPO_PINS` names: the repo set the run
will blame, each at one commit, resolved once up front from the same GraphQL
query the renderer itself uses. Run it before the arms, not per arm.

    scripts/resolve-repo-pins.py --user Nitjsefnie --out pins.json

It is CI tooling: install.sh does not deploy it, and the renderers do not
import it.
"""
import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path


def load_renderer(path=None):
    """Load render-impact.py as a module, for its fetch functions."""
    path = Path(path) if path else Path(__file__).resolve().parent.parent / "render-impact.py"
    spec = importlib.util.spec_from_file_location("render_impact", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"error: cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def pin_manifest(blamed_repos, totals):
    """Return ``{repo: {branch, head}}`` for the repos a run will blame.

    Repos without a resolvable head are omitted rather than pinned to an empty
    commit: update_loc skips those too, so pinning them would make the
    manifest disagree with the run it is supposed to describe.
    """
    pins = {}
    for repo in sorted(blamed_repos):
        t = totals.get(repo) or {}
        if not t.get("head"):
            continue
        pins[repo] = {"branch": t.get("branch") or "", "head": t["head"]}
    return pins


def write_manifest(path, pins):
    """Write the manifest, refusing to write one that pins nothing."""
    if not pins:
        raise SystemExit("error: refusing to write an empty pin manifest — "
                         "an empty file un-pins the whole run silently")
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(pins, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    tmp.replace(path)
    return path


def merged_external_repos(renderer, token, login, insiders):
    """The repo set update_loc would blame: merged, external pull requests."""
    prs, _by_id = renderer.fetch_pull_requests(token, login, None)
    return {pr["repository"]["nameWithOwner"] for pr in prs
            if pr["merged"] and renderer.common.is_external(pr, insiders)}


def parse_args():
    p = argparse.ArgumentParser(
        description="Resolve one commit per repo for a pinned impact run.")
    p.add_argument("--user", default=os.environ.get("GH_USER"),
                   help="GitHub username (env: GH_USER)")
    p.add_argument("--token", default=os.environ.get("GH_TOKEN"),
                   help="GitHub PAT (env: GH_TOKEN)")
    p.add_argument("--token-file", default=os.environ.get("GH_TOKEN_FILE"),
                   help="Read token from a file (env: GH_TOKEN_FILE)")
    p.add_argument("--out", default="pins.json",
                   help="Manifest path to write (default: pins.json)")
    p.add_argument("--renderer", default=None,
                   help="Path to render-impact.py (default: the sibling repo)")
    args = p.parse_args()
    if not args.user:
        p.error("--user (or env GH_USER) is required")
    token = args.token
    if not token and args.token_file:
        token = Path(args.token_file).read_text(encoding="utf-8").strip()
    if not token:
        p.error("--token, --token-file, GH_TOKEN, or GH_TOKEN_FILE required")
    return args, token


def main():
    args, token = parse_args()
    renderer = load_renderer(args.renderer)
    me = renderer.common.fetch_identity(token, args.user, gql_fn=renderer.gql)
    repos = merged_external_repos(renderer, token, me.login, me.insiders)
    totals = renderer.fetch_repo_totals(token, sorted(repos))
    pins = pin_manifest(repos, totals)
    write_manifest(args.out, pins)
    print(f"pinned {len(pins)} repo(s) of {len(repos)} candidate(s) -> "
          f"{args.out}", flush=True)
    for repo, pin in pins.items():
        print(f"  {repo} {pin['branch']} {pin['head']}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # pylint: disable=broad-except
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
