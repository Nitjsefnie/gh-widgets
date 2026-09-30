#!/usr/bin/env python3
"""Tests for gql's HTTP retry rules. Stdlib only; no network or real sleeps."""
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "ghwidgets_common", Path(__file__).with_name("ghwidgets_common.py"))
if spec is None or spec.loader is None:
    raise SystemExit("error: cannot load ghwidgets_common.py")
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return json.dumps(self.body).encode("utf-8")


class GqlRetry(unittest.TestCase):
    @contextmanager
    def patched_io(self, outcomes):
        requests = mock.Mock(side_effect=outcomes)
        sleeps = []
        with mock.patch.object(common.urllib.request, "urlopen", requests), \
                mock.patch.object(common.time, "sleep",
                                  side_effect=sleeps.append):
            yield requests, sleeps

    @staticmethod
    def http_error(code):
        return common.urllib.error.HTTPError(
            "https://api.github.com/graphql", code, "failure", None, None)

    @staticmethod
    def response():
        return FakeResponse({"data": {"answer": 42}})

    def test_401_fails_without_retry_or_backoff(self):
        errors = [self.http_error(401) for _ in range(4)]
        with self.patched_io(errors) as (requests, sleeps):
            with self.assertRaises(common.urllib.error.HTTPError):
                common.gql("token", "query")
        self.assertEqual(requests.call_count, 1)
        self.assertEqual(sleeps, [])

    def test_429_fails_without_retry_or_backoff(self):
        errors = [self.http_error(429) for _ in range(4)]
        with self.patched_io(errors) as (requests, sleeps):
            with self.assertRaises(common.urllib.error.HTTPError):
                common.gql("token", "query")
        self.assertEqual(requests.call_count, 1)
        self.assertEqual(sleeps, [])

    def test_500_retries_until_success(self):
        outcomes = [self.http_error(500), self.http_error(500),
                    self.response()]
        with self.patched_io(outcomes) as (requests, sleeps):
            result = common.gql("token", "query")
        self.assertEqual(result, {"answer": 42})
        self.assertEqual(requests.call_count, 3)
        self.assertEqual(sleeps, [5, 10])

    def test_500_exhausts_all_four_attempts(self):
        outcomes = [self.http_error(500) for _ in range(4)]
        with self.patched_io(outcomes) as (requests, sleeps):
            with self.assertRaises(common.urllib.error.HTTPError):
                common.gql("token", "query")
        self.assertEqual(requests.call_count, 4)
        self.assertEqual(sleeps, [5, 10, 15])

    def test_url_error_still_retries_until_success(self):
        outcomes = [common.urllib.error.URLError("temporary"),
                    self.response()]
        with self.patched_io(outcomes) as (requests, sleeps):
            result = common.gql("token", "query")
        self.assertEqual(result, {"answer": 42})
        self.assertEqual(requests.call_count, 2)
        self.assertEqual(sleeps, [5])
