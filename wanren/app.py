from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import load_settings
from .database import Database
from .schemas import AccountCreate, AccountUpdate, ArticleChatRequest, ArticleUpdate, AssetAnalyzeRequest, AssistToolResult, ConfirmProposalRequest, ConnectionTestRequest, GenerateRequest, LoginRequest, OriginalDeclarationRequest, PublishRequest, RegisterRequest, ReviewRequest, RevertRequest, SchedulePublishRequest, SessionCreate, StyleImportRequest, TemplateCreate, TemplateGenerateRequest, TemplateSearchRequest, TemplateUpdate, TextSettingsUpdate, UserCreate, VideoTranscodeRequest, VisionSettingsUpdate
from .service import WanRenService, ServiceError


def create_app(env: dict[str, str] | None = None) -> FastAPI:
    settings = load_settings(env)

    @asynccontextmanager
    async def workflow_lifespan(app: FastAPI):
        worker_service = None
        worker_task = None
        stop = asyncio.Event()
        if settings.workflow_mode == "background" and settings.embedded_worker:
            worker_service = WanRenService(Database(settings.db_path), settings)

            async def run_worker() -> None:
                while not stop.is_set():
                    results = await asyncio.to_thread(
                        worker_service.run_pending_workflows,
                        f"api-worker-{id(app):x}",
                        1,
                    )
                    if not results:
                        try:
                            await asyncio.wait_for(stop.wait(), timeout=settings.worker_poll_seconds)
                        except asyncio.TimeoutError:
                            pass

            worker_task = asyncio.create_task(run_worker())
        app.state.worker_service = worker_service
        yield
        stop.set()
        if worker_task is not None:
            await worker_task
        if worker_service is not None:
            worker_service.db.close()

    app = FastAPI(title="万仞 WanRen", version="0.1.0", lifespan=workflow_lifespan)
    service = WanRenService(Database(settings.db_path), settings)
    app.state.service = service
    app.state.settings = settings

    @app.middleware("http")
    async def static_cache_middleware(request: Request, call_next):
        response = await call_next(request)
        if request.url.path in {"/app.js", "/site.css", "/site.js"}:
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response

    def require(permission: str):
        async def dependency(request: Request) -> None:
            if settings.auth_mode == "disabled":
                return
            service.require_permission(request.state.user, permission)
        return Depends(dependency)

    def bearer_token(request: Request) -> str:
        value = request.headers.get("Authorization", "")
        if value.startswith("Bearer "):
            return value[7:]
        return request.cookies.get("wanren_session", "")

    @app.middleware("http")
    async def authentication_middleware(request: Request, call_next):
        request.state.user = None
        public_paths = {"/health", "/favicon.ico", "/api/auth/login", "/api/auth/register", "/api/auth/config"}
        if settings.auth_mode == "disabled" or not request.url.path.startswith("/api") or request.url.path in public_paths:
            return await call_next(request)
        token = bearer_token(request)
        user = service.user_from_token(token) if token else None
        if user is None:
            return Response(
                content=json.dumps({"detail": "Authentication required"}, ensure_ascii=False),
                status_code=401,
                media_type="application/json",
            )
        request.state.user = user
        return await call_next(request)

    @app.exception_handler(ServiceError)
    async def service_error_handler(request, exc: ServiceError):
        return Response(
            content=json.dumps({"detail": str(exc)}, ensure_ascii=False),
            status_code=exc.status_code,
            media_type="application/json",
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "publisher": settings.publisher}

    @app.get("/favicon.ico")
    def favicon() -> Response:
        return Response(status_code=204)

    @app.post("/api/auth/login")
    def login(payload: LoginRequest, response: Response) -> dict[str, Any]:
        result = service.login(payload)
        response.set_cookie(
            "wanren_session",
            result["token"],
            httponly=True,
            samesite="lax",
            secure=settings.auth_mode == "production",
            max_age=12 * 60 * 60,
        )
        return result

    @app.get("/api/auth/config")
    def auth_config() -> dict[str, Any]:
        return service.public_auth_config()

    @app.post("/api/auth/register")
    def register(payload: RegisterRequest, response: Response) -> dict[str, Any]:
        result = service.register(payload)
        response.set_cookie(
            "wanren_session",
            result["token"],
            httponly=True,
            samesite="lax",
            secure=settings.auth_mode == "production",
            max_age=12 * 60 * 60,
        )
        return result

    @app.get("/api/auth/me")
    def current_user(request: Request, dependency=require("read")) -> dict[str, Any]:
        if settings.auth_mode == "disabled":
            from .auth import permissions_for_role

            return {
                "authenticated": False,
                "role": "ADMIN",
                "permissions": permissions_for_role("ADMIN"),
            }
        return request.state.user or {}

    @app.post("/api/auth/logout")
    def logout(request: Request, dependency=require("read")) -> dict[str, Any]:
        return service.logout(bearer_token(request))

    @app.get("/api/users")
    def users(request: Request, dependency=require("user.manage")) -> list[dict[str, Any]]:
        return service.list_users(request.state.user)

    @app.post("/api/users")
    def create_user(payload: UserCreate, request: Request, dependency=require("user.manage")) -> dict[str, Any]:
        return service.create_user(request.state.user, payload)

    @app.get("/api/dashboard")
    def dashboard(dependency=require("read")) -> dict[str, Any]:
        return service.dashboard()

    @app.get("/api/settings/vision")
    def get_vision_settings(dependency=require("account.manage")) -> dict[str, Any]:
        return service.get_vision_settings()

    @app.put("/api/settings/vision")
    def update_vision_settings(
        payload: VisionSettingsUpdate,
        dependency=require("account.manage"),
    ) -> dict[str, Any]:
        return service.update_vision_settings(payload.model_dump())

    @app.get("/api/settings/text")
    def get_text_settings(dependency=require("account.manage")) -> dict[str, Any]:
        return service.get_text_settings()

    @app.put("/api/settings/text")
    def update_text_settings(
        payload: TextSettingsUpdate,
        dependency=require("account.manage"),
    ) -> dict[str, Any]:
        return service.update_text_settings(payload.model_dump())

    @app.post("/api/settings/test-connection")
    def test_connection(
        payload: ConnectionTestRequest,
        dependency=require("account.manage"),
    ) -> dict[str, Any]:
        return service.test_connection(payload.target)

    @app.get("/api/system/health")
    def system_health(dependency=require("read")) -> dict[str, Any]:
        return service.system_health()

    @app.get("/metrics")
    def metrics() -> Response:
        return Response(service.prometheus_metrics(), media_type="text/plain; version=0.0.4")

    @app.get("/api/accounts")
    def accounts(dependency=require("read")) -> list[dict[str, Any]]:
        return service.list_accounts()

    @app.get("/api/articles")
    def articles(dependency=require("read")) -> list[dict[str, Any]]:
        return service.list_articles()

    @app.get("/api/audit-logs")
    def audit_logs(limit: int = 100, dependency=require("audit.read")) -> list[dict[str, Any]]:
        return service.list_audit_logs(limit)

    @app.get("/api/audit-logs/page")
    def audit_log_page(
        page: int = 1,
        page_size: int = 20,
        dependency=require("audit.read"),
    ) -> dict[str, Any]:
        return service.list_audit_page(page, page_size)

    @app.get("/api/workflows")
    def workflow_page(
        page: int = 1,
        page_size: int = 20,
        dependency=require("read"),
    ) -> dict[str, Any]:
        return service.list_workflow_page(page, page_size)

    @app.post("/api/accounts")
    def create_account(payload: AccountCreate, dependency=require("account.manage")) -> dict[str, Any]:
        return service.create_account(payload)

    @app.post("/api/accounts/{account_id}/verify")
    async def verify_account(account_id: str, dependency=require("account.manage")) -> dict[str, Any]:
        return await service.verify_account(account_id)

    @app.patch("/api/accounts/{account_id}")
    def update_account(
        account_id: str,
        payload: AccountUpdate,
        dependency=require("account.manage"),
    ) -> dict[str, Any]:
        return service.update_account_name(account_id, payload)

    @app.get("/api/templates")
    def templates(dependency=require("read")) -> list[dict[str, Any]]:
        return service.list_templates()

    @app.post("/api/templates")
    def create_template(payload: TemplateCreate, dependency=require("article.generate")) -> dict[str, Any]:
        return service.create_template(payload)

    @app.post("/api/templates/generate")
    def generate_template(payload: TemplateGenerateRequest, dependency=require("article.generate")) -> dict[str, Any]:
        return service.generate_template(payload)

    @app.post("/api/templates/search")
    def search_template_references(
        payload: TemplateSearchRequest,
        dependency=require("article.generate"),
    ) -> dict[str, Any]:
        return service.search_template_references(payload)

    @app.put("/api/templates/{template_id}")
    def update_template(
        template_id: str,
        payload: TemplateUpdate,
        dependency=require("article.generate"),
    ) -> dict[str, Any]:
        return service.update_template(template_id, payload)

    @app.get("/api/templates/{template_id}/versions")
    def template_versions(template_id: str, dependency=require("read")) -> list[dict[str, Any]]:
        return service.list_template_versions(template_id)

    @app.get("/api/sessions")
    def sessions(dependency=require("read")) -> list[dict[str, Any]]:
        return service.list_sessions()

    @app.post("/api/sessions")
    def create_session(payload: SessionCreate, dependency=require("session.write")) -> dict[str, Any]:
        return service.create_session(payload)

    @app.get("/api/sessions/{session_id}")
    def session_detail(session_id: str, dependency=require("read")) -> dict[str, Any]:
        return service.get_session(session_id)

    @app.get("/api/typesetting-styles")
    def typesetting_styles(dependency=require("read")) -> dict[str, Any]:
        return {"styles": service.list_typesetting_styles()}

    @app.get("/api/typesetting-styles/{style_id}/preview")
    def typesetting_style_preview(
        style_id: str, sample: str | None = None, dependency=require("read")
    ) -> dict[str, Any]:
        return service.preview_typesetting_style(style_id, sample)

    @app.get("/api/typesetting-styles/{style_id}/export")
    def typesetting_style_export(style_id: str, dependency=require("read")) -> dict[str, Any]:
        return service.export_typesetting_style(style_id)

    @app.post("/api/typesetting-styles")
    def import_typesetting_style(
        payload: StyleImportRequest, dependency=require("account.manage")
    ) -> dict[str, Any]:
        return service.import_typesetting_style(payload.model_dump())

    @app.delete("/api/typesetting-styles/{style_id}")
    def delete_typesetting_style(style_id: str, dependency=require("account.manage")) -> dict[str, Any]:
        return service.delete_typesetting_style(style_id)

    @app.post("/api/sessions/{session_id}/propose")
    def propose_architecture(
        session_id: str, payload: ArticleChatRequest, dependency=require("session.write")
    ) -> dict[str, Any]:
        return service.propose_architecture(session_id, payload.instruction)

    @app.post("/api/sessions/{session_id}/confirm-proposal")
    def confirm_proposal(
        session_id: str, payload: ConfirmProposalRequest, dependency=require("session.write")
    ) -> dict[str, Any]:
        return service.confirm_proposal(session_id, payload.style_id)

    @app.delete("/api/sessions/{session_id}")
    def cancel_session(session_id: str, dependency=require("session.write")) -> dict[str, Any]:
        return service.cancel_session(session_id)

    @app.post("/api/sessions/{session_id}/assets")
    async def upload_asset(
        session_id: str,
        file: UploadFile = File(...),
        article_id: str = Form(default=""),
        dependency=require("asset.write"),
    ) -> dict[str, Any]:
        payload = await file.read()
        return service.upload_asset(session_id, file.filename or "", file.content_type or "", payload, article_id or None)

    @app.post("/api/sessions/{session_id}/assets/{asset_id}/analyze")
    def analyze_asset(session_id: str, asset_id: str, payload: AssetAnalyzeRequest, dependency=require("vision.run")) -> dict[str, Any]:
        return service.analyze_asset(session_id, asset_id, payload.prompt)

    @app.post("/api/assets/{asset_id}/transcode")
    def transcode_asset(
        asset_id: str,
        payload: VideoTranscodeRequest,
        dependency=require("asset.write"),
    ) -> dict[str, Any]:
        return service.enqueue_video_transcode(asset_id, payload.preset)

    @app.post("/api/sessions/{session_id}/generate")
    def generate(session_id: str, payload: GenerateRequest, dependency=require("article.generate")) -> dict[str, Any]:
        if settings.workflow_mode == "inline":
            return service.generate_article(session_id, payload.instruction)
        return service.enqueue_generate_article(session_id, payload.instruction)

    @app.get("/api/assets")
    def all_assets(dependency=require("read")) -> list[dict[str, Any]]:
        return service.list_all_assets()

    @app.get("/api/wechat/drafts")
    async def wechat_drafts(account_id: str, dependency=require("read")) -> dict[str, Any]:
        return await service.list_wechat_drafts(account_id)

    @app.post("/api/wechat/drafts/import")
    async def import_wechat_draft(payload: dict[str, Any], dependency=require("article.generate")) -> dict[str, Any]:
        account_id = str(payload.get("account_id") or "")
        media_id = str(payload.get("media_id") or "")
        if not account_id or not media_id:
            raise ServiceError("account_id 与 media_id 必填", 400)
        return await service.import_wechat_draft(account_id, media_id)

    @app.delete("/api/articles/{article_id}")
    def delete_article(article_id: str, dependency=require("article.edit")) -> dict[str, Any]:
        return service.delete_article(article_id)

    @app.post("/api/articles/{article_id}/revert")
    def revert_article(article_id: str, payload: RevertRequest, dependency=require("article.generate")) -> dict[str, Any]:
        return service.revert_article(article_id, payload.to_version)

    @app.get("/api/assets/{asset_id}/file")
    def asset_file(asset_id: str, dependency=require("read")) -> Response:
        asset, payload = service.get_asset_file(asset_id)
        return Response(payload, media_type=asset["mime_type"] or "application/octet-stream")

    @app.get("/api/articles/{article_id}")
    def article_detail(article_id: str, dependency=require("read")) -> dict[str, Any]:
        article = service.db.query_one("SELECT * FROM article WHERE id = ?", (article_id,))
        if article is None:
            raise ServiceError("Article not found", 404)
        versions = service.db.query_all(
            "SELECT id,version_no,source,change_summary,created_at,highlight_json FROM article_version WHERE article_id = ? ORDER BY version_no",
            (article_id,),
        )
        reviews = service.db.query_all(
            "SELECT * FROM review WHERE article_id = ? ORDER BY created_at", (article_id,)
        )
        jobs = service.db.query_all(
            "SELECT * FROM publish_job WHERE article_id = ? ORDER BY created_at", (article_id,)
        )
        article["original_declaration_required"] = settings.publisher == "wechat"
        return {
            "article": article,
            "versions": versions,
            "reviews": reviews,
            "publish_jobs": jobs,
            "messages": service.list_article_messages(article_id),
        }

    @app.get("/api/articles/{article_id}/render-preview")
    def article_render_preview(
        article_id: str, style_id: str | None = None, dependency=require("read")
    ) -> dict[str, Any]:
        return service.render_article_preview(article_id, style_id)

    @app.post("/api/articles/{article_id}/chat")
    def article_chat(
        article_id: str,
        payload: ArticleChatRequest,
        dependency=require("article.generate"),
    ) -> StreamingResponse:
        def event(name: str, data: dict[str, Any]) -> str:
            return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        def stream():
            yield event("state", {"status": "connected", "message": "已连接文章优化会话"})
            try:
                user_message = service.start_article_chat(article_id, payload.instruction, payload.attachments)
                yield event("user", {"message": user_message})
                yield event(
                    "tool",
                    {
                        "name": "UNDERSTAND_REVISION",
                        "status": "COMPLETED",
                        "message": "已记录本轮修改要求",
                    },
                )
                yield event(
                    "state",
                    {"status": "thinking", "message": "正在结合当前文章和素材上下文生成修订"},
                )
                result = service.revise_article_from_chat(article_id, payload.instruction)
                yield event(
                    "tool",
                    {
                        "name": "SAVE_VERSION",
                        "status": "COMPLETED",
                        "message": f"已保存第 {result['version']} 版",
                    },
                )
                reply = str(result["assistant_message"]["content"])
                for start in range(0, len(reply), 24):
                    yield event("delta", {"content": reply[start : start + 24]})
                yield event("completed", result)
            except Exception as exc:
                message = str(exc)
                try:
                    service.fail_article_chat(article_id, payload.instruction, message)
                except Exception:
                    pass
                yield event("error", {"message": message})

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/articles/{article_id}/assist")
    def article_assist(
        article_id: str,
        payload: ArticleChatRequest,
        dependency=require("article.generate"),
    ) -> StreamingResponse:
        def event(name: str, data: dict[str, Any]) -> str:
            return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        def stream():
            queue: list[str] = []

            def emit(name: str, data: dict[str, Any]) -> None:
                queue.append(event(name, data))

            import threading

            done = threading.Event()
            holder: dict[str, Any] = {}

            def run():
                try:
                    holder["result"] = service.assist_article(article_id, payload.instruction, emit, payload.attachments)
                except Exception as exc:  # noqa: BLE001 - SSE 通道内兜底
                    holder["error"] = str(exc)
                finally:
                    done.set()

            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            import time as _time

            while not done.is_set() or queue:
                while queue:
                    yield queue.pop(0)
                if not done.wait(0.25):
                    yield event("heartbeat", {"ts": _time.time()})
            if "error" in holder and holder["error"]:
                yield event("error", {"message": holder["error"]})

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/articles/{article_id}/assist/{assist_id}/tools/{call_id}/result")
    def assist_tool_result(
        article_id: str,
        assist_id: str,
        call_id: str,
        payload: AssistToolResult,
        dependency=require("article.generate"),
    ) -> dict[str, Any]:
        return service.assist_tool_result(assist_id, call_id, payload.model_dump())

    @app.post("/api/articles/draft-sync")
    async def sync_all_drafts(dependency=require("publish.submit")) -> dict[str, Any]:
        return await service.sync_all_wechat_drafts()

    @app.post("/api/articles/{article_id}/draft-sync")
    async def sync_draft(
        article_id: str,
        dependency=require("publish.submit"),
    ) -> dict[str, Any]:
        return await service.sync_wechat_draft_status(article_id)

    @app.post("/api/articles/{article_id}/original-declaration")
    def confirm_original_declaration(
        article_id: str,
        payload: OriginalDeclarationRequest,
        dependency=require("publish.submit"),
    ) -> dict[str, Any]:
        return service.confirm_original_declaration(article_id, payload.reviewer, payload.confirmed)

    @app.post("/api/articles/{article_id}/review")
    def review(article_id: str, dependency=require("review.run")) -> dict[str, Any]:
        return service.rerun_review(article_id)

    @app.put("/api/articles/{article_id}")
    def update_article(article_id: str, payload: ArticleUpdate, dependency=require("article.edit")) -> dict[str, Any]:
        return service.update_article(article_id, payload)

    @app.post("/api/articles/{article_id}/approve")
    def approve(article_id: str, payload: ReviewRequest, dependency=require("review.decide")) -> dict[str, Any]:
        return service.human_decision(article_id, True, payload.reviewer)

    @app.post("/api/articles/{article_id}/reject")
    def reject(article_id: str, payload: ReviewRequest, dependency=require("review.decide")) -> dict[str, Any]:
        return service.human_decision(article_id, False, payload.reviewer)

    @app.post("/api/articles/{article_id}/publish")
    async def publish(article_id: str, payload: PublishRequest, dependency=require("publish.submit")) -> dict[str, Any]:
        return await service.publish_article(article_id, payload.mode)

    @app.post("/api/articles/{article_id}/schedule")
    def schedule_publish(article_id: str, payload: SchedulePublishRequest, dependency=require("publish.submit")) -> dict[str, Any]:
        return service.schedule_article(article_id, payload.scheduled_at, payload.timezone)

    @app.put("/api/articles/{article_id}/schedule")
    def reschedule_publish(article_id: str, payload: SchedulePublishRequest, dependency=require("publish.submit")) -> dict[str, Any]:
        return service.reschedule_article(article_id, payload.scheduled_at)

    @app.delete("/api/articles/{article_id}/schedule")
    def cancel_schedule(article_id: str, dependency=require("publish.submit")) -> dict[str, Any]:
        return service.cancel_schedule(article_id)

    @app.get("/api/workflows/{run_id}")
    def workflow(run_id: str, dependency=require("read")) -> dict[str, Any]:
        run = service.db.query_one("SELECT * FROM workflow_run WHERE id = ?", (run_id,))
        if run is None:
            raise ServiceError("Workflow not found", 404)
        nodes = service.db.query_all(
            "SELECT * FROM workflow_node_run WHERE workflow_run_id = ? ORDER BY started_at", (run_id,)
        )
        return {"run": run, "nodes": nodes}

    @app.get("/wechat/{account_id}/callback", include_in_schema=False)
    def wechat_callback_verify(
        account_id: str,
        signature: str = "",
        timestamp: str = "",
        nonce: str = "",
        echostr: str = "",
    ) -> Response:
        service.verify_wechat_callback(account_id, signature, timestamp, nonce)
        return Response(echostr, media_type="text/plain")

    @app.post("/wechat/{account_id}/callback", include_in_schema=False)
    async def wechat_callback_event(
        account_id: str,
        request: Request,
        signature: str = "",
        timestamp: str = "",
        nonce: str = "",
    ) -> Response:
        body = await request.body()
        service.handle_wechat_publish_callback(
            account_id,
            body,
            signature,
            timestamp,
            nonce,
        )
        return Response("success", media_type="text/plain")

    static_dir = Path(__file__).resolve().parent.parent / "static"

    def _page_response(path: Path) -> FileResponse:
        # Brand and navigation changes must not be held by intermediary caches.
        return FileResponse(path, headers={"Cache-Control": "no-cache, must-revalidate"})

    def authenticated_page_user(request: Request) -> dict[str, Any] | None:
        if settings.auth_mode == "disabled":
            return {"role": "ADMIN"}
        token = bearer_token(request)
        return service.user_from_token(token) if token else None

    @app.get("/", include_in_schema=False)
    def root_page(request: Request):
        if authenticated_page_user(request) is not None:
            return _page_response(static_dir / "index.html")
        return _page_response(static_dir / "landing.html")

    @app.get("/landing", include_in_schema=False)
    def landing_page() -> FileResponse:
        return _page_response(static_dir / "landing.html")

    @app.get("/login", include_in_schema=False)
    def login_page(request: Request):
        if authenticated_page_user(request) is not None:
            return RedirectResponse("/", status_code=303)
        return _page_response(static_dir / "auth.html")

    @app.get("/register", include_in_schema=False)
    def register_page(request: Request):
        if authenticated_page_user(request) is not None:
            return RedirectResponse("/", status_code=303)
        return _page_response(static_dir / "auth.html")

    if static_dir.exists():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return app
