"""Page-layout checker: render pages, then measure ink instead of eyeballing.

Finds, per file:

empty page
    almost no ink inside the body band - a page-break accident (e.g. a table pushed
    whole onto the next page, leaving a blank one).
under-filled page
    a page that is *not* the last one but whose text ends above ``low`` of the page
    height - e.g. one sentence per page because a block did not fit.
bad glyphs (PDF)
    replacement characters in the extracted text - a missing font.
page count
    differs from ``expect``.

"Where does the text end" is measured with *dark* pixels only (``text_threshold``).
Measuring any ink would let light boxes, rules and note lines "fill" the page and
hide an under-filled one.

Rendering engines
    ``pdf``     PyMuPDF (``pip install hwp-autokit[pdf]``).
    ``hangul``  Hangul itself over COM (``CreatePageImage``) - exact, but one file at
                a time; pass ``queue_dir`` to take a turn in the build queue.
    ``rhwp``    the open-source ``rhwp`` renderer, no Hangul needed, parallel-safe.
                In our tests line/page breaks matched Hangul for plain documents, but
                it drops *ruby text* (덧말, dutmal) together with the word it sits on,
                so such documents must go through Hangul.
    ``auto``    rhwp if available and the document has no ruby text, else Hangul.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Sequence, Tuple, Union

from ._proc import CREATE_NO_WINDOW

PathLike = Union[str, "os.PathLike[str]"]
Range = Tuple[float, float]

INK_THRESHOLD = 235    # darker than this counts as ink (empty-page test)
TEXT_THRESHOLD = 150   # darker than this counts as text (where-does-it-end test)
DEFAULT_BAND: Range = (0.04, 0.94)  # skip header/footer
DEFAULT_COL: Range = (0.0, 1.0)
EMPTY_INK = 0.0015     # body-band ink fraction below which a page is "empty"
DEFAULT_LOW = 0.35
DEFAULT_BAD_CHARS = "\ufffd"
SUPPORTED = (".pdf", ".hwp", ".hwpx")

__all__ = [
    "PageStats",
    "Issue",
    "FileReport",
    "measure_page",
    "find_issues",
    "check_file",
    "check_paths",
    "has_ruby_text",
    "find_rhwp",
    "parse_range",
]


@dataclass
class PageStats:
    ink: float          # fraction of sampled body-band pixels that are ink
    bottom: float       # last text row, as a fraction of page height (0 = no text)
    bad_chars: int = 0


@dataclass
class Issue:
    page: Optional[int]  # 1-based; None for file-level issues
    kind: str            # empty | underfilled | bad_glyphs | page_count
    message: str


@dataclass
class FileReport:
    file: str
    engine: str
    pages: List[PageStats] = field(default_factory=list)
    issues: List[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict:
        return {"file": self.file, "engine": self.engine, "page_count": len(self.pages),
                "issues": [asdict(i) for i in self.issues], "pages": [asdict(p) for p in self.pages]}


def parse_range(text: str) -> Range:
    """``"0-0.72"`` -> ``(0.0, 0.72)``."""
    a, b = (float(v) for v in text.split("-", 1))
    if not (0 <= a < b <= 1):
        raise ValueError(f"range must satisfy 0 <= a < b <= 1: {text}")
    return a, b


# ------------------------------------------------------------------ the math
def measure_page(img, col: Range = DEFAULT_COL, band: Range = DEFAULT_BAND, *,
                 ink_threshold: int = INK_THRESHOLD, text_threshold: int = TEXT_THRESHOLD,
                 step: int = 2, min_dark: int = 2) -> PageStats:
    """Measure one rendered page (any PIL image).

    ``col``/``band`` select the horizontal/vertical part of the page to measure, as
    fractions. Use ``col`` to exclude an always-full sidebar and ``band`` to exclude a
    legend that is always at the bottom. Every ``step``-th column is sampled; a row
    counts as text if it has at least ``min_dark`` dark samples.
    """
    g = img.convert("L")
    w, h = g.size
    x0, x1 = int(w * col[0]), max(int(w * col[0]) + 1, int(w * col[1]))
    y0, y1 = int(h * band[0]), max(int(h * band[0]) + 1, int(h * band[1]))
    part = g.crop((x0, y0, x1, y1))
    pw, ph = part.size
    data = part.tobytes()
    ink_tab = bytes(1 if v < ink_threshold else 0 for v in range(256))
    txt_tab = bytes(1 if v < text_threshold else 0 for v in range(256))
    ink = 0
    last_text = -1
    for y in range(ph):
        row = data[y * pw:(y + 1) * pw:step]
        ink += row.translate(ink_tab).count(1)
        if row.translate(txt_tab).count(1) >= min_dark:
            last_text = y
    samples = max(1, len(range(0, pw, step)) * ph)
    bottom = (last_text + y0) / h if last_text >= 0 else 0.0
    return PageStats(ink=round(ink / samples, 5), bottom=round(bottom, 4))


def find_issues(pages: Sequence[PageStats], *, low: float = DEFAULT_LOW, empty_ink: float = EMPTY_INK,
                expect: Optional[int] = None) -> List[Issue]:
    """Turn page measurements into issues. The last page is allowed to be short."""
    issues: List[Issue] = []
    n = len(pages)
    for i, st in enumerate(pages, 1):
        if st.ink < empty_ink:
            issues.append(Issue(i, "empty", f"page {i} is empty"))
        elif i < n and st.bottom < low:
            issues.append(Issue(i, "underfilled", f"page {i} ends at {st.bottom:.0%} (< {low:.0%})"))
        if st.bad_chars:
            issues.append(Issue(i, "bad_glyphs", f"page {i} has {st.bad_chars} replacement glyph(s)"))
    if expect is not None and n != expect:
        issues.append(Issue(None, "page_count", f"{n} pages (expected {expect})"))
    return issues


# --------------------------------------------------------------- renderers
def find_rhwp(explicit: Optional[PathLike] = None) -> Optional[Path]:
    """rhwp executable: argument, ``HWP_AUTOKIT_RHWP`` env var, or ``PATH``."""
    for cand in (explicit, os.environ.get("HWP_AUTOKIT_RHWP"), shutil.which("rhwp")):
        if cand and Path(cand).is_file():
            return Path(cand)
    return None


def default_font_dirs() -> List[str]:
    dirs = [os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(os.path.join(local, "Microsoft", "Windows", "Fonts"))
    for pf in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if pf:
            dirs += [str(p) for p in Path(pf).glob("HNC/Office*/HOffice*/Shared/TTF")]
    return [d for d in dirs if os.path.isdir(d)]


def has_ruby_text(path: PathLike, rhwp: Optional[PathLike] = None) -> bool:
    """Does the document use ruby text (덧말)? HWPX: ``<hp:dutmal`` in a section XML.
    HWP 5: control id ``tdut`` (bytes ``74 75 64 74``) in ``rhwp dump-records``.
    If it cannot be determined, returns True (the safe answer: render with Hangul)."""
    p = Path(path)
    if p.suffix.lower() == ".hwpx":
        try:
            with zipfile.ZipFile(p) as z:
                return any(b"<hp:dutmal" in z.read(n) for n in z.namelist()
                           if n.startswith("Contents/section"))
        except (OSError, zipfile.BadZipFile):
            return True
    exe = find_rhwp(rhwp)
    if not exe:
        return True
    r = subprocess.run([str(exe), "dump-records", str(p)], capture_output=True, timeout=120,
                       creationflags=CREATE_NO_WINDOW)
    return r.returncode != 0 or b"74 75 64 74" in r.stdout


def _pdf_pages(path: Path, dpi: int, bad_chars: str) -> Iterator[Tuple[object, int]]:
    try:
        import fitz  # type: ignore  # PyMuPDF
    except ImportError as e:
        raise RuntimeError("PDF checks need PyMuPDF: pip install hwp-autokit[pdf]") from e
    from PIL import Image

    with fitz.open(path) as doc:
        for page in doc:
            pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
            img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
            text = page.get_text()
            yield img, sum(text.count(c) for c in bad_chars)


def _rhwp_pages(path: Path, dpi: int, exe: Path, font_dirs: Sequence[str]) -> Iterator[Tuple[object, int]]:
    from PIL import Image

    tmp = Path(tempfile.mkdtemp(prefix="hak_rhwp_"))
    try:
        src = tmp / ("doc" + path.suffix.lower())  # ASCII name: avoids issues with spaces/Hangul names
        shutil.copyfile(path, src)
        # Keep scale >= 1 (96 dpi): rhwp's fallback fonts are thin and blur at low dpi,
        # which shows up as false "under-filled" alarms.
        args = [str(exe), "export-png", str(src), "-o", str(tmp / "out"), "--profile", "screen",
                "--scale", f"{max(1.0, dpi / 96):.3f}"]
        for d in font_dirs:
            args += ["--font-path", d]
        r = subprocess.run(args, capture_output=True, timeout=600, creationflags=CREATE_NO_WINDOW)
        pngs = sorted((tmp / "out").glob("*.png"), key=lambda p: (len(p.stem), p.stem))
        if r.returncode or not pngs:
            raise RuntimeError(f"rhwp failed for {path.name}: {r.stderr.decode('utf-8', 'replace')[-300:]}")
        for p in pngs:
            with Image.open(p) as im:
                yield im.copy(), 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _hangul_pages(path: Path, dpi: int, queue_dir: Optional[PathLike],
                  genpy_cache: Union[None, bool, str]) -> Iterator[Tuple[object, int]]:
    from contextlib import nullcontext

    from PIL import Image

    from .lock import BuildQueue
    from .session import HwpSession

    tmp = Path(tempfile.mkdtemp(prefix="hak_hwp_"))
    try:
        turn = BuildQueue(queue_dir).turn("pagecheck") if queue_dir else nullcontext()
        with turn:
            with HwpSession(genpy_cache=genpy_cache) as s:
                s.open(path)
                n = s.hwp.PageCount
                for i in range(n):
                    if not s.hwp.CreatePageImage(str(tmp / f"p{i:04d}.bmp"), i, dpi, 24, "bmp"):
                        raise RuntimeError(f"CreatePageImage failed on page {i + 1} of {path.name}")
        for i in range(n):
            with Image.open(tmp / f"p{i:04d}.bmp") as im:
                yield im.copy(), 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def pick_engine(path: PathLike, engine: str = "auto", rhwp: Optional[PathLike] = None) -> str:
    p = Path(path)
    if p.suffix.lower() == ".pdf":
        return "pdf"
    if engine != "auto":
        return engine
    return "rhwp" if find_rhwp(rhwp) and not has_ruby_text(p, rhwp) else "hangul"


def check_file(path: PathLike, *, low: float = DEFAULT_LOW, col: Range = DEFAULT_COL,
               band: Range = DEFAULT_BAND, expect: Optional[int] = None, dpi: int = 40,
               engine: str = "auto", rhwp: Optional[PathLike] = None,
               font_dirs: Optional[Sequence[str]] = None, queue_dir: Optional[PathLike] = None,
               genpy_cache: Union[None, bool, str] = None, bad_chars: str = DEFAULT_BAD_CHARS,
               empty_ink: float = EMPTY_INK) -> FileReport:
    """Render ``path`` and return its :class:`FileReport`."""
    p = Path(path)
    eng = pick_engine(p, engine, rhwp)
    if eng == "pdf":
        gen: Iterable = _pdf_pages(p, dpi, bad_chars)
    elif eng == "rhwp":
        exe = find_rhwp(rhwp)
        if not exe:
            raise RuntimeError("rhwp not found: pass --rhwp, set HWP_AUTOKIT_RHWP, or put it on PATH")
        gen = _rhwp_pages(p, dpi, exe, default_font_dirs() if font_dirs is None else font_dirs)
    elif eng == "hangul":
        gen = _hangul_pages(p, dpi, queue_dir, genpy_cache)
    else:
        raise ValueError(f"unknown engine {eng!r}")
    pages = []
    for img, bad in gen:
        st = measure_page(img, col, band)
        st.bad_chars = bad
        pages.append(st)
    return FileReport(p.name, eng, pages, find_issues(pages, low=low, empty_ink=empty_ink, expect=expect))


def iter_targets(paths: Iterable[PathLike]) -> List[Path]:
    """Files given directly, plus supported files directly inside given folders (not recursive)."""
    out: List[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            out += sorted(f for f in p.iterdir() if f.is_file() and f.suffix.lower() in SUPPORTED)
        else:
            out.append(p)
    return out


def check_paths(paths: Iterable[PathLike], **kwargs) -> List[FileReport]:
    return [check_file(f, **kwargs) for f in iter_targets(paths)]
