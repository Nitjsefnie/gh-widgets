#!/usr/bin/env python3
"""The steps of gitfame-resync-memory.yml: matrix axes, one measured run, the report.

Each job measures one shard for one arm and repeat; a repeat's full-resync
peak is the max over its shards. CI tooling: install.sh does not deploy it.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

ARMS = ("fork", "up", "new")
MAX_MATRIX_JOBS = 256  # GitHub's cap on the jobs one matrix generates per run
FRESH_PEAK_LIMIT = 52428800
LOC = re.compile(r"^loc \[\d+/\d+\] ours\s+([\d,]+) /\s+([\d,]+) \(.*?\)\s+(\S+)$", re.M)
COMPARE = re.compile(r"^\s+compare (\S+): .*?(ok|MISMATCH)", re.M)
METHOD = re.compile(r"^blame-method: (.+)$", re.M)
GUARD = re.compile(r"^git-fame: (.+)$", re.M)


def run_label(build, repeat, shard):
    # The workflow's GH_WIDGETS_RUN_LABEL spells the same name.
    return f"{build}-r{repeat}-s{shard}"


def manifest_shards(pins):
    shards = [pin.get("shard") for pin in pins.values()]
    if not pins or not all(isinstance(s, int) and not isinstance(s, bool) and s >= 0 for s in shards):
        raise SystemExit("error: the pin manifest does not give every repo a shard")
    return sorted(set(shards))


def matrix_axes(pins, builds_raw, repeats_raw):
    """Validate the dispatch inputs once and return the matrix's three axes."""
    builds = builds_raw.split()
    if not builds:
        raise SystemExit("error: no build arm requested")
    unknown = [b for b in builds if b not in ARMS]
    if unknown:
        raise SystemExit(f"FATAL: unknown build arm {unknown} (known: {' '.join(ARMS)})")
    if len(set(builds)) != len(builds):
        raise SystemExit(f"error: a build arm is requested twice: {builds}")
    shards = manifest_shards(pins)
    raw = repeats_raw.strip()
    cap = MAX_MATRIX_JOBS // (len(builds) * len(shards))
    if not (raw.isascii() and raw.isdigit()) or not 1 <= int(raw) <= cap:
        raise SystemExit(f"error: repeats={repeats_raw!r} is not an integer in 1..{cap}, the "
                         f"most that {len(builds)} arm(s) x {len(shards)} shard(s) fit in "
                         f"{MAX_MATRIX_JOBS} matrix jobs")
    return {"builds": builds, "repeats": list(range(1, int(raw) + 1)), "shards": shards}


PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


def read_rss_kb(pid):
    try:
        with open(f"/proc/{pid}/statm", encoding="ascii") as f:
            return int(f.read().split()[1]) * PAGE // 1024
    except (OSError, IndexError, ValueError):
        return 0


