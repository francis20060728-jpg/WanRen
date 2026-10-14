from fastapi.testclient import TestClient

from wanren.app import create_app


def auth_client(tmp_path):
    app = create_app(
        {
            "WANREN_DB_PATH": str(tmp_path / "auth.sqlite3"),
            "WANREN_STORAGE_PATH": str(tmp_path / "auth-storage"),
            "WANREN_PUBLISHER": "mock",
            "WANREN_WORKFLOW_MODE": "inline",
            "WANREN_AUTH_MODE": "dev",
        }
    )
    return TestClient(app)


def login(client, email="admin@wanren.local", password="wanren-dev-admin"):
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200
    return response.json()


def test_current_user_exposes_permissions(tmp_path):
    with auth_client(tmp_path) as client:
        admin = login(client)
        response = client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {admin['token']}"}
        )
        assert response.status_code == 200
        assert "user.manage" in response.json()["permissions"]
        assert response.json()["authenticated"] is True


def test_api_requires_authentication(tmp_path):
    with auth_client(tmp_path) as client:
        response = client.get("/api/dashboard")
        assert response.status_code == 401
        login(client)
        assert client.get("/api/dashboard").status_code == 200


def test_login_rejects_invalid_password(tmp_path):
    with auth_client(tmp_path) as client:
        response = client.post(
            "/api/auth/login",
            json={"email": "admin@wanren.local", "password": "wrong-password"},
        )
        assert response.status_code == 401


def test_bootstrap_adds_wanren_admin_to_existing_database(tmp_path):
    db_path = tmp_path / "existing-auth.sqlite3"
    legacy_env = {
        "WANREN_DB_PATH": str(db_path),
        "WANREN_STORAGE_PATH": str(tmp_path / "storage"),
        "WANREN_AUTH_MODE": "dev",
        "WANREN_ADMIN_EMAIL": "admin@legacy.test",
        "WANREN_ADMIN_PASSWORD": "legacy-admin-password",
    }
    with TestClient(create_app(legacy_env)) as client:
        response = client.post(
            "/api/auth/login",
            json={"email": "admin@legacy.test", "password": "legacy-admin-password"},
        )
        assert response.status_code == 200

    with TestClient(
        create_app(
            {
                "WANREN_DB_PATH": str(db_path),
                "WANREN_STORAGE_PATH": str(tmp_path / "storage"),
                "WANREN_AUTH_MODE": "dev",
            }
        )
    ) as client:
        assert client.post(
            "/api/auth/login",
            json={"email": "admin@wanren.local", "password": "wanren-dev-admin"},
        ).status_code == 200
        assert client.post(
            "/api/auth/login",
            json={"email": "admin@legacy.test", "password": "legacy-admin-password"},
        ).status_code == 200


def test_rbac_restricts_publisher_from_editing(tmp_path):
    with auth_client(tmp_path) as client:
        admin = login(client)
        created = client.post(
            "/api/users",
            headers={"Authorization": f"Bearer {admin['token']}"},
            json={
                "email": "publisher@test.local",
                "name": "发布员",
                "password": "publisher-password",
                "role": "PUBLISHER",
            },
        )
        assert created.status_code == 200
        publisher = login(client, "publisher@test.local", "publisher-password")
        headers = {"Authorization": f"Bearer {publisher['token']}"}
        assert client.get("/api/dashboard", headers=headers).status_code == 200
        assert "publish.submit" in client.get("/api/auth/me", headers=headers).json()["permissions"]
        assert "session.write" not in client.get("/api/auth/me", headers=headers).json()["permissions"]
        assert client.post("/api/sessions", headers=headers, json={"title": "权限任务"}).status_code == 403
        assert client.get("/api/audit-logs", headers=headers).status_code == 403


def test_logout_invalidates_session(tmp_path):
    with auth_client(tmp_path) as client:
        admin = login(client)
        headers = {"Authorization": f"Bearer {admin['token']}"}
        assert client.post("/api/auth/logout", headers=headers).status_code == 200
        assert client.get("/api/dashboard", headers=headers).status_code == 401


