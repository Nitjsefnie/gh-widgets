"""The sharded gitfame-resync-memory workflow: its script's steps, and its run: blocks executed.

    python3 -m unittest discover -v
"""
import contextlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import yaml

from scripts.bench.bench_platform import REQUIRES_POSIX_SHELL
from tests import _ghw_temp
from tests.test_pins import _load

ROOT = Path(__file__).resolve().parents[1]
rm = _load("resync_memory", "scripts/resync-memory.py")
PINS = {"o/a": {"head": "a", "shard": 0}, "o/b": {"head": "b", "shard": 1},
        "o/c": {"head": "c", "shard": 1}, "o/d": {"head": "d", "shard": 2}}
LINES = {"o/a": 1000, "o/b": 20, "o/c": 3000, "o/d": 45}


def journal(repos, method="both", guard=True, verdict="ok", lines=None):
    text = f"blame-method: {method}\n" + ("git-fame: 3.1.4 with --jobs\n" if guard else "")
    for i, repo in enumerate(repos, 1):
        total = (lines or LINES)[repo]
        text += f"    compare {repo}: fame (1, {total}) vs targeted (1, {total}) {verdict} in 0.1s\n"
        text += f"loc [{i}/{len(repos)}] ours       1 / {total:>8,} ( 0.1%)  {repo}\n"
    return text + "    timing o/a: clone 2.0s wait 1.5s fame 3.0s\n"


def write_run(results, label, text, rc=0, cgroup=1024, tree=2048, gitfame=512):
    results.mkdir(parents=True, exist_ok=True)
    (results / f"{label}.out").write_text(text, encoding="utf-8")
    (results / f"{label}.json").write_text(json.dumps({
        "label": label, "rc": rc, "wall_s": 9.5, "cgroup_memory_peak_kb": cgroup,
        "sampler_tree_peak_kb": tree, "top_processes_by_peak_rss_kb": [
            {"pid": 7, "peak_rss_kb": gitfame, "cmd": "python3 /v/bin/git-fame --jobs"},
            {"pid": 8, "peak_rss_kb": gitfame * 9, "cmd": "git blame x"}]}), encoding="utf-8")


def scratch(case):
    temporary = _ghw_temp.tempdir(prefix="ghw-resync-memory-")
    case.addCleanup(temporary.cleanup)
    return Path(temporary.name)


def full_results(case, builds=("fork", "up"), repeats=(1, 2), skip=(), **overrides):
    """One clean run per (build, repeat, shard), with `overrides[label]` replacing a journal."""
    results = scratch(case) / "results"
    for build in builds:
        for repeat in repeats:
            for shard in (0, 1, 2):
                label = rm.run_label(build, repeat, shard)
                if label not in skip:
                    repos = [r for r, p in PINS.items() if p["shard"] == shard]
                    write_run(results, label, overrides.get(label, journal(repos)))
    return results


def problems(results, builds=("fork", "up"), repeats=(1, 2)):
    return rm.check_runs(results, PINS, list(builds), list(repeats))[1]


class MatrixAxes(unittest.TestCase):
    def test_valid_inputs_become_the_three_axes(self):
        self.assertEqual(rm.matrix_axes(PINS, " up  fork new ", "2"),
                         {"builds": ["up", "fork", "new"], "repeats": [1, 2], "shards": [0, 1, 2]})

    def test_every_invalid_input_is_refused(self):
        cap = 256 // (2 * 3)  # GitHub's matrix-job limit over 2 arms and 3 shards
        self.assertEqual(rm.matrix_axes(PINS, "fork up", str(cap))["repeats"][-1], cap)
        cases = {"unknown arm": ("fork nope", "1"), "duplicate arm": ("fork up fork", "1"),
                 "no arm": ("  ", "1"), "zero repeats": ("fork", "0"), "words": ("fork", "two"),
                 "fraction": ("fork", "1.5"), "negative": ("fork", "-1"),
                 "over the cap": ("fork up", str(cap + 1))}
        for name, (builds, repeats) in cases.items():
            with self.subTest(name), self.assertRaises(SystemExit):
                rm.matrix_axes(PINS, builds, repeats)

    def test_a_manifest_without_a_valid_shard_on_every_pin_is_refused(self):
        for shard in (None, True, -1, "1"):
            with self.subTest(shard=shard), self.assertRaises(SystemExit):
                rm.matrix_axes({**PINS, "o/e": {"head": "e", "shard": shard}}, "fork", "1")
        with self.assertRaises(SystemExit):
            rm.matrix_axes({}, "fork", "1")


