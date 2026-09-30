from __future__ import annotations

import os
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    db_path: Path
    storage_path: Path
    publisher: str
    max_upload_bytes: int
    vidlens_path: Path
    wechat_app_id: str
    wechat_app_secret: str
    workflow_mode: str
    worker_poll_seconds: float
    embedded_worker: bool
    auth_mode: str
    admin_email: str
    admin_password: str
    registration_enabled: bool
    registration_role: str
    callback_token: str
    text_provider: str
    text_model: str
    text_api_base: str
    text_api_key: str
    text_timeout_seconds: float
    # 协作编辑工具循环的思考模式：决策型短轮次默认关闭深度思考（大幅降低每轮延迟）
    assist_thinking: str
    text_max_retries: int
    text_fallback_local: bool
    text_input_price_per_million: float
    text_output_price_per_million: float
    text_model_prices: dict[tuple[str, str], tuple[float, float]]
    text_monthly_budget_usd: float
    search_provider: str
    search_timeout_seconds: float
    search_max_results: int
    vision_provider: str
    vision_api_base: str
    vision_api_key: str
    vision_model: str


def load_settings(env: dict[str, str] | None = None) -> Settings:
    source = env or os.environ
    root = Path(__file__).resolve().parent.parent
    publisher = source.get("WANREN_PUBLISHER", "mock").lower()
    if publisher not in {"mock", "wechat"}:
        raise ValueError("WANREN_PUBLISHER must be mock or wechat")
    workflow_mode = source.get("WANREN_WORKFLOW_MODE", "background").lower()
    if workflow_mode not in {"inline", "background"}:
        raise ValueError("WANREN_WORKFLOW_MODE must be inline or background")
    auth_mode = source.get("WANREN_AUTH_MODE", "dev").lower()
    if auth_mode not in {"disabled", "dev", "production"}:
        raise ValueError("WANREN_AUTH_MODE must be disabled, dev, or production")
    admin_password = source.get("WANREN_ADMIN_PASSWORD", "")
    if auth_mode == "production" and not admin_password:
        raise ValueError("WANREN_ADMIN_PASSWORD is required in production auth mode")
    if auth_mode == "dev" and not admin_password:
        admin_password = "wanren-dev-admin"
    # 生产必须使用独立加密密钥（用于加密落库的模型 API Key），
    # 避免回退到管理员口令派生（认证口令与加密密钥复用的风险）
    if auth_mode == "production" and not source.get("WANREN_APP_SECRET_KEY", ""):
        raise ValueError("WANREN_APP_SECRET_KEY is required in production auth mode")
    registration_role = source.get("WANREN_REGISTRATION_ROLE", "EDITOR").upper()
    if registration_role not in {"EDITOR", "REVIEWER", "PUBLISHER", "VIEWER"}:
        raise ValueError("WANREN_REGISTRATION_ROLE must be EDITOR, REVIEWER, PUBLISHER, or VIEWER")
    text_provider = source.get("WANREN_TEXT_PROVIDER", "local").lower()
    if text_provider not in {"local", "openai_compatible"}:
        raise ValueError("WANREN_TEXT_PROVIDER must be local or openai_compatible")
    text_model = source.get("WANREN_TEXT_MODEL", "")
    if text_provider == "openai_compatible" and not text_model:
        raise ValueError("WANREN_TEXT_MODEL is required when WANREN_TEXT_PROVIDER=openai_compatible")
    text_api_key = source.get("WANREN_TEXT_API_KEY", "")
    if text_provider == "openai_compatible" and not text_api_key:
        raise ValueError("WANREN_TEXT_API_KEY is required when WANREN_TEXT_PROVIDER=openai_compatible")
    search_provider = source.get("WANREN_SEARCH_PROVIDER", "duckduckgo").lower()
    if search_provider not in {"disabled", "duckduckgo"}:
        raise ValueError("WANREN_SEARCH_PROVIDER must be disabled or duckduckgo")
    vision_provider = source.get("WANREN_VISION_PROVIDER", "multimodal").lower()
    if vision_provider not in {"multimodal", "vidlens"}:
        raise ValueError("WANREN_VISION_PROVIDER must be multimodal or vidlens")
    search_timeout_seconds = float(source.get("WANREN_SEARCH_TIMEOUT_SECONDS", "15"))
    search_max_results = int(source.get("WANREN_SEARCH_MAX_RESULTS", "5"))
    if search_timeout_seconds <= 0 or search_max_results < 1 or search_max_results > 20:
        raise ValueError("Search timeout must be positive and result count must be between 1 and 20")
    registration_default = auth_mode != "production"
    registration_enabled = source.get(
        "WANREN_REGISTRATION_ENABLED", "true" if registration_default else "false"
    ).lower() in {"1", "true", "yes", "on"}
    settings = Settings(
        db_path=Path(source.get("WANREN_DB_PATH", "data/wanren.sqlite3")),
        storage_path=Path(source.get("WANREN_STORAGE_PATH", "storage")),
        publisher=publisher,
        max_upload_bytes=int(source.get("WANREN_MAX_UPLOAD_MB", "20")) * 1024 * 1024,
        vidlens_path=Path(
            str(source.get("WANREN_VIDLENS") or "").strip()
            or str(Path(__file__).resolve().parent.parent / "scripts" / "vidlens" / "vidlens.py")
        ),
        wechat_app_id=source.get("WECHAT_APP_ID", ""),
        wechat_app_secret=source.get("WECHAT_APP_SECRET", ""),
        workflow_mode=workflow_mode,
        worker_poll_seconds=float(source.get("WANREN_WORKER_POLL_SECONDS", "1")),
        embedded_worker=source.get("WANREN_EMBEDDED_WORKER", "true").lower() not in {"0", "false", "no"},
        auth_mode=auth_mode,
        admin_email=source.get("WANREN_ADMIN_EMAIL", "admin@wanren.local"),
        admin_password=admin_password,
        registration_enabled=registration_enabled,
        registration_role=registration_role,
        callback_token=source.get("WANREN_WECHAT_CALLBACK_TOKEN", ""),
        text_provider=text_provider,
        text_model=text_model,
        text_api_base=source.get("WANREN_TEXT_API_BASE", "https://api.openai.com/v1").rstrip("/"),
        text_api_key=text_api_key,
        text_timeout_seconds=float(source.get("WANREN_TEXT_TIMEOUT_SECONDS", "120")),
        # 协作编辑工具循环的思考模式：决策型短轮次默认关闭深度思考（大幅降低每轮延迟）
        assist_thinking=str(source.get("WANREN_ASSIST_THINKING", "disabled")),
        text_max_retries=max(0, int(source.get("WANREN_TEXT_MAX_RETRIES", "2"))),
        text_fallback_local=source.get("WANREN_TEXT_FALLBACK_LOCAL", "false").lower()
        in {"1", "true", "yes", "on"},
        text_input_price_per_million=float(source.get("WANREN_TEXT_INPUT_PRICE_PER_M", "0")),
        text_output_price_per_million=float(source.get("WANREN_TEXT_OUTPUT_PRICE_PER_M", "0")),
        text_model_prices=_parse_model_prices(source.get("WANREN_TEXT_MODEL_PRICES", "")),
        text_monthly_budget_usd=float(source.get("WANREN_TEXT_MONTHLY_BUDGET_USD", "0")),
        search_provider=search_provider,
        search_timeout_seconds=search_timeout_seconds,
        search_max_results=search_max_results,
        vision_provider=vision_provider,
        vision_api_base=source.get("WANREN_VISION_API_BASE", "").rstrip("/"),
        vision_api_key=source.get("WANREN_VISION_API_KEY", ""),
        vision_model=source.get("WANREN_VISION_MODEL", ""),
    )
    if settings.text_input_price_per_million < 0 or settings.text_output_price_per_million < 0:
        raise ValueError("Text prices must be non-negative")
    if any(
        input_price < 0 or output_price < 0
        for input_price, output_price in settings.text_model_prices.values()
    ):
        raise ValueError("Text model prices must be non-negative")
    if settings.text_monthly_budget_usd < 0:
        raise ValueError("WANREN_TEXT_MONTHLY_BUDGET_USD must be non-negative")
    return settings


def _parse_model_prices(raw: str) -> dict[tuple[str, str], tuple[float, float]]:
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("WANREN_TEXT_MODEL_PRICES must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("WANREN_TEXT_MODEL_PRICES must be a JSON object")
    prices: dict[tuple[str, str], tuple[float, float]] = {}
    for key, value in payload.items():
        if not isinstance(value, dict):
            raise ValueError("Each model price must be an object")
        provider, model = key.split("/", 1)
        prices[(provider, model)] = (
            float(value["input_per_million"]),
            float(value["output_per_million"]),
        )
    return prices
