from __future__ import annotations

import html
import re
from pathlib import PurePosixPath


ALLOWED_EXTENSIONS = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
}


def safe_file_name(name: str) -> str:
    from pathlib import Path

    cleaned = Path(name.replace("\\", "/")).name
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError("Invalid file name")
    return cleaned


def validate_upload(file_name: str, mime_type: str, size: int, max_bytes: int) -> str:
    name = safe_file_name(file_name)
    suffix = PurePosixPath(name).suffix.lower()
    expected = ALLOWED_EXTENSIONS.get(suffix)
    if expected is None:
        raise ValueError(f"Unsupported file type: {suffix or '(none)'}")
    normalized_mime = (mime_type or "").split(";")[0].strip().lower()
    aliases = {
        "image/jpg": "image/jpeg",
        "text/x-markdown": "text/markdown",
    }
    normalized_mime = aliases.get(normalized_mime, normalized_mime)
    if normalized_mime and normalized_mime != expected:
        raise ValueError("MIME type does not match file extension")
    if size <= 0 or size > max_bytes:
        raise ValueError(f"File must be between 1 byte and {max_bytes} bytes")
    return name


def validate_mp4_signature(payload: bytes) -> None:
    if len(payload) < 12 or payload[4:8] != b"ftyp":
        raise ValueError("Invalid MP4 file signature")


def escape_text(value: str) -> str:
    return html.escape(value, quote=True)


def sanitize_wechat_html(content: str) -> str:
    """Remove executable and event-bearing markup before sending to WeChat."""
    content = re.sub(r"<\s*script\b.*?<\s*/\s*script\s*>", "", content, flags=re.I | re.S)
    content = re.sub(r"<\s*style\b.*?<\s*/\s*style\s*>", "", content, flags=re.I | re.S)
    content = re.sub(r"\son[a-z]+\s*=\s*(['\"]).*?\1", "", content, flags=re.I)
    content = re.sub(r"\son[a-z]+\s*=\s*[^\s>]+", "", content, flags=re.I)
    content = re.sub(r"javascript\s*:", "", content, flags=re.I)
    return content
