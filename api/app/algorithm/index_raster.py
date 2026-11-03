# -*- coding: utf-8 -*-
"""指数栅格值对象（HLD 模块3，REQ-06 / REQ-14）。

把「指数数组 + 对应 profile + 参数快照」打包为不可变值对象，供 service 层与
报告层复用；本身仍是纯函数（无 IO、无随机、无数据库）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

import numpy as np

from app.algorithm.index_calculator import clip_index, ndvi, ndwi

__all__ = ["IndexRaster", "build_index_raster"]


@dataclass(frozen=True)
class IndexRaster:
    """指数栅格值对象：data 为 float32（无效像元 NaN）。"""

    data: np.ndarray
    profile: dict
    name: str = "NDVI"
    valid_mask: Optional[np.ndarray] = None
    run_id: str = ""
    params_snapshot: Dict[str, object] = field(default_factory=dict)

    @property
    def shape(self) -> Tuple[int, ...]:
        return tuple(self.data.shape)

    @property
    def finite_mask(self) -> np.ndarray:
        return np.isfinite(self.data)

    def stats(self) -> Dict[str, object]:
        """有效像元的数值摘要（min / max / mean / count）。"""
        vals = self.data[np.isfinite(self.data)]
        if vals.size == 0:
            return {"name": self.name, "count": 0, "min": None, "max": None, "mean": None}
        return {
            "name": self.name,
            "count": int(vals.size),
            "min": float(vals.min()),
            "max": float(vals.max()),
            "mean": float(vals.mean()),
        }

    def as_dict(self) -> Dict[str, object]:
        """仅导出元数据（不含大数组），用于报告与参数快照。"""
        return {
            "name": self.name,
            "shape": list(self.shape),
            "run_id": self.run_id,
            "params_snapshot": dict(self.params_snapshot),
            "stats": self.stats(),
        }


def build_index_raster(
    band_a,
    band_b,
    profile: dict,
    valid_mask: Optional[np.ndarray] = None,
    index: str = "NDVI",
    clip: bool = True,
    run_id: str = "",
    params_snapshot: Optional[dict] = None,
) -> IndexRaster:
    """由反射率波段构建指数栅格。

    参数：
        band_a / band_b：NDVI 时为 (red, nir)，NDWI 时为 (green, nir)
        profile         ：``cog_reader.read_aoi`` 返回的（已按窗口重算）profile
        index           ："NDVI" / "NDWI"
        clip            ：是否约束回物理定义域 [-1, 1]（默认 True，见 clip_index）
        run_id / params_snapshot：可复现三要素（REQ-14），只进入元数据
    """
    key = str(index).strip().upper()
    if key == "NDVI":
        arr = ndvi(band_a, band_b, valid_mask=valid_mask)
    elif key == "NDWI":
        arr = ndwi(band_a, band_b, valid_mask=valid_mask)
    else:
        raise ValueError(f"不支持的指数：{index!r}，仅支持 NDVI / NDWI")

    if clip:
        arr = clip_index(arr)

    return IndexRaster(
        data=arr,
        profile=dict(profile),
        name=key,
        valid_mask=None if valid_mask is None else np.asarray(valid_mask, dtype=bool),
        run_id=run_id,
        params_snapshot=dict(params_snapshot or {}),
    )
