"""轻量对称加密：用于把视觉分析 API Key 等敏感值加密后落库。

保持零第三方依赖（标准库 hashlib/hmac/base64 实现 HMAC-SHA256 密钥流），
密钥由 WANREN_APP_SECRET_KEY 派生；未配置时退化为管理员口令派生（仅本地开发）。
回显时永远只输出脱敏形式。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os


def _derive_key() -> bytes:
    secret = os.environ.get("WANREN_APP_SECRET_KEY", "")
    if not secret:
        # 本地开发回退：管理员口令 + 固定盐派生（不适用于多实例生产）
        secret = os.environ.get("WANREN_ADMIN_PASSWORD", "wanren-local")
    return hashlib.sha256(("wanren-setting:" + secret).encode("utf-8")).digest()


def encrypt_value(plain: str) -> str:
    if not plain:
        return ""
    nonce = os.urandom(12)
    stream = _keystream(nonce, len(plain.encode("utf-8")))
    data = bytes(b ^ k for b, k in zip(plain.encode("utf-8"), stream))
    return "v1:" + base64.urlsafe_b64encode(nonce + data).decode("ascii")


def decrypt_value(token: str) -> str:
    if not token:
        return ""
    if not token.startswith("v1:"):
        return token  # 兼容历史明文
    raw = base64.urlsafe_b64decode(token[3:].encode("ascii"))
    nonce, data = raw[:12], raw[12:]
    stream = _keystream(nonce, len(data))
    return bytes(b ^ k for b, k in zip(data, stream)).decode("utf-8")


def mask_key(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return value[:3] + "•" * 8 + value[-3:]


def _keystream(nonce: bytes, length: int) -> bytes:
    key = _derive_key()
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])
