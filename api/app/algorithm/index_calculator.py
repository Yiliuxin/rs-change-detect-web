# -*- coding: utf-8 -*-
"""光谱指数计算（HLD 模块3 功能一，REQ-06）。

纯函数：NDVI / NDWI，分块计算 + 分母零保护，不连数据库、不写路由。

要点：
  * NDVI = (NIR - R) / (NIR + R)；分母 ``|NIR + R| < DENOM_EPS`` 判为无效（NaN）。
  * 按常量 ``BLOCK_SIZE`` 分块逐块求商，避免整景同时入内存（NF3 / REQ-03）。
  * ``valid_mask`` 之外的像元一律置 NaN；输入中含 NaN 时自然传播。
  * ``clip_index`` 把指数约束回物理定义域 [-1, 1]：暗水体（BOA 反射率可为小负值）
    在近零分母处会把比值数学放大到 ±数十，属数值伪影而非真实地物。
"""
from __future__ import annotations

from typing import Optional, Tuple

import numpy as np

from app.algorithm.block_iterator import iter_slices
from app.config.constants import BLOCK_SIZE

# 分母零判定的数值容差（数学量，非业务阈值；REQ-06 规定 |NIR+R| < 1e-6）
DENOM_EPS: float = 1e-6
# NDVI / NDWI 的物理定义域（用于剔除近零分母造成的数值伪影）
INDEX_DOMAIN: Tuple[float, float] = (-1.0, 1.0)

__all__ = [
    "DENOM_EPS",
    "INDEX_DOMAIN",
    "ndvi",
    "ndwi",
    "clip_index",
]


def _as_f32(arr) -> np.ndarray:
    """统一转 float32，保证同输入同输出（确定性）。"""
    return np.asarray(arr, dtype=np.float32)


def _safe_ratio(num: np.ndarray, den: np.ndarray, block_size: int = BLOCK_SIZE) -> np.ndarray:
    """分块求 num / den；分母近零或非有限输入 → NaN（float32）。"""
    num = _as_f32(num)
    den = _as_f32(den)
    if num.shape != den.shape:
        raise ValueError(f"分子/分母形状不一致：{num.shape} vs {den.shape}")

    out = np.full(num.shape, np.float32(np.nan), dtype=np.float32)
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        if num.ndim == 2:
            # REQ-03 / NF3：行主序分块，峰值内存约为单块大小
            for row_sl, col_sl in iter_slices(num.shape[0], num.shape[1], block_size=block_size):
                n = num[row_sl, col_sl]
                d = den[row_sl, col_sl]
                good = np.isfinite(n) & np.isfinite(d) & (np.abs(d) >= DENOM_EPS)
                ratio = n / np.where(good, d, np.float32(1.0))
                ratio[~good] = np.float32(np.nan)
                out[row_sl, col_sl] = ratio
        else:
            good = np.isfinite(num) & np.isfinite(den) & (np.abs(den) >= DENOM_EPS)
            ratio = num / np.where(good, den, np.float32(1.0))
            out = np.where(good, ratio, np.float32(np.nan)).astype(np.float32)
    return out


def _apply_valid(arr: np.ndarray, valid_mask: Optional[np.ndarray]) -> np.ndarray:
    """掩膜外置 NaN；掩膜为 None 时原样返回。"""
    if valid_mask is None:
        return arr
    vm = np.asarray(valid_mask, dtype=bool)
    if vm.shape != arr.shape:
        raise ValueError(f"valid_mask 形状 {vm.shape} 与指数数组 {arr.shape} 不一致")
    out = arr.copy()
    out[~vm] = np.float32(np.nan)
    return out


def ndvi(red, nir, valid_mask: Optional[np.ndarray] = None) -> np.ndarray:
    """NDVI = (NIR − R) / (NIR + R)，返回 float32；无效像元为 NaN（REQ-06）。

    结果强制约束回物理定义域 [-1, 1]：低反射率像元（暗水体 / 阴影）的
    NIR + R 可以趋近 0，比值在数学上正确但会放大到 ±数十，属数值伪影。
    """
    r = _as_f32(red)
    n = _as_f32(nir)
    result = clip_index(_safe_ratio(n - r, n + r))
    return _apply_valid(result, valid_mask)


def ndwi(green, nir, valid_mask: Optional[np.ndarray] = None) -> np.ndarray:
    """NDWI（McFeeters）= (G − NIR) / (G + NIR)，返回 float32；无效像元为 NaN。"""
    g = _as_f32(green)
    n = _as_f32(nir)
    result = clip_index(_safe_ratio(g - n, g + n))
    return _apply_valid(result, valid_mask)


def clip_index(index, domain: Tuple[float, float] = INDEX_DOMAIN) -> np.ndarray:
    """把指数约束回物理定义域（默认 [-1, 1]），NaN 保持 NaN。

    暗水体 BOA 反射率可为小负值，近零分母会把比值放大到定义域之外；
    截断是标准的可解释数值稳健化，不改变三类编码与阈值语义。
    """
    arr = _as_f32(index)
    return np.clip(arr, domain[0], domain[1]).astype(np.float32)
