from __future__ import annotations
import json
import time

import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .security import escape_text, sanitize_wechat_html

# 进程内 access_token 缓存：剩余有效期大于该秒数即复用，避免一次发布
# 请求 3 次 stable_token 触发微信限频（凭据仍然零落库，重启后重新获取）。
TOKEN_REUSE_MARGIN_SECONDS = 300


@dataclass
class PublishResult:
    status: str
    external_id: str | None
    error: dict[str, Any] | None = None


@dataclass
class AssetFile:
    asset_id: str
    file_name: str
    mime_type: str
    payload: bytes


class Publisher(Protocol):
    name: str

    async def asset_urls(self, assets: list[AssetFile]) -> dict[str, str]:
        ...


    async def create_draft(
        self,
        *,
        title: str,
        summary: str,
        html: str,
        account: dict[str, Any],
        cover: AssetFile | None,
    ) -> PublishResult:
        ...

    async def get_draft(
        self,
        external_id: str,
        account: dict[str, Any],
    ) -> dict[str, Any] | None:
        ...

    async def publish(self, external_id: str, account: dict[str, Any]) -> PublishResult:
        ...


class MockPublisher:
    name = "mock"

    async def list_drafts(self, account, offset: int = 0, count: int = 20) -> dict[str, Any]:
        return {"total": 0, "items": []}

    async def asset_urls(self, assets: list[AssetFile]) -> dict[str, str]:
        return {asset.asset_id: f"/api/assets/{asset.asset_id}/file" for asset in assets}

    async def create_draft(
        self,
        *,
        title: str,
        summary: str,
        html: str,
        account: dict[str, Any],
        cover: AssetFile | None,
        external_id: str | None = None,
    ) -> PublishResult:
        if external_id:
            return PublishResult("WECHAT_DRAFT", external_id)
        token = f"mock-draft-{account['id']}-{abs(hash(title + summary)) & 0xffffffff:x}"
        return PublishResult("WECHAT_DRAFT", token)

    async def get_draft(
        self,
        external_id: str,
        account: dict[str, Any],
    ) -> dict[str, Any] | None:
        return {"media_id": external_id, "news_item": []}

    async def publish(self, external_id: str, account: dict[str, Any]) -> PublishResult:
        return PublishResult("PUBLISHED", external_id)


