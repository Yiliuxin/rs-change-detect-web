# -*- coding: utf-8 -*-
"""路由：批次 E · 只读实验接口（NF9 可复现 · REQ-07/11/14）。

  POST /api/v1/result/compare      方法对比（指数 / 检测方法 / 阈值方式变体）
  POST /api/v1/result/sensitivity  阈值敏感性（5 档 ``scale * T``）

薄封装：Pydantic 校验 + 归属校验 + 调 ``experiment_service`` + 统一异常封装
``{code, message, run_id}``。枚举/上限校验在 service 的 ``validate_variants``
（错误一律 ``ApiError``，避免 422）；复算/缓存全在 service 层。

**不产生新 run、不写 task 表、不落盘 GeoTIFF/GeoJSON/PNG/PDF 产物**，
仅写 ``runs/<run_id>/experiments.json`` 缓存（受 .gitignore 保护）。
JWT 由 ``main.py`` 网关中间件统一保护 ``/api/v1/*``。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.api.deps import ensure_run_owner
from app.api.errors import ApiError
from app.api.v1.schemas import ErrorResponse
from app.config.constants import (
    COMPARE_MAX_VARIANTS, ERROR_MESSAGES, VALID_CHANGE_METHODS, VALID_INDEXES,
)
from app.service import experiment_service

router = APIRouter(prefix="/result", tags=["Experiments"])


def ensure_experiment_owner(request: Request, run_id: str) -> None:
    """两个实验端点共用的归属校验（批次 E）。

    与 ``GET /runs/{id}`` 一致**不泄漏 run 存在性**：他人 run 的
    ``AUTH-03``(403) 一律转成 ``EX2``(404)；run 不存在的 ``EX2`` 及其它异常
    原样抛出。``AUTH_DISABLED=1`` 时 ``ensure_run_owner`` 直通，run 是否存在
    由 service 的 ``_load_meta`` / ``load_context`` 兜底判 EX2(404)。
    """
    try:
        ensure_run_owner(request, run_id)
    except ApiError as exc:
        if exc.code == "AUTH-03":
            raise ApiError("EX2", run_id=run_id,
                           message=ERROR_MESSAGES["EX2"]) from exc
        raise


class VariantConfig(BaseModel):
    """单个对比变体；枚举/缺省校验放到 service（此处不设约束，避免 422）。"""

    index: Optional[str] = Field(
        None, description=f"指数：{' | '.join(VALID_INDEXES)}（缺省取基准 index，upper 规范化）")
    method: Optional[str] = Field(
        None, description=f"检测方法：{' | '.join(VALID_CHANGE_METHODS)}（缺省取基准 method，lower 规范化）")
    threshold_mode: Optional[str] = Field(
        None, description="阈值方式：otsu | manual（缺省 otsu，lower 规范化）")
    threshold: Optional[float] = Field(
        None, description="manual 时必填，需落在 (-1, 1) 开区间且为有限数；otsu 时忽略")


class CompareRequest(BaseModel):
    """方法对比请求（批次 E · 只读实验，不产生新 run）。"""

    run_id: str = Field(..., description="已有运行标识（REQ-14）")
    # 故意不加 max_length：空 / 超过 COMPARE_MAX_VARIANTS 由 service 显式抛 EX12(400)
    variants: List[VariantConfig] = Field(
        default_factory=list,
        description=f"对比变体，至少 1 个、最多 {COMPARE_MAX_VARIANTS} 个",
    )
    force: bool = Field(False, description="true=忽略缓存重算并覆盖缓存条目")


class SensitivityRequest(BaseModel):
    """阈值敏感性请求（批次 E · 只读实验）。"""

    run_id: str = Field(..., description="已有运行标识（REQ-14）")
    force: bool = Field(False, description="true=忽略缓存重算并覆盖缓存条目")


class CompareResponse(BaseModel):
    """方法对比响应（字段口径逐字锁定， Dict/List 承载避免漂移）。"""

    base: Dict[str, Any] = Field(default_factory=dict, description="基准摘要")
    variants: List[Dict[str, Any]] = Field(default_factory=list, description="各变体复算结果")
    computed_at: str = Field("", description="UTC ISO8601 带时区；缓存命中原样带回")


class SensitivityResponse(BaseModel):
    """阈值敏感性响应。"""

    base_threshold: float = Field(0.0, description="基准阈值 T=|threshold|")
    sweep: List[Dict[str, Any]] = Field(default_factory=list, description="5 档扫描结果")
    computed_at: str = Field("", description="UTC ISO8601 带时区；缓存命中原样带回")


@router.post(
    "/compare",
    response_model=CompareResponse,
    responses={
        400: {"model": ErrorResponse,
              "description": "EX1 变体与基准 (index,method) 全同 / EX12 参数非法（含超过 4 个变体）"},
        403: {"model": ErrorResponse,
              "description": "AUTH-03 归属校验（实验端点对外统一以 404 EX2 呈现，不泄漏存在性）"},
        404: {"model": ErrorResponse,
              "description": "EX2 run 不存在 / 无权访问 / 双时相指数栅格产物缺失"},
        500: {"model": ErrorResponse,
              "description": "SYS-01 面积口径不可判定 / 栅格读取失败"},
    },
    summary="方法对比：基于已有 run 复算指数 / 检测方法 / 阈值方式变体（只读实验）",
)
def compare(request: Request, payload: CompareRequest) -> CompareResponse:
    """POST /result/compare（REQ-07/11/14 · NF9）。

    先归属校验（他人/不存在 run → EX2 404），再交 service 做变体校验、缓存与
    复算；只写 experiments.json 缓存，不产生新 run / 不写 task 表 / 不落盘产物。
    """
    ensure_experiment_owner(request, payload.run_id)
    data = experiment_service.compare(
        payload.run_id,
        [v.model_dump() for v in payload.variants],
        force=payload.force,
    )
    return CompareResponse(**data)


@router.post(
    "/sensitivity",
    response_model=SensitivityResponse,
    responses={
        400: {"model": ErrorResponse, "description": "EX12 参数非法"},
        403: {"model": ErrorResponse,
              "description": "AUTH-03 归属校验（实验端点对外统一以 404 EX2 呈现）"},
        404: {"model": ErrorResponse,
              "description": "EX2 run 不存在 / 无权访问 / 双时相指数栅格产物缺失"},
        500: {"model": ErrorResponse,
              "description": "SYS-01 面积口径不可判定 / 栅格读取失败"},
    },
    summary="阈值敏感性：以基准 T 扫 5 档固定阈值复算三类面积与过渡带（只读实验）",
)
def sensitivity(request: Request, payload: SensitivityRequest) -> SensitivityResponse:
    """POST /result/sensitivity（REQ-07/11/14 · NF9）。

    先归属校验（他人/不存在 run → EX2 404），再交 service 走 5 档 ``recompute``；
    只写 experiments.json 缓存，不产生新 run / 不写 task 表 / 不落盘产物。
    """
    ensure_experiment_owner(request, payload.run_id)
    data = experiment_service.sensitivity(payload.run_id, force=payload.force)
    return SensitivityResponse(**data)
