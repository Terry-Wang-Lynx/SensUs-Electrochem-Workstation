import asyncio
import os
from pathlib import Path
import subprocess
import sys

import pytest

from pa_host import v52_ble

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows process and file-lock regression")


def test_process_liveness_does_not_terminate_process():
    child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
    try:
        for _ in range(3):
            assert v52_ble._pid_alive(child.pid)
            assert child.poll() is None
        assert v52_ble._pid_alive(os.getpid())
    finally:
        child.terminate()
        child.wait(timeout=5)
    assert not v52_ble._pid_alive(child.pid)


def test_other_process_cannot_lease_same_board(tmp_path, monkeypatch):
    monkeypatch.setattr(v52_ble, "LOCK_DIR", tmp_path)
    child_code = (
        "import asyncio;from pathlib import Path;from pa_host import v52_ble as v;"
        f"v.LOCK_DIR=Path({str(tmp_path)!r});"
        "asyncio.run(v.BleLockLease('test-board',timeout=.2).acquire())"
    )

    async def run():
        lease = await v52_ble.BleLockLease("test-board", timeout=2).acquire()
        try:
            child = subprocess.run([sys.executable, "-c", child_code], capture_output=True, text=True, timeout=8)
            assert child.returncode != 0
            assert "TimeoutError" in child.stderr
            assert v52_ble.inspect_locks()["tokens"][0]["status"] == "active"
        finally:
            lease.release()
    asyncio.run(run())
    assert not v52_ble.inspect_locks()["tokens"]


def test_different_boards_can_be_leased_together(tmp_path, monkeypatch):
    monkeypatch.setattr(v52_ble, "LOCK_DIR", tmp_path)

    async def run():
        first = await v52_ble.BleLockLease("test-board-one", timeout=1).acquire()
        second = await v52_ble.BleLockLease("test-board-two", timeout=1).acquire()
        try:
            assert len(v52_ble.inspect_locks()["tokens"]) == 2
        finally:
            second.release()
            first.release()
    asyncio.run(run())


def test_process_exit_releases_kernel_lock_and_prunes_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(v52_ble, "LOCK_DIR", tmp_path)
    child_code = (
        "import asyncio,os;from pathlib import Path;from pa_host import v52_ble as v;"
        f"v.LOCK_DIR=Path({str(tmp_path)!r});"
        "asyncio.run(v.BleLockLease('abandoned-board',timeout=1).acquire());os._exit(0)"
    )
    child = subprocess.run([sys.executable, "-c", child_code], timeout=8)
    assert child.returncode == 0

    async def run():
        lease = await v52_ble.BleLockLease("abandoned-board", timeout=1).acquire()
        try:
            tokens = v52_ble.inspect_locks()["tokens"]
            assert len(tokens) == 1 and tokens[0]["pid"] == os.getpid()
        finally:
            lease.release()
    asyncio.run(run())
