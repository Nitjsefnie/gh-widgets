import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tests import _ghw_temp, _ghw_watchdog  # noqa: E402; pylint: disable=wrong-import-position

_ghw_temp.scavenge()
_ghw_watchdog.install()
