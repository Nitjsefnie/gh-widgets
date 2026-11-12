"""Splitting the targeted-blame audit: the resolver's shards, the renderer's filter, the workflow.

    python3 -m unittest discover -v
"""
import contextlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from scripts.bench.bench_platform import REQUIRES_POSIX_SHELL
from tests import _ghw_temp
from tests.test_pins import impact_loc, resolve_pins

PINS = {r: {"branch": "main", "head": r[-1] * 3} for r in ("o/a", "o/b", "o/c", "o/d", "o/e")}


class AssignShards(unittest.TestCase):
    def test_every_pin_gets_exactly_one_shard_in_range_and_keeps_its_commit(self):
        weights = {"o/a": 9, "o/b": 1, "o/c": 4, "o/d": 4}  # o/e has no weight
        sharded = resolve_pins.assign_shards(dict(PINS), weights, 3)
        self.assertEqual(set(sharded), set(PINS))
        for repo, pin in sharded.items():
            self.assertEqual({k: pin[k] for k in ("branch", "head")}, PINS[repo])
            self.assertIn(pin["shard"], range(3))
        self.assertEqual({pin["shard"] for pin in sharded.values()}, {0, 1, 2})

    def test_the_split_is_the_same_for_the_same_input_in_any_order(self):
        weights = {"o/a": 2, "o/b": 2, "o/c": 2, "o/d": 2, "o/e": 2}
        forward = resolve_pins.assign_shards(dict(PINS), weights, 2)
        backward = resolve_pins.assign_shards(dict(reversed(PINS.items())), weights, 2)
        self.assertEqual(forward, backward)
        self.assertEqual({r: p["shard"] for r, p in forward.items()},
                         {"o/a": 0, "o/b": 1, "o/c": 0, "o/d": 1, "o/e": 0})

    def test_the_largest_repo_is_placed_first(self):
        # Name order or round-robin would put o/a and o/c together (3 against 1).
        pins = {r: PINS[r] for r in ("o/a", "o/b", "o/c")}
        sharded = resolve_pins.assign_shards(pins, {"o/a": 1, "o/b": 1, "o/c": 2}, 2)
        self.assertEqual({r: p["shard"] for r, p in sharded.items()},
                         {"o/a": 1, "o/b": 1, "o/c": 0})

    def test_disk_usage_is_batched_and_a_vanished_repo_weighs_nothing(self):
        calls = []

        def gql(_token, query, **kwargs):
            calls.append((query.count(": repository("), kwargs))
            return {"a0": {"diskUsage": 7}, "a1": None}

        repos = [f"o/r{i:02}" for i in range(16)]
        usage = resolve_pins.fetch_disk_usage(SimpleNamespace(gql=gql), "t", repos)
        self.assertEqual(calls, [(15, {"allow_missing_aliases": True}), (1, {"allow_missing_aliases": True})])
        self.assertEqual((usage["o/r00"], usage["o/r01"], usage["o/r15"]), (7, 0, 7))

    def test_main_writes_a_sharded_manifest_the_renderer_loads(self):
        root = Path(_ghw_temp.mkdtemp(prefix="ghw-shards-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        pr = {"merged": True, "repository": {"nameWithOwner": "o/a"}}
        renderer = SimpleNamespace(
            common=SimpleNamespace(fetch_identity=lambda *_a, **_k: SimpleNamespace(login="me", insiders=set()),
                                   is_external=lambda *_a: True),
            gql=lambda *_a, **_k: {"a0": {"diskUsage": 3}},
            fetch_pull_requests=lambda *_a: ([pr], {}),
            fetch_repo_totals=lambda *_a: {"o/a": {"branch": "main", "head": "aaa"}})
        argv = ["resolve-repo-pins.py", "--user", "me", "--token", "t", "--shards", "4",
                "--out", str(root / "pins.json")]
        with mock.patch.object(resolve_pins, "load_renderer", return_value=renderer), \
                mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
            resolve_pins.main()
        with mock.patch.dict(os.environ, {"GH_WIDGETS_IMPACT_REPO_PINS": str(root / "pins.json")}):
            self.assertEqual(impact_loc.load_repo_pins(), {"o/a": {"branch": "main", "head": "aaa", "shard": 0}})

    def test_a_shard_count_below_one_is_refused(self):
        with mock.patch.object(sys, "argv", ["resolve-repo-pins.py", "--user", "u", "--token", "t", "--shards", "0"]), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            resolve_pins.parse_args()


class ShardedRun(unittest.TestCase):
    """GH_WIDGETS_IMPACT_SHARD narrows the blame to one shard of the manifest."""

    def manifest(self, pins):
        root = Path(_ghw_temp.mkdtemp(prefix="ghw-shardpins-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / "pins.json").write_text(json.dumps(pins), encoding="utf-8")
        return str(root / "pins.json")

    def test_a_shard_that_is_not_a_non_negative_integer_is_fatal(self):
        for shard in (-1, "1", 1.0, True, None):
            with self.subTest(shard=shard), mock.patch.dict(os.environ, {
                    "GH_WIDGETS_IMPACT_REPO_PINS": self.manifest({"o/a": {"head": "aaa", "shard": shard}})}), \
                    self.assertRaises(SystemExit):
                impact_loc.load_repo_pins()

    def blamed(self, env, pins=None):
        totals = {r: {"branch": "main", "head": "tip"} for r in ("o/a", "o/b", "o/c")}
        if pins is not None:
            env = {**env, "GH_WIDGETS_IMPACT_REPO_PINS": self.manifest(pins)}
        seen = []
        with mock.patch.dict(os.environ, {"GH_WIDGETS_IMPACT_REPO_PINS": "", "GH_WIDGETS_IMPACT_SHARD": "", **env}):
            impact_loc.update_loc(list(totals), totals, {}, True, set(),
                                  blame_fn=lambda moved, *_a: seen.extend(moved))
        return [(repo, t["head"]) for repo, t in seen]

    def test_only_the_named_shard_is_blamed_at_its_pins(self):
        pins = {"o/a": {"head": "aaa", "shard": 1}, "o/b": {"head": "bbb", "shard": 0},
                "o/c": {"head": "ccc", "shard": 1}}
        self.assertEqual(self.blamed({"GH_WIDGETS_IMPACT_SHARD": "1"}, pins), [("o/a", "aaa"), ("o/c", "ccc")])
        self.assertEqual(self.blamed({"GH_WIDGETS_IMPACT_SHARD": " 0 "}, pins), [("o/b", "bbb")])
        self.assertEqual(self.blamed({}, pins), [("o/a", "aaa"), ("o/b", "bbb"), ("o/c", "ccc")])
        self.assertEqual(self.blamed({}), [("o/a", "tip"), ("o/b", "tip"), ("o/c", "tip")])

    def test_each_unsatisfiable_shard_request_is_fatal(self):
        sharded = {r: {"head": "x", "shard": 0} for r in ("o/a", "o/b", "o/c")}
        cases = {
            "no manifest": ({"GH_WIDGETS_IMPACT_SHARD": "0"}, None),
            "a pin without a shard": ({"GH_WIDGETS_IMPACT_SHARD": "0"}, {**sharded, "o/b": {"head": "x"}}),
            "an unpinned candidate": ({"GH_WIDGETS_IMPACT_SHARD": "0"}, {"o/a": sharded["o/a"]}),
            "a shard that is not a number": ({"GH_WIDGETS_IMPACT_SHARD": "-1"}, sharded),
            "a shard no pin carries": ({"GH_WIDGETS_IMPACT_SHARD": "1"}, sharded),
        }
        for name, (env, pins) in cases.items():
            with self.subTest(name), self.assertRaises(SystemExit):
                self.blamed(env, pins)


# A python3 on PATH that answers for the resolver and the renderer and runs the
# real interpreter for everything else (the steps' inline `python3 -c` / `-`).
STUB = f"""#!/bin/sh
case "$1" in
  scripts/resolve-repo-pins.py) while [ $# -gt 0 ]; do [ "$1" = --out ] && cp "$STUB_MANIFEST" "$2"; shift; done; exit 0 ;;
  src/render-impact.py) shift
    printf '%s\\n' "shard=$GH_WIDGETS_IMPACT_SHARD method=$GH_WIDGETS_BLAME_METHOD bots=$GH_WIDGETS_IMPACT_COUNT_BOT_PRS pins=$GH_WIDGETS_IMPACT_REPO_PINS args=$*" > "$RECORD"
    cat "$STUB_LOG"; exit "$STUB_RC" ;;
esac
exec {shlex.quote(sys.executable)} "$@"
"""
HEAD = "blame-method: both\ngit-fame: 3.1.4 with --jobs\n"


def compared(*repos):
    return "".join(f"    compare {r}: fame (1, 2) vs targeted (1, 2) ok in 0.1s\n" for r in repos)


class AuditWorkflowSteps(unittest.TestCase):
    """The decoded run: scalars of targeted-blame-audit.yml, run as the runner runs them.

    Each step runs with only the environment the workflow composes for it (job
    env, then step env, `matrix.shard` substituted) plus what bash and the stub
    need, and its /tmp/ paths moved into a scratch directory; python3 is
    stubbed only for the resolver and renderer.
    """

    jobs = yaml.safe_load((Path(__file__).resolve().parents[1]
                           / ".github/workflows/targeted-blame-audit.yml").read_text(encoding="utf-8"))["jobs"]

    def run_step(self, job, name, shard="0", manifest=None, log="", rc=0, audit_log=None):
        step = next(s for s in self.jobs[job]["steps"] if s.get("name") == name)
        scratch = _ghw_temp.tempdir(prefix="ghw-audit-steps-")
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        (root / "bin").mkdir()
        (root / "bin" / "python3").write_text(STUB, encoding="utf-8")
        (root / "bin" / "python3").chmod(0o755)
        (root / "log").write_text(log, encoding="utf-8")
        # The resolver stub copies this to whatever its --out names; only the audit
        # job, which downloads the pins job's manifest, starts with /tmp/pins.json.
        (root / "manifest.json").write_text(json.dumps(manifest or {}), encoding="utf-8")
        if job == "audit":
            shutil.copy(root / "manifest.json", root / "pins.json")
        if audit_log is not None:
            (root / "audit.log").write_text(audit_log, encoding="utf-8")
        # An allowlist, never os.environ: a GH_WIDGETS_* export in the calling shell
        # would stand in for a variable the workflow itself failed to set.
        env = {key: os.environ[key] for key in ("HOME", "LANG", "LC_ALL", "TMPDIR") if key in os.environ}
        env.update(PATH=f"{root / 'bin'}{os.pathsep}{os.environ['PATH']}",
                   GITHUB_OUTPUT=str(root / "output"), RECORD=str(root / "record"),
                   STUB_MANIFEST=str(root / "manifest.json"), STUB_LOG=str(root / "log"), STUB_RC=str(rc))
        for key, value in {**self.jobs[job].get("env", {}), **step.get("env", {})}.items():
            value = re.sub(r"\$\{\{\s*matrix\.shard\s*\}\}", shard, str(value))
            env[key] = re.sub(r"\$\{\{.*?\}\}", "x", value).replace("/tmp/", f"{root}/")
        (root / "step.sh").write_text(step["run"].replace("/tmp/", f"{root}/"), encoding="utf-8")
        # A run: step with no `shell:` runs as `bash -e {0}`; pipefail is only what the script sets.
        done = subprocess.run(["bash", "--noprofile", "--norc", "-e", str(root / "step.sh")],
                              cwd=root, env=env, capture_output=True, text=True, check=False)
        return done, root

    @REQUIRES_POSIX_SHELL
    def test_the_pins_job_outputs_every_shard_the_manifest_uses(self):
        manifest = {r: {**PINS[r], "shard": k} for r, k in (("o/a", 2), ("o/b", 0), ("o/c", 2), ("o/d", 1))}
        done, root = self.run_step("pins", "Resolve one commit per repo and split the repos into shards",
                                   manifest=manifest)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual((root / "output").read_text(encoding="utf-8"), "shards=[0, 1, 2]\n")

    @REQUIRES_POSIX_SHELL
    def test_the_render_step_blames_its_shard_both_ways_and_keeps_the_verdict(self):
        name = "Blame this shard's repos both ways, at the pinned commits"
        done, root = self.run_step("audit", name, shard="3", log=HEAD + compared("o/a"))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual((root / "record").read_text(encoding="utf-8").split(),
                         ["shard=3", "method=both", "bots=1", f"pins={root}/pins.json",
                          "args=--resync", "--cache-file", f"{root}/impact-cache.json"])
        self.assertEqual((root / "audit.log").read_text(encoding="utf-8"), HEAD + compared("o/a"))
        done, _root = self.run_step("audit", name, shard="3", log="error: disagreed\n", rc=2)
        self.assertNotEqual(done.returncode, 0, "tee must not swallow the renderer's exit code")

    @REQUIRES_POSIX_SHELL
    def test_the_proof_step_requires_exactly_the_shards_repos(self):
        manifest = {r: {**PINS[r], "shard": k} for r, k in (("o/a", 0), ("o/b", 1), ("o/c", 0))}
        cases = {
            "exactly the shard, in any order": (HEAD + compared("o/c", "o/a"), 0),
            "a repo missing": (HEAD + compared("o/a"), 1),
            "another shard's repo as well": (HEAD + compared("o/a", "o/c", "o/b"), 1),
            "nothing compared": (HEAD, 1),
            "no blame-method line": ("git-fame: x\n" + compared("o/a", "o/c"), 1),
            "no git-fame line": ("blame-method: both\n" + compared("o/a", "o/c"), 1),
        }
        for case, (log, expected) in cases.items():
            with self.subTest(case):
                done, _root = self.run_step("audit", "Prove the shard compared exactly its repos",
                                            manifest=manifest, audit_log=log)
                self.assertEqual(done.returncode, expected, done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
