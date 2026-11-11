# -*- coding: utf-8 -*-
"""认证路由（注册/登录扩展）：/auth/register、/auth/login、/auth/me。

网关（main.py 中间件）对本组路径放行；其余 /api/v1/* 一律要求 Bearer JWT。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.api.deps import current_user_id, ensure_run_owner
from app.service import auth_service

__all__ = ["router", "ensure_run_owner"]  # ensure_run_owner 转出口，供其他路由复用

router = APIRouter(prefix="/auth", tags=["Auth"])


class RegisterRequest(BaseModel):
    """注册请求体（格式校验交给服务层 → AUTH-05，保持 {code,message} 错误契约）。"""

    username: str = Field(..., max_length=64, description="3–20 位字母/数字/下划线")
    password: str = Field(..., max_length=128, description="6–64 位密码")
    display_name: Optional[str] = Field(None, max_length=64, description="显示名（可空）")


class LoginRequest(BaseModel):
    """登录请求体。"""

    username: str = Field(..., min_length=1, max_length=20)
    password: str = Field(..., min_length=1, max_length=64)


class AuthResponse(BaseModel):
    """注册/登录统一响应：用户信息 + JWT。"""

    user_id: str
    username: str
    display_name: Optional[str] = None
    token: str
    claimed_runs: int = 0


@router.post(
    "/register",
    response_model=AuthResponse,
    summary="注册新用户（纯新账号，不关联任何既有 run）",
)
def register_user(payload: RegisterRequest) -> AuthResponse:
    out = auth_service.register(payload.username, payload.password, payload.display_name)
    return AuthResponse(**out)


@router.post(
    "/login",
    response_model=AuthResponse,
    summary="登录（用户名 + 密码 → JWT）",
)
def login_user(payload: LoginRequest) -> AuthResponse:
    out = auth_service.login(payload.username, payload.password)
    return AuthResponse(**out, claimed_runs=0)


@router.get(
    "/me",
    summary="当前登录用户信息（校验 token 有效性）",
)
def me(request: Request) -> dict:
    user_id = current_user_id(request)
    user = auth_service.user_repo.get_user(user_id) if user_id else None
    if not user:
        from app.api.errors import ApiError
        raise ApiError("AUTH-01")
    return {
        "user_id": user["user_id"],
        "username": user["username"],
        "display_name": user["display_name"],
        "created_at": str(user["created_at"]),
    }


# --------------------------------------------------------------------------- #
# 供其他路由使用的 run 归属校验（转出口，见 deps.ensure_run_owner）
# --------------------------------------------------------------------------- #
def guard_run_owner(request: Request, run_id: str) -> None:
    """校验当前用户对该 run 的所有权（认证关闭时直通）。"""
    ensure_run_owner(request, run_id)
