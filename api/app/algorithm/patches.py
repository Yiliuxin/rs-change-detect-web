# -*- coding: utf-8 -*-
"""变化图斑统计（A2）：增强 / 退化两类各自做 4 邻域连通域标注。

纯函数：不连数据库、不读写文件、不修改入参；只借用 ``rasterio.warp.transform``
做质心重投影（无 IO）。

连通性口径：``skimage.measure.label(..., connectivity=PATCH_CONNECTIVITY)`` 在 2D
下取 1 即 4 邻域，与 ``writers.write_geojson`` 中 ``rasterio.features.shapes``
默认 4 连通一致，保证斑块数与 GeoJSON feature 数同口径。

超限保护：像元数 > ``PATCH_MAX_PIXELS`` 时直接返回 ``skipped=True`` + 原因，
不静默、不分配整幅标签数组。Top-N 只对面积最大的前 N 个斑块求质心。
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

import numpy as np
from rasterio.transform import Affine
from rasterio.warp import transform as warp_transform
from skimage.measure import label as label_regions

from app.algorithm.change_detector import (
    CHANGE_DEGRADED, CHANGE_ENHANCED, CHANGE_LABELS_ZH, M2_PER_HA,
    pixel_area_from_profile,
)
from app.config.constants import (
    PATCH_CONNECTIVITY, PATCH_MAX_PIXELS, PATCH_SKIP_REASON, PATCH_TOP_N,
)

__all__ = ["patch_stats"]

# 平方米 → 平方公里（物理单位换算，非业务阈值）
M2_PER_KM2: float = 1_000_000.0


def _affine(transform) -> Optional[Affine]:
    """把 profile.transform（Affine / 6 元组）统一成 Affine；缺失返回 None。"""
    if transform is None:
        return None
    try:
        if isinstance(transform, Affine):
            return transform
        seq = [float(v) for v in list(transform)[:6]]
        if len(seq) != 6:
            return None
        return Affine(*seq)
    except (TypeError, ValueError):
        return None


def _centroid_lonlat(lab: np.ndarray, label_id: int,
                     transform: Optional[Affine], crs) -> Tuple[Optional[float], Optional[float]]:
    """单个斑块的质心（行列均值 → 栅格 CRS → EPSG:4326）。"""
    if transform is None or crs is None:
        return (None, None)
    coords = np.argwhere(lab == label_id)
    if coords.size == 0:
        return (None, None)
    row = float(coords[:, 0].mean())
    col = float(coords[:, 1].mean())
    x, y = transform @ (col + 0.5, row + 0.5)
    try:
        lons, lats = warp_transform(crs, "EPSG:4326", [float(x)], [float(y)])
        lng, lat = float(lons[0]), float(lats[0])
        if not (math.isfinite(lng) and math.isfinite(lat)):
            return (None, None)
        return (lng, lat)
    except Exception:  # noqa: BLE001 - CRS 不可投影时如实留空，不抛异常
        return (None, None)


def _empty(skipped: bool, reason: str, area: float, total_pixels: int,
           top_n: int) -> Dict[str, object]:
    return {
        "skipped": bool(skipped),
        "reason": str(reason),
        "connectivity": int(PATCH_CONNECTIVITY),
        "max_pixels": int(PATCH_MAX_PIXELS),
        "top_n": int(top_n),
        "pixel_area_m2": float(area),
        "total_pixels": int(total_pixels),
        "total_patches": 0,
        "total_pixel_count": 0,
        "total_area_ha": 0.0,
        "mean_area_ha": 0.0,
        "median_area_ha": 0.0,
        "max_area_ha": 0.0,
        "patch_density_per_km2": 0.0,
        "by_class": {
            str(CHANGE_ENHANCED): 0,
            str(CHANGE_DEGRADED): 0,
        },
        "top": [],
    }


def patch_stats(change, profile: Optional[dict] = None,
                pixel_area_m2: Optional[float] = None,
                top_n: int = PATCH_TOP_N,
                max_pixels: int = PATCH_MAX_PIXELS) -> Dict[str, object]:
    """统计增强 / 退化图斑：数量、面积分布、斑块密度与面积 Top-N（含质心经纬度）。

    参数：
        change         ：三类变化图（0 无效 / 1 增强 / 2 退化 / 3 稳定）
        profile        ：``cog_reader.read_aoi`` 的 profile（transform / crs，用于质心）
        pixel_area_m2  ：单像元面积；缺省时由 profile.transform 换算
        top_n          ：只对前 N 大斑块求质心（默认取常量 PATCH_TOP_N）
        max_pixels     ：像元素上限（默认取常量 PATCH_MAX_PIXELS）
    """
    arr = np.asarray(change)
    if arr.ndim != 2:
        raise ValueError(f"变化图需为二维数组，实际 {arr.shape}")
    n = max(0, int(top_n))
    src = dict(profile or {})
    if pixel_area_m2 is not None:
        area = float(pixel_area_m2)
    else:
        try:
            area = float(pixel_area_from_profile(src))
        except (ValueError, TypeError):
            area = 0.0
    if not math.isfinite(area) or area <= 0.0:
        area = 0.0

    total_pixels = int(arr.size)
    if total_pixels > int(max_pixels):
        return _empty(True, PATCH_SKIP_REASON, area, total_pixels, n)

    transform = _affine(src.get("transform"))
    crs = src.get("crs")
    # (像元数, 三类编码, 标签 id, 该类标签数组)；只保留两份标签数组
    records: List[Tuple[int, int, int, np.ndarray]] = []
    by_class = {str(CHANGE_ENHANCED): 0, str(CHANGE_DEGRADED): 0}
    for code in (CHANGE_ENHANCED, CHANGE_DEGRADED):
        mask = arr == code
        if not bool(mask.any()):
            continue
        lab = label_regions(mask, connectivity=int(PATCH_CONNECTIVITY))
        sizes = np.bincount(lab.ravel())
        ids = np.flatnonzero(sizes[1:]) + 1
        by_class[str(code)] = int(ids.size)
        for label_id in ids:
            records.append((int(sizes[label_id]), int(code), int(label_id), lab))

    if not records:
        return _empty(False, "", area, total_pixels, n)

    sizes_all = [r[0] for r in records]
    total_count = int(sum(sizes_all))
    areas_ha = [s * area / M2_PER_HA for s in sizes_all]
    landscape_km2 = total_pixels * area / M2_PER_KM2
    density = float(len(records) / landscape_km2) if landscape_km2 > 0 else 0.0

    ordered = sorted(records, key=lambda r: (-r[0], r[1], r[2]))[:n]
    top: List[Dict[str, object]] = []
    for rank, (size, code, label_id, lab) in enumerate(ordered, start=1):
        lng, lat = _centroid_lonlat(lab, label_id, transform, crs)
        top.append({
            "rank": rank,
            "change_class": int(code),
            "class_zh": CHANGE_LABELS_ZH[int(code)],
            "pixel_count": int(size),
            "area_ha": float(size) * area / M2_PER_HA,
            "centroid_lng": lng,
            "centroid_lat": lat,
        })

    return {
        "skipped": False,
        "reason": "",
        "connectivity": int(PATCH_CONNECTIVITY),
        "max_pixels": int(PATCH_MAX_PIXELS),
        "top_n": int(n),
        "pixel_area_m2": float(area),
        "total_pixels": total_pixels,
        "total_patches": int(len(records)),
        "total_pixel_count": total_count,
        "total_area_ha": float(total_count) * area / M2_PER_HA,
        "mean_area_ha": float(np.mean(areas_ha)),
        "median_area_ha": float(np.median(areas_ha)),
        "max_area_ha": float(np.max(areas_ha)),
        "patch_density_per_km2": density,
        "by_class": by_class,
        "top": top,
    }
