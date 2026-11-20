# -*- coding: utf-8 -*-
"""路由：GET /api/v1/stats/overview（批次 H / REQ-13 扩展）。

薄封装：JWT 网关已在 ``main.py`` 全局生效，本路由只从 ``request.state.user_id``
取当前用户（ ``current_user_id`` 兼容兜底：部分 Starlette 版本 state 不跨层传递），
调 ``stats_service.overview`` 汇总，Pydantic 校验响应结构；不写任何 SQL / 算法逻辑。

错误封装沿用 ``app.api.errors``：数据库异常经 service 的 ``as_api_error`` →
``{code,message,run_id}``（SYS-04 等），不新增错误码。
"""
from __future__ import annotations

from typing import Dict, List, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.api.deps import current_user_id
from app.api.v1.schemas import ErrorResponse
from app.service import stats_service

__all__ = ["router"]

router = APIRouter(prefix="/stats", tags=["Stats"])


class AreaSummary(BaseModel):
    """三类面积合计（ha，两位）：1 增强 / 2 退化 / 3 稳定（REQ-13 扩展）。"""

    gain_ha: float = 0.0
    loss_ha: float = 0.0
    stable_ha: float = 0.0


class RecentRun(BaseModel):
    """最近 run 摘要（REQ-13 扩展）。"""

    run_id: str = Field(..., description="运行标识前 8 位")
    aoi_name: str = Field("", description="AOI / 影像对名称")
    index: str = Field("NDVI", description="本轮指数")
    date: str = Field("", description="成像/检测日期 YYYY-MM-DD")
    threshold: Optional[float] = Field(None, description="本轮阈值（缺则 null）")
    gain_ha: float = 0.0
    loss_ha: float = 0.0
    stable_ha: float = 0.0


class OverviewResponse(BaseModel):
    """平台总览视图（REQ-13 扩展）：空数据返回全零结构而非报错。"""

    total_runs: int = 0
    by_index: Dict[str, int] = Field(default_factory=lambda: {"NDVI": 0, "NDWI": 0})
    by_method: Dict[str, int] = Field(default_factory=dict)
    area: AreaSummary = Field(default_factory=AreaSummary)
    total_area_ha: float = 0.0
    changed_area_ha: float = 0.0
    skipped: int = 0
    last_run_at: Optional[str] = Field(None, description="最新 run 的 aoi.created_at（ISO）")
    recent: List[RecentRun] = Field(default_factory=list)


@router.get(
    "/overview",
    response_model=OverviewResponse,
    responses={500: {"model": ErrorResponse, "description": "SYS-04 数据库异常"}},
    summary="平台总览：本人 run 数 / 指数与方法分布 / 面积汇总 / 最近 10 条",
)
def overview(request: Request) -> OverviewResponse:
    """批次 H / REQ-13 扩展：只读、按人隔离（JWT 网关 + current_user_id 兜底）。"""
    user_id = getattr(request.state, "user_id", None) or current_user_id(request)
    return OverviewResponse(**stats_service.overview(user_id))