def read_cmd(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode(errors="replace").strip()[:120]
    except OSError:
        return "?"


def fresh_cgroup(cg):
    """Recreate the run's cgroup; a carried-over high-water mark is fatal, not assumed away."""
    try:
        os.rmdir(cg)
    except OSError:
        pass
    os.makedirs(cg, exist_ok=True)
    try:
        Path(cg, "memory.peak").write_text("0", encoding="ascii")
    except OSError:
        pass
    stale = int(Path(cg, "memory.peak").read_text(encoding="ascii").strip())
    resident = len(Path(cg, "cgroup.procs").read_text(encoding="ascii").split())
    print(f"cgroup starts at memory.peak={stale}B with {resident} procs", flush=True)
    if stale > FRESH_PEAK_LIMIT or resident != 0:
        raise SystemExit(f"FATAL: cgroup not fresh (peak={stale}B procs={resident}) — "
                         "a carried-over high-water mark would corrupt this run")


def measure(a):  # pylint: disable=too-many-locals
    """Run a command and report the peak memory of the WHOLE process tree, two ways.

    * cgroup v2 `memory.peak`: the exact high-water mark of the tree, shared
      pages once, but with page cache and kernel memory, so an unattributable
      over-count of anonymous RSS.
    * a sampler summing each member's /proc/<pid>/statm RSS: attributable
      (the `git-fame` child is isolated), but it double-counts pages the
      `git blame` children share and misses spikes shorter than the interval.

    Both are reported; neither is silently preferred.
    """
    cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd
    cg = a.cgroup
    fresh_cgroup(cg)
    procs = os.path.join(cg, "cgroup.procs")
    tree_peak_kb, per_pid_peak, samples = [0], {}, [0]
    stop = threading.Event()

    def sample():
        while not stop.is_set():
            try:
                with open(procs, encoding="ascii") as f:
                    pids = [int(x) for x in f.read().split()]
            except OSError:
                pids = []
            total = 0
            for pid in pids:
                rss = read_rss_kb(pid)
                total += rss
                prev = per_pid_peak.get(pid)
                if prev is None or rss > prev[0]:
                    per_pid_peak[pid] = (rss, read_cmd(pid))
            tree_peak_kb[0] = max(tree_peak_kb[0], total)
            samples[0] += 1
            time.sleep(a.interval)

    # `sh` joins the cgroup and then execs the target, so every descendant is
    # inside from the first instruction; writing the pid after Popen would race.
    shim = 'echo $$ > "$1"/cgroup.procs; shift; exec "$@"'
    t0 = time.monotonic()
    with open(a.stdout, "wb") as fo, open(a.stderr, "wb") as fe:
        with subprocess.Popen(["sh", "-c", shim, "sh", cg] + cmd, stdout=fo, stderr=fe) as p:
            th = threading.Thread(target=sample, daemon=True)
            th.start()
            rc = p.wait()
    stop.set()
    th.join(timeout=2)
    wall = time.monotonic() - t0
    cgroup_peak_kb = None
    try:
        cgroup_peak_kb = int(Path(cg, "memory.peak").read_text(encoding="ascii").strip()) // 1024
    except (OSError, ValueError):
        pass
    top = sorted(per_pid_peak.items(), key=lambda kv: -kv[1][0])[:12]
    out = {
        "label": a.label, "rc": rc, "wall_s": round(wall, 1),
        "cgroup_memory_peak_kb": cgroup_peak_kb,
        "sampler_tree_peak_kb": tree_peak_kb[0],
        "sampler_samples": samples[0], "sampler_interval_s": a.interval,
        "top_processes_by_peak_rss_kb": [
            {"pid": pid, "peak_rss_kb": rss, "cmd": c} for pid, (rss, c) in top],
    }
    Path(a.json).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2))
    return 0 if rc == 0 else 1


def load_run(results, label):
    js, log = Path(results, f"{label}.json"), Path(results, f"{label}.out")
    if not js.is_file() or not log.is_file():
        return None
    run = json.loads(js.read_text(encoding="utf-8"))
    run["text"] = log.read_text(encoding="utf-8", errors="replace")
    run["loc"] = [(repo, int(tot.replace(",", ""))) for _o, tot, repo in LOC.findall(run["text"])]
    run["compares"] = COMPARE.findall(run["text"])
    method = METHOD.search(run["text"])
    run["method"] = method.group(1) if method else None
    gfp = [p["peak_rss_kb"] for p in run["top_processes_by_peak_rss_kb"]
           if "git-fame" in p["cmd"] or "git fame" in p["cmd"]]
    run["gitfame_peak_kb"] = max(gfp, default=0)
    return run


