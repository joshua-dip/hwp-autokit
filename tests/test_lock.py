import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

import pytest

from hwp_autokit._proc import process_start_token
from hwp_autokit.lock import (BuildQueue, FileLock, LockTimeout, QueueTimeout, TicketLost,
                              dotnet_ticks, is_owner_alive, owner_record)

DEAD_PID = 0x7FFFFFF0  # far above any real PID


def fast_queue(d, foreign=lambda: 0):
    return BuildQueue(d, foreign_count=foreign, poll=0.01)


# ------------------------------------------------------------------ helpers
def test_dotnet_ticks_known_value():
    # 2000-01-01T00:00:00Z in .NET ticks
    assert dotnet_ticks(datetime(2000, 1, 1, tzinfo=timezone.utc)) == 630822816000000000


def test_owner_record_is_alive_for_self():
    rec = owner_record("me")
    assert rec["pid"] == os.getpid()
    assert is_owner_alive(rec)


def test_dead_and_recycled_owner():
    assert not is_owner_alive({"pid": DEAD_PID, "start": "1"})
    me = owner_record()
    if me["start"]:
        # same PID, different start time = PID was recycled -> not our owner
        assert not is_owner_alive({"pid": me["pid"], "start": me["start"] + "0"})
    assert not is_owner_alive({"nope": 1})


# --------------------------------------------------------------------- lock
def test_filelock_exclusive(tmp_path):
    p = tmp_path / "x.lock"
    with FileLock(p, timeout=1):
        assert p.exists()
        with pytest.raises(LockTimeout):
            FileLock(p, timeout=0.1, poll=0.02).acquire()
    assert not p.exists()


def test_filelock_breaks_stale(tmp_path):
    p = tmp_path / "x.lock"
    p.write_text(json.dumps({"pid": DEAD_PID, "start": "1"}), encoding="utf-8")
    lk = FileLock(p, timeout=0.5, poll=0.02)
    assert lk.try_acquire()
    assert json.loads(p.read_text(encoding="utf-8"))["pid"] == os.getpid()
    lk.release()


def test_filelock_fresh_unreadable_record_counts_as_alive(tmp_path):
    p = tmp_path / "x.lock"
    p.write_text("{half-written", encoding="utf-8")
    assert not FileLock(p, timeout=0).try_acquire()


# -------------------------------------------------------------------- queue
def test_queue_turn_and_cleanup(tmp_path):
    q = fast_queue(tmp_path)
    with q.turn("job1") as running:
        assert running.suffix == ".running" and running.exists()
        assert [e.state for e in q.entries()] == ["running"]
    assert list(tmp_path.iterdir()) == []


def test_queue_is_serial_and_ordered(tmp_path):
    q = fast_queue(tmp_path)
    order, active, peak = [], [0], [0]
    lock = threading.Lock()

    def worker(tag, prio, delay):
        time.sleep(delay)
        with q.turn(tag, priority=prio):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
                order.append(tag)
            time.sleep(0.15)
            with lock:
                active[0] -= 1

    ts = [threading.Thread(target=worker, args=a) for a in
          (("first", 5, 0.0), ("late-normal", 5, 0.05), ("urgent", 0, 0.08))]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert peak[0] == 1                       # never two at once
    assert order == ["first", "urgent", "late-normal"]  # urgent jumps the line, never pre-empts


def test_queue_removes_dead_entries(tmp_path):
    dead = tmp_path / "P0_0000000000000000001_crashed.running"
    dead.write_text(json.dumps({"pid": DEAD_PID, "start": "1", "tag": "crashed"}), encoding="utf-8")
    q = fast_queue(tmp_path)
    with q.turn("next"):
        assert not dead.exists()


def test_queue_waits_for_foreign_hangul_then_gives_up(tmp_path):
    q = fast_queue(tmp_path, foreign=lambda: 1)
    seen = []
    with pytest.raises(QueueTimeout):
        with q.turn("job", foreign_wait=0.1, on_wait=lambda pos, why: seen.append(why)):
            pass
    assert any("foreign" in s for s in seen)
    assert list(tmp_path.iterdir()) == []     # ticket withdrawn


def test_queue_foreign_hangul_exits(tmp_path):
    state = {"n": 3}

    def foreign():
        state["n"] = max(0, state["n"] - 1)
        return state["n"]

    with fast_queue(tmp_path, foreign).turn("job", foreign_wait=5):
        pass


def test_queue_ticket_lost(tmp_path):
    q = fast_queue(tmp_path)
    blocker = tmp_path / "P0_0000000000000000001_other.running"
    blocker.write_text(json.dumps(owner_record(tag="other")), encoding="utf-8")

    def remove_mine():
        time.sleep(0.1)
        for p in tmp_path.glob("*.ticket"):
            p.unlink()

    threading.Thread(target=remove_mine).start()
    with pytest.raises(TicketLost):
        with q.turn("mine", max_wait=5):
            pass


@pytest.mark.windows
def test_start_token_matches_powershell():
    """The PowerShell script and Python must agree on the owner token."""
    ps = subprocess.Popen(
        ["powershell", "-NoProfile", "-Command",
         "Write-Output \"$PID $((Get-Process -Id $PID).StartTime.ToFileTimeUtc())\"; Start-Sleep 5"],
        stdout=subprocess.PIPE, text=True)
    try:
        line = ps.stdout.readline().split()
        pid, token = int(line[0]), line[1]
        assert process_start_token(pid) == token
    finally:
        ps.kill()
