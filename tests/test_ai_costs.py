import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from wanren.app import create_app
from wanren.config import load_settings
from wanren.database import Database, utc_now


def cost_client(tmp_path, env=None):
    settings = {
        "WANREN_DB_PATH": str(tmp_path / "costs.sqlite3"),
        "WANREN_STORAGE_PATH": str(tmp_path / "costs-storage"),
        "WANREN_PUBLISHER": "mock",
        "WANREN_WORKFLOW_MODE": "inline",
        "WANREN_AUTH_MODE": "disabled",
        "WANREN_TEXT_INPUT_PRICE_PER_M": "2",
        "WANREN_TEXT_OUTPUT_PRICE_PER_M": "4",
    }
    settings.update(env or {})
    return TestClient(create_app(settings))


def test_successful_generation_records_model_cost(tmp_path):
    with cost_client(tmp_path) as client:
        session = client.post("/api/sessions", json={"title": "AI 成本任务"}).json()["session"]
        response = client.post(f"/api/sessions/{session['id']}/generate", json={})
        assert response.status_code == 200
        service = client.app.state.service
        run = service.db.query_one(
            "SELECT * FROM ai_run WHERE session_id=? ORDER BY created_at DESC LIMIT 1",
            (session["id"],),
        )
        expected = run["input_tokens"] * 2 / 1_000_000 + run["output_tokens"] * 4 / 1_000_000
        assert run["cost"] == pytest.approx(expected, abs=1e-8)

        dashboard = client.get("/api/dashboard").json()
        assert dashboard["ai_cost"] == pytest.approx(expected, abs=1e-8)
        assert dashboard["ai_cost_current_month"] == pytest.approx(expected, abs=1e-8)
        assert dashboard["ai_monthly_budget_usd"] == 0
        assert "wanren_ai_cost_usd_total" in client.get("/metrics").text


def test_model_specific_price_overrides_default(tmp_path):
    model_price = json.dumps(
        {"builtin/local-editorial-v2": {"input_per_million": 3, "output_per_million": 6}}
    )
    with cost_client(tmp_path, {"WANREN_TEXT_MODEL_PRICES": model_price}) as client:
        session = client.post("/api/sessions", json={"title": "模型单价任务"}).json()["session"]
        client.post(f"/api/sessions/{session['id']}/generate", json={})
        run = client.app.state.service.db.query_one(
            "SELECT * FROM ai_run WHERE session_id=?", (session["id"],)
        )
        expected = run["input_tokens"] * 3 / 1_000_000 + run["output_tokens"] * 6 / 1_000_000
        assert run["cost"] == pytest.approx(expected, abs=1e-8)


def test_monthly_budget_blocks_new_generation_after_exhaustion(tmp_path):
    with cost_client(tmp_path, {"WANREN_TEXT_MONTHLY_BUDGET_USD": "0.5"}) as client:
        session = client.post("/api/sessions", json={"title": "预算耗尽任务"}).json()["session"]
        service = client.app.state.service
        with service.db.tx():
            service.db.execute(
                """
                INSERT INTO ai_run
                (id,session_id,task_type,provider,model,input_tokens,output_tokens,latency_ms,cost,status,created_at)
                VALUES (?,?,?,?,?,1000,0,1,1.0,'SUCCESS',?)
                """,
                ("ai_seed_cost", session["id"], "WRITE", "test", "model", utc_now()),
            )
        response = client.post(f"/api/sessions/{session['id']}/generate", json={})
        assert response.status_code == 402
        assert "AI monthly budget exhausted" in response.json()["detail"]
        assert service.db.query_one(
            "SELECT COUNT(*) AS value FROM article WHERE session_id=?", (session["id"],)
        )["value"] == 0
        # Budget rejection is not a model call and must not create a second ai_run.
        assert service.db.query_one("SELECT COUNT(*) AS value FROM ai_run")["value"] == 1


def test_ai_budget_health_alerts_and_component_cost(tmp_path):
    with cost_client(tmp_path, {"WANREN_TEXT_MONTHLY_BUDGET_USD": "1.0"}) as client:
        session = client.post("/api/sessions", json={"title": "预算告警任务"}).json()["session"]
        service = client.app.state.service
        with service.db.tx():
            service.db.execute(
                """
                INSERT INTO ai_run
                (id,session_id,task_type,provider,model,input_tokens,output_tokens,latency_ms,cost,status,created_at)
                VALUES (?,?,?,?,?,0,0,1,0.9,'SUCCESS',?)
                """,
                ("ai_seed_alert", session["id"], "WRITE", "test", "model", utc_now()),
            )
        health = client.get("/api/system/health").json()
        assert health["components"]["ai"]["month_cost_usd"] == pytest.approx(0.9)
        assert health["components"]["ai"]["monthly_budget_usd"] == 1.0
        assert any(alert["code"] == "AI_BUDGET_NEAR_LIMIT" for alert in health["alerts"])


def test_invalid_model_price_configuration(tmp_path):
    with pytest.raises(ValueError, match="model prices"):
        load_settings(
            {
                "WANREN_TEXT_MODEL_PRICES": json.dumps(
                    {"local/local-editorial-v2": {"input_per_million": -1, "output_per_million": 2}}
                )
            }
        )


def test_existing_ai_run_database_is_migrated_with_cost(tmp_path):
    path = tmp_path / "migration.sqlite3"
    original = Database(path)
    original.execute("ALTER TABLE ai_run DROP COLUMN cost")
    original.close()
    migrated = Database(path)
    columns = {row["name"] for row in migrated.query_all("PRAGMA table_info(ai_run)")}
    migrated.close()
    assert "cost" in columns
