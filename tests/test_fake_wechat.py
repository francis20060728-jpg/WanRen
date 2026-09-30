"""假微信 API 服务器：真实验证 token 缓存、draft/update 复用与 48001 降级文案。"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from wanren.publishing import WeChatPublisher


class FakeWeChatHandler(BaseHTTPRequestHandler):
    calls: list[tuple[str, dict]] = []

    def log_message(self, *args):
        pass

    def _respond(self, payload, files=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        path = self.path.split("?")[0]
        try:
            body = json.loads(raw) if raw and not raw.startswith(b"--") else {}
        except json.JSONDecodeError:
            body = {}
        self.__class__.calls.append((path, body))
        if path == "/cgi-bin/stable_token":
            self._respond({"access_token": "token-" + str(len(self.__class__.calls)), "expires_in": 7200})
        elif path == "/cgi-bin/media/uploadimg":
            self._respond({"url": "https://mmbiz.qpic.cn/fake/" + str(len(self.__class__.calls))})
        elif path == "/cgi-bin/material/add_material":
            self._respond({"media_id": "thumb-" + str(len(self.__class__.calls))})
        elif path == "/cgi-bin/draft/add":
            self._respond({"media_id": f"draft-media-{len(self.__class__.calls)}"})
        elif path == "/cgi-bin/draft/update":
            self._respond({})
        elif path == "/cgi-bin/freepublish/submit":
            self._respond({"publish_id": "pub-1"})
        elif path == "/cgi-bin/errcode-48001":
            self._respond({"errcode": 48001, "errmsg": "api unauthorized"})
        else:
            self._respond({"errcode": 0})


@pytest.fixture()
def fake_wechat():
    FakeWeChatHandler.calls = []
    server = HTTPServer(("127.0.0.1", 0), FakeWeChatHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original = WeChatPublisher.base_url
    WeChatPublisher.base_url = f"http://127.0.0.1:{server.server_port}"
    yield server
    WeChatPublisher.base_url = original
    server.shutdown()


ACCOUNT = {"id": "acct", "name": "测试号"}


def _cover():
    from wanren.publishing import AssetFile

    return AssetFile("asset-1", "cover.png", "image/png", b"pngbytes")


def test_token_cache_reused_across_calls(fake_wechat):
    import asyncio

    publisher = WeChatPublisher("wx1", "secret")
    html = "<p>hello</p>"

    async def run():
        first = await publisher.create_draft(title="标题一", summary="摘要", html=html, account=ACCOUNT, cover=_cover())
        second = await publisher.create_draft(title="标题二", summary="摘要", html=html, account=ACCOUNT, cover=_cover())
        await publisher.publish(first.external_id, ACCOUNT)
        return first, second

    first, second = asyncio.run(run())
    token_calls = [c for c in FakeWeChatHandler.calls if c[0] == "/cgi-bin/stable_token"]
    assert first.external_id.startswith("draft-media-")
    assert second.external_id != first.external_id  # 新建草稿拿新 media_id
    assert len(token_calls) == 1, "同一发布器实例多次远程调用只应请求一次 stable_token（进程内缓存）"


def test_draft_update_reuses_media_id(fake_wechat):
    import asyncio

    publisher = WeChatPublisher("wx1", "secret")

    async def run():
        return await publisher.create_draft(
            title="改后的标题", summary="摘要", html="<p>new</p>", account=ACCOUNT, cover=_cover(), external_id="existing-media"
        )

    result = asyncio.run(run())
    update_calls = [c for c in FakeWeChatHandler.calls if c[0] == "/cgi-bin/draft/update"]
    add_calls = [c for c in FakeWeChatHandler.calls if c[0] == "/cgi-bin/draft/add"]
    assert result.external_id == "existing-media"
    assert len(update_calls) == 1
    assert update_calls[0][1]["media_id"] == "existing-media"
    assert not add_calls


def test_48001_gets_friendly_message(fake_wechat):
    import asyncio

    publisher = WeChatPublisher("wx1", "secret")

    async def run():
        async with __import__("wanren.publishing", fromlist=["httpx"]).httpx.AsyncClient(timeout=10) as client:
            response = await client.post(f"{publisher.base_url}/cgi-bin/errcode-48001")
            publisher._payload(response)

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(run())
    assert "48001" in str(excinfo.value)
    assert "草稿箱" in str(excinfo.value)
    assert "手动发布" in str(excinfo.value)


def test_paragraph_style_rendered_into_wechat_html():
    from wanren.publishing import render_article_html

    content = {
        "title": "排版",
        "summary": "s",
        "sections": [
            {
                "id": "sec_01",
                "heading": "正文",
                "paragraphs": ["第一段"],
                "asset_refs": [],
                "style": {"indent": 2, "line_height": 1.75, "letter_spacing": 1, "margin_top": 12, "align": "justify"},
            }
        ],
    }
    html = render_article_html(content, [], {}, {})
    assert "text-indent:2em" in html
    assert "line-height:1.75" in html
    assert "letter-spacing:1px" in html
    assert "margin-top:12px" in html
    assert "text-align:justify" in html


def fake_wechat_store_cover(storage_root, payload: bytes) -> str:
    from pathlib import Path
    d = Path(storage_root) / 'assets' / 'images'
    d.mkdir(parents=True, exist_ok=True)
    (d / 'cover.png').write_bytes(payload)
    return 'images/cover.png'


def test_publish_flow_updates_same_draft_across_versions(fake_wechat, tmp_path):
    """端到端：首次存草稿 → 文章改版 → 再存草稿 → 复用同一 media_id（draft/update 推送，不新建草稿）。"""
    import asyncio
    import json as jsonlib

    from wanren.database import Database
    from wanren.service import WanRenService
    from wanren.config import load_settings

    FakeWeChatHandler.calls.clear()
    settings = load_settings({
        "WANREN_DB_PATH": str(tmp_path / "svc.sqlite3"),
        "WANREN_STORAGE_PATH": str(tmp_path / "store"),
        "WANREN_PUBLISHER": "wechat",
        "WECHAT_APP_ID": "wx1", "WECHAT_APP_SECRET": "secret",
        "WANREN_WORKFLOW_MODE": "inline", "WANREN_AUTH_MODE": "disabled",
    })
    db = Database(settings.db_path)
    svc = WanRenService(db, settings)

    async def scenario():
        cover_bytes = __import__('tests.conftest', fromlist=['png_payload']).png_payload()
        cover_rel = fake_wechat_store_cover(settings.storage_path, cover_bytes)
        with db.tx():
            # 专属真实型账号（V5 起内置本地测试账号永远走 MockPublisher，wechat 链路需独立账号）
            db.execute(
                "INSERT INTO official_account(id,workspace_id,name,app_id,token_status,settings,created_at)"
                " VALUES ('oa_wx1','ws_default','复用验证号','wx1','READY','{}','')",
            )
            db.execute(
                "INSERT INTO content_session(id,workspace_id,official_account_id,title,status,created_at,updated_at)"
                " VALUES ('s1','ws_default','oa_wx1','复用验证','ACTIVE','','')",
            )
            db.execute(
                "INSERT INTO session_asset(id,session_id,asset_type,file_name,mime_type,size_bytes,storage_key,processing_status,extracted_text,structured_data,vision_result,metadata_json,created_at,updated_at)"
                " VALUES ('cov1','s1','IMAGE','cover.png','image/png',?,'images/cover.png','READY','','{}','{}','{}','','')",
                (len(cover_bytes),),
            )
            content = jsonlib.dumps({"title": "第一版", "summary": "", "sections": [
                {"id": "sec_01", "heading": "正文", "paragraphs": ["第一段。"], "asset_refs": []}
            ]}, ensure_ascii=False)
            db.execute(
                "INSERT INTO article(id,session_id,title,summary,content_json,status,current_version_id,cover_asset_id,created_at,updated_at)"
                " VALUES ('a1','s1','第一版','',?,'HUMAN_APPROVED','v1','cov1','','')",
                (content,),
            )
            db.execute(
                "INSERT INTO article_version(id,article_id,version_no,source,content_json,change_summary,created_at)"
                " VALUES ('v1','a1',1,'AI',?,'init','')",
                (content,),
            )
        first = await svc.publish_article("a1", "draft")
        # 改版：新版本内容
        with db.tx():
            db.execute(
                "INSERT INTO article_version(id,article_id,version_no,source,content_json,change_summary,created_at)"
                " VALUES ('v2','a1',2,'HUMAN',?,'改版','')",
                (jsonlib.dumps({"title": "第二版", "summary": "", "sections": [
                    {"id": "sec_01", "heading": "正文", "paragraphs": ["第二段内容变了。"], "asset_refs": []}
                ]}, ensure_ascii=False),),
            )
            db.execute("UPDATE article SET content_json=(SELECT content_json FROM article_version WHERE id='v2'), current_version_id='v2', updated_at='' WHERE id='a1'")
        second = await svc.publish_article("a1", "draft")
        return first, second

    first, second = asyncio.run(scenario())
    assert first["external_id"], first
    assert second["external_id"] == first["external_id"]  # 同一条草稿被更新
    add_calls = [c for c in FakeWeChatHandler.calls if c[0] == "/cgi-bin/draft/add"]
    update_calls = [c for c in FakeWeChatHandler.calls if c[0] == "/cgi-bin/draft/update"]
    assert len(add_calls) == 1 and len(update_calls) == 1  # 首次 add，改版 update
