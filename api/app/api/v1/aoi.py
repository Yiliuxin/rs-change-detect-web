# -*- coding: utf-8 -*-
"""路由：POST /api/v1/aoi/select（HLD 2.2.3.1，REQ-01/02/14）。

薄封装：Pydantic 校验 + 调 aoi_service + 统一异常封装（由 errors.install 全局处理）。
业务逻辑（GeoJSON/EX1/run_id/参数快照/写 aoi+task/空间相交 EX2）全在 service 层。
"""
from __future__ import annotations

from datetime import date
from typing import List

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.api.v1.schemas import ErrorResponse, SceneVO
from app.config.constants import AOI_MAX_SPAN_DEG
from app.service import aoi_service

router = APIRouter(prefix="/aoi", tags=["AOI"])


class AoiSelectRequest(BaseModel):
    """AOI 提交请求（REQ-01）。"""

    aoi_geom: str = Field(..., description="GeoJSON Polygon（WGS84，闭合）")
    t1: date = Field(..., description="时相 1（YYYY-MM-DD）")
    t2: date = Field(..., description="时相 2（YYYY-MM-DD，要求 t2 > t1）")


class AoiSelectResponse(BaseModel):
    """AOI 提交响应（REQ-01/02/14）。"""

    aoi_id: str = Field(..., description="AOI 主键 UUID")
    run_id: str = Field(..., description="运行标识（REQ-14）")
    area_deg2: float = Field(..., description="AOI 面积（平方度）")
    matched_scenes: List[SceneVO] = Field(default_factory=list, description="匹配到的预置影像对")


@router.post(
    "/select",
    response_model=AoiSelectResponse,
    responses={400: {"model": ErrorResponse, "description": "EX1 AOI 跨度超限"},
               404: {"model": ErrorResponse, "description": "EX2 无匹配影像对"}},
    summary="AOI 提交与预置影像对匹配",
)
def select_aoi(request: Request, payload: AoiSelectRequest) -> AoiSelectResponse:
    """AOI 提交与预置影像对匹配（REQ-01/02/14）。

    service 完成：闭合/合法校验、跨度 ≤ {AOI_MAX_SPAN_DEG}°（EX1）、PostGIS
    ST_Intersects 检索（EX2）、生成 run_id/参数快照并写 aoi/task。
    """
    user_id = getattr(request.state, "user_id", None)
    result = aoi_service.select(payload.aoi_geom, payload.t1, payload.t2, user_id=user_id)
    return AoiSelectResponse(**result)
