"""Putting text into Hangul and getting it back out without losing marks.

Insertion (verified on Hancom Office 2022 / Hangul 12)
    :func:`insert_text`    plain text at the caret (``InsertText`` action, one paragraph
                           per line). Fast and exact.
    :func:`insert_marked`  plain text with ``<b>``/``<i>``/``<u>`` marks at the caret, by
                           toggling ``CharShapeBold/Italic/Underline`` around
                           ``InsertText``. Exact, spacing preserved. **Preferred.**
    :func:`set_html`       replace the whole document with HTML
                           (``SetTextFile(html, "HTML", "")``).
    :func:`insert_html`    HTML at the caret: ``set_html`` into a scratch document,
                           then Hangul's own Copy/Paste. Overwrites the clipboard.

    Gotchas found while testing (see README):

    * ``SetTextFile(data, "HTML" | "TEXT", "insertfile")`` - and ``Insert`` of an .htm
      file, and pasting CF_HTML - **block forever** (no error, no visible dialog).
      ``SetTextFile(data, "UNICODE", "insertfile")`` works. Wrap risky calls in
      :meth:`hwp_autokit.session.HwpSession.watchdog`.
    * Hangul's HTML importer **drops whitespace at inline-tag boundaries**
      (``<u>a</u> b`` becomes ``ab``) and **garbles Korean if the HTML declares**
      ``<meta charset="utf-8">`` (omit the meta; the string is passed as Unicode).
      Hence :func:`insert_marked` for anything where exact text matters.

Extraction - why two clipboard formats
    ``Copy`` puts both plain text and HTML (CF_HTML) on the clipboard, and neither is
    complete on its own:

    * underline (and other run formatting) exists **only in the HTML**
      (``<u style="text-underline:...">``);
    * automatic numbering (``1.`` ``2.`` and bullets such as ``★``) exists **only in
      the plain text** - in the HTML those characters are simply absent. Using the HTML
      alone once collapsed 26 numbered items into 3.

    So :func:`graft_marks` takes the plain text as the skeleton and grafts the HTML's
    marked fragments onto it, in order. The clipboard is also intermittently locked by
    other programs, so every read retries.
"""
from __future__ import annotations

import html as _html
import re
import time
from typing import Any, Iterable, Optional, Sequence, Tuple

__all__ = [
    "insert_text",
    "insert_marked",
    "split_marked",
    "set_html",
    "insert_html",
    "get_text",
    "copy_document_text",
    "strip_cf_html_header",
    "html_to_marked_text",
    "graft_marks",
]


# --------------------------------------------------------------------- insert
def insert_text(hwp: Any, text: str) -> None:
    """Insert plain ``text`` at the caret; ``\\n`` becomes a paragraph break."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for i, line in enumerate(lines):
        if i:
            hwp.HAction.Run("BreakPara")
        if line:
            ps = hwp.HParameterSet.HInsertText
            hwp.HAction.GetDefault("InsertText", ps.HSet)
            ps.Text = line
            hwp.HAction.Execute("InsertText", ps.HSet)


_TOGGLES = {"b": "CharShapeBold", "i": "CharShapeItalic", "u": "CharShapeUnderline"}
_MARK_TOKEN = re.compile(r"</?([biu])>", re.I)


def split_marked(marked: str) -> list:
    """``"a <u>b</u>"`` -> ``[("a ", frozenset()), ("b", frozenset({"u"}))]`` (pure helper)."""
    runs, active, pos = [], set(), 0
    for m in _MARK_TOKEN.finditer(marked):
        if m.start() > pos:
            runs.append((marked[pos:m.start()], frozenset(active)))
        tag = m.group(1).lower()
        if m.group(0).startswith("</"):
            active.discard(tag)
        else:
            active.add(tag)
        pos = m.end()
    if pos < len(marked):
        runs.append((marked[pos:], frozenset(active)))
    return runs


def insert_marked(hwp: Any, marked: str) -> None:
    """Insert text containing ``<b>``, ``<i>``, ``<u>`` marks (and ``\\n``) at the caret.

    Uses the same toggles as the toolbar buttons, so the result is exactly what a
    person typing would get. Other tags are inserted literally - escape them first.
    """
    on: set = set()
    for text, want in split_marked(marked):
        for tag in sorted(on ^ want):
            hwp.HAction.Run(_TOGGLES[tag])
        on = set(want)
        insert_text(hwp, _html.unescape(text))
    for tag in sorted(on):
        hwp.HAction.Run(_TOGGLES[tag])


def _html_doc(html: str) -> str:
    # No <meta charset>: the string reaches Hangul as Unicode, and declaring utf-8
    # made Hangul decode Korean a second time (mojibake) in our tests.
    s = re.sub(r"<meta\b[^>]*charset[^>]*>", "", html, flags=re.I)
    return s if re.search(r"<html\b", s, re.I) else "<html><body>" + s + "</body></html>"


def set_html(hwp: Any, html: str) -> bool:
    """Replace the whole current document with ``html``. Returns Hangul's result flag."""
    return bool(hwp.SetTextFile(_html_doc(html), "HTML", ""))


