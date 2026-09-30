from wanren.schemas import AccountCreate


def test_multiple_accounts_have_isolated_styles(client):
    account = client.post(
        "/api/accounts",
        json={
            "name": "品牌服务号",
            "app_id": "wx-test-style",
            "style": {"accent": "#8a4b2a", "text": "#222222", "font": "serif"},
        },
    )
    assert account.status_code == 200
    account_id = account.json()["id"]
    session = client.post(
        "/api/sessions",
        json={"title": "多账号样式任务", "official_account_id": account_id},
    )
    assert session.status_code == 200
    assert session.json()["session"]["official_account_id"] == account_id
    settings = next(x for x in client.get("/api/accounts").json() if x["id"] == account_id)["settings"]
    assert "wx-test-style" in settings


def test_account_name_can_be_synced_and_used_by_articles(client):
    response = client.patch(
        "/api/accounts/oa_default",
        json={"name": "演示公众号"},
    )
    assert response.status_code == 200
    assert response.json()["name"] == "演示公众号"
    assert any(
        account["name"] == "演示公众号"
        for account in client.get("/api/accounts").json()
    )


def test_session_defaults_to_ready_account_in_wechat_mode(tmp_path):
    from fastapi.testclient import TestClient

    from wanren.app import create_app

    app = create_app(
        {
            "WANREN_DB_PATH": str(tmp_path / "default-account.sqlite3"),
            "WANREN_STORAGE_PATH": str(tmp_path / "storage"),
            "WANREN_PUBLISHER": "wechat",
            "WANREN_WORKFLOW_MODE": "inline",
            "WANREN_AUTH_MODE": "disabled",
        }
    )
    with TestClient(app) as wechat_client:
        service = wechat_client.app.state.service
        account = service.create_account(
            AccountCreate(name="演示公众号", app_id="wx-ready-account")
        )
        with service.db.tx():
            service.db.execute(
                "UPDATE official_account SET token_status='READY' WHERE id=?",
                (account["id"],),
            )
        created = wechat_client.post(
            "/api/sessions",
            json={"title": "默认真实账号任务"},
        )
        assert created.status_code == 200
        assert created.json()["session"]["official_account_id"] == account["id"]
