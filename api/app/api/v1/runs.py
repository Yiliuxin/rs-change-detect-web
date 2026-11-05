# -*- coding: utf-8 -*-
"""路由：GET /api/v1/runs 与 GET /api/v1/runs/{run_id}（REQ-13 / REQ-14）。

薄封装：Query 参数 → run_service 编排 → 统一异常封装。列表按 created_at 倒序
分页；详情含 params_snapshot 全量与全部产物下载 URL；run 不存在 → EX2 404。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.api.v1.schemas import ErrorResponse
from app.service import run_service

router = APIRouter(prefix="/runs", tags=["Runs"])


class RunSummary(BaseModel):
    """历史 run 列表项（REQ-14）。"""

    run_id: str = Field(..., description="运行标识")
    aoi_id: str = Field("", description="AOI 主键")
    aoi_name: str = Field("", description="AOI 名称（由预置影像对组名派生）")
    bbox: Optional[List[float]] = Field(None, description="AOI 外接矩形 [minx,miny,maxx,maxy]")
    t1: Optional[str] = Field(None, description="时相 1 日期")
    t2: Optional[str] = Field(None, description="时相 2 日期")
    t1_source: str = Field("", description="时相 1 数据源（scene.cog_path）")
    t2_source: str = Field("", description="时相 2 数据源（scene.cog_path）")
    sensor: str = Field("", description="传感器")
    created_at: Optional[str] = Field(None, description="run 创建时间（ISO8601）")
    threshold: Optional[float] = Field(None, description="本轮实际阈值")
    threshold_source: Optional[str] = Field(None, description="otsu / manual")
    index: str = Field("NDVI", description="本轮指数")
    status: Optional[str] = Field(None, description="PENDING / RUNNING / SUCCESS / FAILED")
    total_area_ha: float = Field(0.0, description="三类面积合计（公顷）")
    total_pixel_count: int = Field(0, description="三类像元数合计")


class RunListResponse(BaseModel):
    """分页列表响应（默认 limit=20，按 created_at 倒序）。"""

    total: int = 0
    limit: int = run_service.DEFAULT_PAGE_LIMIT
    offset: int = 0
    items: List[RunSummary] = Field(default_factory=list)


class RunArtifact(BaseModel):
    """产物下载项（URL 复用 ``common.artifact_url``）。"""

    name: str = ""
    url: str = ""
    size: int = 0


class RunDetailResponse(RunSummary):
    """历史 run 详情：摘要 + 参数快照全量 + 全部产物 URL。"""

    params_snapshot: Dict[str, Any] = Field(default_factory=dict)
    artifacts: List[RunArtifact] = Field(default_factory=list)


@router.get(
    "",
    response_model=RunListResponse,
    responses={400: {"model": ErrorResponse, "description": "EX12 分页参数非法"},
               500: {"model": ErrorResponse, "description": "SYS-04 数据库异常"}},
    summary="历史 run 列表（分页，按创建时间倒序）",
)
def list_runs(
    limit: int = Query(run_service.DEFAULT_PAGE_LIMIT, description="每页条数，默认 20，1..100"),
    offset: int = Query(0, description="偏移量，默认 0"),
) -> RunListResponse:
    """REQ-14：返回 run_id / AOI 名称与 bbox / 双时相日期与 source / created_at /
    threshold / 三类面积合计。"""
    return RunListResponse(**run_service.list_runs(limit, offset))


@router.get(
    "/{run_id}",
    response_model=RunDetailResponse,
    responses={404: {"model": ErrorResponse, "description": "EX2 run 不存在"},
               500: {"model": ErrorResponse, "description": "SYS-04 数据库异常"}},
    summary="历史 run 详情（参数快照 + 全部产物下载 URL）",
)
def get_run(run_id: str) -> RunDetailResponse:
    """REQ-14：params_snapshot 全量 + 全部产物下载 URL；run 不存在 → EX2 404。"""
    return RunDetailResponse(**run_service.detail(run_id))
