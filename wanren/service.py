from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import os
import subprocess
import sys
import time
from urllib.parse import urlparse
from pathlib import Path
from typing import Any

import httpx

from .ai import GenerationError, GenerationResult, build_text_adapter
from .typesetting import STYLES as TYPESETTING_STYLES
from .typesetting import (
    account_style_fallback,
    demo_preview_html,
    render_article_html_with_style,
    validate_style_tokens,
)
from .assets import (
    VIDEO_TRANSCODE_PRESETS,
    clean_unicode,
    load_asset_payload,
    save_asset,
    store_derived_video,
    transcode_video_file,
)
from .auth import (
    has_permission,
    hash_password,
    permissions_for_role,
    token_digest,
    token_expired,
    token_expiry,
    verify_password,
)
from .config import Settings
from .context import SessionContext, build_session_context
from .crypto import decrypt_value, encrypt_value, mask_key
from .database import Database, new_id, utc_now
from .publishing import AssetFile, MockPublisher, Publisher, WeChatPublisher, render_article_html
from .review import review_article, review_to_dict
from .search import WebSearchError, build_search_provider
from .schemas import (
    AccountCreate,
    AccountUpdate,
    ArticleUpdate,
    LoginRequest,
    RegisterRequest,
    SessionCreate,
    TemplateCreate,
    TemplateGenerateRequest,
    TemplateSearchRequest,
    TemplateUpdate,
    UserCreate,
)


