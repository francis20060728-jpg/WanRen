import json

from wanren.database import utc_now


def test_system_health_and_metrics_are_available(client):
    health = client.get("/api/system/health")
    assert health.status_code == 200
    payload = health.json()
    assert payload["status"] == "HEALTHY"
    assert payload["score"] == 100
    assert set(payload["components"]) >= {"database", "storage", "queue", "workflow", "ai", "publisher", "auth"}
    assert payload["alerts"] == []

    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "wanren_sessions_total" in metrics.text
    assert "wanren_workflow_queue_jobs" in metrics.text
    assert "wanren_wechat_callback_events_total" in metrics.text
    assert "wanren_wechat_callback_duplicate_deliveries_total" in metrics.text
    assert "wanren_ai_tokens_total" in metrics.text


def test_system_health_reports_workflow_and_queue_degradation(client):
    session = client.post("/api/sessions", json={"title": "监控告警任务"}).json()["session"]
    generated = client.post(f"/api/sessions/{session['id']}/generate", json={})
    run_id = generated.json()["workflow_run_id"]
    service = client.app.state.service
    with service.db.tx():
        service.db.execute(
            "UPDATE workflow_run SET status='FAILED',finished_at=? WHERE id=?",
            (utc_now(), run_id),
        )
        for index in range(10):
            queued_run = f"run_monitor_{index}"
            service.db.execute(
                """
                INSERT INTO workflow_run(id,session_id,workflow_type,status,current_node,started_at)
                VALUES (?,?,?,?,?,?)
                """,
                (queued_run, session["id"], "GENERATE_ARTICLE", "QUEUED", "START", utc_now()),
            )
            service.db.execute(
                """
                INSERT INTO workflow_queue
                (workflow_run_id,workflow_type,status,payload,attempts,available_at,created_at,updated_at)
                VALUES (?,?,?,?,0,?,?,?)
                """,
                (queued_run, "GENERATE_ARTICLE", "QUEUED", "{}", utc_now(), utc_now(), utc_now()),
            )

    payload = client.get("/api/system/health").json()
    codes = {alert["code"] for alert in payload["alerts"]}
    assert "WORKFLOW_RUN_FAILED" in codes
    assert "WORKFLOW_QUEUE_BACKLOG" in codes
    assert payload["status"] == "DEGRADED"
    assert payload["components"]["queue"]["queued"] >= 10
    assert payload["components"]["workflow"]["failed"] >= 1


def test_metrics_endpoint_does_not_require_authentication(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")


def test_monitor_lists_are_paginated_with_user_selected_page_size(client):
    session = client.post("/api/sessions", json={"title": "监控分页任务"}).json()["session"]
    service = client.app.state.service
    now = utc_now()
    with service.db.tx():
        for index in range(12):
            service.db.execute(
                """
                INSERT INTO workflow_run(id,session_id,workflow_type,status,current_node,started_at)
                VALUES (?,?,?,?,?,?)
                """,
                (f"run_page_{index}", session["id"], "GENERATE_ARTICLE", "SUCCESS", "DONE", now),
            )
        for index in range(25):
            service.audit("audit.pagination", "test", f"audit_{index}", {"index": index})

    workflows = client.get("/api/workflows?page=2&page_size=10").json()
    assert workflows["page"] == 2
    assert workflows["page_size"] == 10
    assert workflows["total"] >= 12
    assert len(workflows["items"]) > 0
    assert all(item["session_title"] == "监控分页任务" for item in workflows["items"])

    first = client.get("/api/audit-logs/page?page=1&page_size=20").json()
    second = client.get("/api/audit-logs/page?page=2&page_size=20").json()
    assert first["page"] == 1
    assert second["page"] == 2
    assert first["page_size"] == second["page_size"] == 20
    assert first["total"] >= 25
    assert len(first["items"]) == 20
    assert 1 <= len(second["items"]) <= 20
    first_ids = {item["resource_id"] for item in first["items"]}
    second_ids = {item["resource_id"] for item in second["items"]}
    assert first_ids.isdisjoint(second_ids)


def test_system_health_reports_wechat_callback_failures(client):
    service = client.app.state.service
    now = utc_now()
    with service.db.tx():
        service.db.execute(
            """
            INSERT INTO wechat_callback_event
            (event_key,official_account_id,event,publish_id,payload_json,status,
             attempt_count,duplicate_count,result_json,last_error,received_at,updated_at)
            VALUES (?,?,?,?,?,'FAILED',1,2,'{}',?,?,?)
            """,
            (
                "callback_failed_test",
                "oa_default",
                "PUBLISHJOBFINISH",
                "publish_failed_test",
                json.dumps({"publish_id": "publish_failed_test"}),
                json.dumps({"message": "test failure"}),
                now,
                now,
            ),
        )

    payload = client.get("/api/system/health").json()
    codes = {alert["code"] for alert in payload["alerts"]}
    assert "WECHAT_CALLBACK_FAILED" in codes
    assert payload["components"]["publisher"]["status"] == "DEGRADED"
    assert payload["components"]["publisher"]["callback_failed"] == 1
    assert payload["components"]["publisher"]["callback_duplicates"] == 2
    metrics = client.get("/metrics").text
    assert 'wanren_wechat_callback_events_total{status="FAILED"} 1' in metrics
    assert "wanren_wechat_callback_duplicate_deliveries_total 2" in metrics
