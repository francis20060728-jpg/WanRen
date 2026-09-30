"""多模态视觉分析：默认提供商、配置提示与调用行为。"""
import base64
import json

from fastapi.testclient import TestClient

from wanren.app import create_app


def _upload_png(client: TestClient) -> tuple[str, str]:
    from tests.conftest import png_payload

    session_id = client.post("/api/sessions", json={"title": "多模态任务"}).json()["session"]["id"]
    asset = client.post(
        f"/api/sessions/{session_id}/assets",
        files={"file": ("photo.png", png_payload(), "image/png")},
    ).json()
    return session_id, asset["id"]


def _vision_app(tmp_path, extra=None):
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


def test_multimodal_is_default_provider_hint_when_unconfigured(tmp_path):
    with TestClient(_vision_app(tmp_path)) as client:
        settings = client.get("/api/settings/vision").json()
        assert settings["provider"] == "multimodal"
        assert settings["configured"] is False
        assert "多模态" in settings["hint"]


def test_multimodal_analyze_returns_structured_json(tmp_path, monkeypatch):
    captured: dict = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {"message": {"content": '{"description":"一朵云","objects":["cloud"],"quality_score":88}'}}
                ]
            }

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["body"] = json
        return Response()

    monkeypatch.setattr("wanren.service.httpx.post", fake_post)
    with TestClient(_vision_app(tmp_path, {
        "WANREN_VISION_API_BASE": "https://vision.example/v1",
        "WANREN_VISION_API_KEY": "sk-test",
        "WANREN_VISION_MODEL": "gpt-4o-mini",
    })) as client:
        session_id, asset_id = _upload_png(client)
        response = client.post(f"/api/sessions/{session_id}/assets/{asset_id}/analyze", json={"prompt": None})
    assert response.status_code == 200
    vision = json.loads(response.json()["vision_result"])
    assert vision["description"] == "一朵云"
    assert captured["url"].endswith("/chat/completions")
    content = captured["body"]["messages"][1]["content"]
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert base64.b64decode(content[1]["image_url"]["url"].split(",", 1)[1])


def test_multimodal_analyze_unconfigured_returns_setup_hint(tmp_path):
    with TestClient(_vision_app(tmp_path)) as client:
        session_id, asset_id = _upload_png(client)
        response = client.post(f"/api/sessions/{session_id}/assets/{asset_id}/analyze", json={"prompt": None})
    assert response.status_code == 400
    message = response.json()["detail"]
    assert "视觉分析尚未配置" in message
    assert "WANREN_VISION_API_BASE" in message
    assert "VidLens" in message


def test_multimodal_analyze_model_error_mentions_configuration(tmp_path, monkeypatch):
    class BadResponse:
        def raise_for_status(self):
            raise RuntimeError("boom")

    monkeypatch.setattr("wanren.service.httpx.post", lambda *a, **k: BadResponse())
    with TestClient(_vision_app(tmp_path, {
        "WANREN_VISION_API_BASE": "https://vision.example/v1",
        "WANREN_VISION_API_KEY": "sk-test",
        "WANREN_VISION_MODEL": "text-only-model",
    })) as client:
        session_id, asset_id = _upload_png(client)
        response = client.post(f"/api/sessions/{session_id}/assets/{asset_id}/analyze", json={"prompt": None})
    assert response.status_code == 502
    assert "是否支持图像输入" in response.json()["detail"]


def test_vision_settings_roundtrip_encrypts_key(tmp_path):
    with TestClient(_vision_app(tmp_path)) as client:
        updated = client.put(
            "/api/settings/vision",
            json={"provider": "multimodal", "api_base": "https://vision.example/v1", "api_key": "sk-secret-123456", "model": "gpt-4o-mini"},
        ).json()
        assert updated["configured"] is True
        assert "sk-secret-123456" not in json.dumps(updated)
        assert updated["api_key_masked"].startswith("sk-")
        assert "•" in updated["api_key_masked"]
        # 落库值必须是密文
        row = client.get("/api/settings/vision").json()
        assert row["configured"] is True
        # 未带新 key 的更新保留旧密钥
        again = client.put("/api/settings/vision", json={"provider": "multimodal", "api_base": "https://v2.example/v1", "api_key": "", "model": "gpt-4o"}).json()
        assert again["api_key_masked"] == updated["api_key_masked"]
        assert again["configured"] is True
