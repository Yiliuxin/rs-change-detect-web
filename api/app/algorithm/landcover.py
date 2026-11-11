# -*- coding: utf-8 -*-
"""地物转移矩阵（A3）：NDVI / NDWI 联合判据分 4 类 + 4×4 转移计数。

纯函数：不连数据库、不读写文件、不修改入参。所有阈值 / 档位 / 类别常量
均取自 ``app.config.constants``，本模块只引用，不写死。

判据（顺序判断，先命中先归类；任一指数无效 → 0）：
    NDWI > 0        → 1 水体        （即使 NDVI 很高也优先判水体）
    否则 NDVI ≥ 0.30 → 2 植被
    否则 NDVI < 0.15 → 3 裸土/建成
    否则              → 4 其他

``transition_matrix`` 用一次 ``np.bincount`` 统计 16 种 (t1, t2) 组合，
reshape 成 4×4；非对角 Top-N 只取计数最大的若干项，不含对角。
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional

import numpy as np

from app.algorithm.change_detector import M2_PER_HA
from app.config.constants import (
    LANDCOVER_CODES, LANDCOVER_LABELS_EN, LANDCOVER_LABELS_ZH,
    LANDCOVER_NODATA, LANDCOVER_NOTE, NDVI_BARE_LT, NDVI_VEGETATION_GE,
    NDWI_WATER_GT, TRANSITION_TOP_N,
)

__all__ = [
    "LANDCOVER_NODATA", "classify_landcover_scalar", "classify_landcover",
    "transition_matrix",
]


def classify_landcover_scalar(ndvi, ndwi, valid: bool = True) -> int:
    """标量判类（鼠标拾取探针复用同一口径）。无效 → 0。"""
    if not valid:
        return LANDCOVER_NODATA
    v, w = float(ndvi), float(ndwi)
    if not (math.isfinite(v) and math.isfinite(w)):
        return LANDCOVER_NODATA
    if w > NDWI_WATER_GT:
        return 1
    if v >= NDVI_VEGETATION_GE:
        return 2
    if v < NDVI_BARE_LT:
        return 3
    return 4


def classify_landcover(ndvi, ndwi,
                       valid_mask: Optional[np.ndarray] = None) -> np.ndarray:
    """NDVI / NDWI 联合判据分 4 类，返回 uint8；NaN / 无效 → 0。"""
    v = np.asarray(ndvi, dtype=np.float32)
    w = np.asarray(ndwi, dtype=np.float32)
    if v.shape != w.shape:
        raise ValueError(f"NDVI 形状 {v.shape} 与 NDWI 形状 {w.shape} 不一致")
    ok = np.isfinite(v) & np.isfinite(w)
    if valid_mask is not None:
        vm = np.asarray(valid_mask, dtype=bool)
        if vm.shape != v.shape:
            raise ValueError(f"valid_mask 形状 {vm.shape} 与指数数组 {v.shape} 不一致")
        ok &= vm

    water = ok & (w > NDWI_WATER_GT)
    vegetation = ok & ~water & (v >= NDVI_VEGETATION_GE)
    bare = ok & ~water & ~vegetation & (v < NDVI_BARE_LT)
    other = ok & ~water & ~vegetation & ~bare

    out = np.full(v.shape, LANDCOVER_NODATA, dtype=np.uint8)
    out[water] = 1
    out[vegetation] = 2
    out[bare] = 3
    out[other] = 4
    return out


def transition_matrix(lc_t1, lc_t2, pixel_area_m2: float,
                      top_n: int = TRANSITION_TOP_N,
                      valid_mask: Optional[np.ndarray] = None) -> Dict[str, object]:
    """4×4 像元数 / 面积矩阵、对角未变占比、非对角 Top-N 转移。

    ``np.bincount`` 一次性统计 16 个组合；矩阵行 = t1 地类、列 = t2 地类。
    """
    a = np.asarray(lc_t1)
    b = np.asarray(lc_t2)
    if a.shape != b.shape:
        raise ValueError(f"t1 形状 {a.shape} 与 t2 形状 {b.shape} 不一致")
    ok = ((a >= LANDCOVER_CODES[0]) & (a <= LANDCOVER_CODES[-1])
          & (b >= LANDCOVER_CODES[0]) & (b <= LANDCOVER_CODES[-1]))
    if valid_mask is not None:
        vm = np.asarray(valid_mask, dtype=bool)
        if vm.shape != a.shape:
            raise ValueError(f"valid_mask 形状 {vm.shape} 与地类数组 {a.shape} 不一致")
        ok &= vm

    n_class = len(LANDCOVER_CODES)  # 4
    idx = ((a[ok].astype(np.int64) - 1) * n_class
           + (b[ok].astype(np.int64) - 1))
    # 一次性计数（不逐格循环、不逐类 bincount）
    flat = np.bincount(idx, minlength=n_class * n_class)[:n_class * n_class]
    counts = flat.reshape(n_class, n_class)

    area = float(pixel_area_m2)
    pixel_matrix = [[int(x) for x in row] for row in counts]
    area_matrix = [[int(x) * area / M2_PER_HA for x in row] for row in counts]
    row_totals = [int(x) for x in counts.sum(axis=1)]
    col_totals = [int(x) for x in counts.sum(axis=0)]
    valid_count = int(counts.sum())
    diagonal = int(np.trace(counts))
    unchanged_ratio = float(diagonal / valid_count) if valid_count else 0.0

    candidates: List[Dict[str, object]] = []
    for i, c1 in enumerate(LANDCOVER_CODES):
        for j, c2 in enumerate(LANDCOVER_CODES):
            if i == j:  # 非对角 Top-N 不含「未变」
                continue
            n = int(counts[i, j])
            if n <= 0:
                continue
            candidates.append({
                "from": int(c1),
                "to": int(c2),
                "from_label": LANDCOVER_LABELS_ZH[int(c1)],
                "to_label": LANDCOVER_LABELS_ZH[int(c2)],
                "label_en": (f"{LANDCOVER_LABELS_EN[int(c1)]}->"
                             f"{LANDCOVER_LABELS_EN[int(c2)]}"),
                "pixel_count": n,
                "area_ha": n * area / M2_PER_HA,
            })
    candidates.sort(key=lambda it: (-int(it["pixel_count"]),
                                    int(it["from"]), int(it["to"])))
    top = candidates[:max(0, int(top_n))]

    return {
        "labels": {str(c): LANDCOVER_LABELS_ZH[c] for c in LANDCOVER_CODES},
        "labels_en": {str(c): LANDCOVER_LABELS_EN[c] for c in LANDCOVER_CODES},
        "codes": [int(c) for c in LANDCOVER_CODES],
        "pixel_matrix": pixel_matrix,
        "area_matrix_ha": area_matrix,
        "row_totals": row_totals,
        "col_totals": col_totals,
        "valid_count": valid_count,
        "unchanged_count": diagonal,
        "unchanged_ratio": unchanged_ratio,
        "top": top,
        "top_n": int(top_n),
        "pixel_area_m2": area,
        "criteria": {
            "ndwi_water_gt": float(NDWI_WATER_GT),
            "ndvi_vegetation_ge": float(NDVI_VEGETATION_GE),
            "ndvi_bare_lt": float(NDVI_BARE_LT),
        },
        "note": LANDCOVER_NOTE,
    }
