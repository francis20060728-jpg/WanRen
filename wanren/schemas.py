from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class SessionCreate(BaseModel):
    official_account_id: str | None = Field(default=None, max_length=100)
    template_id: str | None = Field(default=None, max_length=100)
    title: str = Field(min_length=2, max_length=120)
    goal: str = Field(default="", max_length=2000)
    audience: str = Field(default="", max_length=500)
    tone: str = Field(default="专业、清晰、克制", max_length=200)
    requirements: list[str] = Field(default_factory=list, max_length=30)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=200)


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    name: str = Field(default="", max_length=100)
    password: str = Field(min_length=8, max_length=200)
    confirm_password: str = Field(min_length=8, max_length=200)


class UserCreate(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=8, max_length=200)
    role: Literal["ADMIN", "EDITOR", "REVIEWER", "PUBLISHER", "VIEWER"] = "EDITOR"


class AccountStyle(BaseModel):
    accent: str = Field(default="#175641", pattern=r"^#[0-9a-fA-F]{3,8}$")
    text: str = Field(default="#262626", pattern=r"^#[0-9a-fA-F]{3,8}$")
    font: Literal["system", "serif", "sans"] = "system"


class AccountCreate(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    app_id: str = Field(min_length=3, max_length=100)
    style: AccountStyle = AccountStyle()
    callback_base_url: str | None = Field(default=None, max_length=300)


class AccountUpdate(BaseModel):
    name: str = Field(min_length=2, max_length=100)


class TemplateSectionInput(BaseModel):
    id: str = Field(min_length=3, max_length=60, pattern=r"^[a-zA-Z0-9_-]+$")
    heading: str = Field(min_length=1, max_length=120)
    guidance: str = Field(default="", max_length=1000)
    image_policy: Literal["none", "optional", "required"] = "optional"


class TemplateCreate(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    description: str = Field(default="", max_length=1000)
    official_account_id: str | None = Field(default=None, max_length=100)
    sections: list[TemplateSectionInput] = Field(min_length=3, max_length=8)
    style: AccountStyle = AccountStyle()


class TemplateUpdate(TemplateCreate):
    change_summary: str = Field(default="更新模板", min_length=2, max_length=500)


class TemplateSearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=300)


class TemplateGenerateRequest(BaseModel):
    description: str = Field(min_length=5, max_length=4000)
    reference: str = Field(default="", max_length=8000)
    search_query: str = Field(default="", max_length=300)
    auto_search: bool = False
    official_account_id: str | None = Field(default=None, max_length=100)


class AssetAnalyzeRequest(BaseModel):
    prompt: str | None = Field(default=None, max_length=2000)


class VideoTranscodeRequest(BaseModel):
    preset: Literal["wechat_720p", "preview_360p"] = "wechat_720p"


class GenerateRequest(BaseModel):
    instruction: str = Field(default="", max_length=4000)


class VisionSettingsUpdate(BaseModel):
    provider: str = "multimodal"
    api_base: str = ""
    api_key: str = ""
    model: str = ""
    # VidLens 内部厂家（可选；仅在提供时更新）
    vidlens_api_url: str | None = None
    vidlens_api_key: str | None = None
    vidlens_model: str | None = None


class AssistToolResult(BaseModel):
    status: str = "ok"
    message: str = ""
    document: Any = None
    # 本次工具新增/替换的段落文本（入库供历史消息点击回放高亮）
    changed_blocks: list[str] = Field(default_factory=list, max_length=40)


class ConnectionTestRequest(BaseModel):
    target: str = "vision"


class TextSettingsUpdate(BaseModel):
    provider: str = "local"
    api_base: str = ""
    api_key: str = ""
    model: str = ""


class ArticleChatRequest(BaseModel):
    instruction: str = Field(min_length=2, max_length=4000)
    # 随消息发送的素材（已上传）：[{id, name, asset_type}]
    attachments: list[dict[str, Any]] = Field(default_factory=list, max_length=20)


class ArticleSectionInput(BaseModel):
    id: str = Field(min_length=3, max_length=60, pattern=r"^[a-zA-Z0-9_-]+$")
    heading: str = Field(min_length=1, max_length=120)
    paragraphs: list[str] = Field(min_length=1, max_length=30)
    asset_refs: list[str] = Field(default_factory=list, max_length=50)
    style: dict[str, Any] = Field(default_factory=dict)


class ArticleUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    summary: str = Field(default="", max_length=300)
    sections: list[ArticleSectionInput] = Field(min_length=1, max_length=30)
    change_summary: str = Field(default="人工编辑", max_length=500)
    typesetting_id: str | None = Field(default=None, max_length=40)


class ConfirmProposalRequest(BaseModel):
    style_id: str | None = Field(default=None, max_length=40)


class StyleImportRequest(BaseModel):
    name: str = Field(min_length=2, max_length=40)
    vibe: str = Field(default="", max_length=60)
    suits: str = Field(default="", max_length=120)
    tokens: dict[str, Any] = Field(default_factory=dict)


class RevertRequest(BaseModel):
    to_version: int = Field(ge=1, le=9999)


class ReviewRequest(BaseModel):
    reviewer: str = Field(default="local-operator", max_length=100)


class OriginalDeclarationRequest(BaseModel):
    reviewer: str = Field(default="local-operator", max_length=100)
    confirmed: bool = True


class PublishRequest(BaseModel):
    mode: Literal["draft", "publish"] = "draft"


class SchedulePublishRequest(BaseModel):
    scheduled_at: str = Field(min_length=20, max_length=40)
    timezone: str = Field(default="", max_length=64)


class APIResponse(BaseModel):
    ok: bool = True
    data: Any = None
