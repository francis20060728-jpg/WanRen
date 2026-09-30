import json


def test_vid_lens_result_is_structured_and_never_rendered(vidlens_client, monkeypatch):
    from tests.conftest import png_payload

    created = vidlens_client.post("/api/sessions", json={"title": "视觉理解任务"})
    session_id = created.json()["session"]["id"]
    uploaded = vidlens_client.post(
        f"/api/sessions/{session_id}/assets",
        files={"file": ("photo.png", png_payload(), "image/png")},
    ).json()

    class Completed:
        returncode = 0
        stdout = 'prefix\n{"description":"绿色测试图","objects":["rectangle"],"quality_score":96}\n'
        stderr = ""

    def fake_run(*args, **kwargs):
        assert "vidlens" in args[0][1].lower()
        return Completed()

    monkeypatch.setattr("wanren.service.subprocess.run", fake_run)
    response = vidlens_client.post(
        f"/api/sessions/{session_id}/assets/{uploaded['id']}/analyze", json={"prompt": None}
    )
    assert response.status_code == 200
    vision = json.loads(response.json()["vision_result"])
    assert vision["description"] == "绿色测试图"
    assert response.json()["processing_status"] == "VISION_READY"
