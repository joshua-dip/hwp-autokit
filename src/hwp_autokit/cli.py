"""Command line: ``hwp-autokit <command>`` (and ``hwp-pagecheck`` as a shortcut).

Commands
    doctor                 environment checks (Hangul registration, security module, caches)
    pagecheck PATH...      page-overflow check for PDF/HWP/HWPX files or folders
    queue status DIR       show a build queue
    queue run DIR -- CMD   run CMD while holding a turn in the queue
    paths scan DIR         count files on disk and list paths over the limit
    genpy check|rebuild    inspect or rebuild a dedicated gen_py cache
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__
from ._proc import IS_WINDOWS, list_pids


def _out(line: str = "") -> None:
    try:
        print(line, flush=True)
    except UnicodeEncodeError:  # legacy console code page
        enc = sys.stdout.encoding or "ascii"
        print(line.encode(enc, "replace").decode(enc), flush=True)


# ----------------------------------------------------------------- doctor
def _reg_read(root: str, key: str, value: Optional[str] = ""):
    import winreg  # type: ignore

    hive = {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE,
            "HKCR": winreg.HKEY_CLASSES_ROOT}[root]
    with winreg.OpenKey(hive, key) as k:
        if value is None:
            items, i = {}, 0
            while True:
                try:
                    n, v, _ = winreg.EnumValue(k, i)
                except OSError:
                    return items
                items[n] = v
                i += 1
        return winreg.QueryValueEx(k, value)[0]


def cmd_doctor(_: argparse.Namespace) -> int:
    bad = 0

    def report(ok: Optional[bool], what: str, detail: str = "") -> None:
        nonlocal bad
        mark = {True: "ok  ", False: "FAIL", None: "info"}[ok]
        bad += ok is False
        _out(f"[{mark}] {what}" + (f" - {detail}" if detail else ""))

    report(None, "hwp-autokit", __version__)
    report(None, "python", sys.version.split()[0] + " " + sys.executable)
    if not IS_WINDOWS:
        report(False, "platform", "Hangul COM automation needs Windows; only pure helpers work here")
        return 1
    try:
        import win32com  # noqa: F401
        report(True, "pywin32 importable")
    except ImportError:
        report(False, "pywin32", "pip install pywin32")
        return 1
    try:
        clsid = _reg_read("HKCR", r"HWPFrame.HwpObject\CLSID")
        report(True, "Hangul automation object registered", clsid)
    except OSError:
        report(False, "Hangul automation object", "HWPFrame.HwpObject not registered - is Hancom Office installed?")
    try:
        mods = _reg_read("HKCU", r"SOFTWARE\HNC\HwpAutomation\Modules", None)
        if mods:
            for name, dll in mods.items():
                exists = Path(str(dll)).is_file()
                report(exists, f"security module '{name}'", f"{dll}" + ("" if exists else " (file missing)"))
        else:
            report(False, "security module", r"HKCU\SOFTWARE\HNC\HwpAutomation\Modules has no values")
    except OSError:
        report(False, "security module", "not registered - Hangul will ask for file-access approval "
                                          "(see README: security module)")
    try:
        lp = _reg_read("HKLM", r"SYSTEM\CurrentControlSet\Control\FileSystem", "LongPathsEnabled")
        report(None, "LongPathsEnabled", f"{lp} (do not rely on it for Hangul; keep paths <= 250)")
    except OSError:
        report(None, "LongPathsEnabled", "not set")
    r = subprocess.run([sys.executable, "-c", "import win32com; print(win32com.__gen_path__)"],
                       capture_output=True, text=True, errors="replace")
    report(None, "default gen_py cache (shared by all tools)", r.stdout.strip() or r.stderr.strip()[-200:])
    n = len(list_pids("Hwp.exe"))
    report(None, "running Hwp.exe", f"{n}" + (" - someone may be using Hangul; do not kill blindly" if n else ""))
    return 1 if bad else 0


# -------------------------------------------------------------- pagecheck
def _pagecheck_parser(p: argparse.ArgumentParser) -> None:
    p.add_argument("paths", nargs="+", help="files, or folders (not recursive)")
    p.add_argument("--low", type=float, default=0.35, help="a non-last page must be filled at least this far (0-1)")
    p.add_argument("--col", default="0-1", help="horizontal range to measure, e.g. 0-0.72 to skip a right sidebar")
    p.add_argument("--band", default="0.04-0.94", help="vertical range to measure, e.g. 0.04-0.88 to skip a bottom legend")
    p.add_argument("--expect", type=int, help="expected page count")
    p.add_argument("--dpi", type=int, default=40)
    p.add_argument("--engine", choices=["auto", "rhwp", "hangul"], default="auto")
    p.add_argument("--rhwp", help="path to the rhwp executable")
    p.add_argument("--queue-dir", help="take a turn in this build queue before using Hangul")
    p.add_argument("--genpy-cache", help="dedicated gen_py cache name or folder for Hangul rendering")
    p.add_argument("--bad-chars", default="�", help="characters that mean a missing glyph in PDF text")
    p.add_argument("--json", help="write per-page details here")


def cmd_pagecheck(a: argparse.Namespace) -> int:
    from .overflow import check_file, iter_targets, parse_range

    col, band = parse_range(a.col), parse_range(a.band)
    files = iter_targets(a.paths)
    reports, bad = [], 0
    for f in files:
        try:
            r = check_file(f, low=a.low, col=col, band=band, expect=a.expect, dpi=a.dpi, engine=a.engine,
                           rhwp=a.rhwp, queue_dir=a.queue_dir, genpy_cache=a.genpy_cache,
                           bad_chars=a.bad_chars)
        except Exception as e:  # noqa: BLE001 - report and continue with the next file
            bad += 1
            _out(f"!! {f.name}  error: {e}")
            continue
        reports.append(r)
        bad += not r.ok
        _out(f"{'ok' if r.ok else '!!'} {r.file}  {len(r.pages)}p [{r.engine}]  "
             + " | ".join(i.message for i in r.issues))
    _out(f"-- {len(files)} file(s), {bad} with problems")
    if a.json:
        Path(a.json).write_text(json.dumps([r.to_dict() for r in reports], ensure_ascii=False, indent=1),
                                encoding="utf-8")
    return 1 if bad else 0


# ------------------------------------------------------------------ queue
def cmd_queue_status(a: argparse.Namespace) -> int:
    from .lock import BuildQueue

    entries = BuildQueue(a.dir).entries()
    if not entries:
        _out("queue is empty")
    for e in entries:
        _out(f"{e.state:8} P{e.priority} {e.tag:30} pid={e.pid} alive={e.alive} created={e.created} {e.label}")
    return 0


def cmd_queue_run(a: argparse.Namespace) -> int:
    from .lock import BuildQueue

    cmd = list(a.command)
    if not cmd:
        _out("nothing to run: hwp-autokit queue run DIR --tag T -- command args")
        return 2
    q = BuildQueue(a.dir, poll=a.poll)
    last = [None]

    def note(pos, reason):
        if reason != last[0]:
            _out(f"[queue] {reason}")
            last[0] = reason

    with q.turn(a.tag, label=a.label, priority=a.priority, max_wait=a.max_wait * 60,
                foreign_wait=a.foreign_wait, on_wait=note):
        _out(f"[queue] turn acquired; running: {' '.join(cmd)}")
        return subprocess.call(cmd)


# ------------------------------------------------------------------ paths
def cmd_paths_scan(a: argparse.Namespace) -> int:
    from .paths import count_files, find_long_paths

    n = count_files(a.dir, a.pattern, recursive=a.recursive)
    _out(f"{n} file(s) matching {a.pattern!r} in {a.dir}" + (" (recursive)" if a.recursive else ""))
    long_ = find_long_paths(a.dir, a.limit, recursive=True)
    for p in long_:
        _out(f"  LONG {len(p)}: {p}")
    if long_:
        _out(f"{len(long_)} path(s) over {a.limit} chars - many tools (and Hangul) cannot see or write these")
    return 1 if long_ else 0


# ------------------------------------------------------------------ genpy
def cmd_genpy(a: argparse.Namespace) -> int:
    from .session import check_genpy_cache, default_cache_root, rebuild_genpy_cache

    root = Path(a.root) if a.root else default_cache_root(a.name)
    if a.action == "path":
        _out(str(root))
        return 0
    if a.action == "rebuild":
        root.mkdir(parents=True, exist_ok=True)
        marker = root / ".hwp-autokit-cache"
        if not marker.exists() and not os.listdir(root):
            marker.write_text("hwp-autokit dedicated gen_py cache root\n", encoding="utf-8")
        ok, msg = rebuild_genpy_cache(root)
    else:
        ok, msg = check_genpy_cache(root)
    _out(("ok   " if ok else "FAIL ") + msg)
    return 0 if ok else 1


# ------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="hwp-autokit", description="Hancom Hangul automation helpers")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="check the automation environment").set_defaults(fn=cmd_doctor)

    pc = sub.add_parser("pagecheck", help="find empty / under-filled pages")
    _pagecheck_parser(pc)
    pc.set_defaults(fn=cmd_pagecheck)

    q = sub.add_parser("queue", help="serial build queue").add_subparsers(dest="qcmd", required=True)
    qs = q.add_parser("status")
    qs.add_argument("dir")
    qs.set_defaults(fn=cmd_queue_status)
    qr = q.add_parser("run")
    qr.add_argument("dir")
    qr.add_argument("--tag", required=True)
    qr.add_argument("--label", default="")
    qr.add_argument("--priority", type=int, default=5, help="0 (urgent) - 9; default 5")
    qr.add_argument("--max-wait", type=float, default=360, help="minutes")
    qr.add_argument("--foreign-wait", type=float, default=1800, help="seconds to wait for Hangul outside the queue")
    qr.add_argument("--poll", type=float, default=3.0)
    qr.set_defaults(fn=cmd_queue_run, command=[])  # the command comes after "--"

    pth = sub.add_parser("paths", help="long-path checks").add_subparsers(dest="pcmd", required=True)
    ps = pth.add_parser("scan")
    ps.add_argument("dir")
    ps.add_argument("--pattern", default="*")
    ps.add_argument("--recursive", action="store_true")
    ps.add_argument("--limit", type=int, default=250)
    ps.set_defaults(fn=cmd_paths_scan)

    g = sub.add_parser("genpy", help="dedicated win32com gen_py cache")
    g.add_argument("action", choices=["path", "check", "rebuild"])
    g.add_argument("--name", default="default")
    g.add_argument("--root", help="cache root folder (overrides --name)")
    g.set_defaults(fn=cmd_genpy)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    command: List[str] = []
    if "--" in args:
        i = args.index("--")
        args, command = args[:i], args[i + 1:]
    a = build_parser().parse_args(args)
    if command:
        a.command = command
    return a.fn(a)


def pagecheck_main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="hwp-pagecheck", description="find empty / under-filled pages")
    _pagecheck_parser(ap)
    return cmd_pagecheck(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
