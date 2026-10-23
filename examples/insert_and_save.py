"""Create a document with formatted text and save it - with a verified save.

    python examples/insert_and_save.py C:/out/hello.hwp
"""
import sys

from hwp_autokit.session import HwpSession
from hwp_autokit.text import insert_marked, insert_text

out = sys.argv[1] if len(sys.argv) > 1 else "hello.hwp"

# genpy_cache="examples" keeps this script's win32com cache away from other tools.
with HwpSession(genpy_cache="examples") as s:
    with s.watchdog(120):                      # never hang forever on a stuck call
        insert_text(s.hwp, "hwp-autokit example\n")
        insert_marked(s.hwp, "Plain, <b>bold</b>, <u>underlined</u> and <i>italic</i> text.\n")
        insert_marked(s.hwp, "한글도 그대로: <u>밑줄</u> 뒤 띄어쓰기도 유지됩니다.")
        saved = s.save_as(out)                 # raises if the file is not really on disk
print("saved", saved, saved.stat().st_size, "bytes")
