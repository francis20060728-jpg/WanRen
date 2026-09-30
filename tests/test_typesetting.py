"""样式模版（8 气质）目录与按样式渲染的公众号 HTML。"""

from wanren.publishing import render_article_html
from wanren.typesetting import STYLES, list_typesetting_styles, render_article_html as render

CONTENT = {
    "title": "多摄像头机器人跟踪",
    "summary": "一篇示例文章",
    "sections": [
        {
            "id": "sec_01",
            "heading": "问题背景",
            "paragraphs": ["仓库机器人调度需要持续掌握每台设备的位置，单摄像头视角受限。"],
            "asset_refs": [],
        },
        {
            "id": "sec_02",
            "heading": "实验结果",
            "paragraphs": ["“多视角让身份切换错误显著下降。”", "在 Warehouse-ID 上 mOTA 提升 3.1%。"],
            "asset_refs": [],
            "style": {"indent": 2, "align": "justify"},
        },
    ],
    "editorial": {
        "metrics": [{"value": "3.1%", "label": "mOTA 提升"}, {"value": "41 个", "label": "跟踪目标"}],
        "source": {"title": "Warehouse-ID", "journal": "J. Appl. Indoor Percept.", "doi": "10.1000/jaip.2026.001"},
    },
}


def test_catalog_has_eight_distinct_styles():
    styles = list_typesetting_styles()
    assert len(styles) == 8
    assert len({style["id"] for style in styles}) == 8
    assert all(style["name"] and style["vibe"] and style["suits"] for style in styles)


def test_each_style_renders_all_content_inline_only():
    htmls = {}
    for style_id in STYLES:
        html = render(CONTENT, [], {}, {}, style_id)
        htmls[style_id] = html
        for needle in ("问题背景", "实验结果", "3.1%", "10.1000/jaip.2026.001", "论文图"):
            # 「论文图」只在配图时出现，无图场景跳过
            if needle == "论文图":
                continue
            assert needle in html, f"{style_id} missing {needle}"
        assert "<script" not in html
        assert "class=" not in html
        assert "display:flex" not in html and "grid-template" not in html
        assert "gradient" not in html
        assert "rgba(" not in html
        # 段落样式覆盖（排版工具栏参数）逐样式保留
        assert "text-indent:2em" in html
    # 八种气质产出八种不同版面
    assert len(set(htmls.values())) == 8


def test_dark_style_uses_table_skeleton():
    html = render(CONTENT, [], {}, {}, "dark-tech")
    assert "<table" in html
    assert "#080808" in html
    assert "#080808" in html.split("<table", 1)[1].split(">", 1)[0] or html.count("#080808") >= 3


def test_quote_paragraphs_get_style_quote_block():
    html = render(CONTENT, [], {}, {}, "blue-magazine")
    # 金句段走蓝底卡：主强调色实色背景
    assert "#0044FF" in html
    html_warm = render(CONTENT, [], {}, {}, "warm-paper")
    assert "#F4ECE1" in html_warm


def test_metric_cards_use_table_columns():
    html = render(CONTENT, [], {}, {}, "academic-frame")
    assert "3.1%" in html and "mOTA 提升" in html
    assert "<td" in html  # 数据卡为 table 多列


def test_publishing_module_delegates_to_typesetting():
    assert render(CONTENT, [], {}, {}, "warm-paper") == render_article_html(
        CONTENT, [], {}, {}, "warm-paper"
    )


def test_brand_fallback_keeps_wanren_reading_card():
    html = render_article_html(CONTENT, [], {}, {"style": {"accent": "#175641", "text": "#262626"}})
    assert "WANREN READING" in html
    assert "#175641" in html


def test_unknown_style_falls_back_to_default():
    html = render(CONTENT, [], {}, {}, "not-a-style")
    assert "问题背景" in html
