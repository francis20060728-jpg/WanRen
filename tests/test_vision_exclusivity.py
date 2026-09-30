"""视觉链路互斥性铁证：multimodal 模式只调 API 不碰子进程；vidlens 模式只调子进程不碰 API。"""
import json

from fastapi.testclient import TestClient

from wanren.app import create_app


class _ApiResponse:
    status_code = 200
    text = "{}"

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": '{"description":"api","quality_score":1}'}}]}


class _ProcessResult:
    returncode = 0
    stdout = '{"description":"cli","quality_score":2}'
    stderr = ""


def _upload(client):
    from tests.conftest import png_payload

    session_id = client.post("/api/sessions", json={"title": "互斥验证"}).json()["session"]["id"]
    asset = client.post(
        f"/api/sessions/{session_id}/assets",
        files={"file": ("p.png", png_payload(), "image/png")},
    ).json()
    return session_id, asset["id"]


def _app(tmp_path, provider, extra=None):
    env = {
        "WANREN_DB_PATH": str(tmp_path / "wanren.sqlite3"),
        "WANREN_STORAGE_PATH": str(tmp_path / "storage"),
        "WANREN_PUBLISHER": "mock",
        "WANREN_WORKFLOW_MODE": "inline",
        "WANREN_AUTH_MODE": "disabled",
        "WANREN_VISION_PROVIDER": provider,
    }
    env.update(extra or {})
    return create_app(env)


def test_multimodal_mode_never_touches_subprocess(tmp_path, monkeypatch):
    """选多模态 → 只调 httpx API，subprocess.run 必须零调用。"""
    calls = {"api": 0, "subprocess": 0}

    def fake_post(*args, **kwargs):
        calls["api"] += 1
        return _ApiResponse()

    def fail_run(*args, **kwargs):
        calls["subprocess"] += 1
        raise AssertionError("multimodal 模式绝不调用 VidLens 子进程！")

    monkeypatch.setattr("wanren.service.httpx.post", fake_post)
    monkeypatch.setattr("wanren.service.subprocess.run", fail_run)
    with TestClient(_app(tmp_path, "multimodal", {
        "WANREN_VISION_API_BASE": "https://v.example/v1",
        "WANREN_VISION_API_KEY": "sk",
        "WANREN_VISION_MODEL": "m",
    })) as client:
        session_id, asset_id = _upload(client)
        result = client.post(f"/api/sessions/{session_id}/assets/{asset_id}/analyze", json={"prompt": None})
    assert result.status_code == 200
    assert calls["api"] == 1
    assert calls["subprocess"] == 0
    vision = json.loads(result.json()["vision_result"])
    assert vision["description"] == "api"


def test_vidlens_mode_never_touches_api(tmp_path, monkeypatch):
    """选 VidLens → 只调 subprocess，httpx.post 必须零调用。"""
    calls = {"api": 0, "subprocess": 0}

    def fail_post(*args, **kwargs):
        calls["api"] += 1
        raise AssertionError("vidlens 模式绝不调用多模态 API！")

    def fake_run(*args, **kwargs):
        calls["subprocess"] += 1
        return _ProcessResult()

    monkeypatch.setattr("wanren.service.httpx.post", fail_post)
    monkeypatch.setattr("wanren.service.subprocess.run", fake_run)
    with TestClient(_app(tmp_path, "vidlens")) as client:
        session_id, asset_id = _upload(client)
        result = client.post(f"/api/sessions/{session_id}/assets/{asset_id}/analyze", json={"prompt": None})
    assert result.status_code == 200
    assert calls["subprocess"] == 1
    assert calls["api"] == 0
    vision = json.loads(result.json()["vision_result"])
    assert vision["description"] == "cli"


def test_follow_main_model_inherits_text_config_for_vision(tmp_path, monkeypatch):
    """跟随主模型：视觉调用必须用主文本模型的 URL/Key/Model（而非独立视觉配置）。"""
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["model"] = json["model"]
        captured["auth"] = headers["Authorization"]
        return _ApiResponse()

    monkeypatch.setattr("wanren.service.httpx.post", fake_post)
    with TestClient(_app(tmp_path, "multimodal", {
        # 主模型配置（视觉未单独配置 → 应回退到这里）
        "WANREN_TEXT_PROVIDER": "openai_compatible",
        "WANREN_TEXT_MODEL": "glm-4.5v",
        "WANREN_TEXT_API_BASE": "https://text.example/v1",
        "WANREN_TEXT_API_KEY": "sk-text",
    })) as client:
        session_id, asset_id = _upload(client)
        result = client.post(f"/api/sessions/{session_id}/assets/{asset_id}/analyze", json={"prompt": None})
    assert result.status_code == 200
    assert captured["url"].startswith("https://text.example/v1")
    assert captured["model"] == "glm-4.5v"
    assert captured["auth"] == "Bearer sk-text"


def test_external_vision_config_overrides_main(tmp_path, monkeypatch):
    """外挂配置优先于主模型：单独配置了视觉模型时用它而非主模型。"""
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["model"] = json["model"]
        return _ApiResponse()

    monkeypatch.setattr("wanren.service.httpx.post", fake_post)
    with TestClient(_app(tmp_path, "multimodal", {
        "WANREN_TEXT_PROVIDER": "openai_compatible",
        "WANREN_TEXT_MODEL": "text-only",
        "WANREN_TEXT_API_BASE": "https://text.example/v1",
        "WANREN_TEXT_API_KEY": "sk-text",
        # 视觉单独配置
        "WANREN_VISION_API_BASE": "https://vision.example/v1",
        "WANREN_VISION_API_KEY": "sk-vision",
        "WANREN_VISION_MODEL": "glm-4.5v",
    })) as client:
        session_id, asset_id = _upload(client)
        result = client.post(f"/api/sessions/{session_id}/assets/{asset_id}/analyze", json={"prompt": None})
    assert result.status_code == 200
    assert captured["url"].startswith("https://vision.example/v1")
    assert captured["model"] == "glm-4.5v"
