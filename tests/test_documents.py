import io
import json
from pathlib import Path

from docx import Document
from pypdf import PdfWriter


def upload(client, session_id, name, payload, mime):
    return client.post(
        f"/api/sessions/{session_id}/assets",
        files={"file": (name, payload, mime)},
    )


def test_pdf_and_docx_are_extracted(client):
    created = client.post("/api/sessions", json={"title": "文档素材任务"})
    session_id = created.json()["session"]["id"]

    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    pdf_buffer = io.BytesIO()
    writer.write(pdf_buffer)
    pdf = upload(
        client,
        session_id,
        "brief.pdf",
        pdf_buffer.getvalue(),
        "application/pdf",
    )
    assert pdf.status_code == 200
    assert pdf.json()["processing_status"] == "READY"
    assert __import__("json").loads(pdf.json()["structured_data"])["page_count"] == 1

    document = Document()
    document.add_paragraph("车型续航 650km，充电 30 分钟。")
    docx_buffer = io.BytesIO()
    document.save(docx_buffer)
    docx = upload(
        client,
        session_id,
        "brief.docx",
        docx_buffer.getvalue(),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert docx.status_code == 200
    assert "650km" in docx.json()["extracted_text"]


def test_invalid_pdf_and_docx_are_tracked_as_failed(client):
    created = client.post("/api/sessions", json={"title": "文档安全任务"})
    session_id = created.json()["session"]["id"]
    pdf = upload(client, session_id, "bad.pdf", b"not pdf", "application/pdf")
    docx = upload(
        client,
        session_id,
        "bad.docx",
        b"not docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert pdf.json()["processing_status"] == "FAILED"
    assert docx.json()["processing_status"] == "FAILED"


def test_paper_pdf_extracts_embedded_figures(client):
    # 合成示例论文（tests/fixtures/sample-paper.pdf，内嵌 800x500 JPEG）：
    # 覆盖「上传 PDF → 抽取大图 → 派生 IMAGE 资产 → figure_index 入库」全链路
    paper = Path(__file__).parent / "fixtures" / "sample-paper.pdf"
    session = client.post("/api/sessions", json={"title": "论文解析"}).json()["session"]
    response = client.post(
        f"/api/sessions/{session['id']}/assets",
        files={"file": (paper.name, paper.read_bytes(), "application/pdf")},
    )
    assert response.status_code == 200
    source = response.json()
    assert source["processing_status"] == "READY"
    structured = json.loads(source["structured_data"])
    assert structured["figure_count"] >= 1
    assert structured["figures"][0]["width"] >= 400

    detail = client.get(f"/api/sessions/{session['id']}").json()
    metadata = json.loads(source["metadata_json"])
    figures = [
        asset for asset in detail["assets"]
        if json.loads(asset["metadata_json"]).get("derivative_type") == "PDF_FIGURE"
    ]
    assert len(figures) == structured["figure_count"]
    assert [asset["id"] for asset in figures] == metadata["figure_asset_ids"]
    assert all(asset["processing_status"] == "READY" for asset in figures)
    # 论文配图编号写入 structured_data：前端编辑器与发布渲染器都据此生成「论文图 N（第 M 页）」
    for index, asset in enumerate(figures, start=1):
        derived_structured = json.loads(asset["structured_data"])
        assert derived_structured.get("figure_index") == index
        assert derived_structured.get("page") >= 1
