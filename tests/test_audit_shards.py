"""Splitting the targeted-blame audit: the resolver's shards and the renderer's filter.

    python3 -m unittest discover -v
"""
import contextlib
import io
import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

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


if __name__ == "__main__":
    unittest.main()
