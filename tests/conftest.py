import os
import sys

import pytest

IS_WINDOWS = sys.platform == "win32"


def hangul_installed() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        import winreg

        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"HWPFrame.HwpObject\CLSID"))
        return True
    except OSError:
        return False


def pytest_collection_modifyitems(config, items):
    skip_win = pytest.mark.skip(reason="needs Windows")
    com_reason = None
    if not IS_WINDOWS:
        com_reason = "needs Windows"
    elif not hangul_installed():
        com_reason = "Hancom Hangul is not installed"
    elif os.environ.get("HWP_AUTOKIT_COM_TESTS") != "1":
        com_reason = "set HWP_AUTOKIT_COM_TESTS=1 to run tests that launch Hangul"
    for item in items:
        if "windows" in item.keywords and not IS_WINDOWS:
            item.add_marker(skip_win)
        if "com" in item.keywords and com_reason:
            item.add_marker(pytest.mark.skip(reason=com_reason))
