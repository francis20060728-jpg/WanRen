from fastapi.testclient import TestClient

from wanren.app import create_app


def make_client(tmp_path, env):
    settings = {
        "WANREN_DB_PATH": str(tmp_path / "auth.sqlite3"),
        "WANREN_STORAGE_PATH": str(tmp_path / "auth-storage"),
        "WANREN_PUBLISHER": "mock",
        "WANREN_WORKFLOW_MODE": "inline",
        "WANREN_AUTH_MODE": "production",
        "WANREN_ADMIN_PASSWORD": "production-admin-password",
        "WANREN_APP_SECRET_KEY": "test-app-secret-key-0000000000000001",
    }
    settings.update(env)
    return TestClient(create_app(settings))


def test_production_requires_app_secret_key(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="WANREN_APP_SECRET_KEY"):
        create_app(
            {
                "WANREN_DB_PATH": str(tmp_path / "nosecret.sqlite3"),
                "WANREN_STORAGE_PATH": str(tmp_path / "nosecret-storage"),
                "WANREN_AUTH_MODE": "production",
                "WANREN_ADMIN_PASSWORD": "production-admin-password",
            }
        )


def test_production_disables_registration_by_default(tmp_path):
    with make_client(tmp_path, {}) as client:
        config = client.get("/api/auth/config").json()
        assert config["registration_enabled"] is False
        response = client.post(
            "/api/auth/register",
            json={
                "email": "user@example.com",
                "password": "strong-pass-1",
                "confirm_password": "strong-pass-1",
            },
        )
        assert response.status_code == 403


def test_registration_role_is_restricted(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="WANREN_REGISTRATION_ROLE"):
        create_app(
            {
                "WANREN_DB_PATH": str(tmp_path / "invalid.sqlite3"),
                "WANREN_STORAGE_PATH": str(tmp_path / "invalid-storage"),
                "WANREN_AUTH_MODE": "production",
                "WANREN_ADMIN_PASSWORD": "production-admin-password",
                "WANREN_APP_SECRET_KEY": "test-app-secret-key-0000000000000002",
                "WANREN_REGISTRATION_ROLE": "ADMIN",
            }
        )


def test_registration_can_be_explicitly_disabled_in_dev(tmp_path):
    with make_client(
        tmp_path,
        {
            "WANREN_AUTH_MODE": "dev",
            "WANREN_REGISTRATION_ENABLED": "false",
        },
    ) as client:
        assert client.get("/api/auth/config").json()["registration_enabled"] is False
        response = client.post(
            "/api/auth/register",
            json={
                "email": "disabled@example.com",
                "password": "strong-pass-1",
                "confirm_password": "strong-pass-1",
            },
        )
        assert response.status_code == 403


def test_registration_role_can_be_restricted_to_viewer(tmp_path):
    with make_client(
        tmp_path,
        {
            "WANREN_AUTH_MODE": "dev",
            "WANREN_REGISTRATION_ENABLED": "true",
            "WANREN_REGISTRATION_ROLE": "VIEWER",
        },
    ) as client:
        response = client.post(
            "/api/auth/register",
            json={
                "email": "viewer@example.com",
                "password": "strong-pass-1",
                "confirm_password": "strong-pass-1",
            },
        )
        assert response.status_code == 200
        assert response.json()["user"]["role"] == "VIEWER"
        assert "session.write" not in response.json()["user"]["permissions"]
