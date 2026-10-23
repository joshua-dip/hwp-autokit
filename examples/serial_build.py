"""Run several build jobs in parallel, but only one Hangul step at a time.

    python examples/serial_build.py

Each job does its non-Hangul work freely, then waits for a turn in a shared queue
folder before touching Hangul. The same folder can be shared with PowerShell
wrappers that use scripts/hwp_queue.ps1.
"""
import tempfile
import threading
import time
from pathlib import Path

from hwp_autokit.lock import BuildQueue

QUEUE_DIR = Path(tempfile.gettempdir()) / "hwp-autokit-queue-demo"


def job(name: str, priority: int) -> None:
    time.sleep(0.5)                                  # e.g. fetch data, render images ...
    q = BuildQueue(QUEUE_DIR, poll=0.5)             # waits for any Hwp.exe outside the queue, too
    with q.turn(name, priority=priority,
                on_wait=lambda pos, why: print(f"  {name}: {why}")):
        print(f"{name}: using Hangul")               # e.g. HwpSession(...) work here
        time.sleep(1)
    print(f"{name}: done")


threads = [threading.Thread(target=job, args=(f"job{i}", 0 if i == 3 else 5)) for i in range(1, 4)]
for t in threads:
    t.start()
for t in threads:
    t.join()