def test_registration_config_and_successful_login(tmp_path):
    with auth_client(tmp_path) as client:
        config = client.get("/api/auth/config")
        assert config.status_code == 200
        assert config.json()["registration_enabled"] is True
        assert config.json()["registration_role"] == "EDITOR"

        registered = client.post(
            "/api/auth/register",
            json={
                "email": "New.User@example.com",
                "password": "strong-pass-123",
                "confirm_password": "strong-pass-123",
            },
        )
        assert registered.status_code == 200
        payload = registered.json()
        assert payload["created"] is True
        assert payload["user"]["email"] == "new.user@example.com"
        assert payload["user"]["role"] == "EDITOR"

        dashboard = client.get(
            "/api/dashboard", headers={"Authorization": f"Bearer {payload['token']}"}
        )
        assert dashboard.status_code == 200


def test_registration_edge_cases(tmp_path):
    cases = [
        (
            "mismatch",
            {"email": "user@example.com", "password": "strong-pass-1", "confirm_password": "different-1"},
            400,
            "do not match",
        ),
        (
            "invalid-email",
            {"email": "not-an-email", "password": "strong-pass-1", "confirm_password": "strong-pass-1"},
            400,
            "Invalid email",
        ),
        (
            "weak-password",
            {"email": "user@example.com", "password": "password1", "confirm_password": "password1"},
            400,
            "too common",
        ),
        (
            "letters-only",
            {"email": "user@example.com", "password": "strongpassword", "confirm_password": "strongpassword"},
            400,
            "letters and numbers",
        ),
        (
            "short-password",
            {"email": "user@example.com", "password": "short1", "confirm_password": "short1"},
            422,
            "8",
        ),
        (
            "email-with-spaces",
            {"email": " spaced @example.com ", "password": "strong-pass-1", "confirm_password": "strong-pass-1"},
            400,
            "Invalid email",
        ),
    ]
    with auth_client(tmp_path) as test_client:
        for _, payload, expected_status, expected_message in cases:
            response = test_client.post("/api/auth/register", json=payload)
            assert response.status_code == expected_status
            assert expected_message in str(response.json()["detail"])


def test_duplicate_registration_returns_conflict(tmp_path):
    with auth_client(tmp_path) as client:
        payload = {
            "email": "duplicate@example.com",
            "password": "strong-pass-1",
            "confirm_password": "strong-pass-1",
        }
        first = client.post("/api/auth/register", json=payload)
        assert first.status_code == 200
        duplicate = client.post(
            "/api/auth/register",
            json={**payload, "email": payload["email"].upper()},
        )
        assert duplicate.status_code == 409
        assert "already registered" in duplicate.json()["detail"]


def test_registration_session_cookie_security(tmp_path):
    with auth_client(tmp_path) as client:
        response = client.post(
            "/api/auth/register",
            json={
                "email": "cookie@example.com",
                "password": "strong-pass-1",
                "confirm_password": "strong-pass-1",
            },
        )
        cookie = response.headers["set-cookie"]
        assert "wanren_session=" in cookie
        assert "HttpOnly" in cookie
        assert "SameSite=lax" in cookie or "samesite=lax" in cookie
        assert "Secure" not in cookie  # dev mode runs behind local HTTP


def test_create_user_duplicate_email_returns_400(tmp_path):
    with auth_client(tmp_path) as client:
        admin = login(client)
        headers = {"Authorization": f"Bearer {admin['token']}"}
        payload = {
            "email": "once@test.local",
            "name": "首次创建",
            "password": "strong-pass-1",
            "role": "EDITOR",
        }
        assert client.post("/api/users", headers=headers, json=payload).status_code == 200
        duplicate = client.post("/api/users", headers=headers, json=payload)
        assert duplicate.status_code == 400
        assert "已被使用" in duplicate.json()["detail"]
