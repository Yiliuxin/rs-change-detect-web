# -*- coding: utf-8 -*-
"""认证服务（注册/登录扩展）：PBKDF2 口令哈希 + HS256 JWT（纯标准库，零新依赖）。

安全口径：
  - 口令 PBKDF2-HMAC-SHA256 / 120_000 轮 / 16B 随机盐，存 ``pbkdf2_sha256$iter$salt$hash``；
  - JWT HS256，12h 过期，claims 仅 sub/username/exp（无敏感字段）；
  - 密钥取环境变量 ``AUTH_JWT_SECRET``，缺省用开发密钥并告警（生产必须显式配置）；
  - 首个注册用户自动认领全部历史遗留 task（user_id IS NULL）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import uuid
from typing import Optional

from app.api.errors import ApiError
from app.repository import user_repo

__all__ = [
    "register", "login", "decode_token", "hash_password", "verify_password",
]

logger = logging.getLogger(__name__)

_PBKDF2_ITER = 120_000
_TOKEN_TTL_SEC = 12 * 3600
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")

# JWT 签名密钥（环境变量优先；缺省开发密钥仅限本地/演示）
_JWT_SECRET = os.environ.get("AUTH_JWT_SECRET") or "rs-change-dev-secret-do-not-use-in-prod"
if not os.environ.get("AUTH_JWT_SECRET"):
    logger.warning("AUTH_JWT_SECRET 未配置，使用开发缺省密钥（仅限本地演示环境）")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(txt: str) -> bytes:
    pad = "=" * (-len(txt) % 4)
    return base64.urlsafe_b64decode(txt + pad)


# --------------------------------------------------------------------------- #
# 口令哈希（PBKDF2，纯标准库）
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITER)
    return f"pbkdf2_sha256${_PBKDF2_ITER}${_b64url(salt)}${_b64url(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iter_s, salt_s, hash_s = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), _b64url_decode(salt_s), int(iter_s))
        return hmac.compare_digest(digest, _b64url_decode(hash_s))
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- #
# JWT HS256（纯标准库实现，header.payload.signature）
# --------------------------------------------------------------------------- #
def _make_token(user_id: str, username: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": user_id,
        "username": username,
        "iat": int(time.time()),
        "exp": int(time.time()) + _TOKEN_TTL_SEC,
    }
    signing_input = (_b64url(json.dumps(header, separators=(",", ":")).encode())
                     + "." + _b64url(json.dumps(payload, separators=(",", ":")).encode()))
    sig = hmac.new(_JWT_SECRET.encode(), signing_input.encode(), hashlib.sha256).digest()
    return signing_input + "." + _b64url(sig)


def decode_token(token: str) -> dict:
    """校验并解码 JWT；任何失败 → AUTH-01（未登录或登录已过期）。"""
    try:
        head, body, sig = token.split(".")
        expected = hmac.new(_JWT_SECRET.encode(), f"{head}.{body}".encode(),
                            hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64url_decode(sig)):
            raise ValueError("bad signature")
        payload = json.loads(_b64url_decode(body))
        if int(payload.get("exp", 0)) < int(time.time()):
            raise ValueError("expired")
        if not payload.get("sub"):
            raise ValueError("no sub")
        return payload
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001 - 任何解码失败统一 401
        raise ApiError("AUTH-01", detail=f"token 校验失败：{exc}") from exc


# --------------------------------------------------------------------------- #
# 注册 / 登录
# --------------------------------------------------------------------------- #
def register(username: str, password: str,
             display_name: Optional[str] = None) -> dict:
    """注册新用户；首个注册者自动认领历史遗留 run。返回用户信息 + token。"""
    if not username or not _USERNAME_RE.match(username):
        raise ApiError("AUTH-05", http_status=400,
                       detail="用户名需为 3–20 位字母/数字/下划线")
    if not password or not (6 <= len(password) <= 64):
        raise ApiError("AUTH-05", http_status=400,
                       detail="密码长度需在 6–64 位之间")
    if user_repo.get_user_by_username(username):
        raise ApiError("AUTH-04", http_status=409, detail=f"用户名已存在：{username}")

    # 仅首个注册用户认领历史遗留 run（后续注册者不碰既有数据归属）
    is_first_user = user_repo.count_users() == 0
    user = user_repo.insert_user(str(uuid.uuid4()), username,
                                 hash_password(password),
                                 (display_name or "").strip()[:64] or None)
    claimed = user_repo.claim_orphan_tasks(user["user_id"]) if is_first_user else 0
    if claimed:
        logger.info("首个注册用户 %s 认领历史遗留 run：%d 条", username, claimed)
    return {
        "user_id": user["user_id"],
        "username": user["username"],
        "display_name": user["display_name"],
        "claimed_runs": claimed,
        "token": _make_token(user["user_id"], user["username"]),
    }


def login(username: str, password: str) -> dict:
    """登录校验；用户不存在与密码错误统一报 AUTH-02（不泄漏账号存在性）。"""
    user = user_repo.get_user_by_username(username or "")
    if not user or not verify_password(password or "", user["password_hash"]):
        raise ApiError("AUTH-02", http_status=401, detail="用户名或密码错误")
    return {
        "user_id": user["user_id"],
        "username": user["username"],
        "display_name": user["display_name"],
        "token": _make_token(user["user_id"], user["username"]),
    }
