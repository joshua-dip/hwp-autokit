"""A Hangul COM session that assumes Hangul *will* crash, and quits cleanly.

What this encodes (all learned the hard way on multi-thousand-file batches):

* **Isolate the win32com ``gen_py`` cache per tool.** Two Python processes building
  the cache at the same time leave a half-written module behind; every later
  ``Dispatch`` then fails with ``... has no attribute 'MinorVersion'`` or
  ``'CLSIDToClassMap'``. A broken cache never heals inside the same process, so
  :func:`rebuild_genpy_cache` rebuilds it in a *child* process. It also refuses to
  delete the shared default cache that other tools may be using.
* **Reuse one instance** across documents (``FileClose`` between them). Creating and
  quitting per file is slower and less stable. Recycle proactively every N documents
  (e.g. 200) instead of waiting for the crash.
* **On a dead connection** (``RPC server is unavailable`` and friends) every later call
  fails too. Restart once, then retry the document.
* **Do not kill on every retry.** Killing Hangul while it is starting produces
  ``Server execution failed`` forever. Clean up only after the first failure, then
  just wait 6, 10, 14, 18, 22 s.
* **Never kill other people's Hangul.** We record the PID that appeared when *we*
  started Hangul and only ever kill that one.
* A suspiciously short result (e.g. 15-45 characters of text from a normal document)
  is a sign the instance is dying, not that the file is corrupt. Raise
  :class:`SuspectOutputError` from your callback and :meth:`HwpSession.call` restarts.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional, Tuple, TypeVar, Union

from . import paths as _paths
from ._proc import CREATE_NO_WINDOW, IS_WINDOWS, kill_pid, list_pids, wait_pid_exit

log = logging.getLogger("hwp_autokit.session")

T = TypeVar("T")
PathLike = Union[str, "os.PathLike[str]"]

HWP_PROGID = "HWPFrame.HwpObject"
HWP_IMAGE = "Hwp.exe"
#: Type library of the Hangul automation object (CLSID, LCID, major, minor).
HWP_TYPELIB: Tuple[str, int, int, int] = ("{7D2B6F3C-1D95-4E0C-BF5A-5EE564186FBC}", 0, 1, 0)
#: The file-path-checker module name/registry value most installations use. Registering
#: it suppresses the "allow this program to access files?" prompt (see README).
DEFAULT_SECURITY_MODULE: Tuple[str, str] = ("FilePathCheckDLL", "SecurityModule")
#: Value used by our production converters for ``SetMessageBoxMode`` (auto-answer
#: message boxes so a dialog never blocks an unattended run).
DEFAULT_MESSAGE_BOX_MODE = 0x00020000

#: HRESULTs that mean the COM server is gone and every further call will fail.
DEAD_HRESULTS = {
    -2147023174,  # 0x800706BA RPC_S_SERVER_UNAVAILABLE "The RPC server is unavailable"
    -2147023170,  # 0x800706BE RPC_S_CALL_FAILED
    -2147417848,  # 0x80010108 RPC_E_DISCONNECTED "The object invoked has disconnected"
    -2146959355,  # 0x80080005 CO_E_SERVER_EXEC_FAILURE "Server execution failed"
}
_MARKER = ".hwp-autokit-cache"

__all__ = [
    "HwpSession",
    "SuspectOutputError",
    "GenPyIsolationError",
    "HwpStartError",
    "HwpTimeout",
    "is_dead_com_error",
    "isolate_genpy_cache",
    "default_cache_root",
    "isolated_env",
    "check_genpy_cache",
    "rebuild_genpy_cache",
    "HWP_TYPELIB",
    "DEFAULT_SECURITY_MODULE",
]


class SuspectOutputError(RuntimeError):
    """Raise from a :meth:`HwpSession.call` callback when output looks like a dying instance."""


class GenPyIsolationError(RuntimeError):
    pass


class HwpStartError(RuntimeError):
    pass


class HwpTimeout(TimeoutError):
    """A Hangul call blocked past :meth:`HwpSession.watchdog`'s limit; the process was killed."""


