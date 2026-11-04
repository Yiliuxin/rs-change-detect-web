# -*- coding: utf-8 -*-
"""路由：scene/match（HLD 2.2.3.2，REQ-02/03/04）。

- POST /api/v1/scene/match：保留原 SceneMatchResponse 契约（前端在用）。
- GET  /api/v1/scene/match：新增，返回匹配影像对清单（用户明确要求）。

薄封装：Pydantic 校验 + 调 scene_service + 统一异常封装（errors.install 全局处理）。
"""
from __future__ import annotations

from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.api.v1.schemas import ClipWindow, ErrorResponse
from app.config.constants import BLOCK_SIZE
from app.service import scene_service

router = APIRouter(prefix="/scene", tags=["Scene"])


class SceneMatchRequest(BaseModel):
    """双时相场景匹配与辐射口径确认请求（REQ-02，POST 契约保持）。"""

    aoi_id: str = Field(..., description="AOI 主键 UUID")
    scene_t1_id: str = Field(..., description="时相 1 场景 UUID")
    scene_t2_id: str = Field(..., description="时相 2 场景 UUID")
    block_size: int = Field(BLOCK_SIZE, description="分块尺寸，取常量源 BLOCK_SIZE（256/512，REQ-03）")


class SceneMatchResponse(BaseModel):
    """双时相场景匹配与辐射口径确认响应（REQ-02/03/04，POST 契约保持）。"""

    run_id: str = Field(..., description="运行标识（REQ-14）")
    formula_id: str = Field(..., description="锁定的辐射定标公式标识（NF5）")
    proc_baseline: Optional[str] = Field(None, description="处理基线版次（S2 ≥04.00 / Landsat C2 L2）")
    clip_window: ClipWindow = Field(default_factory=ClipWindow, description="AOI 裁切窗口")
    valid_ratio: float = Field(0.0, description="掩膜前预估有效像元占比（REQ-05）")


class SceneMatchItem(BaseModel):
    """GET 清单单项：日期/云量/tile/基线/sensor/cog_path（REQ-02）。"""

    scene_id: str = ""
    sensor: str = ""
    acq_date: Optional[str] = None
    cloud_cover: Optional[float] = None
    tile: Optional[str] = None
    proc_baseline: Optional[str] = None
    cog_path: str = ""


class ScenePairListResponse(BaseModel):
    """GET 匹配影像对清单响应（REQ-02/04）。"""

    aoi_id: Optional[str] = None
    run_id: Optional[str] = None
    t1: Optional[str] = None
    t2: Optional[str] = None
    block_size: int = BLOCK_SIZE
    sensor: str = ""
    formula_id: str = ""
    proc_baseline: Optional[str] = None
    count: int = 0
    scenes: List[SceneMatchItem] = Field(default_factory=list)


@router.post(
    "/match",
    response_model=SceneMatchResponse,
    responses={400: {"model": ErrorResponse, "description": "其余 EX* 业务错误（400）"},
               404: {"model": ErrorResponse, "description": "EX2 无匹配影像对"},
               409: {"model": ErrorResponse, "description": "EX8 口径混用 / EX9 基线不可判定"},
               500: {"model": ErrorResponse, "description": "SYS-01 COG 读取失败"}},
    summary="双时相场景匹配与辐射口径确认（POST 契约）",
)
def match_scene(payload: SceneMatchRequest) -> SceneMatchResponse:
    """双时相场景匹配与辐射口径确认（REQ-02/03/04）。保持原契约，补真实值。"""
    result = scene_service.match_post(
        payload.aoi_id, payload.scene_t1_id, payload.scene_t2_id, payload.block_size
    )
    return SceneMatchResponse(**result)


@router.get(
    "/match",
    response_model=ScenePairListResponse,
    responses={400: {"model": ErrorResponse, "description": "其余 EX* 业务错误（400）"},
               404: {"model": ErrorResponse, "description": "EX2 无匹配影像对"},
               409: {"model": ErrorResponse, "description": "EX8 口径混用 / EX9 基线不可判定"}},
    summary="查询匹配影像对清单（GET）",
)
def list_matched_scenes(
    aoi_id: Optional[str] = Query(None, description="AOI 主键 UUID（可选，按 footprint 相交）"),
    t1: Optional[date] = Query(None, description="时相 1 日期"),
    t2: Optional[date] = Query(None, description="时相 2 日期"),
    scene_t1_id: Optional[str] = Query(None, description="显式指定时相 1 场景 UUID"),
    scene_t2_id: Optional[str] = Query(None, description="显式指定时相 2 场景 UUID"),
    block_size: int = Query(BLOCK_SIZE, description="分块尺寸"),
) -> ScenePairListResponse:
    """查询匹配影像对清单（REQ-02/04）：sensor 不一致 → EX8，基线不达标 → EX9，无匹配 → EX2。"""
    result = scene_service.match_get(
        aoi_id, t1, t2, scene_t1_id, scene_t2_id, block_size
    )
    return ScenePairListResponse(**result)
