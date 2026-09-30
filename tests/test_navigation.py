from tests.conftest import png_payload


def test_global_lists_and_audit_are_available(client):
    session = client.post("/api/sessions", json={"title": "全局导航任务"}).json()["session"]
    generated = client.post(f"/api/sessions/{session['id']}/generate", json={})
    article_id = generated.json()["article_id"]

    articles = client.get("/api/articles").json()
    assert any(item["id"] == article_id for item in articles)
    assert {"id", "session_title", "account_name", "status"}.issubset(articles[0])

    audits = client.get("/api/audit-logs").json()
    assert any(item["resource_id"] == article_id for item in audits)


def test_article_list_has_draft_box_preview_metadata(client):
    session = client.post("/api/sessions", json={"title": "草稿箱卡片任务"}).json()["session"]
    image = client.post(
        f"/api/sessions/{session['id']}/assets",
        files={"file": ("cover.png", png_payload(), "image/png")},
    )
    assert image.status_code == 200
    generated = client.post(f"/api/sessions/{session['id']}/generate", json={})
    assert generated.status_code == 200
    article_id = generated.json()["article_id"]

    articles = client.get("/api/articles").json()
    article = next(item for item in articles if item["id"] == article_id)
    assert article["preview_asset_id"] == image.json()["id"]
    assert article["created_at"]
    assert article["updated_at"]
    assert article["account_name"]


def test_dashboard_reports_queue_counter(client):
    dashboard = client.get("/api/dashboard").json()
    assert "queued" in dashboard
    assert dashboard["queued"] >= 0
