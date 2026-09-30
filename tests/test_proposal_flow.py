"""架构提案流：大模型先提案 → 用户确认 / 修改 / 取消 → 才生成正文。"""

from tests.conftest import png_payload


def _create_session(client, title="论文解读任务", goal="解读这篇论文并写成公众号文章"):
    response = client.post(
        "/api/sessions",
        json={"title": title, "goal": goal, "audience": "研究生与工程师", "tone": "专业、克制"},
    )
    assert response.status_code == 200
    return response.json()["session"]["id"]


def _upload_paper(client, session_id):
    upload = client.post(
        f"/api/sessions/{session_id}/assets",
        files={
            "file": (
                "paper.txt",
                "论文提出仓库多摄像头机器人跟踪方法，跨镜头身份切换下降 87.6%，开源数据集 Warehouse-ID。".encode(),
                "text/plain",
            )
        },
    )
    assert upload.status_code == 200


def test_propose_confirm_generate_flow(client):
    session_id = _create_session(client)
    _upload_paper(client, session_id)

    proposed = client.post(
        f"/api/sessions/{session_id}/propose", json={"instruction": "写一篇论文深读"}
    )
    assert proposed.status_code == 200
    body = proposed.json()
    assert body["proposal_status"] == "AWAITING"
    proposal = body["proposal"]
    assert len(proposal["sections"]) >= 3
    assert all(section["heading"] for section in proposal["sections"])
    assert proposal["typesetting"]["style_id"]

    # 未确认时禁止生成
    blocked = client.post(f"/api/sessions/{session_id}/generate", json={"instruction": "先写"})
    assert blocked.status_code == 409
    assert "确认" in blocked.json()["detail"]

    # 会话详情暴露提案
    detail = client.get(f"/api/sessions/{session_id}").json()
    assert detail["session"]["proposal_status"] == "AWAITING"
    assert detail["session"]["proposal"]["title"] == proposal["title"]

    # 确认（用户换样式）→ 可生成；文章继承样式与已确认大纲
    confirmed = client.post(
        f"/api/sessions/{session_id}/confirm-proposal", json={"style_id": "blue-magazine"}
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["typesetting_id"] == "blue-magazine"
    assert confirmed.json()["proposal_status"] == "CONFIRMED"

    generated = client.post(
        f"/api/sessions/{session_id}/generate", json={"instruction": "按已确认大纲写作"}
    )
    assert generated.status_code == 200
    article_id = generated.json()["article_id"]

    article = client.get(f"/api/articles/{article_id}").json()["article"]
    assert article["typesetting_id"] == "blue-magazine"
    import json as _json

    content = article["content_json"]
    if isinstance(content, str):
        content = _json.loads(content)
    headings = [section["heading"] for section in content["sections"]]
    for planned in proposal["sections"]:
        assert planned["heading"] in headings


def test_modify_keeps_awaiting_and_replaces_outline(client):
    session_id = _create_session(client, title="提案修改流程")
    first = client.post(
        f"/api/sessions/{session_id}/propose", json={"instruction": "写一篇方法解读"}
    ).json()
    second = client.post(
        f"/api/sessions/{session_id}/propose", json={"instruction": "第二节改成讲实验设置"}
    )
    assert second.status_code == 200
    assert second.json()["proposal_status"] == "AWAITING"
    detail = client.get(f"/api/sessions/{session_id}").json()
    assert detail["session"]["proposal_status"] == "AWAITING"
    # 修改后仍需确认才能生成
    assert (
        client.post(f"/api/sessions/{session_id}/generate", json={"instruction": "x"}).status_code
        == 409
    )


def test_cancel_deletes_session_and_assets(client):
    session_id = _create_session(client, title="取消流程")
    _upload_paper(client, session_id)
    client.post(f"/api/sessions/{session_id}/propose", json={"instruction": "写一篇短读"})

    cancelled = client.delete(f"/api/sessions/{session_id}")
    assert cancelled.status_code == 200
    assert client.get(f"/api/sessions/{session_id}").status_code == 404


def test_cancel_blocked_after_article_exists(client):
    session_id = _create_session(client, title="不可取消流程")
    client.post(f"/api/sessions/{session_id}/propose", json={"instruction": "写一篇短读"})
    client.post(f"/api/sessions/{session_id}/confirm-proposal", json={})
    client.post(f"/api/sessions/{session_id}/generate", json={"instruction": "生成"})
    assert client.delete(f"/api/sessions/{session_id}").status_code == 409


def test_confirm_without_proposal_rejected(client):
    session_id = _create_session(client, title="空提案流程")
    response = client.post(f"/api/sessions/{session_id}/confirm-proposal", json={})
    assert response.status_code == 409


def test_legacy_session_without_proposal_still_generates(client):
    """旧路径（未发起提案）不受新守卫影响，保持兼容。"""
    session_id = _create_session(client, title="旧流程兼容")
    _upload_paper(client, session_id)
    response = client.post(f"/api/sessions/{session_id}/generate", json={"instruction": "直接生成"})
    assert response.status_code == 200


def test_typesetting_style_endpoint(client):
    response = client.get("/api/typesetting-styles")
    assert response.status_code == 200
    styles = response.json()["styles"]
    assert len(styles) == 8
    assert all({"id", "name", "vibe", "suits"} <= set(style) for style in styles)
