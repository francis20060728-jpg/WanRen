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


def _vector_chart_pdf() -> bytes:
    """手工构造只含矢量图的 PDF：边框 + 密集折线（300 点）+ 网格，无内嵌位图。"""
    import math

    ops = ["q", "1.2 w", "0 0 0 RG"]
    ops.append("60 80 480 340 re S")  # 图框
    gx = 60
    while gx <= 540:
        ops.append(f"{gx} 80 m {gx} 420 l S")
        gx += 48
    gy = 80
    while gy <= 420:
        ops.append(f"60 {gy} m 540 {gy} l S")
        gy += 34
    for series, phase in ((0.0, 1.1), (1.6, 0.4)):
        points = []
        for i in range(300):
            x = 60 + 480 * i / 299
            y = 250 + 130 * math.sin(i / 299 * 6 * math.pi + phase) * (1 - series * 0.35)
            points.append((x, y))
        ops.append("1 w 0.1 0.3 0.7 RG" if series == 0 else "0.8 w 0.8 0.2 0.2 RG")
        ops.append(f"{points[0][0]:.1f} {points[0][1]:.1f} m")
        ops.extend(f"{px:.1f} {py:.1f} l" for px, py in points[1:])
        ops.append("S")
    ops.append("Q")
    # 图注：图块正下方的文字行，抽取器应抓为 caption_guess 供模型选图
    ops.append("BT /F1 10 Tf 60 64 Td (Fig. 1 Simulated temperature and moisture profiles) Tj ET")
    content = (" ".join(ops) + "\n").encode()

    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        4: f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"endstream",
        5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    pdf = io.BytesIO()
    pdf.write(b"%PDF-1.4\n")
    offsets = {}
    for num in sorted(objects):
        offsets[num] = pdf.tell()
        pdf.write(f"{num} 0 obj\n".encode() + objects[num] + b"\nendobj\n")
    xref = pdf.tell()
    pdf.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for num in sorted(objects):
        pdf.write(f"{offsets[num]:010d} 00000 n \n".encode())
    pdf.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return pdf.getvalue()


def test_vector_chart_pdf_extracts_rendered_figures(client):
    # 矢量图 PDF（曲线以路径绘制、无内嵌位图）：聚合成图块并 200dpi 渲染为 PNG 派生素材
    session = client.post("/api/sessions", json={"title": "矢量图论文解析"}).json()["session"]
    response = client.post(
        f"/api/sessions/{session['id']}/assets",
        files={"file": ("vector-paper.pdf", _vector_chart_pdf(), "application/pdf")},
    )
    assert response.status_code == 200
    source = response.json()
    assert source["processing_status"] == "READY"
    structured = json.loads(source["structured_data"])
    assert structured["figure_count"] >= 1
    assert structured["figures"][0]["width"] >= 400
    assert structured["figures"][0].get("source") == "vector"
    assert "temperature and moisture" in structured["figures"][0].get("caption_guess", "")

    detail = client.get(f"/api/sessions/{session['id']}").json()
    metadata = json.loads(source["metadata_json"])
    figures = [
        asset for asset in detail["assets"]
        if json.loads(asset["metadata_json"]).get("derivative_type") == "PDF_FIGURE"
    ]
    assert len(figures) == structured["figure_count"]
    assert [asset["id"] for asset in figures] == metadata["figure_asset_ids"]
    assert all(asset["processing_status"] == "READY" for asset in figures)
    for index, asset in enumerate(figures, start=1):
        derived_structured = json.loads(asset["structured_data"])
        assert derived_structured.get("figure_index") == index
        assert derived_structured.get("page") >= 1
        assert asset["mime_type"] == "image/png"


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
