"""Blocks holding a locked dir; see test_ghw_temp."""
import os
import time
import unittest

from tests import _ghw_temp


class TestHangProbe(unittest.TestCase):
    def test_hold_and_block(self):
        tmp = _ghw_temp.tempdir(prefix="ghw-hang-probe-")
        marker = os.environ["GH_WIDGETS_HANG_PROBE_DIR_FILE"]
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write(tmp.name)
        time.sleep(300)
