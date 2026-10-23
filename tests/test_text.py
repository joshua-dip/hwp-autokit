from hwp_autokit.session import SuspectOutputError, is_dead_com_error
from hwp_autokit.text import graft_marks, html_to_marked_text, strip_cf_html_header

HANGUL_LIKE_HTML = """<html><head><style>p{margin:0}</style></head><body>
<!--StartFragment-->
<p class="s0"><span>Read the passage. Choose the </span><u style="text-underline:#000000 single"><span>wrong</span></u><span> one.</span></p>
<p><span>(a) </span><u style="text-underline:#000000 single">is</u><u style="text-underline:#000000 single"> going</u><span>&nbsp;to &amp; fro</span></p>
<p><u> </u></p>
<!--EndFragment--></body></html>"""

# Plain clipboard text: has the auto-numbering ("1.", "2.") that the HTML lacks.
PLAIN = "1. Read the passage. Choose the wrong one.\n2. (a) is going to & fro"


def test_html_to_marked_text_keeps_only_underline():
    t = html_to_marked_text(HANGUL_LIKE_HTML)
    assert "<span" not in t and "style" not in t and "margin" not in t
    assert "Choose the <u>wrong</u> one." in t
    assert "<u>is going</u> to & fro" in t     # split runs merged, entities decoded
    assert "<u> </u>" not in t and "<u></u>" not in t


def test_graft_keeps_numbering_from_plain_text():
    marked = html_to_marked_text(HANGUL_LIKE_HTML)
    out = graft_marks(PLAIN, marked)
    assert out == "1. Read the passage. Choose the <u>wrong</u> one.\n2. (a) <u>is going</u> to & fro"


def test_graft_tolerates_whitespace_and_order():
    plain = "the cat  sat on\nthe mat; the cat ran"
    marked = "<u>the cat sat</u> on the mat; <u>the cat</u> ran"
    assert graft_marks(plain, marked) == "<u>the cat  sat</u> on\nthe mat; <u>the cat</u> ran"


def test_graft_skips_missing_fragment():
    assert graft_marks("abc def", "<u>zzz</u> <u>def</u>") == "abc <u>def</u>"


def test_strip_cf_html_header_bytes_offset():
    body = "<html><body>한글 <u>밑줄</u></body></html>".encode("utf-8")
    header = b"Version:0.9\r\nStartHTML:%010d\r\nEndHTML:%010d\r\n"
    start = len(header % (0, 0))
    raw = header % (start, start + len(body)) + body
    assert strip_cf_html_header(raw).startswith("<html>")
    assert "밑줄" in strip_cf_html_header(raw)


def test_other_tags_can_be_kept():
    t = html_to_marked_text("<p><b style='x'>bold</b> and <u>under</u></p>", keep=("b", "u"))
    assert t == "<b>bold</b> and <u>under</u>"


def test_styled_spans_become_marks():
    # what GetTextFile("HTML") produces for underline/bold
    html = ("<p><span style='font-family:\"x\"'>a </span>"
            "<span style='position:relative;text-decoration:underline'>under</span>"
            "<span> b </span><span style='font-weight:bold'>bold</span></p>")
    assert html_to_marked_text(html, keep=("u", "b")) == "a <u>under</u> b <b>bold</b>"


def test_split_marked_runs():
    from hwp_autokit.text import split_marked

    runs = split_marked("a <u>b <b>c</b></u> d")
    assert runs == [("a ", frozenset()), ("b ", frozenset({"u"})), ("c", frozenset({"u", "b"})),
                    (" d", frozenset())]


def test_insert_marked_drives_toggles_in_order():
    from hwp_autokit.text import insert_marked

    calls = []

    class PS:
        HSet = object()
        Text = ""

    class Action:
        def Run(self, name):
            calls.append(name)

        def GetDefault(self, *_):
            pass

        def Execute(self, name, _):
            calls.append("text:" + FakeHwp.HParameterSet.HInsertText.Text)

    class FakeHwp:
        HAction = Action()

        class HParameterSet:
            HInsertText = PS()

    insert_marked(FakeHwp, "x <u>y</u> z")
    assert calls == ["text:x ", "CharShapeUnderline", "text:y", "CharShapeUnderline", "text: z"]


class FakeComError(Exception):
    pass


def test_dead_com_error_detection():
    assert is_dead_com_error(FakeComError(-2147023174, "The RPC server is unavailable.", None, None))
    assert is_dead_com_error(FakeComError(-2146959355, "Server execution failed", None, None))
    e = FakeComError(-2147352567, "Exception occurred.", (0, None, None, None, 0, -2147417848), None)
    assert is_dead_com_error(e)
    assert is_dead_com_error(RuntimeError("RPC 서버를 사용할 수 없습니다."))
    assert not is_dead_com_error(FakeComError(-2147352567, "Exception occurred.", None, None))
    assert not is_dead_com_error(SuspectOutputError("short"))
