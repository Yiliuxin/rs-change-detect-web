# -*- coding: utf-8 -*-
"""路由：POST /api/v1/result/change（HLD 2.2.3.3，REQ-05/06/07/08）。

薄封装：Pydantic 校验 + 调 change_service + 统一异常封装。
全链路编排（EX3/EX11 终止、EX5 告警、EX4/EX8/EX9/SYS-*、写库、三件套）在 service 层。
"""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.v1.schemas import ErrorResponse, StatItem
from app.config.constants import BLOCK_SIZE, OTSU_FALLBACK_DNDVI
from app.service import change_service

router = APIRouter(prefix="/result", tags=["Change"])


class ChangeRequest(BaseModel):
    """变化检测主流程请求（REQ-07）。"""

    run_id: str = Field(..., description="运行标识（REQ-14）")
    index: str = Field("NDVI", description="指数类型，MVP 仅 NDVI（REQ-07）")
    block_size: int = Field(BLOCK_SIZE, description="分块尺寸，取常量源 BLOCK_SIZE（REQ-06）")


class ChangeResponse(BaseModel):
    """变化检测主流程响应（REQ-05/07/08/14）。"""

    run_id: str = Field(..., description="运行标识（REQ-14）")
    change_geotiff: str = Field("", description="GeoTIFF 结果路径（REQ-12）")
    change_png: str = Field("", description="静态专题图 PNG 路径（REQ-10）")
    threshold: float = Field(OTSU_FALLBACK_DNDVI, description="Otsu 阈值，回退时为 0.1（REQ-07）")
    valid_ratio: float = Field(0.0, description="有效像元占比（REQ-05）")
    stats: List[StatItem] = Field(default_factory=list, description="三类像元数 / 面积")
    params_snapshot: Dict[str, Any] = Field(default_factory=dict, description="参数快照（REQ-14）")


@router.post(
    "/change",
    response_model=ChangeResponse,
    responses={400: {"model": ErrorResponse, "description": "EX3/EX4/EX8/EX9/EX11"},
               429: {"model": ErrorResponse, "description": "EX7 并发超限"},
               500: {"model": ErrorResponse, "description": "SYS-01/02/03/04/05"}},
    summary="变化检测主流程",
)
def run_change(payload: ChangeRequest) -> ChangeResponse:
    """变化检测主流程（REQ-05/06/07/08）。

    EX5（0.10–0.50）为告警可继续档：返回 200，warning 置于 params_snapshot。
    """
    result = change_service.run(payload.run_id, payload.index, payload.block_size)
    return ChangeResponse(**result)
