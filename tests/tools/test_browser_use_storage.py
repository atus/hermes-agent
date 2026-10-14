"""Ownership storage refuses path redirection without changing existing cache modes."""
import os
import subprocess
import sys

import pytest

from hermes_constants import get_hermes_home
from tools import browser_use_lifecycle as life


@pytest.mark.platforms("posix")
def test_existing_readable_browser_cache_is_supported():
    root = get_hermes_home() / "cache" / "browser-use"
    root.mkdir(parents=True, mode=0o755)
    root.chmod(0o755)
    root.parent.chmod(0o755)
    assert life._state_dir().is_dir()
    assert root.stat().st_mode & 0o777 == 0o755


@pytest.mark.platforms("posix")
def test_fifo_record_is_rejected_without_blocking(tmp_path):
    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo, 0o600)
    probe = ("import os\nfrom pathlib import Path\n"
             "from tools.browser_use_lifecycle import _safe_open\n"
             f"try: _safe_open(Path({str(fifo)!r}), os.O_RDONLY)\n"
             "except OSError: pass\nelse: raise AssertionError('FIFO accepted')\n")
    try:
        result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=3)
    except subprocess.TimeoutExpired:
        pytest.fail("A FIFO ownership record blocked the lifecycle reader")
    assert result.returncode == 0, result.stderr


@pytest.mark.platforms("posix")
def test_execution_lease_is_released_after_owner_death(tmp_path):
    import time
    ready = tmp_path / "ready"
    probe = ("import time\nfrom pathlib import Path\n"
             "from tools import browser_tool_lifecycle as worker\n"
             "from tools import browser_use_lifecycle as life\n"
             "worker._start_browser_cleanup_thread = lambda: None\n"
             "with life._execution({}, 'crashed-owner', 'lease'):\n"
             f"    Path({str(ready)!r}).touch()\n    time.sleep(60)\n")
    peer = subprocess.Popen([sys.executable, "-c", probe])
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        assert ready.exists()
        path = next(life._state_dir().glob("*.json"))
        with pytest.raises(TimeoutError):
            with life._lease(path):
                pytest.fail("A worker acquired an executing owner's lease")
        peer.kill()
        peer.wait(timeout=5)
        with life._lease(path):
            assert life._read_record(path)["owner"] == "crashed-owner"
    finally:
        if peer.poll() is None:
            peer.kill()
            peer.wait(timeout=5)


@pytest.mark.platforms("posix")
def test_inflight_cli_keeps_lease_after_gateway_death(tmp_path):
    import time
    ready = tmp_path / "cli-ready"
    child = f"import time; from pathlib import Path; Path({str(ready)!r}).touch(); time.sleep(3)"
    owner_code = ("import os, sys\nfrom tools import browser_use_lifecycle as life\n"
                  "from tools import browser_tool_lifecycle as worker\n"
                  "from tools.browser_use_cli import _run_cli_killing_process_group\n"
                  "worker._start_browser_cleanup_thread = lambda: None\n"
                  "with life._execution({}, 'inflight-owner', 'lease'):\n"
                  f"    _run_cli_killing_process_group([sys.executable, '-c', {child!r}], '', dict(os.environ), 10)\n")
    owner = subprocess.Popen([sys.executable, "-c", owner_code])
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        assert ready.exists()
        path = next(life._state_dir().glob("*.json"))
        owner.kill()
        owner.wait(timeout=5)
        with pytest.raises(TimeoutError):
            with life._lease(path):
                pytest.fail("A restarted worker raced the old gateway's still-executing CLI")
        with life._lease(path, timeout=10):
            assert path.exists()
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)


@pytest.mark.platforms("posix")
def test_state_reads_refuse_symlink_ancestors(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    real.joinpath("owned.json").write_text("{}")
    real.joinpath("owned.json").chmod(0o600)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(OSError):
        fd = life._safe_open(alias / "owned.json", os.O_RDONLY)
        os.close(fd)
