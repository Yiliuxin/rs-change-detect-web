# -*- coding: utf-8 -*-
"""COG 窗口读取（HLD 模块1 功能二，REQ-03）。

封装 rasterio：只暴露窗口读取与 AOI 裁切两个纯函数能力，不连数据库、不写路由。
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator, Optional, Tuple, Union

import numpy as np
import rasterio
from rasterio.windows import Window, transform as window_transform

from app.algorithm.geotransform import BBox, aoi_window

PathLike = Union[str, os.PathLike]


@contextmanager
def open_cog(path: PathLike) -> Iterator[rasterio.DatasetReader]:
    """打开 COG（上下文管理器，退出自动关闭）。"""
    ds = rasterio.open(path)
    try:
        yield ds
    finally:
        ds.close()


def get_profile(path: PathLike) -> dict:
    """读取 COG 的 profile（crs / transform / 尺寸 / dtype）。"""
    with open_cog(path) as ds:
        return dict(ds.profile)


def describe(path: PathLike) -> dict:
    """返回 COG 关键元数据摘要，便于日志与自测报告。"""
    with open_cog(path) as ds:
        return {
            "path": str(path),
            "width": ds.width,
            "height": ds.height,
            "crs": str(ds.crs),
            "dtype": ds.dtypes[0],
            "nodata": ds.nodata,
            "res": ds.res,
            "bounds": tuple(ds.bounds),
        }


def read_window(
    ds: rasterio.DatasetReader,
    window: Optional[Window] = None,
    indexes: Optional[int] = 1,
    fill_value: int = 0,
) -> np.ndarray:
    """按窗口读取数组；window 为 None 时读取整幅。"""
    return ds.read(indexes, window=window, boundless=True, fill_value=fill_value)


def read_aoi(
    path: PathLike,
    bbox: BBox,
    indexes: Optional[int] = 1,
    fill_value: int = 0,
) -> Tuple[np.ndarray, dict, Window]:
    """按 WGS84 AOI 裁切读取，返回 (数组, profile, 窗口)。

    profile 已按窗口重算 width / height / transform，可直接用于后续写出与掩膜对齐。
    """
    with open_cog(path) as ds:
        window = aoi_window(ds, bbox)
        arr = read_window(ds, window=window, indexes=indexes, fill_value=fill_value)
        profile = dict(ds.profile)
        profile.update(
            width=int(window.width),
            height=int(window.height),
            transform=window_transform(window, ds.transform),
        )
        return arr, profile, window