class WeChatPublisher:
    name = "wechat"
    base_url = "https://api.weixin.qq.com"

    def __init__(self, app_id: str, app_secret: str):
        if not app_id or not app_secret:
            raise ValueError("WECHAT_APP_ID and WECHAT_APP_SECRET are required")
        self.app_id = app_id
        self.app_secret = app_secret
        self._cached_token: str | None = None
        self._cached_token_expires: float = 0.0

    async def _token(self, client: httpx.AsyncClient, force: bool = False) -> str:
        if not force and self._cached_token and time.monotonic() < self._cached_token_expires - TOKEN_REUSE_MARGIN_SECONDS:
            return self._cached_token
        response = await client.post(
            f"{self.base_url}/cgi-bin/stable_token",
            json={
                "grant_type": "client_credential",
                "appid": self.app_id,
                "secret": self.app_secret,
            },
        )
        payload = self._payload(response)
        token = payload.get("access_token")
        if not token:
            raise RuntimeError(f"WeChat token error: {payload}")
        self._cached_token = str(token)
        expires_in = float(payload.get("expires_in") or 7200)
        self._cached_token_expires = time.monotonic() + max(expires_in - 120, 300)
        return self._cached_token

    @staticmethod
    def _payload(response: httpx.Response) -> dict[str, Any]:
        response.raise_for_status()
        payload = response.json()
        errcode = payload.get("errcode")
        if errcode not in {None, 0}:
            if errcode == 48001:
                raise RuntimeError(
                    "api unauthorized (48001)：该公众号（个人主体或未认证）没有「发布」接口权限。"
                    "请在微信公众号后台的草稿箱中手动发布本文，或改用「仅同步草稿」模式。"
                )
            raise RuntimeError(f"WeChat API error: {payload}")
        return payload

    async def asset_urls(self, assets: list[AssetFile]) -> dict[str, str]:
        urls: dict[str, str] = {}
        async with httpx.AsyncClient(timeout=60) as client:
            token = await self._token(client)
            for asset in assets:
                response = await client.post(
                    f"{self.base_url}/cgi-bin/media/uploadimg",
                    params={"access_token": token},
                    files={"media": (asset.file_name, asset.payload, asset.mime_type)},
                )
                payload = self._payload(response)
                url = payload.get("url")
                if not url:
                    raise RuntimeError(f"WeChat image upload returned no URL: {payload}")
                urls[asset.asset_id] = str(url)
        return urls

    async def _upload_cover(self, client: httpx.AsyncClient, token: str, cover: AssetFile) -> str:
        response = await client.post(
            f"{self.base_url}/cgi-bin/material/add_material",
            params={"access_token": token, "type": "image"},
            files={"media": (cover.file_name, cover.payload, cover.mime_type)},
        )
        payload = self._payload(response)
        media_id = payload.get("media_id")
        if not media_id:
            raise RuntimeError(f"WeChat cover upload returned no media_id: {payload}")
        return str(media_id)

    async def list_drafts(
        self,
        account: dict[str, Any],
        offset: int = 0,
        count: int = 20,
    ) -> dict[str, Any]:
        """草稿箱列表（draft/batchget）：供外部草稿导入。"""
        async with httpx.AsyncClient(timeout=60) as client:
            token = await self._token(client)
            response = await client.post(
                f"{self.base_url}/cgi-bin/draft/batchget",
                params={"access_token": token},
                json={"offset": offset, "count": count, "no_content": 1},
            )
            payload = self._payload(response)
            return {
                "total": int(payload.get("total_count") or 0),
                "items": [
                    {
                        "media_id": str(item.get("media_id") or ""),
                        "title": str((item.get("content") or {}).get("news_item", [{}])[0].get("title") or "未命名草稿"),
                        "update_time": int((item.get("update_time") or 0)),
                    }
                    for item in payload.get("item", [])
                ],
            }

    async def get_draft(
        self,
        external_id: str,
        account: dict[str, Any],
    ) -> dict[str, Any] | None:
        async with httpx.AsyncClient(timeout=60) as client:
            token = await self._token(client)
            response = await client.post(
                f"{self.base_url}/cgi-bin/draft/get",
                params={"access_token": token},
                json={"media_id": external_id},
            )
            payload = response.json()
            # A deleted draft media id is reported by WeChat as errcode 40007.
            if payload.get("errcode") == 40007:
                # token 失效偶发也会报 invalid media：强刷 token 复核一次，避免坏缓存永久误判
                token2 = await self._token(client, force=True)
                retry = await client.post(
                    f"{self.base_url}/cgi-bin/draft/get",
                    params={"access_token": token2},
                    json={"media_id": external_id},
                )
                r2 = retry.json()
                return None if r2.get("errcode") == 40007 else self._payload(retry)
            return self._payload(response)

    async def find_draft_in_list(
        self,
        external_id: str,
        account: dict[str, Any],
    ) -> dict[str, Any] | None:
        """从 draft/batchget 翻页定位指定草稿并返回 news_item 全文。
        部分草稿的 draft/get 间歇报 40007，batchget 则稳定返回全文——导入走这条路径。"""
        async with httpx.AsyncClient(timeout=60) as client:
            token = await self._token(client)
            for offset in (0, 20, 40):
                response = await client.post(
                    f"{self.base_url}/cgi-bin/draft/batchget",
                    params={"access_token": token},
                    json={"offset": offset, "count": 20},
                )
                payload = self._payload(response)
                for item in payload.get("item", []):
                    if str(item.get("media_id")) == external_id:
                        return (item.get("content") or {}).get("news_item", [{}])[0] or {}
                if int(payload.get("item_count") or 0) < 20:
                    break
            return None


    async def create_draft(
        self,
        *,
        title: str,
        summary: str,
        html: str,
        account: dict[str, Any],
        cover: AssetFile | None,
        external_id: str | None = None,
    ) -> PublishResult:
        if cover is None:
            return PublishResult("FAILED", None, {"message": "A cover image is required by WeChat"})
        async with httpx.AsyncClient(timeout=60) as client:
            token = await self._token(client)
            thumb_media_id = await self._upload_cover(client, token, cover)
            article_payload = {
                "article_type": "news",
                "title": title,
                "digest": summary,
                "content": sanitize_wechat_html(html),
                "thumb_media_id": thumb_media_id,
                "need_open_comment": 0,
                "only_fans_can_comment": 0,
            }
            if external_id:
                # 已有微信草稿：更新而不是新建，保留 media_id 与人工在微信侧的调整
                response = await client.post(
                    f"{self.base_url}/cgi-bin/draft/update",
                    params={"access_token": token},
                    json={"media_id": external_id, "index": 0, "articles": article_payload},
                )
                self._payload(response)
                return PublishResult("WECHAT_DRAFT", external_id)
            response = await client.post(
                f"{self.base_url}/cgi-bin/draft/add",
                params={"access_token": token},
                json={"articles": [article_payload]},
            )
            payload = self._payload(response)
            return PublishResult("WECHAT_DRAFT", str(payload["media_id"]))

    async def publish(self, external_id: str, account: dict[str, Any]) -> PublishResult:
        async with httpx.AsyncClient(timeout=60) as client:
            token = await self._token(client)
            response = await client.post(
                f"{self.base_url}/cgi-bin/freepublish/submit",
                params={"access_token": token},
                json={"media_id": external_id},
            )
            payload = self._payload(response)
            publish_id = payload.get("publish_id")
            return PublishResult("PUBLISHING", str(publish_id) if publish_id else external_id)


def render_article_html(
    content: dict[str, Any],
    assets: list[list] | list[dict[str, Any]],
    asset_urls: dict[str, str],
    account_settings: dict[str, Any],
    typesetting_id: str | None = None,
) -> str:
    """按样式模版渲染公众号正文 HTML（实现集中在 wanren.typesetting）。"""
    from .typesetting import render_article_html as _render

    return _render(content, assets, asset_urls, account_settings, typesetting_id)
