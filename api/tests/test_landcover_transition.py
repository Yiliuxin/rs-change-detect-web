# -*- coding: utf-8 -*-
"""A3 地物联合判据 / 4×4 转移矩阵 纯函数单测。

覆盖：四类边界（含 NDWI=0、NDVI=0.30、NDVI=0.15 边界）、顺序优先
（NDWI>0 且 NDVI=0.8 → 水体）、3×3 手算逐格核对、矩阵行列合计与
有效像元数一致、非对角 Top5 不含对角、经验阈值文案取自常量源。

断言一律按 key / 坐标逐项校验，不依赖列表长度。
"""
from __future__ import annotations

import numpy as np
import pytest

from app.algorithm.landcover import (
    classify_landcover, classify_landcover_scalar, transition_matrix,
)
from app.config.constants import (
    LANDCOVER_CODES, LANDCOVER_NODATA, LANDCOVER_NOTE,
    NDVI_BARE_LT, NDVI_VEGETATION_GE, NDWI_WATER_GT, TRANSITION_TOP_N,
)


def test_landcover_four_class_boundaries():
    """NDWI>0 水体 / NDVI≥0.30 植被 / NDVI<0.15 裸土 / 其余其他 / 无效 0。"""
    ndvi = np.array([0.80, 0.30, 0.15, 0.149, 0.20, -0.20, np.nan],
                    dtype=np.float32)
    ndwi = np.full(7, -0.10, dtype=np.float32)
    expected = {0: 2, 1: 2, 2: 4, 3: 3, 4: 4, 5: 3, 6: LANDCOVER_NODATA}
    arr = classify_landcover(ndvi, ndwi)
    for i, want in expected.items():
        assert int(arr[i]) == want, (i, float(ndvi[i]), int(arr[i]), want)
        assert classify_landcover_scalar(ndvi[i], ndwi[i]) == want, i
    assert NDWI_WATER_GT == 0.0
    assert NDVI_VEGETATION_GE == 0.30
    assert NDVI_BARE_LT == 0.15


def test_landcover_water_takes_priority():
    """顺序判断先命中先归类：NDWI>0 即使 NDVI=0.8 也判水体。"""
    assert classify_landcover_scalar(0.80, 0.10) == 1
    assert int(classify_landcover(np.array([[0.80]], np.float32),
                                  np.array([[0.01]], np.float32))[0, 0]) == 1
    # 严格大于 0：NDWI=0 不判水体，落入 NDVI≥0.30 → 植被
    assert classify_landcover_scalar(0.80, 0.0) == 2
    # valid_mask=False / NaN → 0
    assert classify_landcover_scalar(np.nan, -0.1) == 0
    assert classify_landcover_scalar(0.8, 0.5, valid=False) == 0
    mask = np.array([[False]], dtype=bool)
    assert int(classify_landcover(np.array([[0.8]], np.float32),
                                  np.array([[0.5]], np.float32),
                                  valid_mask=mask)[0, 0]) == 0


def test_landcover_3x3_hand_computed():
    """3×3 逐格手算核对（按 (row,col) key 断言，不依赖列表长度）。"""
    ndvi = np.array([
        [0.80, 0.30, 0.15],
        [0.14, 0.20, 0.50],
        [-0.20, np.nan, 0.30],
    ], dtype=np.float32)
    ndwi = np.array([
        [0.10, -0.10, -0.10],
        [-0.10, -0.10, 0.00],
        [-0.10, -0.10, 0.50],
    ], dtype=np.float32)
    expected = {
        (0, 0): 1,  # NDWI>0 优先于 NDVI=0.8 → 水体
        (0, 1): 2,  # NDVI=0.30 边界 → 植被
        (0, 2): 4,  # NDVI=0.15 边界（既不 ≥0.30 也不 <0.15）→ 其他
        (1, 0): 3,  # NDVI=0.14 < 0.15 → 裸土/建成
        (1, 1): 4,  # 0.15 ≤ NDVI < 0.30 → 其他
        (1, 2): 2,  # NDWI=0 不判水体 → NDVI=0.50 → 植被
        (2, 0): 3,
        (2, 1): 0,  # NaN → 无效
        (2, 2): 1,  # NDWI>0 优先于 NDVI=0.30 → 水体
    }
    arr = classify_landcover(ndvi, ndwi)
    for (r, c), want in expected.items():
        assert int(arr[r, c]) == want, ((r, c), int(arr[r, c]), want)


def test_transition_matrix_3x3_hand_computed():
    """3×3 手算 4×4 矩阵：行列合计 == 有效像元数；Top 不含对角。"""
    lc_t1 = np.array([[1, 2, 3], [4, 1, 2], [3, 4, 0]], dtype=np.uint8)
    lc_t2 = np.array([[1, 1, 2], [2, 3, 3], [4, 4, 2]], dtype=np.uint8)
    tr = transition_matrix(lc_t1, lc_t2, pixel_area_m2=100.0)

    assert tr["codes"] == [int(c) for c in LANDCOVER_CODES] == [1, 2, 3, 4]
    expected_matrix = {
        0: [1, 0, 1, 0],  # t1=水体
        1: [1, 0, 1, 0],  # t1=植被
        2: [0, 1, 0, 1],  # t1=裸土/建成
        3: [0, 1, 0, 1],  # t1=其他
    }
    for i, row in expected_matrix.items():
        assert tr["pixel_matrix"][i] == row, (i, tr["pixel_matrix"][i], row)
        for j, count in enumerate(row):
            assert tr["area_matrix_ha"][i][j] == pytest.approx(
                count * 100.0 / 10000.0), (i, j)

    assert tr["valid_count"] == 8
    assert tr["unchanged_count"] == 2
    assert tr["unchanged_ratio"] == pytest.approx(0.25)
    assert tr["row_totals"] == [2, 2, 2, 2]
    assert tr["col_totals"] == [2, 2, 2, 2]
    assert sum(tr["row_totals"]) == tr["valid_count"]
    assert sum(tr["col_totals"]) == tr["valid_count"]
    assert sum(sum(r) for r in tr["pixel_matrix"]) == tr["valid_count"]

    top = tr["top"]
    assert len(top) <= TRANSITION_TOP_N == 5
    assert all(int(t["from"]) != int(t["to"]) for t in top), top
    for t in top:
        assert t["from_label"] and t["to_label"]
        assert t["pixel_count"] == 1
        assert t["area_ha"] == pytest.approx(100.0 / 10000.0)
    assert tr["note"] == LANDCOVER_NOTE == "经验阈值，非物候标定"
    assert tr["criteria"]["ndwi_water_gt"] == NDWI_WATER_GT
    assert tr["criteria"]["ndvi_vegetation_ge"] == NDVI_VEGETATION_GE
    assert tr["criteria"]["ndvi_bare_lt"] == NDVI_BARE_LT


def test_transition_matrix_empty_and_valid_mask():
    """全无效 / valid_mask 全 False → 0 矩阵、占比 0、Top 空，不抛异常。"""
    lc = np.full((3, 3), 4, dtype=np.uint8)
    empty = transition_matrix(np.zeros((3, 3), dtype=np.uint8), lc, 100.0)
    assert empty["valid_count"] == 0
    assert empty["unchanged_ratio"] == 0.0
    assert empty["top"] == []
    assert sum(sum(r) for r in empty["pixel_matrix"]) == 0

    masked = transition_matrix(lc, lc, 100.0,
                               valid_mask=np.zeros((3, 3), dtype=bool))
    assert masked["valid_count"] == 0
    assert masked["unchanged_ratio"] == 0.0
