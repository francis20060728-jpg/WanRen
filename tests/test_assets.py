import io

from openpyxl import Workbook


def test_csv_and_xlsx_are_parsed(client):
    created = client.post(
        "/api/sessions",
        json={"title": "素材解析任务", "goal": "验证解析", "audience": "测试", "tone": "清晰"},
    )
    assert created.status_code == 200
    session_id = created.json()["session"]["id"]

    csv_response = client.post(
        f"/api/sessions/{session_id}/assets",
        files={"file": ("data.csv", b"model,range\nModel A,650km\n", "text/csv")},
    )
    assert csv_response.status_code == 200
    assert csv_response.json()["processing_status"] == "READY"

    workbook = Workbook()
    workbook.active.append(["参数", "数值"])
    workbook.active.append(["续航", "650km"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    xlsx_response = client.post(
        f"/api/sessions/{session_id}/assets",
        files={
            "file": (
                "data.xlsx",
                buffer.getvalue(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert xlsx_response.status_code == 200
    assert "650km" in xlsx_response.json()["extracted_text"]


def test_corrupt_image_is_rejected_but_tracked(client):
    created = client.post("/api/sessions", json={"title": "图片安全任务"})
    session_id = created.json()["session"]["id"]
    response = client.post(
        f"/api/sessions/{session_id}/assets",
        files={"file": ("bad.png", b"not an image", "image/png")},
    )
    assert response.status_code == 200
    assert response.json()["processing_status"] == "FAILED"


def test_invalid_mp4_signature_rejected(client):
    created = client.post("/api/sessions", json={"title": "视频安全任务"})
    session_id = created.json()["session"]["id"]
    response = client.post(
        f"/api/sessions/{session_id}/assets",
        files={"file": ("bad.mp4", b"0123456789ab", "video/mp4")},
    )
    assert response.status_code == 200
    assert response.json()["processing_status"] == "FAILED"


def test_upload_sanitizes_surrogates_from_broken_pdfs(tmp_path, monkeypatch):
    """损坏编码的 PDF 抽出孤立代理字符时：替换为 U+FFFD 入库，不再 UTF-8 编码崩溃。"""
    from wanren import assets as assets_mod
    from wanren.assets import ParsedAsset, save_asset
    from wanren.database import Database, utc_now

    monkeypatch.setattr(
        assets_mod,
        "parse_payload",
        lambda asset_type, payload: ParsedAsset(
            "PDF", "前文\ud800后文\udfff尾", {}, {}, "READY"
        ),
    )
    db = Database(tmp_path / "t.sqlite3")
    with db.tx():
        db.execute(
            "INSERT INTO workspace(id,name,status,created_at) VALUES ('ws_default','w','ACTIVE',?)",
            (utc_now(),),
        )
        db.execute(
            "INSERT INTO official_account(id,workspace_id,name,app_id,token_status,settings,created_at)"
            " VALUES ('oa_default','ws_default','测试号','mock-app-id','MOCK','{}',?)",
            (utc_now(),),
        )
        db.execute(
            "INSERT INTO content_session(id,workspace_id,official_account_id,title,status,created_at,updated_at)"
            " VALUES ('s1','ws_default','oa_default','代理字符','ACTIVE',?,?)",
            (utc_now(), utc_now()),
        )
    asset = save_asset(db, tmp_path / "store", "s1", "paper.pdf", "application/pdf", b"%PDF-1.4 stub", 10_000_000)
    row = db.query_one(
        "SELECT extracted_text FROM session_asset WHERE id = ?", (asset["id"],)
    )
    text = row["extracted_text"]
    text.encode("utf-8")  # 不再抛 UnicodeEncodeError
    assert "�" in text and "\ud800" not in text and "前文" in text
