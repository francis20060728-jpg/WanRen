"""微信草稿导入（外部对话引入）与对话删除。"""
import json

from fastapi.testclient import TestClient

from wanren.app import create_app


def _app(tmp_path, publisher="mock", **extra):
    env = {
        "WANREN_DB_PATH": str(tmp_path / "wanren.sqlite3"),
        "WANREN_STORAGE_PATH": str(tmp_path / "storage"),
        "WANREN_PUBLISHER": publisher,
        "WANREN_WORKFLOW_MODE": "inline",
        "WANREN_AUTH_MODE": "disabled",
    }
    env.update(extra)
    return create_app(env)


def _fake_wechat(monkeypatch):
    """拦截 WeChatPublisher 的 list_drafts / get_draft，返回固定草稿。"""
    from wanren.publishing import WeChatPublisher

    async def fake_list(self, account, offset=0, count=20):
        return {"total": 1, "items": [{"media_id": "MEDIA_X1", "title": "外部写的稿子", "update_time": 1758000000}]}

    async def fake_get(self, external_id, account):
        return {"news_item": [{
            "title": "外部写的稿子",
            "digest": "在别的平台写的",
            "content": '<section><p>引言在这里。</p></section><h2>核心观点</h2><p>观点一。</p><p>观点二。</p>',
        }]}

    monkeypatch.setattr(WeChatPublisher, "list_drafts", fake_list)
    monkeypatch.setattr(WeChatPublisher, "get_draft", fake_get)


def test_list_wechat_drafts_marks_imported(tmp_path, monkeypatch):
    _fake_wechat(monkeypatch)
    with TestClient(_app(tmp_path, publisher="wechat", WECHAT_APP_ID="wx1", WECHAT_APP_SECRET="s")) as client:
        # V5 起内置本地测试账号永远走 MockPublisher；微信草稿链路用真实型账号验证
        created_account = client.post(
            "/api/accounts", json={"name": "外部草稿号", "app_id": "wx1"}
        ).json()
        account_id = created_account["id"]
        result = client.get(f"/api/wechat/drafts?account_id={account_id}").json()
        assert result["total"] == 1 and result["items"][0]["media_id"] == "MEDIA_X1"
        assert result["items"][0]["imported"] is False

        # 导入
        imported = client.post("/api/wechat/drafts/import", json={"account_id": account_id, "media_id": "MEDIA_X1"}).json()
        assert imported["title"] == "外部写的稿子"
        detail = client.get(f"/api/articles/{imported['article_id']}").json()
        content = json.loads(detail["article"]["content_json"])
        assert content["sections"][0]["heading"] == "正文" and content["sections"][0]["paragraphs"] == ["引言在这里。"]
        assert content["sections"][1]["heading"] == "核心观点"
        assert content["sections"][1]["paragraphs"] == ["观点一。", "观点二。"]
        assert detail["article"]["status"] == "WECHAT_DRAFT"
        # 再列表 → 标记已导入；重复导入 → 409
        result2 = client.get(f"/api/wechat/drafts?account_id={account_id}").json()
        assert result2["items"][0]["imported"] is True
        dup = client.post("/api/wechat/drafts/import", json={"account_id": account_id, "media_id": "MEDIA_X1"})
        assert dup.status_code == 409


def test_delete_article_cascades_and_clears_empty_session(client):
    created = client.post("/api/sessions", json={"title": "删除验证任务"}).json()
    session_id = created["session"]["id"]
    generated = client.post(f"/api/sessions/{session_id}/generate", json={"instruction": ""}).json()
    article_id = generated.get("article_id") or generated.get("article", {}).get("id")

    result = client.delete(f"/api/articles/{article_id}").json()
    assert result["deleted"] == article_id and result["session_deleted"] == session_id
    assert client.get(f"/api/articles/{article_id}").status_code == 404
    # 会话已连带删除（素材级联）
    assert client.get(f"/api/sessions/{session_id}").status_code == 404


def test_delete_published_article_blocked(client):
    created = client.post("/api/sessions", json={"title": "已发布不可删"}).json()
    session_id = created["session"]["id"]
    generated = client.post(f"/api/sessions/{session_id}/generate", json={"instruction": ""}).json()
    article_id = generated.get("article_id") or generated.get("article", {}).get("id")
    # 直接把发布记录置为已发布（绕过人工流程，模拟历史发布）
    from wanren.database import new_id, utc_now

    svc = client.app.state.service
    with svc.db.tx():
        svc.db.execute(
            "INSERT INTO publish_job(id,article_id,official_account_id,mode,status,idempotency_key,article_url,retry_count,last_error,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,0,'{}',?,?)",
            (new_id("job"), article_id, "oa_default", "publish", "PUBLISHED", f"del:{article_id}", "https://mp.weixin.qq.com/s/x", utc_now(), utc_now()),
        )
    response = client.delete(f"/api/articles/{article_id}")
    assert response.status_code == 400
    assert "已发布" in response.json()["detail"]
