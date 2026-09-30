"""样式模版（排版气质）目录与公众号 HTML 渲染。

灵感与参数来自开源项目 wechat-typesetting-cy 的八个已验证气质组合
（Apache-2.0），移植为万仞的「样式模版」：只决定排版（配色 / 字体 /
章节头 / 金句卡 / 数据卡 / 文末），内容架构一律由大模型现场提案、
用户确认。

微信兼容底线在渲染器里逐条落实：全部内联样式、无 flex/grid/渐变/
rgba 背景、深底页面用 table 骨架、多列用 table、列表 p+span。
"""
from __future__ import annotations

import html as html_module
import json
import re
from typing import Any

from .security import escape_text


# ---------------------------------------------------------------- 目录 ----

SANS = "-apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif"
SERIF = "Georgia, 'Times New Roman', 'Songti SC', serif"
MAGAZINE_SERIF = "'Times New Roman', 'Songti SC', serif"

STYLES: dict[str, dict[str, Any]] = {
    "blue-magazine": {
        "id": "blue-magazine",
        "name": "蓝调杂志",
        "vibe": "理性、克制、纸质杂志的编辑感",
        "suits": "深度观点、评论、随笔、行业观察",
        "page_bg": "#fefefe",
        "skeleton": "section",
        "font": MAGAZINE_SERIF,
        "text": "#1A1A1A",
        "title": "#000000",
        "accent": "#0044FF",
        "accent_soft": "#E5EBFF",
        "muted": "#888888",
        "border": "#DDDDDD",
        "sizes": {"cover": 32, "section": 22, "body": 15, "small": 13},
        "line_height": 1.75,
        "radius": 0,
        "header": "magazine",
        "section_heading": "watermark",
        "quote": "accent_card",
        "end": "line",
    },
    "dark-tech": {
        "id": "dark-tech",
        "name": "暗夜科技",
        "vibe": "发布会现场感、信息密度高、数据驱动",
        "suits": "产品发布、科技报道、竞品对比、时间线叙事",
        "page_bg": "#080808",
        "skeleton": "table",
        "font": SANS,
        "text": "#AAAAA7",
        "title": "#F0EFEB",
        "accent": "#FF4500",
        "accent2": "#FFB300",
        "accent_soft": "#1a1a1a",
        "muted": "#8A8A87",
        "border": "#1a1a1a",
        "band": "#0d0d0d",
        "card_bg": "#0f0f0f",
        "sizes": {"cover": 36, "section": 24, "body": 14, "small": 11},
        "line_height": 1.7,
        "radius": 12,
        "header": "dark_banner",
        "section_heading": "spaced_label",
        "quote": "dark_card",
        "end": "ladder",
    },
    "mono-minimal": {
        "id": "mono-minimal",
        "name": "极简黑白",
        "vibe": "安静、文学、把舞台完全让给文字",
        "suits": "情感文、演讲稿、严肃长文；拿不准时的安全默认",
        "page_bg": "#fefefe",
        "skeleton": "section",
        "font": SANS,
        "text": "#262626",
        "title": "#111111",
        "accent": "#262626",
        "accent_soft": "#F5F5F5",
        "muted": "#999999",
        "border": "#E0E0E0",
        "sizes": {"cover": 26, "section": 18, "body": 15, "small": 13},
        "line_height": 1.9,
        "radius": 0,
        "header": "minimal",
        "section_heading": "thin_line",
        "quote": "plain_center",
        "end": "dots",
    },
    "warm-paper": {
        "id": "warm-paper",
        "name": "暖纸阅读",
        "vibe": "温暖、手工感、像一本生活杂志",
        "suits": "美食、旅行、手作、生活方式、人物故事",
        "page_bg": "#FAF6F0",
        "skeleton": "table",
        "font": SERIF,
        "text": "#3D3429",
        "title": "#2B241B",
        "accent": "#B45309",
        "accent_soft": "#F4ECE1",
        "muted": "#A1907C",
        "border": "#E5D9C9",
        "sizes": {"cover": 28, "section": 20, "body": 15, "small": 13},
        "line_height": 1.85,
        "radius": 8,
        "header": "center",
        "section_heading": "dashed",
        "quote": "warm_card",
        "end": "warm_note",
    },
    "fresh-healing": {
        "id": "fresh-healing",
        "name": "清新治愈",
        "vibe": "轻盈、干净、让人放松",
        "suits": "健康、心理、育儿、习惯养成、轻知识",
        "page_bg": "#fefefe",
        "skeleton": "section",
        "font": SANS,
        "text": "#37474F",
        "title": "#263238",
        "accent": "#00897B",
        "accent_soft": "#E6F4F1",
        "muted": "#90A4AE",
        "border": "#C3E5DF",
        "sizes": {"cover": 26, "section": 18, "body": 15, "small": 13},
        "line_height": 1.85,
        "radius": 12,
        "header": "center",
        "section_heading": "dot",
        "quote": "soft_card",
        "end": "tip",
    },
    "biz-navy": {
        "id": "biz-navy",
        "name": "商务蓝灰",
        "vibe": "专业、可信、报告感但不刻板",
        "suits": "财经解读、行业分析、职场方法、政策解读",
        "page_bg": "#fefefe",
        "skeleton": "section",
        "font": SANS,
        "text": "#333333",
        "title": "#13243D",
        "accent": "#1B3A6B",
        "accent_soft": "#F2F5F9",
        "muted": "#8A94A6",
        "border": "#D8E0EB",
        "sizes": {"cover": 26, "section": 18, "body": 15, "small": 13},
        "line_height": 1.8,
        "radius": 4,
        "header": "magazine",
        "section_heading": "navy_bar",
        "quote": "outline",
        "end": "references",
    },
    "vivid-orange": {
        "id": "vivid-orange",
        "name": "活力橙黄",
        "vibe": "明快、有热情、年轻",
        "suits": "盘点、清单体、活动宣传、好物推荐",
        "page_bg": "#fefefe",
        "skeleton": "section",
        "font": SANS,
        "text": "#2D2D2D",
        "title": "#1A1A1A",
        "accent": "#FF6F00",
        "accent_soft": "#FFF7E6",
        "highlight": "#FFE9A8",
        "muted": "#9E9E9E",
        "border": "#FFD8A8",
        "sizes": {"cover": 28, "section": 19, "body": 15, "small": 13},
        "line_height": 1.75,
        "radius": 10,
        "header": "center",
        "section_heading": "big_number",
        "quote": "highlight",
        "end": "cta",
    },
    "academic-frame": {
        "id": "academic-frame",
        "name": "学术线框",
        "vibe": "严谨、结构化、工具书感",
        "suits": "教程、方法论、技术科普、论文解读、含代码",
        "page_bg": "#fefefe",
        "skeleton": "section",
        "font": SANS,
        "text": "#2B2B2B",
        "title": "#10301F",
        "accent": "#14532D",
        "accent_soft": "#EFF5F0",
        "muted": "#8C8C8C",
        "border": "#CFE0D4",
        "code_bg": "#1F2329",
        "code_text": "#E6E6E6",
        "sizes": {"cover": 26, "section": 18, "body": 15, "small": 13},
        "line_height": 1.8,
        "radius": 0,
        "header": "frame",
        "section_heading": "step",
        "quote": "definition",
        "end": "references",
    },
}

