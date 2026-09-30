"""editorial.metrics 数据卡：生成产出指标 → 手动编辑保存不丢（前端卡片随它渲染）。"""

import json


def _generate_article(client):
    session_id = client.post(
        "/api/sessions", json={"title": "数据卡任务", "goal": "解读论文并提炼关键指标"}
    ).json()["session"]["id"]
    client.post(
        f"/api/sessions/{session_id}/assets",
        files={
            "file": (
                "paper.txt",
                "模型在 Warehouse-ID 数据集上 mAP 提升 3.1%，FPS 达到 45，跨摄像头身份切换准确率 92%。".encode(),
                "text/plain",
            )
        },
    )
    client.post(f"/api/sessions/{session_id}/propose", json={"instruction": "写论文深读"})
    client.post(f"/api/sessions/{session_id}/confirm-proposal", json={"style_id": "academic-frame"})
    generated = client.post(f"/api/sessions/{session_id}/generate", json={"instruction": "按大纲写作"})
    assert generated.status_code == 200
    return generated.json()["article_id"]


def test_generated_article_carries_editorial_metrics(client):
    article_id = _generate_article(client)
    article = client.get(f"/api/articles/{article_id}").json()["article"]
    editorial = json.loads(article["content_json"])["editorial"]
    assert isinstance(editorial.get("metrics"), list) and editorial["metrics"]


def test_manual_edit_preserves_editorial_metrics(client):
    article_id = _generate_article(client)
    article = client.get(f"/api/articles/{article_id}").json()["article"]
    content = json.loads(article["content_json"])
    metrics_before = content["editorial"]["metrics"]

    # 沉浸式手动编辑只提交 title/summary/sections（前端 collectArticleEditorContent 同构）
    updated = client.put(
        f"/api/articles/{article_id}",
        json={
            "title": content["title"],
            "summary": content["summary"],
            "sections": content["sections"],
            "change_summary": "手动微调",
        },
    )
    assert updated.status_code == 200

    after = json.loads(client.get(f"/api/articles/{article_id}").json()["article"]["content_json"])
    assert after["editorial"]["metrics"] == metrics_before
