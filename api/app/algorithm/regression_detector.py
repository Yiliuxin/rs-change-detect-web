# -*- coding: utf-8 -*-
"""回归残差法变化检测（PRD REQ-11 提前实现）。

与既有差值法（``change_detector.delta_ndvi``）并列的第二种检测方法：

1. 在双时相有效像元交集上，最小二乘拟合 ``index_t2 = a * index_t1 + b``；
2. 迭代剔除 ``|残差| > sigma_factor * sigma`` 的像元后重新拟合，最多 ``max_rounds`` 轮
   （稳健拟合，避免把强变化块拉进回归系数）；
3. 用最终 a/b 对**全部有效像元**计算残差
   ``r = index_t2 - (a * index_t1 + b)``；
4. 对 ``|r|`` 做 Otsu 自适应阈值（或手动阈值）；
   ``r > +T`` 增强 / ``r < -T`` 退化 / ``|r| <= T`` 稳定；
5. 形态学去碎块与差值法完全一致（复用 ``morphology.clean_change_map``）。

纯函数：不连数据库、不做 IO、不引入随机数；全部数值口径取常量源。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

import numpy as np

from app.algorithm.change_detector import classify_change
from app.algorithm.morphology import clean_change_map
from app.algorithm.otsu import otsu_threshold
from app.config.constants import (
    REGRESSION_MAX_ROUNDS, REGRESSION_SIGMA_FACTOR,
)

__all__ = [
    "RegressionFit", "fit_robust_regression", "residual_map",
    "detect_change_regression",
]


@dataclass(frozen=True)
class RegressionFit:
    """稳健最小二乘拟合结果（REQ-14 可复现快照字段来源）。"""

    a: float
    b: float
    sigma: float
    rounds: int
    inlier_mask: np.ndarray
    residuals: np.ndarray
    n_valid: int
    n_inliers: int


def _rms(values: np.ndarray) -> float:
    """残差均方根（LS 拟合后残差均值恒为 0，等价于标准差）。"""
    if values.size == 0:
        return 0.0
    return float(np.sqrt(float(np.mean(np.square(values, dtype=np.float64)))))


def _least_squares(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """一元最小二乘：a = cov(x,y)/var(x)，b = mean(y) − a·mean(x)。"""
    mx, my = float(x.mean()), float(y.mean())
    dx = x - mx
    var = float(np.mean(np.square(dx)))
    if var <= 0.0 or not np.isfinite(var):
        return 0.0, my
    a = float(np.mean(dx * (y - my)) / var)
    return a, my - a * mx


def fit_robust_regression(
    index_t1,
    index_t2,
    valid_mask: Optional[np.ndarray] = None,
    *,
    max_rounds: int = REGRESSION_MAX_ROUNDS,
    sigma_factor: float = REGRESSION_SIGMA_FACTOR,
) -> RegressionFit:
    """迭代剔除粗差后拟合 ``index_t2 = a·index_t1 + b``（最多 max_rounds 轮）。

    ``valid_mask`` 之外的像元与 NaN 一律不参与拟合；返回的 ``residuals`` 用最终
    a/b 对全部有效像元计算，掩膜外为 NaN（与差值法 NaN 传播口径一致）。
    """
    x = np.asarray(index_t1, dtype=np.float64)
    y = np.asarray(index_t2, dtype=np.float64)
    if x.shape != y.shape:
        raise ValueError(f"双时相指数形状不一致：{x.shape} vs {y.shape}")

    finite = np.isfinite(x) & np.isfinite(y)
    if valid_mask is not None:
        vm = np.asarray(valid_mask, dtype=bool)
        if vm.shape != x.shape:
            raise ValueError(f"valid_mask 形状 {vm.shape} 与指数数组 {x.shape} 不一致")
        finite &= vm
    n_valid = int(finite.sum())
    if n_valid < 2:
        raise ValueError(f"有效像元不足（{n_valid} < 2），无法拟合 NDVI 回归关系")

    inliers = finite
    rounds_limit = max(1, int(max_rounds))
    factor = float(sigma_factor)
    a, b = 0.0, 0.0
    rounds = 0
    for round_no in range(1, rounds_limit + 1):
        a, b = _least_squares(x[inliers], y[inliers])
        r = y - (a * x + b)
        sigma = _rms(r[inliers])
        rounds = round_no
        if sigma <= 0.0 or factor <= 0.0:
            break
        keep = inliers & (np.abs(r) <= factor * sigma)
        if np.array_equal(keep, inliers) or int(keep.sum()) < 2:
            break
        inliers = keep

    residuals = np.full(x.shape, np.float32(np.nan), dtype=np.float32)
    residuals[finite] = (y[finite] - (a * x[finite] + b)).astype(np.float32)
    sigma = _rms((y - (a * x + b))[inliers])

    return RegressionFit(
        a=float(a), b=float(b), sigma=float(sigma), rounds=int(rounds),
        inlier_mask=inliers, residuals=residuals,
        n_valid=n_valid, n_inliers=int(inliers.sum()),
    )


def residual_map(index_t1, index_t2, a: float, b: float,
                 valid_mask: Optional[np.ndarray] = None) -> np.ndarray:
    """``r = index_t2 − (a·index_t1 + b)``；无效像元为 NaN（float32）。"""
    x = np.asarray(index_t1, dtype=np.float32)
    y = np.asarray(index_t2, dtype=np.float32)
    if x.shape != y.shape:
        raise ValueError(f"双时相指数形状不一致：{x.shape} vs {y.shape}")
    finite = np.isfinite(x) & np.isfinite(y)
    if valid_mask is not None:
        vm = np.asarray(valid_mask, dtype=bool)
        if vm.shape != x.shape:
            raise ValueError(f"valid_mask 形状 {vm.shape} 与指数数组 {x.shape} 不一致")
        finite &= vm
    out = np.full(x.shape, np.float32(np.nan), dtype=np.float32)
    out[finite] = (y[finite] - (float(a) * x[finite] + float(b))).astype(np.float32)
    return out


def detect_change_regression(
    index_t1,
    index_t2,
    valid_mask: Optional[np.ndarray] = None,
    threshold: Optional[float] = None,
    *,
    max_rounds: int = REGRESSION_MAX_ROUNDS,
    sigma_factor: float = REGRESSION_SIGMA_FACTOR,
) -> Dict[str, object]:
    """回归残差法主入口：返回拟合量、残差、阈值与三类变化图。

    ``threshold`` 提供时跳过 Otsu，作对称阈值（与差值法手动阈值口径一致）。
    返回值中 ``change_raw`` 为形态学前分类结果，由调用方统一执行
    ``clean_change_map``，保证两种方法的后处理完全一致。
    """
    fit = fit_robust_regression(
        index_t1, index_t2, valid_mask=valid_mask,
        max_rounds=max_rounds, sigma_factor=sigma_factor,
    )
    r = fit.residuals
    if threshold is None:
        thr, fallback, reason = otsu_threshold(r, valid_mask=valid_mask)
        threshold_source = "otsu"
    else:
        thr = float(threshold)
        fallback = False
        reason = "手动阈值覆盖，跳过 Otsu 自适应（REQ-07）"
        threshold_source = "manual"

    change_raw = classify_change(r, abs(float(thr)), valid_mask=valid_mask)
    return {
        "a": fit.a,
        "b": fit.b,
        "sigma": fit.sigma,
        "rounds": fit.rounds,
        "max_rounds": int(max_rounds),
        "sigma_factor": float(sigma_factor),
        "n_valid": fit.n_valid,
        "n_inliers": fit.n_inliers,
        "residual": r,
        "threshold": float(thr),
        "threshold_source": threshold_source,
        "otsu_fallback": bool(fallback),
        "otsu_reason": str(reason),
        "change_raw": change_raw,
        "change": clean_change_map(change_raw),
    }
