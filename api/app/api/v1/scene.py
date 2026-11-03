# -*- coding: utf-8 -*-
"""路由：POST /api/v1/scene/match（HLD 2.2.3.2，REQ-02/03/04）。

骨架阶段：只定义路径、HTTP 方法、Pydantic 请求/响应契约与占位返回值。
后续由 SceneService 完成基线判定（EX9）、公式锁定（EX8）与 COG 范围读 + AOI 裁切（REQ-03）。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.v1.schemas import ClipWindow, ErrorResponse
from app.config.constants import BLOCK_SIZE

router = APIRouter(prefix="/scene", tags=["Scene"])


class SceneMatchRequest(BaseModel):
    """双时相场景匹配与辐射口径确认请求（REQ-02）。"""

    aoi_id: str = Field(..., description="AOI 主键 UUID")
    scene_t1_id: str = Field(..., description="时相 1 场景 UUID")
    scene_t2_id: str = Field(..., description="时相 2 场景 UUID")
    block_size: int = Field(BLOCK_SIZE, description="分块尺寸，取常量源 BLOCK_SIZE（256/512，REQ-03）")


class SceneMatchResponse(BaseModel):
    """双时相场景匹配与辐射口径确认响应（REQ-02/03/04）。"""

    run_id: str = Field(..., description="运行标识（REQ-14）")
    formula_id: str = Field(..., description="锁定的辐射定标公式标识（NF5）")
    proc_baseline: Optional[str] = Field(None, description="处理基线版次（S2 ≥04.00 / Landsat C2 L2）")
    clip_window: ClipWindow = Field(default_factory=ClipWindow, description="AOI 裁切窗口")
    valid_ratio: float = Field(0.0, description="掩膜前预估有效像元占比（REQ-05）")


@router.post(
    "/match",
    response_model=SceneMatchResponse,
    responses={400: {"model": ErrorResponse, "description": "EX8 辐射口径混用 / EX9 基线不可判定"},
               404: {"model": ErrorResponse, "description": "EX2 无匹配影像对"},
               500: {"model": ErrorResponse, "description": "SYS-01 COG 读取失败"}},
    summary="双时相场景匹配与辐射口径确认",
)
def match_scene(payload: SceneMatchRequest) -> SceneMatchResponse:
    """双时相场景匹配与辐射口径确认（REQ-02/03/04）。

    TODO(HLD 2.2.3.2)：
      1) BaselineChecker：proc_baseline 缺失或 < 04.00 → EX9；
      2) FormulaPolicy：双时相 sensor 不一致 → EX8；
      3) ClipService + CogReader：按 block_size 分块裁切（REQ-03）。
    """
    _ = payload
    return SceneMatchResponse(
        run_id="",
        formula_id="",
        proc_baseline=None,
        clip_window=ClipWindow(),
        valid_ratio=0.0,
    )
