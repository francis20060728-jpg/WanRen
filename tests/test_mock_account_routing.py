"""内置本地测试账号的发布通道：全局 wechat 模式下仍必须走 MockPublisher。"""

from fastapi.testclient import TestClient

from tests.conftest import png_payload
from wanren.app import create_app


def test_builtin_mock_account_routes_to_mock_publisher_in_wechat_mode(tmp_path):
    app = create_app(
        {
            "WANREN_DB_PATH": str(tmp_path / "wanren.sqlite3"),
            "WANREN_STORAGE_PATH": str(tmp_path / "storage"),
            "WANREN_PUBLISHER": "wechat",
            "WANREN_MAX_UPLOAD_MB": "1",
            "WANREN_WORKFLOW_MODE": "inline",
            "WANREN_AUTH_MODE": "disabled",
        }
    )
    with TestClient(app) as client:
        accounts = client.get("/api/accounts").json()
        mock_account = next(a for a in accounts if str(a["app_id"]).startswith("mock"))
        assert mock_account["name"] == "本地测试账号"

        created = client.post(
            "/api/sessions",
            json={
                "title": "mock 通道测试",
                "goal": "g",
                "official_account_id": mock_account["id"],
            },
        )
        assert created.status_code == 200
        session_id = created.json()["session"]["id"]
        client.post(
            f"/api/sessions/{session_id}/assets",
            files={"file": ("cover.png", png_payload(), "image/png")},
        )
        generated = client.post(
            f"/api/sessions/{session_id}/generate", json={"instruction": "写一篇短文"}
        )
        assert generated.status_code == 200
        article_id = generated.json()["article_id"]

        draft = client.post(
            f"/api/articles/{article_id}/publish", json={"mode": "draft"}
        )
        assert draft.status_code == 200, draft.text
        detail = client.get(f"/api/articles/{article_id}").json()
        job = detail["publish_jobs"][0]
        assert job["status"] == "WECHAT_DRAFT"
        assert job["external_id"]
