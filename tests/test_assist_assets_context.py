"""协作编辑（assist）应把会话素材（拖拽上传的内容）注入模型上下文。"""
import json

from fastapi.testclient import TestClient

from wanren.app import create_app


def _app(tmp_path, **extra):
    env = {
        "WANREN_DB_PATH": str(tmp_path / "wanren.sqlite3"),
        "WANREN_STORAGE_PATH": str(tmp_path / "storage"),
        "WANREN_PUBLISHER": "mock",
        "WANREN_WORKFLOW_MODE": "inline",
        "WANREN_AUTH_MODE": "disabled",
        "WANREN_VISION_PROVIDER": "multimodal",
    }
    env.update(extra)
    return create_app(env)


def _switch_to_remote_assist(client):
    """generate 用本地适配器；assist 前把 DB 主模型设置切到远端（走 httpx 工具循环）。"""
    client.put("/api/settings/text", json={
        "provider": "openai_compatible",
        "api_base": "https://x.example/v1",
        "api_key": "sk-test",
        "model": "m",
    })


def _capture_assist_messages(monkeypatch):
    """拦截 assist 的模型调用，返回捕获到的 messages。"""
    captured = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            # 第一轮返回不带工具调用的普通回复 → 循环结束
            return {"choices": [{"message": {"content": "完成", "tool_calls": []}}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        # 视觉分析请求：返回带描述的 JSON
        class _VisionResp:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": '{"description":"蓝色矩形示意图","quality_score":1}'}}]}

        return _VisionResp()

    monkeypatch.setattr("wanren.service.httpx.post", fake_post)

    from wanren.service import WanRenService

    def fake_call(self, messages):
        captured.setdefault("messages", []).append(messages)
        return {"choices": [{"message": {"content": "完成", "tool_calls": []}}]}

    monkeypatch.setattr(WanRenService, "_assist_call_model", fake_call)
    return captured


def test_assist_includes_asset_text_and_vision_in_context(tmp_path, monkeypatch):
    """拖拽上传的文本素材 + 已分析图片（均挂到该文章对话）→ assist 上下文含其内容与图片描述；公共素材不进。"""
    captured = _capture_assist_messages(monkeypatch)
    with TestClient(_app(tmp_path)) as client:
        session = client.post("/api/sessions", json={"title": "拖拽素材会话"}).json()["session"]
        from tests.conftest import png_payload

        # 先生成文章（text_adapter 构造时为 local，不受 DB 设置影响）
        generated = client.post(f"/api/sessions/{session['id']}/generate", json={"instruction": ""}).json()
        article_id = generated.get("article_id") or generated.get("article", {}).get("id")
        assert article_id, generated
        # 拖拽素材挂到该文章对话（article_id 表单字段）
        doc = client.post(
            f"/api/sessions/{session['id']}/assets",
            files={"file": ("notes.md", "# 要点\n论点A：数据可信。论点B：方法新颖。".encode("utf-8"), "text/markdown")},
            data={"article_id": article_id},
        )
        assert doc.status_code == 200, doc.text
        assert doc.json()["article_id"] == article_id
        img = client.post(
            f"/api/sessions/{session['id']}/assets",
            files={"file": ("figure.png", png_payload(), "image/png")},
            data={"article_id": article_id},
        ).json()
        # 会话公共素材（不带 article_id）→ 不应进入该对话上下文
        client.post(
            f"/api/sessions/{session['id']}/assets",
            files={"file": ("public-other.md", "公共素材不应出现".encode("utf-8"), "text/markdown")},
        )
        _switch_to_remote_assist(client)  # 视觉回退主模型配置 → mock 拦截并返回图片描述
        client.post(f"/api/sessions/{session['id']}/assets/{img['id']}/analyze", json={"prompt": None})
        # 触发 assist
        with client.stream("POST", f"/api/articles/{article_id}/assist", json={"instruction": "用素材改写"}) as resp:
            assert resp.status_code == 200
            list(resp.iter_lines())
    messages = captured["messages"][0]
    digest = next(m["content"] for m in messages if str(m["content"]).startswith("会话素材"))
    assert "notes.md" in digest and "论点A：数据可信" in digest
    assert "figure.png" in digest and "图片描述=" in digest
    assert "public-other.md" not in digest  # 公共素材不进对话上下文
    # 用户指令必须在素材之后（保持指令靠后，模型更关注）
    assert messages.index(next(m for m in messages if m["role"] == "user")) > messages.index(
        next(m for m in messages if str(m["content"]).startswith("会话素材"))
    )


def test_assist_without_assets_has_no_digest(tmp_path, monkeypatch):
    """没有素材时不多注入空消息。"""
    captured = _capture_assist_messages(monkeypatch)
    with TestClient(_app(tmp_path)) as client:
        session = client.post("/api/sessions", json={"title": "无素材会话"}).json()["session"]
        generated = client.post(f"/api/sessions/{session['id']}/generate", json={"instruction": ""}).json()
        article_id = generated.get("article_id") or generated.get("article", {}).get("id")
        assert article_id, generated
        _switch_to_remote_assist(client)
        with client.stream("POST", f"/api/articles/{article_id}/assist", json={"instruction": "改一下"}) as resp:
            list(resp.iter_lines())
    assert not any(str(m["content"]).startswith("会话素材") for m in captured["messages"][0])
