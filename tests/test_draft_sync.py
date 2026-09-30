from tests.conftest import png_payload


class DeletedDraftPublisher:
    name = "deleted-draft"

    async def get_draft(self, external_id, account):
        return None


def create_draft_article(client, title="微信草稿对账"):
    session = client.post("/api/sessions", json={"title": title}).json()["session"]
    image = client.post(
        f"/api/sessions/{session['id']}/assets",
        files={"file": ("cover.png", png_payload(), "image/png")},
    )
    assert image.status_code == 200
    generated = client.post(f"/api/sessions/{session['id']}/generate", json={}).json()
    article_id = generated["article_id"]
    approved = client.post(
        f"/api/articles/{article_id}/approve",
        json={"reviewer": "draft-sync-test"},
    )
    draft = client.post(
        f"/api/articles/{article_id}/publish",
        json={"mode": "draft"},
    )
    assert approved.status_code == 200
    assert draft.status_code == 200
    return article_id, draft.json()["id"]


def test_draft_sync_keeps_existing_mock_draft(client):
    article_id, job_id = create_draft_article(client)
    response = client.post(f"/api/articles/{article_id}/draft-sync")
    assert response.status_code == 200
    assert response.json()["status"] == "EXISTS"
    service = client.app.state.service
    article = service.db.query_one("SELECT * FROM article WHERE id=?", (article_id,))
    job = service.db.query_one("SELECT * FROM publish_job WHERE id=?", (job_id,))
    assert article["status"] == "WECHAT_DRAFT"
    assert job["status"] == "WECHAT_DRAFT"


def test_draft_sync_marks_wechat_deleted_draft_and_reopens_local_article(client):
    article_id, job_id = create_draft_article(client, "微信删除草稿对账")
    service = client.app.state.service
    service._publisher = lambda account: DeletedDraftPublisher()

    response = client.post(f"/api/articles/{article_id}/draft-sync")
    assert response.status_code == 200
    assert response.json()["status"] == "DELETED"
    article = service.db.query_one("SELECT * FROM article WHERE id=?", (article_id,))
    job = service.db.query_one("SELECT * FROM publish_job WHERE id=?", (job_id,))
    assert article["status"] == "HUMAN_APPROVED"
    assert article["original_declaration_status"] is None
    assert job["status"] == "WECHAT_DRAFT_DELETED"
    assert "Deleted in WeChat drafts" in job["last_error"]


def test_batch_sync_quarantines_drafts_on_unverified_accounts(client):
    article_id, job_id = create_draft_article(client, "未验证账号草稿")
    service = client.app.state.service
    with service.db.tx():
        service.db.execute(
            "UPDATE official_account SET token_status='API_ERROR' WHERE id='oa_default'"
        )

    response = client.post("/api/articles/draft-sync")
    assert response.status_code == 200
    payload = response.json()
    assert payload["checked"] == 0
    assert payload["skipped_unverified_accounts"] == 1
    assert payload["quarantined_unverified_drafts"] == 1
    article = service.db.query_one("SELECT * FROM article WHERE id=?", (article_id,))
    job = service.db.query_one("SELECT * FROM publish_job WHERE id=?", (job_id,))
    assert article["status"] == "HUMAN_APPROVED"
    assert job["status"] == "WECHAT_DRAFT_UNVERIFIED"
