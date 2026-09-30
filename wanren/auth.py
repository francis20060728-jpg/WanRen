from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any


ROLE_PERMISSIONS = {
    "ADMIN": {
        "read", "session.write", "asset.write", "vision.run", "article.generate",
        "article.edit", "review.run", "review.decide", "publish.submit",
        "account.manage", "audit.read", "user.manage",
    },
    "EDITOR": {"read", "session.write", "asset.write", "vision.run", "article.generate", "article.edit", "review.run"},
    "REVIEWER": {"read", "review.run", "review.decide"},
    "PUBLISHER": {"read", "publish.submit"},
    "VIEWER": {"read"},
}


def hash_password(password: str) -> str:
    iterations = 240_000
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations).hex()
    return f"pbkdf2_sha256${iterations}${salt}${digest}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, digest = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt.encode(), int(iterations)
        ).hex()
        return hmac.compare_digest(candidate, digest)
    except (ValueError, AttributeError):
        return False


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def token_expiry(hours: int = 12) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def token_expired(expires_at: str) -> bool:
    return datetime.now(timezone.utc) >= datetime.fromisoformat(expires_at)


def has_permission(user: dict[str, Any] | None, permission: str) -> bool:
    if user is None:
        return False
    return permission in ROLE_PERMISSIONS.get(user.get("role", "VIEWER"), set())


def permissions_for_role(role: str) -> list[str]:
    return sorted(ROLE_PERMISSIONS.get(role, set()))
