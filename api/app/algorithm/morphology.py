# -*- coding: utf-8 -*-
"""形态学去碎块（HLD 模块3 功能二，REQ-07 / NF5 可解释后处理）。

纯函数：对 1 增强 / 2 退化 / 3 稳定 **三类标签分别**做「去小连通域 → 开运算 →
闭运算」，再按「变化类优先于稳定类」的确定性顺序合并；0 恒为 nodata，不被
闭运算回填成有效类，三类编码 1/2/3 不被改写。
"""
from __future__ import annotations

from typing import Optional

import numpy as np
from skimage.morphology import closing, disk, opening, remove_small_objects, square

from app.algorithm.change_detector import (
    CHANGE_CODES,
    CHANGE_DEGRADED,
    CHANGE_ENHANCED,
    CHANGE_NODATA,
    CHANGE_STABLE,
)
from app.config.constants import MIN_SAMPLES_PER_CLASS

# 最小连通像元数（形态学参数）：由精度最小样本量导出，避免另立魔法数字
DEFAULT_MIN_SIZE: int = int(MIN_SAMPLES_PER_CLASS) // 2 + 1
DEFAULT_RADIUS: int = 1
CONNECTIVITY: int = 2

__all__ = ["DEFAULT_MIN_SIZE", "make_footprint", "clean_change_map"]


def make_footprint(kind: str = "square", radius: int = DEFAULT_RADIUS) -> np.ndarray:
    """构造结构元：``square`` → square(2r+1)；``disk`` → disk(r)。"""
    r = max(1, int(radius))
    key = str(kind).strip().lower()
    if key == "square":
        return square(2 * r + 1)
    if key == "disk":
        return disk(r)
    raise ValueError(f"不支持的结构元类型：{kind!r}，仅支持 square / disk")


def clean_change_map(
    change,
    min_size: int = DEFAULT_MIN_SIZE,
    footprint: Optional[np.ndarray] = None,
    connectivity: int = CONNECTIVITY,
) -> np.ndarray:
    """三类分别去噪后合并，返回 uint8（0 无效 / 1 增强 / 2 退化 / 3 稳定）。

    合并规则（确定性、无随机）：
      1) 以形态学后的稳定层为底；
      2) 有效区内形态学产生的空洞补为 3 稳定（不产生新的 0）；
      3) 2 退化、1 增强依次覆盖，变化时优先保留变化类；
      4) 原无效像元强制回 0，保证 0 保持无效。
    """
    arr = np.asarray(change)
    fp = make_footprint() if footprint is None else np.asarray(footprint, dtype=bool)
    size = max(1, int(min_size))

    invalid = arr == CHANGE_NODATA
    valid_area = ~invalid
    layers = {}
    for code in CHANGE_CODES:
        binary = arr == code
        binary = remove_small_objects(binary, min_size=size, connectivity=connectivity)
        binary = opening(binary, footprint=fp)
        binary = closing(binary, footprint=fp)
        layers[code] = binary

    out = np.zeros(arr.shape, dtype=np.uint8)
    out[layers[CHANGE_STABLE]] = CHANGE_STABLE
    out[valid_area & (out == CHANGE_NODATA)] = CHANGE_STABLE
    out[layers[CHANGE_DEGRADED]] = CHANGE_DEGRADED
    out[layers[CHANGE_ENHANCED]] = CHANGE_ENHANCED
    out[invalid] = CHANGE_NODATA
    return out