def is_dead_com_error(exc: BaseException) -> bool:
    """True if ``exc`` means the Hangul COM server is gone (restart, don't just retry)."""
    codes = []
    hr = getattr(exc, "hresult", None)
    if isinstance(hr, int):
        codes.append(hr)
    args = getattr(exc, "args", ()) or ()
    if args and isinstance(args[0], int):
        codes.append(args[0])
    if len(args) >= 3 and isinstance(args[2], tuple) and len(args[2]) >= 6 and isinstance(args[2][5], int):
        codes.append(args[2][5])  # pywintypes.com_error excepinfo scode
    if any(c in DEAD_HRESULTS for c in codes):
        return True
    text = str(exc)
    return any(s in text for s in ("RPC server", "RPC 서버", "CoInitialize", "disconnected from its clients"))


# ----------------------------------------------------------------- gen_py cache
def default_cache_root(name: str = "default") -> Path:
    """``<system temp>/hwp-autokit/<name>`` - give each concurrently running tool its own name."""
    base = os.environ.get("HWP_AUTOKIT_CACHE_ROOT") or os.path.join(tempfile.gettempdir(), "hwp-autokit")
    return Path(base) / name


def _gen_dir(root: Path) -> Path:
    return root / "gen_py" / f"{sys.version_info.major}.{sys.version_info.minor}"


