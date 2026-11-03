# -*- coding: utf-8 -*-
"""路由：POST /api/v1/accuracy/sample（HLD 2.2.3.4，REQ-09）。

骨架阶段：只定义路径、HTTP 方法、Pydantic 请求/响应契约与占位返回值。
后续由 SampleService 构建混淆矩阵并计算 OA / Kappa（不足 30 点/类 → EX10）。
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.v1.schemas import ErrorResponse, SampleItem
from app.config.constants import KAPPA_TEACHING_MIN, MIN_SAMPLES_PER_CLASS

router = APIRouter(prefix="/accuracy", tags=["Accuracy"])


class AccuracySampleRequest(BaseModel):
    """样本提交与精度评价请求（REQ-09）。"""

    run_id: str = Field(..., description="运行标识（REQ-14）")
    samples: List[SampleItem] = Field(default_factory=list, description="用户勾选的样本点")


class AccuracySampleResponse(BaseModel):
    """样本提交与精度评价响应（REQ-09）。"""

    confusion_matrix: List[List[int]] = Field(default_factory=list, description="3×3 混淆矩阵")
    overall_accuracy: float = Field(0.0, description="总体精度 OA")
    kappa: float = Field(0.0, description="Kappa 系数")
    note: Optional[str] = Field(None, description="Kappa < 0.6 时为「教学级，非工程级」")


@router.post(
    "/sample",
    response_model=AccuracySampleResponse,
    responses={400: {"model": ErrorResponse, "description": "EX10 任一类样本 < 30 点"}},
    summary="样本提交与精度评价",
)
def submit_samples(payload: AccuracySampleRequest) -> AccuracySampleResponse:
    """样本提交与精度评价（REQ-09）。

    TODO(HLD 2.2.3.4)：
      1) 校验每类样本数 ≥ MIN_SAMPLES_PER_CLASS，否则 EX10；
      2) ConfusionMatrixBuilder + AccuracyMetrics 计算 OA / Kappa；
      3) Kappa < KAPPA_TEACHING_MIN 时返回「教学级，非工程级」标注。
    """
    _ = (payload, MIN_SAMPLES_PER_CLASS, KAPPA_TEACHING_MIN)
    return AccuracySampleResponse()
