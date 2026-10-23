import os
import sys
import time

import pytest

from hwp_autokit.paths import (PathTooLongError, SaveVerificationError, check_path, count_files,
                               extended_path, find_long_paths, fit_filename, path_length,
                               safe_save, verify_saved)


def test_check_path(tmp_path):
    assert check_path(tmp_path / "a.hwp", 250)
    with pytest.raises(PathTooLongError):
        check_path(tmp_path / ("x" * 300 + ".hwp"), 250)


def test_fit_filename_untouched_when_short(tmp_path):
    assert fit_filename(tmp_path, "report", ".hwp") == "report.hwp"


def test_fit_filename_shortens_stably_and_uniquely(tmp_path):
    limit = path_length(tmp_path) + 40
    a = fit_filename(tmp_path, "A" * 100 + "_one", ".hwp", limit=limit)
    b = fit_filename(tmp_path, "A" * 100 + "_two", ".hwp", limit=limit)
    assert a != b
    assert a == fit_filename(tmp_path, "A" * 100 + "_one", ".hwp", limit=limit)
    for name in (a, b):
        assert path_length(tmp_path / name) <= limit
        assert name.endswith(".hwp") and "~" in name


def test_fit_filename_gives_up_when_folder_too_long(tmp_path):
    with pytest.raises(PathTooLongError):
        fit_filename(tmp_path, "whatever", ".hwp", limit=path_length(tmp_path) + 5)


def test_verify_saved(tmp_path):
    p = tmp_path / "out.hwp"
    with pytest.raises(SaveVerificationError, match="no file"):
        verify_saved(p)
    p.write_bytes(b"")
    with pytest.raises(SaveVerificationError, match="bytes"):
        verify_saved(p)
    p.write_bytes(b"data")
    assert verify_saved(p) == 4
    with pytest.raises(SaveVerificationError, match="older"):
        verify_saved(p, not_before=time.time() + 3600)


def test_safe_save_catches_lying_save(tmp_path):
    # Simulates Hangul: returns True but writes nothing.
    with pytest.raises(SaveVerificationError):
        safe_save(lambda p: True, tmp_path / "ghost.hwp", settle=0.1)


def test_safe_save_catches_stale_file(tmp_path):
    p = tmp_path / "old.hwp"
    p.write_bytes(b"old")
    past = time.time() - 3600
    os.utime(p, (past, past))
    with pytest.raises(SaveVerificationError, match="older"):
        safe_save(lambda _: True, p, settle=0.1)


def test_safe_save_ok_and_false(tmp_path):
    p = tmp_path / "sub" / "ok.hwp"
    assert safe_save(lambda s: open(s, "wb").write(b"x"), p) == p
    with pytest.raises(SaveVerificationError, match="False"):
        safe_save(lambda s: False, tmp_path / "f.hwp")


def test_safe_save_refuses_long_path_before_saving(tmp_path):
    called = []
    with pytest.raises(PathTooLongError):
        safe_save(called.append, tmp_path / ("y" * 300 + ".hwp"))
    assert called == []


def test_count_files(tmp_path):
    for n in ("a.hwp", "b.HWP", "c.pdf"):
        (tmp_path / n).write_bytes(b"1")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "d.hwp").write_bytes(b"1")
    assert count_files(tmp_path, "*.hwp") == 2
    assert count_files(tmp_path, "*.hwp", recursive=True) == 3
    assert count_files(tmp_path / "missing") == 0


@pytest.mark.skipif(sys.platform != "win32", reason="\\\\?\\ paths are Windows-only")
def test_long_paths_are_counted_and_found(tmp_path):
    deep = os.path.join(extended_path(tmp_path), *(["d" * 60] * 5))
    os.makedirs(deep)
    with open(os.path.join(deep, "hidden.hwp"), "wb") as f:
        f.write(b"1")
    assert count_files(tmp_path, "*.hwp", recursive=True) == 1
    found = find_long_paths(tmp_path, 250)
    assert len(found) == 1 and found[0].endswith("hidden.hwp") and len(found[0]) > 260


def test_extended_path_prefix():
    if sys.platform == "win32":
        assert extended_path("C:\\x").startswith("\\\\?\\")
        assert extended_path("\\\\server\\share\\f").startswith("\\\\?\\UNC\\server")
    else:
        assert extended_path("/tmp/x") == "/tmp/x"
