def test_asset_analysis_is_scoped_to_session(client):
    first = client.post("/api/sessions", json={"title": "素材所属任务A"}).json()["session"]["id"]
    second = client.post("/api/sessions", json={"title": "素材所属任务B"}).json()["session"]["id"]
    upload = client.post(
        f"/api/sessions/{first}/assets",
        files={"file": ("a.txt", b"safe", "text/plain")},
    ).json()
    response = client.post(f"/api/sessions/{second}/assets/{upload['id']}/analyze", json={})
    assert response.status_code == 403
