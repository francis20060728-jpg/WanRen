import pytest

from wanren.security import sanitize_wechat_html, validate_upload


def test_rejects_unsupported_extension():
    with pytest.raises(ValueError):
        validate_upload("payload.exe", "application/octet-stream", 10, 1000)


def test_normalizes_path_traversal_name():
    assert validate_upload("../secret.txt", "text/plain", 10, 1000) == "secret.txt"


def test_rejects_size_out_of_range():
    with pytest.raises(ValueError):
        validate_upload("a.txt", "text/plain", 0, 1000)


def test_rejects_mime_mismatch():
    with pytest.raises(ValueError):
        validate_upload("a.png", "text/html", 10, 1000)


def test_sanitize_removes_script_and_event_handlers():
    dirty = '<div onclick="steal()">A</div><script>alert(1)</script><a href="javascript:alert(2)">B</a>'
    clean = sanitize_wechat_html(dirty)
    assert "script" not in clean.lower()
    assert "onclick" not in clean.lower()
    assert "javascript:" not in clean.lower()