DEFAULT_STYLE_ID = "mono-minimal"


def list_typesetting_styles() -> list[dict[str, Any]]:
    """前端目录：每个样式一张卡片（含气质与适用内容，不暴露渲染细节）。"""
    return [
        {
            "id": style["id"],
            "name": style["name"],
            "vibe": style["vibe"],
            "suits": style["suits"],
            "page_bg": style["page_bg"],
            "accent": style["accent"],
        }
        for style in STYLES.values()
    ]


def get_style(style_id: str | None) -> dict[str, Any]:
    return STYLES.get(style_id or "", STYLES[DEFAULT_STYLE_ID])


def account_style_fallback(account_settings: dict[str, Any]) -> dict[str, Any]:
    """未指定样式模版时，用公众号品牌色生成一个克制的默认样式。"""
    brand = (account_settings or {}).get("style") or {}
    accent = str(brand.get("accent") or "#175641")
    text = str(brand.get("text") or "#262626")
    base = dict(STYLES[DEFAULT_STYLE_ID])
    font_map = {"serif": SERIF, "sans": SANS, "system": SANS}
    base.update(
        {
            "font": font_map.get(str(brand.get("font") or "system"), SANS),
            "accent": accent,
            "text": text,
            "title": text,
            "accent_soft": "#F3F5F4",
            "border": "#DDE1DF",
            "header": "brand",
            "section_heading": "navy_bar",
            "quote": "outline",
            "end": "line",
            "id": "",
            "name": "品牌默认",
        }
    )
    return base


def style_for_article(typesetting_id: str | None, account_settings: dict[str, Any]) -> dict[str, Any]:
    if typesetting_id and typesetting_id in STYLES:
        return get_style(typesetting_id)
    return account_style_fallback(account_settings)


# ------------------------------------------------- 自定义样式（导入） ----

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_HEADER_KINDS = {"magazine", "dark_banner", "center", "frame", "minimal", "brand"}
_HEADING_KINDS = {
    "watermark", "spaced_label", "thin_line", "dashed", "dot",
    "navy_bar", "big_number", "step",
}
_QUOTE_KINDS = {
    "accent_card", "dark_card", "plain_center", "warm_card",
    "soft_card", "highlight", "definition", "outline",
}
_END_KINDS = {"line", "ladder", "warm_note", "tip", "cta", "references", "dots"}


_FONT_ENUMS: dict[str, str] = {
    "songti": "'Songti SC', 'SimSun', 'Songti SC', serif",
    "fangsong": "'Fangsong SC', 'STFangsong', 'FangSong', serif",
    "heiti": "'PingFang SC', 'Microsoft YaHei', 'Heiti SC', sans-serif",
    "kai": "'Kaiti SC', 'STKaiti', 'KaiTi', serif",
    "sans": SANS,
    "serif": SERIF,
}


def _mix(hex_color: str, other: str, ratio: float) -> str:
    """hex 混色（ratio 为 other 占比），用于从种子色派生浅底/边框。"""
    a = [int(hex_color[i : i + 2], 16) for i in (1, 3, 5)]
    b = [int(other[i : i + 2], 16) for i in (1, 3, 5)]
    mixed = [round(x * (1 - ratio) + y * ratio) for x, y in zip(a, b)]
    return "#" + "".join(f"{v:02X}" for v in mixed)


