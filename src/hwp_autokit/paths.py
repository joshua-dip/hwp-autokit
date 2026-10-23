"""Long-path safety and verified saves.

The gotcha: when the target path is longer than Windows ``MAX_PATH`` (260 chars incl.
the terminator), Hangul's ``SaveAs`` fails to write the file **but still returns
success over COM**. A batch runner then logs "12/12 saved" over an empty folder.
So: (1) refuse long paths *before* saving, (2) after saving, check on disk that the
file exists, is non-empty and is fresh, and (3) at the end of a batch, count files
on disk instead of trusting the log.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import time
from pathlib import Path
from typing import Callable, Iterator, List, Optional, Tuple, Union

from ._proc import IS_WINDOWS

PathLike = Union[str, "os.PathLike[str]"]

WINDOWS_MAX_PATH = 260
#: Default budget. Leaves a margin under the 259 usable characters for temp/backup
#: names that applications derive from the target path.
DEFAULT_LIMIT = 250

__all__ = [
    "WINDOWS_MAX_PATH",
    "DEFAULT_LIMIT",
    "PathTooLongError",
    "SaveVerificationError",
    "path_length",
    "check_path",
    "fit_filename",
    "extended_path",
    "verify_saved",
    "safe_save",
    "count_files",
    "find_long_paths",
]


class PathTooLongError(ValueError):
    pass


class SaveVerificationError(RuntimeError):
    pass


def path_length(path: PathLike) -> int:
    """Length of the absolute path as Windows will see it."""
    return len(os.path.abspath(os.fspath(path)))


def check_path(path: PathLike, limit: int = DEFAULT_LIMIT) -> Path:
    n = path_length(path)
    if n > limit:
        raise PathTooLongError(
            f"path is {n} chars (limit {limit}); Hangul may 'succeed' without writing it: {path}")
    return Path(path)


def fit_filename(directory: PathLike, stem: str, suffix: str, *, limit: int = DEFAULT_LIMIT,
                 min_stem: int = 8) -> str:
    """Return ``stem + suffix``, shortened if needed so ``directory/<name>`` fits ``limit``.

    Shortened names keep a prefix of the stem plus ``~`` and a 6-char hash of the full
    stem, so different long names stay different and the same input is stable.
    """
    base = os.path.abspath(os.fspath(directory))
    budget = limit - len(base) - 1 - len(suffix)  # 1 = path separator
    if len(stem) <= budget:
        return stem + suffix
    tail = "~" + hashlib.sha1(stem.encode("utf-8")).hexdigest()[:6]
    keep = budget - len(tail)
    if keep < min_stem:
        raise PathTooLongError(
            f"directory is too long ({len(base)} chars) to fit a {min_stem}+ char file name under {limit}")
    return stem[:keep].rstrip(" .") + tail + suffix


def extended_path(path: PathLike) -> str:
    r"""``\\?\``-prefixed absolute path on Windows (bypasses MAX_PATH for *our* checks)."""
    p = os.path.abspath(os.fspath(path))
    if not IS_WINDOWS or p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def verify_saved(path: PathLike, *, min_bytes: int = 1, not_before: Optional[float] = None) -> int:
    """Raise :class:`SaveVerificationError` unless ``path`` exists, has ``min_bytes``+ bytes,
    and (if ``not_before`` is given) was modified at or after that epoch time. Returns the size."""
    try:
        st = os.stat(extended_path(path))
    except FileNotFoundError:
        raise SaveVerificationError(f"save reported success but no file on disk: {path}") from None
    if st.st_size < min_bytes:
        raise SaveVerificationError(f"saved file is {st.st_size} bytes (< {min_bytes}): {path}")
    if not_before is not None and st.st_mtime < not_before:
        raise SaveVerificationError(f"file on disk is older than this save (stale copy?): {path}")
    return st.st_size


def safe_save(save: Callable[[str], object], path: PathLike, *, limit: int = DEFAULT_LIMIT,
              min_bytes: int = 1, settle: float = 3.0, poll: float = 0.2,
              clock_slack: float = 2.0) -> Path:
    """Run ``save(str(path))`` with a pre-check on length and a post-check on disk.

    ``save`` returning ``False`` is treated as failure; any other return is ignored,
    because the return value is exactly what cannot be trusted. ``settle`` gives the
    application a moment to flush before we look.
    """
    target = check_path(path, limit)
    target.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time() - clock_slack
    if save(str(target)) is False:
        raise SaveVerificationError(f"save returned False: {target}")
    deadline = time.monotonic() + settle
    while True:
        try:
            verify_saved(target, min_bytes=min_bytes, not_before=t0)
            return target
        except SaveVerificationError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(poll)


def _walk(directory: PathLike, recursive: bool) -> Iterator[Tuple[str, os.DirEntry]]:
    stack = [extended_path(directory)]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    if e.is_dir(follow_symlinks=False):
                        if recursive:
                            stack.append(e.path)
                    else:
                        yield cur, e
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            continue


def _plain(p: str) -> str:
    if p.startswith("\\\\?\\UNC\\"):
        return "\\\\" + p[8:]
    return p[4:] if p.startswith("\\\\?\\") else p


def count_files(directory: PathLike, pattern: str = "*", *, recursive: bool = False) -> int:
    """Count files on disk (long paths included). Use this, not the batch log, as the truth."""
    return sum(1 for _, e in _walk(directory, recursive) if fnmatch.fnmatch(e.name.lower(), pattern.lower()))


def find_long_paths(directory: PathLike, limit: int = DEFAULT_LIMIT, *, recursive: bool = True) -> List[str]:
    """Files under ``directory`` whose full path exceeds ``limit`` (invisible to many tools)."""
    out = []
    for _, e in _walk(directory, recursive):
        p = _plain(e.path)
        if len(p) > limit:
            out.append(p)
    return sorted(out)
