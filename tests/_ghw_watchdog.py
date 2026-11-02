"""No test starting or ending for GH_WIDGETS_TEST_TIMEOUT s dumps all stacks, exits 1."""
import faulthandler
import os
import sys
import unittest

ENV = "GH_WIDGETS_TEST_TIMEOUT"


def install():
    limit = float(os.environ.get(ENV) or 0)
    if limit > 0:
        stream = sys.stderr
        for name in ("startTest", "stopTest"):
            original = getattr(unittest.TestResult, name)
            setattr(unittest.TestResult, name,
                    _rearming(original, limit, stream))


def _rearming(original, limit, stream):
    def hook(self, test):
        faulthandler.dump_traceback_later(limit, exit=True, file=stream)
        return original(self, test)
    return hook
