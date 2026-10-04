#!/usr/bin/env python3
"""Sentinel: no scratch owner lock survives the suites that consumed them.

A consumer that disposes of a prefetched_clones scratch directory with
plain rmtree leaves its owner-lock handle registered in
impact_clone._SCRATCH_LOCKS forever, printed as an unclosed-file
ResourceWarning at interpreter shutdown (issue 147). unittest discover
orders modules alphabetically, and every module that can register a lock
(test_impact and the test_impact_* modules) sorts before this one, so a
survivor here names a real leak rather than a test still in flight.

    python3 -m unittest test_owner_locks
"""
import unittest

import impact_clone


class TestOwnerLockRegistryDrains(unittest.TestCase):
    """The registry is empty once every registering suite has run."""

    def test_registry_is_empty(self):
        self.assertEqual(
            impact_clone._SCRATCH_LOCKS, {})  # pylint: disable=protected-access
