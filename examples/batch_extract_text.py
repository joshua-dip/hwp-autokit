"""Extract text (with underlines) from many HWP files, surviving Hangul crashes.

    python examples/batch_extract_text.py <folder with .hwp files> <output folder>

Patterns shown:
* one Hangul instance reused for all files, recycled every 200 documents;
* restart-and-retry when the COM server dies (``session.call``);
* a suspiciously short result is treated as "Hangul is dying", not "file is corrupt";
* at the end, count the output files on disk instead of trusting the counters.
"""
import sys
from pathlib import Path

from hwp_autokit.paths import count_files, fit_filename
from hwp_autokit.session import HwpSession, SuspectOutputError
from hwp_autokit.text import copy_document_text

src, dst = Path(sys.argv[1]), Path(sys.argv[2])
dst.mkdir(parents=True, exist_ok=True)
files = sorted(src.glob("*.hwp"))
MIN_CHARS = 50  # tune per corpus: what a healthy document always exceeds


def extract(hwp, path):
    hwp.Open(str(path.resolve()), "HWP", "forceopen:true")
    try:
        text = copy_document_text(hwp)
    finally:
        hwp.Run("FileClose")
    if len(text) < MIN_CHARS:
        raise SuspectOutputError(f"only {len(text)} chars from {path.name}")
    return text


ok = failed = 0
with HwpSession(genpy_cache="batch-extract", recycle_every=200) as s:
    for i, f in enumerate(files, 1):
        try:
            with s.watchdog(180):
                text = s.call(extract, f, retries=1)
        except Exception as e:  # noqa: BLE001 - log and continue with the next file
            failed += 1
            print(f"[{i}/{len(files)}] FAIL {f.name}: {e}")
            continue
        out = dst / fit_filename(dst, f.stem, ".txt")   # never exceed the path budget
        out.write_text(text, encoding="utf-8")
        ok += 1
        s.tick()                                          # counts toward recycling

on_disk = count_files(dst, "*.txt")
print(f"ok={ok} failed={failed} restarts={s.restarts} files on disk={on_disk}")
if on_disk != ok:
    print("WARNING: the log and the disk disagree - check for long paths: hwp-autokit paths scan", dst)
