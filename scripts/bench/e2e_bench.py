#!/usr/bin/env python3
"""Run deterministic renderer workloads and write one JUnit suite."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path


WORKLOADS = (
    ("render", "render.py",
     ("stats.svg", "streak.svg", "languages.svg", "external.svg"),
     "profile-cache.json"),
    ("render-impact", "render-impact.py", ("impact.svg",),
     "impact-cache.json"),
    ("render-responsiveness", "render-responsiveness.py",
     ("responsiveness.svg",), "impact-cache.json"),
)
DEAD_PROXY = "http://127.0.0.1:9"


def _is_within(path, parent):
    try:
        return os.path.commonpath([str(Path(path).resolve()),
                                  str(Path(parent).resolve())]) == str(
                                      Path(parent).resolve())
    except ValueError:
        return False


def _renderer_environment(fixture_root, bench_dir, cache_file, out_dir):
    env = os.environ.copy()
    payloads = fixture_root / "payloads"
    mirror = fixture_root / "mirror"
    env.update({
        "GH_USER": "bench-user",
        "GH_TOKEN": "bench-token",
        "GH_EXTRA_EMAILS": "bench-user@users.noreply.github.com",
        "GH_EXTRA_INSIDERS": "",
        "CACHE_FILE": str(cache_file),
        "OUT_DIR": str(out_dir),
        "CLONE_SOURCE_DIR": str(mirror),
        "PYTHONPATH": str(bench_dir),
        "GH_BENCH_PAYLOADS": str(payloads),
        "PYTHONDONTWRITEBYTECODE": "1",
        "HTTPS_PROXY": DEAD_PROXY,
        "https_proxy": DEAD_PROXY,
        "NO_PROXY": "",
        "no_proxy": "",
        "BLAME_METHOD": "targeted",
    })
    manifest = json.loads((payloads / "manifest.json").read_text(
        encoding="utf-8"))
    repositories = sorted(manifest["repo_heads"])
    env["GIT_CONFIG_COUNT"] = str(len(repositories))
    for index, repository in enumerate(repositories):
        mirror_repo = mirror / repository.replace("/", "__")
        env[f"GIT_CONFIG_KEY_{index}"] = (
            f"url.{mirror_repo.as_uri()}.insteadOf")
        env[f"GIT_CONFIG_VALUE_{index}"] = (
            f"https://github.com/{repository}.git")
    return env


def _capture_outputs(out_dir, destination):
    destination.mkdir(parents=True, exist_ok=True)
    if out_dir.is_dir():
        for path in sorted(out_dir.iterdir()):
            if path.is_file() and (path.suffix == ".svg"
                                   or path.name == "last-updated.txt"):
                shutil.copyfile(path, destination / path.name)


def _prepare_cache(run_dir, fixture_root, pristine_cache):
    profile_cache = run_dir / "cache.json"
    impact_cache = run_dir / "impact-cache.json"
    shutil.copyfile(fixture_root / "caches" / "profile-cache.json",
                    profile_cache)
    shutil.copyfile(fixture_root / "caches" / "impact-cache.json",
                    impact_cache)
    if pristine_cache == "profile-cache.json":
        return profile_cache
    return impact_cache


def _run_renderer(renderer, repo_root, env):
    result = {"time": 0.0, "failure": None, "stdout": "", "stderr": ""}
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [sys.executable, str(renderer)], cwd=str(repo_root), env=env,
            capture_output=True, text=True, check=False, timeout=600)
        result["time"] = time.perf_counter() - started
        result["stdout"] = completed.stdout or ""
        result["stderr"] = completed.stderr or ""
        if completed.returncode != 0:
            result["failure"] = (
                f"renderer exited with code {completed.returncode}\n"
                f"stdout:\n{result['stdout']}\nstderr:\n{result['stderr']}")
    except subprocess.TimeoutExpired as exc:
        result["time"] = time.perf_counter() - started
        result["stdout"] = _decode(exc.stdout)
        result["stderr"] = _decode(exc.stderr)
        result["failure"] = (
            "renderer exceeded the 600 second timeout\n"
            f"stdout:\n{result['stdout']}\nstderr:\n{result['stderr']}")
    except OSError as exc:
        result["time"] = time.perf_counter() - started
        result["failure"] = f"could not run renderer: {exc}"
    return result


def _validate_live_result(result, out_dir, expected_svgs):
    if result["failure"] is not None:
        return
    missing = [svg for svg in expected_svgs
               if not (out_dir / svg).is_file()
               or (out_dir / svg).stat().st_size == 0]
    if missing:
        result["failure"] = "renderer did not write SVG(s): " + ", ".join(
            missing)
    elif "fetch failed" in result["stdout"].lower():
        result["failure"] = (
            "renderer used the degraded cache-recovery path\n"
            f"stdout:\n{result['stdout']}")
    elif not any(line.startswith("wrote ")
                 for line in result["stdout"].splitlines()):
        result["failure"] = (
            "renderer did not print a live-path wrote summary\n"
            f"stdout:\n{result['stdout']}")


def _validate_impact_refresh(result, cache_file, fixture_root):
    """Require every stale LOC entry to refresh from its local mirror."""
    try:
        manifest = json.loads((fixture_root / "payloads" /
                              "manifest.json").read_text(encoding="utf-8"))
        seeded = json.loads((fixture_root / "caches" /
                             "impact-cache.json").read_text(
                                 encoding="utf-8"))["ourloc"]
        updated = json.loads(cache_file.read_text(encoding="utf-8"))["ourloc"]
    except (KeyError, OSError, ValueError) as exc:
        result["failure"] = f"could not verify impact ourloc refresh: {exc}"
        return

    problems = []
    for repository, head in sorted(manifest["repo_heads"].items()):
        expected_lines = manifest["expected_ourloc_lines"][repository]
        old_entry = seeded[repository]
        new_entry = updated.get(repository, {})
        if old_entry.get("head") == head:
            problems.append(f"{repository}: fixture head was not stale")
        if old_entry.get("ours") == expected_lines:
            problems.append(f"{repository}: fixture ours count was not stale")
        if new_entry.get("head") != head:
            problems.append(
                f"{repository}: ourloc head did not refresh to mirror HEAD")
        if (new_entry.get("ours") == old_entry.get("ours")
                or new_entry.get("ours") != expected_lines):
            problems.append(
                f"{repository}: ourloc count did not refresh from "
                f"{old_entry.get('ours')} to {expected_lines}")
        if new_entry.get("total") != expected_lines:
            problems.append(
                f"{repository}: ourloc total is {new_entry.get('total')}, "
                f"expected {expected_lines}")

    if problems:
        result["failure"] = (
            "impact ourloc refresh validation failed:\n- "
            + "\n- ".join(problems))


def _run_workload(side, repo_root, round_number, work_root, fixture_root,
                  workload):
    name, script_name = workload[:2]
    renderer = repo_root / script_name
    result = {"name": f"bench.{name}", "time": 0.0, "skipped": False,
              "failure": None, "stdout": "", "stderr": ""}
    if not renderer.is_file():
        result["skipped"] = True
        return result

    run_dir = work_root / "runs" / f"{side}-{round_number}" / name
    run_dir.mkdir(parents=True, exist_ok=False)
    cache_file = _prepare_cache(run_dir, fixture_root, workload[3])
    out_dir = run_dir / "out"
    output_dir = work_root / "outputs" / f"{side}-{round_number}" / name
    env = _renderer_environment(fixture_root, Path(__file__).resolve().parent,
                                cache_file, out_dir)

    result.update(_run_renderer(renderer, repo_root, env))
    _capture_outputs(out_dir, output_dir)
    _validate_live_result(result, out_dir, workload[2])
    if name == "render-impact" and result["failure"] is None:
        _validate_impact_refresh(result, cache_file, fixture_root)
    return result


def _decode(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _compare_workload_outputs(output_root, workload_name, head_rounds):
    round_files = []
    diffs = []
    for round_dir in head_rounds:
        svg_dir = round_dir / workload_name
        svg_files = {path.name: path.read_bytes()
                     for path in sorted(svg_dir.glob("*.svg"))}
        if svg_files:
            round_files.append((round_dir.name, svg_files))
    if len(round_files) != len(head_rounds):
        return [f"{workload_name}: SVG output missing in a head round"]

    reference_round, reference_files = round_files[0]
    for round_name, files in round_files[1:]:
        if set(files) != set(reference_files):
            diffs.append(
                f"{workload_name}: SVG file set differs between "
                f"{reference_round} and {round_name}")
            continue
        for filename in sorted(reference_files):
            if files[filename] != reference_files[filename]:
                diffs.append(
                    f"{workload_name}/{filename}: bytes differ between "
                    f"{reference_round} and {round_name}")
    return diffs


def _selfcheck(work_root, results):
    output_root = work_root / "outputs"
    head_rounds = sorted(
        path for path in output_root.glob("head-*") if path.is_dir())
    if len(head_rounds) < 2:
        message = "selfcheck needs at least two stored head rounds"
        for result in results:
            if not result["skipped"]:
                result["failure"] = _append_failure(result["failure"], message)
        return [message]

    diffs = []
    for result, workload in zip(results, WORKLOADS):
        if not result["skipped"]:
            diffs.extend(_compare_workload_outputs(
                output_root, workload[0], head_rounds))

    if diffs:
        message = "SVG selfcheck failed:\n" + "\n".join(diffs)
        for result in results:
            if not result["skipped"]:
                result["failure"] = _append_failure(result["failure"], message)
        return diffs
    return []


def _append_failure(existing, message):
    if existing:
        return existing + "\n\n" + message
    return message


def _write_junit(path, results):
    suite = ET.Element("testsuite", {
        "name": "renderer-e2e",
        "tests": str(len(results)),
        "failures": str(sum(
            result["failure"] is not None for result in results)),
        "errors": "0",
        "skipped": str(sum(result["skipped"] for result in results)),
        "time": f"{sum(result['time'] for result in results):.6f}",
    })
    for result in results:
        case = ET.SubElement(suite, "testcase", {
            "classname": "e2e",
            "name": result["name"],
            "time": f"{result['time']:.6f}",
        })
        if result["skipped"]:
            ET.SubElement(case, "skipped", {"message": "renderer not present"})
        elif result["failure"] is not None:
            failure = ET.SubElement(case, "failure", {
                "message": result["failure"].splitlines()[0],
                "type": "RendererFailure",
            })
            failure.text = result["failure"]
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--side", choices=("base", "head"), required=True)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--round", required=True, type=int)
    parser.add_argument("--junit-file", required=True, type=Path)
    parser.add_argument("--work-root", required=True, type=Path)
    parser.add_argument("--selfcheck", action="store_true")
    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve()
    fixture_value = os.environ.get("GH_BENCH_FIXTURE_ROOT")
    if not fixture_value:
        parser.error("GH_BENCH_FIXTURE_ROOT must name a built fixture root")
    fixture_root = Path(fixture_value).resolve()
    work_root = args.work_root.resolve()
    if not repo_root.is_dir():
        parser.error(f"renderer checkout does not exist: {repo_root}")
    if not (fixture_root / "payloads").is_dir() or not (
            fixture_root / "caches").is_dir():
        parser.error(f"fixture root is incomplete: {fixture_root}")
    if _is_within(work_root, repo_root):
        parser.error("work root must be outside the renderer checkout")
    if args.round < 1:
        parser.error("round must be a positive integer")
    if args.selfcheck and args.side != "head":
        parser.error("--selfcheck is only valid for the head side")

    work_root.mkdir(parents=True, exist_ok=True)
    results = []
    for workload in WORKLOADS:
        result = _run_workload(args.side, repo_root, args.round, work_root,
                               fixture_root, workload)
        results.append(result)
        status = "skipped" if result["skipped"] else (
            "failed" if result["failure"] else "passed")
        print(f"{result['name']} ({args.side} round {args.round}): "
              f"{result['time']:.3f}s {status}")

    selfcheck_diffs = []
    if args.selfcheck:
        selfcheck_diffs = _selfcheck(work_root, results)
        if selfcheck_diffs:
            print("SVG selfcheck differences:", file=sys.stderr)
            for diff in selfcheck_diffs:
                print(f"  {diff}", file=sys.stderr)
        else:
            print("SVG selfcheck: all head-round outputs are byte-identical")

    _write_junit(args.junit_file, results)
    return int(any(result["failure"] is not None for result in results))


if __name__ == "__main__":
    sys.exit(main())
