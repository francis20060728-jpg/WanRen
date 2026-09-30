from unittest.mock import patch

from tests.conftest import png_payload


def create_session(client, title="端到端公众号任务"):
    response = client.post(
        "/api/sessions",
        json={
            "title": title,
            "goal": "基于素材生成一篇可审核文章",
            "audience": "已有用户",
            "tone": "专业、克制",
            "requirements": ["保留数据来源"],
        },
    )
    assert response.status_code == 200
    return response.json()["session"]["id"]


def test_full_local_workflow_with_human_gate_and_idempotency(client):
    session_id = create_session(client)
    upload = client.post(
        f"/api/sessions/{session_id}/assets",
        files={"file": ("brief.txt", "车型续航 650km，充电 30 分钟。".encode(), "text/plain")},
    )
    assert upload.status_code == 200
    image = client.post(
        f"/api/sessions/{session_id}/assets",
        files={"file": ("cover.png", png_payload(), "image/png")},
    )
    assert image.status_code == 200

    generated = client.post(
        f"/api/sessions/{session_id}/generate", json={"instruction": "突出续航与充电事实"}
    )
    assert generated.status_code == 200
    payload = generated.json()
    article_id = payload["article_id"]
    assert payload["review"]["decision"] in {"PASS", "REVISE"}

    # V4 起：正式发布保留人工门禁；存草稿随时允许（主动同步即视为人的确认）
    blocked = client.post(f"/api/articles/{article_id}/publish", json={"mode": "publish"})
    assert blocked.status_code == 409

    first = client.post(f"/api/articles/{article_id}/publish", json={"mode": "draft"})
    assert first.status_code == 200
    second = client.post(f"/api/articles/{article_id}/publish", json={"mode": "draft"})
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]

    detail = client.get(f"/api/articles/{article_id}").json()
    assert detail["article"]["status"] == "WECHAT_DRAFT"
    assert detail["publish_jobs"][0]["external_id"]

    approved = client.post(
        f"/api/articles/{article_id}/approve", json={"reviewer": "test-editor"}
    )
    assert approved.status_code == 200

    workflow = client.get(f"/api/workflows/{payload['workflow_run_id']}").json()
    assert workflow["run"]["status"] == "SUCCESS"
    assert {node["node_key"] for node in workflow["nodes"]} >= {
        "UNDERSTAND_CONTEXT",
        "GENERATE_ARTICLE",
        "FORMAT_ARTICLE",
        "AI_REVIEW",
    }


def test_rejected_review_cannot_be_directly_approved(client):
    session_id = create_session(client, "审核保护任务")
    generated = client.post(f"/api/sessions/{session_id}/generate", json={})
    article_id = generated.json()["article_id"]
    with patch(
        "wanren.service.review_article",
        return_value=__import__("wanren.review", fromlist=["ReviewResult"]).ReviewResult(
            40,
            "REJECT",
            "HIGH",
            [],
            {},
        ),
    ):
        rejected = client.post(f"/api/articles/{article_id}/review")
        assert rejected.json()["decision"] == "REJECT"
    response = client.post(f"/api/articles/{article_id}/approve", json={"reviewer": "x"})
    assert response.status_code == 400


def test_dashboard_reflects_workflow(client):
    session_id = create_session(client, "仪表盘任务")
    client.post(f"/api/sessions/{session_id}/generate", json={})
    dashboard = client.get("/api/dashboard").json()
    assert dashboard["sessions"] >= 1
    assert dashboard["recent_workflows"][0]["session_title"] == "仪表盘任务"


def test_human_edit_creates_version_and_reReview(client):
    session_id = create_session(client, "人工版本任务")
    generated = client.post(f"/api/sessions/{session_id}/generate", json={})
    article_id = generated.json()["article_id"]
    original = client.get(f"/api/articles/{article_id}").json()["article"]
    updated = client.put(
        f"/api/articles/{article_id}",
        json={
            "title": "人工修订后的标题",
            "summary": "人工编辑后自动进入复审。",
            "sections": [
                {
                    "id": "sec_01",
                    "heading": "修订段落",
                    "paragraphs": ["编辑补充了一段人工确认的内容。"],
                    "asset_refs": [],
                }
            ],
            "change_summary": "测试人工版本",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["version"] == 2
    detail = client.get(f"/api/articles/{article_id}").json()
    assert detail["article"]["title"] == "人工修订后的标题"
    assert detail["article"]["current_version_id"] != original["current_version_id"]
    assert detail["versions"][-1]["source"] == "HUMAN"
    assert detail["reviews"][-1]["review_type"] == "AI"
