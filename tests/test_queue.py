from wanren.config import load_settings
from wanren.database import Database
from wanren.service import WanRenService


def test_generation_queue_survives_service_restart(client, tmp_path):
    created = client.post("/api/sessions", json={"title": "持久队列任务"})
    session_id = created.json()["session"]["id"]
    original_service = client.app.state.service

    queued = original_service.enqueue_generate_article(session_id, "验证持久化队列")
    assert queued["status"] == "QUEUED"
    assert original_service.db.query_one(
        "SELECT COUNT(*) AS value FROM article WHERE session_id = ?", (session_id,)
    )["value"] == 0

    restarted_db = Database(original_service.db.path)
    restarted_service = WanRenService(
        restarted_db,
        load_settings(
            {
                "WANREN_DB_PATH": str(original_service.db.path),
                "WANREN_STORAGE_PATH": str(tmp_path / "storage"),
                "WANREN_PUBLISHER": "mock",
                "WANREN_WORKFLOW_MODE": "background",
            }
        ),
    )
    try:
        results = restarted_service.run_pending_workflows("worker-restart")
        assert len(results) == 1
        assert results[0]["status"] == "SUCCESS"
        assert original_service.db.query_one(
            "SELECT COUNT(*) AS value FROM article WHERE session_id = ?", (session_id,)
        )["value"] == 1
        queue = original_service.db.query_one(
            "SELECT status FROM workflow_queue WHERE workflow_run_id = ?",
            (queued["workflow_run_id"],),
        )
        assert queue["status"] == "DONE"
    finally:
        restarted_db.close()


def test_queued_submission_shape(client):
    created = client.post("/api/sessions", json={"title": "队列响应任务"})
    session_id = created.json()["session"]["id"]
    queued = client.app.state.service.enqueue_generate_article(session_id, "queued response")
    assert queued["workflow_run_id"].startswith("run_")
    assert queued["status"] == "QUEUED"
