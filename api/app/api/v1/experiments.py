# -*- coding: utf-8 -*-
"""路由：批次 E · 只读实验接口（NF9 可复现 · REQ-07/11/14）。

  POST /api/v1/result/compare      方法对比（指数 / 检测方法 / 阈值方式变体）
  POST /api/v1/result/sensitivity  阈值敏感性（5 档 ``scale * T``）
  POST /api/v1/result/reclass      批次 K 阈值滑块：单倍数只读复算 + 内存专题图

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
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app.api.deps import ensure_run_owner
from app.api.errors import ApiError
from app.api.v1.schemas import ErrorResponse
from app.config.constants import (
    COMPARE_MAX_VARIANTS, ERROR_MESSAGES, VALID_CHANGE_METHODS, VALID_INDEXES,
)
from app.repository import task_repo
from app.service import experiment_service, run_service

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


class ReclassRequest(BaseModel):
    """阈值滑块单倍数请求（批次 K · 只读预览）。

    scale 故意不设 Pydantic 约束：越界 / 非数值由 service 抛 EX12(400)，
    避免默认 422 破坏 ``{code,message,run_id}`` 错误契约。
    """

    run_id: str = Field(..., description="已有运行标识（REQ-14）")
    scale: Any = Field(None, description="相对基准 T 的倍数：0.6~1.4，步进 0.05")


class ReclassResponse(BaseModel):
    """阈值滑块预览响应：阈值 / 三类面积 / 过渡带 / 渲染数据（Dict 承载避免漂移）。"""

    run_id: str = Field("", description="原 run_id（不产生新 run）")
    scale: float = Field(1.0, description="本次倍数")
    base_threshold: float = Field(0.0, description="基准阈值 T=|threshold|")
    threshold: float = Field(0.0, description="换算后阈值 scale*T（保留 4 位小数展示）")
    stats: List[Dict[str, Any]] = Field(default_factory=list, description="三类面积（恒 3 项）")
    transition_pct: float = Field(0.0, description="过渡带占比（0~100 百分数）")
    transition_band: Dict[str, Any] = Field(default_factory=dict, description="过渡带完整摘要")
    index_histogram: Dict[str, Any] = Field(default_factory=dict, description="直方图（阈值线/过渡带区间已更新）")
    change_png_data: str = Field("", description="内存渲染专题图 data:image/png;base64,...（不落盘）")
    computed_at: str = Field("", description="UTC ISO8601 带时区")


@router.post(
    "/reclass",
    response_model=ReclassResponse,
    responses={
        400: {"model": ErrorResponse, "description": "EX12 倍数越界 / 非 0.05 网格 / 非有限数"},
        404: {"model": ErrorResponse,
              "description": "EX2 run 不存在 / 无权访问 / 双时相指数栅格产物缺失"},
        500: {"model": ErrorResponse, "description": "SYS-01 面积口径不可判定 / 渲染失败"},
    },
    summary="阈值滑块预览：单倍数只读复算三类面积/过渡带/专题图（不落盘、不产生新 run）",
)
def reclass(request: Request, payload: ReclassRequest) -> ReclassResponse:
    """POST /result/reclass（批次 K · NF9 同口径）。

    先归属校验（他人/不存在 run → 404 EX2），再交 service 复用 ``recompute``
    单倍数复算；不读写 experiments.json 缓存、不改基准参数、不落盘新产物，
    页面刷新即回到基准 T。
    """
    ensure_experiment_owner(request, payload.run_id)
    return ReclassResponse(**experiment_service.reclass(payload.run_id, payload.scale))


# --------------------------------------------------------------------------- #
# 批次 F · 只读实验数据 CSV 导出（REQ-12/14）
#
# 严格只读：只从 experiments.json 缓存读，绝不调用 compare() / sensitivity()、
# 绝不重算、绝不写缓存、绝不落盘。force 接受但**忽略**。
# 他人 / 不存在 run → 404 EX2；缓存未命中 → 404 EX3（请先在页面执行实验）。
# --------------------------------------------------------------------------- #
@router.post(
    "/compare/csv",
    response_class=PlainTextResponse,
    responses={
        400: {"model": ErrorResponse,
              "description": "EX1 / EX12 变体参数非法（同主接口签名口径）"},
        404: {"model": ErrorResponse,
              "description": "EX2 run 不存在 / 无权访问；EX3 缓存未命中（请先在页面执行实验）"},
        500: {"model": ErrorResponse, "description": "SYS-*"},
    },
    summary="方法对比 CSV 导出（UTF-8 BOM，只读缓存，不触发重算）",
)
def compare_csv(request: Request, payload: CompareRequest) -> PlainTextResponse:
    """POST /result/compare/csv（批次 F · REQ-12/14）。

    入参与 ``/result/compare`` 完全相同（``force`` 接受但**忽略**）。以与主接口
    完全相同的签名定位缓存；未命中 404 EX3，绝不悄悄复算。
    """
    ensure_experiment_owner(request, payload.run_id)
    # AUTH_DISABLED=1 时 ensure_run_owner 直通且不查 run，必须显式判空 → EX2(404)
    if not task_repo.get_task(payload.run_id):
        raise ApiError("EX2", run_id=payload.run_id, message=ERROR_MESSAGES["EX2"])
    cached = experiment_service.read_compare_cached(
        payload.run_id, [v.model_dump() for v in payload.variants],
    )
    if cached is None:
        raise ApiError("EX3", run_id=payload.run_id, http_status=404,
                       message="请先在页面执行实验")
    return PlainTextResponse(
        content=run_service.CSV_BOM + experiment_service.compare_csv(cached),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="compare_{payload.run_id[:8]}.csv"'},
    )


@router.post(
    "/sensitivity/csv",
    response_class=PlainTextResponse,
    responses={
        404: {"model": ErrorResponse,
              "description": "EX2 run 不存在 / 无权访问；EX3 缓存未命中（请先在页面执行实验）"},
        500: {"model": ErrorResponse, "description": "SYS-*"},
    },
    summary="阈值敏感性 CSV 导出（UTF-8 BOM，只读缓存，不触发重算）",
)
def sensitivity_csv(request: Request, payload: SensitivityRequest) -> PlainTextResponse:
    """POST /result/sensitivity/csv（批次 F · REQ-12/14）。

    入参与 ``/result/sensitivity`` 相同（``force`` 接受但**忽略**）；只读缓存，
    未命中 404 EX3，不调用 sensitivity()、不写缓存、不落盘。
    """
    ensure_experiment_owner(request, payload.run_id)
    if not task_repo.get_task(payload.run_id):
        raise ApiError("EX2", run_id=payload.run_id, message=ERROR_MESSAGES["EX2"])
    cached = experiment_service.read_sensitivity_cached(payload.run_id)
    if cached is None:
        raise ApiError("EX3", run_id=payload.run_id, http_status=404,
                       message="请先在页面执行实验")
    return PlainTextResponse(
        content=run_service.CSV_BOM + experiment_service.sensitivity_csv(cached),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="sensitivity_{payload.run_id[:8]}.csv"'},
    )