class FreshCgroup(unittest.TestCase):
    def cgroup(self, peak, procs=""):
        cg = scratch(self) / "cg"
        cg.mkdir()
        (cg / "memory.peak").write_text(str(peak), encoding="ascii")
        (cg / "cgroup.procs").write_text(procs, encoding="ascii")
        return cg

    def test_a_peak_the_kernel_lets_us_reset_is_reset(self):
        cg = self.cgroup(10 ** 12)
        with contextlib.redirect_stdout(io.StringIO()):
            rm.fresh_cgroup(cg)
        self.assertEqual((cg / "memory.peak").read_text(encoding="ascii"), "0")

    def test_a_carried_over_peak_or_a_resident_process_is_fatal(self):
        limit = 50 * 1024 * 1024
        for peak, procs, fatal in ((limit, "", False), (limit + 1, "", True), (0, "123\n", True)):
            cg = self.cgroup(peak, procs)
            with self.subTest(peak=peak, procs=procs), contextlib.redirect_stdout(io.StringIO()), \
                    mock.patch.object(Path, "write_text", side_effect=OSError("read-only")):
                if fatal:
                    self.assertRaises(SystemExit, rm.fresh_cgroup, cg)
                else:
                    rm.fresh_cgroup(cg)


class Measure(unittest.TestCase):
    def test_the_harness_records_the_command_and_its_tree(self):
        root = scratch(self)
        (root / "cg").mkdir()
        (root / "cg" / "memory.peak").write_text("4096", encoding="ascii")
        for code, expected in ((0, 0), (3, 1)):
            (root / "cg" / "cgroup.procs").write_text("", encoding="ascii")  # the kernel empties it on exit
            argv = ["measure", "--cgroup", str(root / "cg"), "--json", str(root / "r.json"),
                    "--label", "fork-r1-s0", "--stdout", str(root / "o"), "--stderr", str(root / "e"),
                    "--", sys.executable, "-c", f"print('hi'); raise SystemExit({code})"]
            with self.subTest(code=code), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(rm.main(argv), expected)
            run = json.loads((root / "r.json").read_text(encoding="utf-8"))
            self.assertEqual((run["label"], run["rc"], run["cgroup_memory_peak_kb"]), ("fork-r1-s0", code, 0))
            self.assertEqual((root / "o").read_text(encoding="utf-8"), "hi\n")


class Validate(unittest.TestCase):
    def check(self, text, rc=0, svg=b"<svg/>"):
        root = scratch(self)
        write_run(root, "fork-r1-s0", text, rc=rc)
        if svg is not None:
            (root / "impact.svg").write_bytes(svg)
        with contextlib.redirect_stdout(io.StringIO()):
            return rm.validate("fork-r1-s0", root, root / "impact.svg")

    def test_a_good_run_passes_and_each_defect_alone_is_refused(self):
        good = journal(["o/a"])
        self.assertEqual(self.check(good), [])
        self.assertEqual(self.check(journal(["o/a"], method="targeted", guard=False)), [])
        cases = {"rc": {"text": good, "rc": 1}, "empty svg": {"text": good, "svg": b""},
                 "no svg": {"text": good, "svg": None}, "nothing blamed": {"text": journal([])},
                 "no method": {"text": good.replace("blame-method: both\n", "")},
                 "no git-fame line": {"text": journal(["o/a"], guard=False)}}
        for name, kwargs in cases.items():
            with self.subTest(name):
                self.assertEqual(len(self.check(**kwargs)), 1, name)

    def test_a_run_without_results_is_refused(self):
        self.assertEqual(rm.validate("fork-r1-s0", scratch(self), "x.svg"), ["fork-r1-s0: no results"])


