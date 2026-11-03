# -*- coding: utf-8 -*-
"""路由：POST /api/v1/report/export（HLD 2.2.3.5，REQ-12/14/15）。

骨架阶段：只定义路径、HTTP 方法、Pydantic 请求/响应契约与占位返回值。
后续由 ReportService 汇总参数快照、专题图、GeoTIFF 与精度指标，产出三格式报告。
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.v1.schemas import ErrorResponse, ExportFile
from app.config.constants import ATTRIBUTION_S2, EXPORT_FORMATS

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
    responses={400: {"model": ErrorResponse, "description": "EX6 导出格式不支持"}},
    summary="报告导出",
)
def export_report(payload: ReportExportRequest) -> ReportExportResponse:
    """报告导出（REQ-12/14/15）。

    TODO(HLD 2.2.3.5)：
      1) 校验 formats ⊆ EXPORT_FORMATS，否则 EX6；
      2) GeoTiffWriter / PdfBuilder 产出 GeoTIFF + PNG + PDF；
      3) AttributionProvider 强制嵌入 Copernicus / USGS 署名（REQ-15）。
    """
    _ = (payload, ATTRIBUTION_S2, EXPORT_FORMATS)
    return ReportExportResponse(run_id="")