def isolated_env(root: PathLike, base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Environment for a child process whose win32com cache lives under ``root``.

    win32com puts ``gen_py`` under ``GetTempPath()``, which follows ``TMP``/``TEMP``.
    This is the most robust isolation: launch each batch runner with this env.
    """
    env = dict(os.environ if base is None else base)
    env["TMP"] = env["TEMP"] = str(Path(root))
    env.setdefault("PYTHONIOENCODING", "utf-8")  # emoji/Korean logs on a cp949 console
    return env


def isolate_genpy_cache(root: Optional[PathLike] = None, *, name: str = "default",
                        set_env: bool = True) -> Path:
    """Point this process's win32com ``gen_py`` cache at a dedicated folder.

    Must run before ``win32com.client`` is imported. Returns the gen_py directory.
    With ``set_env`` (default) ``TMP``/``TEMP`` are also redirected, so child
    processes inherit the same isolation.
    """
    r = Path(root) if root else default_cache_root(name)
    r.mkdir(parents=True, exist_ok=True)
    (r / _MARKER).write_text("hwp-autokit dedicated gen_py cache root\n", encoding="utf-8")
    gen = _gen_dir(r)
    if "win32com.client" in sys.modules:
        import win32com  # type: ignore

        if Path(win32com.__gen_path__).resolve() != gen.resolve():
            raise GenPyIsolationError(
                "win32com.client was imported before isolate_genpy_cache(); its cache is "
                f"{win32com.__gen_path__}. Call isolate_genpy_cache() first, or launch the "
                "process with isolated_env().")
        return gen
    if set_env:
        os.environ["TMP"] = os.environ["TEMP"] = str(r)
    import win32com  # type: ignore

    gen.mkdir(parents=True, exist_ok=True)
    win32com.__gen_path__ = str(gen)
    sys.modules["win32com.gen_py"].__path__ = [str(gen)]  # type: ignore[attr-defined]
    return gen


_CHECK_SNIPPET = r"""
import sys, win32com
from win32com.client import gencache
clsid, lcid, major, minor = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
mode = sys.argv[5]
try:
    if mode == "build":
        mod = gencache.EnsureModule(clsid, lcid, major, minor)
    else:
        mod = gencache.GetModuleForTypelib(clsid, lcid, major, minor)
except Exception as e:
    print("ERR", type(e).__name__, e); sys.exit(2)
ok = mod is not None and hasattr(mod, "MinorVersion") and hasattr(mod, "CLSIDToClassMap")
print("OK" if ok else "BROKEN", win32com.__gen_path__)
sys.exit(0 if ok else 1)
"""


def _run_cache_child(root: Path, mode: str, timeout: float) -> Tuple[bool, str]:
    cmd = [sys.executable, "-c", _CHECK_SNIPPET, *map(str, HWP_TYPELIB), mode]
    r = subprocess.run(cmd, env=isolated_env(root), capture_output=True, text=True, errors="replace",
                       timeout=timeout, creationflags=CREATE_NO_WINDOW)
    msg = (r.stdout + r.stderr).strip().splitlines()
    return r.returncode == 0, (msg[-1] if msg else f"exit {r.returncode}")


def check_genpy_cache(root: PathLike, timeout: float = 120) -> Tuple[bool, str]:
    """Check (in a child process) that the Hangul wrapper module in ``root`` imports cleanly."""
    return _run_cache_child(Path(root), "check", timeout)


def rebuild_genpy_cache(root: PathLike, timeout: float = 300) -> Tuple[bool, str]:
    """Delete and regenerate the Hangul wrapper module under a *dedicated* cache root.

    Stop every process using that cache first: a process that already imported a broken
    module keeps using it no matter what is on disk. Refuses to touch a folder that was
    not created by :func:`isolate_genpy_cache` (never wipes the shared default cache).
    """
    r = Path(root)
    if not (r / _MARKER).exists():
        raise GenPyIsolationError(
            f"{r} is not an hwp-autokit cache root (no {_MARKER}); refusing to delete a cache "
            "other tools may share. Use a dedicated root.")
    gen = _gen_dir(r)
    if gen.exists():
        shutil.rmtree(gen)
    gen.mkdir(parents=True, exist_ok=True)
    return _run_cache_child(r, "build", timeout)


# ---------------------------------------------------------------------- session
class HwpSession:
    """One Hangul instance, reused across documents, restarted when it dies.

    >>> with HwpSession(genpy_cache="my-tool") as s:
    ...     s.open("in.hwpx")
    ...     s.save_as("out.hwp", "HWP")
    """

    def __init__(self, *, visible: bool = False,
                 security_module: Optional[Tuple[str, str]] = DEFAULT_SECURITY_MODULE,
                 message_box_mode: Optional[int] = DEFAULT_MESSAGE_BOX_MODE,
                 genpy_cache: Union[None, bool, str, PathLike] = None,
                 recycle_every: int = 200, start_attempts: int = 6,
                 quit_timeout: float = 15.0, early_binding: bool = False):
        if not IS_WINDOWS:
            raise HwpStartError("Hangul COM automation needs Windows")
        # genpy_cache: True -> default dedicated cache; "name" -> named cache under the temp
        # dir; a path -> that folder. None/False leaves win32com's cache as it is.
        if genpy_cache is True:
            isolate_genpy_cache()
        elif genpy_cache:
            s = os.fspath(genpy_cache)
            if any(c in s for c in "/\\:"):
                isolate_genpy_cache(s)
            else:
                isolate_genpy_cache(name=s)
        self.visible = visible
        self.security_module = security_module
        self.message_box_mode = message_box_mode
        self.recycle_every = recycle_every
        self.start_attempts = start_attempts
        self.quit_timeout = quit_timeout
        self.early_binding = early_binding
        self.security_registered: Optional[bool] = None
        self.documents = 0
        self.restarts = 0
        self._hwp: Any = None
        self._pid: Optional[int] = None

    # -- lifecycle ----------------------------------------------------------
    @property
    def hwp(self) -> Any:
        if self._hwp is None:
            self.start()
        return self._hwp

    @property
    def pid(self) -> Optional[int]:
        """PID of the Hangul process this session started (None if it could not be identified)."""
        return self._pid

    def _spawn(self) -> None:
        import win32com.client as w  # type: ignore

        before = list_pids(HWP_IMAGE)
        try:
            obj = w.gencache.EnsureDispatch(HWP_PROGID) if self.early_binding else w.DispatchEx(HWP_PROGID)
        finally:
            new = list_pids(HWP_IMAGE) - before
            self._pid = new.pop() if len(new) == 1 else None
        self._hwp = obj
        if self.security_module:
            self.security_registered = bool(obj.RegisterModule(*self.security_module))
            if not self.security_registered:
                log.warning("security module %s not registered - Hangul may show a file-access "
                            "prompt and block. See README 'security module'.", self.security_module)
        if self.message_box_mode is not None:
            obj.SetMessageBoxMode(self.message_box_mode)
        try:
            obj.XHwpWindows.Item(0).Visible = self.visible
        except Exception:  # noqa: BLE001 - not fatal; some versions have no window yet
            pass

    def start(self) -> "HwpSession":
        if self._hwp is not None:
            return self
        last: Optional[BaseException] = None
        for k in range(self.start_attempts):
            try:
                self._spawn()
                return self
            except Exception as e:  # noqa: BLE001
                last = e
                log.warning("Hangul start failed (attempt %d/%d): %s", k + 1, self.start_attempts, e)
                if k == 0 and self._pid:  # clean up our own half-started instance once
                    kill_pid(self._pid)
                self._hwp, self._pid = None, None
                if k + 1 < self.start_attempts:
                    time.sleep(6 + 4 * k)
        raise HwpStartError(f"could not start Hangul after {self.start_attempts} attempts: {last}")

    def quit(self) -> None:
        """Discard the document, quit Hangul, and make sure *our* process is gone."""
        obj, pid = self._hwp, self._pid
        self._hwp, self._pid = None, None
        if obj is not None:
            for step in (lambda: obj.Clear(1), lambda: obj.Quit()):
                try:
                    step()
                except Exception as e:  # noqa: BLE001
                    log.debug("quit step failed: %s", e)
            del obj
        if pid and not wait_pid_exit(pid, self.quit_timeout):
            log.warning("Hangul pid %s did not exit in %.0fs; killing that pid only", pid, self.quit_timeout)
            kill_pid(pid)

    def restart(self, pause: float = 4.0) -> None:
        self.quit()
        self.restarts += 1
        time.sleep(pause)
        self.start()

    def __enter__(self) -> "HwpSession":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.quit()

    # -- documents ----------------------------------------------------------
    @staticmethod
    def _format_for(path: PathLike) -> str:
        return {".hwpx": "HWPX", ".hwp": "HWP"}.get(Path(path).suffix.lower(), "")

    def open(self, path: PathLike, fmt: Optional[str] = None, options: str = "forceopen:true") -> None:
        _paths.check_path(path, _paths.WINDOWS_MAX_PATH - 1)
        if not Path(_paths.extended_path(path)).exists():
            raise FileNotFoundError(path)
        if not self.hwp.Open(str(Path(path).resolve()), fmt if fmt is not None else self._format_for(path), options):
            raise RuntimeError(f"Hangul could not open {path}")

    def close_document(self) -> None:
        """Close the current document without saving and count it toward recycling."""
        try:
            self.hwp.Run("FileClose")
        except Exception as e:  # noqa: BLE001
            if is_dead_com_error(e):
                raise
        self.tick()

    def tick(self) -> None:
        """Count one processed document; restart Hangul every ``recycle_every`` documents
        (proactive replacement beats waiting for the crash)."""
        self.documents += 1
        if self.recycle_every and self.documents % self.recycle_every == 0:
            log.info("recycling Hangul after %d documents", self.documents)
            self.restart(pause=2.0)

    def save_as(self, path: PathLike, fmt: Optional[str] = None, arg: str = "", **verify) -> Path:
        """``SaveAs`` with a length pre-check and an on-disk post-check (see :mod:`hwp_autokit.paths`)."""
        f = fmt if fmt is not None else (self._format_for(path) or "HWP")
        return _paths.safe_save(lambda p: self.hwp.SaveAs(p, f, arg), Path(path).absolute(), **verify)

    @contextmanager
    def watchdog(self, seconds: float) -> Iterator[None]:
        """Kill *our* Hangul process if the block is still running after ``seconds``.

        Some calls block forever without an error (see :mod:`hwp_autokit.text`). Killing
        the server makes the blocked COM call return with a dead-connection error, which
        is re-raised as :class:`HwpTimeout`; the session starts a fresh Hangul on next use.
        Needs :attr:`pid` (known when this session started the process).
        """
        pid = self._pid
        if pid is None:
            log.warning("watchdog: Hangul pid unknown - a blocked call cannot be interrupted")
        fired = threading.Event()

        def fire() -> None:
            fired.set()
            if pid:
                log.warning("watchdog: call still blocked after %.0fs; killing Hangul pid %s", seconds, pid)
                kill_pid(pid)

        timer = threading.Timer(seconds, fire)
        timer.daemon = True
        timer.start()
        try:
            yield
        except Exception as e:  # noqa: BLE001
            if fired.is_set():
                self._hwp, self._pid = None, None
                raise HwpTimeout(f"Hangul call blocked for more than {seconds:.0f}s (process killed)") from e
            raise
        finally:
            timer.cancel()
        if fired.is_set():
            self._hwp, self._pid = None, None

    def call(self, fn: Callable[..., T], *args: Any, retries: int = 1, **kwargs: Any) -> T:
        """Run ``fn(hwp, *args, **kwargs)``; on a dead connection or :class:`SuspectOutputError`
        restart Hangul and retry (``retries`` times). Your ``fn`` must be safe to repeat."""
        for attempt in range(retries + 1):
            try:
                return fn(self.hwp, *args, **kwargs)
            except Exception as e:  # noqa: BLE001
                if attempt < retries and (isinstance(e, SuspectOutputError) or is_dead_com_error(e)):
                    log.warning("Hangul looks dead (%s); restarting and retrying", e)
                    self.restart()
                    continue
                raise
        raise AssertionError("unreachable")
