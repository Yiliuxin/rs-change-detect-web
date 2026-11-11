# -*- coding: utf-8 -*-
"""路由层公共依赖：当前用户提取与 run 归属校验（注册/登录扩展）。

约定：网关中间件（main.py）校验 JWT 后把 ``request.state.user_id`` /
``request.state.username`` 写入请求上下文；``AUTH_DISABLED=1``（测试进程内
客户端）时两者为 None，所有鉴权点直通，既有 166 条测试语义不变。
"""
from __future__ import annotations

from typing import Optional

from fastapi import Request

from app.api.errors import ApiError
from app.repository import task_repo

__all__ = ["current_user_id", "ensure_run_owner"]


def current_user_id(request: Request) -> Optional[str]:
    """取当前登录用户 ID；认证关闭/未命中时为 None。

    优先读网关中间件写入的 ``request.state``；部分 Starlette 版本在
    BaseHTTPMiddleware 下 state 不跨层传递，这里兜底直接解码 Bearer token。
    """
    user_id = getattr(request.state, "user_id", None)
    if user_id:
        return user_id
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    try:
        from app.service import auth_service
        return auth_service.decode_token(header[7:].strip())["sub"]
    except Exception:  # noqa: BLE001 - token 非法时按未登录处理
        return None


def ensure_run_owner(request: Request, run_id: str) -> None:
    """校验 run 归属：非本人 → AUTH-03(403)；run 不存在 → EX2(404)。

    认证关闭（AUTH_DISABLED，测试环境）时直通，不做归属检查。
    """
    user_id = current_user_id(request)
    if not user_id:
        return
    task = task_repo.get_task(run_id)
    if task is None:
        raise ApiError("EX2", run_id=run_id, detail=f"运行记录不存在：{run_id}")
    if task.get("user_id") and task["user_id"] != user_id:
        raise ApiError("AUTH-03", run_id=run_id)
