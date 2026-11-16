# -*- coding: utf-8 -*-
"""ΔNDVI 差分与三类变化判别（HLD 模块3 功能二，REQ-07 / REQ-08 / REQ-11）。

纯函数：三类编码由本模块统一定义（HLD 锁定，禁止改写）：
    0 = nodata/无效；1 = 增强；2 = 退化；3 = 稳定。
分类口径：Δ > +T → 1；Δ < −T → 2；|Δ| ≤ T → 3；无效像元 → 0。

批次 B1 追加「置信度过渡带」：| |Δ| − T | ≤ 0.25·T 的像元判定证据不足。
过渡带是**置信度提示，不是第四类变化**：它单独编码为 CHANGE_TRANSITION，
只用于专题图渲染与 stats 单列，不进 CHANGE_CODES、不改写三类分类图
（GeoTIFF / 图斑统计 / 精度评价仍严格只有 1/2/3）。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from app.algorithm.geotransform import pixel_area_m2 as _ds_pixel_area_m2
from app.config.constants import (
    TRANSITION_BAND_CODE, TRANSITION_BAND_LABEL_EN, TRANSITION_BAND_LABEL_ZH,
    TRANSITION_BAND_NOTE, TRANSITION_BAND_RATIO,
)

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
# 批次 B1：过渡带是「证据不足」的旁路标记，不是第四类变化，故不进 CHANGE_CODES；
# 也不写进 CHANGE_LABELS_ZH/EN（精度评价的类别字典严格只有三类）。
CHANGE_TRANSITION: int = TRANSITION_BAND_CODE

# 平方米 → 公顷（物理单位换算，非业务阈值）
M2_PER_HA: float = 10000.0

__all__ = [
    "CHANGE_NODATA", "CHANGE_ENHANCED", "CHANGE_DEGRADED", "CHANGE_STABLE",
    "CHANGE_CODES", "CHANGE_LABELS_ZH", "CHANGE_LABELS_EN", "CHANGE_TRANSITION",
    "delta_ndvi", "classify_change", "change_pixel_ratio",
    "pixel_area_from_profile", "change_stats",
    "transition_band_edges", "transition_band_mask", "transition_band_stats",
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


def transition_band_edges(threshold: float,
                          ratio: float = TRANSITION_BAND_RATIO) -> Tuple[float, float]:
    """过渡带上下界：((1−r)·T, (1+r)·T)，T 取 |T|；r 需落在 [0, 1)。"""
    t = abs(float(threshold))
    r = float(ratio)
    if not np.isfinite(t) or t < 0.0 or not (0.0 <= r < 1.0):
        raise ValueError(f"阈值/带宽非法：T={threshold!r}, ratio={ratio!r}")
    return (1.0 - r) * t, (1.0 + r) * t


def transition_band_mask(delta, threshold: float,
                         valid_mask: Optional[np.ndarray] = None,
                         ratio: float = TRANSITION_BAND_RATIO) -> np.ndarray:
    """置信度过渡带掩膜（批次 B1）：| |Δ| − T | ≤ r·T 的有效像元为 True。

    口径取常量 ``TRANSITION_BAND_RATIO``；Δ 用与分类阈值同源的差分/残差
    （差值法 = index_t2 − index_t1，回归法 = 残差 r）。这些像元「证据不足」，
    **不是第四类变化**：本函数只产出旁路标记，不改写三类分类图。
    """
    low, high = transition_band_edges(threshold, ratio)
    a = np.abs(_as_f32(delta))
    valid = np.isfinite(a)
    if valid_mask is not None:
        vm = np.asarray(valid_mask, dtype=bool)
        if vm.shape != a.shape:
            raise ValueError(f"valid_mask 形状 {vm.shape} 与 Δ 数组 {a.shape} 不一致")
        valid &= vm
    return valid & (a >= low) & (a <= high)


def transition_band_stats(mask, valid_count: int, pixel_area_m2: float,
                          threshold: float,
                          ratio: float = TRANSITION_BAND_RATIO) -> Dict[str, object]:
    """过渡带像元数 / 占比 / 面积与带宽口径（stats 增补字段，单列不并入三类）。

    占比分母为三类有效像元总数 ``valid_count``（过渡带是其中的子集标记）。
    """
    low, high = transition_band_edges(threshold, ratio)
    n = int(np.count_nonzero(np.asarray(mask, dtype=bool)))
    denom = int(valid_count)
    return {
        "code": CHANGE_TRANSITION,
        "label": TRANSITION_BAND_LABEL_ZH,
        "label_en": TRANSITION_BAND_LABEL_EN,
        "pixel_count": n,
        "ratio": float(n / denom) if denom > 0 else 0.0,
        "area_ha": float(n) * float(pixel_area_m2) / M2_PER_HA,
        "threshold": abs(float(threshold)),
        "band_ratio": float(ratio),
        "band_low": float(low),
        "band_high": float(high),
        "note": TRANSITION_BAND_NOTE,
    }


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
