"""hwp-autokit - practical helpers for automating Hancom Hangul (HWP/HWPX) on Windows.

Modules
    session   COM session: isolated gen_py cache, restart-on-death, safe quit
    text      plain/HTML insertion; plain+HTML clipboard extraction that keeps underlines
    lock      FileLock and BuildQueue for one-at-a-time Hangul work
    paths     long-path pre-checks, verified saves, counting files on disk
    overflow  page-overflow checker (empty / under-filled pages)

Heavy imports (pywin32, PyMuPDF) happen lazily, so the pure parts work on any OS.
"""
__version__ = "0.1.0"

from .lock import BuildQueue, FileLock  # noqa: E402
from .paths import PathTooLongError, SaveVerificationError, count_files, fit_filename, safe_save  # noqa: E402

__all__ = [
    "__version__",
    "BuildQueue",
    "FileLock",
    "PathTooLongError",
    "SaveVerificationError",
    "count_files",
    "fit_filename",
    "safe_save",
]
