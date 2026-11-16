# -*- coding: utf-8 -*-
"""路由：检测参数模板 CRUD（批次 D，NF9 参数快照模板化）。

薄封装：Pydantic 校验 + 调 template_service + 统一异常封装 {code,message,run_id}。
全部 JWT 保护（网关中间件对 /api/v1/* 除 /auth/* 一律要求 Bearer token），
模板按 user_id 隔离；鉴权风格与 deps.current_user_id / ensure_run_owner 一致。

契约：
  POST   /api/v1/templates           创建模板（name 必填；同用户重名 → TMPL-01 409）
  GET    /api/v1/templates           本人模板列表（created_at 倒序）
  DELETE /api/v1/templates/{id}      仅本人可删；不存在或属他人 → TMPL-02 404
口径参数 params 只接受白名单键（index/method/block_size/threshold），多余键丢弃；
index/method/block_size 非法枚举按 EX1 口径报错（400，中文提示）。
"""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.api.deps import current_user_id
from app.api.v1.schemas import ErrorResponse
from app.config.constants import TEMPLATE_NAME_MAX
from app.service import template_service

router = APIRouter(prefix="/templates", tags=["Templates"])


class TemplateCreateRequest(BaseModel):
    """创建模板请求：name 必填；params 为口径面板参数（白名单外键丢弃）。"""

    name: str = Field(..., min_length=1, max_length=TEMPLATE_NAME_MAX,
                      description=f"模板名（同用户内唯一，1..{TEMPLATE_NAME_MAX} 字符）")
    params: Dict[str, Any] = Field(
        default_factory=dict,
        description="口径参数：仅接受 index/method/block_size/threshold，多余键丢弃",
    )


class TemplateVO(BaseModel):
    """模板视图对象。"""

    id: str = Field(..., description="模板主键 UUID")
    user_id: str = Field(..., description="归属用户 ID")
    name: str = Field(..., description="模板名")
    params: Dict[str, Any] = Field(default_factory=dict,
                                   description="白名单过滤后的口径参数（前端回填用）")
    created_at: str = Field("", description="创建时间（ISO8601）")


class TemplateListResponse(BaseModel):
    """模板列表响应（按 created_at 倒序）。"""

    total: int = 0
    items: List[TemplateVO] = Field(default_factory=list)


class TemplateDeleteResponse(BaseModel):
    """删除成功响应。"""

    id: str
    deleted: bool = True


@router.post(
    "",
    response_model=TemplateVO,
    responses={
        400: {"model": ErrorResponse,
              "description": "EX1 口径参数枚举非法 / EX12 name 缺失或超长"},
        401: {"model": ErrorResponse, "description": "AUTH-01 未登录"},
        409: {"model": ErrorResponse, "description": "TMPL-01 同用户重名"},
        500: {"model": ErrorResponse, "description": "SYS-04 数据库异常"},
    },
    summary="创建检测参数模板（口径面板参数快照）",
)
def create_template(request: Request, payload: TemplateCreateRequest) -> TemplateVO:
    """保存口径面板当前参数为模板：白名单过滤 + 枚举校验后落库。"""
    user_id = current_user_id(request)
    return TemplateVO(**template_service.create_template(user_id, payload.name, payload.params))


@router.get(
    "",
    response_model=TemplateListResponse,
    responses={
        401: {"model": ErrorResponse, "description": "AUTH-01 未登录"},
        500: {"model": ErrorResponse, "description": "SYS-04 数据库异常"},
    },
    summary="本人模板列表（按创建时间倒序）",
)
def list_templates(request: Request) -> TemplateListResponse:
    """返回当前登录用户的全部模板，created_at 倒序（前端口径面板回填）。"""
    user_id = current_user_id(request)
    return TemplateListResponse(**template_service.list_templates(user_id))


@router.delete(
    "/{template_id}",
    response_model=TemplateDeleteResponse,
    responses={
        401: {"model": ErrorResponse, "description": "AUTH-01 未登录"},
        404: {"model": ErrorResponse, "description": "TMPL-02 模板不存在或属他人"},
        500: {"model": ErrorResponse, "description": "SYS-04 数据库异常"},
    },
    summary="删除检测参数模板（仅本人可删）",
)
def delete_template(request: Request, template_id: str) -> TemplateDeleteResponse:
    """仅本人可删；模板不存在或属他人一律 404（不泄漏存在性）。"""
    user_id = current_user_id(request)
    return TemplateDeleteResponse(**template_service.delete_template(user_id, template_id))
