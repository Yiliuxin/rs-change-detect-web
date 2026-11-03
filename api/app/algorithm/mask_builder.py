# -*- coding: utf-8 -*-
"""云 / 云影 / 雪 / 无效值掩膜构建（HLD 模块2 功能二，REQ-05）。

要点：Sentinel-2 的 SCL 为**半分辨率**（20 m），必须先重采样对齐到全分辨率（10 m）网格后
再生成掩膜，避免"先缩放后掩膜"导致的像元错位（HLD 2.2.2.2 功能二 / NF4）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Union

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import reproject

from app.algorithm.cog_reader import open_cog

# Sentinel-2 Scene Classification Layer（SCL）类别定义
SCL_NODATA = 0
SCL_SATURATED_OR_DEFECTIVE = 1
SCL_CLOUD_SHADOW = 3
SCL_CLOUD_MEDIUM_PROB = 8
SCL_CLOUD_HIGH_PROB = 9
SCL_THIN_CIRRUS = 10
SCL_SNOW_OR_ICE = 11

# 参与掩膜的类别分组
SCL_INVALID_VALUES = (SCL_NODATA, SCL_SATURATED_OR_DEFECTIVE)
SCL_CLOUD_VALUES = (SCL_CLOUD_MEDIUM_PROB, SCL_CLOUD_HIGH_PROB, SCL_THIN_CIRRUS)
SCL_SHADOW_VALUES = (SCL_CLOUD_SHADOW,)
SCL_SNOW_VALUES = (SCL_SNOW_OR_ICE,)


@dataclass(frozen=True)
class MaskSet:
    """掩膜集合：True 表示命中该类。"""

    cloud: np.ndarray
    cloud_shadow: np.ndarray
    snow: np.ndarray
    invalid: np.ndarray
    valid: np.ndarray

    def counts(self) -> dict:
        """各类命中像元数统计。"""
        return {
            "total": int(self.valid.size),
            "cloud": int(self.cloud.sum()),
            "cloud_shadow": int(self.cloud_shadow.sum()),
            "snow": int(self.snow.sum()),
            "invalid": int(self.invalid.sum()),
            "valid": int(self.valid.sum()),
        }


def _in(values, arr: np.ndarray) -> np.ndarray:
    mask = np.zeros(arr.shape, dtype=bool)
    for v in values:
        mask |= arr == v
    return mask


def resample_scl(
    scl_path,
    ref_profile: dict,
    resampling: Resampling = Resampling.nearest,
) -> np.ndarray:
    """把半分辨率 SCL 重采样对齐到参考（全分辨率）网格。

    参数：
        scl_path    ：SCL 文件路径
        ref_profile ：参考栅格 profile（需含 width / height / transform / crs）
    返回：与参考网格同形的 uint8 SCL 数组，未覆盖区域填 0（SCL nodata → 判为无效）
    """
    dst_shape = (int(ref_profile["height"]), int(ref_profile["width"]))
    dst = np.zeros(dst_shape, dtype=np.uint8)

    with open_cog(scl_path) as src:
        # 注意：必须在数据集打开期间完成重投影，rasterio.band 句柄随数据集关闭而失效
        reproject(
            source=rasterio.band(src, 1),
            destination=dst,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=ref_profile["transform"],
            dst_crs=ref_profile["crs"],
            resampling=resampling,
        )
    return dst


def build_masks(
    scl_aligned: np.ndarray,
    nodata_mask: Optional[np.ndarray] = None,
) -> MaskSet:
    """由（已对齐的）SCL 数组构建云 / 云影 / 雪 / 无效值掩膜与有效掩膜。

    参数：
        scl_aligned：与全分辨率波段同形的 SCL 数组
        nodata_mask：可选的波段 NODATA 掩膜（True 表示无数据），并入 invalid
    """
    scl_aligned = np.asarray(scl_aligned)

    cloud = _in(SCL_CLOUD_VALUES, scl_aligned)
    cloud_shadow = _in(SCL_SHADOW_VALUES, scl_aligned)
    snow = _in(SCL_SNOW_VALUES, scl_aligned)
    invalid = _in(SCL_INVALID_VALUES, scl_aligned)

    if nodata_mask is not None:
        invalid = invalid | np.asarray(nodata_mask, dtype=bool)

    valid = ~(cloud | cloud_shadow | snow | invalid)
    return MaskSet(cloud=cloud, cloud_shadow=cloud_shadow, snow=snow, invalid=invalid, valid=valid)
