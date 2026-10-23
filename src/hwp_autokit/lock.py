"""Serialise Hangul work: a simple file lock and a fair, priority-aware build queue.

Why: one Windows session can host only one *reliable* Hangul automation job at a
time. Two jobs at once either cut each other's COM connection ("RPC server is
unavailable") or - worse - both report success while one output is silently wrong
(a 30-page PDF came out as 2 pages, no error). Non-Hangul steps can run in parallel;
the Hangul step must be serial. See README "Troubleshooting".

Both primitives are plain files in a directory, so any process (Python or the
bundled ``scripts/hwp_queue.ps1``) can take part, and a crashed owner is detected
by ``(pid, process start time)`` instead of a timeout.

Queue rules - a waiter starts only when all three hold:

1. no live ``.running`` entry exists;
2. it is first in line (lower priority number first, then first come);
3. no foreign ``Hwp.exe`` is running (somebody outside the queue may be using
   Hangul; we wait for it instead of killing it).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, List, Optional, Union

from ._proc import list_pids, process_start_token

log = logging.getLogger("hwp_autokit.queue")

PathLike = Union[str, "os.PathLike[str]"]
_DOTNET_EPOCH = datetime(1, 1, 1, tzinfo=timezone.utc)
_FRESH_SECONDS = 15.0  # an unreadable record younger than this is "being written"

__all__ = [
    "FileLock",
    "LockTimeout",
    "BuildQueue",
    "QueueEntry",
    "QueueTimeout",
    "TicketLost",
    "owner_record",
    "is_owner_alive",
    "dotnet_ticks",
]


class LockTimeout(TimeoutError):
    pass


class QueueTimeout(TimeoutError):
    pass


class TicketLost(RuntimeError):
    """Our ticket disappeared (deleted by hand?) - we stop rather than silently re-queue."""


def dotnet_ticks(now: Optional[datetime] = None) -> int:
    """.NET ``DateTime.UtcNow.Ticks`` (100 ns since 0001-01-01), used in ticket names."""
    now = now or datetime.now(timezone.utc)
    delta = now - _DOTNET_EPOCH
    return (delta.days * 86_400 + delta.seconds) * 10_000_000 + delta.microseconds * 10


def owner_record(label: str = "", **extra) -> dict:
    pid = os.getpid()
    rec = {
        "pid": pid,
        "start": process_start_token(pid) or "",
        "label": label,
        "created": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }
    rec.update(extra)
    return rec


def is_owner_alive(rec: dict) -> bool:
    """True if the process that wrote ``rec`` is still the same running process."""
    try:
        pid = int(rec["pid"])
    except (KeyError, TypeError, ValueError):
        return False
    token = process_start_token(pid)
    if token is None:
        return False
    want = str(rec.get("start") or "")
    return not want or not token or token == want


def _record_alive(path: Path, alive: Callable[[dict], bool] = is_owner_alive) -> bool:
    try:
        rec = json.loads(path.read_text(encoding="utf-8-sig"))
        return alive(rec)
    except (OSError, ValueError):
        try:
            return time.time() - path.stat().st_mtime < _FRESH_SECONDS
        except OSError:
            return False


def _write_atomic(path: Path, rec: dict) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- lock
class FileLock:
    """Exclusive, crash-safe lock file.

    >>> with FileLock("C:/build/hangul.lock", timeout=600):
    ...     do_hangul_work()

    A lock whose owner process is gone is broken automatically.
    """

    def __init__(self, path: PathLike, *, timeout: float = 600.0, poll: float = 1.0,
                 label: str = "", alive: Callable[[dict], bool] = is_owner_alive):
        self.path = Path(path)
        self.timeout = timeout
        self.poll = poll
        self.label = label
        self._alive = alive
        self._held = False

    def try_acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if not _record_alive(self.path, self._alive):
                log.info("breaking stale lock %s", self.path)
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                return self.try_acquire()
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(owner_record(self.label), f, ensure_ascii=False)
        self._held = True
        return True

    def acquire(self) -> "FileLock":
        deadline = time.monotonic() + self.timeout
        while not self.try_acquire():
            if time.monotonic() >= deadline:
                raise LockTimeout(f"could not lock {self.path} within {self.timeout:.0f}s")
            time.sleep(self.poll)
        return self

    def release(self) -> None:
        if self._held:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            self._held = False

    @property
    def held(self) -> bool:
        return self._held

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


# -------------------------------------------------------------------------- queue
@dataclass
class QueueEntry:
    name: str
    state: str  # "waiting" | "running"
    tag: str
    label: str
    pid: Optional[int]
    priority: int
    created: str
    alive: bool


def _hwp_count(image: str) -> int:
    return len(list_pids(image))


class BuildQueue:
    """File-based FIFO queue (with priorities 0-9) for one-at-a-time Hangul builds.

    Directory layout::

        <directory>/P5_0638650000000000000_my-build.ticket   waiting
        <directory>/P5_0638650000000000000_my-build.running  holding the turn

    The ticket name sorts by priority, then by arrival time, so ``sorted()`` is the queue order.
    """

    def __init__(self, directory: PathLike, *, foreign_process: Optional[str] = "Hwp.exe",
                 foreign_count: Optional[Callable[[], int]] = None, poll: float = 3.0,
                 alive: Callable[[dict], bool] = is_owner_alive):
        self.dir = Path(directory)
        self.poll = poll
        self._alive = alive
        if foreign_count is not None:
            self._foreign = foreign_count
        elif foreign_process:
            self._foreign = lambda: _hwp_count(foreign_process)
        else:
            self._foreign = lambda: 0

    # -- inspection ---------------------------------------------------------
    def _files(self, suffix: str) -> List[Path]:
        if not self.dir.is_dir():
            return []
        return sorted(p for p in self.dir.iterdir() if p.suffix == suffix)

    def entries(self) -> List[QueueEntry]:
        out = []
        for state, suffix in (("running", ".running"), ("waiting", ".ticket")):
            for p in self._files(suffix):
                try:
                    rec = json.loads(p.read_text(encoding="utf-8-sig"))
                except (OSError, ValueError):
                    rec = {}
                m = re.match(r"P(\d)_\d+_(.*)$", p.stem)
                out.append(QueueEntry(
                    name=p.name, state=state,
                    tag=str(rec.get("tag") or (m.group(2) if m else p.stem)),
                    label=str(rec.get("label") or ""),
                    pid=int(rec["pid"]) if str(rec.get("pid", "")).isdigit() else None,
                    priority=int(m.group(1)) if m else 5,
                    created=str(rec.get("created") or ""),
                    alive=_record_alive(p, self._alive),
                ))
        return out

    def cleanup(self, keep: Optional[Path] = None) -> List[str]:
        """Remove entries whose owner process is gone. Returns removed names."""
        removed = []
        for p in self._files(".ticket") + self._files(".running"):
            if p != keep and not _record_alive(p, self._alive):
                try:
                    p.unlink()
                    removed.append(p.name)
                    log.info("removed dead queue entry %s", p.name)
                except FileNotFoundError:
                    pass
        return removed

    # -- taking a turn ------------------------------------------------------
    def _new_ticket(self, tag: str, label: str, priority: int) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        pr = min(9, max(0, int(priority)))
        safe = re.sub(r"[^\w\-.]", "_", tag) or "job"
        name = f"P{pr}_{dotnet_ticks():019d}_{safe}"
        ticket = self.dir / f"{name}.ticket"
        _write_atomic(ticket, owner_record(label, tag=tag, priority=pr))
        return ticket

    @contextmanager
    def turn(self, tag: str, *, label: str = "", priority: int = 5,
             max_wait: float = 6 * 3600, foreign_wait: float = 1800,
             on_wait: Optional[Callable[[Optional[int], str], None]] = None) -> Iterator[Path]:
        """Block until it is our turn, hold it for the ``with`` block, then release.

        ``max_wait``     total seconds we are willing to queue.
        ``foreign_wait`` seconds to wait, once first in line, for foreign Hangul to exit.
        ``on_wait(position, reason)`` is called on every poll while waiting.
        """
        ticket = self._new_ticket(tag, label, priority)
        running = ticket.with_suffix(".running")
        holding = False
        t0 = time.monotonic()
        foreign_since: Optional[float] = None
        log.info("queued %s (priority %s)", ticket.name, priority)
        try:
            while True:
                self.cleanup(keep=ticket)
                if not ticket.exists():
                    raise TicketLost(f"queue ticket vanished: {ticket.name}")
                busy = self._files(".running")
                waiting = [p.name for p in self._files(".ticket")]
                pos = waiting.index(ticket.name) + 1 if ticket.name in waiting else None
                if not busy and pos == 1:
                    n = self._foreign()
                    if n == 0:
                        os.replace(ticket, running)
                        holding = True
                        log.info("turn started for %s after %.0fs", tag, time.monotonic() - t0)
                        break
                    foreign_since = foreign_since or time.monotonic()
                    if time.monotonic() - foreign_since > foreign_wait:
                        raise QueueTimeout(
                            f"first in line, but {n} Hangul process(es) outside the queue stayed open "
                            f"for {foreign_wait:.0f}s - check who is using Hangul (they were not killed)")
                    if on_wait:
                        on_wait(pos, f"waiting for {n} foreign Hangul process(es) to exit")
                else:
                    foreign_since = None
                    if on_wait:
                        on_wait(pos, f"position {pos}, {len(busy)} running")
                if time.monotonic() - t0 > max_wait:
                    raise QueueTimeout(f"queued for more than {max_wait:.0f}s")
                time.sleep(self.poll)
            yield running
        finally:
            for p in (ticket, running) if holding else (ticket,):
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
