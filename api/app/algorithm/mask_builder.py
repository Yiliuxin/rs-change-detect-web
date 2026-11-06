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
from app.config.constants import (
    LANDSAT_NODATA_DN,
    QA_PIXEL_BIT_FILL,
    QA_PIXEL_CLOUD_BITS,
    QA_PIXEL_INVALID_BITS,
    QA_PIXEL_SHADOW_BITS,
    QA_PIXEL_SNOW_BITS,
)

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


# --------------------------------------------------------------------------- #
# Landsat Collection 2 Level-2 QA_PIXEL 位解析（REQ-04 / REQ-05）
# --------------------------------------------------------------------------- #
# 重投影未覆盖区域的填充值：只置 bit0(Fill)，使之外于有效区（SCL 的 0 在 QA_PIXEL
# 中恰好是"全清"，不能沿用 resample_scl 的填 0 策略，否则无效区会被判为有效）。
QA_PIXEL_FILL_VALUE: int = 1 << QA_PIXEL_BIT_FILL


def qa_pixel_bit_mask(qa_aligned, bits) -> np.ndarray:
    """QA_PIXEL 位组合解析：命中任一给定位即 True（bit 编号从 0 起）。"""
    arr = np.asarray(qa_aligned).astype(np.uint32, copy=False)
    out = np.zeros(arr.shape, dtype=bool)
    for bit in bits:
        out |= (arr & (np.uint32(1) << np.uint32(int(bit)))) != 0
    return out


def landsat_dn_nodata_mask(dn_bands: dict, band_keys) -> Optional[np.ndarray]:
    """Landsat SR 波段 ``DN == LANDSAT_NODATA_DN(0)`` 归 NODATA（任一波段命中即无效）。

    先掩膜后缩放（NF4）：本函数只产出掩膜，绝不改动 DN 或做定标。
    """
    out: Optional[np.ndarray] = None
    for key in band_keys:
        arr = np.asarray(dn_bands[key])
        hit = arr == LANDSAT_NODATA_DN
        out = hit if out is None else (out | hit)
    return out


def resample_qa_pixel(qa_path, ref_profile: dict) -> np.ndarray:
    """把 QA_PIXEL 对齐到参考网格（同网格直读，异网格最近邻重投影）。

    返回 ``uint32``，避免把 QA_PIXEL 的 16 位位域截断成 uint8；未覆盖区域填
    ``QA_PIXEL_FILL_VALUE``（仅置 Fill 位）而不是 0（0 = 全清，会被误判为有效）。
    """
    dst_shape = (int(ref_profile["height"]), int(ref_profile["width"]))
    with open_cog(qa_path) as src:
        same_grid = (
            (src.height, src.width) == dst_shape
            and tuple(float(v) for v in src.transform[:6])
            == tuple(float(v) for v in ref_profile["transform"][:6])
            and str(src.crs) == str(ref_profile["crs"])
        )
        if same_grid:
            return src.read(1).astype(np.uint32)
        dst = np.full(dst_shape, QA_PIXEL_FILL_VALUE, dtype=np.uint32)
        reproject(
            source=rasterio.band(src, 1),
            destination=dst,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=ref_profile["transform"],
            dst_crs=ref_profile["crs"],
            resampling=Resampling.nearest,
        )
        return dst


def build_qa_pixel_masks(
    qa_aligned,
    nodata_mask: Optional[np.ndarray] = None,
) -> MaskSet:
    """由（已对齐的）QA_PIXEL 位域构建云 / 云影 / 雪 / 无效 / 有效掩膜。

    bit0 Fill / bit1 Dilated Cloud / bit3 Cloud / bit4 Cloud Shadow / bit5 Snow
    **任一命中即判无效**（``QA_PIXEL_INVALID_BITS`` 取自常量源）；bit2 cirrus /
    bit6 clear / bit7 water 不参与判定。分组统计与 SCL 版本同口径。
    """
    qa = np.asarray(qa_aligned)
    cloud = qa_pixel_bit_mask(qa, QA_PIXEL_CLOUD_BITS)
    cloud_shadow = qa_pixel_bit_mask(qa, QA_PIXEL_SHADOW_BITS)
    snow = qa_pixel_bit_mask(qa, QA_PIXEL_SNOW_BITS)
    invalid = qa_pixel_bit_mask(qa, QA_PIXEL_INVALID_BITS)
    if nodata_mask is not None:
        invalid = invalid | np.asarray(nodata_mask, dtype=bool)
    valid = ~invalid
    return MaskSet(cloud=cloud, cloud_shadow=cloud_shadow, snow=snow,
                   invalid=invalid, valid=valid)
