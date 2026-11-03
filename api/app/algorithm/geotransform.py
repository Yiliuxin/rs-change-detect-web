# -*- coding: utf-8 -*-
"""经纬度 ↔ 像素换算与 AOI 跨度校验（HLD 模块1 功能二，REQ-03 / REQ-01）。

纯函数：不连数据库、不依赖路由。阈值一律取自 ``app.config.constants``。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence, Tuple

import rasterio
from rasterio.crs import CRS
from rasterio.windows import Window, from_bounds
from rasterio.warp import transform as warp_transform

from app.config.constants import AOI_MAX_SPAN_DEG

WGS84 = CRS.from_epsg(4326)


class AoiTooLargeError(Exception):
    """AOI 跨度超过 AOI_MAX_SPAN_DEG（0.25°）→ EX1 前端拦截。"""

    code = "EX1"


@dataclass(frozen=True)
class BBox:
    """WGS84 经纬度外接矩形（minx, miny, maxx, maxy）。"""

    minx: float
    miny: float
    maxx: float
    maxy: float

    @classmethod
    def from_sequence(cls, seq: Sequence[float]) -> "BBox":
        values = [float(v) for v in seq]
        if len(values) != 4:
            raise ValueError(f"AOI 需为 [minx, miny, maxx, maxy]，实际 {len(values)} 个值")
        minx, miny, maxx, maxy = values
        if maxx <= minx or maxy <= miny:
            raise ValueError(f"AOI 非法（max 必须大于 min）：{values}")
        return cls(minx, miny, maxx, maxy)

    @property
    def span_x(self) -> float:
        return self.maxx - self.minx

    @property
    def span_y(self) -> float:
        return self.maxy - self.miny

    @property
    def area_deg2(self) -> float:
        return self.span_x * self.span_y

    def as_tuple(self) -> Tuple[float, float, float, float]:
        return (self.minx, self.miny, self.maxx, self.maxy)


def validate_aoi_span(bbox: BBox) -> BBox:
    """校验 AOI 跨度 ≤ AOI_MAX_SPAN_DEG；超限抛 :class:`AoiTooLargeError`（EX1）。"""
    if bbox.span_x > AOI_MAX_SPAN_DEG or bbox.span_y > AOI_MAX_SPAN_DEG:
        raise AoiTooLargeError(
            f"AOI 跨度 {bbox.span_x:.4f}°×{bbox.span_y:.4f}° 超过上限 {AOI_MAX_SPAN_DEG}°（EX1）"
        )
    return bbox


def bbox_to_dataset_bounds(ds: rasterio.DatasetReader, bbox: BBox) -> Tuple[float, float, float, float]:
    """把 WGS84 的 AOI 外接矩形投影到数据集 CRS，返回该 CRS 下的 (minx, miny, maxx, maxy)。"""
    xs, ys = warp_transform(
        WGS84, ds.crs, [bbox.minx, bbox.maxx], [bbox.miny, bbox.maxy]
    )
    return min(xs), min(ys), max(xs), max(ys)


def aoi_window(ds: rasterio.DatasetReader, bbox: BBox) -> Window:
    """把 AOI 外接矩形换算为像素窗口（REQ-03），并顺带执行跨度校验。"""
    validate_aoi_span(bbox)
    bounds = bbox_to_dataset_bounds(ds, bbox)
    return from_bounds(*bounds, transform=ds.transform).round_offsets().round_lengths()


def window_transform(ds: rasterio.DatasetReader, window: Window):
    """返回窗口对应的仿射变换。"""
    return rasterio.windows.transform(window, ds.transform)


def lonlat_to_pixel(ds: rasterio.DatasetReader, lon: float, lat: float) -> Tuple[int, int]:
    """经纬度 → (row, col) 像元坐标。"""
    xs, ys = warp_transform(WGS84, ds.crs, [lon], [lat])
    row, col = ds.index(xs[0], ys[0])
    return int(row), int(col)


def pixel_to_lonlat(ds: rasterio.DatasetReader, row: int, col: int) -> Tuple[float, float]:
    """(row, col) 像元坐标 → 经纬度。"""
    x, y = ds.xy(row, col)
    lons, lats = warp_transform(ds.crs, WGS84, [x], [y])
    return float(lons[0]), float(lats[0])


def pixel_area_m2(ds: rasterio.DatasetReader) -> float:
    """单像元面积（平方米），用于面积换算（REQ-11）。"""
    res_x, res_y = ds.res
    return abs(res_x * res_y)


def iter_corners(bbox: BBox) -> Iterable[Tuple[float, float]]:
    """返回 AOI 四角（WGS84）。"""
    return (
        (bbox.minx, bbox.miny),
        (bbox.minx, bbox.maxy),
        (bbox.maxx, bbox.maxy),
        (bbox.maxx, bbox.miny),
    )
