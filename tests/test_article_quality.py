from __future__ import annotations

from wanren.ai import LocalTextAdapter
from wanren.context import SessionContext
from wanren.editorial import build_evidence_digest, build_editorial_metadata
from wanren.publishing import render_article_html
from wanren.review import review_article
from wanren.security import sanitize_wechat_html


PAPER_TEXT = """
Contents lists available at SciLite Archive
Journal of Applied Indoor Perception
Original papers
A resource-efficient spatio-temporal framework for multi-camera robot tracking in warehouses
A R T I C L E  I N F O
A B S T R A C T
Multi-camera robot monitoring is difficult because dense warehouse aisles create frequent occlusions and identical robot appearances. Existing appearance-based Re-identification methods are computationally intensive and struggle with viewpoint variation. We propose a spatio-temporal framework using a lightweight detector, a single-view tracker, homography calibration, and a Global ID Pool. The detection module achieves a test-set mAP50 of 0.944 on consumer-grade hardware. The complete pipeline operates at approximately 13 FPS on an NVIDIA RTX 4060 Laptop. The cross-camera fusion reduces global raw identity switches by 87.6% and achieves a 99.33% cross-view identity consistency. The study uses 3917 frames and 8661 instances collected in a commercial warehouse. The work is limited to small fleets in indoor aisles and overlapping camera views. https://doi.org/10.1000/jaip.2026.001 This is an open access article under the CC BY license.
"""


def source_asset() -> dict[str, object]:
    return {
        "id": "asset_source000001",
        "asset_type": "PDF",
        "file_name": "paper.pdf",
        "processing_status": "READY",
        "extracted_text": PAPER_TEXT,
        "structured_data": "{}",
        "vision_result": "{}",
    }


def image_asset() -> dict[str, object]:
    return {
        "id": "asset_figure00001",
        "asset_type": "IMAGE",
        "file_name": "figure-1.png",
        "processing_status": "READY",
        "extracted_text": "",
        "structured_data": "{\"figure_index\": 1, \"page\": 3}",
        "vision_result": "{\"description\": \"System overview\"}",
    }


def paper_context() -> SessionContext:
    session = {
        "id": "session_quality0001",
        "title": "多摄像头机器人跟踪研究解读",
        "goal": "写一篇论文深读",
        "audience": "机器人与计算机视觉读者",
        "tone": "专业、清晰、克制",
        "requirements": "[\"保留论文来源\", \"解释关键指标\"]",
    }
    assets = [source_asset(), image_asset()]
    digest = build_evidence_digest(session, assets)
    return SessionContext(session=session, assets=assets, evidence_digest=digest)


def article_text(article) -> str:
    return "\n".join(
        [article.title, article.summary]
        + [paragraph for section in article.sections for paragraph in section.paragraphs]
    )


def test_paper_generation_is_editorial_and_passes_quality_gate():
    context = paper_context()
    result = LocalTextAdapter().generate(context, "写一篇论文深读")
    article = result.article

    assert article.editorial["style"] == "PAPER_INTERPRETATION"
    assert article.editorial["depth"] == "DEEP"
    assert article.title == "多摄像头目标跟踪的新解法"
    assert "10.1000/jaip.2026.001" in article_text(article)
    assert "mAP50" in article_text(article)
    assert all(not paragraph.startswith(("- ", "* ")) for section in article.sections for paragraph in section.paragraphs)
    assert "asset_" not in article_text(article)

    review = review_article(article.to_content_json(), context.facts)
    assert review.decision == "PASS", [issue.type for issue in review.issues]


def test_short_request_naturally_reduces_structure_without_fixed_template():
    context = paper_context()
    result = LocalTextAdapter().generate(context, "写一篇短读")
    assert result.article.editorial["depth"] == "BRIEF"
    assert 3 <= len(result.article.sections) <= 4
    assert any("10.1000/jaip.2026.001" in p for p in result.article.sections[-1].paragraphs)


def test_institutional_request_selects_announcement_style():
    context = paper_context()
    metadata = build_editorial_metadata(context, "学院成果宣传，突出录用和科研训练")
    assert metadata["style"] == "ORG_ANNOUNCEMENT"
    result = LocalTextAdapter().generate(context, "学院成果宣传，突出录用和科研训练")
    assert result.article.sections[0].heading == "成果速递"
    assert result.article.sections[-1].heading == "这项成果的意义"


def test_review_rejects_instruction_internal_id_and_source_leaks():
    context = paper_context()
    leaked_sentence = PAPER_TEXT.split("A B S T R A C T", 1)[1][:240]
    content = {
        "title": "模板终验",
        "summary": "system prompt asset_12345678",
        "sections": [
            {
                "id": "sec_01",
                "heading": "内部结果",
                "paragraphs": [leaked_sentence, "- fact one", "- fact two"],
                "asset_refs": [],
            }
        ],
    }
    result = review_article(content, [PAPER_TEXT])
    issue_types = {issue.type for issue in result.issues}
    assert result.decision == "REJECT"
    assert {"INSTRUCTION_LEAK", "INTERNAL_ID_LEAK", "MECHANICAL_TITLE", "SOURCE_TEXT_LEAK"} <= issue_types


def test_wechat_html_is_rich_safe_and_does_not_leak_asset_ids():
    context = paper_context()
    article = LocalTextAdapter().generate(context, "写一篇论文深读").article
    html = render_article_html(
        article.to_content_json(),
        context.assets,
        {str(context.assets[1]["id"]): "https://example.com/wechat-image.png"},
        {"style": {"accent": "#175641", "text": "#262626"}},
    )

    assert "WANREN READING" in html
    assert "论文图 1" in html
    assert "10.1000/jaip.2026.001" in html
    assert "asset_" not in html
    assert "figure-1.png" not in html
    assert "<script" not in sanitize_wechat_html(html + '<script>alert(1)</script>')
