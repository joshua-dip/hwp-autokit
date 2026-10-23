"""Dependency-free process helpers (Windows first, POSIX fallbacks).

The queue and lock record ``(pid, start_token)`` instead of a bare PID so that a
recycled PID is never mistaken for the original owner. The start token is the
process creation time as a Windows FILETIME integer (100 ns ticks since 1601 UTC),
which PowerShell can produce identically with ``$p.StartTime.ToFileTimeUtc()``.
"""
from __future__ import annotations

import csv
import io
import os
import subprocess
import sys
import time
from typing import Optional, Set

IS_WINDOWS = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

__all__ = [
    "IS_WINDOWS",
    "list_pids",
    "process_start_token",
    "pid_alive",
    "kill_pid",
    "wait_pid_exit",
]


def list_pids(image: str) -> Set[int]:
    """PIDs of running processes whose image name is ``image`` (e.g. ``"Hwp.exe"``).

    Returns an empty set on non-Windows systems or if ``tasklist`` is unavailable.
    """
    if not IS_WINDOWS:
        return set()
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH", "/FI", f"IMAGENAME eq {image}"],
            capture_output=True, text=True, errors="replace", timeout=30,
            creationflags=CREATE_NO_WINDOW,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    pids: Set[int] = set()
    for row in csv.reader(io.StringIO(out)):
        if len(row) >= 2 and row[0].strip().lower() == image.lower() and row[1].strip().isdigit():
            pids.add(int(row[1]))
    return pids


def _windows_start_token(pid: int) -> Optional[str]:
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != STILL_ACTIVE:
            return None
        c, e, k, u = (wintypes.FILETIME() for _ in range(4))
        if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
            return ""
        return str((c.dwHighDateTime << 32) | c.dwLowDateTime)
    finally:
        k32.CloseHandle(h)


def process_start_token(pid: int) -> Optional[str]:
    """A string that identifies *this* incarnation of ``pid``.

    * ``None`` - the process does not exist (or has exited).
    * ``""``   - it exists but no start time could be read (unknown platform).
    * otherwise - a stable token (FILETIME on Windows, ``/proc`` start ticks on Linux).
    """
    if pid <= 0:
        return None
    if IS_WINDOWS:
        return _windows_start_token(pid)
    stat = f"/proc/{pid}/stat"
    if os.path.exists(stat):
        try:
            with open(stat, encoding="utf-8", errors="replace") as f:
                fields = f.read().rsplit(")", 1)[1].split()
            return "linux:" + fields[19]  # field 22 overall = starttime
        except (OSError, IndexError):
            return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return ""
    return ""


def pid_alive(pid: int) -> bool:
    return process_start_token(pid) is not None


def kill_pid(pid: int) -> None:
    """Force-kill exactly one process (and its children on Windows). Never by image name."""
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True,
                       timeout=30, creationflags=CREATE_NO_WINDOW)
    else:
        import signal
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def wait_pid_exit(pid: int, timeout: float, poll: float = 0.25) -> bool:
    """Wait until ``pid`` is gone. True if it exited within ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(poll)
    return not pid_alive(pid)
