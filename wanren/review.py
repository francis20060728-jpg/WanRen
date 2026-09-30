from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any


PROHIBITED = ["全网最低", "100%有效", "绝对有效", "第一品牌"]

INSTRUCTION_MARKERS = (
    "system prompt",
    "json schema",
    "输出 json",
    "只返回一个合法 json",
    "提示词",
    "prompt",
    "asset_refs",
    "素材id",
)

INTERNAL_ID = re.compile(
    r"\b(?:asset|pdf|doc|docx|img|image|vid|video|version|article|session|template)_[a-z0-9]{8,}\b",
    re.IGNORECASE,
)

MECHANICAL_TITLE = re.compile(
    r"测试|终验|模板终验|验证万仞|真实链路|示例文章",
    re.IGNORECASE,
)


@dataclass
class ReviewIssue:
    type: str
    severity: str
    section_id: str | None
    message: str
    suggestion: str


@dataclass
class ReviewResult:
    score: int
    decision: str
    risk_level: str
    issues: list[ReviewIssue]
    metrics: dict[str, int]


def review_article(content: dict[str, Any], context_facts: list[str]) -> ReviewResult:
    issues: list[ReviewIssue] = []
    title = content.get("title", "")
    summary = content.get("summary", "")
    sections = content.get("sections", [])

    def normalized(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip()

    def source_leak() -> bool:
        # Citation sections may quote the source title, journal, DOI, and
        # license; long-copy detection is for narrative paragraphs.
        text = normalized(
            "\n".join(
                paragraph
                for section in sections
                if str(section.get("heading", "")).strip() not in {"论文信息", "来源信息"}
                for paragraph in section.get("paragraphs", [])
            )
        )
        for fact in context_facts:
            fact_text = normalized(str(fact))
            if len(fact_text) < 120:
                continue
            for start in range(0, len(fact_text) - 120, 60):
                chunk = fact_text[start : start + 120]
                if chunk and chunk in text:
                    return True
        return False

    def metric_without_context(paragraph: str) -> bool:
        if not re.search(r"(?:mAP|FPS|\d+(?:\.\d+)?\s*%)", paragraph, re.IGNORECASE):
            return False
        explanation_words = ("means", "indicates", "explains", "threshold", "accuracy", "consistency", "switch", "performance", "表示", "说明", "意味着", "阈值", "精度", "一致性", "切换", "性能", "工程意义")
        return len(paragraph) < 45 or not any(word in paragraph.lower() for word in explanation_words)

    full_text = "\n".join(
        [title, summary] + [p for section in sections for p in section.get("paragraphs", [])]
    )
    if len(title) > 32:
        issues.append(ReviewIssue("TITLE_TOO_LONG", "HIGH", None, "标题超过微信32字限制", "压缩标题"))
    if len(summary) > 120:
        issues.append(ReviewIssue("SUMMARY_TOO_LONG", "MEDIUM", None, "摘要超过120字", "压缩摘要"))
    if len(full_text) < 500:
        issues.append(ReviewIssue("TOO_SHORT", "MEDIUM", None, "正文内容过短", "补充任务素材中的事实和解释"))
    for term in PROHIBITED:
        if term in full_text:
            issues.append(ReviewIssue("PROHIBITED_TERM", "HIGH", None, f"包含禁用表达：{term}", "删除或改写"))

    lower_full = full_text.lower()
    if any(marker in lower_full for marker in INSTRUCTION_MARKERS):
        issues.append(
            ReviewIssue(
                "INSTRUCTION_LEAK",
                "HIGH",
                None,
                "Instruction text leaked into the article",
                "Remove system prompt or schema wording from the published text",
            )
        )
    if INTERNAL_ID.search(full_text):
        issues.append(
            ReviewIssue(
                "INTERNAL_ID_LEAK",
                "HIGH",
                None,
                "Internal asset or file ID leaked into the article",
                "Remove internal IDs from reader-facing text",
            )
        )
    if MECHANICAL_TITLE.search(title):
        issues.append(
            ReviewIssue(
                "MECHANICAL_TITLE",
                "HIGH",
                None,
                "Title looks like an internal test or template name",
                "Write a reader-facing title",
            )
        )
    if source_leak():
        issues.append(
            ReviewIssue(
                "SOURCE_TEXT_LEAK",
                "HIGH",
                None,
                "A long passage is copied from source material",
                "Rewrite the passage as an editorial interpretation",
            )
        )

    editorial = content.get("editorial") if isinstance(content.get("editorial"), dict) else {}
    editorial_source = editorial.get("source") if isinstance(editorial.get("source"), dict) else {}
    has_editorial_source = bool(editorial_source.get("doi") or editorial_source.get("title"))
    if not sections:
        issues.append(ReviewIssue("EMPTY_ARTICLE", "HIGH", None, "文章没有可用段落", "重新生成"))

    for section in sections:
        bullets = [p for p in section.get("paragraphs", []) if p.startswith(("- ", "* ", "• "))]
        if len(bullets) >= 2:
            issues.append(
                ReviewIssue(
                    "MECHANICAL_BULLET_LIST",
                    "MEDIUM",
                    section.get("id"),
                    "The section reads like a fact list rather than an article",
                    "Rewrite bullet points as connected paragraphs",
                )
            )
        if any(metric_without_context(p) for p in section.get("paragraphs", [])):
            issues.append(
                ReviewIssue(
                    "METRIC_WITHOUT_CONTEXT",
                    "MEDIUM",
                    section.get("id"),
                    "A metric is stated without enough explanation",
                    "Explain what the metric measures and why it matters",
                )
            )
        if not section.get("asset_refs") and not has_editorial_source and any(char.isdigit() for char in "\n".join(section.get("paragraphs", []))):
            issues.append(
                ReviewIssue(
                    "DATA_WITHOUT_SOURCE",
                    "MEDIUM",
                    section.get("id"),
                    "包含数字但未关联任务素材",
                    "补充asset_refs或删除无法溯源的数据",
                )
            )

    penalty = sum({"HIGH": 18, "MEDIUM": 8, "LOW": 3}[issue.severity] for issue in issues)
    score = max(0, min(100, 100 - penalty))
    high = any(issue.severity == "HIGH" for issue in issues)
    medium = any(issue.severity == "MEDIUM" for issue in issues)
    decision = "REJECT" if high else ("REVISE" if medium else "PASS")
    risk = "HIGH" if high else ("MEDIUM" if medium else "LOW")
    return ReviewResult(
        score,
        decision,
        risk,
        issues,
        {
            "fact_consistency": 92 if context_facts else 85,
            "instruction_fit": 95,
            "logic": 92,
            "readability": 90,
            "title_quality": 88 if len(title) <= 32 else 70,
            "image_match": 90,
            "risk_control": max(50, 100 - penalty),
        },
    )


def review_to_dict(result: ReviewResult) -> dict[str, Any]:
    value = asdict(result)
    value["issues"] = [asdict(issue) for issue in result.issues]
    return value
