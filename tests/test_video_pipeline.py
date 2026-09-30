import io
import json
import subprocess
import tempfile
from pathlib import Path

import imageio_ffmpeg
from PIL import Image


def make_video(width=320, height=240, seconds=2, fps=10):
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    output = Path(tempfile.gettempdir()) / f"wanren-video-{width}x{height}-{fps}.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size={width}x{height}:rate={fps}",
            "-t",
            str(seconds),
            "-pix_fmt",
            "yuv420p",
            str(output),
        ],
        check=True,
    )
    try:
        return output.read_bytes()
    finally:
        output.unlink(missing_ok=True)


def test_video_upload_extracts_metadata_and_cover_frame(client):
    session = client.post("/api/sessions", json={"title": "视频处理任务"}).json()["session"]
    response = client.post(
        f"/api/sessions/{session['id']}/assets",
        files={"file": ("product-demo.mp4", make_video(), "video/mp4")},
    )
    assert response.status_code == 200
    video = response.json()
    assert video["processing_status"] == "READY"
    metadata = json.loads(video["metadata_json"])
    assert metadata["width"] == 320
    assert metadata["height"] == 240
    assert metadata["duration_seconds"] == 2.0
    assert metadata["fps"] == 10.0
    assert metadata["codec"] == "h264"
    assert metadata["thumbnail_status"] == "READY"

    thumbnail_id = metadata["thumbnail_asset_id"]
    thumbnail = client.app.state.service.db.query_one(
        "SELECT * FROM session_asset WHERE id = ?", (thumbnail_id,)
    )
    assert thumbnail is not None
    assert thumbnail["asset_type"] == "IMAGE"
    assert thumbnail["processing_status"] == "READY"
    derived_metadata = json.loads(thumbnail["metadata_json"])
    assert derived_metadata["derivative_type"] == "VIDEO_COVER_FRAME"
    assert derived_metadata["source_asset_id"] == video["id"]

    image_response = client.get(f"/api/assets/{thumbnail_id}/file")
    assert image_response.status_code == 200
    with Image.open(io.BytesIO(image_response.content)) as image:
        assert image.format == "JPEG"
        assert image.size == (320, 240)


def test_video_context_contains_processing_metadata(client):
    session = client.post("/api/sessions", json={"title": "视频上下文任务"}).json()["session"]
    client.post(
        f"/api/sessions/{session['id']}/assets",
        files={"file": ("context-demo.mp4", make_video(width=160, height=120, seconds=1), "video/mp4")},
    )
    generated = client.post(f"/api/sessions/{session['id']}/generate", json={})
    assert generated.status_code == 200
    article_id = generated.json()["article_id"]
    detail = client.get(f"/api/articles/{article_id}").json()
    content = json.loads(detail["article"]["content_json"])
    text = "\n".join(
        paragraph
        for section in content["sections"]
        for paragraph in section["paragraphs"]
    )
    assert "160x120" in text
    assert "duration=1.0s" in text
    assert "codec=h264" in text


def test_video_vision_uses_derived_cover(monkeypatch, vidlens_client):
    session = vidlens_client.post("/api/sessions", json={"title": "视频视觉任务"}).json()["session"]
    video = vidlens_client.post(
        f"/api/sessions/{session['id']}/assets",
        files={"file": ("vision-demo.mp4", make_video(width=160, height=120), "video/mp4")},
    ).json()

    class Completed:
        returncode = 0
        stdout = '{"description":"测试视频首帧","quality_score":95}'
        stderr = ""

    def fake_run(*args, **kwargs):
        command = args[0]
        assert command[1].endswith("vidlens.py")
        assert str(command[2]).endswith(".jpg")
        return Completed()

    monkeypatch.setattr("wanren.service.subprocess.run", fake_run)
    result = vidlens_client.post(
        f"/api/sessions/{session['id']}/assets/{video['id']}/analyze", json={}
    )
    assert result.status_code == 200
    metadata = json.loads(result.json()["metadata_json"])
    assert metadata["vision_analysis_source"] == "derived_video_cover"
    vision = json.loads(result.json()["vision_result"])
    assert vision["description"] == "测试视频首帧"
