# -*- coding: utf-8 -*-
"""路由：POST /api/v1/report/export（HLD 2.2.3.5，REQ-12/14/15）。

薄封装：Pydantic 校验 + 调 report_service + 统一异常封装。
格式白名单（EX6）/ 三件套落盘上传 / commit / 署名全在 service 层。
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.v1.schemas import ErrorResponse, ExportFile
from app.config.constants import ATTRIBUTION_S2, EXPORT_FORMATS
from app.service import report_service

router = APIRouter(prefix="/report", tags=["Report"])


class ReportExportRequest(BaseModel):
    """报告导出请求（REQ-12）。"""

    run_id: str = Field(..., description="运行标识（REQ-14）")
    formats: List[str] = Field(default_factory=list, description=f"子集：{' | '.join(EXPORT_FORMATS)}")


class ReportExportResponse(BaseModel):
    """报告导出响应（REQ-12/14/15）。"""

    files: List[ExportFile] = Field(default_factory=list, description="下载链接清单")
    run_id: str = Field(..., description="运行标识（REQ-14）")
    commit: Optional[str] = Field(None, description="代码 commit（可复现三要素）")
    attribution: Optional[str] = Field(None, description="合规署名（REQ-15）")


@router.post(
    "/export",
    response_model=ReportExportResponse,
    responses={400: {"model": ErrorResponse, "description": "EX6 导出格式不支持"},
               409: {"model": ErrorResponse, "description": "EX7 并发超限"},
               500: {"model": ErrorResponse, "description": "SYS-*"}},
    summary="报告导出",
)
def export_report(payload: ReportExportRequest) -> ReportExportResponse:
    """报告导出（REQ-12/14/15）。

    formats ⊆ EXPORT_FORMATS，否则 EX6；产出 GeoTIFF/PNG/PDF 三件套并强制署名。
    """
    result = report_service.export(payload.run_id, payload.formats)
    return ReportExportResponse(**result)
