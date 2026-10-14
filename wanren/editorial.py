from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Protocol


BOILERPLATE_LINES = {
    "original papers",
    "a r t i c l e  i n f o",
    "keywords:",
}


@dataclass(frozen=True)
class MetricEvidence:
    value: str
    label: str
    statement: str


def normalize_source_text(value: str) -> str:
    text = value.replace("\r", "\n").replace("\u00a0", " ")
    text = re.sub(r"-\n", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_sentences(value: str, limit: int = 420) -> list[str]:
    text = normalize_source_text(value)
    raw = re.split(r"(?<=[.!?。；])\s+", text)
    sentences: list[str] = []
    for item in raw:
        sentence = re.sub(r"\s+", " ", item).strip()
        if 35 <= len(sentence) <= limit:
            sentences.append(sentence)
    return sentences


def _paper_metadata(text: str, fallback_title: str) -> dict[str, Any]:
    doi_match = re.search(r"(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", text, re.IGNORECASE)
    doi = doi_match.group(1).rstrip(" .,;") if doi_match else ""
    journal = ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines[:20]:
        candidate = line.strip(" .,;")
        if (
            len(candidate) >= 8
            and candidate.lower() not in BOILERPLATE_LINES
            and re.search(r"letters|review|transactions|journal|proceedings", candidate, re.I)
            and not candidate.lower().startswith(("journal homepage", "www."))
        ):
            journal = candidate
            break

    title = ""
    head = text[: min(len(text), 5000)].split("A B S T R A C T", 1)[0]
    lines = [line.strip() for line in head.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        normalized = line.strip(" .,:;")
        if normalized.lower() not in BOILERPLATE_LINES or normalized.lower() != "original papers":
            continue
        candidates = lines[index + 1 : index + 7]
        title_lines: list[str] = []
        for candidate in candidates:
            if candidate.lstrip().startswith(","):
                break
            if re.match(r"^[A-Z][a-z]+\s+[A-Z][a-z]+\s+[a-z](?:,[a-z])?", candidate):
                break
            if "A R T I C L E" in candidate:
                break
            if candidate.lower() in BOILERPLATE_LINES or candidate == journal:
                continue
            if len(candidate) >= 18:
                title_lines.append(candidate)
        title = " ".join(title_lines[:3])
        break
    if not title:
        candidates = [line for line in lines if 16 <= len(line) <= 180 and ":" in line]
        title = max(candidates, key=len) if candidates else fallback_title

    license_match = re.search(r"(CC[\s-]*(?:BY|BY-NC|BY-NC-ND)(?:[-\s][0-9.]+)?)", text, re.IGNORECASE)
    year_match = re.search(r"\b(?:19|20)\d{2}\b", text)
    return {
        "title": re.sub(r"\s+", " ", title).strip(" ."),
        "journal": journal,
        "doi": doi,
        "license": license_match.group(1).strip() if license_match else "",
        "year": year_match.group(0) if year_match else "",
    }


_LABEL_FILLER = "的为是约达高达了仅只余"


def _context_label(sentence: str, start: int, fallback: str) -> str:
    """从数字前缀里取名词短语当标签：「体积收缩约64%」→「体积收缩」。

    裸「百分比」这类标签对读者零信息量；中文语境取数字前的连续汉字段，
    去掉「约/为/达到」等填充词，取不到（如英文句子）再退回 fallback。
    """
    match = re.search(r"([一-龥]{2,12})\s*$", sentence[:start])
    if not match:
        return fallback
    label = match.group(1)
    while label and label[-1] in _LABEL_FILLER:
        label = label[:-1]
    while label and label[0] in _LABEL_FILLER:
        label = label[1:]
    return label if len(label) >= 2 else fallback


def _metric_evidences(sentences: list[str]) -> list[MetricEvidence]:
    patterns = [
        (r"\bmAP(?:50)?\s*(?:of|is|=|达到)?\s*(0?\.\d+)", "mAP50"),
        (r"(\b\d+(?:\.\d+)?\s*FPS\b)", "FPS"),
        (r"(\b\d+(?:\.\d+)?\s*(?:GB|MB)\b)", "显存"),
        (r"(?:reduce[sd]?|by|降低|达到)\s*(\d+(?:\.\d+)?\s*%)", "百分比"),
        (r"(\d+(?:\.\d+)?\s*%)", "\u767e\u5206\u6bd4"),
        (r"\b(\d{3,}\s*(?:frames?|instances?|participants?|samples?))\b", "样本规模"),
    ]
    metrics: list[MetricEvidence] = []
    seen: set[str] = set()
    for sentence in sentences:
        for pattern, label in patterns:
            for match in re.finditer(pattern, sentence, re.IGNORECASE):
                value = re.sub(r"\s+", " ", match.group(1).strip())
                key = value.lower()
                if key in seen:
                    continue
                seen.add(key)
                metrics.append(
                    MetricEvidence(value, _context_label(sentence, match.start(1), label), sentence)
                )
                if len(metrics) >= 12:
                    return metrics
    return metrics


def _select_statements(
    sentences: list[str],
    keywords: tuple[str, ...],
    exclude: tuple[str, ...] = (),
    limit: int = 8,
) -> list[str]:
    selected: list[str] = []
    seen: set[str] = set()
    for sentence in sentences:
        lower = sentence.lower()
        if any(keyword.lower() in lower for keyword in keywords) and not any(
            keyword.lower() in lower for keyword in exclude
        ):
            key = lower[:100]
            if key not in seen:
                seen.add(key)
                selected.append(sentence)
        if len(selected) >= limit:
            break
    return selected


def _requirements(session: dict[str, Any]) -> list[str]:
    value = session.get("requirements") or []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = []
    return [str(item) for item in value] if isinstance(value, list) else []


def _asset_json(asset: dict[str, Any], key: str) -> dict[str, Any]:
    try:
        value = json.loads(asset.get(key) or "{}")
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def build_evidence_digest(
    session: dict[str, Any],
    assets: list[dict[str, Any]],
) -> dict[str, Any]:
    documents = [
        asset
        for asset in assets
        if asset["asset_type"] in {"PDF", "DOCX", "TEXT"}
        and asset["processing_status"] in {"READY", "VISION_READY"}
        and (asset["extracted_text"] or "").strip()
    ]
    primary = max(documents, key=lambda item: len(item["extracted_text"] or "")) if documents else None
    source_text = normalize_source_text(primary["extracted_text"] or "") if primary else ""
    sentences = split_sentences(source_text)
    paper = _paper_metadata(source_text, str(session.get("title") or "")) if primary and primary["asset_type"] == "PDF" else {}

    problem_statements = _select_statements(
        sentences,
        ("challenge", "limitation", "difficult", "however", "bottleneck", "难以", "挑战", "限制"),
        ("future work",),
        8,
    )
    method_statements = _select_statements(
        sentences,
        ("propose", "method", "framework", "system", "detector", "tracker", "提出", "方法", "框架"),
        (),
        10,
    )
    result_statements = _select_statements(
        sentences,
        ("achieve", "reduce", "improve", "accuracy", "performance", "fps", "map", "达到", "降低", "提升"),
        (),
        10,
    )
    limitation_statements = _select_statements(
        sentences,
        ("limitation", "future work", "small-group", "small group", "局限", "未来", "边界"),
        (),
        5,
    )
    metrics = _metric_evidences(sentences)

    images: list[dict[str, Any]] = []
    for asset in assets:
        if asset["asset_type"] != "IMAGE" or asset["processing_status"] not in {"READY", "VISION_READY"}:
            continue
        structured = _asset_json(asset, "structured_data")
        vision = _asset_json(asset, "vision_result")
        images.append(
            {
                "id": asset["id"],
                "file_name": asset["file_name"],
                "summary": str(vision.get("description") or vision.get("raw") or asset["file_name"])[:240],
                "page": structured.get("page"),
                "figure_index": structured.get("figure_index"),
            }
        )

    evidence_score = min(
        100,
        sum(
            [
                30 if source_text else 0,
                min(20, len(sentences) // 8),
                min(16, len(metrics) * 2),
                min(16, len(method_statements) * 2),
                min(12, len(result_statements) * 2),
                min(6, len(images) * 2),
            ]
        ),
    )
    return {
        "source_type": "paper" if paper and paper.get("title") else ("document" if source_text else "general"),
        "source_asset_id": primary["id"] if primary else "",
        "source_file": primary["file_name"] if primary else "",
        "paper": paper,
        "topic": paper.get("title") or session.get("title") or "",
        "problem_statements": problem_statements,
        "method_statements": method_statements,
        "result_statements": result_statements,
        "limitation_statements": limitation_statements,
        "metrics": [asdict(metric) for metric in metrics],
        "figures": images,
        "evidence_score": evidence_score,
        "source_excerpt": source_text[:14000],
    }


def editorial_style(context: SessionContextLike, instruction: str) -> str:
    intent = " ".join(
        [
            instruction,
            str(context.session.get("goal") or ""),
            str(context.session.get("title") or ""),
            str(context.session.get("tone") or ""),
            " ".join(_requirements(context.session)),
        ]
    )
    if any(word in intent for word in ("学院", "我院", "成果", "录用", "喜报", "宣传稿", "机构")):
        return "ORG_ANNOUNCEMENT"
    digest = context.evidence_digest or {}
    if digest.get("source_type") == "paper" or any(word in intent for word in ("论文", "文献", "期刊", "研究")):
        return "PAPER_INTERPRETATION"
    return "GENERAL_EXPLAINER"


def editorial_depth(context: SessionContextLike, instruction: str) -> str:
    intent = instruction.lower()
    if any(word in instruction for word in ("短读", "简讯", "摘要版", "短文章", "写短")):
        return "BRIEF"
    if any(word in instruction for word in ("深读", "详细", "深度", "系统解读")):
        return "DEEP"
    digest = context.evidence_digest or {}
    if int(digest.get("evidence_score") or 0) >= 58 and digest.get("source_type") == "paper":
        return "DEEP"
    if int(digest.get("evidence_score") or 0) >= 30:
        return "STANDARD"
    return "BRIEF"


def build_editorial_metadata(context: SessionContextLike, instruction: str = "") -> dict[str, Any]:
    digest = context.evidence_digest or {}
    paper = digest.get("paper") or {}
    metrics = digest.get("metrics") or []
    return {
        "style": editorial_style(context, instruction),
        "depth": editorial_depth(context, instruction),
        "metrics": [
            {"value": item.get("value", ""), "label": item.get("label", "指标")}
            for item in metrics
            if isinstance(item, dict)
        ][:6],
        "source": {
            "type": digest.get("source_type", "general"),
            "title": paper.get("title") or digest.get("topic", ""),
            "journal": paper.get("journal", ""),
            "doi": paper.get("doi", ""),
            "license": paper.get("license", ""),
        },
    }


class SessionContextLike(Protocol):
    session: dict[str, Any]
    evidence_digest: dict[str, Any] | None
