# -*- coding: utf-8 -*-
"""双时相网格对齐校验 + 真彩/假彩合成（HLD 模块3，REQ-03 / REQ-10）。

纯函数：不连数据库、不写路由。网格校验失败抛 ``GridMismatchError``（EX4）。
"""
from __future__ import annotations

import math
from typing import Dict, Optional, Sequence, Tuple

import numpy as np

from app.config.constants import ERROR_MESSAGES

# 左上角坐标 / 分辨率比较的数值容差（米；非业务阈值）
COORD_ATOL: float = 1e-6
# 线性拉伸默认百分位（算法参数，可由调用方覆盖）
STRETCH_LOW_PCT: float = 2.0
STRETCH_HIGH_PCT: float = 98.0
STRETCH_MAX: int = 255

__all__ = [
    "GridMismatchError",
    "assert_grid_aligned",
    "linear_stretch",
    "true_color",
    "false_color",
    "rgb_to_hwc",
]


class GridMismatchError(Exception):
    """双时相左上角 / 尺寸 / CRS 任一不一致 → EX4，拒绝互算（REQ-03）。"""

    code = "EX4"

    def __init__(self, message: str, fields: Optional[Sequence[str]] = None):
        super().__init__(message)
        self.fields = tuple(fields or ())


def _transform_six(transform) -> Optional[Tuple[float, ...]]:
    """把 Affine 或 6/9 元组统一成 (a, b, c, d, e, f)。"""
    if transform is None:
        return None
    if hasattr(transform, "a"):
        return (
            float(transform.a), float(transform.b), float(transform.c),
            float(transform.d), float(transform.e), float(transform.f),
        )
    values = tuple(float(v) for v in transform[:6])
    if len(values) != 6:
        raise ValueError("transform 需为 Affine 或 6/9 元素序列")
    return values


def _crs_key(crs) -> Optional[str]:
    """CRS 归一化为字符串，避免 CRS 对象 / 字符串 / 字典写法差异误判。"""
    if crs is None:
        return None
    try:
        from rasterio.crs import CRS

        return CRS.from_user_input(crs).to_string()
    except Exception:
        return str(crs)


def assert_grid_aligned(profile_t1: dict, profile_t2: dict) -> bool:
    """校验双时相网格：左上角(c/f)、尺寸(width/height)、CRS 三者必须一致。

    同时校验仿射分辨率/旋转系数（同属"网格"语义）。任一不一致抛
    :class:`GridMismatchError`（EX4）；一致返回 True。
    """
    t1 = _transform_six(profile_t1.get("transform"))
    t2 = _transform_six(profile_t2.get("transform"))
    mismatched = []

    if t1 is None or t2 is None:
        mismatched.append("transform 缺失")
    else:
        names = ("transform.a", "transform.b", "transform.c(左上角X)",
                 "transform.d", "transform.e", "transform.f(左上角Y)")
        for i, name in enumerate(names):
            if not math.isclose(t1[i], t2[i], rel_tol=0.0, abs_tol=COORD_ATOL):
                mismatched.append(f"{name}: {t1[i]} vs {t2[i]}")

    for field in ("width", "height"):
        if profile_t1.get(field) != profile_t2.get(field):
            mismatched.append(f"{field}: {profile_t1.get(field)} vs {profile_t2.get(field)}")

    c1, c2 = _crs_key(profile_t1.get("crs")), _crs_key(profile_t2.get("crs"))
    if c1 != c2:
        mismatched.append(f"crs: {c1} vs {c2}")

    if mismatched:
        raise GridMismatchError(
            f"{ERROR_MESSAGES['EX4']}；不一致字段：{'; '.join(mismatched)}",
            fields=mismatched,
        )
    return True


def linear_stretch(
    band,
    low_pct: float = STRETCH_LOW_PCT,
    high_pct: float = STRETCH_HIGH_PCT,
    valid_mask: Optional[np.ndarray] = None,
) -> np.ndarray:
    """线性百分位拉伸到 uint8（2%–98% 截断），无效/NaN 像元置 0。

    纯函数：常量数组（无对比度）时输出全 0，不引入随机数。
    """
    arr = np.asarray(band, dtype=np.float32)
    finite = np.isfinite(arr)
    if valid_mask is not None:
        vm = np.asarray(valid_mask, dtype=bool)
        if vm.shape != arr.shape:
            raise ValueError(f"valid_mask 形状 {vm.shape} 与波段 {arr.shape} 不一致")
        finite &= vm

    out = np.zeros(arr.shape, dtype=np.uint8)
    if not finite.any():
        return out

    vals = arr[finite]
    lo = float(np.percentile(vals, low_pct))
    hi = float(np.percentile(vals, high_pct))
    if not (np.isfinite(lo) and np.isfinite(hi)) or hi <= lo:
        return out

    scaled = (arr - lo) / (hi - lo) * float(STRETCH_MAX)
    scaled = np.clip(scaled, 0.0, float(STRETCH_MAX))
    out = np.where(finite, scaled, 0.0).astype(np.uint8)
    return out


def _stack_rgb(bands: Sequence[np.ndarray], valid_mask: Optional[np.ndarray],
               low_pct: float, high_pct: float) -> np.ndarray:
    """按波段优先顺序 (3, H, W) 堆叠，便于 rasterio 直接写出。"""
    stretched = [
        linear_stretch(b, low_pct=low_pct, high_pct=high_pct, valid_mask=valid_mask)
        for b in bands
    ]
    return np.stack(stretched, axis=0)


def true_color(red, green, blue, valid_mask: Optional[np.ndarray] = None,
               low_pct: float = STRETCH_LOW_PCT,
               high_pct: float = STRETCH_HIGH_PCT) -> np.ndarray:
    """真彩合成 R=红(B04) / G=绿(B03) / B=蓝(B02)，返回 (3,H,W) uint8。"""
    return _stack_rgb((red, green, blue), valid_mask, low_pct, high_pct)


def false_color(nir, red, green, valid_mask: Optional[np.ndarray] = None,
                low_pct: float = STRETCH_LOW_PCT,
                high_pct: float = STRETCH_HIGH_PCT) -> np.ndarray:
    """假彩合成 R=近红外(B08) / G=红(B04) / B=绿(B03)，返回 (3,H,W) uint8。"""
    return _stack_rgb((nir, red, green), valid_mask, low_pct, high_pct)


def rgb_to_hwc(rgb_band_first: np.ndarray) -> np.ndarray:
    """(3,H,W) → (H,W,3)，供 PIL 等行优先库使用。"""
    return np.moveaxis(np.asarray(rgb_band_first), 0, -1)
