# -*- coding: utf-8 -*-
"""变化强度分级（A1，REQ-07 扩展）：在三类阈值 T 之上按 |Δ| 相对 T 的倍数分五档。

纯函数：不连数据库、不读写文件、不修改入参（所有输出均为新数组 / 新 dict）。
三类编码 0/1/2/3 仍由 ``app.algorithm.change_detector`` 唯一定义，本模块只引用。

分档口径（K 取自 ``constants.INTENSITY_K``，缺省 2.0）：
    d ≤ −K·T  → 1 显著退化      −K·T < d < −T → 2 轻微退化
    |d| ≤ T   → 3 稳定          T < d < K·T   → 4 轻微改善
    d ≥ K·T   → 5 显著改善      NaN / 无效     → 0

口径一致性：``change`` 传入（管线形态学后的最终三类图）时，以三类为粗类权威，
在类内按同一 K·T 边界细分，保证 4+5 档计数 == 增强、1+2 档 == 退化、3 == 稳定。
``change=None`` 时按上述差分边界直接落五档（单测用）。
"""
from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np

from app.algorithm.change_detector import (
    CHANGE_DEGRADED, CHANGE_ENHANCED, CHANGE_NODATA, CHANGE_STABLE, M2_PER_HA,
)
from app.config.constants import (
    INTENSITY_CODES, INTENSITY_K, INTENSITY_LABELS_EN, INTENSITY_LABELS_ZH,
    INTENSITY_NODATA, INTENSITY_NOTE,
)

__all__ = [
    "INTENSITY_NODATA", "intensity_grade", "classify_intensity", "intensity_stats",
]


def _as_f32(arr) -> np.ndarray:
    return np.asarray(arr, dtype=np.float32)


def _boundary(threshold: float, k: float) -> float:
    """校验并返回 K·T（T 取绝对值以与「对称阈值」口径一致）。"""
    t = abs(float(threshold))
    kk = float(k)
    if not np.isfinite(t) or t < 0.0:
        raise ValueError(f"threshold 需为有限非负数，实际 {threshold!r}")
    if not np.isfinite(kk) or kk < 1.0:
        raise ValueError(f"K 需为 ≥ 1 的有限数，实际 {k!r}")
    return kk * t


def intensity_grade(delta, threshold: float, change_class=None,
                    k: float = INTENSITY_K) -> int:
    """标量分档（鼠标拾取探针复用同一口径）。NaN / 无效 → 0。"""
    strong = _boundary(threshold, k)
    t = abs(float(threshold))
    d = float(delta)
    if not np.isfinite(d):
        return INTENSITY_NODATA

    if change_class is None:
        if d <= -strong:
            return 1
        if d < -t:
            return 2
        if d <= t:
            return 3
        if d < strong:
            return 4
        return 5

    code = int(change_class)
    if code == CHANGE_ENHANCED:
        return 5 if d >= strong else 4
    if code == CHANGE_DEGRADED:
        return 1 if d <= -strong else 2
    if code == CHANGE_STABLE:
        return 3
    return INTENSITY_NODATA


def classify_intensity(delta, threshold: float,
                       valid_mask: Optional[np.ndarray] = None,
                       change=None,
                       k: float = INTENSITY_K) -> np.ndarray:
    """按五档边界分类，返回 uint8（0 无效）；不修改任何入参。"""
    d = _as_f32(delta)
    strong = _boundary(threshold, k)
    t = abs(float(threshold))

    valid = np.isfinite(d)
    if valid_mask is not None:
        vm = np.asarray(valid_mask, dtype=bool)
        if vm.shape != d.shape:
            raise ValueError(f"valid_mask 形状 {vm.shape} 与 Δ 数组 {d.shape} 不一致")
        valid &= vm

    out = np.full(d.shape, INTENSITY_NODATA, dtype=np.uint8)
    if change is None:
        # 纯差分边界：先铺稳定，再按 |Δ| 逐级覆盖（顺序保证边界落在低档）
        out[valid] = 3
        out[valid & (d < -t)] = 2
        out[valid & (d <= -strong)] = 1
        out[valid & (d > t)] = 4
        out[valid & (d >= strong)] = 5
        return out

    c = np.asarray(change)
    if c.shape != d.shape:
        raise ValueError(f"change 形状 {c.shape} 与 Δ 数组 {d.shape} 不一致")
    # 形态学后的三类图为粗类权威；类内仍按同一 K·T 边界细分
    enh = valid & (c == CHANGE_ENHANCED)
    deg = valid & (c == CHANGE_DEGRADED)
    sta = valid & (c == CHANGE_STABLE)
    out[enh] = 4
    out[enh & (d >= strong)] = 5
    out[deg] = 2
    out[deg & (d <= -strong)] = 1
    out[sta] = 3
    return out


def intensity_stats(delta, threshold: float,
                    change=None,
                    valid_mask: Optional[np.ndarray] = None,
                    k: float = INTENSITY_K,
                    pixel_area_m2: Optional[float] = None) -> Dict[str, object]:
    """五档统计（可 JSON 序列化）：逐档计数 / 面积 / 占比 + 三类口径自洽量。

    返回值直接进入 params_snapshot / run_report.json / 接口响应三处。
    """
    grades = classify_intensity(delta, threshold, valid_mask=valid_mask,
                                change=change, k=k)
    strong = _boundary(threshold, k)
    t = abs(float(threshold))
    area = float(pixel_area_m2) if pixel_area_m2 is not None else None

    counts = {int(code): int((grades == code).sum()) for code in INTENSITY_CODES}
    valid_count = int(sum(counts.values()))
    rows: List[Dict[str, object]] = []
    for code in INTENSITY_CODES:
        n = counts[int(code)]
        item: Dict[str, object] = {
            "code": int(code),
            "label": INTENSITY_LABELS_ZH[int(code)],
            "label_en": INTENSITY_LABELS_EN[int(code)],
            "pixel_count": n,
            "ratio": float(n / valid_count) if valid_count else 0.0,
        }
        if area is not None:
            item["area_ha"] = float(n) * area / M2_PER_HA
        rows.append(item)

    return {
        "skipped": False,
        "threshold": t,
        "k": float(k),
        "boundary": float(strong),
        "note": INTENSITY_NOTE,
        "labels": {str(c): INTENSITY_LABELS_ZH[c] for c in INTENSITY_CODES},
        "levels": rows,
        "counts": {str(c): counts[c] for c in INTENSITY_CODES},
        "valid_count": valid_count,
        "invalid_count": int((grades == INTENSITY_NODATA).sum()),
        "total_pixels": int(grades.size),
        # 三类口径自洽量：4+5 增强 / 1+2 退化 / 3 稳定
        "group_counts": {
            str(CHANGE_ENHANCED): counts[4] + counts[5],
            str(CHANGE_DEGRADED): counts[1] + counts[2],
            str(CHANGE_STABLE): counts[3],
        },
        "pixel_area_m2": area,
    }
