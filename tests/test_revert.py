"""Codex 式对话回退：恢复到之前版本（历史保留，产生新版本）。"""


def test_revert_restores_previous_content(client):
    created = client.post("/api/sessions", json={"title": "回退验证任务"}).json()
    session_id = created["session"]["id"]
    generated = client.post(f"/api/sessions/{session_id}/generate", json={"instruction": ""}).json()
    article_id = generated.get("article_id") or generated.get("article", {}).get("id")
    assert article_id, generated

    detail = client.get(f"/api/articles/{article_id}").json()
    v1_title = detail["article"]["title"]

    # 人工改动一版（v2，标题变化）
    import json as jsonlib
    v1_content = jsonlib.loads(detail["article"]["content_json"])
    updated = client.put(f"/api/articles/{article_id}", json={
        "title": "改过的标题" + v1_title[-4:],
        "summary": detail["article"]["summary"],
        "sections": [
            {"id": s["id"], "heading": s["heading"], "paragraphs": [p + "（改动）" for p in s["paragraphs"]], "asset_refs": [], "style": {}}
            for s in v1_content["sections"]
        ],
        "change_summary": "人工改动",
    })
    assert updated.status_code == 200, updated.text
    v2_title = client.get(f"/api/articles/{article_id}").json()["article"]["title"]
    assert v2_title != v1_title

    # 回退到 v1
    result = client.post(f"/api/articles/{article_id}/revert", json={"to_version": 1}).json()
    assert result["reverted_to"] == 1 and result["new_version"] == 3
    after = client.get(f"/api/articles/{article_id}").json()
    assert after["article"]["title"] == v1_title  # 内容已恢复
    assert len(after["versions"]) == 3  # 历史版本全保留
    assert "恢复到第 1 版" in after["versions"][-1]["change_summary"]
    # 回退确认消息入库（带版本号）
    assert any(m["role"] == "ASSISTANT" and "已回退到第 1 版" in m["content"] for m in after["messages"])


def test_revert_unknown_version_returns_404(client):
    created = client.post("/api/sessions", json={"title": "回退不存在版本"}).json()
    session_id = created["session"]["id"]
    generated = client.post(f"/api/sessions/{session_id}/generate", json={"instruction": ""}).json()
    article_id = generated.get("article_id") or generated.get("article", {}).get("id")
    response = client.post(f"/api/articles/{article_id}/revert", json={"to_version": 99})
    assert response.status_code == 404


def test_generate_records_first_turn_messages(client):
    """新对话首轮：指令 + 初稿完成消息入库（带版本供回退按钮）。"""
    created = client.post("/api/sessions", json={"title": "首轮消息验证"}).json()
    session_id = created["session"]["id"]
    generated = client.post(f"/api/sessions/{session_id}/generate", json={"instruction": "写一篇关于测试的文章"}).json()
    article_id = generated.get("article_id") or generated.get("article", {}).get("id")
    messages = client.get(f"/api/articles/{article_id}").json()["messages"]
    roles = [m["role"] for m in messages]
    assert roles[:2] == ["USER", "ASSISTANT"]
    assert messages[0]["content"] == "写一篇关于测试的文章"
    import json as jsonlib
    meta = jsonlib.loads(messages[1]["metadata_json"])
    assert meta.get("version") == 1
