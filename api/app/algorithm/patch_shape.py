# -*- coding: utf-8 -*-
"""批次 B2 图斑形态指标（纯函数，仅 numpy 标准库）。

对每个矢量图斑（WGS84 外环）计算：
  * ``perimeter_m``     周长 P（米）；
  * ``compactness``     紧凑度 4πA/P²（圆 ≈ 1，越狭长/破碎越小）；
  * ``orientation_deg`` 主走向：最小面积外接矩形（MBR）长轴方位角，0–180°，
                        以正北为 0°、顺时针增大，90° 为东西向（长轴无方向性）。

**度→米近似**：按外环平均纬度 lat 作等距近似，
Δx_m = Δlng · 111320 · cos(lat)、Δy_m = Δlat · 111320。
AOI 跨度 ≤ 0.25°、纬度变化极小时误差可忽略；这是**展示口径的近似**，
不用于面积入库或精度评价（面积口径见 ``writers._crs_ring_area_ha`` 与
``patches.patch_stats``）。周长只用外环，内环（洞）不计入；面积 A 直接取
该图斑 ``area_ha × 10000`` m²，与 GeoJSON 属性同源，不自算第二套面积。
"""
from __future__ import annotations

import math
from typing import Dict, List, Tuple

import numpy as np

from app.config.constants import PATCH_SHAPE_DEG_TO_M

DEG_TO_M: float = PATCH_SHAPE_DEG_TO_M

__all__ = ["DEG_TO_M", "shape_metrics", "shape_metrics_from_labels"]


def _empty() -> Dict[str, float]:
    return {"perimeter_m": 0.0, "compactness": 0.0, "orientation_deg": 0.0}


def _to_meters(ring) -> List[Tuple[float, float]]:
    """WGS84 环 → 局部平面米坐标（相对首点平移，形状/角度不变）。"""
    pts = [(float(p[0]), float(p[1])) for p in ring]
    if len(pts) < 2:
        return []
    lat = sum(p[1] for p in pts) / len(pts)
    # 近似：经度按平均纬度收缩，纬度 1° ≈ 111320 m；忽略椭球与逐点纬度变化。
    kx = DEG_TO_M * math.cos(math.radians(lat))
    ky = DEG_TO_M
    x0, y0 = pts[0]
    return [((x - x0) * kx, (y - y0) * ky) for x, y in pts]


def _perimeter_m(points_m) -> float:
    if len(points_m) < 2:
        return 0.0
    return float(sum(math.hypot(b[0] - a[0], b[1] - a[1])
                     for a, b in zip(points_m, points_m[1:])))


def _convex_hull(points) -> List[Tuple[float, float]]:
    """Andrew monotone chain 凸包（去掉共线点，减少 MBR 候选边）。"""
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return ((a[0] - o[0]) * (b[1] - o[1])
                - (a[1] - o[1]) * (b[0] - o[0]))

    lower: List[Tuple[float, float]] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: List[Tuple[float, float]] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _azimuth(dx: float, dy: float) -> float:
    """长轴方向向量 → 方位角 0–180°（正北 0°，顺时针，东西向 90°）。"""
    return float(math.degrees(math.atan2(dx, dy)) % 180.0)


def _mbr_orientation(points_m) -> float:
    """旋转卡壳：沿凸包每条边建立外接矩形，取面积最小者的长轴方位角。"""
    hull = _convex_hull(points_m)
    if not hull:
        return 0.0
    if len(hull) == 2:
        return _azimuth(hull[1][0] - hull[0][0], hull[1][1] - hull[0][1])
    best = None
    for i in range(len(hull)):
        x1, y1 = hull[i]
        x2, y2 = hull[(i + 1) % len(hull)]
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy)
        if length <= 0:
            continue
        ux, uy = dx / length, dy / length          # 旋转后 x 轴
        us = [p[0] * ux + p[1] * uy for p in hull]
        vs = [-p[0] * uy + p[1] * ux for p in hull]  # 旋转后 y 轴
        w, h = max(us) - min(us), max(vs) - min(vs)
        key = (w * h, 2.0 * (w + h))              # 面积优先，周长兜底
        if best is None or key < best[0]:
            lx, ly = (ux, uy) if w >= h else (-uy, ux)
            best = (key, _azimuth(lx, ly))
    return float(best[1]) if best else 0.0


def shape_metrics(ring_lonlat, area_ha) -> Dict[str, float]:
    """由 WGS84 外环 + area_ha 计算三项形态指标（可直接写进 GeoJSON properties）。"""
    points_m = _to_meters(ring_lonlat)
    p = _perimeter_m(points_m)
    area_m2 = max(0.0, float(area_ha)) * 10000.0   # ha → m²
    compactness = (4.0 * math.pi * area_m2 / (p * p)) if p > 0 else 0.0
    return {
        "perimeter_m": round(p, 2),
        "compactness": round(compactness, 4),
        "orientation_deg": round(_mbr_orientation(points_m), 1),
    }


def shape_metrics_from_labels(lab, label_id, transform, crs, area_ha) -> Dict[str, float]:
    """由 skimage 连通域标签数组，把单个图斑轮廓重投影到 WGS84 后算形态指标。

    与 ``write_geojson`` 同用 ``rasterio.features.shapes``（默认 4 连通），
    保证 Top10 表与 GeoJSON 图斑三列数字同源；缺 transform / 轮廓时返回全 0。
    """
    mask = np.asarray(lab) == int(label_id)
    if transform is None or not bool(mask.any()):
        return _empty()
    try:
        from rasterio.features import shapes as feature_shapes
        from rasterio.warp import transform as warp_transform

        values = mask.astype(np.uint8)
        for geom, value in feature_shapes(values, mask=mask, transform=transform):
            if int(value) != 1:
                continue
            ring = geom["coordinates"][0]
            xs = [float(p[0]) for p in ring]
            ys = [float(p[1]) for p in ring]
            txs, tys = warp_transform(crs or "EPSG:4326", "EPSG:4326", xs, ys)
            ring_lonlat = [[float(x), float(y)] for x, y in zip(txs, tys)]
            return shape_metrics(ring_lonlat, area_ha)
    except Exception:  # noqa: BLE001 - 缺 CRS / 投影失败时如实返回 0，不阻断统计
        return _empty()
    return _empty()