def insert_html(hwp: Any, html: str) -> bool:
    """Insert HTML at the caret via a scratch document and Hangul's native Copy/Paste.

    ``SetTextFile(..., "HTML", "insertfile")`` blocks forever on Hangul 2022, so this
    renders the HTML in a temporary document, copies it (Hangul's own clipboard format),
    closes the scratch document, and pastes. Overwrites the clipboard. Mind the
    importer's whitespace loss at tag boundaries - prefer :func:`insert_marked`.
    """
    docs = hwp.XHwpDocuments
    original = docs.Active_XHwpDocument
    scratch = docs.Add(True)
    try:
        scratch.SetActive_XHwpDocument()
        ok = set_html(hwp, html)
        hwp.Run("SelectAll")
        hwp.Run("Copy")
    finally:
        scratch.Clear(1)
        scratch.Close(False)
        original.SetActive_XHwpDocument()
    hwp.Run("Paste")
    return ok


def get_text(hwp: Any, fmt: str = "UNICODE", option: str = "") -> str:
    """Whole-document text without the clipboard: ``fmt`` is ``UNICODE``, ``TEXT``, ``HTML`` ...

    ``UNICODE`` loses run formatting such as underline; ``HTML`` keeps it as styled
    spans (``text-decoration:underline``) that :func:`html_to_marked_text` understands,
    but - like the clipboard HTML - may lack auto-numbering characters.
    """
    return hwp.GetTextFile(fmt, option) or ""


# ----------------------------------------------------------------- clipboard
def _clipboard_read(fmt: int, tries: int, pause: float) -> Optional[Any]:
    import win32clipboard as wc  # type: ignore

    for _ in range(tries):
        try:
            wc.OpenClipboard()
            try:
                if wc.IsClipboardFormatAvailable(fmt):
                    return wc.GetClipboardData(fmt)
                return None
            finally:
                wc.CloseClipboard()
        except Exception:  # noqa: BLE001 - clipboard locked by another program
            time.sleep(pause)
    return None


def copy_document_text(hwp: Any, *, keep: Sequence[str] = ("u",), settle: float = 0.5,
                       tries: int = 8, pause: float = 0.4) -> str:
    """Select all, copy, and return plain text with ``<u>...</u>`` (or other ``keep`` tags)
    grafted from the clipboard HTML. Overwrites the user's clipboard."""
    import win32clipboard as wc  # type: ignore

    hwp.Run("MoveDocBegin")
    hwp.Run("SelectAll")
    hwp.Run("Copy")
    try:
        hwp.Run("Cancel")
    except Exception:  # noqa: BLE001
        pass
    time.sleep(settle)
    plain = ""
    for _ in range(tries):
        plain = _clipboard_read(wc.CF_UNICODETEXT, 1, pause) or ""
        if plain:
            break
        time.sleep(pause)
    plain = plain.replace("\r\n", "\n").replace("\r", "\n")
    raw = _clipboard_read(wc.RegisterClipboardFormat("HTML Format"), tries, pause)
    if not raw:
        return plain
    marked = html_to_marked_text(strip_cf_html_header(raw), keep=keep)
    for tag in keep:
        plain = graft_marks(plain, marked, tag=tag)
    return plain


# --------------------------------------------------------------- pure parsing
def strip_cf_html_header(raw: Any) -> str:
    """Drop the ``Version:/StartHTML:`` header of a CF_HTML clipboard payload.

    ``StartHTML`` is a *byte* offset, so it is applied before decoding.
    """
    if isinstance(raw, (bytes, bytearray)):
        b = bytes(raw)
        m = re.search(rb"StartHTML:(\d+)", b[:400])
        if m and 0 < int(m.group(1)) < len(b):
            b = b[int(m.group(1)):]
        s = b.decode("utf-8", "replace")
    else:
        s = str(raw)
    i = s.lower().find("<html")
    return s[i:] if i > 0 else s


