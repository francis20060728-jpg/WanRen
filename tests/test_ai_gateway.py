import json
import re

import httpx
import pytest

from wanren.ai import GenerationError, OpenAICompatibleAdapter, build_text_adapter
from wanren.config import load_settings
from wanren.context import SessionContext


def make_context(client):
    service = client.app.state.service
    session = client.post("/api/sessions", json={"title": "AI Gateway 任务"}).json()["session"]
    row = service.db.query_one("SELECT * FROM content_session WHERE id = ?", (session["id"],))
    assets = service.db.query_all(
        "SELECT * FROM session_asset WHERE session_id = ?", (session["id"],)
    )
    return SessionContext(session=row, assets=assets)


def model_response(content, usage=None):
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 137, "completion_tokens": 321, **(usage or {})},
    }


def article_payload():
    return {
        "title": "模型生成的公众号文章",
        "summary": "基于任务素材生成的结构化摘要。",
        "sections": [
            {
                "id": "sec_01",
                "heading": "核心信息",
                "paragraphs": ["第一段来自任务素材。", "第二段保留来源引用。"],
                "asset_refs": [],
            }
        ],
    }


def adapter(transport, **kwargs):
    return OpenAICompatibleAdapter(
        model="test-model",
        api_key="test-key",
        api_base="https://model.test/v1",
        max_retries=kwargs.pop("max_retries", 2),
        fallback_local=kwargs.pop("fallback_local", False),
        http_client=httpx.Client(transport=transport),
        **kwargs,
    )


def test_local_adapter_reports_real_estimated_usage(client):
    generated = client.post(
        "/api/sessions", json={"title": "本地模型计量任务"}
    ).json()["session"]
    response = client.post(f"/api/sessions/{generated['id']}/generate", json={})
    assert response.status_code == 200
    run = client.app.state.service.db.query_one(
        "SELECT * FROM ai_run WHERE session_id = ? ORDER BY created_at DESC LIMIT 1",
        (generated["id"],),
    )
    assert run["provider"] == "builtin"
    assert run["model"] == "local-editorial-v2"
    assert run["input_tokens"] > 0
    assert run["output_tokens"] > 0
    assert run["latency_ms"] >= 0
    assert run["status"] == "SUCCESS"


def test_openai_compatible_success_parses_json_and_usage(client):
    context = make_context(client)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=model_response(json.dumps(article_payload(), ensure_ascii=False)))
    )
    result = adapter(transport).generate(context, "写一篇重点清晰的文章")
    assert result.provider == "openai_compatible"
    assert result.model == "test-model"
    assert result.article.title == "模型生成的公众号文章"
    assert result.article.editorial["style"] == "GENERAL_EXPLAINER"
    assert "像真人编辑写给读者" in OpenAICompatibleAdapter._system_prompt()

    assert result.input_tokens == 137
    assert result.output_tokens == 321
    assert result.attempts == 1


def test_openai_compatible_accepts_fenced_json(client):
    context = make_context(client)
    fenced = f"```json\n{json.dumps(article_payload(), ensure_ascii=False)}\n```"
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=model_response(fenced)))
    result = adapter(transport).generate(context, "")
    assert result.article.sections[0].id == "sec_01"


def test_model_metrics_override_heuristic_and_ground_to_article_text(client):
    context = make_context(client)
    payload = article_payload()
    payload["sections"][0]["paragraphs"].append("固定边界模型的烘干时长为 57.86 小时，全程体积收缩 64%。")
    payload["metrics"] = [
        {"value": "57.86 h", "label": "定边界模型的烘干时长"},
        {"value": "64%", "label": "烘干全程的体积收缩"},
        {"value": "99.9%", "label": "正文没有的数字"},  # 数字不在正文 → 丢弃
    ]
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=model_response(json.dumps(payload, ensure_ascii=False)))
    )
    editorial = adapter(transport).generate(context, "").article.editorial
    assert editorial["metrics"] == [
        {"value": "57.86 h", "label": "定边界模型的烘干时长"},
        {"value": "64%", "label": "烘干全程的体积收缩"},
    ]


def test_model_empty_metrics_means_no_cards(client):
    context = make_context(client)
    payload = article_payload()
    payload["metrics"] = []
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=model_response(json.dumps(payload, ensure_ascii=False)))
    )
    editorial = adapter(transport).generate(context, "").article.editorial
    assert editorial["metrics"] == []


def test_absent_metrics_fall_back_to_evidence_heuristic(client):
    context = make_context(client)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=model_response(json.dumps(article_payload(), ensure_ascii=False)))
    )
    editorial = adapter(transport).generate(context, "").article.editorial
    # 模型没给 metrics：回退证据摘要启发式（普通会话无证据数字 → 空列表，结构仍是 list）
    assert isinstance(editorial["metrics"], list)


