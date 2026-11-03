# -*- coding: utf-8 -*-
"""API v1 共享 Pydantic 契约（VO）。

统一前后端字段口径，避免字段漂移（HLD 2.2.2.1 AoiVO / SceneVO）。
骨架阶段仅定义契约，不承载业务逻辑。
"""
from __future__ import annotations

from datetime import date
from typing import List, Optional

from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    """统一错误封装：{code, message, run_id}（HLD 2.2.3 / 第 5 章）。"""

    code: str = Field(..., description="EX* / SYS-* 错误码")
    message: str = Field(..., description="面向非专业用户的中文提示")
    run_id: Optional[str] = Field(None, description="运行标识（REQ-14）")


class SceneVO(BaseModel):
    """预置影像对场景视图对象（REQ-02）。"""

    scene_id: str = Field(..., description="场景主键 UUID")
    sensor: str = Field(..., description="传感器：S2 / L8 / L9")
    acq_date: date = Field(..., description="成像时相")
    cloud_cover: Optional[float] = Field(None, description="云量（%）")
    cog_path: str = Field(..., description="COG 对象存储路径（MinIO）")
    proc_baseline: Optional[str] = Field(None, description="处理基线版次")


class ClipWindow(BaseModel):
    """AOI 裁切窗口（REQ-03）。"""

    row_off: int = 0
    col_off: int = 0
    width: int = 0
    height: int = 0
    crs: Optional[str] = None
    res: Optional[float] = None


class StatItem(BaseModel):
    """三类变化面积统计项（REQ-08 / REQ-11）。"""

    change_class: int = Field(..., description="1 增强 / 2 退化 / 3 稳定")
    pixel_count: int = 0
    area_ha: float = 0.0


class SampleItem(BaseModel):
    """用户勾选的验证样本点（REQ-09）。"""

    x: float = Field(..., description="像元列坐标")
    y: float = Field(..., description="像元行坐标")
    ref_class: str = Field(..., description="参考类别：增强 / 退化 / 稳定")


class ExportFile(BaseModel):
    """报告产物下载项（REQ-12）。"""

    format: str = Field(..., description="geotiff / png / pdf")
    url: str = ""
    size: Optional[int] = Field(None, description="字节数")
