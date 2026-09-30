import json

from tests.test_video_pipeline import make_video


def upload_video(client, name="transcode-source.mp4"):
    session = client.post(
        "/api/sessions", json={"title": "视频转码任务"}
    ).json()["session"]
    response = client.post(
        f"/api/sessions/{session['id']}/assets",
        files={"file": (name, make_video(width=320, height=180), "video/mp4")},
    )
    assert response.status_code == 200
    return response.json()


def test_video_transcode_queues_and_produces_derived_asset(client):
    video = upload_video(client)
    queued = client.post(
        f"/api/assets/{video['id']}/transcode", json={"preset": "preview_360p"}
    )
    assert queued.status_code == 200
    assert queued.json()["status"] == "QUEUED"

    service = client.app.state.service
    source = service.db.query_one(
        "SELECT * FROM session_asset WHERE id = ?", (video["id"],)
    )
    assert source["processing_status"] == "TRANSCODING"

    duplicate = client.post(
        f"/api/assets/{video['id']}/transcode", json={"preset": "preview_360p"}
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "ALREADY_QUEUED"

    results = service.run_pending_workflows("video-transcode-test", limit=1)
    assert len(results) == 1
    assert results[0]["status"] == "SUCCESS"
    rendition_id = results[0]["output"]["rendition_asset_id"]
    rendition = service.db.query_one(
        "SELECT * FROM session_asset WHERE id = ?", (rendition_id,)
    )
    assert rendition is not None
    assert rendition["asset_type"] == "VIDEO"
    assert rendition["processing_status"] == "READY"
    rendition_metadata = json.loads(rendition["metadata_json"])
    assert rendition_metadata["derivative_type"] == "VIDEO_RENDITION"
    assert rendition_metadata["source_asset_id"] == video["id"]
    assert rendition_metadata["preset"] == "preview_360p"
    assert rendition_metadata["width"] == 320
    assert rendition_metadata["height"] == 180

    source = service.db.query_one(
        "SELECT * FROM session_asset WHERE id = ?", (video["id"],)
    )
    source_metadata = json.loads(source["metadata_json"])
    assert source["processing_status"] == "READY"
    assert source_metadata["renditions"][0]["asset_id"] == rendition_id

    queue = service.db.query_one(
        "SELECT status FROM workflow_queue WHERE workflow_run_id = ?",
        (queued.json()["workflow_run_id"],),
    )
    assert queue["status"] == "DONE"
    nodes = service.db.query_all(
        "SELECT node_key,status FROM workflow_node_run WHERE workflow_run_id = ? ORDER BY started_at",
        (queued.json()["workflow_run_id"],),
    )
    assert [(node["node_key"], node["status"]) for node in nodes] == [
        ("TRANSCODE_VIDEO", "SUCCESS"),
        ("STORE_RENDITION", "SUCCESS"),
    ]


def test_transcode_rejects_non_video_and_failed_video(client):
    session = client.post("/api/sessions", json={"title": "转码边界任务"}).json()["session"]
    text = client.post(
        f"/api/sessions/{session['id']}/assets",
        files={"file": ("facts.txt", b"safe", "text/plain")},
    ).json()
    invalid = client.post(
        f"/api/sessions/{session['id']}/assets",
        files={"file": ("bad.mp4", b"0123456789ab", "video/mp4")},
    ).json()

    non_video = client.post(f"/api/assets/{text['id']}/transcode", json={})
    failed_video = client.post(f"/api/assets/{invalid['id']}/transcode", json={})
    assert non_video.status_code == 400
    assert failed_video.status_code == 409


def test_transcode_failure_restores_source_and_records_error(client, monkeypatch):
    video = upload_video(client, "损坏转码任务.mp4")
    response = client.post(
        f"/api/assets/{video['id']}/transcode", json={"preset": "preview_360p"}
    )
    assert response.status_code == 200
    service = client.app.state.service

    def fail_transcode(source, preset):
        raise ValueError("mock ffmpeg failure")

    monkeypatch.setattr("wanren.service.transcode_video_file", fail_transcode)
    results = service.run_pending_workflows("failed-transcode-test", limit=1)
    assert results[0]["status"] == "FAILED"
    source = service.db.query_one(
        "SELECT * FROM session_asset WHERE id = ?", (video["id"],)
    )
    metadata = json.loads(source["metadata_json"])
    assert source["processing_status"] == "READY"
    assert metadata["last_transcode_error"]["message"] == "mock ffmpeg failure"
