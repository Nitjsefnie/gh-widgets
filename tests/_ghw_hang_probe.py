#!/usr/bin/env python3
"""A test-run stand-in that blocks while holding one helper temp directory.

Not collected: the name misses ``unittest discover``'s ``test*.py`` pattern.
The end-to-end scavenger test launches it as its own run and kills it, so
its ``ghw-hang-probe-*`` directory exercises the dead-owner reclaim.

    python3 -m unittest tests._ghw_hang_probe
"""
import os
import time
import unittest

from tests import _ghw_temp


class TestHangProbe(unittest.TestCase):
    """One locked directory, then a block the parent run ends with a signal."""

    def test_hold_and_block(self):
        tmp = _ghw_temp.tempdir(prefix="ghw-hang-probe-")
        marker = os.environ["GH_WIDGETS_HANG_PROBE_DIR_FILE"]
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write(tmp.name)
        time.sleep(300)
