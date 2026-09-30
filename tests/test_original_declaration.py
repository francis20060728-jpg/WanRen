from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from wanren.app import create_app
from wanren.database import Database
from wanren.publishing import PublishResult
from tests.conftest import png_payload


class FakeWeChatPublisher:
    name = "fake-wechat"

    async def asset_urls(self, assets):
        if getattr(FakeWeChatPublisher, "reject_ip", False):
            raise RuntimeError(
                "WeChat API error: {'errcode': 40164, 'errmsg': 'invalid ip 203.0.113.9 not in whitelist'}"
            )
        return {asset.asset_id: f"https://mmbiz.qpic.cn/test/{asset.asset_id}" for asset in assets}

    async def create_draft(self, **kwargs):
        return PublishResult("WECHAT_DRAFT", "draft-media-id")

    async def publish(self, external_id, account):
        assert external_id == "draft-media-id"
        return PublishResult("PUBLISHING", "publish-job-id")


def wechat_client(tmp_path: Path, monkeypatch):
    app = create_app(
        {
            "WANREN_DB_PATH": str(tmp_path / "original.sqlite3"),
            "WANREN_STORAGE_PATH": str(tmp_path / "storage"),
            "WANREN_PUBLISHER": "wechat",
            "WANREN_WORKFLOW_MODE": "inline",
            "WANREN_AUTH_MODE": "disabled",
        }
    )
    monkeypatch.setattr(
        app.state.service,
        "_publisher",
        lambda account: FakeWeChatPublisher(),
    )
    return TestClient(app)


def create_approved_article(client: TestClient):
    session = client.post(
        "/api/sessions", json={"title": "原创声明任务"}
    ).json()["session"]
    client.post(
        f"/api/sessions/{session['id']}/assets",
        files={"file": ("cover.png", png_payload(), "image/png")},
    )
    generated = client.post(
        f"/api/sessions/{session['id']}/generate", json={}
    ).json()
    article_id = generated["article_id"]
    client.post(
        f"/api/articles/{article_id}/approve", json={"reviewer": "reviewer"}
    )
    return article_id


def test_wechat_publish_requires_version_bound_original_confirmation(tmp_path, monkeypatch):
    with wechat_client(tmp_path, monkeypatch) as client:
        article_id = create_approved_article(client)

        confirm_before_draft = client.post(
            f"/api/articles/{article_id}/original-declaration",
            json={"reviewer": "publisher"},
        )
        assert confirm_before_draft.status_code == 409

        direct_publish = client.post(
            f"/api/articles/{article_id}/publish", json={"mode": "publish"}
        )
        assert direct_publish.status_code == 409
        assert "original declaration" in direct_publish.json()["detail"].lower()

        schedule = client.post(
            f"/api/articles/{article_id}/schedule",
            json={"scheduled_at": "2099-01-01T00:00:00+00:00"},
        )
        assert schedule.status_code == 409

        draft = client.post(
            f"/api/articles/{article_id}/publish", json={"mode": "draft"}
        )
        assert draft.status_code == 200
        assert draft.json()["status"] == "WECHAT_DRAFT"
        assert draft.json()["external_id"] == "draft-media-id"

        confirmed = client.post(
            f"/api/articles/{article_id}/original-declaration",
            json={"reviewer": "publisher"},
        )
        assert confirmed.status_code == 200
        article = confirmed.json()
        assert article["original_declaration_status"] == "MANUAL_CONFIRMED"
        assert article["original_declaration_version_id"] == article["current_version_id"]

        published = client.post(
            f"/api/articles/{article_id}/publish", json={"mode": "publish"}
        )
        assert published.status_code == 200
        assert published.json()["id"] == draft.json()["id"]
        assert published.json()["mode"] == "publish"
        assert published.json()["status"] == "PUBLISHING"
        assert published.json()["external_id"] == "publish-job-id"


def test_human_edit_resets_original_confirmation(tmp_path, monkeypatch):
    with wechat_client(tmp_path, monkeypatch) as client:
        article_id = create_approved_article(client)
        draft = client.post(
            f"/api/articles/{article_id}/publish", json={"mode": "draft"}
        )
        assert draft.status_code == 200
        confirmed = client.post(
            f"/api/articles/{article_id}/original-declaration",
            json={"reviewer": "publisher"},
        )
        assert confirmed.status_code == 200

        article = client.get(f"/api/articles/{article_id}").json()["article"]
        content = json.loads(article["content_json"])
        content["sections"][0]["heading"] = "人工修改后的段落"
        updated = client.put(
            f"/api/articles/{article_id}",
            json={
                "title": article["title"],
                "summary": article["summary"],
                "sections": content["sections"],
                "change_summary": "修改后必须重新确认原创",
            },
        )
        assert updated.status_code == 200
        reset_article = client.get(f"/api/articles/{article_id}").json()["article"]
        assert reset_article["original_declaration_status"] is None
        assert reset_article["original_declaration_version_id"] is None

        blocked = client.post(
            f"/api/articles/{article_id}/publish", json={"mode": "publish"}
        )
        assert blocked.status_code == 409


def test_mock_publisher_rejects_original_declaration_endpoint(tmp_path, monkeypatch):
    app = create_app(
        {
            "WANREN_DB_PATH": str(tmp_path / "mock.sqlite3"),
            "WANREN_STORAGE_PATH": str(tmp_path / "storage"),
            "WANREN_PUBLISHER": "mock",
            "WANREN_WORKFLOW_MODE": "inline",
            "WANREN_AUTH_MODE": "disabled",
        }
    )
    with TestClient(app) as client:
        article_id = create_approved_article(client)
        response = client.post(
            f"/api/articles/{article_id}/original-declaration",
            json={"reviewer": "publisher"},
        )
        assert response.status_code == 400


def test_wechat_ip_whitelist_error_returns_actionable_502(tmp_path, monkeypatch):
    FakeWeChatPublisher.reject_ip = True
    try:
        with wechat_client(tmp_path, monkeypatch) as client:
            article_id = create_approved_article(client)
            response = client.post(
                f"/api/articles/{article_id}/publish", json={"mode": "draft"}
            )
            assert response.status_code == 502
            assert "203.0.113.9" in response.json()["detail"]
            assert "IP whitelist" in response.json()["detail"]
    finally:
        FakeWeChatPublisher.reject_ip = False


def test_legacy_article_schema_gets_original_declaration_columns(tmp_path):
    path = tmp_path / "legacy-original.sqlite3"
    original = Database(path)
    original.close()
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE article DROP COLUMN original_declaration_status")
        connection.execute("ALTER TABLE article DROP COLUMN original_declaration_version_id")
        connection.execute("ALTER TABLE article DROP COLUMN original_declaration_confirmed_by")
        connection.execute("ALTER TABLE article DROP COLUMN original_declaration_confirmed_at")

    migrated = Database(path)
    try:
        columns = {
            row["name"] for row in migrated.query_all("PRAGMA table_info(article)")
        }
        assert {
            "original_declaration_status",
            "original_declaration_version_id",
            "original_declaration_confirmed_by",
            "original_declaration_confirmed_at",
        }.issubset(columns)
    finally:
        migrated.close()
