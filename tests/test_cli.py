import subprocess
import sys
import time
from pathlib import Path

import pytest

from hwp_autokit.cli import main
from hwp_autokit.lock import BuildQueue

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "hwp_queue.ps1"


def test_queue_run_passes_exit_code(tmp_path, monkeypatch):
    monkeypatch.setattr("hwp_autokit.lock._hwp_count", lambda image: 0)
    rc = main(["queue", "run", str(tmp_path), "--tag", "t", "--poll", "0.01", "--",
               sys.executable, "-c", "raise SystemExit(7)"])
    assert rc == 7
    assert list(tmp_path.iterdir()) == []


def test_paths_scan(tmp_path, capsys):
    (tmp_path / "a.hwp").write_bytes(b"1")
    assert main(["paths", "scan", str(tmp_path), "--pattern", "*.hwp"]) == 0
    assert "1 file(s)" in capsys.readouterr().out


@pytest.mark.windows
def test_powershell_script_interoperates(tmp_path):
    """A PowerShell holder of the queue blocks a Python waiter until it exits."""
    ps = subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
         f". '{SCRIPT}'; $t = Enter-HwpQueue -QueueDir '{tmp_path}' -Tag ps-job -ForeignName nonexistent-proc; "
         "Write-Output 'HOLDING'; Start-Sleep 3; Exit-HwpQueue $t"],
        stdout=subprocess.PIPE, text=True)
    try:
        assert ps.stdout.readline().strip() == "HOLDING"
        q = BuildQueue(tmp_path, foreign_count=lambda: 0, poll=0.1)
        running = [e for e in q.entries() if e.state == "running"]
        assert len(running) == 1 and running[0].tag == "ps-job" and running[0].alive
        t0 = time.monotonic()
        with q.turn("py-job"):
            waited = time.monotonic() - t0
        assert waited > 1.0, "Python must wait for the PowerShell holder"
    finally:
        ps.wait(30)
