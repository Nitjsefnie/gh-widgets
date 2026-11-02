"""No test starting or ending for GH_WIDGETS_TEST_TIMEOUT s dumps all stacks, exits 1."""
import faulthandler
import os
import sys
import types
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
        unittest.TestCase.run = _named(unittest.TestCase.run)


def _rearming(original, limit, stream):
    def hook(self, test):
        faulthandler.dump_traceback_later(limit, exit=True, file=stream)
        return original(self, test)
    return hook


def _named(run):
    def frame(self, result=None):
        return run(self, result)

    def named_run(self, result=None):
        # faulthandler prints frame names, so name one for the test.
        code = frame.__code__.replace(co_name=self.id())
        return types.FunctionType(code, {}, None, None, frame.__closure__)(
            self, result)
    return named_run
