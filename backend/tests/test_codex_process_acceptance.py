"""Process-lifecycle acceptance uses local Python only, never a real model."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from businessmap import codex_provider


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group acceptance")
def test_cleanup_kills_orphan_child_even_after_parent_has_exited(tmp_path):
    pid_file = tmp_path / "child.pid"
    script = ("import subprocess,sys\n"
              "from pathlib import Path\n"
              "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
              f"Path({str(pid_file)!r}).write_text(str(child.pid))\n")
    parent = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    child_pid = None
    try:
        assert parent.wait(timeout=5) == 0
        child_pid = int(pid_file.read_text())
        os.kill(child_pid, 0)  # The parent is gone but its child is still alive.
        codex_provider._stop(parent)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            state = subprocess.run(["ps", "-o", "stat=", "-p", str(child_pid)],
                                   capture_output=True, text=True, check=False, timeout=2).stdout.strip()
            if not state or state.startswith("Z"):
                break
            time.sleep(.02)
        else:
            pytest.fail("A child kept running after its exited Codex parent was cleaned up")
    finally:
        try:
            os.killpg(parent.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        parent.wait()
