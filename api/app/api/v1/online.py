# -*- coding: utf-8 -*-
"""路由：在线取数（B 级扩展，REQ-02 在线分支）。

| 方法 | 路径                                | 说明                              |
|------|-------------------------------------|-----------------------------------|
| POST | /api/v1/scene/discover              | STAC 检索候选 tile（纯检索）      |
| POST | /api/v1/scene/fetch                 | 按需下载入库，返回 fetch_id       |
| GET  | /api/v1/scene/fetch/{fetch_id}      | 查询取数进度与结果场景            |

与既有 5 个 MVP 路由**并存不改**：预置影像对（离线演示路径）走 aoi/scene 原接口，
在线取数为可选模式。本文件保持薄路由：Pydantic 校验 + 调 ``online_fetch`` service，
错误统一由 ``app.api.errors`` 全局封装为 ``{code, message, run_id}``。
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field, model_validator

from app.api.v1.schemas import ErrorResponse
from app.service import online_fetch

router = APIRouter(prefix="/scene", tags=["SceneOnline"])

# 时相窗口/日期的正则：允许纯日期或带时间（服务层统一取前 10 位解析）
_DATE_TEXT_REGEX = r"^\d{4}-\d{2}-\d{2}([T ].*)?$"


class DiscoverRequest(BaseModel):
    """STAC 检索请求（B 级扩展）：aoi_geom 或 bbox 二选一 + 两时相窗口。"""

    aoi_geom: Optional[str] = Field(None, description="GeoJSON Polygon（WGS84，闭合）")
    bbox: Optional[List[float]] = Field(None, description="AOI 外包框 [minx,miny,maxx,maxy]")
    t1_window: List[str] = Field(..., description="时相 1 窗口 [start, end]（YYYY-MM-DD）")
    t2_window: List[str] = Field(..., description="时相 2 窗口 [start, end]（YYYY-MM-DD）")

    @model_validator(mode="after")
    def _require_aoi(self) -> "DiscoverRequest":
        if not self.aoi_geom and not self.bbox:
            raise ValueError("需提供 aoi_geom 或 bbox 之一")
        if len(self.t1_window) != 2 or len(self.t2_window) != 2:
            raise ValueError("t1_window / t2_window 需为 [start, end] 两项")
        return self


class PhaseCandidate(BaseModel):
    """单时相候选（discover 响应 t1 / t2 子对象）。"""

    source_id: str = Field(..., description="STAC 场景标识（如 S2B_49RFN_20230805_0_L2A）")
    date: str = Field(..., description="成像日期（YYYY-MM-DD）")
    cloud_cover: Optional[float] = Field(None, description="云量（%）")
    proc_baseline: Optional[str] = Field(None, description="处理基线版次（≥04.00）")


class DiscoverCandidate(BaseModel):
    """两时相共同 tile 的候选影像对。"""

    tile: str = Field(..., description="MGRS 景号（如 49RFN）")
    t1: PhaseCandidate
    t2: PhaseCandidate
    combined_cloud: Optional[float] = Field(None, description="综合云量（t1 + t2）")


class DiscoverResponse(BaseModel):
    """STAC 检索响应（按综合云量升序）。"""

    candidates: List[DiscoverCandidate] = Field(default_factory=list)
    run_id: str = Field(..., description="本次检索标识（可追溯）")


class PhaseSelect(BaseModel):
    """fetch 请求中的单时相选择（直接取自 discover 候选）。"""

    source_id: str = Field(..., min_length=1, description="STAC 场景标识")
    date: str = Field(..., pattern=_DATE_TEXT_REGEX, description="成像日期 YYYY-MM-DD")
    proc_baseline: str = Field(..., min_length=1, description="处理基线版次（discover 返回）")
    cloud_cover: Optional[float] = Field(None, description="云量（%）")


class FetchRequest(BaseModel):
    """按需下载入库请求（B 级扩展）。"""

    name: str = Field(..., pattern=r"^[a-z0-9_]{1,40}$",
                      description="取数组名，规范为 [a-z0-9_]（用作 pair_<name> 与 uuid5 种子）")
    aoi_geom: Optional[str] = Field(None, description="GeoJSON Polygon（WGS84，闭合）")
    bbox: Optional[List[float]] = Field(None, description="AOI 外包框 [minx,miny,maxx,maxy]")
    tile: str = Field(..., pattern=r"^[0-9]{2}[A-Za-z]{3}$", description="MGRS 景号（如 49RFN）")
    t1: PhaseSelect
    t2: PhaseSelect

    @model_validator(mode="after")
    def _require_aoi(self) -> "FetchRequest":
        if not self.aoi_geom and not self.bbox:
            raise ValueError("需提供 aoi_geom 或 bbox 之一")
        return self


class FetchStartResponse(BaseModel):
    """取数任务受理响应。"""

    fetch_id: str = Field(..., description="取数任务标识（UUID）")
    status: str = Field(..., description="PENDING / RUNNING（同时提交的重复请求返回已有任务）")


class FetchProgress(BaseModel):
    """进度（以波段为单位，一组 10 波段）。"""

    done: int = 0
    total: int = online_fetch.TOTAL_BANDS


class FetchError(BaseModel):
    """失败信息（EX1 / EX2 / SYS-06 / SYS-07）。"""

    code: str
    message: str


class FetchStatusResponse(BaseModel):
    """取数任务状态响应。"""

    fetch_id: str
    status: str = Field(..., description="PENDING / RUNNING / SUCCESS / FAILED")
    progress: FetchProgress = Field(default_factory=FetchProgress)
    error: Optional[FetchError] = None
    scene_ids: Optional[dict] = Field(None, description="成功后的 {t1, t2} scene_id")


@router.post(
    "/discover",
    response_model=DiscoverResponse,
    responses={400: {"model": ErrorResponse, "description": "EX1 AOI 跨度超限"},
               404: {"model": ErrorResponse, "description": "EX2 两时相无共同 tile"},
               500: {"model": ErrorResponse, "description": "SYS-06 STAC 不可达"}},
    summary="STAC 检索候选影像对（在线取数）",
)
def discover_scenes(payload: DiscoverRequest) -> DiscoverResponse:
    """STAC 检索候选 tile（B 级扩展）：过滤版次 ≥04.00，按综合云量升序，不下载。"""
    result = online_fetch.discover(
        payload.aoi_geom, payload.bbox, payload.t1_window, payload.t2_window
    )
    return DiscoverResponse(**result)


@router.post(
    "/fetch",
    response_model=FetchStartResponse,
    responses={400: {"model": ErrorResponse, "description": "EX1 / 参数非法"},
               500: {"model": ErrorResponse, "description": "SYS-06 下载失败 / SYS-07 写入失败"}},
    summary="按需下载入库（在线取数，后台线程）",
)
def fetch_scenes(payload: FetchRequest) -> FetchStartResponse:
    """受理取数任务（B 级扩展）：返回 fetch_id，进度由 GET 轮询。"""
    result = online_fetch.start_fetch(
        payload.name, payload.aoi_geom, payload.bbox, payload.tile,
        payload.t1.model_dump(), payload.t2.model_dump(),
    )
    return FetchStartResponse(**result)


@router.get(
    "/fetch/{fetch_id}",
    response_model=FetchStatusResponse,
    responses={404: {"model": ErrorResponse, "description": "EX2 未知 fetch_id"}},
    summary="查询在线取数进度",
)
def get_fetch_status(fetch_id: str) -> FetchStatusResponse:
    """查询取数进度（B 级扩展）：progress 以波段为单位（0/10 → 10/10）。"""
    return FetchStatusResponse(**online_fetch.get_status(fetch_id))