_SCRIPT = re.compile(r"<(script|style)\b.*?</\1\s*>", re.S | re.I)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_MSOIF = re.compile(r"<!\[(?:if|endif)[^\]]*\]>", re.I)
_BLOCK = re.compile(r"</?(?:p|div|tr|br|li|h[1-6]|table)\b[^>]*>", re.I)
_ANYTAG = re.compile(r"<[^>]+>")


#: Inline-style equivalents of the mark tags. Hangul's ``GetTextFile("HTML")`` writes
#: underline as ``<span style="...text-decoration:underline">``, the clipboard as
#: ``<u style="text-underline:...">`` - both must be understood.
_STYLE_MARKS = {
    "u": re.compile(r"text-decoration[^;\"']*underline|text-underline\s*:\s*(?!none)", re.I),
    "b": re.compile(r"font-weight\s*:\s*(?:bold|[6-9]00)", re.I),
    "i": re.compile(r"font-style\s*:\s*italic", re.I),
}
_SPAN = re.compile(r"<(/?)span\b([^>]*)>", re.I)


def _styled_spans_to_tags(s: str, keep: Sequence[str]) -> str:
    out, stack, pos = [], [], 0
    for m in _SPAN.finditer(s):
        out.append(s[pos:m.start()])
        pos = m.end()
        if m.group(1):
            tags = stack.pop() if stack else []
            out.append("".join(f"</{t}>" for t in reversed(tags)))
        else:
            tags = [t for t in keep if t in _STYLE_MARKS and _STYLE_MARKS[t].search(m.group(2))]
            stack.append(tags)
            out.append("".join(f"<{t}>" for t in tags))
    out.append(s[pos:])
    return "".join(out)


def _marks(keep: Iterable[str]) -> Tuple[Tuple[str, str, str], ...]:
    # private-use code points as placeholders so tag stripping cannot touch them
    return tuple((t, chr(0xE000 + 2 * i), chr(0xE001 + 2 * i)) for i, t in enumerate(keep))


def html_to_marked_text(html: str, keep: Sequence[str] = ("u",)) -> str:
    """HTML -> plain text that keeps only the tags in ``keep`` (normalised, e.g. ``<u>``).

    Empty marks are dropped, whitespace is moved outside marks, and adjacent fragments
    of the same mark (Hangul splits runs freely) are merged.
    """
    s = _MSOIF.sub("", _COMMENT.sub("", _SCRIPT.sub("", html)))
    s = _styled_spans_to_tags(s, keep)
    marks = _marks(keep)
    for tag, o, c in marks:
        s = re.sub(rf"<{tag}\b[^>]*>", o, s, flags=re.I)
        s = re.sub(rf"</{tag}\s*>", c, s, flags=re.I)
    s = _BLOCK.sub("\n", s)
    s = _ANYTAG.sub("", s)
    s = _html.unescape(s).replace(" ", " ").replace("\r", "")
    for tag, o, c in marks:
        s = s.replace(o, f"<{tag}>").replace(c, f"</{tag}>")
        s = re.sub(rf"<{tag}>(\s*)</{tag}>", r"\1", s)
        s = re.sub(rf"<{tag}>(\s+)", rf"\1<{tag}>", s)
        s = re.sub(rf"(\s+)</{tag}>", rf"</{tag}>\1", s)
        for _ in range(8):
            merged = re.sub(rf"</{tag}>([ \t]*)<{tag}>", r"\1", s)
            if merged == s:
                break
            s = merged
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return "\n".join(line.strip() for line in s.split("\n")).strip()


def graft_marks(plain: str, marked: str, tag: str = "u") -> str:
    """Wrap, in ``plain``, each ``<tag>`` fragment found in ``marked``, in order.

    Fragments are located left to right; whitespace differences are tolerated.
    A fragment that cannot be found is skipped (never misplaced earlier).
    """
    frags = [f for f in re.findall(rf"<{tag}>(.*?)</{tag}>", marked, re.S) if f.strip()]
    if not frags:
        return plain
    out = []
    cur = 0
    for frag in frags:
        needle = frag.strip()
        i = plain.find(needle, cur)
        if i >= 0:
            j = i + len(needle)
        else:
            m = re.compile(r"\s+".join(map(re.escape, needle.split()))).search(plain, cur)
            if not m:
                continue
            i, j = m.start(), m.end()
        out.append(plain[cur:i])
        out.append(f"<{tag}>{plain[i:j]}</{tag}>")
        cur = j
    out.append(plain[cur:])
    return "".join(out)
