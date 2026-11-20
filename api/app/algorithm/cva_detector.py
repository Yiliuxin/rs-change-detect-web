# -*- coding: utf-8 -*-
"""变化向量分析（CVA，Change Vector Analysis）变化检测（批次 G / REQ-11）。

与差值法（``change_detector.delta_ndvi``）、回归残差法（``regression_detector``）
并列的第三种检测方法。**唯一新增的步骤是「幅值计算」**，分类、清理、统计、过渡带、
转移矩阵、图斑形态全部复用既有管线：

1. 输入取与现有检测**同源的双波段指数栅格**（``index_t1.tif`` / ``index_t2.tif``，
   band1 = 主检测指数、band2 = 辅助指数；回归法只取其中的主指数波段）。
   逐波段求 ``Δ_b = t2_b − t1_b``，变化幅值为逐像元欧氏距离
   ``CVA = sqrt(Σ_b Δ_b²)``（float32，无效像元 NaN 语义与差值法一致）；
2. 对 **CVA 幅值图** 做 Otsu 自适应阈值（复用 ``otsu.otsu_threshold``，与
   regression 落 otsu_threshold 的路径一致）；manual 时取用户给定的幅值阈值；
3. ``classify_change`` 需要带方向的输入，故用主指数差符号给幅值定向：
   ``signed = sign(Δ_主) · CVA``。此时 ``|signed| ≡ CVA``，于是
   ``transition_band_*`` 的 ``| |Δ| − T | ≤ 0.25T`` 恰好等价于任务书口径
   ``|CVA − T| ≤ 0.25T``，A1 强度分级 / 直方图也无需任何改动。

纯函数：不连数据库、不做 IO、不引入随机数；数值口径全部取常量源。
"""
from __future__ import annotations

from typing import Dict, Optional

import numpy as np

from app.algorithm.change_detector import classify_change
from app.algorithm.morphology import clean_change_map
from app.algorithm.otsu import otsu_threshold

__all__ = ["cva_magnitude", "cva_signed_magnitude", "detect_change_cva"]


def _as_bands(bands, name: str) -> list:
    """把输入统一成波段列表：2D 单波段 / (B,H,W) 三维栈 / 2D 数组序列。"""
    if isinstance(bands, np.ndarray):
        if bands.ndim == 2:
            return [np.asarray(bands, dtype=np.float32)]
        if bands.ndim == 3 and bands.shape[0] >= 1:
            return [np.asarray(bands[i], dtype=np.float32)
                    for i in range(bands.shape[0])]
        raise ValueError(f"{name} 需为 2D 或 (B,H,W) 三维数组，实际 shape={bands.shape}")
    if isinstance(bands, (list, tuple)):
        items = [np.asarray(b, dtype=np.float32) for b in bands]
        if not items:
            raise ValueError(f"{name} 波段列表不能为空")
        if any(b.ndim != 2 for b in items):
            raise ValueError(f"{name} 每个波段需为 2D 数组")
        return items
    raise ValueError(f"{name} 需为 numpy 数组或 2D 波段序列，实际 {type(bands)!r}")


def _cva(bands_t1, bands_t2, valid_mask):
    """返回 ``(幅值, 定向幅值)``；幅值恒 ≥ 0，定向幅值的绝对值恒等于幅值。"""
    b1 = _as_bands(bands_t1, "t1")
    b2 = _as_bands(bands_t2, "t2")
    if len(b1) != len(b2):
        raise ValueError(f"双时相波段数不一致：t1={len(b1)} vs t2={len(b2)}")
    shape = b1[0].shape
    for side, group in (("t1", b1), ("t2", b2)):
        for band in group:
            if band.shape != shape:
                raise ValueError(f"{side} 波段形状不一致：{band.shape} vs {shape}")

    finite = np.ones(shape, dtype=bool)
    sq = np.zeros(shape, dtype=np.float64)
    for x, y in zip(b1, b2):
        finite &= np.isfinite(x) & np.isfinite(y)
        d = y.astype(np.float64) - x.astype(np.float64)
        sq += d * d
    if valid_mask is not None:
        vm = np.asarray(valid_mask, dtype=bool)
        if vm.shape != shape:
            raise ValueError(f"valid_mask 形状 {vm.shape} 与波段数组 {shape} 不一致")
        finite &= vm

    mag = np.full(shape, np.float32(np.nan), dtype=np.float32)
    mag[finite] = np.sqrt(sq[finite]).astype(np.float32)

    # CVA 方向分量：主指数差符号 × 幅值，|signed| 恒等于 CVA 幅值，
    # 使 classify_change / transition_band_* / intensity 无需任何改动。
    primary = b2[0].astype(np.float64) - b1[0].astype(np.float64)
    signed = np.where(primary >= 0.0, mag, -mag).astype(np.float32)
    signed[~np.isfinite(mag)] = np.float32(np.nan)
    return mag, signed


def cva_magnitude(bands_t1, bands_t2,
                  valid_mask: Optional[np.ndarray] = None) -> np.ndarray:
    """``CVA = sqrt(Σ_b (t2_b − t1_b)²)``；float32，无效像元为 NaN。"""
    return _cva(bands_t1, bands_t2, valid_mask)[0]


def cva_signed_magnitude(bands_t1, bands_t2,
                         valid_mask: Optional[np.ndarray] = None) -> np.ndarray:
    """带方向的幅值 ``sign(Δ_主)·CVA``；``|out| ≡ cva_magnitude``（float32 / NaN）。"""
    return _cva(bands_t1, bands_t2, valid_mask)[1]


def detect_change_cva(
    bands_t1,
    bands_t2,
    valid_mask: Optional[np.ndarray] = None,
    threshold: Optional[float] = None,
) -> Dict[str, object]:
    """CVA 主入口：返回幅值、定向幅值、阈值与三类变化图。

    ``threshold`` 提供时跳过 Otsu，作**幅值阈值**（与差值法手动阈值口径一致）。
    返回值中 ``change_raw`` 为形态学前分类结果，由调用方统一执行
    ``clean_change_map``，保证三种方法的后处理完全一致。
    """
    mag, signed = _cva(bands_t1, bands_t2, valid_mask)
    if threshold is None:
        thr, fallback, reason = otsu_threshold(mag, valid_mask=valid_mask)
        threshold_source = "otsu"
    else:
        thr = float(threshold)
        fallback = False
        reason = "手动幅值阈值覆盖，跳过 Otsu 自适应（REQ-07）"
        threshold_source = "manual"

    change_raw = classify_change(signed, abs(float(thr)), valid_mask=valid_mask)
    return {
        "magnitude": mag,
        "signed_magnitude": signed,
        "threshold": float(thr),
        "threshold_source": threshold_source,
        "otsu_fallback": bool(fallback),
        "otsu_reason": str(reason),
        "change_raw": change_raw,
        "change": clean_change_map(change_raw),
    }
