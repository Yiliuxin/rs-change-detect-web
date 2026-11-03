# -*- coding: utf-8 -*-
"""Otsu 自适应阈值（HLD 模块3 功能二，REQ-07）。

纯函数：自实现直方图类间方差最大化，阈值 T > 0 用于对称三类判别
（Δ > +T 增强 / Δ < −T 退化 / |Δ| ≤ T 稳定），故对 ``|ΔNDVI|`` 建直方图。

直方图范围固定在 ``|ΔNDVI| ∈ [0, 2]``（NDVI 定义域 [-1,1] 推导所得），
避免暗水体近零分母产生的极端离群值把箱宽拉爆、把阈值推到无意义的量级。

退化情形（全 NaN / 有效样本过少 / 单峰单值 / 方差过小 / 类间方差恒 0）一律回退
``app.config.constants.OTSU_FALLBACK_DNDVI``；**阈值不硬编码**。
"""
from __future__ import annotations

from typing import Optional, Tuple

import numpy as np

from app.config.constants import BLOCK_SIZE, MIN_SAMPLES_PER_CLASS, OTSU_FALLBACK_DNDVI

# NDVI 物理定义域（与 index_calculator.INDEX_DOMAIN 一致）
NDVI_DOMAIN_MIN: float = -1.0
NDVI_DOMAIN_MAX: float = 1.0
# |ΔNDVI| 的数学上界 = max(NDVI) − min(NDVI) = 2.0
DNDVI_ABS_MAX: float = NDVI_DOMAIN_MAX - NDVI_DOMAIN_MIN
# 直方图箱数复用常量源 BLOCK_SIZE（256），避免另立魔法数字
HISTOGRAM_BINS: int = int(BLOCK_SIZE)
# 数值退化判据的比例因子（相对回退阈值，属数值容差而非业务阈值）
DEGENERATE_REL_EPS: float = 1e-3

__all__ = ["DNDVI_ABS_MAX", "HISTOGRAM_BINS", "otsu_threshold"]


def otsu_threshold(
    dndvi,
    valid_mask: Optional[np.ndarray] = None,
    *,
    bins: int = HISTOGRAM_BINS,
    histogram_max: float = DNDVI_ABS_MAX,
    min_samples: int = MIN_SAMPLES_PER_CLASS,
    fallback: float = OTSU_FALLBACK_DNDVI,
) -> Tuple[float, bool, str]:
    """返回 ``(threshold, fallback_used, reason)``。

    参数：
        dndvi        ：ΔNDVI 数组（NaN 表示无效）
        valid_mask   ：可选有效掩膜（True 有效）
        bins         ：直方图箱数（默认取 BLOCK_SIZE）
        histogram_max：直方图上限（默认由 NDVI 定义域推导的 2.0）
        min_samples  ：直方图有效性所需最少样本（默认取 MIN_SAMPLES_PER_CLASS）
        fallback     ：退化回退阈值（默认取 OTSU_FALLBACK_DNDVI）
    """
    arr = np.asarray(dndvi, dtype=np.float32)
    finite = np.isfinite(arr)
    if valid_mask is not None:
        vm = np.asarray(valid_mask, dtype=bool)
        if vm.shape != arr.shape:
            raise ValueError(f"valid_mask 形状 {vm.shape} 与 ΔNDVI {arr.shape} 不一致")
        finite &= vm

    # 对称阈值取 |Δ|，量纲与"增强/退化"判别一致
    values = np.abs(arr[finite]).astype(np.float64)
    n = int(values.size)

    if n == 0:
        return (float(fallback), True, "全 NaN / 无有效样本，回退固定阈值")
    if n < min_samples:
        return (float(fallback), True,
                f"有效样本过少（{n} < {min_samples}），回退固定阈值")

    upper = float(histogram_max)
    if not np.isfinite(upper) or upper <= 0.0:
        raise ValueError(f"histogram_max 需为正有限数，实际 {histogram_max!r}")
    nbins = int(bins)
    if nbins < 2:
        raise ValueError(f"bins 需 ≥ 2，实际 {bins!r}")

    # 退化判据：单值 / 方差过小。比例容差由回退阈值导出，不引入业务硬编码
    range_eps = float(fallback) * DEGENERATE_REL_EPS
    std_eps = float(fallback) * DEGENERATE_REL_EPS
    value_range = float(values.max() - values.min())
    value_std = float(values.std())
    if value_range <= range_eps or value_std <= std_eps:
        return (float(fallback), True,
                f"直方图退化：单值/方差过小（range={value_range:.6g}, std={value_std:.6g}），回退固定阈值")

    # 固定物理定义域直方图：超上界的离群值收敛到末箱，不污染箱宽
    hist, edges = np.histogram(np.clip(values, 0.0, upper), bins=nbins, range=(0.0, upper))
    if int(np.count_nonzero(hist)) <= 1:
        return (float(fallback), True, "直方图退化：仅单箱有效（单峰），回退固定阈值")

    centers = (edges[:-1] + edges[1:]) / 2.0
    w0 = np.cumsum(hist).astype(np.float64)
    total = float(w0[-1])
    w1 = total - w0
    cum_sum = np.cumsum(centers * hist).astype(np.float64)

    # 类间方差 ∝ w0*w1*(μ0−μ1)^2（分母 total² 为常数，略去不影响 argmax）
    mean0 = np.divide(cum_sum, w0, out=np.zeros_like(cum_sum), where=w0 > 0)
    mean1 = np.divide(cum_sum[-1] - cum_sum, w1,
                      out=np.zeros_like(cum_sum), where=w1 > 0)
    between = w0 * w1 * (mean0 - mean1) ** 2
    between[(w0 == 0) | (w1 == 0)] = 0.0

    if not np.any(between > 0.0):
        return (float(fallback), True, "类间方差恒为 0（直方图退化），回退固定阈值")

    threshold = float(centers[int(np.argmax(between))])
    if not np.isfinite(threshold) or threshold <= 0.0 or threshold >= upper:
        return (float(fallback), True,
                f"Otsu 阈值落在边界/非有限值（{threshold:.6g}），回退固定阈值")

    return (
        threshold,
        False,
        f"Otsu 类间方差最大化（bins={nbins}, 有效样本={n}, |Δ|上界={upper:g}）",
    )
