"""协作编辑（assist）协议：本地模型降级提示与工具回传端点。"""
import json


def test_assist_local_provider_downgrades_with_notice(client):
    created = client.post("/api/sessions", json={"title": "assist 任务"}).json()
    session_id = created["session"]["id"]
    from tests.conftest import png_payload

    client.post(
        f"/api/sessions/{session_id}/assets",
        files={"file": ("c.png", png_payload(), "image/png")},
    )
    generated = client.post(f"/api/sessions/{session_id}/generate", json={"instruction": ""}).json()
    article_id = generated.get("article_id") or generated.get("article", {}).get("id")
    assert article_id, generated

    with client.stream(
        "POST",
        f"/api/articles/{article_id}/assist",
        json={"instruction": "把标题改得更吸引人"},
    ) as response:
        assert response.status_code == 200
        events = []
        for line in response.iter_lines():
            if line.startswith("event: "):
                events.append(line[7:].strip())
    assert "notice" in events
    assert "completed" in events  # 降级为整篇修订后正常完成


def test_assist_tool_result_unknown_call_returns_404(client):
    response = client.post(
        "/api/articles/any/assist/assist_x/tools/call_y/result",
        json={"status": "ok", "message": "", "document": None},
    )
    assert response.status_code == 404
