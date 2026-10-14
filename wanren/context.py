from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .database import Database
from .editorial import build_evidence_digest


@dataclass
class SessionContext:
    session: dict[str, Any]
    assets: list[dict[str, Any]]
    template: dict[str, Any] | None = None
    facts: list[str] = field(default_factory=list)
    image_summaries: list[str] = field(default_factory=list)
    truncated: bool = False
    estimated_tokens: int = 0
    evidence_digest: dict[str, Any] = field(default_factory=dict)
    writing_style: dict[str, Any] | None = None

    def to_model_payload(self) -> dict[str, Any]:
        payload = {
            "title": self.session["title"],
            "goal": self.session["goal"],
            "audience": self.session["audience"],
            "tone": self.session["tone"],
            "requirements": json.loads(self.session["requirements"]),
            "facts": self.facts[:80],
            "image_summaries": self.image_summaries[:40],
            "evidence_digest": self.evidence_digest,
            "template": self.template,
        }
        # 文字风格只传刻画与规则给下游（范文由生成端以 few-shot 形式注入，避免双份）
        if self.writing_style:
            payload["writing_style"] = {
                key: self.writing_style.get(key)
                for key in ("id", "name", "vibe", "description", "rules")
                if self.writing_style.get(key) is not None
            }
        return payload


def _asset_facts(asset: dict[str, Any], limit: int) -> list[str]:
    facts: list[str] = []
    asset_type = asset["asset_type"]
    if asset_type in {"CSV", "XLSX", "JSON"}:
        structured = json.loads(asset["structured_data"] or "{}")
        if asset_type == "JSON":
            facts.append(f"{asset['file_name']}: {json.dumps(structured.get('value'), ensure_ascii=False)[:limit]}")
        else:
            sheets = structured.get("sheets") if asset_type == "XLSX" else None
            if sheets:
                for sheet in sheets:
                    rows = sheet.get("rows", [])
                    header = rows[0] if rows else []
                    for row in rows[1:4]:
                        facts.append(
                            f"{asset['file_name']}/{sheet['name']}: "
                            + "; ".join(f"{key}={value}" for key, value in zip(header, row))
                        )
            else:
                columns = structured.get("columns", [])
                for row in structured.get("rows", [])[:4]:
                    facts.append(
                        f"{asset['file_name']}: "
                        + "; ".join(f"{key}={row.get(key)}" for key in columns)
                    )
    elif asset_type in {"TEXT", "PDF", "DOCX"}:
        facts.append(f"{asset['file_name']}: {(asset['extracted_text'] or '')[:limit]}")
    elif asset_type == "IMAGE":
        metadata = json.loads(asset["metadata_json"] or "{}")
        vision = json.loads(asset["vision_result"] or "{}")
        description = vision.get("description") or vision.get("raw") or ""
        if metadata.get("derivative_type") == "PDF_FIGURE":
            # 论文派生配图：视觉分析通常缺席，用抽取时抓到的图注兜底描述
            page_no = metadata.get("page") or json.loads(asset["structured_data"] or "{}").get("page")
            description = description or (metadata.get("caption_guess") or "论文配图") + (
                f"（第 {page_no} 页）" if page_no else ""
            )
        facts.append(
            f"{asset['file_name']}: {description or 'awaiting vision analysis'}; "
            f"{metadata.get('width')}x{metadata.get('height')}"
        )
    elif asset_type == "VIDEO":
        metadata = json.loads(asset["metadata_json"] or "{}")
        facts.append(
            f"{asset['file_name']}: video {metadata.get('width')}x{metadata.get('height')}, "
            f"duration={metadata.get('duration_seconds')}s, codec={metadata.get('codec')}, "
            f"fps={metadata.get('fps')}, thumbnail={'yes' if metadata.get('thumbnail_asset_id') else 'no'}"
        )
    return facts


def build_session_context(db: Database, session_id: str, token_budget: int = 12000) -> SessionContext:
    session = db.query_one("SELECT * FROM content_session WHERE id = ?", (session_id,))
    if session is None:
        raise KeyError("Session not found")
    assets = db.query_all(
        "SELECT * FROM session_asset WHERE session_id = ? ORDER BY created_at",
        (session_id,),
    )
    facts: list[str] = []
    image_summaries: list[str] = []
    truncated = False
    for asset in assets:
        if asset["processing_status"] == "FAILED":
            continue
        facts.extend(_asset_facts(asset, limit=5000 if asset["asset_type"] == "PDF" else 800))
        if asset["asset_type"] == "IMAGE":
            metadata = json.loads(asset["metadata_json"] or "{}")
            if metadata.get("derivative_type") == "PDF_FIGURE":
                caption = metadata.get("caption_guess") or "论文配图"
                image_summaries.append(f"{asset['id']}: {caption}（第 {metadata.get('page')} 页）")
            else:
                image_summaries.append(f"{asset['id']}: {asset['file_name']}")
    rendered = json.dumps(SessionContext(session, assets).to_model_payload(), ensure_ascii=False)
    estimated_tokens = max(1, len(rendered) // 4)
    while estimated_tokens > token_budget and facts:
        facts.pop()
        truncated = True
        rendered = json.dumps({"facts": facts}, ensure_ascii=False)
        estimated_tokens = max(1, len(rendered) // 4)
    evidence_digest = build_evidence_digest(session, assets)
    writing_style: dict[str, Any] | None = None
    writing_style_id = session.get("writing_style_id")
    if writing_style_id:
        from .writing import WRITING_STYLES

        builtin = WRITING_STYLES.get(writing_style_id)
        if builtin is not None:
            writing_style = dict(builtin)
        else:
            row = db.query_one("SELECT * FROM writing_style WHERE id = ?", (writing_style_id,))
            if row is not None:
                try:
                    payload = json.loads(row["payload_json"])
                except (json.JSONDecodeError, TypeError):
                    payload = {}
                writing_style = {
                    **payload,
                    "id": row["id"],
                    "name": row["name"],
                    "vibe": row["vibe"],
                    "suits": row["suits"],
                }
    context = SessionContext(
        session,
        assets,
        json.loads(session["brief"] or "{}").get("template"),
        facts,
        image_summaries,
        truncated,
        estimated_tokens,
        evidence_digest,
        writing_style,
    )
    rendered = json.dumps(context.to_model_payload(), ensure_ascii=False, default=str)
    context.estimated_tokens = max(1, len(rendered) // 4)
    return context