def validate(label, results, svg):
    """Refuse a crashed run: it allocates little and would otherwise read as the winner."""
    run = load_run(results, label)
    if run is None:
        return [f"{label}: no results"]
    size = os.path.getsize(svg) if os.path.exists(svg) else 0
    guard = GUARD.search(run["text"])
    bad = []
    if run["rc"] != 0:
        bad.append(f"{label}: rc={run['rc']}")
    if size == 0:
        bad.append(f"{label}: impact.svg missing or empty")
    if not run["loc"]:
        bad.append(f"{label}: blamed ZERO repos — not a max-blame workload")
    if not run["method"]:
        bad.append(f"{label}: no blame-method line")
    # Only the methods that USE git-fame can be expected to identify a build.
    if not guard and run["method"] != "targeted":
        bad.append(f"{label}: no git-fame identification line")
    print(f"{label}: rc={run['rc']} svg={size}B repos_blamed={len(run['loc'])} "
          f"method={run['method'] or 'NONE'} guard={guard.group(1) if guard else 'n/a'}")
    return bad


def shard_problems(label, run, want):
    bad = []
    blamed = [repo for repo, _ in run["loc"]]
    twice = sorted({repo for repo in blamed if blamed.count(repo) > 1})
    if twice:
        bad.append(f"{label}: blamed {twice} more than once")
    if set(blamed) != want:
        bad.append(f"{label}: missing {sorted(want - set(blamed))}, extra {sorted(set(blamed) - want)}")
    # The fast method under-reports SILENTLY, so a disagreement fails the run.
    mismatch = [repo for repo, verdict in run["compares"] if verdict == "MISMATCH"]
    if mismatch:
        bad.append(f"{label}: TARGETED DISAGREES WITH git-fame on {mismatch}")
    return bad


def check_runs(results, pins, builds, repeats):
    """Every run by (build, repeat, shard), and every reason not to believe them."""
    shards = manifest_shards(pins)
    runs, bad = {}, []
    expected = {run_label(b, r, s) for b in builds for r in repeats for s in shards}
    extra = sorted(p.stem for p in Path(results).glob("*.json") if p.stem not in expected)
    if extra:
        bad.append(f"results for runs the matrix never asked for: {extra}")
    for key in ((b, r, s) for b in builds for r in repeats for s in shards):
        run = load_run(results, run_label(*key))
        if run is None:
            bad.append(f"{run_label(*key)}: no results — that shard of that arm and repeat did not run")
            continue
        runs[key] = run
        bad += shard_problems(run_label(*key), run, {r for r, pin in pins.items() if pin["shard"] == key[2]})
    # The arms are comparable only if every run blamed the same surviving lines.
    lines = {}
    for key, run in sorted(runs.items()):
        for repo, total in run["loc"]:
            lines.setdefault(repo, {})[run_label(*key)] = total
    bad += [f"{repo}: surviving lines differ across runs {seen}"
            for repo, seen in sorted(lines.items()) if len(set(seen.values())) > 1]
    return runs, bad


def peaks(runs):
    """Each peak's max over a repeat's shards; None when a shard read none."""
    out = {}
    for (build, repeat, _shard), run in sorted(runs.items()):
        row = out.setdefault((build, repeat), [0, 0, 0])
        cg = run["cgroup_memory_peak_kb"]
        row[0] = None if cg is None or row[0] is None else max(row[0], cg)
        row[1] = max(row[1], run["sampler_tree_peak_kb"])
        row[2] = max(row[2], run["gitfame_peak_kb"])
    return out


def mib(kb):
    return "n/a" if kb is None else f"{kb / 1024:.1f}"


def phases(run):
    # Match each `name value` pair, not a fixed field order, so a new field
    # cannot silently read as an uninstrumented run.
    ph = [dict(re.findall(r"(\w+)\s+([\d.]+)s", line))
          for line in re.findall(r"^\s+timing \S+: .*$", run["text"], re.M)]
    if not ph:
        return "NO TIMING LINES -- attribution is missing, not zero (is GH_WIDGETS_DEBUG_TIMING set?)"
    clone, wait, fame = (sum(float(p[k]) for p in ph if k in p) for k in ("clone", "wait", "fame"))
    return (f"clone {clone:7.1f}s (waited {wait:6.1f}s, hidden {clone - wait:6.1f}s)  "
            f"fame {fame:7.1f}s  blocking {wait + fame:7.1f}s  wall {run['wall_s']:7.1f}s  "
            f"unattributed {run['wall_s'] - wait - fame:6.1f}s")