def _clean_hex(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    return text if _HEX.fullmatch(text) else fallback


def _clean_font(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    # 只保留常见字体栈字符，杜绝注入 style 属性
    if not text or re.search(r"[<>{};]|expression|url\(", text, re.IGNORECASE):
        return fallback
    return _FONT_ENUMS.get(text.lower(), text)[:160]


def validate_style_tokens(payload: dict[str, Any]) -> dict[str, Any]:
    """校验并归一化导入的样式令牌：缺失项回退极简黑白默认，非法值报错。"""
    if not isinstance(payload, dict):
        raise ValueError("样式必须是 JSON 对象")
    raw_accent = str(payload.get("accent") or "").strip()
    if not _HEX.fullmatch(raw_accent):
        raise ValueError("accent 必须是 #RRGGBB 颜色（如 #0044FF）")
    base = dict(STYLES[DEFAULT_STYLE_ID])
    sizes = payload.get("sizes") if isinstance(payload.get("sizes"), dict) else {}

    def _num(key: str, low: float, high: float, default: float) -> float:
        try:
            return min(max(float(payload[key]), low), high)
        except (KeyError, TypeError, ValueError):
            return default

    def _size(key: str, default: int) -> int:
        try:
            return int(min(max(int(sizes[key]), 10), 48))
        except (KeyError, TypeError, ValueError):
            return default

    def _kind(key: str, allowed: set[str], default: str) -> str:
        value = str(payload.get(key) or "")
        return value if value in allowed else default

    page_bg = _clean_hex(payload.get("page_bg"), "#fefefe")
    # 种子色派生（MarkFlow 思路）：未显式给出时，浅底/边框/辅助色由种子色自动派生
    explicit_soft = str(payload.get("accent_soft") or "").strip()
    explicit_border = str(payload.get("border") or "").strip()
    explicit_muted = str(payload.get("muted") or "").strip()
    explicit_text = _clean_hex(payload.get("text"), "#333333")
    explicit_title = _clean_hex(payload.get("title"), _mix(explicit_text, "#000000", 0.3))
    derived_soft = _mix(raw_accent, "#FFFFFF", 0.86) if page_bg.lower() != "#fefefe" else _mix(raw_accent, "#FFFFFF", 0.88)
    derived_border = _mix(raw_accent, "#FFFFFF", 0.76)
    derived_muted = _mix(explicit_text, "#FFFFFF", 0.42)
    style = {
        "page_bg": page_bg,
        "skeleton": "table" if str(payload.get("skeleton")) == "table" or page_bg.lower() != "#fefefe" else "section",
        "font": _clean_font(payload.get("font"), base["font"]),
        "text": _clean_hex(payload.get("text"), explicit_text),
        "title": _clean_hex(payload.get("title"), explicit_title),
        "accent": _clean_hex(payload.get("accent"), base["accent"]),
        "accent_soft": _clean_hex(explicit_soft, derived_soft),
        "muted": _clean_hex(explicit_muted, derived_muted),
        "border": _clean_hex(explicit_border, derived_border),
        "card_bg": _clean_hex(payload.get("card_bg"), "#1F1F1F"),
        "highlight": _clean_hex(payload.get("highlight"), "#FFE9A8"),
        "sizes": {
            "cover": _size("cover", base["sizes"]["cover"]),
            "section": _size("section", base["sizes"]["section"]),
            "body": _size("body", base["sizes"]["body"]),
            "small": _size("small", base["sizes"]["small"]),
        },
        "line_height": round(_num("line_height", 1.1, 2.6, base["line_height"]), 2),
        "radius": int(_num("radius", 0, 24, base["radius"])),
        "header": _kind("header", _HEADER_KINDS, "minimal"),
        "section_heading": _kind("section_heading", _HEADING_KINDS, "thin_line"),
        "quote": _kind("quote", _QUOTE_KINDS, "outline"),
        "end": _kind("end", _END_KINDS, "line"),
    }
    return style


DEMO_SAMPLES: dict[str, dict[str, Any]] = {
    "opinion": {
        "name": "观点长文",
        "content": {
            "title": "排版样例：一篇文章穿上这套样式之后",
            "summary": "同一段内容，不同气质——看标题、章节、金句、数据与收尾如何随样式变化。",
            "sections": [
                {
                    "id": "demo_open",
                    "heading": "开篇这样开始",
                    "paragraphs": [
                        "这是一段用来展示版式的示例正文。它没有任何信息量，只有节奏：句子有长有短，"
                        "段落之间保持呼吸感，阅读时的视线应当是顺滑的。",
                        "“好排版自己会说话，读者只觉得舒服，说不出为什么。”",
                    ],
                    "asset_refs": [],
                },
                {
                    "id": "demo_body",
                    "heading": "章节标题长这个样子",
                    "paragraphs": [
                        "章节编号、标题装饰与正文的字号层级，是这套样式最容易被记住的部分。"
                        "数据会排成两列卡片，指标醒目、标签克制。",
                    ],
                    "asset_refs": [],
                },
            ],
            "editorial": {
                "metrics": [
                    {"value": "98.6%", "label": "示例指标 A"},
                    {"value": "3.2×", "label": "示例指标 B"},
                ],
                "source": {"title": "示例来源论文", "journal": "Journal of Demo", "doi": "10.0000/demo.0001"},
            },
        },
    },
    "data": {
        "name": "数据速览",
        "content": {
            "title": "数据速览：把指标放进版面",
            "summary": "数据密集型内容的样例——多指标卡片、来源信息与克制正文。",
            "sections": [
                {
                    "id": "data_open",
                    "heading": "先给结论",
                    "paragraphs": [
                        "数据型文章的版面要点是层级：结论先行，指标成卡，正文只做解释。"
                        "读者扫一眼卡片就能拿到全部数字，读正文才知道数字意味着什么。",
                    ],
                    "asset_refs": [],
                },
                {
                    "id": "data_read",
                    "heading": "怎么读这些数字",
                    "paragraphs": [
                        "每个指标卡片只放一个数字和一个标签，颜色强调只给最重要的那一项。"
                        "“数据不吓人，吓人的是没有解释的数据。”",
                        "来源信息放在文末信息卡里，标题、期刊、DOI 一样不少，方便核查。",
                    ],
                    "asset_refs": [],
                },
            ],
            "editorial": {
                "metrics": [
                    {"value": "41 个", "label": "跟踪目标"},
                    {"value": "3.1%", "label": "mOTA 提升"},
                    {"value": "98.5%", "label": "切换错误降幅"},
                    {"value": "24/7", "label": "全天候运行"},
                ],
                "source": {"title": "Warehouse-ID（示例）", "journal": "J. Appl. Indoor Percept.", "doi": "10.1000/demo.2026.001"},
            },
        },
    },
    "tutorial": {
        "name": "步骤教程",
        "content": {
            "title": "三步上手：步骤型内容的排版",
            "summary": "教程与方法的样例——STEP 式章节、短段落与收束清单。",
            "sections": [
                {
                    "id": "step_1",
                    "heading": "第一步：准备素材",
                    "paragraphs": [
                        "教程型内容每节只讲一件事，段落短，动词开头。读者是边做边读的，"
                        "版面要让他们随时找得到自己在第几步。",
                    ],
                    "asset_refs": [],
                },
                {
                    "id": "step_2",
                    "heading": "第二步：按节填写",
                    "paragraphs": [
                        "把每一步的关键动作写成一两句，提醒和坑位用强调块拎出来。",
                        "“先跑通，再优化；先完成，再完美。”",
                    ],
                    "asset_refs": [],
                },
                {
                    "id": "step_3",
                    "heading": "第三步：检查并发布",
                    "paragraphs": [
                        "发布前对照清单逐项检查：标题、摘要、封面、来源。都齐了再点发布。",
                    ],
                    "asset_refs": [],
                },
            ],
            "editorial": {"metrics": [], "source": {}},
        },
    },
    "listicle": {
        "name": "清单盘点",
        "content": {
            "title": "盘点：清单体内容长什么样",
            "summary": "清单与盘点的样例——大编号、短句金句与轻快收尾。",
            "sections": [
                {
                    "id": "pick_1",
                    "heading": "第一件事",
                    "paragraphs": ["清单体的节奏靠编号推进，每节两三段就够，重点是标题敢短。"],
                    "asset_refs": [],
                },
                {
                    "id": "pick_2",
                    "heading": "第二件事",
                    "paragraphs": [
                        "金句和亮点用强调块抬出来，读者滑动时一眼能抓住重点。",
                        "“清单的尽头不是罗列，是取舍。”",
                    ],
                    "asset_refs": [],
                },
                {
                    "id": "pick_3",
                    "heading": "第三件事",
                    "paragraphs": ["收尾用行动号召：点在看、留言、下期再见。轻内容配轻收尾。"],
                    "asset_refs": [],
                },
            ],
            "editorial": {"metrics": [], "source": {}},
        },
    },
}

DEMO_DEFAULT_SAMPLE = "opinion"
DEMO_CONTENT: dict[str, Any] = DEMO_SAMPLES[DEMO_DEFAULT_SAMPLE]["content"]


def _steps_block(items: list[tuple[str, str]], style: dict[str, Any]) -> list[str]:
    """竖向步骤卡（MarkFlow Steps_DA02 的微信安全版：section + 圆形序号徽章）。"""
    parts = ['<section style="margin:24px 24px;">']
    for index, (name, desc) in enumerate(items, start=1):
        parts.append(
            f'<section style="margin-bottom:10px;padding:12px 14px;background-color:{style["accent_soft"]};'
            f'border:1px solid {style["border"]};border-radius:{style["radius"]}px;">'
            f'<p style="margin:0 0 4px 0;">'
            f'<span style="display:inline-block;width:24px;height:24px;line-height:24px;border-radius:50%;'
            f'background-color:{style["accent"]};color:#fefefe;font-size:12px;font-weight:700;'
            f'text-align:center;margin-right:8px;">{index}</span>'
            f'<span style="font-size:{style["sizes"]["body"]}px;font-weight:700;color:{style["title"]};">{escape_text(name)}</span></p>'
            f'<p style="margin:0 0 0 32px;font-size:{style["sizes"]["small"]}px;line-height:1.6;color:{style["muted"]};">{escape_text(desc)}</p>'
            "</section>"
        )
    parts.append("</section>")
    return parts


def _timeline_block(items: list[tuple[str, str, str]], style: dict[str, Any]) -> list[str]:
    """时间线（MarkFlow Timeline_DA01 的微信安全版：圆点 + 左竖线 + 日期/标题/描述）。"""
    parts = ['<section style="margin:24px 24px;padding:4px 0;">']
    for index, (date, title, desc) in enumerate(items):
        is_last = index == len(items) - 1
        transparent = "border-color:transparent;" if is_last else ""
        parts.append(
            f'<section style="padding:0 0 14px 8px;border-left:2px solid {style["border"]};{transparent}">'
            f'<p style="margin:0 0 2px 0;">'
            f'<span style="display:inline-block;width:9px;height:9px;border-radius:50%;background-color:{style["accent"]};'
            f'margin-right:8px;"></span>'
            f'<span style="font-size:{style["sizes"]["small"]}px;font-weight:700;color:{style["accent"]};letter-spacing:.5px;">{escape_text(date)}</span></p>'
            f'<p style="margin:0 0 2px 14px;font-size:{style["sizes"]["body"]}px;font-weight:700;color:{style["title"]};">{escape_text(title)}</p>'
            f'<p style="margin:0 0 0 14px;font-size:{style["sizes"]["small"]}px;line-height:1.6;color:{style["muted"]};">{escape_text(desc)}</p>'
            "</section>"
        )
    parts.append("</section>")
    return parts


def _compare_table(rows: list[tuple[str, str, str, bool]], style: dict[str, Any]) -> list[str]:
    """对比表（MarkFlow compare 的微信安全版：三列表格，推荐行整行高亮）。"""
    small = style["sizes"]["small"]
    header = "".join(
        f'<td style="padding:9px 10px;border:none;background-color:{style["accent"]};font-size:{small}px;'
        f'font-weight:700;color:#fefefe;">{escape_text(label)}</td>'
        for label in ("对比维度", "方案 A", "方案 B")
    )
    parts = [
        f'<table width="100%" cellspacing="0" cellpadding="0" border="0" '
        f'style="border-collapse:collapse;border-spacing:0;margin:24px 24px;width:calc(100% - 48px);border:none;">',
        f'<tr>{header}</tr>',
    ]
    light_page = str(style.get("page_bg", "#fefefe")).lower() == "#fefefe"
    for dim, a, b, highlight in rows:
        bg = style["accent_soft"] if highlight else ("#FFFFFF" if light_page else str(style.get("card_bg", "#1F1F1F")))
        weight = "font-weight:700;" if highlight else ""
        cells = "".join(
            f'<td style="padding:9px 10px;border:none;background-color:{bg};font-size:{small}px;'
            f'line-height:1.6;color:{style["text"]};{weight}">{escape_text(value)}</td>'
            for value in (dim, a, b)
        )
        parts.append(f"<tr>{cells}</tr>")
    parts.append("</table>")
    return parts


_DEMO_STEPS: list[tuple[str, str]] = [
    ("准备素材", "上传论文 PDF 与论文图，系统自动抽取文字与图表。"),
    ("确认架构", "AI 先给大纲与样式提案，你确认或改完再动笔。"),
    ("逐块成文", "按节生成，改动实时高亮，随时回退。"),
    ("同步草稿", "一键推送到公众号草稿箱，改版自动更新同一条草稿。"),
]
_DEMO_TIMELINE: list[tuple[str, str, str]] = [
    ("2026 · 03", "立项", "从一份素材管理清单开始，确定对话式生产的主线。"),
    ("2026 · 07", "版本链与回退", "逐轮改动可回退，历史全保留。"),
    ("2026 · 09", "架构提案 + 样式模版", "先提案后动笔，八种排版气质一键切换。"),
]
_DEMO_COMPARE: list[tuple[str, str, str, bool]] = [
    ("上线周期", "自建排版规则数周", "提案确认后当天成文", True),
    ("改稿方式", "整篇重写", "逐块修改 + 高亮回放", True),
    ("排版一致性", "依赖手工", "样式模版统一渲染", False),
]


def _demo_extra_blocks(sample_key: str, style: dict[str, Any]) -> list[str]:
    """按样例形态附加组件演示：教程→步骤条，观点→时间线，数据→对比表。"""
    if sample_key == "tutorial":
        return _steps_block(_DEMO_STEPS, style)
    if sample_key == "opinion":
        return _timeline_block(_DEMO_TIMELINE, style)
    if sample_key == "data":
        return _compare_table(_DEMO_COMPARE, style)
    return []


def demo_preview_html(style: dict[str, Any], sample_id: str | None = None) -> str:
    """用样例文章渲染某样式的公众号效果（画廊点进卡片所见，可切换样例形态）。"""
    sample = DEMO_SAMPLES.get(sample_id or "", DEMO_SAMPLES[DEMO_DEFAULT_SAMPLE])
    sample_key = next((key for key, value in DEMO_SAMPLES.items() if value is sample), DEMO_DEFAULT_SAMPLE)
    content = sample["content"]
    body = "".join(_body_html(content, [], {}, style)) + "".join(_demo_extra_blocks(sample_key, style))
    if style.get("skeleton") == "table":
        return (
            f'<section style="max-width:677px;margin:0 auto;background-color:{style["page_bg"]};padding:0;">'
            f'<table width="100%" cellspacing="0" cellpadding="0" border="0" '
            f'style="border-collapse:collapse;border-spacing:0;background-color:'
            f'{style["page_bg"]};border:none;">'
            f'<tr><td style="background-color:{style["page_bg"]};padding:0 0 40px 0;'
            f'border:none;">{body}</td></tr></table></section>'
        )
    return (
        f'<section style="max-width:677px;margin:0 auto;background-color:{style["page_bg"]};'
        f'padding:0 0 40px 0;font-family:{style["font"]};color:{style["text"]};'
        f'line-height:{style["line_height"]};">{body}</section>'
    )


# --------------------------------------------------- 段落参数与素材图注 ----


def _clamp(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(number, low), high)


def section_paragraph_style(section: dict[str, Any]) -> str:
    """把编辑器排版工具栏写入的 section.style 转成公众号兼容的内联样式。"""
    style = section.get("style") if isinstance(section.get("style"), dict) else {}
    if not style:
        return ""
    extra: list[str] = []
    if "indent" in style:
        extra.append(f"text-indent:{_clamp(style['indent'], 0, 4, 0):g}em")
    if "line_height" in style:
        extra.append(f"line-height:{_clamp(style['line_height'], 1.1, 3, 1.85):g}")
    if "letter_spacing" in style:
        extra.append(f"letter-spacing:{_clamp(style['letter_spacing'], -1, 4, 0):g}px")
    if "margin_top" in style:
        extra.append(f"margin-top:{_clamp(style['margin_top'], 0, 64, 0):g}px")
    if "margin_bottom" in style:
        extra.append(f"margin-bottom:{_clamp(style['margin_bottom'], 0, 64, 17):g}px")
    align = str(style.get("align") or "")
    if align in {"left", "center", "right", "justify"}:
        extra.append(f"text-align:{align}")
    return ";".join(extra)


def asset_caption(asset: dict[str, Any]) -> str:
    try:
        structured = json.loads(asset.get("structured_data") or "{}")
    except (json.JSONDecodeError, TypeError):
        structured = {}
    figure_index = structured.get("figure_index")
    page = structured.get("page")
    if figure_index:
        caption = f"论文图 {figure_index}"
        if page:
            caption += f"（第 {page} 页）"
        return caption
    return "论文配图"


# --------------------------------------------------------------- 渲染器 ----


def _fig(url: str, caption: str, style: dict[str, Any]) -> list[str]:
    rad = style["radius"]
    return [
        '<section style="margin:24px 24px;">',
        f'<img src="{html_module.escape(str(url), quote=True)}" '
        f'style="width:100%;display:block;border-radius:{rad}px;" alt="{escape_text(caption)}" />',
        f'<p style="margin:8px 0 0;font-size:{style["sizes"]["small"]}px;line-height:1.5;'
        f'color:{style["muted"]};text-align:center;">{escape_text(caption)}</p>',
        "</section>",
    ]


def _header(content: dict[str, Any], style: dict[str, Any]) -> list[str]:
    title = escape_text(str(content.get("title", "")))
    summary = escape_text(str(content.get("summary", "")))
    sizes = style["sizes"]
    kind = style["header"]

    if kind == "magazine":
        return [
            f'<section style="background-color:{style["accent"]};padding:14px 24px;">',
            '<p style="margin:0;font-size:12px;letter-spacing:3px;color:#fefefe;'
            'font-weight:700;">WANREN</p>',
            "</section>",
            '<section style="padding:28px 24px 8px;">',
            f'<p style="margin:0 0 12px 0;"><span style="font-size:{sizes["small"]}px;'
            f'color:{style["accent"]};font-weight:600;letter-spacing:2px;'
            f'font-family:{style["font"]};">深度阅读</span></p>',
            f'<p style="margin:0 0 12px 0;"><span style="font-size:{sizes["cover"]}px;'
            f'font-weight:900;color:{style["title"]};line-height:1.25;'
            f'font-family:{style["font"]};">{title}</span></p>',
            f'<p style="margin:0;"><span style="font-size:{sizes["small"]}px;'
            f'color:{style["muted"]};">{summary}</span></p>',
            "</section>",
        ]
    if kind == "dark_banner":
        return [
            f'<section style="background-color:{style["accent"]};padding:14px 24px;">',
            '<p style="margin:0;font-size:11px;letter-spacing:3px;color:#080808;'
            'font-weight:700;">WANREN TECH</p>',
            "</section>",
            '<section style="padding:30px 24px 8px;">',
            f'<p style="margin:0 0 12px 0;"><span style="font-size:11px;letter-spacing:2px;'
            f'color:{style["accent2"]};font-weight:700;">SPECIAL REPORT</span></p>',
            f'<p style="margin:0 0 12px 0;"><span style="font-size:{sizes["cover"]}px;'
            f'font-weight:800;color:{style["title"]};line-height:1.25;'
            f'font-family:{style["font"]};">{title}</span></p>',
            f'<p style="margin:0;"><span style="font-size:{style["sizes"]["body"]}px;'
            f'color:{style["muted"]};">{summary}</span></p>',
            "</section>",
        ]
    if kind == "center":
        return [
            '<section style="padding:36px 24px 8px;text-align:center;">',
            f'<p style="margin:0 0 14px 0;"><span style="font-size:12px;color:{style["accent"]};'
            'letter-spacing:4px;">栏目</span></p>',
            f'<p style="margin:0 0 12px 0;"><span style="font-size:{sizes["cover"]}px;'
            f'font-weight:800;color:{style["title"]};line-height:1.35;'
            f'font-family:{style["font"]};">{title}</span></p>',
            f'<p style="margin:0 0 18px 0;"><span style="font-size:14px;'
            f'color:{style["muted"]};">{summary}</span></p>',
            f'<section style="width:36px;height:3px;margin:0 auto;background-color:'
            f'{style["accent"]};font-size:0;line-height:0;">&nbsp;</section>',
            "</section>",
        ]
    if kind == "frame":
        return [
            f'<section style="margin:24px 24px 8px;padding:18px 20px;border:1px solid '
            f'{style["border"]};background-color:{style["accent_soft"]};">',
            f'<p style="margin:0 0 10px 0;"><span style="font-size:11px;letter-spacing:2px;'
            f'color:{style["accent"]};font-weight:700;">PAPER READING</span></p>',
            f'<p style="margin:0 0 8px 0;"><span style="font-size:{sizes["cover"]}px;'
            f'font-weight:800;color:{style["title"]};line-height:1.3;'
            f'font-family:{style["font"]};">{title}</span></p>',
            f'<p style="margin:0;"><span style="font-size:{sizes["small"]}px;'
            f'color:{style["muted"]};line-height:1.6;">{summary}</span></p>',
            "</section>",
        ]
    if kind == "brand":
        # 公众号品牌回退：沿用旧版 WANREN READING 头卡
        return [
            f'<section style="margin:0 24px 28px;padding:20px 22px;border-left:6px solid '
            f'{style["accent"]};background-color:{style["accent_soft"]};">',
            f'<p style="margin:0 0 8px;font-size:12px;letter-spacing:3px;color:{style["accent"]};'
            'font-weight:700;">WANREN READING</p>',
            f'<p style="margin:0 0 10px;"><span style="font-size:25px;line-height:1.35;'
            f'color:{style["title"]};font-family:{style["font"]};">{title}</span></p>',
            f'<p style="margin:0;font-size:15px;line-height:1.7;color:{style["muted"]};">{summary}</p>',
            "</section>",
        ]
    # minimal / 品牌默认
    return [
        '<section style="padding:32px 24px 8px;">',
        f'<p style="margin:0 0 14px 0;"><span style="font-size:11px;letter-spacing:2px;'
        f'color:{style["muted"]};">WANREN</span></p>',
        f'<p style="margin:0 0 10px 0;"><span style="font-size:{sizes["cover"]}px;'
        f'font-weight:700;color:{style["title"]};line-height:1.4;'
        f'font-family:{style["font"]};">{title}</span></p>',
        f'<p style="margin:0;"><span style="font-size:{sizes["small"]}px;'
        f'color:{style["muted"]};line-height:1.7;">{summary}</span></p>',
        "</section>",
    ]


def _section_heading(index: int, heading: str, style: dict[str, Any]) -> list[str]:
    heading = escape_text(str(heading))
    sizes = style["sizes"]
    kind = style["section_heading"]

    if kind == "watermark":
        return [
            '<section style="margin:34px 24px 14px;">',
            '<p style="margin:0 0 2px 0;"><span data-layout-text="true" '
            'style="font-size:44px;line-height:1;color:#E5EBFF;'
            "font-weight:900;font-family:'Times New Roman',serif;\">"
            f"{index:02d}</span></p>",
            f'<p style="margin:0;padding-bottom:8px;border-bottom:1px solid {style["border"]};">'
            f'<span style="font-size:{sizes["section"]}px;font-weight:700;'
            f'color:{style["title"]};font-family:{style["font"]};">{heading}</span></p>',
            "</section>",
        ]
    if kind == "spaced_label":
        spaced = "&nbsp;".join(list(str(index).zfill(2)))
        return [
            '<section style="margin:30px 24px 12px;">',
            f'<p style="margin:0 0 6px 0;"><span data-layout-text="true" '
            f'style="font-size:11px;letter-spacing:3px;color:{style["accent"]};'
            f'font-weight:700;">{spaced}</span></p>',
            f'<p style="margin:0;"><span style="font-size:{sizes["section"]}px;'
            f'font-weight:700;color:{style["title"]};font-family:{style["font"]};">{heading}</span></p>',
            f'<p style="margin:8px 0 0;font-size:0;line-height:0;border-top:1px solid '
            f'{style["border"]};">&nbsp;</p>',
            "</section>",
        ]
    if kind == "thin_line":
        return [
            f'<section style="margin:36px 24px 16px;padding-top:14px;'
            f'border-top:1px solid {style["border"]};">',
            f'<p style="margin:0;"><span data-layout-text="true" '
            f'style="font-size:{sizes["small"]}px;color:{style["muted"]};'
            f'margin-right:10px;">{index:02d}</span>'
            f'<span style="font-size:{sizes["section"]}px;font-weight:600;'
            f'color:{style["title"]};font-family:{style["font"]};">{heading}</span></p>',
            "</section>",
        ]
    if kind == "dashed":
        return [
            f'<section style="margin:30px 24px 12px;padding-top:14px;'
            f'border-top:1px dashed {style["border"]};">',
            f'<p style="margin:0;"><span style="font-size:{sizes["section"]}px;'
            f'font-weight:700;color:{style["title"]};font-family:{style["font"]};">'
            f'<span style="color:{style["accent"]};margin-right:8px;">❋</span>{heading}</span></p>',
            "</section>",
        ]
    if kind == "dot":
        return [
            '<section style="margin:30px 24px 12px;">',
            "<p style=\"margin:0;\">"
            f'<span style="display:inline-block;width:26px;height:26px;line-height:26px;'
            f'border-radius:50%;background-color:{style["accent"]};color:#fefefe;'
            f'font-size:{sizes["small"]}px;font-weight:700;text-align:center;'
            f'margin-right:10px;">{index}</span>'
            f'<span style="font-size:{sizes["section"]}px;font-weight:700;'
            f'color:{style["title"]};font-family:{style["font"]};">{heading}</span></p>',
            "</section>",
        ]
    if kind == "navy_bar":
        return [
            f'<section style="margin:32px 24px 12px;border-left:4px solid {style["accent"]};'
            f'padding-left:12px;">',
            f'<p style="margin:0 0 2px 0;"><span data-layout-text="true" '
            f'style="font-size:12px;color:{style["accent"]};font-weight:700;'
            f'letter-spacing:1px;">{index:02d} /</span></p>',
            f'<p style="margin:0;"><span style="font-size:{sizes["section"]}px;'
            f'font-weight:700;color:{style["title"]};font-family:{style["font"]};">{heading}</span></p>',
            "</section>",
        ]
    if kind == "big_number":
        return [
            '<section style="margin:30px 24px 12px;">',
            f'<p style="margin:0 0 4px 0;"><span data-layout-text="true" '
            f'style="font-size:30px;font-weight:900;color:{style["accent"]};'
            f'line-height:1;">{index}</span></p>',
            f'<p style="margin:0;"><span style="font-size:{sizes["section"]}px;'
            f'font-weight:700;color:{style["title"]};font-family:{style["font"]};">{heading}</span></p>',
            "</section>",
        ]
    if kind == "step":
        return [
            '<section style="margin:32px 24px 12px;">',
            f'<p style="margin:0 0 6px 0;"><span data-layout-text="true" '
            f'style="display:inline-block;padding:2px 10px;border:1px solid {style["accent"]};'
            f'color:{style["accent"]};font-size:11px;font-weight:700;'
            f'letter-spacing:1px;">STEP {index:02d}</span></p>',
            f'<p style="margin:0;"><span style="font-size:{sizes["section"]}px;'
            f'font-weight:700;color:{style["title"]};font-family:{style["font"]};">{heading}</span></p>',
            "</section>",
        ]
    return _section_heading(index, heading, {**style, "section_heading": "thin_line"})


def _quote_block(paragraph: str, style: dict[str, Any]) -> list[str]:
    text = escape_text(str(paragraph))
    kind = style["quote"]
    sizes = style["sizes"]

    if kind == "accent_card":
        return [
            f'<section style="margin:22px 24px;padding:16px 18px;background-color:'
            f'{style["accent"]};border-radius:{style["radius"]}px;">',
            f'<p style="margin:0;font-size:{sizes["body"]}px;line-height:1.75;color:#fefefe;'
            f'font-weight:600;text-align:center;">{text}</p>',
            "</section>",
        ]
    if kind == "dark_card":
        return [
            f'<section style="margin:22px 24px;padding:16px 18px;background-color:'
            f'{style["card_bg"]};border:1px solid {style["border"]};'
            f'border-radius:{style["radius"]}px;">',
            f'<p style="margin:0;font-size:{sizes["body"]}px;line-height:1.75;'
            f'color:{style["title"]};text-align:center;">{text}</p>',
            "</section>",
        ]
    if kind == "plain_center":
        return [
            '<section style="margin:30px 24px;">',
            f'<p style="margin:0;font-size:{sizes["body"] + 1}px;line-height:1.9;'
            f'color:{style["title"]};text-align:center;font-weight:600;">{text}</p>',
            "</section>",
        ]
    if kind == "warm_card":
        return [
            f'<section style="margin:22px 24px;padding:14px 18px;background-color:'
            f'{style["accent_soft"]};border-radius:{style["radius"]}px;">',
            f'<p style="margin:0;font-size:{sizes["body"]}px;line-height:1.8;'
            f'color:{style["title"]};text-align:center;font-family:{style["font"]};">'
            f'<span style="color:{style["accent"]};">“</span>{text}'
            f'<span style="color:{style["accent"]};">”</span></p>',
            "</section>",
        ]
    if kind == "soft_card":
        return [
            f'<section style="margin:22px 24px;padding:14px 18px;background-color:'
            f'{style["accent_soft"]};border-radius:{style["radius"]}px;">',
            f'<p style="margin:0;font-size:{sizes["body"]}px;line-height:1.8;'
            f'color:{style["text"]};text-align:center;">{text}</p>',
            "</section>",
        ]
    if kind == "highlight":
        return [
            '<section style="margin:20px 24px;">',
            f'<p style="margin:0;font-size:{sizes["body"]}px;line-height:1.8;'
            f'color:{style["title"]};text-align:center;font-weight:600;">'
            f'<span style="background-color:{style.get("highlight", "#FFE9A8")};'
            f'padding:2px 4px;">{text}</span></p>',
            "</section>",
        ]
    if kind == "definition":
        return [
            f'<section style="margin:22px 24px;padding:14px 16px;border:1px solid '
            f'{style["border"]};background-color:{style["accent_soft"]};">',
            f'<p style="margin:0;font-size:{sizes["body"]}px;line-height:1.8;'
            f'color:{style["text"]};">{text}</p>',
            "</section>",
        ]
    # outline（品牌默认 / 商务蓝灰）
    return [
        f'<section style="margin:22px 24px;padding:12px 16px;border-left:3px solid '
        f'{style["accent"]};background-color:{style["accent_soft"]};">',
        f'<p style="margin:0;font-size:{sizes["body"]}px;line-height:1.75;'
        f'color:{style["text"]};">{text}</p>',
        "</section>",
    ]


def _metric_cards(metrics: list[Any], style: dict[str, Any]) -> list[str]:
    """指标数据卡：两列 table（微信安全的多列实现）。"""
    clean = [m for m in metrics[:4] if isinstance(m, dict)]
    if not clean:
        return []
    sizes = style["sizes"]
    parts = [
        f'<table width="100%" cellspacing="0" cellpadding="0" border="0" '
        f'style="border-collapse:collapse;border-spacing:0;margin:24px 24px;'
        f'width:calc(100% - 48px);border:none;">'
    ]
    for start in range(0, len(clean), 2):
        row = clean[start : start + 2]
        parts.append("<tr>")
        for metric in row:
            value = escape_text(str(metric.get("value", "")))
            label = escape_text(str(metric.get("label", "指标")))
            parts.append(
                f'<td style="width:50%;padding:0 6px 12px 0;border:none;">'
                f'<section style="padding:14px 12px;background-color:{style["accent_soft"]};'
                f'border:1px solid {style["border"]};border-radius:{style["radius"]}px;'
                f'text-align:center;">'
                f'<p style="margin:0 0 5px;font-size:20px;font-weight:700;'
                f'color:{style["accent"]};">{value}</p>'
                f'<p style="margin:0;font-size:{sizes["small"]}px;color:{style["muted"]};">'
                f"{label}</p></section></td>"
            )
        if len(row) == 1:
            parts.append('<td style="width:50%;border:none;">&nbsp;</td>')
        parts.append("</tr>")
    parts.append("</table>")
    return parts


def _source_card(content: dict[str, Any], style: dict[str, Any]) -> list[str]:
    editorial = content.get("editorial") if isinstance(content.get("editorial"), dict) else {}
    source = editorial.get("source") if isinstance(editorial.get("source"), dict) else {}
    items: list[str] = []
    if source.get("title"):
        items.append(f"论文：{escape_text(str(source['title']))}")
    if source.get("journal"):
        items.append(f"期刊：{escape_text(str(source['journal']))}")
    if source.get("doi"):
        items.append(f"DOI：{escape_text(str(source['doi']))}")
    if source.get("license"):
        items.append(f"许可：{escape_text(str(source['license']))}")
    if not items:
        return []
    sizes = style["sizes"]
    return [
        f'<section style="margin:34px 24px 18px;padding:16px 18px;border:1px solid '
        f'{style["border"]};background-color:{style["accent_soft"]};'
        f'border-radius:{style["radius"]}px;">',
        f'<p style="margin:0 0 7px 0;font-size:{sizes["body"]}px;font-weight:700;'
        f'color:{style["accent"]};">论文信息</p>',
        f'<p style="margin:0;font-size:{sizes["body"]}px;line-height:1.8;'
        f'color:{style["muted"]};">{"；".join(items)}</p>',
        "</section>",
    ]


def _end(style: dict[str, Any]) -> list[str]:
    kind = style["end"]
    small = style["sizes"]["small"]

    if kind == "dots":
        return [
            '<section style="margin:36px 0 8px;text-align:center;">',
            f'<p style="margin:0;font-size:14px;color:{style["muted"]};'
            'letter-spacing:6px;">· · ·</p>',
            "</section>",
        ]
    if kind == "ladder":
        return [
            f'<section style="margin:36px 24px 8px;padding-top:16px;'
            f'border-top:1px solid {style["border"]};text-align:center;">',
            f'<p style="margin:0;font-size:{small}px;letter-spacing:3px;'
            f'color:{style["accent"]};font-weight:700;">CONTINUE READING</p>',
            "</section>",
        ]
    if kind == "warm_note":
        return [
            f'<section style="margin:36px 24px 8px;padding-top:14px;'
            f'border-top:1px dashed {style["border"]};text-align:center;">',
            f'<p style="margin:0;font-size:{small}px;color:{style["muted"]};'
            f'font-family:{style["font"]};">谢谢读到这里 ❋</p>',
            "</section>",
        ]
    if kind == "tip":
        return [
            f'<section style="margin:32px 24px 8px;padding:12px 16px;background-color:'
            f'{style["accent_soft"]};border-radius:{style["radius"]}px;text-align:center;">',
            f'<p style="margin:0;font-size:{small}px;color:{style["accent"]};'
            'line-height:1.7;">觉得有用就点个「在看」吧</p>',
            "</section>",
        ]
    if kind == "cta":
        return [
            f'<section style="margin:32px 24px 8px;padding:14px 16px;background-color:'
            f'{style["accent"]};border-radius:{style["radius"]}px;text-align:center;">',
            f'<p style="margin:0;font-size:{small}px;color:#fefefe;font-weight:700;'
            'letter-spacing:1px;">关注我，下期继续 →</p>',
            "</section>",
        ]
    if kind == "references":
        return [
            f'<section style="margin:36px 24px 8px;padding-top:12px;'
            f'border-top:1px solid {style["border"]};">',
            f'<p style="margin:0;font-size:{small}px;color:{style["muted"]};'
            'line-height:1.7;">本文由万仞辅助写作，内容与结论以原文为准。</p>',
            "</section>",
        ]
    return [
        f'<section style="margin:36px 24px 8px;padding-top:12px;'
        f'border-top:1px solid {style["border"]};text-align:center;">',
        f'<p style="margin:0;font-size:{small}px;letter-spacing:3px;'
        f'color:{style["muted"]};">— END —</p>',
        "</section>",
    ]


def _quote_like(paragraph: str) -> bool:
    text = str(paragraph).strip()
    if len(text) > 80:
        return False
    opens_ = (chr(0x201C), chr(34), chr(0x300C))
    closes_ = (chr(0x201D), chr(34), chr(0x300D))
    return text.startswith(opens_) or text.endswith(closes_)


def _body_html(content: dict[str, Any], assets: list[dict[str, Any]], urls: dict[str, str], style: dict[str, Any]) -> list[str]:
    """正文区块（不含页面骨架）：头部 + 章节 + 图 + 金句 + 数据卡 + 文末。"""
    parts: list[str] = []
    parts.extend(_header(content, style))
    asset_index = {str(asset.get("id")): asset for asset in assets}
    sizes = style["sizes"]
    editorial = content.get("editorial") if isinstance(content.get("editorial"), dict) else {}
    metrics = editorial.get("metrics") if isinstance(editorial.get("metrics"), list) else []

    metric_cards_added = False
    sections = content.get("sections", [])
    for index, section in enumerate(sections, start=1):
        parts.extend(_section_heading(index, section.get("heading", ""), style))
        paragraph_style = section_paragraph_style(section)
        paragraph_suffix = (";" + paragraph_style) if paragraph_style else ""
        for paragraph in section.get("paragraphs", []):
            paragraph = str(paragraph)
            if _quote_like(paragraph):
                parts.extend(_quote_block(paragraph, style))
                continue
            parts.append(
                f'<p style="margin:0 24px 16px;font-size:{sizes["body"]}px;'
                f'line-height:{style["line_height"]};color:{style["text"]};'
                f'text-align:justify;font-family:{style["font"]};{paragraph_suffix.lstrip(";")}'
                f'">{escape_text(paragraph)}</p>'
            )
        for ref in section.get("asset_refs", []):
            asset = asset_index.get(str(ref))
            if asset is None:
                continue
            if asset.get("asset_type") == "IMAGE":
                url = urls.get(str(asset["id"]))
                if url:
                    parts.extend(_fig(url, asset_caption(asset), style))
            elif asset.get("asset_type") == "VIDEO":
                parts.append(
                    f'<section style="margin:22px 24px;padding:14px 16px;'
                    f'border:1px dashed {style["border"]};border-radius:{style["radius"]}px;'
                    f'color:{style["muted"]};font-size:{sizes["small"]}px;'
                    f'text-align:center;">视频素材：{escape_text(str(asset.get("file_name", "")))}'
                    "（发布后由公众号素材库替换）</section>"
                )
        if (
            not metric_cards_added
            and metrics
            and any(word in str(section.get("heading", "")) for word in ("实验", "结果", "效果"))
        ):
            metric_cards_added = True
            parts.extend(_metric_cards(metrics, style))

    if metrics and not metric_cards_added:
        parts.extend(_metric_cards(metrics, style))
    parts.extend(_source_card(content, style))
    parts.extend(_end(style))
    return parts


def render_article_html_with_style(
    content: dict[str, Any],
    assets: list[dict[str, Any]],
    urls: dict[str, str],
    style: dict[str, Any],
) -> str:
    """按已解析的完整样式字典渲染公众号正文 HTML（内置与自定义样式同路）。"""
    body = "".join(_body_html(content, assets, urls, style))
    if style["skeleton"] == "table":
        return (
            f'<section style="max-width:677px;margin:0 auto;background-color:{style["page_bg"]};'
            f'padding:0;">'
            f'<table width="100%" cellspacing="0" cellpadding="0" border="0" '
            f'style="border-collapse:collapse;border-spacing:0;background-color:'
            f'{style["page_bg"]};border:none;">'
            f'<tr><td style="background-color:{style["page_bg"]};padding:0 0 40px 0;'
            f'border:none;">{body}</td></tr></table></section>'
        )
    return (
        f'<section style="max-width:677px;margin:0 auto;background-color:{style["page_bg"]};'
        f'padding:0 0 40px 0;font-family:{style["font"]};color:{style["text"]};'
        f'line-height:{style["line_height"]};">{body}</section>'
    )


def render_article_html(
    content: dict[str, Any],
    assets: list[dict[str, Any]],
    urls: dict[str, str],
    settings: dict[str, Any],
    typesetting_id: str | None = None,
) -> str:
    """按样式模版渲染公众号正文 HTML（全内联样式，微信粘贴安全）。"""
    style = style_for_article(typesetting_id, settings or {})
    return render_article_html_with_style(content, assets, urls, style)
