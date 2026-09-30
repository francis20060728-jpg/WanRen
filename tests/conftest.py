from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from wanren.app import create_app


@pytest.fixture()
def client(tmp_path: Path):
    app = create_app(
        {
            "WANREN_DB_PATH": str(tmp_path / "wanren.sqlite3"),
            "WANREN_STORAGE_PATH": str(tmp_path / "storage"),
            "WANREN_PUBLISHER": "mock",
            "WANREN_MAX_UPLOAD_MB": "1",
            "WANREN_WORKFLOW_MODE": "inline",
            "WANREN_AUTH_MODE": "disabled",
        }
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def vidlens_client(tmp_path: Path):
    """VidLens 视觉模式的独立 app（默认 fixture 走多模态，未配置时 analyze 会 400）。"""
    app = create_app(
        {
            "WANREN_DB_PATH": str(tmp_path / "wanren.sqlite3"),
            "WANREN_STORAGE_PATH": str(tmp_path / "storage"),
            "WANREN_PUBLISHER": "mock",
            "WANREN_MAX_UPLOAD_MB": "4",
            "WANREN_WORKFLOW_MODE": "inline",
            "WANREN_AUTH_MODE": "disabled",
            "WANREN_VISION_PROVIDER": "vidlens",
        }
    )
    with TestClient(app) as test_client:
        yield test_client


def png_payload() -> bytes:
    image = Image.new("RGB", (8, 6), "#175641")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