def summary(runs, pins):
    print(f"{'run':14} {'rc':>3} {'wall_s*':>8} {'cgroup_peak_MiB':>16} "
          f"{'sampler_tree_MiB':>17} {'git-fame_proc_MiB':>18} {'repos':>6}  method")
    for key, run in sorted(runs.items()):
        print(f"{run_label(*key):14} {run['rc']:>3} {run['wall_s']:>8} "
              f"{mib(run['cgroup_memory_peak_kb']):>16} {mib(run['sampler_tree_peak_kb']):>17} "
              f"{mib(run['gitfame_peak_kb']):>18} {len(run['loc']):>6}  {run['method']}")
    print("* wall is an indicative observation of one shared runner, not a magnitude")
    print("\n--- full-resync peak per build and repeat: the max over its shards ---")
    for (build, repeat), (cg, tree, gf) in peaks(runs).items():
        print(f"{build:5} repeat {repeat}: cgroup {mib(cg)} MiB  sampler tree {mib(tree)} MiB  "
              f"git-fame proc {mib(gf)} MiB")
    print("\n--- repos and surviving lines per shard ---")
    first = {}
    for key, run in sorted(runs.items()):
        first.setdefault(key[2], run)
    for shard, run in sorted(first.items()):
        print(f"shard {shard}: {len(run['loc'])} repos, {sum(t for _, t in run['loc']):,} lines")
    # Where each run's wall goes: clone is network-bound and git fame CPU-bound.
    print("\n--- phase attribution (GH_WIDGETS_DEBUG_TIMING) ---")
    for key, run in sorted(runs.items()):
        print(f"{run_label(*key)}: {phases(run)}")
    print("\n--- targeted-vs-fame agreement ---")
    for key, run in sorted(runs.items()):
        agree = sum(verdict == "ok" for _repo, verdict in run["compares"])
        print(f"{run_label(*key)}: " + (f"{agree}/{len(run['compares'])} repos agree" if run["compares"]
                                        else "no comparison ran (GH_WIDGETS_BLAME_METHOD != both)"))
    print("\n--- largest repos blamed (drives the peak) ---")
    sizes = {repo: total for run in first.values() for repo, total in run["loc"]}
    for repo, loc in sorted(sizes.items(), key=lambda kv: -kv[1])[:10]:
        print(f"  {loc:>10,}  {repo}  shard {pins[repo]['shard']}")


def parse_args(argv):
    ap = argparse.ArgumentParser(description="The gitfame-resync-memory workflow's steps.")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("axes")
    p.add_argument("--pins", required=True)
    p.add_argument("--builds", required=True)
    p.add_argument("--repeats", required=True)
    p = sub.add_parser("measure")
    for name in ("--cgroup", "--json", "--label", "--stdout", "--stderr"):
        p.add_argument(name, required=True)
    p.add_argument("--interval", type=float, default=0.02)
    p.add_argument("cmd", nargs=argparse.REMAINDER)
    p = sub.add_parser("validate")
    for name in ("--label", "--results", "--svg"):
        p.add_argument(name, required=True)
    p = sub.add_parser("report")
    for name in ("--results", "--pins", "--builds", "--repeats"):
        p.add_argument(name, required=True)
    return ap.parse_args(argv)


def main(argv=None):
    a = parse_args(argv)
    if a.command == "measure":
        return measure(a)
    if a.command == "validate":
        bad = validate(a.label, a.results, a.svg)
    else:
        pins = json.loads(Path(a.pins).read_text(encoding="utf-8"))
        if a.command == "axes":
            for axis, values in matrix_axes(pins, a.builds, a.repeats).items():
                print(f"{axis}={json.dumps(values)}")
            return 0
        runs, bad = check_runs(a.results, pins, json.loads(a.builds), json.loads(a.repeats))
        if not bad:
            summary(runs, pins)
    if bad:
        print("VALIDATION FAILED:\n  " + "\n  ".join(bad), file=sys.stderr)
        return 1
    print("all runs valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
