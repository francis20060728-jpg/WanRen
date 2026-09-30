import json
from fastapi.testclient import TestClient

from wanren.app import create_app


def test_builtin_templates_are_available(client):
    templates = client.get("/api/templates").json()
    names = {item["name"] for item in templates}
    assert {"论文解读", "产品更新"}.issubset(names)
    paper = next(item for item in templates if item["name"] == "论文解读")
    assert paper["source"] == "BUILTIN"
    sections = json.loads(paper["structure_json"])
    assert len(sections) == 5
    assert sections[0]["image_policy"] == "required"


def test_user_template_changes_generated_article_structure(client):
    created = client.post(
        "/api/templates",
        json={
            "name": "科研论文模板",
            "description": "面向论文解读",
            "sections": [
                {"id": "problem", "heading": "研究问题", "guidance": "说明限制。", "image_policy": "required"},
                {"id": "method", "heading": "方法设计", "guidance": "解释模块。", "image_policy": "none"},
                {"id": "result", "heading": "结果解读", "guidance": "引用指标。", "image_policy": "optional"},
            ],
            "style": {"accent": "#123456", "text": "#222222", "font": "serif"},
        },
    )
    assert created.status_code == 200
    template_id = created.json()["id"]

    duplicate = client.post(
        "/api/templates",
        json={
            "name": "科研论文模板",
            "sections": [
                {"id": "sec_a", "heading": "A", "guidance": "a"},
                {"id": "sec_b", "heading": "B", "guidance": "b"},
                {"id": "sec_c", "heading": "C", "guidance": "c"},
            ],
        },
    )
    assert duplicate.status_code == 409

    session = client.post(
        "/api/sessions",
        json={"title": "模板任务", "template_id": template_id},
    ).json()["session"]
    image = client.post(
        f"/api/sessions/{session['id']}/assets",
        files={"file": ("figure.png", b"", "image/png")},
    )
    assert image.status_code == 400

    from tests.conftest import png_payload

    image = client.post(
        f"/api/sessions/{session['id']}/assets",
        files={"file": ("figure.png", png_payload(), "image/png")},
    ).json()
    generated = client.post(
        f"/api/sessions/{session['id']}/generate", json={}
    ).json()
    article = client.get(f"/api/articles/{generated['article_id']}").json()["article"]
    content = json.loads(article["content_json"])
    assert [section["heading"] for section in content["sections"]] == [
        "研究问题",
        "方法设计",
        "结果解读",
    ]
    assert content["sections"][0]["asset_refs"] == [image["id"]]
    assert content["sections"][1]["asset_refs"] == []


def test_ai_description_generates_versioned_template(client):
    response = client.post(
        "/api/templates/generate",
        json={
            "description": "生成论文公众号解读模板，需要图表解释和指标解读。",
            "reference": "参考风格：标题直接，段落短，首图解释研究场景。",
        },
    )
    assert response.status_code == 200
    template = response.json()
    assert template["source"] == "AI_REFERENCE"
    sections = json.loads(template["structure_json"])
    assert 3 <= len(sections) <= 8
    assert sections[0]["image_policy"] == "required"
    assert all(section["guidance"] for section in sections)
    service = client.app.state.service
    ai_runs = service.db.query_all(
        "SELECT * FROM ai_run WHERE task_type='TEMPLATE'"
    )
    assert len(ai_runs) >= 1
    assert ai_runs[-1]["status"] == "SUCCESS"


def test_account_wizard_exposes_safe_connection_metadata(client):
    account = client.post(
        "/api/accounts",
        json={
            "name": "论文公众号",
            "app_id": "wx-paper-test",
            "callback_base_url": "https://api.example.com/",
            "style": {"accent": "#175641", "text": "#262626", "font": "system"},
        },
    ).json()
    assert account["credential_env"] == "WECHAT_APP_SECRET_wx-paper-test"
    assert account["callback_token_env"] == "WECHAT_CALLBACK_TOKEN_wx-paper-test"
    assert account["callback_url"] == f"https://api.example.com/wechat/{account['id']}/callback"

    listed = client.get("/api/accounts").json()
    assert next(item for item in listed if item["id"] == account["id"])["callback_url"]
    verification = client.post(f"/api/accounts/{account['id']}/verify").json()
    assert verification["status"] == "MOCK_MODE"


def test_account_wizard_rejects_invalid_callback_origin(client):
    response = client.post(
        "/api/accounts",
        json={
            "name": "错误回调",
            "app_id": "wx-bad-callback",
            "callback_base_url": "ftp://example.com",
        },
    )
    assert response.status_code == 400


def test_wechat_account_verification_updates_status_without_leaking_token(tmp_path, monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {"access_token": "secret-token"}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json):
            assert url == "https://api.weixin.qq.com/cgi-bin/stable_token"
            assert json["appid"] == "wx-verify-test"
            assert json["secret"] == "test-secret"
            return FakeResponse()

    monkeypatch.setattr("wanren.service.httpx.AsyncClient", FakeClient)
    app = create_app(
        {
            "WANREN_DB_PATH": str(tmp_path / "verify.sqlite3"),
            "WANREN_STORAGE_PATH": str(tmp_path / "storage"),
            "WANREN_PUBLISHER": "wechat",
            "WECHAT_APP_SECRET": "test-secret",
            "WANREN_WORKFLOW_MODE": "inline",
            "WANREN_AUTH_MODE": "disabled",
        }
    )
    with TestClient(app) as verify_client:
        account = verify_client.post(
            "/api/accounts",
            json={"name": "验证账号", "app_id": "wx-verify-test"},
        ).json()
        response = verify_client.post(f"/api/accounts/{account['id']}/verify")
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "READY"
        assert "access_token" not in payload
        row = verify_client.app.state.service.db.query_one(
            "SELECT * FROM official_account WHERE id=?", (account["id"],)
        )
        assert row["token_status"] == "READY"


def test_missing_template_rejects_session(client):
    response = client.post(
        "/api/sessions",
        json={"title": "缺失模板任务", "template_id": "tpl_missing"},
    )
    assert response.status_code == 404
