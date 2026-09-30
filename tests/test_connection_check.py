"""设置页连通性检测：视觉/主文本模型，含本地适配器与未配置分支。"""
import json

import pytest
from fastapi.testclient import TestClient

from wanren.app import create_app


def _app(tmp_path, extra=None):
    env = {
        "WANREN_DB_PATH": str(tmp_path / "wanren.sqlite3"),
        "WANREN_STORAGE_PATH": str(tmp_path / "storage"),
        "WANREN_PUBLISHER": "mock",
        "WANREN_WORKFLOW_MODE": "inline",
        "WANREN_AUTH_MODE": "disabled",
        "WANREN_VISION_PROVIDER": "multimodal",
    }
    env.update(extra or {})
    return create_app(env)


class _Response:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def test_text_local_provider_reports_offline_ok(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        result = client.post("/api/settings/test-connection", json={"target": "text"}).json()
    assert result["ok"] is True
    assert "本地适配器" in result["message"]


def test_vision_unconfigured_returns_setup_hint(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        result = client.post("/api/settings/test-connection", json={"target": "vision"}).json()
    assert result["ok"] is False
    assert "视觉分析尚未配置" in result["message"]


def test_remote_success_reports_latency_and_model(tmp_path, monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        if url.endswith("/models"):
            return _Response(payload={"data": [{"id": "glm-5.3-flash"}, {"id": "glm-4.5v"}]})
        return _Response(status_code=404)

    def fake_post(url, headers=None, json=None, timeout=None):
        assert url.endswith("/chat/completions")
        return _Response(payload={"model": json["model"], "choices": [{"message": {"content": "pong"}}]})

    monkeypatch.setattr("wanren.service.httpx.get", fake_get)
    monkeypatch.setattr("wanren.service.httpx.post", fake_post)
    with TestClient(_app(tmp_path, {
        "WANREN_TEXT_PROVIDER": "openai_compatible",
        "WANREN_TEXT_MODEL": "glm-5.3-flash",
        "WANREN_TEXT_API_BASE": "https://llm.example/v1",
        "WANREN_TEXT_API_KEY": "sk-x",
        "WANREN_VISION_API_BASE": "https://vision.example/v1",
        "WANREN_VISION_API_KEY": "sk-v",
        "WANREN_VISION_MODEL": "glm-4.5v",
    })) as client:
        text_result = client.post("/api/settings/test-connection", json={"target": "text"}).json()
        vision_result = client.post("/api/settings/test-connection", json={"target": "vision"}).json()
    assert text_result["ok"] is True and "主文本模型连接成功" in text_result["message"] and "glm-5.3-flash" in text_result["message"]
    assert vision_result["ok"] is True and "视觉模型连接成功" in vision_result["message"] and "glm-4.5v" in vision_result["message"]


def test_remote_http_error_surfaces_detail(tmp_path, monkeypatch):
    monkeypatch.setattr("wanren.service.httpx.get", lambda *a, **k: _Response(status_code=404, text="not found"))
    monkeypatch.setattr("wanren.service.httpx.post", lambda *a, **k: _Response(status_code=401, text="unauthorized"))
    with TestClient(_app(tmp_path, {
        "WANREN_TEXT_PROVIDER": "openai_compatible",
        "WANREN_TEXT_MODEL": "m",
        "WANREN_TEXT_API_BASE": "https://llm.example/v1",
        "WANREN_TEXT_API_KEY": "bad",
    })) as client:
        result = client.post("/api/settings/test-connection", json={"target": "text"}).json()
    assert result["ok"] is False
    assert "401" in result["message"]


def test_settings_include_text_config(tmp_path):
    with TestClient(_app(tmp_path, {
        "WANREN_TEXT_PROVIDER": "openai_compatible",
        "WANREN_TEXT_MODEL": "glm-5.3-flash",
        "WANREN_TEXT_API_BASE": "https://llm.example/v1",
        "WANREN_TEXT_API_KEY": "sk-secret-12345678",
    })) as client:
        settings = client.get("/api/settings/vision").json()
    tc = settings["text_config"]
    assert tc["provider"] == "openai_compatible"
    assert tc["model"] == "glm-5.3-flash"
    assert "sk-secret-12345678" not in json.dumps(settings)
    assert tc["api_key_masked"].startswith("sk-")


def test_vidlens_connection_reports_missing_config(tmp_path, monkeypatch):
    """VidLens 已内置但内部厂家未配置 → 明确中文指引。"""
    from wanren.config import load_settings

    settings = load_settings()
    # 使用项目内置脚本路径（config.py 默认指向 scripts/vidlens/vidlens.py）
    assert settings.vidlens_path.exists(), "内置 VidLens 脚本应存在"
    monkeypatch.delenv("VIDLENS_API_URL", raising=False)
    monkeypatch.delenv("VIDLENS_API_KEY", raising=False)
    monkeypatch.delenv("VIDLENS_MODEL", raising=False)
    with TestClient(_app(tmp_path)) as client:
        result = client.post("/api/settings/test-connection", json={"target": "vidlens"}).json()
    assert result["ok"] is False
    assert "VIDLENS_API_URL" in result["message"]


def test_vidlens_connection_probes_internal_provider(tmp_path, monkeypatch):
    """VidLens 配置了内部厂家（VIDLENS_* env）→ 对该厂家发 GET /models 并报模型在列。"""
    monkeypatch.setenv("VIDLENS_API_URL", "https://vl.example/v1")
    monkeypatch.setenv("VIDLENS_API_KEY", "sk-vl")
    monkeypatch.setenv("VIDLENS_MODEL", "vl-vision-1")

    def fake_get(url, headers=None, timeout=None):
        assert url == "https://vl.example/v1/models"
        assert headers["Authorization"] == "Bearer sk-vl"
        return _Response(payload={"data": [{"id": "vl-vision-1"}]})

    monkeypatch.setattr("wanren.service.httpx.get", fake_get)
    with TestClient(_app(tmp_path)) as client:
        result = client.post("/api/settings/test-connection", json={"target": "vidlens"}).json()
    assert result["ok"] is True
    assert "vl.example" in result["message"] and "在列表中" in result["message"]


def test_vidlens_connection_missing_script(tmp_path, monkeypatch):
    """WANREN_VIDLENS 指向不存在的脚本 → 明确报路径。"""
    with TestClient(_app(tmp_path, {"WANREN_VIDLENS": str(tmp_path / "nope.py")})) as client:
        result = client.post("/api/settings/test-connection", json={"target": "vidlens"}).json()
    assert result["ok"] is False
    assert "不存在" in result["message"]


def test_vidlens_settings_saved_via_api_and_used(tmp_path, monkeypatch):
    """设置页保存 VidLens 内部厂家 → 检测用 DB 配置（不再依赖环境变量）。"""
    # 确保环境变量不干扰
    monkeypatch.delenv("VIDLENS_API_URL", raising=False)
    monkeypatch.delenv("VIDLENS_API_KEY", raising=False)
    monkeypatch.delenv("VIDLENS_MODEL", raising=False)

    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["auth"] = headers["Authorization"]
        return _Response(payload={"data": [{"id": "vl-db-model"}]})

    monkeypatch.setattr("wanren.service.httpx.get", fake_get)
    with TestClient(_app(tmp_path)) as client:
        # 保存：DB 配置（模拟设置页操作）
        saved = client.put("/api/settings/vision", json={
            "provider": "vidlens",
            "vidlens_api_url": "https://db-vl.example/v4",
            "vidlens_api_key": "sk-db-key",
            "vidlens_model": "vl-db-model",
        }).json()
        vc = saved["vidlens_config"]
        assert vc["configured"] is True
        assert vc["source"] == "db"
        assert vc["api_key_masked"].startswith("sk-")
        assert "sk-db-key" not in json.dumps(saved)
        # 检测：应使用 DB 里的厂家
        result = client.post("/api/settings/test-connection", json={"target": "vidlens"}).json()
    assert result["ok"] is True
    assert captured["url"] == "https://db-vl.example/v4/models"
    assert captured["auth"] == "Bearer sk-db-key"
    assert "db-vl.example" in result["message"]


def test_vidlens_key_blank_keeps_stored(tmp_path):
    """Key 留空提交 → 保留已存密钥（不清空）。"""
    with TestClient(_app(tmp_path)) as client:
        client.put("/api/settings/vision", json={
            "provider": "vidlens",
            "vidlens_api_url": "https://vl.example/v4",
            "vidlens_api_key": "sk-first",
            "vidlens_model": "vl-m",
        })
        # 只改 URL，key 不带字段（None → 不更新）；带空串且 url/model 非空 → 保留
        updated = client.put("/api/settings/vision", json={
            "provider": "vidlens",
            "vidlens_api_url": "https://vl2.example/v4",
            "vidlens_api_key": "",
            "vidlens_model": "vl-m",
        }).json()
        vc = updated["vidlens_config"]
        assert vc["api_url"] == "https://vl2.example/v4"
        assert vc["configured"] is True  # key 仍在（configured 需三件齐全）


def test_vision_settings_do_not_touch_vidlens_when_absent(tmp_path, monkeypatch):
    """保存多模态视觉设置（不带 vidlens 字段）→ VidLens 配置保持不变。"""
    monkeypatch.setenv("VIDLENS_API_URL", "https://env-vl.example/v4")
    monkeypatch.setenv("VIDLENS_API_KEY", "sk-env")
    monkeypatch.setenv("VIDLENS_MODEL", "vl-env")
    with TestClient(_app(tmp_path)) as client:
        client.put("/api/settings/vision", json={
            "provider": "multimodal",
            "api_base": "https://vision.example/v1",
            "api_key": "sk-v",
            "model": "glm-4.5v",
        })
        settings = client.get("/api/settings/vision").json()
        vc = settings["vidlens_config"]
        assert vc["api_url"] == "https://env-vl.example/v4"
        assert vc["source"] == "env"