class ServiceError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class WanRenService:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.text_adapter = build_text_adapter(settings)
        self.search_provider = build_search_provider(settings)
        self.bootstrap()
        self._rebuild_text_adapter()  # DB 覆盖层（设置页保存的模型）优先于环境变量

    def _rebuild_text_adapter(self) -> None:
        """按生效配置（DB > 环境变量）重建文本适配器；设置页保存后立即生效。"""
        from .ai import LocalTextAdapter, OpenAICompatibleAdapter

        config = self._text_config()
        if config["provider"] != "openai_compatible" or not config["configured"]:
            self.text_adapter = LocalTextAdapter()
            return
        self.text_adapter = OpenAICompatibleAdapter(
            model=config["model"],
            api_key=config["api_key"],
            api_base=config["api_base"],
            timeout_seconds=self.settings.text_timeout_seconds,
            max_retries=self.settings.text_max_retries,
            fallback_local=self.settings.text_fallback_local,
        )

    def _model_prices(self, provider: str, model: str) -> tuple[float, float]:
        return self.settings.text_model_prices.get(
            (provider, model),
            (
                self.settings.text_input_price_per_million,
                self.settings.text_output_price_per_million,
            ),
        )

    def _ai_call_cost(
        self, provider: str, model: str, input_tokens: int, output_tokens: int
    ) -> float:
        input_price, output_price = self._model_prices(provider, model)
        cost = Decimal(str(input_price)) * Decimal(input_tokens) / Decimal(1_000_000)
        cost += Decimal(str(output_price)) * Decimal(output_tokens) / Decimal(1_000_000)
        return float(cost.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP))

    def _current_month_ai_cost(self) -> float:
        prefix = datetime.now(timezone.utc).strftime("%Y-%m")
        row = self.db.query_one(
            "SELECT COALESCE(SUM(cost),0) AS value FROM ai_run WHERE created_at LIKE ?",
            (f"{prefix}%",),
        )
        return float(row["value"] or 0) if row else 0.0

    def _enforce_ai_budget(self, provider: str, model: str) -> None:
        budget = self.settings.text_monthly_budget_usd
        if budget <= 0:
            return
        if self._current_month_ai_cost() >= budget:
            raise ServiceError(
                f"AI monthly budget exhausted: ${self._current_month_ai_cost():.8f} >= ${budget:.8f}",
                402,
            )

    def bootstrap(self) -> None:
        now = utc_now()
        with self.db.tx():
            if self.db.query_one("SELECT id FROM workspace WHERE id = 'ws_default'") is None:
                self.db.execute(
                    "INSERT INTO workspace(id,name,status,created_at) VALUES ('ws_default','默认工作空间','ACTIVE',?)",
                    (now,),
                )
            if self.db.query_one("SELECT id FROM official_account WHERE id = 'oa_default'") is None:
                self.db.execute(
                    """
                    INSERT INTO official_account
                    (id,workspace_id,name,app_id,token_status,settings,created_at)
                    VALUES ('oa_default','ws_default','本地测试账号','mock-app-id','MOCK',?,?)
                    """,
                    (
                        json.dumps(
                            {"style": {"accent": "#175641", "text": "#262626", "font": "system"}},
                            ensure_ascii=False,
                        ),
                        now,
                    ),
            )
            self._seed_builtin_templates()
            if self.settings.auth_mode != "disabled":
                self._ensure_admin_user(now)

    def _ensure_admin_user(self, now: str) -> None:
        admin_email = self.settings.admin_email.lower()
        existing = self.db.query_one(
            "SELECT id,role FROM app_user WHERE workspace_id = ? AND email = ?",
            ("ws_default", admin_email),
        )
        if existing is None:
            self.db.execute(
                """
                INSERT INTO app_user
                (id,workspace_id,email,name,password_hash,role,status,created_at)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    new_id("user"),
                    "ws_default",
                    admin_email,
                    "系统管理员",
                    hash_password(self.settings.admin_password),
                    "ADMIN",
                    "ACTIVE",
                    now,
                ),
            )
            return
        if existing["role"] != "ADMIN":
            raise ValueError(f"Configured admin email is already used by a non-admin user: {admin_email}")

        # Keeps container/env password changes effective without touching other users.
        self.db.execute(
            "UPDATE app_user SET password_hash = ?, role = ?, status = ? WHERE id = ?",
            (hash_password(self.settings.admin_password), "ADMIN", "ACTIVE", existing["id"]),
        )

    # ---------- 微信草稿导入 / 对话删除 ----------

    @staticmethod
    def _html_to_sections(html: str) -> list[dict[str, Any]]:
        """公众号正文 HTML → sections（标题按 h1-h4 切节；无标题归一节）。"""
        text_html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", html or "")

        def strip_tags(fragment: str) -> list[str]:
            fragment = re.sub(r"(?i)<br\s*/?>", "\n", fragment)
            fragment = re.sub(r"(?is)</p>\s*<p[^>]*>", "\n", fragment)
            text = re.sub(r"<[^>]+>", "", fragment)
            text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                        .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'"))
            lines = [line.strip() for line in text.split("\n")]
            return [line for line in lines if line]

        # 按 h1-h4 开标签切节（分隔符保留），首个块是无标题引言
        parts = re.split(r"(?is)<h[1-4][^>]*>", text_html)
        sections: list[dict[str, Any]] = []
        for index, part in enumerate(parts):
            if index == 0:
                lead = strip_tags(part)
                if lead:
                    sections.append({"heading": "正文", "paragraphs": lead})
                continue
            # 每个标题块：到最近的块级闭合为止取标题文本，其后为正文（吞掉残余的闭合标签名）
            title_zone, _, body_left = part.partition("</")
            title_lines = strip_tags(title_zone)
            heading = (title_lines[0][:60] if title_lines else "").strip()
            body = re.sub(r"^[^>]*>", "", body_left, count=1) if body_left else part
            paras = strip_tags(body) or strip_tags(part)
            paras = [p for p in paras if p != heading][:40]
            if heading:
                sections.append({"heading": heading, "paragraphs": paras or ["（本节为空）"]})
            elif sections:
                sections[-1]["paragraphs"].extend(paras)
            else:
                sections.append({"heading": "正文", "paragraphs": paras or ["（草稿正文为空）"]})
        if not sections:
            sections = [{"heading": "正文", "paragraphs": ["（草稿正文为空）"]}]
        out = []
        for index, sec in enumerate(sections, start=1):
            out.append({
                "id": f"sec_{index:02d}",
                "heading": sec["heading"] or f"第 {index} 节",
                "paragraphs": sec["paragraphs"][:40],
                "asset_refs": [],
            })
        return out[:12]

    async def list_wechat_drafts(self, account_id: str) -> dict[str, Any]:
        """草稿箱列表（标记哪些已导入本地，避免重复导入）。"""
        account = self.db.query_one('SELECT * FROM official_account WHERE id = ?', (account_id,))
        if account is None:
            raise ServiceError('Account not found', 404)
        result = await self._publisher(account).list_drafts(account)
        imported = {
            row["external_id"]
            for row in self.db.query_all(
                "SELECT DISTINCT external_id FROM publish_job WHERE external_id IS NOT NULL AND status='WECHAT_DRAFT'"
            )
        }
        for item in result.get("items", []):
            item["imported"] = item.get("media_id") in imported
        return result

    async def import_wechat_draft(self, account_id: str, media_id: str) -> dict[str, Any]:
        """把外部微信草稿导入为本地对话（session + article，保留 external_id 供同步）。"""
        account = self.db.query_one('SELECT * FROM official_account WHERE id = ?', (account_id,))
        if account is None:
            raise ServiceError('Account not found', 404)
        existing = self.db.query_one(
            "SELECT article_id FROM publish_job WHERE external_id=? AND status='WECHAT_DRAFT' LIMIT 1",
            (media_id,),
        )
        if existing:
            raise ServiceError("该草稿已导入过", 409)
        publisher = self._publisher(account)
        payload = await publisher.get_draft(media_id, account)
        news = (payload.get("news_item") or [{}])[0] if payload else None
        if not news:
            news = await publisher.find_draft_in_list(media_id, account)
        if not news:
            raise ServiceError("草稿不存在或已被删除", 404)
        title = str(news.get("title") or "导入草稿")[:32]
        summary = str(news.get("digest") or "")[:120]
        sections = self._html_to_sections(str(news.get("content") or ""))
        now = utc_now()
        session_id = new_id("session")
        article_id = new_id("article")
        version_id = new_id("version")
        content = {"title": title, "summary": summary, "sections": sections}
        with self.db.tx():
            self.db.execute(
                "INSERT INTO content_session(id,workspace_id,official_account_id,title,status,goal,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (session_id, "ws_default", account_id, title, "WAITING_HUMAN", "从微信草稿导入", now, now),
            )
            self.db.execute(
                "INSERT INTO article(id,session_id,title,summary,content_json,status,current_version_id,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (article_id, session_id, title, summary, json.dumps(content, ensure_ascii=False), "WECHAT_DRAFT", version_id, now, now),
            )
            self.db.execute(
                "INSERT INTO article_version(id,article_id,version_no,source,content_json,change_summary,created_at,highlight_json)"
                " VALUES (?,?,?,?,?,?,?,'[]')",
                (version_id, article_id, 1, "HUMAN", json.dumps(content, ensure_ascii=False), "从微信草稿导入", now),
            )
            self.db.execute(
                "INSERT INTO publish_job(id,article_id,official_account_id,mode,status,idempotency_key,external_id,retry_count,last_error,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (new_id("job"), article_id, account_id, "draft", "WECHAT_DRAFT",
                 f"import:{media_id}", media_id, 0, "{}", now, now),
            )
        self._insert_article_message(article_id, "ASSISTANT", f"已从微信草稿导入：《{title}》（外部草稿 {media_id[:12]}…）", "COMPLETED", {"version": 1})
        self.audit("article.import_wechat_draft", "article", article_id, {"media_id": media_id, "session": session_id})
        return {"article_id": article_id, "session_id": session_id, "title": title}

    def delete_article(self, article_id: str) -> dict[str, Any]:
        """删除对话：文章（版本/审核/消息/发布记录级联）；会话无剩余文章时连同其素材一并删除。"""
        article = self._get_article(article_id)
        session_id = article["session_id"]
        published = self.db.query_one(
            "SELECT id FROM publish_job WHERE article_id=? AND article_url IS NOT NULL LIMIT 1", (article_id,)
        )
        if published:
            raise ServiceError("该文章已发布，保留归档不可删除", 400)
        with self.db.tx():
            self.db.execute("DELETE FROM article WHERE id=?", (article_id,))
            remaining = self.db.query_one(
                "SELECT COUNT(*) AS n FROM article WHERE session_id=?", (session_id,)
            )
            if (remaining["n"] or 0) == 0:
                self.db.execute("DELETE FROM content_session WHERE id=?", (session_id,))
        self.audit("article.delete", "article", article_id, {"session_id": session_id, "cascade_session": True})
        return {"deleted": article_id, "session_deleted": session_id}

    @staticmethod
    def _diff_added_paragraphs(old_content: dict[str, Any], new_content: dict[str, Any]) -> list[str]:
        """新版相对旧版新增/改动的段落文本（命中即前端黄色高亮）。"""
        old_texts = {
            str(p).strip()
            for sec in (old_content or {}).get("sections", [])
            for p in sec.get("paragraphs", [])
        }
        added = [
            str(p).strip()
            for sec in (new_content or {}).get("sections", [])
            for p in sec.get("paragraphs", [])
            if str(p).strip() and str(p).strip() not in old_texts
        ]
        return added[:80]  # 防御性截断

    def audit(self, action: str, resource_type: str, resource_id: str, detail: dict[str, Any]) -> None:
        with self.db.tx():
            self.db.execute(
                """
                INSERT INTO audit_log(id,workspace_id,actor,action,resource_type,resource_id,detail,created_at)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    new_id("audit"),
                    "ws_default",
                    "local-operator",
                    action,
                    resource_type,
                    resource_id,
                    json.dumps(detail, ensure_ascii=False),
                    utc_now(),
                ),
            )

    def _seed_builtin_templates(self) -> None:
        now = utc_now()
        templates = [
            (
                "tpl_paper_interpretation",
                "论文解读",
                "面向科研论文的公众号解读结构，突出研究问题、方法、结果和应用价值。",
                [
                    {"id": "problem", "heading": "论文要解决什么问题", "guidance": "说明现有方式的关键限制，以及为什么值得读者关注。", "image_policy": "required"},
                    {"id": "method", "heading": "方法是怎么设计的", "guidance": "解释核心模块和系统流程，避免堆砌术语。", "image_policy": "optional"},
                    {"id": "results", "heading": "效果有多好", "guidance": "优先引用论文中的可核查指标和实验条件。", "image_policy": "optional"},
                    {"id": "application", "heading": "对实际场景的意义", "guidance": "连接读者场景，说明可落地的价值与边界。", "image_policy": "optional"},
                    {"id": "paper_info", "heading": "论文信息", "guidance": "保留题名、期刊、DOI、许可和阅读入口。", "image_policy": "none"},
                ],
            ),
            (
                "tpl_product_update",
                "产品更新",
                "适合产品功能发布和版本更新说明。",
                [
                    {"id": "scene", "heading": "用户遇到的变化", "guidance": "从一个具体使用场景切入。", "image_policy": "optional"},
                    {"id": "update", "heading": "这次更新了什么", "guidance": "按功能分组说明，突出用户收益。", "image_policy": "required"},
                    {"id": "usage", "heading": "怎么使用", "guidance": "给出清晰步骤和注意事项。", "image_policy": "optional"},
                    {"id": "next", "heading": "后续计划", "guidance": "说明路线图和反馈渠道。", "image_policy": "none"},
                ],
            ),
        ]
        for template_id, name, description, sections in templates:
            if self.db.query_one("SELECT id FROM article_template WHERE id = ?", (template_id,)) is None:
                with self.db.tx():
                    self.db.execute(
                        """
                        INSERT INTO article_template
                        (id,workspace_id,official_account_id,name,description,source,structure_json,style_json,status,version,created_by,created_at,updated_at)
                        VALUES (?,?,NULL,?,?,?,?,'{}','ACTIVE',1,'system',?,?)
                        """,
                        (
                            template_id,
                            "ws_default",
                            name,
                            description,
                            "BUILTIN",
                            json.dumps(sections, ensure_ascii=False),
                            now,
                            now,
                        ),
                    )

    def create_session(self, payload: SessionCreate) -> dict[str, Any]:
        account_id = payload.official_account_id
        if not account_id:
            preferred_status = "MOCK" if self.settings.publisher == "mock" else "READY"
            preferred = self.db.query_one(
                """
                SELECT id FROM official_account
                WHERE token_status=?
                ORDER BY created_at DESC LIMIT 1
                """,
                (preferred_status,),
            )
            account_id = preferred["id"] if preferred else "oa_default"
        account = self.db.query_one("SELECT * FROM official_account WHERE id = ?", (account_id,))
        if account is None:
            raise ServiceError("Official account not found", 404)
        template = None
        if payload.template_id:
            template = self.db.query_one(
                """
                SELECT * FROM article_template
                WHERE id=? AND workspace_id=? AND status='ACTIVE'
                """,
                (payload.template_id, "ws_default"),
            )
            if template is None:
                raise ServiceError("Article template not found", 404)
            template = {
                "id": template["id"],
                "name": template["name"],
                "description": template["description"],
                "source": template["source"],
                "structure": json.loads(template["structure_json"] or "[]"),
                "style": json.loads(template["style_json"] or "{}"),
                "version": template["version"],
            }
        session_id = new_id("session")
        now = utc_now()
        with self.db.tx():
            self.db.execute(
                """
                INSERT INTO content_session
                (id,workspace_id,official_account_id,title,status,goal,audience,tone,requirements,brief,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    session_id,
                    "ws_default",
                    account_id,
                    payload.title,
                    "OPEN",
                    payload.goal,
                    payload.audience,
                    payload.tone,
                    json.dumps(payload.requirements, ensure_ascii=False),
                    json.dumps(
                        {
                            "core_points": [],
                            "confirmed_title": None,
                            "template": template,
                        },
                        ensure_ascii=False,
                    ),
                    now,
                    now,
                ),
            )
            self.db.execute(
                "INSERT INTO session_message(id,session_id,role,content,created_at) VALUES (?,?,?,?,?)",
                (new_id("msg"), session_id, "USER", payload.goal or payload.title, now),
            )
        self.audit("session.create", "session", session_id, {"title": payload.title})
        return self.get_session(session_id)

    def list_sessions(self) -> list[dict[str, Any]]:
        return self.db.query_all(
            """
            SELECT s.*, a.title AS article_title, a.status AS article_status, a.id AS article_id
            FROM content_session s
            LEFT JOIN article a ON a.session_id = s.id
            ORDER BY s.created_at DESC LIMIT 100
            """
        )

    def get_session(self, session_id: str) -> dict[str, Any]:
        session = self.db.query_one("SELECT * FROM content_session WHERE id = ?", (session_id,))
        if session is None:
            raise ServiceError("Session not found", 404)
        assets = self.db.query_all(
            "SELECT * FROM session_asset WHERE session_id = ? ORDER BY created_at", (session_id,)
        )
        articles = self.db.query_all(
            "SELECT id,title,summary,status,current_version_id,created_at,updated_at FROM article WHERE session_id = ? ORDER BY created_at DESC",
            (session_id,),
        )
        workflows = self.db.query_all(
            "SELECT * FROM workflow_run WHERE session_id = ? ORDER BY started_at DESC", (session_id,)
        )
        publish_jobs = self.db.query_all(
            """
            SELECT p.* FROM publish_job p
            JOIN article a ON a.id=p.article_id
            WHERE a.session_id = ?
            ORDER BY p.created_at DESC
            """,
            (session_id,),
        )
        brief = self._session_brief(session)
        session = dict(session)
        session["proposal"] = brief.get("proposal")
        session["proposal_status"] = brief.get("proposal_status")
        session["typesetting_id"] = brief.get("typesetting_id")
        return {
            "session": session,
            "assets": assets,
            "articles": articles,
            "workflows": workflows,
            "publish_jobs": publish_jobs,
        }

    def upload_asset(self, session_id: str, file_name: str, mime_type: str, payload: bytes, article_id: str | None = None) -> dict[str, Any]:
        if self.db.query_one("SELECT id FROM content_session WHERE id = ?", (session_id,)) is None:
            raise ServiceError("Session not found", 404)
        if article_id:
            article = self.db.query_one("SELECT id,session_id FROM article WHERE id = ?", (article_id,))
            if article is None or article["session_id"] != session_id:
                raise ServiceError("article_id 不属于该会话", 400)
        try:
            asset = save_asset(
                self.db,
                self.settings.storage_path,
                session_id,
                file_name,
                mime_type,
                payload,
                self.settings.max_upload_bytes,
                article_id,
            )
        except ValueError as exc:
            raise ServiceError(str(exc), 400) from exc
        self.audit("asset.upload", "asset", asset["id"], {"file_name": asset["file_name"], "status": asset["processing_status"]})
        return asset

    def list_all_assets(self) -> list[dict[str, Any]]:
        """全量素材（带公众号/会话/文章归属），供素材库按 账号→对话 分组展示。"""
        rows = self.db.query_all(
            """
            SELECT a.id, a.session_id, a.article_id, a.asset_type, a.file_name, a.mime_type,
                   a.size_bytes, a.processing_status, a.extracted_text, a.vision_result,
                   a.metadata_json, a.created_at,
                   s.title AS session_title, s.official_account_id,
                   art.title AS article_title,
                   oa.name AS account_name
            FROM session_asset a
            JOIN content_session s ON s.id = a.session_id
            LEFT JOIN article art ON art.id = a.article_id
            LEFT JOIN official_account oa ON oa.id = s.official_account_id
            ORDER BY a.created_at DESC
            """
        )
        return [dict(r) for r in rows]

    def get_asset_file(self, asset_id: str) -> tuple[dict[str, Any], bytes]:
        asset = self.db.query_one("SELECT * FROM session_asset WHERE id = ?", (asset_id,))
        if asset is None:
            raise ServiceError("Asset not found", 404)
        return asset, load_asset_payload(self.settings.storage_path, asset["storage_key"])

    # ------------------------------------------------------------------
    # 视觉分析配置：默认原生多模态（图片直接进入对话上下文，连贯性优于外挂
    # VidLens 子进程）；VidLens 作为可选回退。数据库设置优先于环境变量。
    # ------------------------------------------------------------------
    VISION_SETTING_KEY = "vision"
    VISION_SETUP_HINT = (
        "视觉分析尚未配置：默认使用原生多模态模型，需要在"
        "「用户权限旁的 设置 → 视觉分析」中填写 API 地址、API Key 与模型名"
        "（或设置 WANREN_VISION_API_BASE / WANREN_VISION_API_KEY / WANREN_VISION_MODEL）。"
        "也可以把视觉提供商切换为 VidLens（需要可用的 WANREN_VIDLENS 路径）。"
    )

    def _vision_config(self) -> dict[str, Any]:
        stored = self.db.get_setting(self.VISION_SETTING_KEY) or {}
        provider = str(stored.get("provider") or self.settings.vision_provider).lower()
        if provider not in {"multimodal", "vidlens"}:
            provider = "multimodal"
        api_base = str(stored.get("api_base") or self.settings.vision_api_base).rstrip("/")
        api_key = decrypt_value(str(stored.get("api_key_encrypted") or "")) or self.settings.vision_api_key
        model = str(stored.get("model") or self.settings.vision_model)
        mode = "external" if (api_base and model) else "main"
        if provider == "multimodal" and mode == "main":
            # 「跟随主模型」：主模型是 OpenAI-compatible 时直接复用其配置看图（DB 覆盖优先）
            text_config = self._text_config()
            if text_config["provider"] == "openai_compatible":
                api_base = text_config["api_base"]
                api_key = text_config["api_key"]
                model = text_config["model"]
        return {
            "provider": provider,
            "mode": mode,
            "api_base": api_base,
            "api_key": api_key,
            "model": model,
            "configured": bool(api_base and api_key and model),
            "vidlens_available": self.settings.vidlens_path.is_file(),
        }

    VIDLens_SETTING_KEY = "vidlens"
    VIDLens_ENV_MAP = {"api_url": "VIDLENS_API_URL", "api_key": "VIDLENS_API_KEY", "model": "VIDLENS_MODEL"}

    def _vidlens_config(self) -> dict[str, str]:
        """VidLens 内部厂家配置：DB（设置页保存）> VIDLENS_* 环境变量。"""
        stored = self.db.get_setting(self.VIDLens_SETTING_KEY) or {}
        cfg: dict[str, str] = {}
        for field, env_name in self.VIDLens_ENV_MAP.items():
            if field == "api_key":
                encrypted = str(stored.get("api_key_encrypted") or "").strip()
                value = decrypt_value(encrypted) if encrypted else ""
            else:
                value = str(stored.get(field) or "").strip()
            cfg[field] = value or os.environ.get(env_name, "")
        cfg["configured"] = bool(cfg["api_url"] and cfg["api_key"] and cfg["model"])
        cfg["source"] = "db" if str(stored.get("api_url") or "").strip() else ("env" if os.environ.get("VIDLENS_API_URL") else "none")
        return cfg

    def _vidlens_env_overlay(self) -> dict[str, str]:
        """subprocess 注入用：DB 里配了的部分覆盖环境变量，未配的交给 VidLens 自身（env/config.yaml）。"""
        stored = self.db.get_setting(self.VIDLens_SETTING_KEY) or {}
        overlay: dict[str, str] = {}
        for field, env_name in self.VIDLens_ENV_MAP.items():
            if field == "api_key":
                encrypted = str(stored.get("api_key_encrypted") or "").strip()
                value = decrypt_value(encrypted) if encrypted else ""
            else:
                value = str(stored.get(field) or "").strip()
            if value:
                overlay[env_name] = value
        return overlay

    def _test_vidlens_connection(self) -> dict[str, Any]:
        """检测 VidLens 集成：脚本存在 → 内部厂家配置（DB > env）→ 实测该厂家连通。"""
        script = self.settings.vidlens_path
        if not script.is_file():
            return {"ok": False, "message": f"VidLens 脚本不存在：{script}。可设置 WANREN_VIDLENS 指向脚本位置。"}
        cfg = self._vidlens_config()
        url, key, model = cfg["api_url"], cfg["api_key"], cfg["model"]
        missing = [n for n, v in (("api_url", url), ("api_key", key), ("model_name", model)) if not v]
        if missing:
            return {
                "ok": False,
                "message": (
                    "VidLens 内部视觉模型未配置（缺 "
                    + "、".join(missing)
                    + "）。请在下方「VidLens 内部视觉模型」中填写（VidLens 可使用与主模型不同的厂家）并保存，"
                    + "或设置环境变量 VIDLENS_API_URL / VIDLENS_API_KEY / VIDLENS_MODEL。"
                ),
            }
        base = url.rstrip("/")  # 与 VidLens 内部拼接方式一致（vidlens.py:625），不加任何后缀猜测
        started = time.perf_counter()
        try:
            probe = httpx.get(base + "/models", headers={"Authorization": "Bearer " + key}, timeout=10)
        except httpx.HTTPError as exc:
            return {"ok": False, "message": f"VidLens 厂家（{url}）连接失败：{exc}"}
        latency = int((time.perf_counter() - started) * 1000)
        if probe.status_code != 200:
            return {"ok": False, "message": f"VidLens 厂家返回 HTTP {probe.status_code}：{probe.text[:180]}"}
        models_list = []
        try:
            models_list = [m.get("id", "") for m in probe.json().get("data", []) if isinstance(m, dict)]
        except (ValueError, TypeError):
            pass
        suffix = f"，模型 {model} 在列表中" if model in models_list else (f"，列表含 {len(models_list)} 个模型" if models_list else "")
        return {"ok": True, "message": f"VidLens 连接成功（{latency}ms，{url}{suffix}）"}

    TEXT_SETTING_KEY = "text_model"

    def _text_config(self) -> dict[str, Any]:
        """主文本模型配置：DB 覆盖 > 环境变量（与视觉一致的两层来源）。"""
        stored = self.db.get_setting(self.TEXT_SETTING_KEY) or {}
        provider = str(stored.get("provider") or self.settings.text_provider).lower()
        if provider not in {"local", "openai_compatible"}:
            provider = self.settings.text_provider
        api_base = str(stored.get("api_base") or self.settings.text_api_base).rstrip("/")
        api_key = decrypt_value(str(stored.get("api_key_encrypted") or "")) or self.settings.text_api_key
        model = str(stored.get("model") or self.settings.text_model)
        return {
            "provider": provider,
            "api_base": api_base,
            "api_key": api_key,
            "model": model,
            "configured": provider == "local" or bool(api_base and api_key and model),
            "env_fallback": {
                "api_base": bool(self.settings.text_api_base),
                "api_key": bool(self.settings.text_api_key),
                "model": bool(self.settings.text_model),
            },
        }

    def get_text_settings(self) -> dict[str, Any]:
        config = self._text_config()
        stored = self.db.get_setting(self.TEXT_SETTING_KEY) or {}
        has_stored_key = bool(stored.get("api_key_encrypted"))
        return {
            **{k: v for k, v in config.items() if k != "api_key"},
            "api_key_masked": mask_key(config["api_key"]) if (config["api_key"] or has_stored_key) else "",
        }

    def update_text_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        provider = str(payload.get("provider") or "local").lower()
        if provider not in {"local", "openai_compatible"}:
            raise ServiceError("主文本模型提供商必须是 local 或 openai_compatible", 400)
        current = self.db.get_setting(self.TEXT_SETTING_KEY) or {}
        api_key_raw = str(payload.get("api_key") or "")
        clearing = not str(payload.get("api_base") or "").strip() and not str(payload.get("model") or "").strip() and not api_key_raw
        stored = {
            "provider": provider,
            "api_base": str(payload.get("api_base") or "").strip(),
            "model": str(payload.get("model") or "").strip(),
            "api_key_encrypted": encrypt_value(api_key_raw) if api_key_raw else ("" if clearing else str(current.get("api_key_encrypted") or "")),
        }
        self.db.set_setting(self.TEXT_SETTING_KEY, stored)
        self._rebuild_text_adapter()
        self.audit("settings.text_model.update", "settings", "text_model", {"provider": provider, "has_key": bool(stored["api_key_encrypted"])})
        return self.get_text_settings()

    def test_connection(self, target: str) -> dict[str, Any]:
        """检测视觉/主文本/VidLens 连通性（服务端代理发起，密钥不离开服务端）。"""
        if target == "text":
            text_config = self._text_config()
            if text_config["provider"] == "local":
                return {"ok": True, "message": "本地适配器：离线可用，无需外部连接"}
            base, key, model = text_config["api_base"], text_config["api_key"], text_config["model"]
            label = "主文本模型"
        elif target == "vidlens":
            return self._test_vidlens_connection()
        elif target == "vision":
            config = self._vision_config()
            if not config["configured"]:
                return {"ok": False, "message": self.VISION_SETUP_HINT}
            if config["provider"] == "vidlens":
                available = config["vidlens_available"]
                return {"ok": available, "message": ("VidLens 脚本存在：" + str(self.settings.vidlens_path)) if available else ("未找到 VidLens 脚本：" + str(self.settings.vidlens_path))}
            base, key, model = config["api_base"], config["api_key"], config["model"]
            label = "视觉模型"
        else:
            raise ServiceError("target 必须是 vision 或 text", 400)
        started = time.perf_counter()
        # 优先用轻量 GET /models（纯路由验证，<100ms）；不支持时回退 chat ping
        try:
            probe = httpx.get(base + "/models", headers={"Authorization": "Bearer " + key}, timeout=10)
            if probe.status_code == 200:
                latency = int((time.perf_counter() - started) * 1000)
                models_list = []
                try:
                    payload = probe.json()
                    models_list = [m.get("id", "") for m in payload.get("data", []) if isinstance(m, dict)]
                except (ValueError, TypeError):
                    pass
                model_found = model in models_list if models_list else None
                suffix = f"，模型 {model} 在列表中" if model_found else (f"，列表含 {len(models_list)} 个模型" if models_list else "")
                return {"ok": True, "message": f"{label}连接成功（{latency}ms{suffix}）"}
        except httpx.HTTPError:
            pass  # /models 不可用，走完整 ping
        # 回退：真实生成 1 个 token 验证模型可用
        try:
            response = httpx.post(
                base + "/chat/completions",
                headers={"Authorization": "Bearer " + key},
                json={"model": model, "max_tokens": 1, "temperature": 0, "messages": [{"role": "user", "content": "hi"}]},
                timeout=30,
            )
        except httpx.HTTPError as exc:
            return {"ok": False, "message": f"{label}连接失败：{exc}"}
        latency = int((time.perf_counter() - started) * 1000)
        if response.status_code != 200:
            detail = response.text[:220]
            return {"ok": False, "message": f"{label}返回 HTTP {response.status_code}：{detail}"}
        try:
            response.json()
            return {"ok": True, "message": f"{label}连接成功（{latency}ms，模型 {model}）"}
        except ValueError:
            return {"ok": False, "message": f"{label}返回了非 JSON 响应（{latency}ms）"}

    def get_vision_settings(self) -> dict[str, Any]:
        config = self._vision_config()
        stored = self.db.get_setting(self.VISION_SETTING_KEY) or {}
        has_stored_key = bool(stored.get("api_key_encrypted"))
        return {
            "provider": config["provider"],
            "api_base": config["api_base"],
            "api_key_masked": mask_key(config["api_key"]) if (config["api_key"] or has_stored_key) else "",
            "model": config["model"],
            "configured": config["configured"],
            "vidlens_available": config["vidlens_available"],
            "vidlens_config": self._vidlens_public_config(),
            "text_provider": self._text_config()["provider"],
            "text_config": self.get_text_settings(),
            "env_fallback": {
                "api_base": bool(self.settings.vision_api_base),
                "api_key": bool(self.settings.vision_api_key),
                "model": bool(self.settings.vision_model),
            },
            "hint": None if config["provider"] == "vidlens" or config["configured"] else self.VISION_SETUP_HINT,
        }

    def _vidlens_public_config(self) -> dict[str, Any]:
        cfg = self._vidlens_config()
        return {
            "api_url": cfg["api_url"],
            "model": cfg["model"],
            "api_key_masked": mask_key(cfg["api_key"]) if cfg["api_key"] else "",
            "configured": cfg["configured"],
            "source": cfg["source"],
        }

    def update_vision_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        provider = str(payload.get("provider") or "multimodal").lower()
        if provider not in {"multimodal", "vidlens"}:
            raise ServiceError("视觉提供商必须是 multimodal 或 vidlens", 400)
        current = self.db.get_setting(self.VISION_SETTING_KEY) or {}
        api_key_raw = str(payload.get("api_key") or "")
        clearing = not str(payload.get("api_base") or "").strip() and not str(payload.get("model") or "").strip() and not api_key_raw
        stored = {
            "provider": provider,
            "api_base": str(payload.get("api_base") or "").strip(),
            "model": str(payload.get("model") or "").strip(),
            "api_key_encrypted": encrypt_value(api_key_raw) if api_key_raw else ("" if clearing else str(current.get("api_key_encrypted") or "")),
        }
        self.db.set_setting(self.VISION_SETTING_KEY, stored)
        # VidLens 内部厂家配置（可与主模型不同厂家；只在请求带了字段时更新）
        vl_url = payload.get("vidlens_api_url")
        vl_model = payload.get("vidlens_model")
        vl_key = payload.get("vidlens_api_key")
        if vl_url is not None or vl_model is not None or vl_key is not None:
            vl_current = self.db.get_setting(self.VIDLens_SETTING_KEY) or {}
            vl_key_raw = str(vl_key or "")
            vl_clearing = not str(vl_url or "").strip() and not str(vl_model or "").strip() and not vl_key_raw
            self.db.set_setting(self.VIDLens_SETTING_KEY, {
                "api_url": str(vl_url).strip() if vl_url is not None else str(vl_current.get("api_url") or ""),
                "model": str(vl_model).strip() if vl_model is not None else str(vl_current.get("model") or ""),
                "api_key_encrypted": encrypt_value(vl_key_raw) if vl_key_raw else ("" if vl_clearing else str(vl_current.get("api_key_encrypted") or "")),
            })
            self.audit("settings.vidlens.update", "settings", "vidlens", {"source": "db"})
        self.audit("settings.vision.update", "settings", "vision", {"provider": provider, "has_key": bool(stored["api_key_encrypted"])})
        return self.get_vision_settings()

    def _analyze_with_multimodal(self, path: Path, task: str) -> dict[str, Any]:
        config = self._vision_config()
        if not config["configured"]:
            raise ServiceError(self.VISION_SETUP_HINT, 400)
        mime = "image/png"
        if path.suffix.lower() in {".jpg", ".jpeg"}:
            mime = "image/jpeg"
        elif path.suffix.lower() == ".webp":
            mime = "image/webp"
        data_uri = "data:" + mime + ";base64," + base64.b64encode(path.read_bytes()).decode("ascii")
        body = {
            "model": config["model"],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "你是图片分析助手，只输出合法 JSON，不输出 Markdown。"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": task},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                },
            ],
        }
        last_error = ""
        for _ in range(2):
            try:
                response = httpx.post(
                    config["api_base"] + "/chat/completions",
                    headers={"Authorization": "Bearer " + config["api_key"]},
                    json=body,
                    timeout=120,
                )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                raw = str(content).strip()
                start, end = raw.find("{"), raw.rfind("}")
                if start >= 0 and end > start:
                    try:
                        return json.loads(raw[start : end + 1])
                    except json.JSONDecodeError:
                        pass
                last_error = "模型未返回合法 JSON：" + raw[:200]
            except (httpx.HTTPError, RuntimeError, KeyError, IndexError, ValueError) as exc:
                last_error = str(exc)
        raise ServiceError(
            "多模态视觉分析失败：" + last_error + "。请检查设置中的 API 地址 / API Key / 模型名是否正确，"
            "以及该模型是否支持图像输入（不支持图片的纯文本模型会返回此类错误）；也可切换为 VidLens。",
            502,
        )

    def analyze_asset(self, session_id: str, asset_id: str, prompt: str | None) -> dict[str, Any]:
        asset, payload = self.get_asset_file(asset_id)
        if asset["session_id"] != session_id:
            raise ServiceError("Asset does not belong to this session", 403)
        if asset["asset_type"] not in {"IMAGE", "VIDEO"}:
            raise ServiceError("Only image and video assets can be analyzed")
        task = prompt or (
            "请只返回一个合法 JSON 对象，字段包括 description、objects、scene、quality_score、"
            "recommended_usage、risk_flags。不要输出 Markdown。"
        )
        path = self.settings.storage_path / "assets" / asset["storage_key"]
        analysis_source = asset["asset_type"]
        if asset["asset_type"] == "VIDEO":
            metadata = json.loads(asset["metadata_json"] or "{}")
            thumbnail_id = metadata.get("thumbnail_asset_id")
            thumbnail = (
                self.db.query_one("SELECT * FROM session_asset WHERE id = ?", (thumbnail_id,))
                if thumbnail_id
                else None
            )
            if thumbnail is not None:
                path = self.settings.storage_path / "assets" / thumbnail["storage_key"]
                analysis_source = "derived_video_cover"
        vision_config = self._vision_config()
        if vision_config["provider"] == "multimodal":
            vision = self._analyze_with_multimodal(path, task)
            provider_name = "multimodal"
        else:
            command = [sys.executable, str(self.settings.vidlens_path), str(path), "--task", task]
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=180,
                    check=False,
                    env={**os.environ, **self._vidlens_env_overlay()},
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ServiceError(f"VidLens execution failed: {exc}", 502) from exc
            if completed.returncode != 0:
                raise ServiceError(f"VidLens failed: {completed.stderr.strip() or completed.stdout.strip()}", 502)
            raw = completed.stdout.strip()
            start, end = raw.find("{"), raw.rfind("}")
            if start < 0 or end <= start:
                vision = {"raw": raw[:10000]}
            else:
                try:
                    vision = json.loads(raw[start : end + 1])
                except json.JSONDecodeError:
                    vision = {"raw": raw[:10000]}
            provider_name = "vidlens"
        with self.db.tx():
            self.db.execute(
            "UPDATE session_asset SET vision_result = ?, processing_status = ?, metadata_json = ?, updated_at = ? WHERE id = ?",
            (
                clean_unicode(json.dumps(vision, ensure_ascii=False)),
                "VISION_READY",
                clean_unicode(json.dumps({**json.loads(asset["metadata_json"] or "{}"), "vision_analysis_source": analysis_source}, ensure_ascii=False)),
                utc_now(),
                asset_id,
            ),
        )
        self.audit("asset.vision", "asset", asset_id, {"provider": provider_name, "source": analysis_source})
        result = self.db.query_one("SELECT * FROM session_asset WHERE id = ?", (asset_id,))
        assert result is not None
        return result

    def _start_workflow(self, session_id: str, workflow_type: str, status: str = "RUNNING") -> str:
        run_id = new_id("run")
        with self.db.tx():
            self.db.execute(
                """
                INSERT INTO workflow_run(id,session_id,workflow_type,status,current_node,started_at)
                VALUES (?,?,?,?,?,?)
                """,
                (run_id, session_id, workflow_type, status, "START", utc_now()),
            )
        return run_id

    def _node(self, run_id: str, node_key: str, handler) -> Any:
        node_id = new_id("node")
        started = time.perf_counter()
        now = utc_now()
        with self.db.tx():
            self.db.execute(
                """
                INSERT INTO workflow_node_run
                (id,workflow_run_id,node_key,status,started_at)
                VALUES (?,?,?,?,?)
                """,
                (node_id, run_id, node_key, "RUNNING", now),
            )
            self.db.execute("UPDATE workflow_run SET current_node = ? WHERE id = ?", (node_key, run_id))
        try:
            output = handler()
            with self.db.tx():
                self.db.execute(
                    "UPDATE workflow_node_run SET status='SUCCESS',duration_ms=?,finished_at=? WHERE id=?",
                    (int((time.perf_counter() - started) * 1000), utc_now(), node_id),
                )

            return output
        except Exception as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
            with self.db.tx():
                self.db.execute(
                    "UPDATE workflow_node_run SET status='FAILED',duration_ms=?,finished_at=?,error=? WHERE id=?",
                    (int((time.perf_counter() - started) * 1000), utc_now(), json.dumps(error, ensure_ascii=False), node_id),
                )
                self.db.execute(
                    "UPDATE workflow_run SET status='FAILED',finished_at=?,error=? WHERE id=?",
                    (utc_now(), json.dumps(error, ensure_ascii=False), run_id),
                )
            raise

    def _session_brief(self, session: dict[str, Any]) -> dict[str, Any]:
        try:
            brief = json.loads(session.get("brief") or "{}")
        except (json.JSONDecodeError, TypeError):
            brief = {}
        return brief if isinstance(brief, dict) else {}

    def propose_architecture(self, session_id: str, instruction: str, feedback: str | None = None) -> dict[str, Any]:
        """第一次使用（或用户提出修改意见）时：大模型先给出文章架构提案，等用户确认。"""
        session = self.db.query_one("SELECT * FROM content_session WHERE id = ?", (session_id,))
        if session is None:
            raise ServiceError("Session not found", 404)
        context = build_session_context(self.db, session_id)
        self._enforce_ai_budget(
            self.text_adapter.provider,
            self.settings.text_model or self.text_adapter.name,
        )
        brief = self._session_brief(session)
        previous = brief.get("proposal") if brief.get("proposal_status") == "AWAITING" else None
        if previous is not None and feedback is None:
            # 已有待确认提案时，本次输入视为对提案的修改意见
            feedback = instruction
        propose_fn = getattr(self.text_adapter, "propose", None)
        if propose_fn is None:
            raise ServiceError("当前文本适配器不支持架构提案", 400)
        proposal = propose_fn(context, instruction, previous=previous, feedback=feedback)
        if not isinstance(proposal, dict) or not proposal.get("sections"):
            raise ServiceError("模型返回的架构提案无效，请重试", 502)
        typesetting = proposal.get("typesetting") if isinstance(proposal.get("typesetting"), dict) else {}
        style = TYPESETTING_STYLES.get(str(typesetting.get("style_id", "")))
        now = utc_now()
        brief["proposal"] = proposal
        brief["proposal_status"] = "AWAITING"
        with self.db.tx():
            self.db.execute(
                "UPDATE content_session SET brief=?, updated_at=? WHERE id=?",
                (json.dumps(brief, ensure_ascii=False), now, session_id),
            )
            self.db.execute(
                "INSERT INTO session_message(id,session_id,role,content,created_at) VALUES (?,?,?,?,?)",
                (new_id("msg"), session_id, "USER", str(feedback or instruction or "请给出这篇文章的写作架构"), now),
            )
            self.db.execute(
                "INSERT INTO session_message(id,session_id,role,content,created_at) VALUES (?,?,?,?,?)",
                (
                    new_id("msg"),
                    session_id,
                    "ASSISTANT",
                    "架构提案：《{}》 · {} 节 · 样式：{}。请确认后开始写作，或直接回复修改意见。".format(
                        proposal.get("title", ""),
                        len(proposal.get("sections", [])),
                        style["name"] if style else "默认",
                    ),
                    now,
                ),
            )
        self.audit("session.propose", "content_session", session_id, {"sections": len(proposal.get("sections", []))})
        return {"proposal": proposal, "proposal_status": "AWAITING"}

    def confirm_proposal(self, session_id: str, style_id: str | None = None) -> dict[str, Any]:
        """用户确认架构提案（可顺带改选样式模版），确认后才能生成正文。"""
        session = self.db.query_one("SELECT * FROM content_session WHERE id = ?", (session_id,))
        if session is None:
            raise ServiceError("Session not found", 404)
        brief = self._session_brief(session)
        proposal = brief.get("proposal")
        if not isinstance(proposal, dict) or not proposal.get("sections"):
            raise ServiceError("还没有架构提案：先发送写作需求，让 AI 先出提案", 409)
        style_map = self._style_map()
        chosen = style_id if style_id in style_map else None
        if chosen is None:
            typesetting = proposal.get("typesetting") if isinstance(proposal.get("typesetting"), dict) else {}
            chosen = str(typesetting.get("style_id", ""))
        if chosen not in style_map:
            chosen = "mono-minimal"
        chosen_name = str(style_map[chosen].get("name", ""))
        brief["proposal_status"] = "CONFIRMED"
        brief["typesetting_id"] = chosen
        # 确认的大纲即本会话的内容架构（复用 template 通道进入生成管线，不再依赖预设模板）
        brief["template"] = {
            "structure": [
                {
                    "id": str(section.get("id") or f"sec_{index:02d}")[:60],
                    "heading": str(section.get("heading", "")),
                    "guidance": str(section.get("plan", "")),
                    "image_policy": "optional",
                }
                for index, section in enumerate(proposal.get("sections", []), start=1)
            ],
            "style": {},
        }
        now = utc_now()
        with self.db.tx():
            self.db.execute(
                "UPDATE content_session SET brief=?, updated_at=? WHERE id=?",
                (json.dumps(brief, ensure_ascii=False), now, session_id),
            )
            self.db.execute(
                "INSERT INTO session_message(id,session_id,role,content,created_at) VALUES (?,?,?,?,?)",
                (
                    new_id("msg"),
                    session_id,
                    "ASSISTANT",
                    "架构已确认，样式模版：{}。开始写作…".format(chosen_name),
                    now,
                ),
            )
        self.audit("session.confirm_proposal", "content_session", session_id, {"style": chosen})
        return {
            "proposal_status": "CONFIRMED",
            "typesetting_id": chosen,
            "proposal": proposal,
        }

    def cancel_session(self, session_id: str) -> dict[str, Any]:
        """取消尚未生成文章的对话（提案阶段）：连同素材与消息级联删除。"""
        session = self.db.query_one("SELECT * FROM content_session WHERE id = ?", (session_id,))
        if session is None:
            raise ServiceError("Session not found", 404)
        articles = self.db.query_all("SELECT id FROM article WHERE session_id = ?", (session_id,))
        if articles:
            raise ServiceError("对话已有文章，请从左侧对话树中删除", 409)
        with self.db.tx():
            self.db.execute("DELETE FROM content_session WHERE id = ?", (session_id,))
        self.audit("session.cancel", "content_session", session_id, {})
        return {"ok": True, "session_id": session_id}

    def _require_proposal_confirmed(self, session_id: str) -> None:
        row = self.db.query_one("SELECT brief FROM content_session WHERE id = ?", (session_id,))
        if row is None:
            raise ServiceError("Session not found", 404)
        brief = self._session_brief(row)
        if brief.get("proposal_status") == "AWAITING":
            raise ServiceError("架构提案待确认：请在对话框中确认或修改提案后再生成", 409)

    # ------------------------------------------------- 样式模版（含导入） ----

    def _custom_style_rows(self) -> list[dict[str, Any]]:
        return self.db.query_all(
            "SELECT * FROM typesetting_style WHERE workspace_id='ws_default' ORDER BY created_at"
        )

    def _style_map(self) -> dict[str, dict[str, Any]]:
        """内置 + 自定义样式的并集（id → 完整样式字典）。"""
        styles: dict[str, dict[str, Any]] = {sid: dict(style) for sid, style in TYPESETTING_STYLES.items()}
        for row in self._custom_style_rows():
            try:
                tokens = validate_style_tokens(json.loads(row["tokens_json"]))
            except (json.JSONDecodeError, ValueError):
                continue
            styles[row["id"]] = {**tokens, "id": row["id"], "name": row["name"]}
        return styles

    def _style_card(self, style: dict[str, Any], builtin: bool) -> dict[str, Any]:
        tokens = {
            key: style.get(key)
            for key in (
                "page_bg", "skeleton", "font", "text", "title", "accent",
                "accent_soft", "muted", "border", "sizes", "line_height",
                "radius", "header", "section_heading", "quote", "end",
            )
            if style.get(key) is not None
        }
        return {
            "id": style["id"],
            "name": style.get("name", ""),
            "vibe": str(style.get("vibe", "")),
            "suits": str(style.get("suits", "")),
            "page_bg": style["page_bg"],
            "accent": style["accent"],
            "builtin": builtin,
            "tokens": tokens,
        }

    def list_typesetting_styles(self) -> list[dict[str, Any]]:
        cards = [self._style_card(style, True) for style in TYPESETTING_STYLES.values()]
        for row in self._custom_style_rows():
            try:
                tokens = validate_style_tokens(json.loads(row["tokens_json"]))
            except (json.JSONDecodeError, ValueError):
                continue
            cards.append(
                self._style_card(
                    {**tokens, "id": row["id"], "name": row["name"], "vibe": row["vibe"], "suits": row["suits"]},
                    False,
                )
            )
        return cards

    def import_typesetting_style(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        if not (2 <= len(name) <= 40):
            raise ServiceError("样式名称需要 2-40 个字符", 400)
        try:
            tokens = validate_style_tokens(payload.get("tokens") or payload)
        except ValueError as exc:
            raise ServiceError(f"样式令牌无效：{exc}", 400) from exc
        duplicate = self.db.query_one(
            "SELECT id FROM typesetting_style WHERE workspace_id='ws_default' AND name=?",
            (name,),
        )
        if duplicate is not None:
            raise ServiceError(f"已存在同名样式：{name}", 409)
        if any(style.get("name") == name for style in TYPESETTING_STYLES.values()):
            raise ServiceError(f"与内置样式重名：{name}", 409)
        style_id = new_id("style")
        with self.db.tx():
            self.db.execute(
                """
                INSERT INTO typesetting_style
                (id,workspace_id,name,vibe,suits,tokens_json,created_at)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    style_id,
                    "ws_default",
                    name,
                    str(payload.get("vibe") or "")[:60],
                    str(payload.get("suits") or "")[:120],
                    json.dumps(tokens, ensure_ascii=False),
                    utc_now(),
                ),
            )
        self.audit("typesetting.import", "typesetting_style", style_id, {"name": name})
        return {"style": self._style_card({**tokens, "id": style_id, "name": name}, False)}

    def delete_typesetting_style(self, style_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM typesetting_style WHERE id = ?", (style_id,))
        if row is None:
            raise ServiceError("样式不存在或为内置样式（内置不可删除）", 404)
        with self.db.tx():
            self.db.execute("DELETE FROM typesetting_style WHERE id = ?", (style_id,))
        self.audit("typesetting.delete", "typesetting_style", style_id, {"name": row["name"]})
        return {"ok": True}

    def export_typesetting_style(self, style_id: str) -> dict[str, Any]:
        style = self._style_map().get(style_id)
        if style is None:
            raise ServiceError("样式不存在", 404)
        tokens = {k: v for k, v in style.items() if k not in {"id", "name"}}
        return {"name": style.get("name", ""), "vibe": style.get("vibe", ""), "suits": style.get("suits", ""), "tokens": tokens}

    def preview_typesetting_style(self, style_id: str, sample_id: str | None = None) -> dict[str, Any]:
        style = self._style_map().get(style_id)
        if style is None:
            raise ServiceError("样式不存在", 404)
        from .typesetting import DEMO_SAMPLES

        sample = DEMO_SAMPLES.get(sample_id or "", DEMO_SAMPLES["opinion"])
        return {
            "html": demo_preview_html(style, sample_id),
            "name": style.get("name", ""),
            "sample": {"id": next(k for k, v in DEMO_SAMPLES.items() if v is sample), "name": sample["name"]},
            "samples": [{"id": key, "name": value["name"]} for key, value in DEMO_SAMPLES.items()],
        }

    def _resolved_style_for(self, typesetting_id: str | None, account_settings: dict[str, Any]) -> dict[str, Any]:
        if typesetting_id:
            style = self._style_map().get(typesetting_id)
            if style is not None:
                return style
        return account_style_fallback(account_settings)

    def render_article_preview(self, article_id: str, style_id: str | None = None) -> dict[str, Any]:
        """按样式渲染当前文章的公众号成品 HTML（编辑器「公众号预览」所见）。"""
        article = self._get_article(article_id)
        content = json.loads(article["content_json"])
        assets = self.db.query_all(
            "SELECT * FROM session_asset WHERE session_id = ? ORDER BY created_at",
            (article["session_id"],),
        )
        urls = {
            str(asset["id"]): f"/api/assets/{asset['id']}/file"
            for asset in assets
            if asset.get("asset_type") == "IMAGE"
        }
        session = self.db.query_one(
            "SELECT official_account_id FROM content_session WHERE id = ?", (article["session_id"],)
        )
        account = (
            self.db.query_one(
                "SELECT * FROM official_account WHERE id = ?", (session["official_account_id"],)
            )
            if session
            else None
        )
        account_settings = json.loads(account["settings"] or "{}") if account else {}
        style_map = self._style_map()
        chosen = style_id if style_id in style_map else (
            article["typesetting_id"] if "typesetting_id" in article.keys() and article["typesetting_id"] in style_map else None
        )
        style = self._resolved_style_for(chosen, account_settings)
        return {
            "html": render_article_html_with_style(content, assets, urls, style),
            "name": str(style.get("name", "")),
            "style_id": chosen or "",
        }


    def generate_article(self, session_id: str, instruction: str) -> dict[str, Any]:
        if self.db.query_one("SELECT id FROM content_session WHERE id = ?", (session_id,)) is None:
            raise ServiceError("Session not found", 404)
        self._require_proposal_confirmed(session_id)
        run_id = self._start_workflow(session_id, "GENERATE_ARTICLE")
        return self._execute_generate_workflow(run_id, session_id, instruction)

    def enqueue_generate_article(self, session_id: str, instruction: str) -> dict[str, Any]:
        if self.db.query_one("SELECT id FROM content_session WHERE id = ?", (session_id,)) is None:
            raise ServiceError("Session not found", 404)
        self._require_proposal_confirmed(session_id)
        run_id = self._start_workflow(session_id, "GENERATE_ARTICLE", "QUEUED")
        now = utc_now()
        with self.db.tx():
            self.db.execute(
                """
                INSERT INTO workflow_queue
                (workflow_run_id,workflow_type,status,payload,attempts,available_at,created_at,updated_at)
                VALUES (?,?,?,?,0,?,?,?)
                """,
                (
                    run_id,
                    "GENERATE_ARTICLE",
                    "QUEUED",
                    json.dumps({"session_id": session_id, "instruction": instruction}, ensure_ascii=False),
                    now,
                    now,
                    now,
                ),
            )
        self.audit("workflow.enqueue", "workflow_run", run_id, {"workflow_type": "GENERATE_ARTICLE"})
        return {"workflow_run_id": run_id, "status": "QUEUED"}

    def enqueue_video_transcode(self, asset_id: str, preset: str) -> dict[str, Any]:
        asset = self.db.query_one("SELECT * FROM session_asset WHERE id = ?", (asset_id,))
        if asset is None:
            raise ServiceError("Asset not found", 404)
        if asset["asset_type"] != "VIDEO":
            raise ServiceError("Only VIDEO assets can be transcoded", 400)
        if asset["processing_status"] not in {"READY", "VISION_READY", "TRANSCODING"}:
            raise ServiceError("Video must be READY before transcoding", 409)
        if preset not in VIDEO_TRANSCODE_PRESETS:
            raise ServiceError("Unsupported video transcode preset", 400)

        active_jobs = self.db.query_all(
            """
            SELECT q.payload
            FROM workflow_queue q
            JOIN workflow_run r ON r.id=q.workflow_run_id
            WHERE q.status IN ('QUEUED','RUNNING')
              AND r.workflow_type='VIDEO_TRANSCODE'
            """
        )
        for job in active_jobs:
            payload = json.loads(job["payload"])
            if payload.get("asset_id") == asset_id and payload.get("preset") == preset:
                return {
                    "workflow_run_id": None,
                    "asset_id": asset_id,
                    "preset": preset,
                    "status": "ALREADY_QUEUED",
                }

        run_id = self._start_workflow(asset["session_id"], "VIDEO_TRANSCODE", "QUEUED")
        now = utc_now()
        payload = {"asset_id": asset_id, "preset": preset}
        with self.db.tx():
            self.db.execute(
                """
                INSERT INTO workflow_queue
                (workflow_run_id,workflow_type,status,payload,attempts,available_at,created_at,updated_at)
                VALUES (?,?,?,?,0,?,?,?)
                """,
                (
                    run_id,
                    "VIDEO_TRANSCODE",
                    "QUEUED",
                    json.dumps(payload, ensure_ascii=False),
                    now,
                    now,
                    now,
                ),
            )
            self.db.execute(
                "UPDATE session_asset SET processing_status='TRANSCODING',updated_at=? WHERE id=?",
                (now, asset_id),
            )
        self.audit("video.transcode.enqueue", "asset", asset_id, {
            "workflow_run_id": run_id,
            "preset": preset,
        })
        return {
            "workflow_run_id": run_id,
            "asset_id": asset_id,
            "preset": preset,
            "status": "QUEUED",
        }

    def run_pending_workflows(self, worker_id: str = "worker-local", limit: int = 1) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for _ in range(max(1, limit)):
            item = self.db.claim_next_workflow(worker_id)
            if item is None:
                break
            run_id = item["workflow_run_id"]
            payload = json.loads(item["payload"])
            try:
                if item["workflow_type"] == "VIDEO_TRANSCODE":
                    output = self._execute_video_transcode_workflow(
                        run_id, payload["asset_id"], payload["preset"]
                    )
                else:
                    output = self._execute_generate_workflow(
                        run_id, payload["session_id"], payload.get("instruction", "")
                    )
                with self.db.tx():
                    self.db.execute(
                        "UPDATE workflow_queue SET status='DONE',updated_at=? WHERE workflow_run_id=?",
                        (utc_now(), run_id),
                    )
                results.append({"workflow_run_id": run_id, "status": "SUCCESS", "output": output})
            except Exception as exc:
                error = {"type": type(exc).__name__, "message": str(exc)}
                with self.db.tx():
                    self.db.execute(
                        "UPDATE workflow_queue SET status='FAILED',updated_at=? WHERE workflow_run_id=?",
                        (utc_now(), run_id),
                    )
                results.append({"workflow_run_id": run_id, "status": "FAILED", "error": error})
        results.extend(self.run_due_publish_jobs(limit=10))
        return results

    def _execute_video_transcode_workflow(
        self, run_id: str, asset_id: str, preset: str
    ) -> dict[str, Any]:
        asset = self.db.query_one("SELECT * FROM session_asset WHERE id = ?", (asset_id,))
        if asset is None:
            raise ServiceError("Asset not found", 404)
        source_path = self.settings.storage_path / "assets" / asset["storage_key"]
        output_path: Path | None = None
        try:
            output_path, metadata = self._node(
                run_id,
                "TRANSCODE_VIDEO",
                lambda: transcode_video_file(source_path, preset),
            )
            derived = self._node(
                run_id,
                "STORE_RENDITION",
                lambda: store_derived_video(
                    self.db,
                    self.settings.storage_path,
                    asset["session_id"],
                    asset,
                    output_path,
                    metadata,
                ),
            )
            with self.db.tx():
                self.db.execute(
                    "UPDATE session_asset SET processing_status='READY',updated_at=? WHERE id=?",
                    (utc_now(), asset_id),
                )
                self.db.execute(
                    """
                    UPDATE workflow_run
                    SET status='SUCCESS',current_node='COMPLETE',finished_at=?
                    WHERE id=?
                    """,
                    (utc_now(), run_id),
                )
            self.audit(
                "video.transcode.complete",
                "asset",
                derived["id"],
                {"source_asset_id": asset_id, "preset": preset},
            )
            return {
                "source_asset_id": asset_id,
                "rendition_asset_id": derived["id"],
                "preset": preset,
                "status": "READY",
            }
        except Exception as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
            metadata = json.loads(asset["metadata_json"] or "{}")
            metadata["last_transcode_error"] = error
            with self.db.tx():
                self.db.execute(
                    "UPDATE session_asset SET processing_status='READY',metadata_json=?,updated_at=? WHERE id=?",
                    (json.dumps(metadata, ensure_ascii=False), utc_now(), asset_id),
                )
            raise
        finally:
            if output_path is not None:
                for _ in range(20):
                    try:
                        output_path.unlink(missing_ok=True)
                        break
                    except PermissionError:
                        time.sleep(0.05)

    def run_due_publish_jobs(self, limit: int = 10) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for job_id in self.db.claim_due_publish_jobs(limit):
            job = self.db.query_one("SELECT * FROM publish_job WHERE id = ?", (job_id,))
            if job is None:
                continue
            try:
                output = asyncio.run(self.publish_article(job["article_id"], job["mode"]))
                results.append({"publish_job_id": job_id, "status": "SUCCESS", "output": output})
            except Exception as exc:
                error = {"type": type(exc).__name__, "message": str(exc)}
                results.append({"publish_job_id": job_id, "status": "FAILED", "error": error})
        return results

    def _execute_generate_workflow(self, run_id: str, session_id: str, instruction: str) -> dict[str, Any]:
        context = None
        generation: GenerationResult | None = None
        try:
            context = self._node(run_id, "UNDERSTAND_CONTEXT", lambda: build_session_context(self.db, session_id))
            self._enforce_ai_budget(
                self.text_adapter.provider,
                self.settings.text_model or self.text_adapter.name,
            )
            generated = self._node(
                run_id,
                "GENERATE_ARTICLE",
                lambda: self.text_adapter.generate(context, instruction),
            )
            generation = generated
            article_result = generated.article
            content = self._node(run_id, "FORMAT_ARTICLE", article_result.to_content_json)
            result = self._node(run_id, "AI_REVIEW", lambda: review_article(content, context.facts))
            article_id = new_id("article")
            version_id = new_id("version")
            now = utc_now()
            article_status = "AI_REVIEWED" if result.decision == "PASS" else "NEEDS_REVISION"
            with self.db.tx():
                self.db.execute(
                    """
                    INSERT INTO article
                    (id,session_id,title,summary,cover_asset_id,content_json,typesetting_id,status,current_version_id,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        article_id,
                        session_id,
                        article_result.title,
                        article_result.summary,
                        next((asset["id"] for asset in context.assets if asset["asset_type"] == "IMAGE"), None),
                        json.dumps(content, ensure_ascii=False),
                        self._session_brief(context.session).get("typesetting_id"),
                        article_status,
                        version_id,
                        now,
                        now,
                    ),
                )
                self.db.execute(
                    """
                    INSERT INTO article_version
                    (id,article_id,version_no,source,content_json,change_summary,created_at,highlight_json)
                    VALUES (?,?,?,?,?,?,?,'[]')
                    """,
                    (version_id, article_id, 1, "AI", json.dumps(content, ensure_ascii=False), "Initial AI generation", now),
                )
                self.db.execute(
                    """
                    INSERT INTO review
                    (id,article_id,review_type,score,decision,risk_level,result_json,reviewer,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        new_id("review"),
                        article_id,
                        "AI",
                        result.score,
                        result.decision,
                        result.risk_level,
                        json.dumps(review_to_dict(result), ensure_ascii=False),
                        f"{generation.provider}/{generation.model}",
                        now,
                    ),
                )
                self.db.execute(
                    """
                    INSERT INTO ai_run
                    (id,session_id,task_type,provider,model,input_tokens,output_tokens,latency_ms,cost,status,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        new_id("ai"),
                        session_id,
                        "WRITE",
                        generation.provider,
                        generation.model,
                        generation.input_tokens,
                        generation.output_tokens,
                        generation.latency_ms,
                        self._ai_call_cost(
                            generation.provider,
                            generation.model,
                            generation.input_tokens,
                            generation.output_tokens,
                        ),
                        "SUCCESS",
                        now,
                    ),
                )
                self.db.execute(
                    "UPDATE content_session SET status=?, updated_at=? WHERE id=?",
                    ("WAITING_HUMAN", now, session_id),
                )
                self.db.execute(
                    "UPDATE workflow_run SET status='SUCCESS',current_node='WAIT_HUMAN_REVIEW',finished_at=? WHERE id=?",
                    (utc_now(), run_id),
                )
            # Codex 式首轮对话历史（tx 外，避免嵌套提交）：用户指令 + 初稿完成消息（带版本，供回退）
            if instruction.strip():
                self._insert_article_message(article_id, "USER", instruction.strip()[:400], "COMPLETED")
            self._insert_article_message(
                article_id,
                "ASSISTANT",
                f"初稿已生成（第 1 版）：{content.get('title') or '未命名'}",
                "COMPLETED",
                {"version": 1},
            )
            self.audit("article.generate", "article", article_id, {"workflow_run_id": run_id})
            return {"article_id": article_id, "workflow_run_id": run_id, "review": review_to_dict(result)}
        except KeyError as exc:
            raise ServiceError("Session not found", 404) from exc
        except Exception as exc:
            is_budget_error = isinstance(exc, ServiceError) and "budget" in str(exc).lower()
            if (
                context is not None
                and not is_budget_error
                and (isinstance(exc, GenerationError) or generation is None)
            ):
                error = {
                    "type": type(exc).__name__,
                    "message": str(exc),
                }
                provider = getattr(exc, "provider", self.text_adapter.provider)
                model = getattr(exc, "model", self.text_adapter.name)
                input_tokens = getattr(exc, "input_tokens", context.estimated_tokens)
                latency_ms = getattr(exc, "latency_ms", 0)
                with self.db.tx():
                    self.db.execute(
                        """
                        INSERT INTO ai_run
                        (id,session_id,task_type,provider,model,input_tokens,output_tokens,latency_ms,cost,status,created_at)
                        VALUES (?,?,?,?,?,?,0,?,?,?,?)
                        """,
                        (
                            new_id("ai"),
                            session_id,
                            "WRITE",
                            provider,
                            model,
                            input_tokens,
                            latency_ms,
                            self._ai_call_cost(provider, model, input_tokens, 0),
                            "FAILED",
                            utc_now(),
                        ),
                    )
            if isinstance(exc, GenerationError):
                raise ServiceError(str(exc), 502) from exc
            raise

    def _get_article(self, article_id: str) -> dict[str, Any]:
        article = self.db.query_one("SELECT * FROM article WHERE id = ?", (article_id,))
        if article is None:
            raise ServiceError("Article not found", 404)
        return article

    def list_article_messages(self, article_id: str) -> list[dict[str, Any]]:
        self._get_article(article_id)
        return self.db.query_all(
            "SELECT * FROM article_message WHERE article_id=? ORDER BY created_at",
            (article_id,),
        )

    def _insert_article_message(
        self,
        article_id: str,
        role: str,
        content: str,
        status: str = "COMPLETED",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        message = {
            "id": new_id("message"),
            "article_id": article_id,
            "role": role,
            "content": content,
            "status": status,
            "metadata_json": json.dumps(metadata or {}, ensure_ascii=False),
            "created_at": utc_now(),
        }
        self.db.execute(
            """
            INSERT INTO article_message
            (id,article_id,role,content,status,metadata_json,created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                message["id"],
                article_id,
                role,
                content,
                status,
                message["metadata_json"],
                message["created_at"],
            ),
        )
        return message

    def start_article_chat(self, article_id: str, instruction: str, attachments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        article = self._get_article(article_id)
        if not instruction.strip():
            raise ServiceError("请输入修改要求", 400)
        clean_attachments = [
            {"id": str(a.get("id") or ""), "name": str(a.get("name") or ""), "asset_type": str(a.get("asset_type") or "")}
            for a in (attachments or [])
            if a.get("id")
        ]
        with self.db.tx():
            message = self._insert_article_message(
                article_id, "USER", instruction.strip(),
                metadata={"attachments": clean_attachments} if clean_attachments else None,
            )
        self.audit("article.chat", "article", article_id, {"session_title": article["title"]})
        return message

    def revise_article_from_chat(self, article_id: str, instruction: str) -> dict[str, Any]:
        article = self._get_article(article_id)
        context = build_session_context(self.db, article["session_id"])
        current = json.loads(article["content_json"] or "{}")
        self._enforce_ai_budget(
            self.text_adapter.provider,
            self.settings.text_model or self.text_adapter.name,
        )

        if hasattr(self.text_adapter, "revise"):
            generation = self.text_adapter.revise(context, instruction, current)
        else:
            revision_instruction = (
                "请基于当前文章完整 JSON 做增量修订，不要忽略用户这次的要求。"
                f"修改要求：{instruction}\n当前文章 JSON：{json.dumps(current, ensure_ascii=False)}"
            )
            generation = self.text_adapter.generate(context, revision_instruction)

        article_result = generation.article
        content = article_result.to_content_json()
        valid_asset_ids = {asset["id"] for asset in context.assets}
        for section in content.get("sections", []):
            section["asset_refs"] = [
                asset_id for asset_id in section.get("asset_refs", []) if asset_id in valid_asset_ids
            ]
        result = review_article(content, context.facts)
        version_row = self.db.query_one(
            "SELECT MAX(version_no) AS next_version FROM article_version WHERE article_id=?",
            (article_id,),
        )
        next_version = int(version_row["next_version"] or 0) + 1
        version_id = new_id("version")
        now = utc_now()
        status = "AI_REVIEWED" if result.decision == "PASS" else "NEEDS_REVISION"
        reply = (
            f"已完成第 {next_version} 版修订：{article_result.title}。"
            f"AI 预审结果 {result.decision}，评分 {result.score}。"
        )

        highlight_texts = self._diff_added_paragraphs(current, content)
        with self.db.tx():
            self.db.execute(
                """
                INSERT INTO article_version
                (id,article_id,version_no,source,content_json,change_summary,created_at,highlight_json)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    version_id,
                    article_id,
                    next_version,
                    "AI",
                    json.dumps(content, ensure_ascii=False),
                    f"AI 对话优化：{instruction.strip()[:180]}",
                    now,
                    json.dumps(highlight_texts, ensure_ascii=False),
                ),
            )
            self.db.execute(
                """
                UPDATE article
                SET title=?,summary=?,content_json=?,status=?,current_version_id=?,
                    original_declaration_status=NULL,original_declaration_version_id=NULL,
                    original_declaration_confirmed_by=NULL,original_declaration_confirmed_at=NULL,updated_at=?
                WHERE id=?
                """,
                (article_result.title, article_result.summary, json.dumps(content), status, version_id, now, article_id),
            )
            self.db.execute(
                """
                INSERT INTO review
                (id,article_id,review_type,score,decision,risk_level,result_json,reviewer,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    new_id("review"),
                    article_id,
                    "AI",
                    result.score,
                    result.decision,
                    result.risk_level,
                    json.dumps(review_to_dict(result), ensure_ascii=False),
                    f"{generation.provider}/{generation.model}",
                    now,
                ),
            )
            self.db.execute(
                """
                INSERT INTO ai_run
                (id,session_id,task_type,provider,model,input_tokens,output_tokens,latency_ms,cost,status,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    new_id("ai"),
                    article["session_id"],
                    "REVISE",
                    generation.provider,
                    generation.model,
                    generation.input_tokens,
                    generation.output_tokens,
                    generation.latency_ms,
                    self._ai_call_cost(
                        generation.provider,
                        generation.model,
                        generation.input_tokens,
                        generation.output_tokens,
                    ),
                    "SUCCESS",
                    now,
                ),
            )
            assistant = self._insert_article_message(
                article_id,
                "ASSISTANT",
                reply,
                "COMPLETED",
                {
                    "version": next_version,
                    "version_id": version_id,
                    "review": review_to_dict(result),
                    "provider": generation.provider,
                    "model": generation.model,
                    "blocks": highlight_texts,  # 本版改动段落：消息可点击回放高亮
                },
            )
        self.audit("article.chat.revise", "article", article_id, {"version": next_version})
        updated = self._get_article(article_id)
        return {
            "article": updated,
            "version": next_version,
            "review": review_to_dict(result),
            "assistant_message": assistant,
        }

    def fail_article_chat(self, article_id: str, instruction: str, message: str) -> dict[str, Any]:
        self._get_article(article_id)
        with self.db.tx():
            return self._insert_article_message(
                article_id,
                "ASSISTANT",
                f"修订失败：{message}",
                "FAILED",
                {"instruction": instruction},
            )

    def rerun_review(self, article_id: str) -> dict[str, Any]:
        article = self._get_article(article_id)
        session = self.db.query_one("SELECT * FROM content_session WHERE id = ?", (article["session_id"],))
        assert session is not None
        context = build_session_context(self.db, article["session_id"])
        content = json.loads(article["content_json"])
        result = review_article(content, context.facts)
        with self.db.tx():
            self.db.execute(
                """
                INSERT INTO review
                (id,article_id,review_type,score,decision,risk_level,result_json,reviewer,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    new_id("review"),
                    article_id,
                    "AI",
                    result.score,
                    result.decision,
                    result.risk_level,
                    json.dumps(review_to_dict(result), ensure_ascii=False),
                    f"{self.text_adapter.provider}/{self.text_adapter.name}",
                    utc_now(),
                ),
            )
            self.db.execute(
                "UPDATE article SET status=?,updated_at=? WHERE id=?",
                ("AI_REVIEWED" if result.decision == "PASS" else "NEEDS_REVISION", utc_now(), article_id),
            )
        return review_to_dict(result)

    # ------------------------------------------------------------------
    # 协作编辑（assist）：LLM 通过 tool.call 指挥前端编辑器直接增删改块，
    # 服务端编排循环并等待浏览器回传，结束一次性入库（乐观版本 + 重审 +
    # 声明重置全部复用 update_article 管线）。本地适配器自动降级整篇修订。
    # ------------------------------------------------------------------
    ASSIST_MAX_TOOL_CALLS = 12
    ASSIST_TOOL_TIMEOUT = 90.0
    ASSIST_TOOLS = [
        {
            "type": "function",
            "function": {
                "name": "read_article",
                "description": "读取当前文章的标题、摘要与全部小节（含段落与配图引用）",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "insert_blocks",
                "description": "在指定小节的指定段落位置插入一个或多个新段落",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "section_id": {"type": "string"},
                        "index": {"type": "integer", "description": "插入位置（0 表示最前）"},
                        "paragraphs": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    },
                    "required": ["section_id", "index", "paragraphs"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "replace_blocks",
                "description": "替换指定小节 [start, end) 区间内的段落",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "section_id": {"type": "string"},
                        "start": {"type": "integer"},
                        "end": {"type": "integer"},
                        "paragraphs": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    },
                    "required": ["section_id", "start", "end", "paragraphs"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "update_metadata",
                "description": "更新文章标题和/或摘要",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "新标题，32 字以内；不改则省略"},
                        "summary": {"type": "string", "description": "新摘要，120 字以内；不改则省略"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "update_cover",
                "description": "设置文章封面素材",
                "parameters": {
                    "type": "object",
                    "properties": {"asset_id": {"type": "string"}},
                    "required": ["asset_id"],
                },
            },
        },
    ]
    ASSIST_SYSTEM_PROMPT = (
        "你是公众号文章的协作编辑智能体。你通过调用工具让浏览器编辑器直接修改文章："
        "先 read_article 了解现状，再用 insert_blocks / replace_blocks 逐块修改正文，"
        "需要时用 update_metadata 调整标题摘要。每次修改保持段落完整通顺，不要输出 Markdown。"
        "完成全部修改后，直接用自然语言简要说明你做了什么（不再调用工具）。"
    )

    def _session_assets_digest(self, session_id: str, article_id: str | None = None) -> str:
        """拼素材清单给协作编辑模型（默认只取当前对话专属素材）：文件名/类型/内容摘要/图片视觉描述。"""
        rows = self.db.query_all(
            "SELECT id,file_name,asset_type,extracted_text,vision_result FROM session_asset"
            " WHERE session_id=? AND article_id IS ? ORDER BY created_at",
            (session_id, article_id),
        )
        if not rows:
            return ""
        lines = []
        for row in rows:
            parts = [f"id={row['id']}", f"文件名={row['file_name']}", f"类型={row['asset_type']}"]
            text = str(row["extracted_text"] or "").strip()
            if text:
                parts.append("内容摘要=" + text[:1200])
            vision = str(row["vision_result"] or "").strip()
            if vision and vision != "{}":
                try:
                    described = json.loads(vision).get("description")
                    if described:
                        parts.append("图片描述=" + str(described)[:600])
                except ValueError:
                    pass
            lines.append("· " + "；".join(parts))
        return "会话素材（可引用其内容/图片；插入配图时使用对应素材 id）：\n" + "\n".join(lines)

    def _recent_chat_digest(self, article_id: str, current_instruction: str) -> str:
        """最近几轮对话摘要：让模型带着上下文记忆工作（编程工具的会话缓存语义）。"""
        rows = self.db.query_all(
            "SELECT role, content FROM article_message WHERE article_id=? AND role IN ('USER','ASSISTANT')"
            " ORDER BY created_at DESC LIMIT 8",
            (article_id,),
        )
        lines = []
        for row in reversed(rows):
            text = str(row["content"] or "").strip()
            if not text or text == current_instruction.strip():
                continue
            who = "用户" if row["role"] == "USER" else "万仞"
            lines.append(f"{who}：{text[:160]}")
        if not lines:
            return ""
        return "最近对话记录（此前改动的来龙去脉，直接接着做，不要重复已完成的修改）：\n" + "\n".join(lines)

    def _assist_registry(self) -> dict[str, Any]:
        registry = getattr(self, "_assist_sessions", None)
        if registry is None:
            registry = {}
            self._assist_sessions = registry
        return registry

    def assist_tool_result(self, assist_id: str, call_id: str, body: dict[str, Any]) -> dict[str, Any]:
        session = self._assist_registry().get(assist_id)
        if session is None or call_id not in session.get("pending", {}):
            raise ServiceError("Assist tool call not found or already finished", 404)
        slot = session["pending"][call_id]
        raw_blocks = body.get("changed_blocks")
        slot["result"] = {
            "status": str(body.get("status") or "ok"),
            "message": str(body.get("message") or ""),
            "document": body.get("document") if isinstance(body.get("document"), dict) else None,
            "changed_blocks": [str(t)[:600] for t in raw_blocks if str(t).strip()] if isinstance(raw_blocks, list) else [],
        }
        slot["event"].set()
        return {"received": True}

    def _assist_wait(self, session: dict[str, Any], call_id: str) -> dict[str, Any]:
        slot = session["pending"][call_id]
        if not slot["event"].wait(timeout=self.ASSIST_TOOL_TIMEOUT):
            return {"status": "timeout", "message": "浏览器未在超时内回传工具结果", "document": None}
        return slot["result"]

    def _assist_call_model(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """流式聚合工具调用决策：tool_calls 分片拼装；读超时只约束 chunk 间隔。"""
        text_config = self._text_config()
        body: dict[str, Any] = {
            "model": text_config["model"],
            "temperature": 0.3,
            "messages": messages,
            "tools": self.ASSIST_TOOLS,
            "stream": True,
        }
        # 决策型循环默认关闭深度思考（GLM thinking 参数；不支持的网关会忽略该字段）
        if self.settings.assist_thinking == "disabled":
            body["thinking"] = {"type": "disabled"}
        headers = {"Authorization": "Bearer " + text_config["api_key"]}
        content_parts: list[str] = []
        tool_acc: dict[int, dict[str, str]] = {}
        with httpx.stream(
            "POST", text_config["api_base"] + "/chat/completions", headers=headers, json=body,
            timeout=self.settings.text_timeout_seconds,
        ) as response:
            if response.status_code >= 400:
                response.read()
                raise ServiceError(f"模型返回 HTTP {response.status_code}：{response.text[:200]}", 502)
            if "text/event-stream" not in response.headers.get("content-type", ""):
                response.read()
                return response.json()
            for line in response.iter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[len("data: "):].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except ValueError:
                    continue
                for choice in event.get("choices", []):
                    delta = choice.get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        content_parts.append(str(piece))
                    for tc in delta.get("tool_calls") or []:
                        index = int(tc.get("index") or 0)
                        slot = tool_acc.setdefault(index, {"id": "", "name": "", "arguments": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"]
        tool_calls = [
            {"id": slot["id"] or f"call_{index}", "type": "function",
             "function": {"name": slot["name"], "arguments": slot["arguments"] or "{}"}}
            for index, slot in sorted(tool_acc.items())
        ]
        message: dict[str, Any] = {"role": "assistant", "content": "".join(content_parts)}
        if tool_calls:
            message["tool_calls"] = tool_calls
        return {"choices": [{"message": message}]}

    def assist_article(self, article_id: str, instruction: str, emit, attachments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """emit(name, data) 由路由层桥接到 SSE。返回最终入库结果。"""
        article = self._get_article(article_id)
        text_config = self._text_config()
        if text_config["provider"] != "openai_compatible":
            emit(
                "notice",
                {
                    "message": "当前文本模型为本地适配器，暂不支持工具调用，"
                    "已自动切换为整篇修订模式。配置 OpenAI-compatible 模型后可体验逐块协作编辑。"
                },
            )
            result = self.revise_article_from_chat(article_id, instruction)
            emit("completed", {"result": result, "tool_calls": 0, "downgraded": True})
            return result
        import threading

        assist_id = new_id("assist")
        session = {"pending": {}, "lock": threading.Lock()}
        self._assist_registry()[assist_id] = session
        emit("state", {"status": "connected", "assist_id": assist_id, "message": "协作编辑会话已建立"})
        user_message = self.start_article_chat(article_id, instruction, attachments)
        emit("user", {"message": user_message})
        content = json.loads(article["content_json"] or "{}")
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.ASSIST_SYSTEM_PROMPT},
            {
                "role": "system",
                "content": "当前文章（JSON）：" + json.dumps(
                    {
                        "title": content.get("title"),
                        "summary": content.get("summary"),
                        "sections": [
                            {
                                "id": s.get("id"),
                                "heading": s.get("heading"),
                                "paragraphs": s.get("paragraphs", []),
                                "asset_refs": s.get("asset_refs", []),
                            }
                            for s in content.get("sections", [])
                        ],
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        assets_digest = self._session_assets_digest(article["session_id"], article_id)
        if assets_digest:
            messages.append({"role": "system", "content": assets_digest})
        history = self._recent_chat_digest(article_id, instruction)
        if history:
            messages.append({"role": "system", "content": history})
        messages.append({"role": "user", "content": instruction})
        # 预执行 read_article：文章已在上下文，模型无需再花一轮思考决定读取
        preload_call_id = "preload_read_article"
        article_brief = messages[1]["content"]  # 「当前文章（JSON）」system 消息
        messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": preload_call_id,
                "type": "function",
                "function": {"name": "read_article", "arguments": "{}"},
            }],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": preload_call_id,
            "content": json.dumps({"status": "ok", "message": "文章已预读取（系统注入，跳过等待）", "document": json.loads(article_brief.split("：", 1)[1] or "{}")}, ensure_ascii=False)[:12000],
        })
        emit("tool_call", {"call_id": preload_call_id, "name": "read_article", "arguments": {}})
        emit("tool_result", {"call_id": preload_call_id, "name": "read_article", "status": "ok", "message": "已读取当前文章（预注入）"})
        total_usage = {"input": 0, "output": 0}
        final_document: dict[str, Any] | None = None
        assistant_reply = ""
        tool_calls_used = 0
        try:
            self._enforce_ai_budget(self.settings.text_provider, self.settings.text_model)
            for _round in range(self.ASSIST_MAX_TOOL_CALLS):
                emit("state", {"status": "thinking", "message": "正在思考下一步修改"})
                payload = self._assist_call_model(messages)
                choice = payload["choices"][0]["message"]
                usage = payload.get("usage") or {}
                total_usage["input"] += int(usage.get("prompt_tokens") or 0)
                total_usage["output"] += int(usage.get("completion_tokens") or 0)
                tool_calls = choice.get("tool_calls") or []
                if not tool_calls:
                    assistant_reply = str(choice.get("content") or "")
                    for start in range(0, len(assistant_reply), 24):
                        emit("delta", {"content": assistant_reply[start : start + 24]})
                    break
                messages.append(choice)
                for call in tool_calls:
                    tool_calls_used += 1
                    function = call.get("function", {})
                    call_id = str(call.get("id") or new_id("call"))
                    try:
                        arguments = json.loads(function.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        arguments = {"_raw": function.get("arguments")}
                    emit(
                        "tool_call",
                        {"call_id": call_id, "name": function.get("name"), "arguments": arguments},
                    )
                    with session["lock"]:
                        session["pending"][call_id] = {"event": threading.Event(), "result": None}
                    result = self._assist_wait(session, call_id)
                    with session["lock"]:
                        session["pending"].pop(call_id, None)
                    emit(
                        "tool_result",
                        {
                            "call_id": call_id,
                            "name": function.get("name"),
                            "status": result["status"],
                            "message": result["message"][:200] if result.get("message") else "",
                        },
                    )
                    if result.get("document") is not None:
                        final_document = result["document"]
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": json.dumps(
                                {"status": result["status"], "message": (result.get("message") or "")[:400]},
                                ensure_ascii=False,
                            ),
                        }
                    )
                    if result["status"] != "ok":
                        raise ServiceError("浏览器工具执行失败：" + str(result.get("message") or ""), 502)
                    changed_blocks = [t for t in (result.get("changed_blocks") or []) if t]
                    if changed_blocks:
                        # 工具改动入库：历史消息可点击回放高亮
                        self._insert_article_message(
                            article_id,
                            "TOOL",
                            result["message"][:400],
                            status="COMPLETED",
                            metadata={"tool": function.get("name"), "blocks": changed_blocks},
                        )
            else:
                raise ServiceError(f"单轮工具调用超过 {self.ASSIST_MAX_TOOL_CALLS} 次上限，已停止", 400)
            # 入库：最后一个回传的完整文档（或无工具调用时保持原文）
            result_payload: dict[str, Any]
            if final_document is not None:
                from .schemas import ArticleSectionInput, ArticleUpdate

                sections_in = []
                for section in final_document.get("sections", []):
                    sections_in.append(
                        ArticleSectionInput(
                            id=str(section.get("id") or "sec_01"),
                            heading=str(section.get("heading") or "未命名小节"),
                            paragraphs=[str(p) for p in section.get("paragraphs", [])] or ["（空）"],
                            asset_refs=[str(a) for a in section.get("asset_refs", [])],
                            style=section.get("style") if isinstance(section.get("style"), dict) else {},
                        )
                    )
                update = ArticleUpdate(
                    title=str(final_document.get("title") or content.get("title") or "未命名"),
                    summary=str(final_document.get("summary") or ""),
                    sections=sections_in or [ArticleSectionInput(id="sec_01", heading="正文", paragraphs=["（空）"])],
                    change_summary="AI 协作编辑（" + str(tool_calls_used) + " 次工具调用）",
                )
                assist_highlight = self._diff_added_paragraphs(content, {
                    "sections": [
                        {"paragraphs": [str(p) for p in sec.paragraphs]}
                        for sec in sections_in
                    ]
                })
                result_payload = self.update_article(article_id, update, assist_highlight)
            else:
                result_payload = {
                    "article_id": article_id,
                    "version": None,
                    "message": "模型未请求修改文章",
                }
            with self.db.tx():
                self.db.execute(
                    "INSERT INTO ai_run(id,session_id,task_type,provider,model,input_tokens,output_tokens,latency_ms,cost,status,created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        new_id("airun"),
                        article["session_id"],
                        "ASSIST",
                        self.settings.text_provider,
                        self.settings.text_model,
                        total_usage["input"],
                        total_usage["output"],
                        0,
                        str(self._ai_call_cost(self.settings.text_provider, self.settings.text_model, total_usage["input"], total_usage["output"])),
                        "SUCCESS",
                        utc_now(),
                    ),
                )
            self._insert_article_message(
                article_id,
                "ASSISTANT",
                assistant_reply or "（已完成工具修改）",
                "COMPLETED",
                {"version": (result_payload or {}).get("version"), "tool_calls": tool_calls_used},
            )
            emit("completed", {"assist_id": assist_id, "result": result_payload, "tool_calls": tool_calls_used})
            return result_payload
        except Exception as exc:
            self.fail_article_chat(article_id, instruction, str(exc))
            emit("error", {"message": str(exc)})
            raise
        finally:
            self._assist_registry().pop(assist_id, None)

    def revert_article(self, article_id: str, to_version: int) -> dict[str, Any]:
        """Codex 式回退：把第 to_version 版内容保存为最新版（历史不丢，可再回退）。"""
        self._get_article(article_id)
        row = self.db.query_one(
            "SELECT content_json, version_no FROM article_version WHERE article_id=? AND version_no=?",
            (article_id, to_version),
        )
        if row is None:
            raise ServiceError(f"版本 {to_version} 不存在", 404)
        old = json.loads(row["content_json"] or "{}")
        from .schemas import ArticleSectionInput, ArticleUpdate

        sections_in = [
            ArticleSectionInput(
                id=str(sec.get("id") or f"sec_{index:02d}"),
                heading=str(sec.get("heading") or "未命名小节"),
                paragraphs=[str(p) for p in sec.get("paragraphs", [])] or ["（空）"],
                asset_refs=[str(a) for a in sec.get("asset_refs", [])],
                style=sec.get("style") if isinstance(sec.get("style"), dict) else {},
            )
            for index, sec in enumerate(old.get("sections", []), start=1)
        ] or [ArticleSectionInput(id="sec_01", heading="正文", paragraphs=["（空）"])]
        update = ArticleUpdate(
            title=str(old.get("title") or "未命名"),
            summary=str(old.get("summary") or ""),
            sections=sections_in,
            change_summary=f"对话回退：恢复到第 {to_version} 版",
        )
        result = self.update_article(article_id, update)
        new_version = result.get("version")
        self._insert_article_message(
            article_id,
            "ASSISTANT",
            f"已回退到第 {to_version} 版（当前第 {new_version} 版，改动前内容已保留在版本历史）",
            "COMPLETED",
            {"version": new_version, "reverted_from_version": to_version},
        )
        return {"article_id": article_id, "reverted_to": to_version, "new_version": new_version}

    def update_article(self, article_id: str, payload: ArticleUpdate, highlight_texts: list[str] | None = None) -> dict[str, Any]:
        article = self._get_article(article_id)
        if payload.typesetting_id is not None:
            if payload.typesetting_id not in self._style_map():
                raise ServiceError("未知样式模版", 400)
            with self.db.tx():
                self.db.execute(
                    "UPDATE article SET typesetting_id=?,updated_at=? WHERE id=?",
                    (payload.typesetting_id, utc_now(), article_id),
                )
        previous_content = json.loads(article["content_json"] or "{}")
        content = {
            "title": payload.title,
            "summary": payload.summary,
            "sections": [section.model_dump() for section in payload.sections],
            "editorial": previous_content.get("editorial", {}),
        }
        context = build_session_context(self.db, article["session_id"])
        result = review_article(content, context.facts)
        version_row = self.db.query_one(
            "SELECT MAX(version_no) AS next_version FROM article_version WHERE article_id = ?",
            (article_id,),
        )
        next_version = int(version_row["next_version"] or 0) + 1
        version_id = new_id("version")
        now = utc_now()
        status = "AI_REVIEWED" if result.decision == "PASS" else "NEEDS_REVISION"
        with self.db.tx():
            self.db.execute(
                """
                INSERT INTO article_version
                (id,article_id,version_no,source,content_json,change_summary,created_at,highlight_json)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    version_id,
                    article_id,
                    next_version,
                    "HUMAN",
                    json.dumps(content, ensure_ascii=False),
                    payload.change_summary,
                    now,
                    json.dumps(highlight_texts or [], ensure_ascii=False),
                ),
            )
            self.db.execute(
                """
                UPDATE article
                SET title=?,summary=?,content_json=?,status=?,current_version_id=?,
                    original_declaration_status=NULL,original_declaration_version_id=NULL,
                    original_declaration_confirmed_by=NULL,original_declaration_confirmed_at=NULL,updated_at=?
                WHERE id=?
                """,
                (payload.title, payload.summary, json.dumps(content), status, version_id, now, article_id),
            )
            self.db.execute(
                """
                INSERT INTO review
                (id,article_id,review_type,score,decision,risk_level,result_json,reviewer,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    new_id("review"),
                    article_id,
                    "AI",
                    result.score,
                    result.decision,
                    result.risk_level,
                    json.dumps(review_to_dict(result), ensure_ascii=False),
                    "post-human-edit",
                    now,
                ),
            )
        self.audit("article.update", "article", article_id, {"version": next_version})
        return {"article_id": article_id, "version": next_version, "review": review_to_dict(result)}

    def human_decision(self, article_id: str, approved: bool, reviewer: str) -> dict[str, Any]:
        article = self._get_article(article_id)
        latest = self.db.query_one(
            "SELECT * FROM review WHERE article_id = ? ORDER BY created_at DESC LIMIT 1", (article_id,)
        )
        if latest is None:
            raise ServiceError("AI review must run before human review")
        if approved and latest["decision"] == "REJECT":
            raise ServiceError("A REJECT review cannot be approved without a new article version")
        status = "HUMAN_APPROVED" if approved else "REJECTED"
        with self.db.tx():
            self.db.execute(
                "UPDATE article SET status=?,updated_at=? WHERE id=?", (status, utc_now(), article_id)
            )
            self.db.execute(
                """
                INSERT INTO review
                (id,article_id,review_type,score,decision,risk_level,result_json,reviewer,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    new_id("review"),
                    article_id,
                    "HUMAN",
                    latest["score"],
                    "PASS" if approved else "REJECT",
                    latest["risk_level"],
                    json.dumps({"reviewer": reviewer}, ensure_ascii=False),
                    reviewer,
                    utc_now(),
                ),
            )
            self.db.execute(
                "UPDATE content_session SET status=?,updated_at=? WHERE id=?",
                (status, utc_now(), article["session_id"]),
            )
        self.audit("article.approve" if approved else "article.reject", "article", article_id, {"reviewer": reviewer})
        return {"article_id": article_id, "status": status}

    def _article_account(self, article: dict[str, Any]) -> dict[str, Any]:
        session = self.db.query_one(
            "SELECT * FROM content_session WHERE id = ?", (article["session_id"],)
        )
        assert session is not None
        account = self.db.query_one(
            "SELECT * FROM official_account WHERE id = ?", (session["official_account_id"],)
        )
        assert account is not None
        return account

    def _current_draft_job(
        self, article: dict[str, Any], account: dict[str, Any]
    ) -> dict[str, Any] | None:
        draft_idempotency = hashlib.sha256(
            f"{article['id']}:{account['id']}:{article['current_version_id']}:draft".encode()
        ).hexdigest()
        return self.db.query_one(
            """
            SELECT * FROM publish_job
            WHERE article_id=? AND official_account_id=? AND idempotency_key=?
              AND status='WECHAT_DRAFT' AND external_id IS NOT NULL
            ORDER BY updated_at DESC LIMIT 1
            """,
            (article["id"], account["id"], draft_idempotency),
        )

    @staticmethod
    def _original_declaration_confirmed(article: dict[str, Any]) -> bool:
        return (
            article.get("original_declaration_status") == "MANUAL_CONFIRMED"
            and article.get("original_declaration_version_id") == article.get("current_version_id")
        )

    def _require_original_declaration(self, article: dict[str, Any]) -> None:
        if self.settings.publisher == "wechat" and not self._original_declaration_confirmed(article):
            raise ServiceError("Confirm the WeChat original declaration before publishing", 409)

    def confirm_original_declaration(
        self, article_id: str, reviewer: str, confirmed: bool
    ) -> dict[str, Any]:
        article = self._get_article(article_id)
        if self.settings.publisher != "wechat":
            raise ServiceError("Original declaration is only used by the WeChat publisher", 400)
        now = utc_now()
        if not confirmed:
            with self.db.tx():
                self.db.execute(
                    """
                    UPDATE article
                    SET original_declaration_status=NULL,original_declaration_version_id=NULL,
                        original_declaration_confirmed_by=NULL,original_declaration_confirmed_at=NULL,updated_at=?
                    WHERE id=?
                    """,
                    (now, article_id),
                )
            self.audit("article.original_declaration.reset", "article", article_id, {"reviewer": reviewer})
        else:
            account = self._article_account(article)
            if self._current_draft_job(article, account) is None:
                raise ServiceError("Sync a WeChat draft before confirming original declaration", 409)
            with self.db.tx():
                self.db.execute(
                    """
                    UPDATE article
                    SET original_declaration_status='MANUAL_CONFIRMED',original_declaration_version_id=?,
                        original_declaration_confirmed_by=?,original_declaration_confirmed_at=?,updated_at=?
                    WHERE id=?
                    """,
                    (
                        article["current_version_id"],
                        reviewer,
                        now,
                        now,
                        article_id,
                    ),
                )
            self.audit(
                "article.original_declaration.confirm",
                "article",
                article_id,
                {"reviewer": reviewer, "version_id": article["current_version_id"]},
            )
        result = self._get_article(article_id)
        result["original_declaration_required"] = True
        return result

    @staticmethod
    def _is_mock_account(account: dict[str, Any]) -> bool:
        """内置本地测试账号：mock 占位 AppID 永远走 MockPublisher，不受全局发布通道影响。"""
        app_id = str(account.get("app_id") or "")
        return app_id.startswith("mock") or account.get("token_status") == "MOCK"

    def _publisher(self, account: dict[str, Any]) -> Publisher:
        if self.settings.publisher == "mock" or self._is_mock_account(account):
            return MockPublisher()
        settings = json.loads(account["settings"] or "{}")
        secret_env = settings.get("credential_env") or f"WECHAT_APP_SECRET_{account['app_id']}"
        secret = os.environ.get(secret_env, self.settings.wechat_app_secret)
        # 实例级缓存：让 WeChatPublisher 的进程内 access_token 缓存跨请求复用
        cache_key = account["app_id"] + ":" + secret_env
        cached = getattr(self, "_publisher_cache", None)
        if cached is None:
            cached = {}
            self._publisher_cache = cached
        if cache_key not in cached:
            cached[cache_key] = WeChatPublisher(account["app_id"], secret)
        return cached[cache_key]

    def _callback_token(self, account: dict[str, Any]) -> str:
        settings = json.loads(account.get("settings") or "{}")
        token_env = settings.get("callback_token_env") or (
            f"WECHAT_CALLBACK_TOKEN_{account['app_id']}"
        )
        token = os.environ.get(token_env, self.settings.callback_token)
        if not token:
            raise ServiceError("WeChat callback token is not configured", 503)
        return token

    @staticmethod
    def _wechat_signature(token: str, timestamp: str, nonce: str) -> str:
        raw = "".join(sorted((token, timestamp, nonce))).encode()
        return hashlib.sha1(raw).hexdigest()

    def verify_wechat_callback(
        self,
        account_id: str,
        signature: str,
        timestamp: str,
        nonce: str,
    ) -> dict[str, Any]:
        account = self.db.query_one(
            "SELECT * FROM official_account WHERE id = ?", (account_id,)
        )
        if account is None:
            raise ServiceError("Official account not found", 404)
        if not signature or not timestamp or not nonce:
            raise ServiceError("Missing WeChat callback parameters", 400)
        expected = self._wechat_signature(
            self._callback_token(account), timestamp, nonce
        )
        if not hmac.compare_digest(expected, signature):
            raise ServiceError("Invalid WeChat callback signature", 403)
        return account

    @staticmethod
    def _parse_wechat_xml(body: bytes) -> Any:
        import xml.etree.ElementTree as ET

        if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
            raise ServiceError("WeChat callback XML contains forbidden DTD/entities", 400)
        try:
            return ET.fromstring(body)
        except (ET.ParseError, ValueError) as exc:
            raise ServiceError("Invalid WeChat callback XML", 400) from exc

    @staticmethod
    def _wechat_callback_event_key(
        account_id: str,
        root: Any,
        event_info: dict[str, Any],
    ) -> str:
        msg_id = root.findtext(".//MsgId", default="").strip()
        if msg_id:
            identity = {"message_id": msg_id}
        else:
            identity = {
                "event": event_info["event"],
                "publish_id": event_info["publish_id"],
                "publish_status": event_info["publish_status"],
                "article_url": event_info["article_url"],
                "fail_idx": event_info["fail_idx"],
            }
        raw = json.dumps(
            {"account_id": account_id, "identity": identity},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def handle_wechat_publish_callback(
        self,
        account_id: str,
        body: bytes,
        signature: str,
        timestamp: str,
        nonce: str,
    ) -> dict[str, Any]:
        account = self.verify_wechat_callback(account_id, signature, timestamp, nonce)
        root = self._parse_wechat_xml(body)
        event = root.findtext(".//Event", default="").strip()
        if event != "PUBLISHJOBFINISH":
            return {"ok": True, "account_id": account_id, "ignored_event": event or None}

        publish_id = root.findtext(".//publish_id", default="").strip()
        if not publish_id:
            raise ServiceError("WeChat publish callback has no publish_id", 400)

        publish_status = root.findtext(".//publish_status", default="").strip()
        article_url = root.findtext(".//article_url", default="").strip()
        fail_idx = [
            element.text.strip()
            for element in root.findall(".//fail_idx")
            if element.text
        ]
        event_info = {
            "event": event,
            "publish_id": publish_id,
            "publish_status": publish_status,
            "article_url": article_url,
            "fail_idx": fail_idx,
            "received_at": utc_now(),
        }
        try:
            numeric_publish_status = int(publish_status)
        except ValueError as exc:
            raise ServiceError("Invalid WeChat publish_status", 400) from exc
        mapping = {
            0: "PUBLISHED",
            1: "PUBLISHING",
            2: "FAILED",
            3: "FAILED",
            4: "FAILED",
            5: "FAILED",
        }
        if numeric_publish_status not in mapping:
            raise ServiceError(
                f"Unsupported WeChat publish_status: {numeric_publish_status}", 400
            )

        payload_json = json.dumps(event_info, ensure_ascii=False, sort_keys=True)
        event_key = self._wechat_callback_event_key(account_id, root, event_info)
        reservation = self.db.claim_wechat_callback_event(
            event_key=event_key,
            official_account_id=account_id,
            event=event,
            publish_id=publish_id,
            payload_json=payload_json,
        )
        if reservation["duplicate"]:
            if reservation["status"] == "PROCESSING":
                raise ServiceError("WeChat callback event is already being processed", 409)
            result = json.loads(reservation["result_json"] or "{}")
            return {**result, "duplicate": True}

        job = self.db.query_one(
            """
            SELECT * FROM publish_job
            WHERE official_account_id = ? AND external_id = ?
            ORDER BY updated_at DESC LIMIT 1
            """,
            (account_id, publish_id),
        )
        try:
            if job is None:
                result = {
                    "ok": True,
                    "account_id": account_id,
                    "matched": False,
                    "event": event_info,
                }
                self.db.finish_wechat_callback_event(
                    event_key,
                    status="PROCESSED",
                    result_json=json.dumps(result, ensure_ascii=False),
                )
                return result

            status = mapping[numeric_publish_status]
            with self.db.tx():
                self.db.execute(
                    """
                    UPDATE publish_job
                    SET status=?, article_url=?, last_error=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        status,
                        article_url or job.get("article_url"),
                        json.dumps(event_info, ensure_ascii=False),
                        utc_now(),
                        job["id"],
                    ),
                )
                if status != "PUBLISHING":
                    self.db.execute(
                        "UPDATE article SET status=?,updated_at=? WHERE id=?",
                        (status, utc_now(), job["article_id"]),
                    )
            self.db.finish_wechat_callback_event(
                event_key,
                status="PROCESSED",
                result_json=json.dumps(
                    {
                        "ok": True,
                        "account_id": account_id,
                        "matched": True,
                        "publish_job_id": job["id"],
                        "status": status,
                        "article_url": article_url,
                    },
                    ensure_ascii=False,
                ),
            )
            self.audit(
                "publish.callback",
                "publish_job",
                job["id"],
                {"status": status, "publish_status": numeric_publish_status, "article_url": article_url},
            )
            return {
                "ok": True,
                "account_id": account_id,
                "matched": True,
                "publish_job_id": job["id"],
                "status": status,
                "article_url": article_url,
            }
        except Exception as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
            self.db.finish_wechat_callback_event(
                event_key,
                status="FAILED",
                last_error=json.dumps(error, ensure_ascii=False),
            )
            raise

    def schedule_article(self, article_id: str, scheduled_at: str, timezone_name: str = "") -> dict[str, Any]:
        try:
            parsed = datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ServiceError("Invalid scheduled_at datetime", 400) from exc
        if parsed.tzinfo is None:
            # 无时区的本地时间按调用方声明的 IANA 时区解释（默认 UTC）
            try:
                from zoneinfo import ZoneInfo

                parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name) if timezone_name else timezone.utc)
            except Exception as exc:  # noqa: BLE001 - 非法时区名兜底
                raise ServiceError(f"Unknown timezone: {timezone_name}", 400) from exc
        parsed = parsed.astimezone(timezone.utc)
        if parsed <= datetime.now(timezone.utc) + timedelta(seconds=5):
            raise ServiceError("scheduled_at must be at least 5 seconds in the future", 400)

        article = self._get_article(article_id)
        if article["status"] not in {"HUMAN_APPROVED", "WECHAT_DRAFT"}:
            raise ServiceError("Article must be human approved before scheduling", 409)
        session = self.db.query_one("SELECT * FROM content_session WHERE id = ?", (article["session_id"],))
        assert session is not None
        account = self.db.query_one(
            "SELECT * FROM official_account WHERE id = ?", (session["official_account_id"],)
        )
        assert account is not None
        self._require_original_declaration(article)

        active = self.db.query_one(
            """
            SELECT * FROM publish_job
            WHERE article_id = ? AND status = 'SCHEDULED'
            ORDER BY scheduled_at LIMIT 1
            """,
            (article_id,),
        )
        if active is not None:
            return active

        job_id = new_id("publish")
        normalized = parsed.isoformat()
        idempotency = hashlib.sha256(
            f"{article['id']}:{account['id']}:{article['current_version_id']}:publish:{normalized}".encode()
        ).hexdigest()
        now = utc_now()
        with self.db.tx():
            self.db.execute(
                """
                INSERT INTO publish_job
                (id,article_id,official_account_id,mode,status,idempotency_key,retry_count,scheduled_at,created_at,updated_at)
                VALUES (?,?,?,?,?,?,0,?,?,?)
                """,
                (job_id, article_id, account["id"], "publish", "SCHEDULED", idempotency, normalized, now, now),
            )
        self.audit(
            "publish.schedule",
            "publish_job",
            job_id,
            {"scheduled_at": normalized, "article_id": article_id},
        )
        output = self.db.query_one("SELECT * FROM publish_job WHERE id = ?", (job_id,))
        assert output is not None
        return output

    def reschedule_article(self, article_id: str, scheduled_at: str) -> dict[str, Any]:
        try:
            parsed = datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ServiceError("Invalid scheduled_at datetime", 400) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed <= datetime.now(timezone.utc) + timedelta(seconds=5):
            raise ServiceError("scheduled_at must be at least 5 seconds in the future", 400)
        active = self.db.query_one(
            """
            SELECT * FROM publish_job
            WHERE article_id = ? AND status = 'SCHEDULED'
            ORDER BY scheduled_at LIMIT 1
            """,
            (article_id,),
        )
        if active is None:
            raise ServiceError("No scheduled publish job for this article", 404)
        normalized = parsed.isoformat()
        idempotency = hashlib.sha256(
            f"{article_id}:{active['official_account_id']}:{active['id']}:{normalized}".encode()
        ).hexdigest()
        with self.db.tx():
            self.db.execute(
                """
                UPDATE publish_job
                SET scheduled_at=?, idempotency_key=?, updated_at=?
                WHERE id=? AND status='SCHEDULED'
                """,
                (normalized, idempotency, utc_now(), active["id"]),
            )
        self.audit(
            "publish.reschedule",
            "publish_job",
            active["id"],
            {"scheduled_at": normalized, "article_id": article_id},
        )
        output = self.db.query_one("SELECT * FROM publish_job WHERE id = ?", (active["id"],))
        assert output is not None
        return output

    def cancel_schedule(self, article_id: str) -> dict[str, Any]:
        active = self.db.query_one(
            """
            SELECT * FROM publish_job
            WHERE article_id = ? AND status = 'SCHEDULED'
            ORDER BY scheduled_at LIMIT 1
            """,
            (article_id,),
        )
        if active is None:
            raise ServiceError("No scheduled publish job for this article", 404)
        with self.db.tx():
            self.db.execute(
                """
                UPDATE publish_job SET status='CANCELLED',updated_at=?
                WHERE id=? AND status='SCHEDULED'
                """,
                (utc_now(), active["id"]),
            )
        self.audit("publish.cancel", "publish_job", active["id"], {"article_id": article_id})
        output = self.db.query_one("SELECT * FROM publish_job WHERE id = ?", (active["id"],))
        assert output is not None
        return output

    async def sync_wechat_draft_status(self, article_id: str) -> dict[str, Any]:
        article = self._get_article(article_id)
        account = self._article_account(article)
        allowed_account_status = {"MOCK"} if self.settings.publisher == "mock" else {"READY"}
        if account["token_status"] not in allowed_account_status:
            raise ServiceError(
                f"Official account is not ready for draft verification: {account['token_status']}",
                409,
            )
        job = self._current_draft_job(article, account)
        if job is None:
            return {"article_id": article_id, "status": "NO_DRAFT"}

        publisher = self._publisher(account)
        try:
            draft = await publisher.get_draft(str(job["external_id"]), account)
        except Exception as exc:
            raise ServiceError(f"WeChat draft verification failed: {exc}", 502) from exc

        if draft is not None:
            with self.db.tx():
                self.db.execute(
                    "UPDATE publish_job SET last_error='{}',updated_at=? WHERE id=?",
                    (utc_now(), job["id"]),
                )
            news_items = draft.get("news_item") if isinstance(draft, dict) else []
            first = news_items[0] if isinstance(news_items, list) and news_items else {}
            return {
                "article_id": article_id,
                "status": "EXISTS",
                "publish_job_id": job["id"],
                "external_id": job["external_id"],
                "external_title": first.get("title") if isinstance(first, dict) else None,
            }

        now = utc_now()
        with self.db.tx():
            self.db.execute(
                """
                UPDATE publish_job
                SET status='WECHAT_DRAFT_DELETED',last_error=?,updated_at=?
                WHERE id=? AND status='WECHAT_DRAFT'
                """,
                (
                    json.dumps({"message": "Deleted in WeChat drafts"}, ensure_ascii=False),
                    now,
                    job["id"],
                ),
            )
            if article["status"] == "WECHAT_DRAFT":
                self.db.execute(
                    """
                    UPDATE article
                    SET status='HUMAN_APPROVED',
                        original_declaration_status=NULL,
                        original_declaration_version_id=NULL,
                        original_declaration_confirmed_by=NULL,
                        original_declaration_confirmed_at=NULL,
                        updated_at=?
                    WHERE id=?
                    """,
                    (now, article_id),
                )
        self.audit(
            "article.wechat_draft.deleted",
            "publish_job",
            job["id"],
            {"article_id": article_id, "external_id": job["external_id"]},
        )
        return {
            "article_id": article_id,
            "status": "DELETED",
            "publish_job_id": job["id"],
            "external_id": job["external_id"],
        }

    async def sync_all_wechat_drafts(self, limit: int = 50) -> dict[str, Any]:
        safe_limit = max(1, min(limit, 100))
        allowed_status = "MOCK" if self.settings.publisher == "mock" else "READY"
        rows = self.db.query_all(
            """
            SELECT DISTINCT a.id
            FROM article a
            JOIN publish_job p ON p.article_id=a.id
            JOIN official_account o ON o.id=p.official_account_id
            WHERE a.status='WECHAT_DRAFT' AND p.status='WECHAT_DRAFT'
              AND p.external_id IS NOT NULL
              AND o.token_status=?
            ORDER BY a.updated_at DESC
            LIMIT ?
            """,
            (allowed_status, safe_limit),
        )
        skipped_row = self.db.query_one(
            """
            SELECT COUNT(DISTINCT a.id) AS value
            FROM article a
            JOIN publish_job p ON p.article_id=a.id
            JOIN official_account o ON o.id=p.official_account_id
            WHERE a.status='WECHAT_DRAFT' AND p.status='WECHAT_DRAFT'
              AND p.external_id IS NOT NULL AND o.token_status!=?
            """,
            (allowed_status,),
        )
        unverified_rows = self.db.query_all(
            """
            SELECT DISTINCT a.id
            FROM article a
            JOIN publish_job p ON p.article_id=a.id
            JOIN official_account o ON o.id=p.official_account_id
            WHERE a.status='WECHAT_DRAFT' AND p.status='WECHAT_DRAFT'
              AND p.external_id IS NOT NULL AND o.token_status!=?
            """,
            (allowed_status,),
        )
        for row in unverified_rows:
            now = utc_now()
            with self.db.tx():
                self.db.execute(
                    """
                    UPDATE publish_job
                    SET status='WECHAT_DRAFT_UNVERIFIED',updated_at=?
                    WHERE article_id=? AND status='WECHAT_DRAFT'
                    """,
                    (now, row["id"]),
                )
                self.db.execute(
                    """
                    UPDATE article
                    SET status='HUMAN_APPROVED',
                        original_declaration_status=NULL,
                        original_declaration_version_id=NULL,
                        original_declaration_confirmed_by=NULL,
                        original_declaration_confirmed_at=NULL,
                        updated_at=?
                    WHERE id=? AND status='WECHAT_DRAFT'
                    """,
                    (now, row["id"]),
                )
            self.audit(
                "article.wechat_draft.quarantine_unverified_account",
                "article",
                row["id"],
                {"reason": "Official account is not READY"},
            )
        results = []
        errors = []
        for row in rows:
            try:
                result = await self.sync_wechat_draft_status(row["id"])
                results.append(result)
            except ServiceError as exc:
                errors.append({"article_id": row["id"], "message": str(exc)})
        counts = {"EXISTS": 0, "DELETED": 0}
        for result in results:
            if result.get("status") in counts:
                counts[result["status"]] += 1
        return {
            "checked": len(rows),
            "skipped_unverified_accounts": int(skipped_row["value"] or 0) if skipped_row else 0,
            "quarantined_unverified_drafts": len(unverified_rows),
            "exists": counts["EXISTS"],
            "deleted": counts["DELETED"],
            "results": results,
            "errors": errors,
        }

    async def publish_article(self, article_id: str, mode: str) -> dict[str, Any]:
        article = self._get_article(article_id)
        allowed_after_submit = {"HUMAN_APPROVED", "WECHAT_DRAFT", "PUBLISHING", "PUBLISHED"}
        # 正式发布保留不可绕过的人工卡口；存草稿随时允许（作者在对话中主动同步即视为人的确认）
        if mode == "publish" and article["status"] not in allowed_after_submit:
            raise ServiceError("Article must be human approved before publishing", 409)
        session = self.db.query_one("SELECT * FROM content_session WHERE id = ?", (article["session_id"],))
        assert session is not None
        account = self.db.query_one(
            "SELECT * FROM official_account WHERE id = ?", (session["official_account_id"],)
        )
        assert account is not None
        if mode == "publish":
            self._require_original_declaration(article)
        assets = self.db.query_all(
            "SELECT * FROM session_asset WHERE session_id = ? ORDER BY created_at", (article["session_id"],)
        )
        content = json.loads(article["content_json"])
        referenced = {
            asset_id
            for section in content.get("sections", [])
            for asset_id in section.get("asset_refs", [])
        }
        image_files: list[AssetFile] = []
        cover: AssetFile | None = None
        # 封面兜底：未指定时取第一张正文引用图，再退会话首图（公众号草稿必须有封面）
        cover_id = article.get("cover_asset_id")
        if not cover_id and referenced:
            cover_id = next(iter(referenced))
        if not cover_id:
            fallback = self.db.query_one(
                "SELECT id FROM session_asset WHERE session_id=? AND asset_type='IMAGE'"
                " AND processing_status IN ('READY','VISION_READY') ORDER BY created_at LIMIT 1",
                (article["session_id"],),
            )
            cover_id = fallback["id"] if fallback else None
        for asset in assets:
            is_cover = asset["id"] == cover_id
            if (asset["id"] not in referenced and not is_cover) or asset["asset_type"] != "IMAGE":
                continue
            _, payload = self.get_asset_file(asset["id"])
            asset_file = AssetFile(asset["id"], asset["file_name"], asset["mime_type"], payload)
            image_files.append(asset_file)
            if cover is None or asset["id"] == article.get("cover_asset_id"):
                cover = asset_file
        publisher = self._publisher(account)
        try:
            urls = await publisher.asset_urls(image_files)
        except Exception as exc:
            message = str(exc)
            ip_match = re.search(r"invalid ip ([0-9.]+)", message, re.IGNORECASE)
            if "40164" in message and ip_match:
                raise ServiceError(
                    f"WeChat API IP whitelist rejected current IP {ip_match.group(1)}. "
                    "Add this IP in the WeChat developer platform and retry.",
                    502,
                ) from exc
            raise ServiceError(f"Publish failed while preparing WeChat assets: {exc}", 502) from exc
        account_settings = json.loads(account["settings"] or "{}")
        template = json.loads(session["brief"] or "{}").get("template") or {}
        template_style = template.get("style") if isinstance(template, dict) else None
        if isinstance(template_style, dict) and template_style:
            account_settings["style"] = {
                **(account_settings.get("style") or {}),
                **template_style,
            }
        resolved_style = self._resolved_style_for(
            article["typesetting_id"] if "typesetting_id" in article.keys() else None,
            account_settings,
        )
        html = render_article_html_with_style(content, assets, urls, resolved_style)
        idempotency = hashlib.sha256(
            f"{article['id']}:{account['id']}:{article['current_version_id']}:{mode}".encode()
        ).hexdigest()
        existing = self.db.query_one(
            "SELECT * FROM publish_job WHERE idempotency_key = ?", (idempotency,)
        )
        if existing and existing["status"] in {"WECHAT_DRAFT", "PUBLISHING", "PUBLISHED"}:
            return existing
        active_job = self.db.query_one(
            """
            SELECT * FROM publish_job
            WHERE article_id = ? AND mode = ? AND status IN ('SCHEDULED','PENDING')
            ORDER BY CASE WHEN status = 'PENDING' THEN 0 ELSE 1 END, scheduled_at
            LIMIT 1
            """,
            (article_id, mode),
        )
        if active_job is not None and active_job["status"] == "SCHEDULED":
            return active_job
        current_draft = (
            self._current_draft_job(article, account) if mode == "publish" else None
        )
        job_id = (
            current_draft["id"]
            if current_draft
            else (existing["id"] if existing else (active_job["id"] if active_job else new_id("publish")))
        )
        now = utc_now()
        if current_draft is None and existing is None and active_job is None:
            with self.db.tx():
                self.db.execute(
                    """
                    INSERT INTO publish_job
                    (id,article_id,official_account_id,mode,status,idempotency_key,retry_count,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,0,?,?)
                    """,
                    (job_id, article_id, account["id"], mode, "PENDING", idempotency, now, now),
                )
        try:
            if current_draft is not None and mode == "publish":
                result = await publisher.publish(str(current_draft["external_id"]), account)
            else:
                # 后续更新自动推送：复用该文章在本账号的最新草稿 media_id（draft/update 覆盖同一条，保留微信侧人工调整）
                latest_draft = self.db.query_one(
                    "SELECT external_id FROM publish_job"
                    " WHERE article_id=? AND official_account_id=? AND mode='draft' AND status='WECHAT_DRAFT'"
                    " AND external_id IS NOT NULL ORDER BY created_at DESC LIMIT 1",
                    (article_id, account["id"]),
                )
                reuse_media_id = str(latest_draft["external_id"]) if latest_draft else None
                result = await publisher.create_draft(
                    title=article["title"],
                    summary=article["summary"],
                    html=html,
                    account=account,
                    cover=cover,
                    external_id=reuse_media_id,
                )
                if result.error:
                    message = str(result.error.get("message") or result.error)
                    if "cover image is required" in message:
                        message = "缺少封面图：请先在对话中上传一张图片（将自动作为封面），或在文章中指定封面后再同步微信草稿"
                    raise RuntimeError(json.dumps({"message": message}, ensure_ascii=False))
                if mode == "publish":
                    result = await publisher.publish(str(result.external_id), account)
            result_idempotency = (
                hashlib.sha256(
                    f"{article['id']}:{account['id']}:{article['current_version_id']}:publish:draft:{current_draft['id']}".encode()
                ).hexdigest()
                if current_draft is not None
                else idempotency
            )
            with self.db.tx():
                self.db.execute(
                    """
                    UPDATE publish_job
                    SET mode=?,status=?,external_id=?,idempotency_key=?,updated_at=?,last_error='{}'
                    WHERE id=?
                    """,
                    (
                        mode,
                        result.status,
                        result.external_id,
                        result_idempotency,
                        utc_now(),
                        job_id,
                    ),
                )
                self.db.execute(
                    "UPDATE article SET status=?,updated_at=? WHERE id=?",
                    (result.status, utc_now(), article_id),
                )
            self.audit("publish.submit", "publish_job", job_id, {"mode": mode, "status": result.status})
            output = self.db.query_one("SELECT * FROM publish_job WHERE id = ?", (job_id,))
            assert output is not None
            return output
        except Exception as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
            with self.db.tx():
                self.db.execute(
                    "UPDATE publish_job SET status='FAILED',retry_count=retry_count+1,last_error=?,updated_at=? WHERE id=?",
                    (json.dumps(error, ensure_ascii=False), utc_now(), job_id),
                )
            raise ServiceError(f"Publish failed: {exc}", 502) from exc

    def create_account(self, payload: AccountCreate) -> dict[str, Any]:
        account_id = new_id("oa")
        callback_base = None
        if payload.callback_base_url:
            parsed = urlparse(payload.callback_base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
                raise ServiceError("callback_base_url must be an http(s) origin without credentials")
            callback_base = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
        settings = {
            "style": payload.style.model_dump(),
            "credential_env": f"WECHAT_APP_SECRET_{payload.app_id}",
            "callback_token_env": f"WECHAT_CALLBACK_TOKEN_{payload.app_id}",
            **({"callback_base_url": callback_base} if callback_base else {}),
        }
        with self.db.tx():
            self.db.execute(
                """
                INSERT INTO official_account
                (id,workspace_id,name,app_id,token_status,settings,created_at)
                VALUES (?,?,?,?,?,?,?)
                """,
                (account_id, "ws_default", payload.name, payload.app_id, "UNVERIFIED", json.dumps(settings), utc_now()),
            )
        self.audit("account.create", "official_account", account_id, {"app_id": payload.app_id})
        row = self.db.query_one("SELECT * FROM official_account WHERE id = ?", (account_id,))
        assert row is not None
        self._decorate_account(row)
        return row

    def update_account_name(self, account_id: str, payload: AccountUpdate) -> dict[str, Any]:
        account = self.db.query_one("SELECT * FROM official_account WHERE id = ?", (account_id,))
        if account is None:
            raise ServiceError("Official account not found", 404)
        name = payload.name.strip()
        if len(name) < 2:
            raise ServiceError("Official account name is too short", 400)
        with self.db.tx():
            self.db.execute(
                "UPDATE official_account SET name=? WHERE id=?",
                (name, account_id),
            )
        self.audit("account.name.sync", "official_account", account_id, {"name": name})
        row = self.db.query_one("SELECT * FROM official_account WHERE id = ?", (account_id,))
        assert row is not None
        self._decorate_account(row)
        return row

    def list_accounts(self) -> list[dict[str, Any]]:
        rows = self.db.query_all("SELECT * FROM official_account ORDER BY created_at")
        for row in rows:
            self._decorate_account(row)
        return rows

    def _decorate_account(self, row: dict[str, Any]) -> None:
        settings = json.loads(row.get("settings") or "{}")
        callback_base = settings.get("callback_base_url")
        row["credential_env"] = settings.get("credential_env") or f"WECHAT_APP_SECRET_{row['app_id']}"
        row["callback_token_env"] = settings.get("callback_token_env") or f"WECHAT_CALLBACK_TOKEN_{row['app_id']}"
        row["callback_url"] = (
            f"{callback_base}/wechat/{row['id']}/callback" if callback_base else None
        )

    async def verify_account(self, account_id: str) -> dict[str, Any]:
        account = self.db.query_one("SELECT * FROM official_account WHERE id = ?", (account_id,))
        if account is None:
            raise ServiceError("Official account not found", 404)
        settings = json.loads(account["settings"] or "{}")
        secret_env = settings.get("credential_env") or f"WECHAT_APP_SECRET_{account['app_id']}"
        secret = os.environ.get(secret_env, self.settings.wechat_app_secret)
        if self.settings.publisher != "wechat" or self._is_mock_account(account):
            return {
                "account_id": account_id,
                "status": "MOCK_MODE",
                "message": "Publisher is mock; token verification is skipped",
            }
        if not secret:
            self._update_account_verification(account, "SECRET_MISSING", settings, {"message": f"{secret_env} is not set"})
            return {
                "account_id": account_id,
                "status": "SECRET_MISSING",
                "secret_env": secret_env,
                "message": f"Set {secret_env} in .env and restart WanRen",
            }

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    "https://api.weixin.qq.com/cgi-bin/stable_token",
                    json={
                        "grant_type": "client_credential",
                        "appid": account["app_id"],
                        "secret": secret,
                    },
                )
            payload = response.json()
        except Exception as exc:
            self._update_account_verification(account, "API_ERROR", settings, {"message": str(exc)})
            return {"account_id": account_id, "status": "API_ERROR", "message": str(exc)}

        if payload.get("access_token"):
            evidence = {"verified_at": utc_now(), "errcode": None}
            self._update_account_verification(account, "READY", settings, evidence)
            return {
                "account_id": account_id,
                "status": "READY",
                "message": "AppSecret and API IP whitelist are valid",
            }

        message = str(payload.get("errmsg") or payload)
        current_ip = None
        match = re.search(r"invalid ip ([0-9.]+)", message)
        if match:
            current_ip = match.group(1)
        status = "IP_BLOCKED" if payload.get("errcode") == 40164 else "API_ERROR"
        evidence = {
            "verified_at": utc_now(),
            "errcode": payload.get("errcode"),
            "current_ip": current_ip,
        }
        self._update_account_verification(account, status, settings, evidence)
        return {
            "account_id": account_id,
            "status": status,
            "errcode": payload.get("errcode"),
            "message": message,
            "current_ip": current_ip,
        }

    def _update_account_verification(
        self,
        account: dict[str, Any],
        status: str,
        settings: dict[str, Any],
        evidence: dict[str, Any],
    ) -> None:
        settings["last_verification"] = evidence
        with self.db.tx():
            self.db.execute(
                "UPDATE official_account SET token_status=?,settings=? WHERE id=?",
                (status, json.dumps(settings, ensure_ascii=False), account["id"]),
            )
        self.audit("account.verify", "official_account", account["id"], {"status": status, **evidence})

    def list_templates(self, include_disabled: bool = False) -> list[dict[str, Any]]:
        where = "" if include_disabled else "WHERE status='ACTIVE'"
        return self.db.query_all(
            f"""
            SELECT t.*, a.name AS official_account_name
            FROM article_template t
            LEFT JOIN official_account a ON a.id=t.official_account_id
            {where}
            ORDER BY t.created_at
            """
        )

    def create_template(self, payload: TemplateCreate) -> dict[str, Any]:
        if self.db.query_one(
            "SELECT id FROM article_template WHERE workspace_id=? AND name=?",
            ("ws_default", payload.name),
        ):
            raise ServiceError("Template name already exists", 409)
        if payload.official_account_id:
            account = self.db.query_one(
                "SELECT id FROM official_account WHERE id=?", (payload.official_account_id,)
            )
            if account is None:
                raise ServiceError("Official account not found", 404)
        template_id = new_id("tpl")
        now = utc_now()
        with self.db.tx():
            self.db.execute(
                """
                INSERT INTO article_template
                (id,workspace_id,official_account_id,name,description,source,structure_json,style_json,status,version,created_by,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?, 'ACTIVE',1,'user',?,?)
                """,
                (
                    template_id,
                    "ws_default",
                    payload.official_account_id,
                    payload.name,
                    payload.description,
                    "USER",
                    json.dumps([section.model_dump() for section in payload.sections], ensure_ascii=False),
                    json.dumps(payload.style.model_dump(), ensure_ascii=False),
                    now,
                    now,
                ),
            )
        self.audit("template.create", "article_template", template_id, {"name": payload.name})
        row = self.db.query_one("SELECT * FROM article_template WHERE id = ?", (template_id,))
        assert row is not None
        return row

    def update_template(self, template_id: str, payload: TemplateUpdate) -> dict[str, Any]:
        template = self.db.query_one(
            "SELECT * FROM article_template WHERE id=? AND workspace_id=?",
            (template_id, "ws_default"),
        )
        if template is None:
            raise ServiceError("Template not found", 404)
        if template["source"] == "BUILTIN":
            raise ServiceError("Built-in templates cannot be edited", 403)
        duplicate = self.db.query_one(
            """
            SELECT id FROM article_template
            WHERE workspace_id=? AND name=? AND id<>?
            """,
            ("ws_default", payload.name, template_id),
        )
        if duplicate is not None:
            raise ServiceError("Template name already exists", 409)
        if payload.official_account_id:
            account = self.db.query_one(
                "SELECT id FROM official_account WHERE id=?", (payload.official_account_id,)
            )
            if account is None:
                raise ServiceError("Official account not found", 404)

        now = utc_now()
        with self.db.tx():
            self.db.execute(
                """
                INSERT OR IGNORE INTO article_template_version
                (id,template_id,version,name,description,official_account_id,source,
                 structure_json,style_json,references_json,change_summary,created_by,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    new_id("tplver"),
                    template["id"],
                    template["version"],
                    template["name"],
                    template["description"],
                    template["official_account_id"],
                    template["source"],
                    template["structure_json"],
                    template["style_json"],
                    template["references_json"],
                    payload.change_summary,
                    template["created_by"],
                    template["updated_at"],
                ),
            )
            self.db.execute(
                """
                UPDATE article_template
                SET name=?,description=?,official_account_id=?,structure_json=?,style_json=?,
                    version=version+1,updated_at=?
                WHERE id=?
                """,
                (
                    payload.name,
                    payload.description,
                    payload.official_account_id,
                    json.dumps(
                        [section.model_dump() for section in payload.sections], ensure_ascii=False
                    ),
                    json.dumps(payload.style.model_dump(), ensure_ascii=False),
                    now,
                    template_id,
                ),
            )
        self.audit(
            "template.update",
            "article_template",
            template_id,
            {"from_version": template["version"], "to_version": template["version"] + 1},
        )
        row = self.db.query_one("SELECT * FROM article_template WHERE id = ?", (template_id,))
        assert row is not None
        return row

    def list_template_versions(self, template_id: str) -> list[dict[str, Any]]:
        template = self.db.query_one(
            "SELECT id FROM article_template WHERE id=? AND workspace_id=?",
            (template_id, "ws_default"),
        )
        if template is None:
            raise ServiceError("Template not found", 404)
        return self.db.query_all(
            """
            SELECT * FROM article_template_version
            WHERE template_id=?
            ORDER BY version DESC
            """,
            (template_id,),
        )

    def search_template_references(self, payload: TemplateSearchRequest) -> dict[str, Any]:
        if self.search_provider is None:
            raise ServiceError("Template web search is disabled", 503)
        try:
            references = self.search_provider.search(
                payload.query,
                self.settings.search_max_results,
            )
        except WebSearchError as exc:
            raise ServiceError(f"Template web search failed: {exc}", 502) from exc
        return {
            "provider": self.search_provider.name,
            "query": payload.query,
            "results": [reference.to_dict() for reference in references],
        }

    def generate_template(self, payload: TemplateGenerateRequest) -> dict[str, Any]:
        design_session_id = self._template_design_session_id()
        self._enforce_ai_budget(
            self.text_adapter.provider,
            self.text_adapter.name,
        )
        pseudo_session = {
            "title": payload.description[:60],
            "goal": payload.description,
            "audience": "公众号编辑",
            "tone": "清晰、可执行、保留编辑偏好",
            "requirements": json.dumps(
                ["输出公众号文章模板结构", "每个段落说明写作指导", "不编造具体数据"],
                ensure_ascii=False,
            ),
        }
        references: list[dict[str, str]] = []
        search_query = ""
        search_provider_name = None
        if payload.auto_search:
            if self.search_provider is None:
                raise ServiceError("Template web search is disabled", 503)
            search_query = payload.search_query.strip() or f"{payload.description} 论文 公众号 排版"
            try:
                search_results = self.search_provider.search(
                    search_query,
                    self.settings.search_max_results,
                )
            except WebSearchError as exc:
                raise ServiceError(f"Template web search failed: {exc}", 502) from exc
            search_provider_name = self.search_provider.name
            references.extend(
                {
                    **reference.to_dict(),
                    "type": "web_search",
                    "provider": search_provider_name,
                    "query": search_query,
                }
                for reference in search_results
            )
        facts = [f"模板需求: {payload.description}"]
        if payload.reference:
            facts.append(f"参考风格或内容: {payload.reference[:6000]}")
        for reference in references:
            facts.append(
                "搜索参考: {title} {url} {snippet}".format(**reference)
            )
        context = SessionContext(
            session=pseudo_session,
            assets=[],
            template=None,
            facts=facts,
            image_summaries=[],
        )
        instruction = (
            "为公众号生成文章模板结构。每个章节标题应体现叙事节奏，段落内容作为该章节的写作指导，"
            "不要虚构论文或产品事实。"
        )
        try:
            generation = self.text_adapter.generate(context, instruction)
            with self.db.tx():
                self.db.execute(
                    """
                    INSERT INTO ai_run
                    (id,session_id,task_type,provider,model,input_tokens,output_tokens,latency_ms,cost,status,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        new_id("ai"),
                        design_session_id,
                        "TEMPLATE",
                        generation.provider,
                        generation.model,
                        generation.input_tokens,
                        generation.output_tokens,
                        generation.latency_ms,
                        self._ai_call_cost(
                            generation.provider,
                            generation.model,
                            generation.input_tokens,
                            generation.output_tokens,
                        ),
                        "SUCCESS",
                        utc_now(),
                    ),
                )
        except Exception as exc:
            provider = getattr(exc, "provider", self.text_adapter.provider)
            model = getattr(exc, "model", self.text_adapter.name)
            input_tokens = getattr(exc, "input_tokens", max(1, len(payload.description) // 4))
            with self.db.tx():
                self.db.execute(
                    """
                    INSERT INTO ai_run
                    (id,session_id,task_type,provider,model,input_tokens,output_tokens,latency_ms,cost,status,created_at)
                    VALUES (?,?,?,?,?,?,0,0,0,?,?)
                    """,
                    (
                        new_id("ai"),
                        design_session_id,
                        "TEMPLATE",
                        provider,
                        model,
                        input_tokens,
                        "FAILED",
                        utc_now(),
                    ),
                )
            if isinstance(exc, GenerationError):
                raise ServiceError(str(exc), 502) from exc
            raise

        source = "AI_REFERENCE" if payload.reference or references else "AI_DESCRIPTION"
        base_name = generation.article.title[:80] or payload.description[:40]
        name = f"AI：{base_name}"[:100]
        suffix = 2
        while self.db.query_one(
            "SELECT id FROM article_template WHERE workspace_id=? AND name=?",
            ("ws_default", name),
        ):
            name = f"AI：{base_name} {suffix}"[:100]
            suffix += 1
        first_policy = "required" if any(word in payload.description for word in ["论文", "图表", "配图"]) else "optional"
        sections = []
        for index, section in enumerate(generation.article.sections[:8], start=1):
            sections.append(
                {
                    "id": section.id or f"sec_{index:02d}",
                    "heading": section.heading,
                    "guidance": " ".join(section.paragraphs)[:1000],
                    "image_policy": first_policy if index == 1 else "optional",
                }
            )
        while len(sections) < 3:
            sections.append(
                {
                    "id": f"sec_{len(sections) + 1:02d}",
                    "heading": "补充段落",
                    "guidance": "围绕模板目标补充写作指导。",
                    "image_policy": "optional",
                }
            )
        template_id = new_id("tpl")
        now = utc_now()
        with self.db.tx():
            self.db.execute(
                """
                INSERT INTO article_template
                (id,workspace_id,official_account_id,name,description,source,
                 structure_json,style_json,references_json,status,version,created_by,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,'ACTIVE',1,'ai-template',?,?)
                """,
                (
                    template_id,
                    "ws_default",
                    payload.official_account_id,
                    name,
                    payload.description[:1000],
                    source,
                    json.dumps(sections, ensure_ascii=False),
                    "{}",
                    json.dumps(references, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        self.audit(
            "template.generate",
            "article_template",
            template_id,
            {
                "source": source,
                "provider": generation.provider,
                "model": generation.model,
                "search_provider": search_provider_name,
                "search_query": search_query,
                "reference_count": len(references),
            },
        )
        row = self.db.query_one("SELECT * FROM article_template WHERE id = ?", (template_id,))
        assert row is not None
        return row

    def _template_design_session_id(self) -> str:
        row = self.db.query_one(
            "SELECT id FROM content_session WHERE title=? ORDER BY created_at LIMIT 1",
            ("模板设计工作台",),
        )
        if row:
            return row["id"]
        session_id = new_id("session")
        now = utc_now()
        with self.db.tx():
            self.db.execute(
                """
                INSERT INTO content_session
                (id,workspace_id,official_account_id,title,status,goal,audience,tone,requirements,brief,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    session_id,
                    "ws_default",
                    "oa_default",
                    "模板设计工作台",
                    "OPEN",
                    "沉淀公众号文章模板与写作规范",
                    "公众号编辑",
                    "清晰、可执行",
                    "[]",
                    "{}",
                    now,
                    now,
                ),
            )
        return session_id

    def list_articles(self) -> list[dict[str, Any]]:
        return self.db.query_all(
            """
            SELECT a.id,a.session_id,a.title,a.summary,a.status,a.created_at,a.updated_at,
                   COALESCE(
                       a.cover_asset_id,
                       (
                           SELECT sa.id FROM session_asset sa
                           WHERE sa.session_id=a.session_id
                             AND sa.asset_type='IMAGE'
                             AND sa.processing_status IN ('READY','VISION_READY')
                           ORDER BY sa.created_at LIMIT 1
                       )
                   ) AS preview_asset_id,
                   s.title AS session_title,o.name AS account_name
            FROM article a
            JOIN content_session s ON s.id = a.session_id
            JOIN official_account o ON o.id = s.official_account_id
            ORDER BY a.updated_at DESC LIMIT 100
            """
        )

    def list_audit_logs(self, limit: int = 100) -> list[dict[str, Any]]:
        safe_limit = max(1, min(limit, 200))
        return self.db.query_all(
            "SELECT * FROM audit_log ORDER BY created_at DESC LIMIT ?", (safe_limit,)
        )

    def list_workflow_page(self, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        safe_page = max(1, page)
        safe_size = max(1, min(page_size, 100))
        total_row = self.db.query_one("SELECT COUNT(*) AS value FROM workflow_run")
        total = int(total_row["value"] or 0) if total_row else 0
        total_pages = (total + safe_size - 1) // safe_size
        current_page = min(safe_page, total_pages or 1)
        items = self.db.query_all(
            """
            SELECT w.*, s.title AS session_title
            FROM workflow_run w JOIN content_session s ON s.id=w.session_id
            ORDER BY w.started_at DESC
            LIMIT ? OFFSET ?
            """,
            (safe_size, (current_page - 1) * safe_size),
        )
        return {
            "items": items,
            "page": current_page,
            "page_size": safe_size,
            "total": total,
            "total_pages": total_pages,
        }

    def list_audit_page(self, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        safe_page = max(1, page)
        safe_size = max(1, min(page_size, 100))
        total_row = self.db.query_one("SELECT COUNT(*) AS value FROM audit_log")
        total = int(total_row["value"] or 0) if total_row else 0
        total_pages = (total + safe_size - 1) // safe_size
        current_page = min(safe_page, total_pages or 1)
        items = self.db.query_all(
            """
            SELECT * FROM audit_log
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            """,
            (safe_size, (current_page - 1) * safe_size),
        )
        return {
            "items": items,
            "page": current_page,
            "page_size": safe_size,
            "total": total,
            "total_pages": total_pages,
        }

    def dashboard(self) -> dict[str, Any]:
        def count(sql: str) -> int:
            row = self.db.query_one(sql)
            return int(row["value"] if "value" in row else list(row.values())[0])

        def sum_float(sql: str, params: tuple[Any, ...] = ()) -> float:
            row = self.db.query_one(sql, params)
            return float(row["value"] or 0) if row else 0.0

        month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
        ai_month_cost = sum_float(
            "SELECT COALESCE(SUM(cost),0) AS value FROM ai_run WHERE created_at LIKE ?",
            (f"{month_prefix}%",),
        )
        budget = self.settings.text_monthly_budget_usd

        return {
            "drafts": count("SELECT COUNT(*) AS value FROM article WHERE status IN ('DRAFT','AI_REVIEWED','NEEDS_REVISION')"),
            "waiting_human": count("SELECT COUNT(*) AS value FROM article WHERE status='NEEDS_REVISION'"),
            "published": count("SELECT COUNT(*) AS value FROM publish_job WHERE status='PUBLISHED'"),
            "scheduled": count("SELECT COUNT(*) AS value FROM publish_job WHERE status='SCHEDULED'"),
            "running": count("SELECT COUNT(*) AS value FROM workflow_run WHERE status='RUNNING'"),
            "queued": count("SELECT COUNT(*) AS value FROM workflow_queue WHERE status='QUEUED'"),
            "sessions": count("SELECT COUNT(*) AS value FROM content_session"),
            "accounts": count("SELECT COUNT(*) AS value FROM official_account"),
            "ai_tokens": count("SELECT COALESCE(SUM(input_tokens+output_tokens),0) AS value FROM ai_run"),
            "ai_cost": sum_float("SELECT COALESCE(SUM(cost),0) AS value FROM ai_run"),
            "ai_cost_current_month": ai_month_cost,
            "ai_monthly_budget_usd": budget,
            "ai_budget_usage_percent": (
                round(ai_month_cost / budget * 100, 2) if budget > 0 else 0.0
            ),
            "health": 100 if count("SELECT COUNT(*) AS value FROM workflow_run WHERE status='FAILED'") == 0 else 82,
            "recent_workflows": self.db.query_all(
                """
                SELECT w.*, s.title AS session_title
                FROM workflow_run w JOIN content_session s ON s.id=w.session_id
                ORDER BY w.started_at DESC LIMIT 10
                """
            ),
        }

    def system_health(self) -> dict[str, Any]:
        from datetime import datetime, timezone

        started = time.perf_counter()
        alerts: list[dict[str, str]] = []

        database_status = "HEALTHY"
        database_detail = "ok"
        try:
            check = self.db.query_one("PRAGMA quick_check")
            database_detail = str(check["quick_check"] if check else "no result")
            if database_detail.lower() != "ok":
                database_status = "UNHEALTHY"
                alerts.append({
                    "severity": "CRITICAL",
                    "code": "DB_CHECK_FAILED",
                    "message": f"SQLite quick_check returned {database_detail}",
                    "suggestion": "Stop writes and inspect the database before continuing.",
                })
        except Exception as exc:
            database_status = "UNHEALTHY"
            database_detail = str(exc)
            alerts.append({
                "severity": "CRITICAL",
                "code": "DB_UNAVAILABLE",
                "message": f"Database check failed: {exc}",
                "suggestion": "Check database volume, permissions, and connection health.",
            })

        storage_status = "HEALTHY"
        try:
            self.settings.storage_path.mkdir(parents=True, exist_ok=True)
            probe = self.settings.storage_path / ".wanren_healthcheck"
            probe.write_bytes(b"ok")
            if probe.read_bytes() != b"ok":
                raise IOError("storage verification mismatch")
            probe.unlink(missing_ok=True)
        except Exception as exc:
            storage_status = "UNHEALTHY"
            alerts.append({
                "severity": "CRITICAL",
                "code": "STORAGE_NOT_WRITABLE",
                "message": f"Storage write check failed: {exc}",
                "suggestion": "Check the storage volume and process permissions.",
            })

        queue_rows = {
            row["status"]: int(row["value"])
            for row in self.db.query_all(
                "SELECT status, COUNT(*) AS value FROM workflow_queue GROUP BY status"
            )
        }
        queued = queue_rows.get("QUEUED", 0)
        queue_running = queue_rows.get("RUNNING", 0)
        queue_failed = queue_rows.get("FAILED", 0)
        due_scheduled = int(
            self.db.query_one(
                """
                SELECT COUNT(*) AS value
                FROM publish_job p JOIN article a ON a.id=p.article_id
                WHERE p.status='SCHEDULED' AND p.scheduled_at <= ?
                  AND a.status='HUMAN_APPROVED'
                """,
                (utc_now(),),
            )["value"]
        )
        now = datetime.now(timezone.utc)
        stale_running = 0
        for row in self.db.query_all(
            "SELECT locked_at FROM workflow_queue WHERE status = 'RUNNING' AND locked_at IS NOT NULL"
        ):
            try:
                locked_at = datetime.fromisoformat(row["locked_at"])
                if locked_at.tzinfo is None:
                    locked_at = locked_at.replace(tzinfo=timezone.utc)
                if (now - locked_at).total_seconds() > 300:
                    stale_running += 1
            except ValueError:
                stale_running += 1

        if stale_running:
            alerts.append({
                "severity": "WARNING",
                "code": "WORKFLOW_WORKER_STALE",
                "message": f"{stale_running} running workflow(s) have not heartbeat-updated for over 5 minutes",
                "suggestion": "Check standalone and API workers; retry or requeue stale jobs.",
            })
        if queued >= 10:
            alerts.append({
                "severity": "WARNING",
                "code": "WORKFLOW_QUEUE_BACKLOG",
                "message": f"{queued} workflows are waiting for a worker",
                "suggestion": "Add worker processes or inspect long-running generation tasks.",
            })
        if queue_failed:
            alerts.append({
                "severity": "WARNING",
                "code": "WORKFLOW_QUEUE_FAILED",
                "message": f"{queue_failed} workflow queue item(s) failed",
                "suggestion": "Inspect workflow node errors and retry after fixing the cause.",
            })
        if due_scheduled:
            alerts.append({
                "severity": "WARNING",
                "code": "PUBLISH_SCHEDULE_DUE",
                "message": f"{due_scheduled} approved scheduled publish job(s) are due",
                "suggestion": "Check standalone/API Worker availability and publish adapter credentials.",
            })

        workflow_rows = {
            row["status"]: int(row["value"])
            for row in self.db.query_all(
                "SELECT status, COUNT(*) AS value FROM workflow_run GROUP BY status"
            )
        }
        workflow_total = sum(workflow_rows.values())
        workflow_success = workflow_rows.get("SUCCESS", 0)
        workflow_failed = workflow_rows.get("FAILED", 0)
        if workflow_failed:
            alerts.append({
                "severity": "WARNING",
                "code": "WORKFLOW_RUN_FAILED",
                "message": f"{workflow_failed} workflow run(s) failed",
                "suggestion": "Open monitor workflow traces and inspect failed nodes.",
            })

        callback_rows = {
            row["status"]: int(row["value"])
            for row in self.db.query_all(
                "SELECT status, COUNT(*) AS value FROM wechat_callback_event GROUP BY status"
            )
        }
        callback_total = sum(callback_rows.values())
        callback_failed = callback_rows.get("FAILED", 0)
        callback_duplicate_row = self.db.query_one(
            "SELECT COALESCE(SUM(duplicate_count),0) AS value FROM wechat_callback_event"
        )
        callback_duplicates = int(callback_duplicate_row["value"] or 0) if callback_duplicate_row else 0
        if callback_failed:
            alerts.append({
                "severity": "WARNING",
                "code": "WECHAT_CALLBACK_FAILED",
                "message": f"{callback_failed} WeChat callback event(s) failed",
                "suggestion": "Inspect callback last_error, fix the cause, and let WeChat retry the event.",
            })

        if self.settings.publisher == "wechat" and not self.settings.wechat_app_secret:
            alerts.append({
                "severity": "WARNING",
                "code": "WECHAT_CREDENTIAL_MISSING",
                "message": "WeChat publisher is selected but the default secret is missing",
                "suggestion": "Configure WECHAT_APP_SECRET or the account-specific secret environment variable.",
            })

        ai_rows = {
            row["status"]: int(row["value"])
            for row in self.db.query_all(
                "SELECT status, COUNT(*) AS value FROM ai_run GROUP BY status"
            )
        }
        ai_total = sum(ai_rows.values())
        ai_failed = ai_rows.get("FAILED", 0)
        if ai_failed:
            alerts.append({
                "severity": "WARNING",
                "code": "AI_RUN_FAILED",
                "message": f"{ai_failed} AI generation call(s) failed",
                "suggestion": "Inspect provider credentials, network access, response schema, and model retries.",
            })
        ai_month_cost = self._current_month_ai_cost()
        ai_budget = self.settings.text_monthly_budget_usd
        if ai_budget > 0:
            usage = ai_month_cost / ai_budget
            if usage >= 1:
                alerts.append({
                    "severity": "CRITICAL",
                    "code": "AI_BUDGET_EXCEEDED",
                    "message": f"AI month cost ${ai_month_cost:.8f} exceeds budget ${ai_budget:.8f}",
                    "suggestion": "Raise the budget after review or pause AI generation.",
                })
            elif usage >= 0.8:
                alerts.append({
                    "severity": "WARNING",
                    "code": "AI_BUDGET_NEAR_LIMIT",
                    "message": f"AI month cost has reached {usage * 100:.1f}% of budget",
                    "suggestion": "Review model usage and pricing before the budget is exhausted.",
                })

        score = 100
        for alert in alerts:
            score -= 30 if alert["severity"] == "CRITICAL" else 8
        score = max(0, score)
        status = "UNHEALTHY" if database_status != "HEALTHY" or storage_status != "HEALTHY" else ("DEGRADED" if alerts else "HEALTHY")
        ai_row = self.db.query_one(
            """
            SELECT COUNT(*) AS runs, COALESCE(SUM(input_tokens+output_tokens),0) AS tokens
            FROM ai_run
            """
        )
        return {
            "checked_at": utc_now(),
            "status": status,
            "score": score,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "components": {
                "database": {"status": database_status, "detail": database_detail},
                "storage": {"status": storage_status, "path": str(self.settings.storage_path)},
                "queue": {
                    "status": "HEALTHY" if queued < 10 and not stale_running and not queue_failed else "DEGRADED",
                    "queued": queued,
                    "running": queue_running,
                    "scheduled": int(self.db.query_one("SELECT COUNT(*) AS value FROM publish_job WHERE status='SCHEDULED'")["value"]),
                    "due_scheduled": due_scheduled,
                    "stale_running": stale_running,
                    "failed": queue_failed,
                },
                "workflow": {
                    "status": "HEALTHY" if workflow_failed == 0 else "DEGRADED",
                    "total": workflow_total,
                    "success": workflow_success,
                    "failed": workflow_failed,
                    "success_rate": round(workflow_success / workflow_total * 100, 2) if workflow_total else 100.0,
                },
                "ai": {
                    "status": "HEALTHY" if ai_failed == 0 else "DEGRADED",
                    "runs": int(ai_row["runs"]) if ai_row else 0,
                    "tokens": int(ai_row["tokens"]) if ai_row else 0,
                    "month_cost_usd": round(ai_month_cost, 8),
                    "monthly_budget_usd": ai_budget,
                    "failed": ai_failed,
                    "success_rate": round((ai_total - ai_failed) / ai_total * 100, 2) if ai_total else 100.0,
                    "provider": self.settings.text_provider,
                    "model": self.settings.text_model or self.text_adapter.name,
                },
                "publisher": {
                    "status": "DEGRADED" if callback_failed else ("READY" if self.settings.publisher == "mock" else "CONFIGURED"),
                    "mode": self.settings.publisher,
                    "callback_events": callback_total,
                    "callback_processed": callback_rows.get("PROCESSED", 0),
                    "callback_processing": callback_rows.get("PROCESSING", 0),
                    "callback_failed": callback_failed,
                    "callback_duplicates": callback_duplicates,
                },
                "auth": {"status": "READY", "mode": self.settings.auth_mode},
            },
            "alerts": alerts,
        }

    def prometheus_metrics(self) -> str:
        def count(sql: str) -> int:
            row = self.db.query_one(sql)
            return int(row["value"])

        workflow_rows = {
            row["status"]: int(row["value"])
            for row in self.db.query_all(
                "SELECT status, COUNT(*) AS value FROM workflow_run GROUP BY status"
            )
        }
        queue_rows = {
            row["status"]: int(row["value"])
            for row in self.db.query_all(
                "SELECT status, COUNT(*) AS value FROM workflow_queue GROUP BY status"
            )
        }
        ai = self.db.query_one(
            "SELECT COUNT(*) AS runs, COALESCE(SUM(input_tokens+output_tokens),0) AS tokens FROM ai_run"
        )
        ai_cost = self.db.query_one("SELECT COALESCE(SUM(cost),0) AS value FROM ai_run")
        ai_metric_rows = {
            row["status"]: int(row["value"])
            for row in self.db.query_all(
                "SELECT status, COUNT(*) AS value FROM ai_run GROUP BY status"
            )
        }
        callback_metric_rows = {
            row["status"]: int(row["value"])
            for row in self.db.query_all(
                "SELECT status, COUNT(*) AS value FROM wechat_callback_event GROUP BY status"
            )
        }
        callback_duplicate_row = self.db.query_one(
            "SELECT COALESCE(SUM(duplicate_count),0) AS value FROM wechat_callback_event"
        )
        lines = [
            "# HELP wanren_build_info Build and runtime mode information.",
            "# TYPE wanren_build_info gauge",
            f'wanren_build_info{{publisher="{self.settings.publisher}",auth="{self.settings.auth_mode}",workflow="{self.settings.workflow_mode}"}} 1',
            "# HELP wanren_sessions_total Total content sessions.",
            "# TYPE wanren_sessions_total counter",
            f"wanren_sessions_total {count('SELECT COUNT(*) AS value FROM content_session')}",
            "# HELP wanren_articles_total Total generated articles.",
            "# TYPE wanren_articles_total counter",
            f"wanren_articles_total {count('SELECT COUNT(*) AS value FROM article')}",
            "# HELP wanren_workflow_runs_total Workflow runs by status.",
            "# TYPE wanren_workflow_runs_total counter",
            f"wanren_workflow_runs_total{{status=\"SUCCESS\"}} {workflow_rows.get('SUCCESS', 0)}",
            f"wanren_workflow_runs_total{{status=\"FAILED\"}} {workflow_rows.get('FAILED', 0)}",
            f"wanren_workflow_runs_total{{status=\"RUNNING\"}} {workflow_rows.get('RUNNING', 0)}",
            "# HELP wanren_workflow_queue_jobs Workflow queue jobs by status.",
            "# TYPE wanren_workflow_queue_jobs gauge",
            f"wanren_workflow_queue_jobs{{status=\"QUEUED\"}} {queue_rows.get('QUEUED', 0)}",
            f"wanren_workflow_queue_jobs{{status=\"RUNNING\"}} {queue_rows.get('RUNNING', 0)}",
            f"wanren_workflow_queue_jobs{{status=\"FAILED\"}} {queue_rows.get('FAILED', 0)}",
            "# HELP wanren_wechat_callback_events_total WeChat callback events by status.",
            "# TYPE wanren_wechat_callback_events_total counter",
            f"wanren_wechat_callback_events_total{{status=\"PROCESSED\"}} {callback_metric_rows.get('PROCESSED', 0)}",
            f"wanren_wechat_callback_events_total{{status=\"PROCESSING\"}} {callback_metric_rows.get('PROCESSING', 0)}",
            f"wanren_wechat_callback_events_total{{status=\"FAILED\"}} {callback_metric_rows.get('FAILED', 0)}",
            "# HELP wanren_wechat_callback_duplicate_deliveries_total Duplicate WeChat callback deliveries.",
            "# TYPE wanren_wechat_callback_duplicate_deliveries_total counter",
            f"wanren_wechat_callback_duplicate_deliveries_total {int(callback_duplicate_row['value'] or 0) if callback_duplicate_row else 0}",
            "# HELP wanren_ai_runs_total Total AI calls.",
            "# TYPE wanren_ai_runs_total counter",
            f"wanren_ai_runs_total{{status=\"SUCCESS\"}} {ai_metric_rows.get('SUCCESS', 0)}",
            f"wanren_ai_runs_total{{status=\"FAILED\"}} {ai_metric_rows.get('FAILED', 0)}",
            "# HELP wanren_ai_tokens_total Total input and output tokens.",
            "# TYPE wanren_ai_tokens_total counter",
            f"wanren_ai_tokens_total {int(ai['tokens']) if ai else 0}",
            "# HELP wanren_ai_cost_usd_total Total model cost in USD.",
            "# TYPE wanren_ai_cost_usd_total counter",
            f"wanren_ai_cost_usd_total {float(ai_cost['value'] or 0) if ai_cost else 0:.8f}",
            "",
        ]
        return "\n".join(lines)

    def login(self, payload: LoginRequest) -> dict[str, Any]:
        user = self.db.query_one(
            "SELECT * FROM app_user WHERE workspace_id = ? AND email = ?",
            ("ws_default", payload.email.lower()),
        )
        if user is None or user["status"] != "ACTIVE" or not verify_password(
            payload.password, user["password_hash"]
        ):
            raise ServiceError("Invalid email or password", 401)

        from .auth import new_token

        token = new_token()
        now = utc_now()
        with self.db.tx():
            self.db.execute(
                "INSERT INTO auth_session(token_hash,user_id,expires_at,created_at) VALUES (?,?,?,?)",
                (token_digest(token), user["id"], token_expiry(), now),
            )
            self.db.execute(
                "UPDATE app_user SET last_login_at = ? WHERE id = ?", (now, user["id"])
            )
        self.audit("auth.login", "user", user["id"], {"email": user["email"]})
        return {"token": token, "user": self.public_user(user)}

    def public_auth_config(self) -> dict[str, Any]:
        return {
            "auth_mode": self.settings.auth_mode,
            "registration_enabled": self.settings.registration_enabled,
            "registration_role": self.settings.registration_role,
        }

    def register(self, payload: RegisterRequest) -> dict[str, Any]:
        import re

        if not self.settings.registration_enabled:
            raise ServiceError("Registration is disabled", 403)
        email = payload.email.strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise ServiceError("Invalid email address", 400)
        if payload.password != payload.confirm_password:
            raise ServiceError("Passwords do not match", 400)
        if not re.search(r"[A-Za-z]", payload.password) or not re.search(r"\d", payload.password):
            raise ServiceError("Password must contain letters and numbers", 400)
        if payload.password.lower() in {
            "password1", "wanren123", "12345678", "qwerty123", "admin1234"
        }:
            raise ServiceError("This password is too common", 400)
        if self.db.query_one(
            "SELECT id FROM app_user WHERE workspace_id = ? AND email = ?",
            ("ws_default", email),
        ):
            raise ServiceError("Email already registered", 409)

        user_id = new_id("user")
        display_name = payload.name.strip() or email.split("@", 1)[0][:100]
        with self.db.tx():
            self.db.execute(
                """
                INSERT INTO app_user
                (id,workspace_id,email,name,password_hash,role,status,created_at)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    user_id,
                    "ws_default",
                    email,
                    display_name,
                    hash_password(payload.password),
                    self.settings.registration_role,
                    "ACTIVE",
                    utc_now(),
                ),
            )
        self.audit("auth.register", "user", user_id, {"email": email, "role": self.settings.registration_role})
        login_payload = LoginRequest(email=email, password=payload.password)
        result = self.login(login_payload)
        result["created"] = True
        return result

    def logout(self, token: str) -> dict[str, Any]:
        with self.db.tx():
            self.db.execute(
                "DELETE FROM auth_session WHERE token_hash = ?", (token_digest(token),)
            )
        return {"ok": True}

    def user_from_token(self, token: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM auth_session WHERE token_hash = ?", (token_digest(token),)
        )
        if row is None or token_expired(row["expires_at"]):
            return None
        user = self.db.query_one("SELECT * FROM app_user WHERE id = ?", (row["user_id"],))
        if user is None or user["status"] != "ACTIVE":
            return None
        return self.public_user(user)

    @staticmethod
    def public_user(user: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": user["id"],
            "email": user["email"],
            "name": user["name"],
            "role": user["role"],
            "permissions": permissions_for_role(user["role"]),
            "authenticated": True,
            "last_login_at": user.get("last_login_at"),
        }

    def require_permission(self, user: dict[str, Any] | None, permission: str) -> None:
        if not has_permission(user, permission):
            raise ServiceError(f"Permission denied: {permission}", 403)

    def create_user(self, actor: dict[str, Any], payload: UserCreate) -> dict[str, Any]:
        self.require_permission(actor, "user.manage")
        user_id = new_id("user")
        with self.db.tx():
            self.db.execute(
                """
                INSERT INTO app_user
                (id,workspace_id,email,name,password_hash,role,status,created_at)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    user_id,
                    "ws_default",
                    payload.email.lower(),
                    payload.name,
                    hash_password(payload.password),
                    payload.role,
                    "ACTIVE",
                    utc_now(),
                ),
            )
        self.audit("user.create", "user", user_id, {"email": payload.email, "role": payload.role})
        row = self.db.query_one("SELECT * FROM app_user WHERE id = ?", (user_id,))
        assert row is not None
        return self.public_user(row)

    def list_users(self, actor: dict[str, Any]) -> list[dict[str, Any]]:
        self.require_permission(actor, "user.manage")
        return [self.public_user(row) for row in self.db.query_all("SELECT * FROM app_user ORDER BY created_at")]
