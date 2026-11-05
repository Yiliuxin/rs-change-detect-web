# -*- coding: utf-8 -*-
"""路由：POST /api/v1/result/change（HLD 2.2.3.3，REQ-05/06/07/08）。

薄封装：Pydantic 校验 + 调 change_service + 统一异常封装。
全链路编排（EX3/EX11 终止、EX5 告警、EX4/EX8/EX9/SYS-*、写库、三件套）在 service 层。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.v1.schemas import ErrorResponse, StatItem
from app.config.constants import (
    BLOCK_SIZE, MANUAL_THRESHOLD_RANGE, OTSU_FALLBACK_DNDVI, VALID_INDEXES,
)
from app.service import change_service

router = APIRouter(prefix="/result", tags=["Change"])


class ChangeRequest(BaseModel):
    """变化检测主流程请求（REQ-07）。"""

    run_id: str = Field(..., description="运行标识（REQ-14）")
    index: str = Field("NDVI", description=f"指数类型：{' | '.join(VALID_INDEXES)}（REQ-07，非法 → EX12）")
    block_size: int = Field(BLOCK_SIZE, description="分块尺寸，取常量源 BLOCK_SIZE（REQ-06）")
    threshold: Optional[float] = Field(
        None,
        description=f"手动阈值（提供则跳过 Otsu），需落在 {MANUAL_THRESHOLD_RANGE} 开区间，越界 → EX12",
    )


class ChangeResponse(BaseModel):
    """变化检测主流程响应（REQ-05/07/08/14）。

    新增字段均有默认值，不传 index/threshold 时响应口径与历史版本完全一致。
    """

    run_id: str = Field(..., description="运行标识（REQ-14）")
    change_geotiff: str = Field("", description="GeoTIFF 结果路径（REQ-12）")
    change_png: str = Field("", description="静态专题图 PNG 路径（REQ-10）")
    threshold: float = Field(OTSU_FALLBACK_DNDVI, description="实际阈值：Otsu 或手动（REQ-07）")
    valid_ratio: float = Field(0.0, description="有效像元占比（REQ-05）")
    stats: List[StatItem] = Field(default_factory=list, description="三类像元数 / 面积")
    params_snapshot: Dict[str, Any] = Field(default_factory=dict, description="参数快照（REQ-14）")
    index: str = Field("NDVI", description="本次使用的指数（REQ-07）")
    threshold_source: str = Field("otsu", description="阈值来源：otsu / manual（REQ-07/14）")
    artifacts: Dict[str, str] = Field(default_factory=dict,
                                      description="全部产物下载 URL 清单（REQ-10/12/14）")


@router.post(
    "/change",
    response_model=ChangeResponse,
    responses={400: {"model": ErrorResponse, "description": "EX3/EX4/EX11/EX12 参数非法"},
               409: {"model": ErrorResponse, "description": "EX7 并发超限 / EX8 口径混用 / EX9 基线不可判定"},
               500: {"model": ErrorResponse, "description": "SYS-01/02/03/04/05"}},
    summary="变化检测主流程",
)
def run_change(payload: ChangeRequest) -> ChangeResponse:
    """变化检测主流程（REQ-05/06/07/08）。

    EX5（0.10–0.50）为告警可继续档：返回 200，warning 置于 params_snapshot。
    index 非法 / threshold 越界由 service 抛 EX12（400）；缺省 index=NDVI、
    threshold=None 时行为与历史版本一致（Otsu 自适应）。
    """
    result = change_service.run(
        payload.run_id, payload.index, payload.block_size, payload.threshold
    )
    return ChangeResponse(**result)
