# -*- coding: utf-8 -*-
"""路由：POST /api/v1/sample/from_lnglat（REQ-09，样本图上点选）。

薄封装：Pydantic 契约 → ``sample_service.from_lnglat`` → 统一异常封装。
校验（run 存在 / 参考类别）、像元定位与入库均与既有 /accuracy/sample 路径
共用 service + repository 构件；既有 5 接口的请求/响应语义不受影响。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.api.deps import ensure_run_owner

from app.api.v1.schemas import ErrorResponse
from app.service import sample_service

router = APIRouter(prefix="/sample", tags=["Samples"])


class SampleFromLngLatRequest(BaseModel):
    """WGS84 经纬度点选请求。"""

    run_id: str = Field(..., description="运行标识（REQ-14）")
    lng: float = Field(..., description="WGS84 经度")
    lat: float = Field(..., description="WGS84 纬度")
    ref_class: str = Field(..., description="参考类别：增强 / 退化 / 稳定")
    label: Optional[str] = Field(None, description="样本备注，缺省取 ref_class")


class SampleFromLngLatResponse(BaseModel):
    """反算后的像元坐标与入库主键。"""

    sample_id: str = Field(..., description="sample 表主键 UUID")
    px_x: int = Field(..., description="像元列坐标")
    px_y: int = Field(..., description="像元行坐标")
    ref_class: str = Field(..., description="入库的参考类别")


@router.post(
    "/from_lnglat",
    response_model=SampleFromLngLatResponse,
    responses={400: {"model": ErrorResponse, "description": "EX12 AOI 外 / EX10 非法 ref_class"},
               404: {"model": ErrorResponse, "description": "EX2 run 不存在"},
               500: {"model": ErrorResponse, "description": "SYS-01 裁切栅格读取失败"}},
    summary="样本图上点选（WGS84 经纬度 → 像元坐标 → 入库）",
)
def create_sample_from_lnglat(request: Request, payload: SampleFromLngLatRequest) -> SampleFromLngLatResponse:
    """REQ-09：由该 run 裁切栅格 geotransform 反算 (px_x, px_y)，AOI 外 → 400 EX12；非本人 → AUTH-03。"""
    ensure_run_owner(request, payload.run_id)
    result = sample_service.from_lnglat(
        payload.run_id, payload.lng, payload.lat,
        payload.ref_class, payload.label,
    )
    return SampleFromLngLatResponse(**result)