def test_openai_compatible_retries_transient_and_recovers(client):
    context = make_context(client)
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json=model_response(json.dumps(article_payload())))

    result = adapter(httpx.MockTransport(handler)).generate(context, "")
    assert len(calls) == 2
    assert result.attempts == 2
    assert result.output_tokens == 321


def test_openai_compatible_does_not_retry_permanent_auth_error(client):
    context = make_context(client)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, json={"error": "invalid key"})

    with pytest.raises(GenerationError) as error:
        adapter(httpx.MockTransport(handler)).generate(context, "")
    assert len(calls) == 1
    assert error.value.attempts == 1
    assert "Text model failed" in str(error.value)


def test_openai_compatible_rejects_invalid_schema_after_retries(client):
    context = make_context(client)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=model_response(json.dumps({"title": ""})))

    with pytest.raises(GenerationError):
        adapter(httpx.MockTransport(handler)).generate(context, "")
    assert len(calls) == 3


def test_openai_compatible_can_fallback_to_local(client):
    context = make_context(client)
    transport = httpx.MockTransport(lambda request: httpx.Response(503, json={"error": "down"}))
    result = adapter(transport, fallback_local=True).generate(context, "回退生成")
    assert result.provider == "builtin-fallback"
    assert result.fallback_from == "openai_compatible/test-model"
    assert result.error["type"] == "HTTPStatusError"
    assert result.article.sections


def test_remote_generation_failure_is_recorded_in_ai_run(tmp_path):
    from fastapi.testclient import TestClient

    from wanren.app import create_app

    app = create_app(
        {
            "WANREN_DB_PATH": str(tmp_path / "remote.sqlite3"),
            "WANREN_STORAGE_PATH": str(tmp_path / "remote-storage"),
            "WANREN_PUBLISHER": "mock",
            "WANREN_WORKFLOW_MODE": "inline",
            "WANREN_AUTH_MODE": "disabled",
            "WANREN_TEXT_PROVIDER": "openai_compatible",
            "WANREN_TEXT_MODEL": "remote-model",
            "WANREN_TEXT_API_KEY": "test-key",
        }
    )
    with TestClient(app, raise_server_exceptions=False) as remote_client:
        created = remote_client.post("/api/sessions", json={"title": "远端失败任务"})
        assert created.status_code == 200, created.text
        session = created.json()["session"]
        remote_client.app.state.service.text_adapter = adapter(
            httpx.MockTransport(lambda request: httpx.Response(500, json={"error": "down"}))
        )
        response = remote_client.post(f"/api/sessions/{session['id']}/generate", json={})
        assert response.status_code == 502
        run = remote_client.app.state.service.db.query_one(
            "SELECT * FROM ai_run WHERE session_id = ?", (session["id"],)
        )
        assert run is not None
        assert run["provider"] == "openai_compatible"
        assert run["model"] == "test-model"
        assert run["status"] == "FAILED"
        assert run["output_tokens"] == 0
        health = remote_client.get("/api/system/health").json()
        assert health["components"]["ai"]["status"] == "DEGRADED"
        assert health["components"]["ai"]["failed"] >= 1
        assert any(alert["code"] == "AI_RUN_FAILED" for alert in health["alerts"])
        metrics = remote_client.get("/metrics").text
        assert re.search(r'wanren_ai_runs_total\{status="FAILED"\} [1-9]', metrics)


def test_remote_adapter_configuration_and_defaults(tmp_path):
    settings = load_settings(
        {
            "WANREN_TEXT_PROVIDER": "openai_compatible",
            "WANREN_TEXT_MODEL": "gpt-test",
            "WANREN_TEXT_API_KEY": "key",
            "WANREN_TEXT_API_BASE": "https://example.test/v1/",
            "WANREN_TEXT_MAX_RETRIES": "4",
            "WANREN_TEXT_FALLBACK_LOCAL": "true",
        }
    )
    assert settings.text_api_base == "https://example.test/v1"
    assert settings.text_max_retries == 4
    assert settings.text_fallback_local is True
    remote = build_text_adapter(settings)
    assert remote.model == "gpt-test"


def test_remote_adapter_requires_model_and_key():
    with pytest.raises(ValueError, match="WANREN_TEXT_MODEL"):
        load_settings({"WANREN_TEXT_PROVIDER": "openai_compatible"})
    with pytest.raises(ValueError, match="WANREN_TEXT_API_KEY"):
        load_settings(
            {
                "WANREN_TEXT_PROVIDER": "openai_compatible",
                "WANREN_TEXT_MODEL": "model",
            }
        )
