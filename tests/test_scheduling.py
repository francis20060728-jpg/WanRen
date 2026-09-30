from datetime import datetime, timedelta, timezone

from wanren.database import Database


def create_approved_article(client, title="定时发布任务"):
    session = client.post("/api/sessions", json={"title": title}).json()["session"]
    generated = client.post(f"/api/sessions/{session['id']}/generate", json={}).json()
    article_id = generated["article_id"]
    approved = client.post(
        f"/api/articles/{article_id}/approve", json={"reviewer": "schedule-tester"}
    )
    assert approved.status_code == 200
    return article_id


def test_schedule_requires_human_approval(client):
    session = client.post("/api/sessions", json={"title": "未批准定时任务"}).json()["session"]
    article_id = client.post(f"/api/sessions/{session['id']}/generate", json={}).json()["article_id"]
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    response = client.post(
        f"/api/articles/{article_id}/schedule", json={"scheduled_at": future}
    )
    assert response.status_code == 409


def test_schedule_validates_future_datetime(client):
    article_id = create_approved_article(client, "过期时间任务")
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    response = client.post(
        f"/api/articles/{article_id}/schedule", json={"scheduled_at": past}
    )
    assert response.status_code == 400
    assert "future" in response.json()["detail"]


def test_schedule_is_idempotent_per_article(client):
    article_id = create_approved_article(client)
    first_time = datetime.now(timezone.utc) + timedelta(hours=2)
    second_time = first_time + timedelta(hours=1)
    first = client.post(
        f"/api/articles/{article_id}/schedule",
        json={"scheduled_at": first_time.isoformat()},
    )
    assert first.status_code == 200
    assert first.json()["status"] == "SCHEDULED"
    second = client.post(
        f"/api/articles/{article_id}/schedule",
        json={"scheduled_at": second_time.isoformat()},
    )
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["scheduled_at"] == first.json()["scheduled_at"]


def test_schedule_can_be_rescheduled(client):
    article_id = create_approved_article(client, "定时改期任务")
    first_time = datetime.now(timezone.utc) + timedelta(hours=2)
    scheduled = client.post(
        f"/api/articles/{article_id}/schedule",
        json={"scheduled_at": first_time.isoformat()},
    ).json()
    second_time = first_time + timedelta(hours=1)
    updated = client.put(
        f"/api/articles/{article_id}/schedule",
        json={"scheduled_at": second_time.isoformat()},
    )
    assert updated.status_code == 200
    assert updated.json()["id"] == scheduled["id"]
    assert updated.json()["scheduled_at"] == second_time.isoformat()


def test_reschedule_requires_active_job_and_future_time(client):
    article_id = create_approved_article(client, "改期边界任务")
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    response = client.put(
        f"/api/articles/{article_id}/schedule",
        json={"scheduled_at": future.isoformat()},
    )
    assert response.status_code == 404
    client.post(
        f"/api/articles/{article_id}/schedule",
        json={"scheduled_at": future.isoformat()},
    )
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    invalid = client.put(
        f"/api/articles/{article_id}/schedule",
        json={"scheduled_at": past.isoformat()},
    )
    assert invalid.status_code == 400


def test_due_schedule_is_claimed_and_published_once(client):
    article_id = create_approved_article(client, "到期定时任务")
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    scheduled = client.post(
        f"/api/articles/{article_id}/schedule",
        json={"scheduled_at": future.isoformat()},
    ).json()
    service = client.app.state.service
    with service.db.tx():
        service.db.execute(
            "UPDATE publish_job SET scheduled_at=? WHERE id=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), scheduled["id"]),
        )

    results = service.run_due_publish_jobs(limit=10)
    assert len(results) == 1
    assert results[0]["status"] == "SUCCESS"
    jobs = service.db.query_all("SELECT * FROM publish_job WHERE id = ?", (scheduled["id"],))
    assert len(jobs) == 1
    assert jobs[0]["status"] == "PUBLISHED"
    assert service.db.claim_due_publish_jobs() == []


def test_cancel_schedule_and_create_new_job(client):
    article_id = create_approved_article(client, "取消定时任务")
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    scheduled = client.post(
        f"/api/articles/{article_id}/schedule",
        json={"scheduled_at": future.isoformat()},
    ).json()
    cancelled = client.delete(f"/api/articles/{article_id}/schedule")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"
    assert cancelled.json()["id"] == scheduled["id"]

    missing = client.delete(f"/api/articles/{article_id}/schedule")
    assert missing.status_code == 404

    later = future + timedelta(hours=1)
    recreated = client.post(
        f"/api/articles/{article_id}/schedule",
        json={"scheduled_at": later.isoformat()},
    )
    assert recreated.status_code == 200
    assert recreated.json()["id"] != scheduled["id"]
    assert recreated.json()["status"] == "SCHEDULED"


def test_dashboard_and_health_report_scheduled_jobs(client):
    article_id = create_approved_article(client, "监控定时任务")
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    client.post(
        f"/api/articles/{article_id}/schedule", json={"scheduled_at": future.isoformat()}
    )
    dashboard = client.get("/api/dashboard").json()
    assert dashboard["scheduled"] >= 1
    health = client.get("/api/system/health").json()
    assert health["components"]["queue"]["scheduled"] >= 1


def test_existing_database_is_migrated_with_scheduled_at(tmp_path):
    path = tmp_path / "migration.sqlite3"
    original = Database(path)
    original.execute("DROP INDEX IF EXISTS idx_publish_schedule")
    original.execute("ALTER TABLE publish_job DROP COLUMN scheduled_at")
    original.execute("ALTER TABLE publish_job DROP COLUMN article_url")
    original.close()

    migrated = Database(path)
    columns = {row["name"] for row in migrated.query_all("PRAGMA table_info(publish_job)")}
    indexes = {row["name"] for row in migrated.query_all("PRAGMA index_list(publish_job)")}
    migrated.close()
    assert "scheduled_at" in columns
    assert "article_url" in columns
    assert "idx_publish_schedule" in indexes
