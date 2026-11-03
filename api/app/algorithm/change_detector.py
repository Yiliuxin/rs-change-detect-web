# -*- coding: utf-8 -*-
"""ΔNDVI 差分与三类变化判别（HLD 模块3 功能二，REQ-07 / REQ-08 / REQ-11）。

纯函数：三类编码由本模块统一定义（HLD 锁定，禁止改写）：
    0 = nodata/无效；1 = 增强；2 = 退化；3 = 稳定。
分类口径：Δ > +T → 1；Δ < −T → 2；|Δ| ≤ T → 3；无效像元 → 0。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from app.algorithm.geotransform import pixel_area_m2 as _ds_pixel_area_m2

# 三类编码（HLD 锁定值，渲染 / 掩膜 / 报告统一引用此处的唯一来源）
CHANGE_NODATA: int = 0
CHANGE_ENHANCED: int = 1
CHANGE_DEGRADED: int = 2
CHANGE_STABLE: int = 3
CHANGE_CODES: Tuple[int, int, int] = (CHANGE_ENHANCED, CHANGE_DEGRADED, CHANGE_STABLE)
CHANGE_LABELS_ZH: Dict[int, str] = {1: "增强", 2: "退化", 3: "稳定", 0: "无效"}
CHANGE_LABELS_EN: Dict[int, str] = {
    1: "ENHANCED", 2: "DEGRADED", 3: "STABLE", 0: "NODATA",
}

# 平方米 → 公顷（物理单位换算，非业务阈值）
M2_PER_HA: float = 10000.0

__all__ = [
    "CHANGE_NODATA", "CHANGE_ENHANCED", "CHANGE_DEGRADED", "CHANGE_STABLE",
    "CHANGE_CODES", "CHANGE_LABELS_ZH", "CHANGE_LABELS_EN",
    "delta_ndvi", "classify_change", "change_pixel_ratio",
    "pixel_area_from_profile", "change_stats",
]


def _as_f32(arr) -> np.ndarray:
    return np.asarray(arr, dtype=np.float32)


def delta_ndvi(ndvi_t2, ndvi_t1) -> np.ndarray:
    """ΔNDVI = NDVI(t2) − NDVI(t1)；float32，NaN 自然传播（REQ-07）。"""
    return _as_f32(ndvi_t2) - _as_f32(ndvi_t1)


def classify_change(dndvi, threshold: float, valid_mask: Optional[np.ndarray] = None) -> np.ndarray:
    """按对称阈值 T 分三类，返回 uint8（0 为 nodata）。

    参数：
        dndvi      ：ΔNDVI 数组（float32，无效处为 NaN）
        threshold  ：非负阈值 T；Otsu 自适应阈值或回退固定阈值
        valid_mask ：可选有效掩膜（True 有效）；None 时用有限值判定
    """
    d = _as_f32(dndvi)
    t = float(threshold)
    if not np.isfinite(t) or t < 0.0:
        raise ValueError(f"threshold 需为有限非负数，实际 {threshold!r}")

    valid = np.isfinite(d)
    if valid_mask is not None:
        vm = np.asarray(valid_mask, dtype=bool)
        if vm.shape != d.shape:
            raise ValueError(f"valid_mask 形状 {vm.shape} 与 ΔNDVI {d.shape} 不一致")
        valid &= vm

    out = np.zeros(d.shape, dtype=np.uint8)
    out[valid] = CHANGE_STABLE                       # |Δ| ≤ T 稳定
    out[valid & (d > t)] = CHANGE_ENHANCED           # Δ > +T 增强
    out[valid & (d < -t)] = CHANGE_DEGRADED          # Δ < −T 退化
    return out


def change_pixel_ratio(change, valid_mask: Optional[np.ndarray] = None) -> float:
    """变化像元占比 = (增强 + 退化) / 有效像元数；无有效像元返回 0.0。

    有效像元定义为分类输出中编码非 0 的像元（0 恒为 nodata），再与可选
    ``valid_mask`` 取交集，避免把 NaN 差分产生的空像元计入分母。
    """
    arr = np.asarray(change)
    valid = arr != CHANGE_NODATA
    if valid_mask is not None:
        vm = np.asarray(valid_mask, dtype=bool)
        if vm.shape != arr.shape:
            raise ValueError(f"valid_mask 形状 {vm.shape} 与变化图 {arr.shape} 不一致")
        valid &= vm

    denom = int(valid.sum())
    if denom == 0:
        return 0.0
    changed = int((valid & ((arr == CHANGE_ENHANCED) | (arr == CHANGE_DEGRADED))).sum())
    return float(changed) / float(denom)


def pixel_area_from_profile(profile: dict) -> float:
    """由 profile 的 transform（或 res）换算单像元面积（平方米）。

    与 ``geotransform.pixel_area_m2(ds)`` 口径一致；profile 来自
    ``cog_reader.read_aoi`` 时 transform 已按窗口重算。
    """
    transform = profile.get("transform")
    if transform is not None:
        if hasattr(transform, "a"):
            a, e = float(transform.a), float(transform.e)
        else:
            seq = tuple(float(v) for v in transform[:6])
            a, e = seq[0], seq[4]
        return abs(a * e)

    res = profile.get("res")
    if res is not None:
        return abs(float(res[0]) * float(res[1]))
    raise ValueError("profile 缺少 transform / res，无法换算单像元面积（REQ-11）")


def _resolve_pixel_area(profile: Optional[dict], area_m2: Optional[float], ds) -> float:
    if area_m2 is not None:
        return float(area_m2)
    if ds is not None:
        return float(_ds_pixel_area_m2(ds))
    if profile is not None:
        return pixel_area_from_profile(profile)
    raise ValueError("需提供 profile / pixel_area_m2 / ds 之一以换算面积")


def change_stats(
    change,
    profile: Optional[dict] = None,
    pixel_area_m2_value: Optional[float] = None,
    ds=None,
    valid_mask: Optional[np.ndarray] = None,
    run_id: str = "",
    params_snapshot: Optional[dict] = None,
) -> Dict[str, object]:
    """三类统计（REQ-08 / REQ-11）：每类 ``pixel_count`` / ``area_ha``。

    像元面积优先取显式 ``pixel_area_m2_value``，其次 ``geotransform.pixel_area_m2(ds)``，
    最后用 profile.transform 或 profile.res。返回 dict 可直接喂给 service 的 StatItem。
    """
    arr = np.asarray(change)
    area = _resolve_pixel_area(profile, pixel_area_m2_value, ds)

    stats: List[Dict[str, object]] = [
        {
            "change_class": int(code),
            "pixel_count": int((arr == code).sum()),
            "area_ha": float((arr == code).sum()) * area / M2_PER_HA,
        }
        for code in CHANGE_CODES
    ]
    valid_count = int(sum(item["pixel_count"] for item in stats))
    return {
        "stats": stats,
        "pixel_area_m2": area,
        "valid_count": valid_count,
        "invalid_count": int((arr == CHANGE_NODATA).sum()),
        "total": int(arr.size),
        "change_pixel_ratio": change_pixel_ratio(arr, valid_mask=valid_mask),
        "run_id": run_id,
        "params_snapshot": dict(params_snapshot or {}),
    }
