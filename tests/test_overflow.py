import io
import zipfile

import pytest
from PIL import Image, ImageDraw

from hwp_autokit.overflow import (Issue, PageStats, find_issues, has_ruby_text, iter_targets,
                                  measure_page, parse_range)

W, H = 400, 600


def page(text_to=None, light_to=None, sidebar=False, legend=False):
    """A synthetic page: black 'text' lines down to ``text_to`` (fraction of height)."""
    im = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(im)
    if light_to:
        d.rectangle((20, int(H * 0.05), W - 20, int(H * light_to)), outline=200, fill=230)
    if text_to:
        y = int(H * 0.06)
        while y < int(H * text_to):
            d.rectangle((30, y, 260, y + 4), fill=0)
            y += 12
    if sidebar:
        d.rectangle((300, int(H * 0.05), W - 10, int(H * 0.93)), fill=0)
    if legend:
        d.rectangle((30, int(H * 0.90), 260, int(H * 0.92)), fill=0)
    return im


def test_blank_page_has_no_ink():
    st = measure_page(page())
    assert st.ink == 0 and st.bottom == 0


def test_bottom_tracks_text():
    st = measure_page(page(text_to=0.5))
    assert 0.45 < st.bottom <= 0.51


def test_light_boxes_do_not_count_as_text():
    st = measure_page(page(text_to=0.3, light_to=0.9))
    assert st.bottom < 0.32           # the light box reaches 90% but text ends at 30%
    assert st.ink > 0.1


def test_col_excludes_sidebar_and_band_excludes_legend():
    im = page(text_to=0.3, sidebar=True, legend=True)
    assert measure_page(im).bottom > 0.9
    assert measure_page(im, col=(0, 0.72)).bottom > 0.85       # legend still counted
    st = measure_page(im, col=(0, 0.72), band=(0.04, 0.88))
    assert st.bottom < 0.32


def test_find_issues_rules():
    pages = [PageStats(0.05, 0.9), PageStats(0.0001, 0.0), PageStats(0.05, 0.2),
             PageStats(0.05, 0.8, bad_chars=2), PageStats(0.05, 0.1)]
    issues = find_issues(pages, low=0.35, expect=4)
    kinds = [(i.page, i.kind) for i in issues]
    assert (2, "empty") in kinds
    assert (3, "underfilled") in kinds
    assert (4, "bad_glyphs") in kinds
    assert (5, "underfilled") not in kinds    # last page may be short
    assert (None, "page_count") in kinds
    assert (1, "underfilled") not in kinds


def test_find_issues_clean():
    assert find_issues([PageStats(0.05, 0.9), PageStats(0.05, 0.2)]) == []


def test_end_to_end_measure_then_judge():
    stats = [measure_page(page(text_to=t)) for t in (0.9, 0.25, 0.9, 0.3)]
    issues = find_issues(stats, low=0.35)
    assert [i.page for i in issues] == [2]


def test_parse_range():
    assert parse_range("0-0.72") == (0.0, 0.72)
    with pytest.raises(ValueError):
        parse_range("0.8-0.2")


def _hwpx(tmp_path, body):
    p = tmp_path / "doc.hwpx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("mimetype", "application/hwp+zip")
        z.writestr("Contents/section0.xml", body)
    return p


def test_ruby_text_detection_hwpx(tmp_path):
    assert has_ruby_text(_hwpx(tmp_path, "<hp:p><hp:dutmal>x</hp:dutmal></hp:p>"))
    assert not has_ruby_text(_hwpx(tmp_path, "<hp:p><hp:t>plain</hp:t></hp:p>"))


def test_iter_targets(tmp_path):
    for n in ("a.pdf", "b.hwp", "c.hwpx", "d.txt"):
        (tmp_path / n).write_bytes(b"")
    names = [p.name for p in iter_targets([tmp_path])]
    assert names == ["a.pdf", "b.hwp", "c.hwpx"]


def test_pdf_check_if_pymupdf_available(tmp_path):
    fitz = pytest.importorskip("fitz")
    from hwp_autokit.overflow import check_file

    doc = fitz.open()
    for lines in (40, 3, 40):            # page 2 is under-filled
        pg = doc.new_page()
        for k in range(lines):
            pg.insert_text((72, 72 + k * 17), "The quick brown fox jumps over the lazy dog " * 2, fontsize=11)
    out = tmp_path / "t.pdf"
    doc.save(out)
    rep = check_file(out, low=0.35, dpi=50)
    assert rep.engine == "pdf" and len(rep.pages) == 3
    assert [(i.page, i.kind) for i in rep.issues] == [(2, "underfilled")]
