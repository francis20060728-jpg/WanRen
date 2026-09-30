from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from wanren.app import create_app
from wanren.database import Database
from wanren.search import DuckDuckGoSearchProvider, SearchReference


def create_app_with_env(tmp_path: Path, extra: dict[str, str] | None = None) -> TestClient:
    env = {
        "WANREN_DB_PATH": str(tmp_path / "template-search.sqlite3"),
        "WANREN_STORAGE_PATH": str(tmp_path / "storage"),
        "WANREN_PUBLISHER": "mock",
        "WANREN_WORKFLOW_MODE": "inline",
        "WANREN_AUTH_MODE": "disabled",
    }
    env.update(extra or {})
    return TestClient(create_app(env))


def test_duckduckgo_provider_parses_and_normalizes_results(tmp_path):
    payload = """
    <html><body>
      <div class="result">
        <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=abc">Paper Account A</a>
        <a class="result__snippet" href="#"> Long headline, short paragraphs, figure explanation. </a>
      </div>
      <div class="result">
        <a class="result__a" href="https://example.com/b">Paper Account B</a>
        <a class="result__snippet" href="#"> Uses a metrics table and method diagram. </a>
      </div>
    </body></html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["q"] == "论文 公众号"
        return httpx.Response(200, text=payload)

    provider = DuckDuckGoSearchProvider(
        httpx.Client(transport=httpx.MockTransport(handler)),
        timeout_seconds=5,
    )
    results = provider.search("论文 公众号", 5)
    assert [item.url for item in results] == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert results[0].title == "Paper Account A"
    assert "figure explanation" in results[0].snippet


def test_template_search_endpoint_uses_provider_and_can_be_disabled(tmp_path, monkeypatch):
    class FakeProvider:
        name = "fake-search"

        def search(self, query, limit):
            assert query == "论文解读 公众号"
            assert limit == 5
            return [
                SearchReference(
                    "论文公众号排版参考",
                    "https://example.com/paper-style",
                    "首图解释研究场景，指标图表后接解读。",
                )
            ]

    with create_app_with_env(tmp_path) as client:
        client.app.state.service.search_provider = FakeProvider()
        response = client.post("/api/templates/search", json={"query": "论文解读 公众号"})
        assert response.status_code == 200
        payload = response.json()
        assert payload["provider"] == "fake-search"
        assert payload["results"][0]["url"] == "https://example.com/paper-style"

        client.app.state.service.search_provider = None
        disabled = client.post("/api/templates/search", json={"query": "论文解读"})
        assert disabled.status_code == 503


def test_auto_search_generates_template_with_reference_evidence(tmp_path, monkeypatch):
    class FakeProvider:
        name = "fake-search"

        def search(self, query, limit):
            return [
                SearchReference(
                    "论文公众号排版参考",
                    "https://example.com/paper-style",
                    "首图解释研究场景，指标图表后接解读。",
                )
            ]

    with create_app_with_env(tmp_path) as client:
        monkeypatch.setattr(client.app.state.service, "search_provider", FakeProvider())
        response = client.post(
            "/api/templates/generate",
            json={
                "description": "生成科研论文解读模板，需要图表解释和指标解读。",
                "auto_search": True,
                "search_query": "论文解读 公众号 排版",
            },
        )
        assert response.status_code == 200
        template = response.json()
        assert template["source"] == "AI_REFERENCE"
        references = json.loads(template["references_json"])
        assert references[0]["type"] == "web_search"
        assert references[0]["provider"] == "fake-search"
        assert references[0]["query"] == "论文解读 公众号 排版"
        assert references[0]["url"] == "https://example.com/paper-style"


def test_template_update_creates_version_snapshots(client):
    created = client.post(
        "/api/templates",
        json={
            "name": "版本模板",
            "description": "v1",
            "sections": [
                {"id": "one", "heading": "一", "guidance": "一"},
                {"id": "two", "heading": "二", "guidance": "二"},
                {"id": "three", "heading": "三", "guidance": "三"},
            ],
        },
    ).json()
    sections = [
        {"id": "new_one", "heading": "新一", "guidance": "新指导", "image_policy": "required"},
        {"id": "new_two", "heading": "新二", "guidance": "新指导", "image_policy": "optional"},
        {"id": "new_three", "heading": "新三", "guidance": "新指导", "image_policy": "none"},
    ]
    updated = client.put(
        f"/api/templates/{created['id']}",
        json={
            "name": "版本模板 v2",
            "description": "v2",
            "sections": sections,
            "change_summary": "调整图片策略",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["version"] == 2

    versions = client.get(f"/api/templates/{created['id']}/versions").json()
    assert [item["version"] for item in versions] == [1]
    assert versions[0]["name"] == "版本模板"
    assert versions[0]["change_summary"] == "调整图片策略"
    assert json.loads(versions[0]["structure_json"])[0]["id"] == "one"
    assert updated.json()["name"] == "版本模板 v2"


def test_builtin_template_cannot_be_edited(client):
    templates = client.get("/api/templates").json()
    built_in = next(item for item in templates if item["source"] == "BUILTIN")
    response = client.put(
        f"/api/templates/{built_in['id']}",
        json={
            "name": "不应更新",
            "sections": [
                {"id": "a_one", "heading": "A", "guidance": "a"},
                {"id": "a_two", "heading": "B", "guidance": "b"},
                {"id": "a_three", "heading": "C", "guidance": "c"},
            ],
        },
    )
    assert response.status_code == 403


def test_template_update_rejects_duplicate_name(client):
    first = client.post(
        "/api/templates",
        json={
            "name": "唯一模板",
            "sections": [
                {"id": "one", "heading": "一", "guidance": "一"},
                {"id": "two", "heading": "二", "guidance": "二"},
                {"id": "three", "heading": "三", "guidance": "三"},
            ],
        },
    ).json()
    second = client.post(
        "/api/templates",
        json={
            "name": "另一个模板",
            "sections": [
                {"id": "one", "heading": "一", "guidance": "一"},
                {"id": "two", "heading": "二", "guidance": "二"},
                {"id": "three", "heading": "三", "guidance": "三"},
            ],
        },
    ).json()
    response = client.put(
        f"/api/templates/{second['id']}",
        json={
            "name": "唯一模板",
            "sections": [
                {"id": "one", "heading": "一", "guidance": "一"},
                {"id": "two", "heading": "二", "guidance": "二"},
                {"id": "three", "heading": "三", "guidance": "三"},
            ],
        },
    )
    assert response.status_code == 409


def test_legacy_template_schema_gets_references_and_versions(tmp_path):
    path = tmp_path / "legacy-template.sqlite3"
    original = Database(path)
    original.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE article_template_version")
        connection.execute("ALTER TABLE article_template DROP COLUMN references_json")

    migrated = Database(path)
    try:
        columns = {
            row["name"]
            for row in migrated.query_all("PRAGMA table_info(article_template)")
        }
        version_columns = {
            row["name"]
            for row in migrated.query_all("PRAGMA table_info(article_template_version)")
        }
        assert "references_json" in columns
        assert {
            "template_id", "version", "structure_json", "style_json",
            "references_json", "change_summary",
        }.issubset(version_columns)
    finally:
        migrated.close()
