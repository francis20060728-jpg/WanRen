"""文字风格模版：目录/导入/删除、会话持久化、生成注入（范文进模型请求）。"""

import json

import httpx

from tests.test_ai_gateway import adapter, article_payload, make_context, model_response
from wanren.ai import OpenAICompatibleAdapter
from wanren.writing import writing_style_prompt


def test_builtin_catalog_and_detail(client):
    styles = client.get("/api/writing-styles").json()["styles"]
    assert len(styles) == 6
    assert all({"id", "name", "vibe", "suits", "builtin"} <= set(s) for s in styles)
    detail = client.get("/api/writing-styles/deep-read").json()["style"]
    assert len(detail["samples"]) >= 2
    assert all(len(sample["text"]) >= 80 for sample in detail["samples"])
    assert detail["rules"]


def test_import_validation_and_lifecycle(client):
    payload = {
        "name": "我的口吻",
        "vibe": "带点冷幽默",
        "suits": "随笔",
        "description": "写得像个爱吐槽但靠谱的朋友，句子利落，吐槽只在段尾出现一次。",
        "rules": ["句子平均不超过 22 字", "每段一个观点"],
        "samples": [
            {"title": "样本", "text": "搬家第三次，我终于学会了扔东西。那些『万一有用』的纸箱，最后都成了『果然没用』的纸箱。" * 2}
        ],
    }
    imported = client.post("/api/writing-styles", json=payload)
    assert imported.status_code == 200
    style_id = imported.json()["style"]["id"]
    assert style_id.startswith("wstyle_")

    # 重名 409；太短的范文 400；缺刻画 400；内置删除 404
    assert client.post("/api/writing-styles", json=payload).status_code == 409
    thin = {**payload, "name": "另一个名字", "samples": [{"text": "太短"}]}
    assert client.post("/api/writing-styles", json=thin).status_code in (400, 422)
    no_desc = {**payload, "name": "没有刻画", "description": "短"}
    assert client.post("/api/writing-styles", json=no_desc).status_code in (400, 422)
    assert client.delete("/api/writing-styles/deep-read").status_code == 404
    assert client.delete(f"/api/writing-styles/{style_id}").status_code == 200
    assert client.get(f"/api/writing-styles/{style_id}").status_code == 404


def test_session_persists_writing_style(client):
    service = client.app.state.service
    created = client.post(
        "/api/sessions", json={"title": "文风会话", "writing_style_id": "story-telling"}
    )
    assert created.status_code == 200
    session_id = created.json()["session"]["id"]
    assert created.json()["session"]["writing_style_id"] == "story-telling"
    # context 能还原完整风格（含范文）
    from wanren.context import build_session_context

    context = build_session_context(service.db, session_id)
    assert context.writing_style is not None
    assert context.writing_style["name"] == "故事叙事"
    assert context.writing_style["samples"]
    # payload 只带刻画与规则，不重复携带范文
    payload = context.to_model_payload()
    assert payload["writing_style"]["description"]
    assert "samples" not in payload["writing_style"]
    # 未知风格 400
    assert client.post("/api/sessions", json={"title": "坏文风", "writing_style_id": "ghost"}).status_code == 400


def test_generate_prompt_includes_samples(client):
    service = client.app.state.service
    session = client.post(
        "/api/sessions", json={"title": "注入验证任务", "writing_style_id": "gentle-essay"}
    ).json()["session"]
    from wanren.context import build_session_context

    context = build_session_context(service.db, session["id"])
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.read())
        return httpx.Response(200, json=model_response(json.dumps(article_payload(), ensure_ascii=False)))

    adapter(httpx.MockTransport(handler)).generate(context, "写一篇文章")
    user_message = captured["body"]["messages"][1]["content"]
    # 关键断言：范文全文进入模型请求，且带有“只学笔感不学题材”的防污染指令
    style = context.writing_style
    assert user_message.count("【文字风格：温柔随笔】") == 1
    assert style["samples"][0]["text"][:30] in user_message
    assert "只学笔感与节奏" in user_message


def test_no_style_no_injection(client):
    service = client.app.state.service
    session = client.post("/api/sessions", json={"title": "无文风任务"}).json()["session"]
    from wanren.context import build_session_context

    context = build_session_context(service.db, session["id"])
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.read())
        return httpx.Response(200, json=model_response(json.dumps(article_payload(), ensure_ascii=False)))

    adapter(httpx.MockTransport(handler)).generate(context, "")
    assert "【文字风格" not in captured["body"]["messages"][1]["content"]


def test_prompt_compiler_carries_guard():
    block = writing_style_prompt(
        {
            "name": "测试",
            "description": "描述",
            "rules": ["规则一"],
            "samples": [{"title": "样本", "text": "正文"}],
        }
    )
    assert "【文字风格：测试】" in block
    assert "严禁照抄内容或沿用其题材" in block


def test_figure_pacing_clamp(client):
    """配图节奏守门：单节至多 2 张、全文至多 6 张（模型塞图成墙时确定性截断）。"""
    service = client.app.state.service
    session = client.post("/api/sessions", json={"title": "配图节奏任务"}).json()["session"]
    # 造 8 张 READY 图片素材，模型全部引用
    from tests.conftest import png_payload

    for _ in range(8):
        client.post(
            f"/api/sessions/{session['id']}/assets",
            files={"file": ("f.png", png_payload(), "image/png")},
        )
    ids = [
        asset["id"]
        for asset in client.get(f"/api/sessions/{session['id']}").json()["assets"]
        if asset["asset_type"] == "IMAGE"
    ]
    assert len(ids) == 8
    payload = article_payload()
    payload["sections"][0]["asset_refs"] = ids[:5]  # 单节塞 5 张
    payload["sections"].append(
        {"id": "sec_02", "heading": "第二节", "paragraphs": ["内容。"], "asset_refs": ids[5:]}
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=model_response(json.dumps(payload, ensure_ascii=False)))
    )
    from wanren.context import build_session_context

    context = build_session_context(service.db, session["id"])
    article = adapter(transport).generate(context, "").article
    total = sum(len(s.asset_refs) for s in article.sections)
    assert total <= 6
    assert all(len(s.asset_refs) <= 2 for s in article.sections)
