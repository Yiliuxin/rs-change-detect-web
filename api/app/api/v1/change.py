# -*- coding: utf-8 -*-
"""路由：POST /api/v1/result/change（HLD 2.2.3.3，REQ-05/06/07/08）。

薄封装：Pydantic 校验 + 调 change_service + 统一异常封装。
全链路编排（EX3/EX11 终止、EX5 告警、EX4/EX8/EX9/SYS-*、写库、三件套）在 service 层。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.api.v1.schemas import ErrorResponse, StatItem
from app.config.constants import (
    BLOCK_SIZE, MANUAL_THRESHOLD_RANGE, METHOD_OTSU, OTSU_FALLBACK_DNDVI,
    VALID_CHANGE_METHODS, VALID_INDEXES,
)
from app.service import change_async, change_service
from app.service.change_async import STAGES

router = APIRouter(prefix="/result", tags=["Change"])


class ChangeRequest(BaseModel):
    """变化检测主流程请求（REQ-07 / REQ-13 异步化）。"""

    model_config = ConfigDict(populate_by_name=True)

    run_id: str = Field(..., description="运行标识（REQ-14）")
    index: str = Field("NDVI", description=f"指数类型：{' | '.join(VALID_INDEXES)}（REQ-07，非法 → EX12）")
    method: str = Field(
        METHOD_OTSU,
        description=(f"检测方法：{' | '.join(VALID_CHANGE_METHODS)}（REQ-11，缺省 otsu 差值法；"
                     "regression=NDVI 回归残差法，非法 → EX12）"),
    )
    block_size: int = Field(BLOCK_SIZE, description="分块尺寸，取常量源 BLOCK_SIZE（REQ-06）")
    threshold: Optional[float] = Field(
        None,
        description=f"手动阈值（提供则跳过 Otsu），需落在 {MANUAL_THRESHOLD_RANGE} 开区间，越界 → EX12",
    )
    # REQ-13：异步受理（后台线程 + 阶段进度轮询）；alias 兼容 JSON 键 ``async``
    async_mode: bool = Field(False, alias="async",
                            description="true=异步受理，立即返回 PENDING 并由 GET /task/{id} 轮询")


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
    method: str = Field(METHOD_OTSU, description="本次使用的检测方法：otsu / regression（REQ-11）")
    threshold_source: str = Field("otsu", description="阈值来源：otsu / manual（REQ-07/14）")
    artifacts: Dict[str, str] = Field(default_factory=dict,
                                      description="全部产物下载 URL 清单（REQ-10/12/14）")


class ChangeAsyncAccept(BaseModel):
    """异步受理响应（REQ-13）：立即返回 PENDING，task_id 即 run_id。"""

    run_id: str = Field(..., description="运行标识（REQ-14）")
    task_id: str = Field(..., description="异步任务标识（与 run_id 一致）")
    status: str = Field(..., description="PENDING / RUNNING（受理即 PENDING）")


class ChangeProgress(BaseModel):
    """阶段进度（5 阶段：读裁切→掩膜→指数→变化检测→渲染导出）。"""

    stage: str = Field("", description="当前阶段名（空串表示未开始）")
    step: int = Field(0, description="已完成阶段数（1–5）")
    total: int = Field(len(STAGES), description="阶段总数（5）")


class ChangeError(BaseModel):
    """失败信息（沿用既有错误码 EX*/SYS-*，中文文案取 ERROR_MESSAGES）。"""

    code: str
    message: str


class ChangeTaskStatusResponse(BaseModel):
    """异步任务状态响应（REQ-13 轮询）。"""

    task_id: str
    status: str = Field(..., description="PENDING / RUNNING / SUCCESS / FAILED")
    progress: Optional[ChangeProgress] = None
    error: Optional[ChangeError] = None
    result: Optional[ChangeResponse] = None


@router.post(
    "/change",
    response_model=None,
    responses={
        200: {"model": ChangeResponse, "description": "同步模式：完整 ChangeResponse"},
        400: {"model": ErrorResponse, "description": "EX3/EX4/EX11/EX12 参数非法"},
        409: {"model": ErrorResponse, "description": "EX7 并发超限 / EX8 口径混用 / EX9 基线不可判定"},
        500: {"model": ErrorResponse, "description": "SYS-01/02/03/04/05"},
    },
    summary="变化检测主流程（同步 / 异步受理）",
)
def run_change(payload: ChangeRequest):
    """变化检测主流程（REQ-05/06/07/08/13）。

    EX5（0.10–0.50）为告警可继续档：返回 200，warning 置于 params_snapshot。
    index 非法 / threshold 越界由 service 抛 EX12（400）；缺省 index=NDVI、
    threshold=None 时行为与历史版本一致（Otsu 自适应）。
    async=true 时过并发闸门后立即返回 ``{run_id, task_id, status:PENDING}``，
    后台线程执行完整管线，进度由 ``GET /result/change/task/{task_id}`` 轮询。
    """
    if payload.async_mode:
        accept = change_async.start_change(
            payload.run_id, payload.index, payload.block_size, payload.threshold,
            payload.method,
        )
        return ChangeAsyncAccept(**accept)
    result = change_service.run(
        payload.run_id, payload.index, payload.block_size, payload.threshold,
        payload.method,
    )
    result.setdefault("method", METHOD_OTSU)
    return ChangeResponse(**result)


@router.get(
    "/change/task/{task_id}",
    response_model=ChangeTaskStatusResponse,
    responses={404: {"model": ErrorResponse, "description": "EX2 未知 task_id"}},
    summary="查询异步变化检测任务状态与阶段进度",
)
def get_change_task(task_id: str) -> ChangeTaskStatusResponse:
    """轮询异步任务（REQ-13）：status + 5 阶段进度 + 失败 error + 成功 result。

    未知 task_id → EX2(404)。
    """
    return ChangeTaskStatusResponse(**change_async.get_status(task_id))
