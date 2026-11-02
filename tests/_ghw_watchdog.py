"""Per-test hang watchdog; see GH_WIDGETS_TEST_TIMEOUT in CONTRIBUTING.md."""
import faulthandler
import os
import sys
import types
import unittest

ENV = "GH_WIDGETS_TEST_TIMEOUT"


def install():
    limit = float(os.environ.get(ENV) or 0)
    if limit > 0:
        unittest.TestCase.run = _watched(unittest.TestCase.run, limit,
                                         sys.stderr)


def _watched(run, limit, stream):
    scope, cell = run.__globals__, run.__closure__

    def watched_run(self, result=None):
        faulthandler.dump_traceback_later(limit, exit=True, file=stream)
        code = run.__code__.replace(co_name=self.id())  # dumps name frames
        return types.FunctionType(code, scope, None, None, cell)(self, result)
    return watched_run
