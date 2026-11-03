# -*- coding: utf-8 -*-
"""路由：POST /api/v1/aoi/select（HLD 2.2.3.1，REQ-01/02）。

骨架阶段：只定义路径、HTTP 方法、Pydantic 请求/响应契约与占位返回值。
后续由 AoiService 完成跨度校验与 SceneRepository 完成 PostGIS 空间相交检索。
"""
from __future__ import annotations

from datetime import date
from typing import List

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.v1.schemas import ErrorResponse, SceneVO
from app.config.constants import AOI_MAX_SPAN_DEG

router = APIRouter(prefix="/aoi", tags=["AOI"])


class AoiSelectRequest(BaseModel):
    """AOI 提交请求（REQ-01）。"""

    aoi_geom: str = Field(..., description="GeoJSON Polygon（WGS84，闭合）")
    t1: date = Field(..., description="时相 1（YYYY-MM-DD）")
    t2: date = Field(..., description="时相 2（YYYY-MM-DD，要求 t2 > t1）")


class AoiSelectResponse(BaseModel):
    """AOI 提交响应（REQ-01/02）。"""

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
def select_aoi(payload: AoiSelectRequest) -> AoiSelectResponse:
    """AOI 提交与预置影像对匹配（REQ-01/02）。

    TODO(HLD 2.2.3.1)：
      1) AoiService.validate：闭合校验、跨度 ≤ {AOI_MAX_SPAN_DEG}° 校验（超限 EX1）；
      2) SceneRepository.findByAoi：PostGIS ST_Intersects 检索（无匹配 EX2）；
      3) 生成 run_id 与参数快照并写 aoi / task 表（REQ-14）。
    """
    # 占位返回值（骨架）：不连数据库、不做计算
    _ = (payload, AOI_MAX_SPAN_DEG)
    return AoiSelectResponse(
        aoi_id="",
        run_id="",
        area_deg2=0.0,
        matched_scenes=[],
    )
