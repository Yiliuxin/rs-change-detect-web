# -*- coding: utf-8 -*-
"""分层随机抽样（REQ-09 自动化 / 半自动流程，HLD 模块4 辅助）。

纯 numpy 函数：读变化检测结果栅格的三类编码（1 增强 / 2 退化 / 3 稳定，
编码与中文类别一律取 ``change_detector.CHANGE_CODES`` / ``CHANGE_LABELS_ZH``
与 ``constants`` 的抽样上限，不在此处写死），用 ``numpy.random.RandomState(seed)``
对每类独立无放回抽 ``per_class`` 个像元。

可用像元不足时如实返回实际数（不报错、不凑数、不重复抽样）；某类为 0 时
该类样本为空、``per_class_actual`` 记 0，供前端提示用户手工补点。
只返回 (row, col, code)，不做经纬度换算、不连库、不写 sample 表。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import numpy as np

from app.algorithm.change_detector import CHANGE_CODES, CHANGE_LABELS_ZH
from app.config.constants import AUTO_SAMPLE_PER_CLASS_MAX

__all__ = ["stratified_pixels"]


def stratified_pixels(
    change,
    per_class: int,
    seed: Optional[int] = None,
) -> Tuple[List[Tuple[int, int, int]], Dict[str, int]]:
    """按三类编码分层抽样，返回 ``[(row, col, code)]`` 与每类实际抽样数。

    ``seed`` 相同且栅格不变时结果逐字相同（RandomState 可复现，REQ-14）。
    """
    if (isinstance(per_class, bool) or not isinstance(per_class, int)
            or not (0 < per_class <= AUTO_SAMPLE_PER_CLASS_MAX)):
        raise ValueError(
            f"per_class 需为 (0, {AUTO_SAMPLE_PER_CLASS_MAX}] 内整数，实际 {per_class!r}"
        )
    arr = np.asarray(change)
    rng = np.random.RandomState(seed)

    picks: List[Tuple[int, int, int]] = []
    actual: Dict[str, int] = {}
    for code in CHANGE_CODES:
        coords = np.argwhere(arr == code)  # (row, col)，按行优先稳定排序
        available = int(coords.shape[0])
        take = min(per_class, available)
        actual[CHANGE_LABELS_ZH[code]] = take
        if take == 0:
            continue
        chosen = rng.choice(available, size=take, replace=False)
        for row, col in coords[chosen]:
            picks.append((int(row), int(col), int(code)))
    return picks, actual
