# -*- coding: utf-8 -*-
"""路由：POST /api/v1/accuracy/sample + /auto_sample（HLD 2.2.3.4，REQ-09）。

薄封装：Pydantic 校验 + 调 sample_service + 统一异常封装。
样本入库 / EX10 / OA / Kappa / 分层抽样全在 service + algorithm 层。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.v1.schemas import ErrorResponse, SampleItem
from app.config.constants import (
    AUTO_SAMPLE_PER_CLASS_DEFAULT, AUTO_SAMPLE_PER_CLASS_MAX,
    KAPPA_TEACHING_MIN, MIN_SAMPLES_PER_CLASS,
)
from app.service import sample_service

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


class AutoSampleRequest(BaseModel):
    """半自动分层抽样请求（REQ-09 自动化）。

    ``per_class`` 故意不声明 int：任意非法取值都交给 service 统一映射为 EX12(400)，
    避免 FastAPI 原生 422 绕过既有 ``{code,message,run_id}`` 错误体契约。
    """

    run_id: str = Field(..., description="运行标识（REQ-14）")
    per_class: Any = Field(
        AUTO_SAMPLE_PER_CLASS_DEFAULT,
        description=(f"每类抽样数，需为 (0, {AUTO_SAMPLE_PER_CLASS_MAX}] 内整数"
                     f"（缺省 {AUTO_SAMPLE_PER_CLASS_DEFAULT}），越界 → EX12"),
    )
    seed: Optional[int] = Field(None, description="numpy RandomState 随机种子（同 seed 可复现）")


class AutoSamplePoint(BaseModel):
    """抽样点：像元中心反算的 WGS84 经纬度 + 参考类别（由栅格三类编码给出）。"""

    lng: float = Field(..., description="WGS84 经度")
    lat: float = Field(..., description="WGS84 纬度")
    ref_class: str = Field(..., description="参考类别：增强 / 退化 / 稳定")


class AutoSampleResponse(BaseModel):
    """半自动抽样响应（REQ-09 自动化）：不写 sample 表，仅返回点列表。"""

    run_id: str = Field(..., description="运行标识（REQ-14）")
    samples: List[AutoSamplePoint] = Field(default_factory=list, description="分层抽样点")
    per_class_actual: Dict[str, int] = Field(
        default_factory=dict, description="三类实际抽样数（不足时如实记实际值）"
    )


@router.post(
    "/sample",
    response_model=AccuracySampleResponse,
    responses={400: {"model": ErrorResponse, "description": "EX10 任一类样本 < 30 点"}},
    summary="样本提交与精度评价",
)
def submit_samples(payload: AccuracySampleRequest) -> AccuracySampleResponse:
    """样本提交与精度评价（REQ-09）。

    累计每类样本数 ≥ MIN_SAMPLES_PER_CLASS，否则 EX10；Kappa < KAPPA_TEACHING_MIN
    时标注「教学级，非工程级」。
    """
    samples = [s.model_dump() for s in payload.samples]
    result = sample_service.submit(payload.run_id, samples)
    return AccuracySampleResponse(**result)


@router.post(
    "/auto_sample",
    response_model=AutoSampleResponse,
    responses={400: {"model": ErrorResponse, "description": "EX12 per_class 非法"},
               404: {"model": ErrorResponse, "description": "EX2 run 不存在"},
               500: {"model": ErrorResponse, "description": "SYS-01 结果栅格缺失/不可读"}},
    summary="样本分层随机采样（REQ-09 自动化，半自动流程不写库）",
)
def auto_sample(payload: AutoSampleRequest) -> AutoSampleResponse:
    """POST /accuracy/auto_sample（REQ-09 自动化）。

    按 run 的 ``params_snapshot.method`` 选 change.tif / change_reg.tif，对三类
    编码分层用 ``RandomState(seed)`` 抽样并反算经纬度；**不写 sample 表**，
    前端把样本点填入本地列表、用户修正 ref_class 后再走既有 /accuracy/sample。
    """
    result = sample_service.auto_sample(
        payload.run_id, payload.per_class, payload.seed
    )
    return AutoSampleResponse(**result)