class CheckRuns(unittest.TestCase):
    def test_a_complete_agreeing_set_passes(self):
        self.assertEqual(problems(full_results(self)), [])

    def test_each_way_a_shard_or_arm_can_go_wrong_is_refused(self):
        moved = {**LINES, "o/c": 3001}
        cases = {
            "a missing shard": {"skip": {"up-r2-s1"}},
            "another shard's repo": {"fork-r1-s0": journal(["o/a", "o/d"])},
            "a repo blamed twice": {"fork-r2-s1": journal(["o/b", "o/c", "o/c"])},
            "a missing repo": {"up-r1-s1": journal(["o/b"])},
            "lines differ across arms": {"up-r1-s1": journal(["o/b", "o/c"], lines=moved)},
            "targeted disagrees": {"fork-r1-s2": journal(["o/d"], verdict="MISMATCH")},
        }
        for name, overrides in cases.items():
            with self.subTest(name):
                self.assertEqual(len(problems(full_results(self, **overrides))), 1, name)

    def test_a_result_the_matrix_never_asked_for_is_refused(self):
        results = full_results(self)
        write_run(results, "new-r1-s0", journal(["o/a"]))
        self.assertEqual(len(problems(results)), 1)


class Peaks(unittest.TestCase):
    def test_the_full_resync_peak_is_the_max_over_the_repeats_shards(self):
        results = scratch(self)
        for shard, (cg, tree, gf) in enumerate(((10, 500, 30), (900, 20, 70), (40, 60, 5))):
            write_run(results, rm.run_label("fork", 1, shard), journal([]), cgroup=cg, tree=tree, gitfame=gf)
            write_run(results, rm.run_label("up", 1, shard), journal([]), cgroup=cg * 2, tree=1, gitfame=1)
        runs = {(b, 1, s): rm.load_run(results, rm.run_label(b, 1, s)) for b in ("fork", "up") for s in range(3)}
        self.assertEqual(rm.peaks(runs), {("fork", 1): [900, 500, 70], ("up", 1): [1800, 1, 1]})
        runs["fork", 1, 2]["cgroup_memory_peak_kb"] = None
        self.assertEqual(rm.peaks(runs)["fork", 1], [None, 500, 70])

    def test_the_report_prints_the_max_row_and_fails_on_a_refusal(self):
        results = full_results(self, builds=("fork",), repeats=(1,))
        write_run(results, "fork-r1-s1", journal(["o/b", "o/c"]), cgroup=5 * 1024)
        pins = scratch(self) / "pins.json"
        pins.write_text(json.dumps(PINS), encoding="utf-8")
        argv = ["report", "--results", str(results), "--pins", str(pins), "--builds", '["fork"]', "--repeats", "[1]"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(rm.main(argv), 0)
        self.assertIn("fork  repeat 1: cgroup 5.0 MiB  sampler tree 2.0 MiB  git-fame proc 0.5 MiB", out.getvalue())
        self.assertIn("shard 1: 2 repos, 3,020 lines", out.getvalue())
        (results / "fork-r1-s2.json").unlink()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(rm.main(argv), 1)


# A python3 that answers for the resolver, the renderer and `-m venv`, and runs
# the real interpreter for everything else; sudo runs its command as is.
STUB = f"""#!/bin/sh
case "$1" in
  scripts/resolve-repo-pins.py) while [ $# -gt 0 ]; do [ "$1" = --out ] && cp "$STUB_MANIFEST" "$2"; shift; done; exit 0 ;;
  */src/render-impact.py) shift
    printf '%s\\n' "path=${{PATH%%:*}}" "shard=$GH_WIDGETS_IMPACT_SHARD" "method=$GH_WIDGETS_BLAME_METHOD" \\
      "bots=$GH_WIDGETS_IMPACT_COUNT_BOT_PRS" "pins=$GH_WIDGETS_IMPACT_REPO_PINS" "args=$*" > "$RECORD"
    cat "$STUB_LOG"; echo '<svg/>' > "$GH_WIDGETS_OUT_DIR/impact.svg"; exit 0 ;;
  -m) [ "$2" = venv ] || exit 9; mkdir -p "$3/bin"
    printf '#!/bin/sh\\necho "$*" >> "$RECORD"\\n' > "$3/bin/pip"
    printf '#!/bin/sh\\necho "git-fame 9 $STUB_HELP"\\n' > "$3/bin/git-fame"
    chmod +x "$3/bin/pip" "$3/bin/git-fame"; exit 0 ;;
esac
exec {shlex.quote(sys.executable)} "$@"
"""
SUDO = '#!/bin/sh\n[ "$1" = -E ] && shift\nexec "$@"\n'


class WorkflowSteps(unittest.TestCase):
    """The decoded run: scalars of gitfame-resync-memory.yml, run as the runner runs them.

    Each step gets only the env the workflow composes for it (job, then step,
    `matrix.*` substituted) plus what bash and the stubs need; /tmp/ and the
    cgroup tree move into a scratch directory.
    """

    jobs = yaml.safe_load((ROOT / ".github/workflows/gitfame-resync-memory.yml").read_text(encoding="utf-8"))["jobs"]
    matrix = {"build": "fork", "repeat": "2", "shard": "1"}

    def run_step(self, job, name, root=None, env=None):
        step = next(s for s in self.jobs[job]["steps"] if s.get("name") == name)
        root = root or scratch(self)
        (root / "bin").mkdir(exist_ok=True)
        for tool, body in (("python3", STUB), ("sudo", SUDO)):
            (root / "bin" / tool).write_text(body, encoding="utf-8")
            (root / "bin" / tool).chmod(0o755)

        def decode(text):
            text = re.sub(r"\$\{\{\s*matrix\.(\w+)\s*\}\}", lambda m: self.matrix[m.group(1)], str(text))
            text = re.sub(r"\$\{\{.*?\}\}", "x", text).replace("/tmp/", f"{root}/")
            return text.replace("/sys/fs/cgroup/", f"{root}/cgroup/")

        # An allowlist, never os.environ: an exported GH_WIDGETS_* would stand in
        # for a variable the workflow itself failed to set.
        full = {key: os.environ[key] for key in ("HOME", "LANG", "LC_ALL", "TMPDIR") if key in os.environ}
        full.update(PATH=f"{root / 'bin'}{os.pathsep}{os.environ['PATH']}", GITHUB_OUTPUT=str(root / "output"),
                    GITHUB_WORKSPACE=str(ROOT), RECORD=str(root / "record"), STUB_LOG=str(root / "log"),
                    STUB_MANIFEST=str(root / "manifest.json"), STUB_HELP="--jobs")
        full.update({k: decode(v) for k, v in {**self.jobs[job].get("env", {}), **step.get("env", {})}.items()})
        full.update(env or {})
        (root / "step.sh").write_text(decode(step["run"]), encoding="utf-8")
        # A run: step with no `shell:` runs as `bash -e {0}`; pipefail is only what the script sets.
        return subprocess.run(["bash", "--noprofile", "--norc", "-e", str(root / "step.sh")], cwd=ROOT,
                              env=full, capture_output=True, text=True, check=False), root

    def test_the_label_is_the_one_the_report_reads(self):
        self.assertEqual(self.jobs["measure"]["env"]["GH_WIDGETS_RUN_LABEL"].replace("${{ matrix.", "{").replace(" }}", "}")
                         .format(**self.matrix), rm.run_label("fork", 2, 1))

    @REQUIRES_POSIX_SHELL
    def test_the_pins_job_emits_validated_axes_or_nothing(self):
        name = "Resolve one commit per repo, split the repos into shards, emit the matrix"
        root = scratch(self)
        (root / "manifest.json").write_text(json.dumps(PINS), encoding="utf-8")
        done, _ = self.run_step("pins", name, root, {"GH_WIDGETS_BUILDS": "fork up", "GH_WIDGETS_REPEATS": "2"})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual((root / "output").read_text(encoding="utf-8"),
                         'builds=["fork", "up"]\nrepeats=[1, 2]\nshards=[0, 1, 2]\n')
        (root / "output").unlink()
        done, _ = self.run_step("pins", name, root, {"GH_WIDGETS_BUILDS": "fork fork", "GH_WIDGETS_REPEATS": "2"})
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual((root / "output").read_text(encoding="utf-8"), "")

    @REQUIRES_POSIX_SHELL
    def test_the_venv_step_builds_only_its_arm_and_demands_jobs(self):
        name = "Build this arm's virtualenv"
        installs = {}
        for build in ("fork", "up", "new"):
            done, root = self.run_step("measure", name, env={"GH_WIDGETS_BUILD": build, "GH_WIDGETS_NEW_REF": "my-ref"})
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertEqual(sorted(p.name for p in root.glob("venv-*")), [f"venv-{build}"])
            upgrade, install = (root / "record").read_text(encoding="utf-8").splitlines()
            self.assertEqual(upgrade, "install -q --upgrade pip")
            installs[build] = install
        self.assertEqual(len(set(installs.values())), 3, installs)
        self.assertTrue(installs["new"].endswith("git-fame@my-ref"), installs)
        for env in ({"GH_WIDGETS_BUILD": "nope"}, {"GH_WIDGETS_BUILD": "up", "STUB_HELP": "serial"}):
            with self.subTest(env=env):
                self.assertNotEqual(self.run_step("measure", name, env=env)[0].returncode, 0)

    @REQUIRES_POSIX_SHELL
    def test_the_cgroup_must_be_v2_and_delegate_memory(self):
        for files, expected in (({"cgroup.controllers", "gfbench/memory.peak"}, 0),
                                ({"gfbench/memory.peak"}, 1), ({"cgroup.controllers"}, 1)):
            root = scratch(self)
            (root / "cgroup" / "gfbench").mkdir(parents=True)
            (root / "cgroup" / "cgroup.subtree_control").write_text("memory\n", encoding="ascii")
            for rel in files:
                (root / "cgroup" / rel).write_text("x\n", encoding="ascii")
            with self.subTest(files=files):
                done, _ = self.run_step("measure", "Prepare the cgroup", root)
                self.assertEqual(done.returncode, expected, done.stdout + done.stderr)

    @REQUIRES_POSIX_SHELL
    def test_one_run_measures_its_shard_validates_and_reports(self):
        root = scratch(self)
        cg = root / "cgroup" / "gfbench"
        cg.mkdir(parents=True)
        (cg / "memory.peak").write_text("0", encoding="ascii")
        (cg / "cgroup.procs").write_text("", encoding="ascii")
        (root / "pins.json").write_text(json.dumps(PINS), encoding="utf-8")
        (root / "log").write_text(journal(["o/b", "o/c"]), encoding="utf-8")
        env = {"GH_WIDGETS_BLAME_METHOD": "both"}
        done, _ = self.run_step("measure", "Measure one resync of this shard", root, env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual((root / "record").read_text(encoding="utf-8").splitlines(),
                         [f"path={root}/venv-fork/bin", "shard=1", "method=both", "bots=1",
                          f"pins={root}/pins.json", "args=--resync"])
        label = rm.run_label("fork", 2, 1)
        self.assertEqual(json.loads((root / "results" / f"{label}.json").read_text(encoding="utf-8"))["rc"], 0)
        done, _ = self.run_step("measure", "Validate the run before any number is believed", root)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        (root / "out" / "impact.svg").write_text("", encoding="utf-8")
        self.assertEqual(self.run_step("measure", "Validate the run before any number is believed", root)[0].returncode, 1)
        report = {"GH_WIDGETS_BUILDS": '["fork"]', "GH_WIDGETS_REPEATS": "[2]"}
        done, _ = self.run_step("report", "Prove every shard ran once, then summarise", root, report)
        self.assertEqual(done.returncode, 1, "shards 0 and 2 never ran")
        for shard, repos in ((0, ["o/a"]), (2, ["o/d"])):
            write_run(root / "results", rm.run_label("fork", 2, shard), journal(repos))
        done, _ = self.run_step("report", "Prove every shard ran once, then summarise", root, report)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
