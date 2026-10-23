"""Tests that launch Hancom Hangul. Opt-in: HWP_AUTOKIT_COM_TESTS=1 (see conftest.py).

They use a dedicated gen_py cache and a short output folder. Run them only when no
other Hangul automation is running on this PC (or inside your build queue).
"""
import shutil
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.com


@pytest.fixture(scope="module")
def outdir():
    d = Path(tempfile.gettempdir()) / "hak_t"   # short on purpose (see the 260-char gotcha)
    d.mkdir(exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(scope="module")
def session():
    from hwp_autokit.session import HwpSession

    s = HwpSession(genpy_cache="pytest")
    s.start()
    yield s
    pid = s.pid
    s.quit()
    if pid:
        from hwp_autokit._proc import pid_alive
        assert not pid_alive(pid), "safe quit left our Hangul process running"


@pytest.fixture
def doc(session):
    """The session's document, emptied before and after each test."""
    with session.watchdog(60):
        session.hwp.Clear(1)
    yield session
    with session.watchdog(60):
        session.hwp.Clear(1)


def _html(session):
    from hwp_autokit.text import get_text, html_to_marked_text

    return html_to_marked_text(get_text(session.hwp, "HTML"), keep=("b", "u"))


def test_session_identifies_its_pid(session):
    assert session.pid, "could not identify the Hangul process this session started"
    assert session.security_registered in (True, False)


def test_insert_marked_keeps_spacing_and_marks(doc):
    from hwp_autokit.text import get_text, insert_marked

    with doc.watchdog(60):
        insert_marked(doc.hwp, "plain <u>under</u> 한글 <b>bold</b> end\nsecond line")
        assert get_text(doc.hwp).replace("\r\n", "\n").strip() == "plain under 한글 bold end\nsecond line"
        marked = _html(doc)
    assert "<u>under</u> 한글" in marked
    assert "<b>bold</b>" in marked


def test_set_html_and_insert_html(doc):
    from hwp_autokit.text import get_text, insert_html, insert_text, set_html

    with doc.watchdog(60):
        assert set_html(doc.hwp, "<p>whole <u>doc</u></p>")
        assert "whole" in get_text(doc.hwp)
        doc.hwp.Clear(1)
        insert_text(doc.hwp, "before ")
        assert insert_html(doc.hwp, "<p><u>pasted</u>한글</p>")
        text = get_text(doc.hwp)
    assert "before" in text and "pasted한글" in text
    assert "<u>pasted</u>" in _html(doc)


def test_save_verified_and_reopen(doc, outdir):
    from hwp_autokit.text import get_text, insert_text

    with doc.watchdog(60):
        insert_text(doc.hwp, "saved text")
        out = doc.save_as(outdir / "roundtrip.hwp")
        assert out.stat().st_size > 0
        doc.hwp.Clear(1)
        doc.open(out)
        assert "saved text" in get_text(doc.hwp)


def test_clipboard_extraction_keeps_underline(doc):
    from hwp_autokit.text import copy_document_text, insert_marked

    with doc.watchdog(60):
        insert_marked(doc.hwp, "1) plain <u>marked</u> end")
        out = copy_document_text(doc.hwp)
    assert "<u>marked</u>" in out


def test_long_path_is_refused_before_hangul_lies(doc, outdir):
    from hwp_autokit.paths import PathTooLongError

    with pytest.raises(PathTooLongError):
        doc.save_as(outdir / ("x" * 280 + ".hwp"))


def test_watchdog_recovers_from_known_blocking_call(session):
    """SetTextFile(..., "TEXT", "insertfile") blocks forever on Hangul 2022 - the
    watchdog must kill our instance and the session must come back on next use."""
    from hwp_autokit.session import HwpTimeout
    from hwp_autokit.text import get_text, insert_text

    old = session.pid
    with pytest.raises(HwpTimeout):
        with session.watchdog(10):
            session.hwp.SetTextFile("x", "TEXT", "insertfile")
    insert_text(session.hwp, "alive again")     # restarts transparently
    assert session.pid != old
    assert "alive again" in get_text(session.hwp)


def test_isolated_cache_location(session):
    import win32com

    assert "hwp-autokit" in win32com.__gen_path__ and "pytest" in win32com.__gen_path__
