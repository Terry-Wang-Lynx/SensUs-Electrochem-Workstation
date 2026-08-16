"""Keep host tests out of the developer's persistent workstation state."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory


_TEST_RUNTIME = TemporaryDirectory(prefix="sensus-host-tests-")
_TEST_ROOT = Path(_TEST_RUNTIME.name)

os.environ["SENSUS_STATE_DIR"] = str(_TEST_ROOT / "state")
os.environ["SENSUS_LOG_DIR"] = str(_TEST_ROOT / "logs")
os.environ["SENSUS_MEASUREMENTS_DIR"] = str(_TEST_ROOT / "measurements")
# Unit tests must never probe or open whatever lab hardware happens to be
# attached to the developer machine. Transport-specific tests override the
# module globals explicitly.
os.environ["SENSUS_TRANSPORT"] = "rtt"
